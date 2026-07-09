"""EdgeMath Layer 1-3 — continuous edge-based position sizing (PURE, no I/O).

Replaces binary entry gates with continuous size scaling: a strategy sizes UP
when its own rolling edge is fat and the day is green, and fades toward ZERO
when the edge decays or the day turns red — smoothly, never a hard block.

Three layers (all pure so the live monitor and the OOS backtester share one code
path — the RES design rule):

  L1  signal  -> edge score : expectancy + payoff ratio from the strategy's OWN
                              recent CLOSED trades (fed from trade_attribution).
  L2  edge    -> base size  : fractional-Kelly conviction x volatility-target risk.
  L3  day P&L -> governor   : green-day compounding + red-day de-risking + a
                              profit ratchet, so the day's loss stays bounded and
                              asymmetric versus its profit.

Honest scope: this does NOT guarantee a green day (variance forbids that). It
targets E[daily P&L] > 0 with a compressed, asymmetric loss tail — small capped
losses, larger let-run wins. The asymmetry is what keeps profit >= loss over any
stretch, not a promise on any single trade.

Nothing here reads a DB or the network; callers pass primitives. The I/O adapters
(EM-2 rolling stats, EM-3 market_context) live outside this module. Everything is
env-tunable via EDGE_* so behaviour can be dialled without a code change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

# ── Config (env-tunable) ─────────────────────────────────────────────────────────
# Reference Kelly fraction treated as "full conviction" (conviction saturates at 1.0
# once raw Kelly f reaches this). Deliberately small — full-Kelly assumes W and b are
# known exactly; they are ESTIMATES, so we scale off a conservative reference.
EDGE_KELLY_REF = float(os.environ.get("EDGE_KELLY_REF", "0.15"))
# Volatility target: fraction of equity risked on ONE trade at full conviction.
EDGE_RISK_PER_TRADE_PCT = float(os.environ.get("EDGE_RISK_PER_TRADE_PCT", "0.004"))  # 0.4%
# Day-governor multiplier bounds and the profit-ratchet giveback threshold.
EDGE_DAY_GOV_MIN = float(os.environ.get("EDGE_DAY_GOV_MIN", "0.25"))
EDGE_DAY_GOV_MAX = float(os.environ.get("EDGE_DAY_GOV_MAX", "1.5"))
EDGE_DAY_GOV_GIVEBACK = float(os.environ.get("EDGE_DAY_GOV_GIVEBACK", "0.5"))
# Below this many closed trades we can't trust the edge estimate → trade small
# ("cold start") to gather data rather than block.
EDGE_MIN_TRADES = int(os.environ.get("EDGE_MIN_TRADES", "10"))
EDGE_COLDSTART_CONVICTION = float(os.environ.get("EDGE_COLDSTART_CONVICTION", "0.5"))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ── L1: rolling edge statistics ──────────────────────────────────────────────────

@dataclass
class RollingStats:
    n: int                 # number of closed trades in the window
    win_rate: float        # W in [0,1]
    avg_win: float         # mean winning P&L (>= 0)
    avg_loss: float        # mean losing P&L MAGNITUDE (>= 0)
    expectancy: float      # per-trade E = W*avg_win - (1-W)*avg_loss (rupees)


def rolling_stats_from_pnls(pnls: Sequence[float]) -> RollingStats:
    """Win rate, average win, average loss magnitude, and per-trade expectancy from
    a list of realised trade P&Ls (rupees; sign = win/loss). Break-even trades
    (P&L == 0) count toward n but are neither a win nor a loss."""
    vals = [float(p) for p in pnls]
    n = len(vals)
    if n == 0:
        return RollingStats(0, 0.0, 0.0, 0.0, 0.0)
    wins = [p for p in vals if p > 0]
    losses = [-p for p in vals if p < 0]  # store magnitudes
    win_rate = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = win_rate * avg_win - (1.0 - win_rate) * avg_loss
    return RollingStats(
        n=n,
        win_rate=round(win_rate, 4),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        expectancy=round(expectancy, 2),
    )


def payoff_ratio(avg_win: float, avg_loss: float) -> Optional[float]:
    """b = avg_win / avg_loss. None when no losses have been observed (unknown, not
    'infinite' — the caller treats None as 'edge unproven')."""
    if avg_loss <= 0:
        return None
    return avg_win / avg_loss


# ── L2: edge -> conviction and base size ─────────────────────────────────────────

def kelly_conviction(win_rate: float, b: Optional[float], *, ref: float = EDGE_KELLY_REF) -> float:
    """Conviction in [0,1] from the (fractional) Kelly fraction f = W - (1-W)/b.

    f <= 0  → 0.0   (no edge → do not add size)
    f >= ref → 1.0   (strong edge → full base size)
    linear in between. `ref` is the Kelly value treated as full conviction; keeping
    it small is the fractional-Kelly safety margin against estimation error."""
    if b is None or b <= 0 or ref <= 0:
        return 0.0
    f = win_rate - (1.0 - win_rate) / b
    return _clamp(f / ref, 0.0, 1.0)


def vol_target_base_lots(
    equity: float,
    per_lot_max_loss: float,
    *,
    risk_pct: float = EDGE_RISK_PER_TRADE_PCT,
) -> float:
    """Base lots such that one trade's defined risk ≈ risk_pct of equity. Returns a
    float (the caller rounds after applying conviction/day multipliers). 0 when
    inputs are unusable — never raises."""
    if equity <= 0 or per_lot_max_loss <= 0 or risk_pct <= 0:
        return 0.0
    return (risk_pct * equity) / per_lot_max_loss


# ── L3: day-P&L governor ─────────────────────────────────────────────────────────

def day_governor_mult(
    day_pnl: float,
    daily_risk_budget: float,
    peak_day_pnl: Optional[float] = None,
    *,
    m_min: float = EDGE_DAY_GOV_MIN,
    m_max: float = EDGE_DAY_GOV_MAX,
    giveback: float = EDGE_DAY_GOV_GIVEBACK,
) -> float:
    """Continuous size multiplier from the day's running P&L.

    m = clamp(1 + day_pnl / budget, m_min, m_max)
      green day  → m > 1  (let winners compound)
      red day    → m < 1  (each further trade is smaller → the hole self-limits;
                           the opposite of martingale — no revenge blow-up)
    Profit ratchet: once we've been up (peak_day_pnl > 0) and have given back more
    than `giveback` of that peak, clamp to m_min to protect the gains. Smooth, not a
    hard stop — the strategy keeps trading, just tiny."""
    if daily_risk_budget <= 0:
        return 1.0
    m = _clamp(1.0 + day_pnl / daily_risk_budget, m_min, m_max)
    if peak_day_pnl is not None and peak_day_pnl > 0 and day_pnl < peak_day_pnl * (1.0 - giveback):
        m = min(m, m_min)
    return round(m, 3)


# ── Combined decision ────────────────────────────────────────────────────────────

@dataclass
class EdgeSizeDecision:
    lots: int              # final lots to trade (>= floor_lots)
    base_lots: float       # vol-target base before edge/day scaling
    conviction: float      # L2 conviction in [0,1]
    day_mult: float        # L3 day-governor multiplier
    edge_score: float      # conviction * day_mult (composite, for telemetry)
    expectancy: float      # L1 per-trade expectancy that drove the call
    reason: str            # human-readable explanation


def edge_size(
    *,
    stats: RollingStats,
    payoff_b: Optional[float],
    equity: float,
    per_lot_max_loss: float,
    day_pnl: float,
    daily_risk_budget: float,
    peak_day_pnl: Optional[float] = None,
    costs_per_trade: float = 0.0,
    min_trades: int = EDGE_MIN_TRADES,
    floor_lots: int = 0,
) -> EdgeSizeDecision:
    """Full L1→L3 sizing decision. Pure. Never blocks — a dead edge or a deep red day
    just drives lots toward `floor_lots` (default 0 = stand down)."""
    base = vol_target_base_lots(equity, per_lot_max_loss)

    if stats.n < min_trades:
        # Not enough evidence to trust the edge → trade small to gather data, don't block.
        conviction = EDGE_COLDSTART_CONVICTION
        reason = f"cold-start n={stats.n}<{min_trades} → neutral conviction {conviction}"
    elif stats.expectancy <= costs_per_trade:
        # Edge is gone (after costs) → soft stand-down. This replaces the loss-streak GATE.
        conviction = 0.0
        reason = f"expectancy {stats.expectancy:.1f} ≤ costs {costs_per_trade:.1f} → stand down"
    else:
        conviction = kelly_conviction(stats.win_rate, payoff_b)
        _b = f"{payoff_b:.2f}" if payoff_b is not None else "n/a"
        reason = (f"edge ok E={stats.expectancy:.1f} W={stats.win_rate:.2f} "
                  f"b={_b} → conviction {conviction:.2f}")

    day_mult = day_governor_mult(day_pnl, daily_risk_budget, peak_day_pnl)
    raw = base * conviction * day_mult
    lots = max(int(floor_lots), int(round(raw)))
    return EdgeSizeDecision(
        lots=lots,
        base_lots=round(base, 3),
        conviction=round(conviction, 3),
        day_mult=day_mult,
        edge_score=round(conviction * day_mult, 3),
        expectancy=stats.expectancy,
        reason=reason,
    )


# ── L(exit): volatility-scaled TP/SL (light; EM-6 owns the wiring) ───────────────

def vol_scaled_exit_frac(
    base_tp_frac: float,
    base_sl_mult: float,
    vol_ratio: float,
    *,
    lo: float = 0.7,
    hi: float = 1.5,
) -> Dict[str, float]:
    """Scale the stop by the current-vs-reference volatility ratio so a stop isn't
    tripped by noise on a high-vol day (wider) nor left too loose on a calm one.
    `vol_ratio` = current sigma / reference sigma. TP fraction (of credit) is left
    as-is; only the stop breathes. Clamped to [lo, hi]. EM-6 refines/wires this."""
    k = _clamp(float(vol_ratio), lo, hi)
    return {"tp_frac": round(float(base_tp_frac), 4), "sl_mult": round(float(base_sl_mult) * k, 4)}

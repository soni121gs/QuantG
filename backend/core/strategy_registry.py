"""Strategy Registry — config-as-data foundation (QGX / INFRA-11..12).

The `strategies` collection is ALREADY the book's data (RAE/custom rows are DB-only).
What was missing is a *validated* view of the config projection + a coherence checker
that catches the geometry-drift bug classes we keep hitting by hand:

  - hold-to-expiry TRAP: an intraday credit spread seeded with exit_mode="expiry" /
    time_exit_minutes=0 / no credit_tp_frac|sl → sits until square-off (the 2026-07-13
    RAE regression, and the QG-O1/O4 07-09 fix that never reached the new rows).
  - tp/sl fractions out of sane range; required_capital incoherent across options/risk.

This module is PURE (no I/O): callers pass a strategy doc dict, it returns a typed
projection + a list of coherence problems. It is the seed for INFRA-51's formal
schema registry and the validator behind the config-write path (INFRA-16).

Nothing here trades or mutates state — it validates config before it is trusted.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field


# ── typed config projection ────────────────────────────────────────────────
# extra="ignore": the real strategy doc carries ~90 runtime-state fields (last_*,
# counts, cache_*). We validate only the CONFIG projection and ignore the rest.

class OptionsGeometry(BaseModel):
    model_config = ConfigDict(extra="allow")
    structure: Optional[str] = None            # single_leg | credit_spread | debit_spread
    strike_mode: Optional[str] = None
    exit_mode: Optional[str] = None            # "" (intraday) | "expiry" (hold)
    credit_tp_frac: Optional[float] = None
    credit_sl_mult: Optional[float] = None
    required_capital: Optional[float] = None
    spread_width: Optional[float] = None
    wing_width: Optional[float] = None
    short_otm_pct: Optional[float] = None
    lots: Optional[int] = None
    candle_interval: Optional[str] = None
    owned_regimes: Optional[List[str]] = None
    specialist_role: Optional[str] = None


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    exit_mode: Optional[str] = None            # signal_or_tp_sl_trailing | hold_to_expiry
    time_exit_minutes: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    max_trades_day: Optional[int] = None
    daily_loss_limit: Optional[float] = None
    cooldown_minutes: Optional[float] = None
    required_capital: Optional[float] = None


class VisualConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    symbol: Optional[str] = None
    exchange: Optional[str] = None
    options: OptionsGeometry = Field(default_factory=OptionsGeometry)
    risk: RiskConfig = Field(default_factory=RiskConfig)


class StrategyConfig(BaseModel):
    """The config projection of a strategy doc — the registry's typed view."""
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    mode: str = "paper"
    status: str = "paused"
    required_capital: Optional[float] = None
    visual_config: VisualConfig = Field(default_factory=VisualConfig)


class ConfigValidation(BaseModel):
    ok: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


_SPREAD = ("credit_spread", "debit_spread")


# ERP Phase 0 source of truth for the book cutover. Kept rows are paused until
# they pass the rebuilt judge/forward-paper ladder; purge rows are archived in
# `data/archive/book_snapshot_2026-07/` before deletion from active config.
ERP_KEEP_STRATEGY_NAMES: Set[str] = {
    "QG-O1 NIFTY Put Spread Theta Core",
    "QG-O4 SENSEX Call Spread Range Pilot",
    "RAE NIFTY Range Seller (RANGE/INSIDE)",
    "RAE SENSEX Range Seller (RANGE/INSIDE)",
    "RAE NIFTY Trend Delta-1 (TREND)",
    "RAE BANKNIFTY Trend Delta-1 (TREND)",
    "RAE SENSEX Trend Delta-1 (TREND)",
    # founder IDX sleeves (index_alpha_sleeves; founder_forced_live) — kept, paper-forward
    "IDX NIFTY VRP Call-Spread (RANGE+rich)",
    "IDX SENSEX VRP Put-Spread (RANGE+rich)",
    "IDX NIFTY Long-Gamma (HIGH_VOL debit)",
    "IDX NIFTY Mean-Reversion Fade (debit)",
    # far-OTM put-spread tail hedge (long-vol §18; convex crash insurance, held to expiry)
    "Tail Hedge NIFTY Far-OTM Put Spread",
    # 2026-08-01 book cutover: QG-O11 (ratio 0.255, −₹8,075; width-1 friction trap) and
    # RAE BANKNIFTY Range Seller (monthly-expiry → theta unreachable, §21.2) RETIRED to PURGE.
}

ERP_PURGE_STRATEGY_NAMES: Set[str] = {
    "QG-O11 NIFTY Regime Seller Credit Scalp",       # 2026-08-01: ratio 0.255, −₹8,075
    "RAE BANKNIFTY Range Seller (RANGE/INSIDE)",     # 2026-08-01: monthly expiry, theta unreachable
    "AXISBANK Trend Follower",
    "BANKNIFTY Breakout Buyer",
    "BANKNIFTY HFT Momentum Scalper",
    "BANKNIFTY Theta Credit Spread",
    "BANKNIFTY Volatility Breakout",
    "BHARTIARTL Intraday Trend",
    "HDFCBANK Range Rebound",
    "ICICIBANK Volatility Breakout",
    "INFY VWAP Pullback",
    "KOTAKBANK RSI Rebound",
    "LT Momentum Rider",
    "MANUAL_RECOVERY",
    "NIFTY HFT Quick Scalper",
    "NIFTY Micro-Lot Trend Follower",
    "NIFTY Momentum Buyer",
    "NIFTY Quick EMA Scalper",
    "NIFTY Range Credit Spread",
    "NIFTY Theta Credit Spread",
    "NIFTY VWAP Trend Breakout",
    "QG-O2 NIFTY Trend-Filtered Put Spread Theta",
    "QG-O3 SENSEX Put Spread Theta Pilot",
    "QG-O5 NIFTY Opening Range Call Buyer",
    "QG-O6 NIFTY Opening Range Put Buyer",
    "QG-O7 BANKNIFTY VWAP Reclaim Call Buyer",
    "QG-O8 BANKNIFTY VWAP Reject Put Buyer",
    "QG-O9 NIFTY Tail Event Put Buyer",
    "QG-O10 NIFTY Premium-Safe Debit Buyer",
    "RELIANCE Trend Rider",
    "SBIN Short Seller",
    "SENSEX Swing RSI Pullback",
    "SENSEX Theta Credit Spread",
    "TCS Swing Accumulator",
}


def registry_active_default(name: str) -> bool:
    return str(name or "") in ERP_KEEP_STRATEGY_NAMES


def registry_purge_default(name: str) -> bool:
    return str(name or "") in ERP_PURGE_STRATEGY_NAMES


def _is_hold_to_expiry(opts: OptionsGeometry, risk: RiskConfig) -> bool:
    return (opts.exit_mode or "").lower() == "expiry" or (risk.exit_mode or "").lower() == "hold_to_expiry"


def coherence_problems(cfg: StrategyConfig) -> ConfigValidation:
    """The heart: catch geometry that will silently misbehave. Errors = will trade
    wrong; warnings = suspicious. Deterministic, no I/O."""
    errors: List[str] = []
    warnings: List[str] = []
    opts = cfg.visual_config.options
    risk = cfg.visual_config.risk
    structure = (opts.structure or "").lower()

    # 1) hold-to-expiry TRAP on a spread with no intraday exit geometry (the recurring bug)
    if structure in _SPREAD:
        hold = _is_hold_to_expiry(opts, risk)
        has_tp_sl = opts.credit_tp_frac is not None and opts.credit_sl_mult is not None
        has_time = bool(risk.time_exit_minutes and risk.time_exit_minutes > 0)
        if not hold and not has_tp_sl and not has_time:
            errors.append(
                "spread has NO intraday exit (no credit_tp_frac/credit_sl_mult, "
                "time_exit_minutes=0) and is not marked hold-to-expiry — it will sit "
                "until square-off (the hold-to-expiry trap)."
            )
        if hold and (has_tp_sl or has_time):
            warnings.append("marked hold-to-expiry but also carries intraday tp/sl/time — ambiguous exit intent.")

    # 2) exit-fraction sanity
    if opts.credit_tp_frac is not None and not (0.0 < opts.credit_tp_frac <= 1.0):
        errors.append(f"credit_tp_frac={opts.credit_tp_frac} out of range (0,1].")
    if opts.credit_sl_mult is not None and not (0.0 < opts.credit_sl_mult <= 10.0):
        errors.append(f"credit_sl_mult={opts.credit_sl_mult} out of range (0,10].")
    for nm, v in (("stop_loss_pct", risk.stop_loss_pct), ("take_profit_pct", risk.take_profit_pct)):
        if v is not None and not (0.0 < v <= 100.0):
            errors.append(f"{nm}={v} out of range (0,100].")

    # 3) capital coherence across the two places it lives
    caps = [c for c in (cfg.required_capital, opts.required_capital, risk.required_capital) if c is not None]
    if len(set(round(c, 2) for c in caps)) > 1:
        warnings.append(f"required_capital differs across fields {caps} — pick one source of truth.")

    # 4) cheap identity checks
    if not cfg.id or not cfg.name:
        errors.append("missing id or name.")

    return ConfigValidation(ok=not errors, errors=errors, warnings=warnings)


def validate_strategy_doc(doc: Dict[str, Any]) -> ConfigValidation:
    """Parse a raw strategy doc into the typed projection + run coherence checks.
    A parse failure is itself an error (never raises)."""
    try:
        cfg = StrategyConfig.model_validate(doc)
    except Exception as exc:
        return ConfigValidation(ok=False, errors=[f"config parse failed: {exc}"])
    return coherence_problems(cfg)


def project_config(doc: Dict[str, Any]) -> Optional[StrategyConfig]:
    """The registry's typed read of a strategy doc (config only). None on parse fail."""
    try:
        return StrategyConfig.model_validate(doc)
    except Exception:
        return None

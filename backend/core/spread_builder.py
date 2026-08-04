"""Phase 2 #5 — defined-risk credit spread builder (pure).

Builds a two-leg vertical credit spread from an option chain:
  - bullish signal  → bull put spread  (SELL higher-strike PE, BUY lower-strike PE)
  - bearish signal  → bear call spread (SELL lower-strike CE, BUY higher-strike CE)

The short leg is chosen near a target |delta|; the long leg is `width_points`
away to cap the loss. Returns net credit, max loss, and net greeks so the risk
manager can size by defined risk (max loss), not premium. No DB, no I/O — the
caller passes in the already-fetched chain nodes.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from options_delta import pick_delta_strike, _to_float  # noqa: E402

import session_times  # noqa: E402

CREDIT_SPREADS_ENABLED = os.environ.get("CREDIT_SPREADS_ENABLED", "false").strip().lower() == "true"
CREDIT_SPREAD_SHORT_DELTA = float(os.environ.get("CREDIT_SPREAD_SHORT_DELTA", "0.30"))
# Default distance between short and long strikes, in number of strike intervals.
CREDIT_SPREAD_WIDTH_STRIKES = int(os.environ.get("CREDIT_SPREAD_WIDTH_STRIKES", "2"))

# --- ERP cost-floor law, enforced at BUILD time (2026-07-21) -----------------
# The law (CLAUDE.md §20) says reject any structure whose expected edge is below
# 3x modeled round-trip friction. It was only ever enforced in the research
# validators and as a Hermes *finding* — nothing stopped the live path from
# opening the trade anyway. QG-O1 did exactly that: it opened spreads collecting
# 8.5 premium points on a 500-point-wide wing (credit/width = 1.7%, max loss 58x
# the credit, long wing priced at 0.46 = no real protection). Round-trip friction
# on 4 legs is ~Rs250-400/lot (the figure dynamic_exit.TRAIL_MIN_ARM_RUPEES
# already encodes), so the ENTIRE achievable profit was smaller than the cost of
# transacting it. This is a structural veto, not a signal opinion: a spread this
# thin is negative-expectancy before the market moves, so refuse to build it.
CREDIT_SPREAD_MIN_CREDIT_RATIO = float(os.environ.get("CREDIT_SPREAD_MIN_CREDIT_RATIO", "0.12"))
# Modeled round-trip friction per lot (slippage on 4 legs + brokerage + taxes).
# FLOOR ONLY, not the whole model. Real friction is PROPORTIONAL to premium, and a
# flat constant misprices every instrument that is not NIFTY. Measured over 123
# closed July spreads (slippage + real charges, per lot):
#     NIFTY     Rs  303   -> 1.01x the constant   (the constant was fitted here)
#     SENSEX    Rs  240   -> 0.80x                (conservative)
#     BANKNIFTY Rs 1271   -> 4.24x  UNDER-CHARGED
# So every BANKNIFTY credit spread was clearing a bar set at a quarter of its true
# cost. Third instance of the §21.5 "probe under-measures" class. Friction is now
# derived from the legs when they are known, with the constant as a lower bound for
# the fixed part (brokerage/taxes) and for callers with no leg context.
SPREAD_ROUND_TRIP_COST_PER_LOT = float(os.environ.get("SPREAD_ROUND_TRIP_COST_PER_LOT", "300"))
# Per-leg-transaction slippage. Mirrors spread_lifecycle._apply_paper_slippage, which
# is what the paper fills actually pay; a round trip crosses 4 leg-transactions
# (open short+long, close short+long).
SPREAD_SLIPPAGE_PCT = float(os.environ.get("PAPER_SPREAD_SLIPPAGE_PCT", "0.03"))
SPREAD_COST_FLOOR_MULT = float(os.environ.get("SPREAD_COST_FLOOR_MULT", "3.0"))
# UPPER bound on credit/width — "the short strike is too close to the money".
#
# 2026-07-30: the ratio law had a floor but no ceiling, and a ceiling is the only
# delta-independent way to keep a 0-DTE seller out of the at-the-money strike. At
# 0 DTE the delta curve is a CLIFF (just-OTM ~0.05, ATM ~0.5), so there is no
# 0.30-delta strike to target and any delta-based selection snaps to ATM. Measured
# over the 56 real DTE-0 trades:
#     ratio 0.10-0.16  n=20  WR 85%  avg +Rs182
#     ratio 0.16-0.22  n=14  WR 86%  avg +Rs210
#     ratio >=0.22     n= 4  WR 50%  avg -Rs206   <- ATM, max gamma, coin flip
# Live confirmation: three SENSEX 0-DTE spreads opened at ratio 0.241/0.252/0.258
# with short strikes AT spot (77600 vs 77645) and all three went straight to a
# loss. Off by default book-wide; the DTE policy sets it at near expiry.
CREDIT_SPREAD_MAX_CREDIT_RATIO = os.environ.get("CREDIT_SPREAD_MAX_CREDIT_RATIO", "")

# --- Law 2, theta reachability, enforced at BUILD time (2026-07-22) ----------
# CLAUDE.md §21.2 states the law but nothing enforced it on the live path, and the
# static probe judged the CONFIGURED `target_dte_days` — a field no selection code
# ever reads. So the book kept picking whatever expiry the chain offered: on a
# Wednesday the nearest NIFTY weekly is 6 days out, which makes a 300-minute hold
# able to decay ~13% of the credit against a 45% take-profit (ratio 0.30). The
# result was measured on 2026-07-22: 5 of 5 spreads exited on the clock, none on
# price. Enforce it where the cost floor already is, off the REALIZED expiry.
SPREAD_MIN_TP_REACHABILITY = float(os.environ.get("SPREAD_MIN_TP_REACHABILITY", "0.55"))
SPREAD_ENFORCE_REACHABILITY = os.environ.get(
    "SPREAD_ENFORCE_REACHABILITY", "true").strip().lower() == "true"

# Book-wide ceiling on the risk budget any ONE trade may size against, in rupees.
# See lots_for_risk(). 8,000 is chosen from the live book: it is above every
# strategy's per-LOT defined risk that already trades at 1 lot (SENSEX sellers
# ~12.9k/lot, QG-O1 ~10.7k/lot, HTE ~30.7k/lot all size to 1 lot and are
# unaffected because the caller floors at 1), and below the 20,000 that let the
# mean-reversion sleeve take FIVE lots and Rs15,811 of risk on one 0-DTE spread.
# 0 disables.
#
# LIMITATION, stated because someone will assume otherwise: callers floor lots at
# 1 (`min(max(1, capital_cap), ...)`), so this bounds MULTI-LOT scaling, not the
# absolute risk of a single lot. A one-lot spread with a very wide wing can still
# exceed it. Capping that requires refusing the trade outright, which is a
# different decision from sizing it.
MAX_RISK_PER_TRADE_RUPEES = float(os.environ.get("MAX_RISK_PER_TRADE_RUPEES", "8000"))


# Tradeable minutes in one session. Was a hardcoded 375 (09:15-15:30); NSE F&O
# runs 09:15-15:40 = 385 from 2026-08-03. This is a DIVISOR in the §21.2
# reachability law, so a stale value overstates how much decay a hold can reach
# and lets through spreads the law is meant to veto.
MARKET_MINUTES_PER_DAY = float(session_times.session_minutes("NSE_FO"))


def theta_reachable_tp_frac(dte_days: float, hold_minutes: float) -> float:
    """Fraction of a credit spread's credit that TIME DECAY ALONE can deliver
    inside the hold window.

    A credit spread's extrinsic value bleeds toward zero over its remaining life,
    so over `hold_minutes` of a `dte_days` contract roughly
    `hold_minutes / (dte_days * MARKET_MINUTES_PER_DAY)` of the credit decays away (linear
    approximation; decay is convex and faster near expiry, so this is
    conservative for short DTE).

    This matters because it is the difference between a theta strategy and a
    directional bet wearing a theta costume. If the take-profit is set at 0.50 of
    credit but theta can only deliver 0.05 of it inside the hold window, then 90%
    of the target has to arrive as a favourable price move — the position is a
    coin flip and the exit is decided by whichever clock fires first.
    """
    dte = max(float(dte_days or 0), 0.05)
    return max(0.0, float(hold_minutes or 0)) / (dte * MARKET_MINUTES_PER_DAY)


def tp_reachability(tp_frac: float, dte_days: float, hold_minutes: float) -> Dict[str, Any]:
    """How much of the take-profit target theta can supply on its own.

    ratio >= 1.0 : decay alone reaches the target — a genuine theta harvest.
    ratio ~ 0.6  : theta does most of the work, direction is a tailwind.
    ratio < 0.3  : the target requires a directional gift; the clock will decide
                   the trade. Measured across QuantG's seller book on 2026-07-21,
                   this ratio rank-ordered both the price-exit rate and the P&L.
    """
    tp = max(1e-9, float(tp_frac or 0))
    reachable = theta_reachable_tp_frac(dte_days, hold_minutes)
    return {
        "tp_frac": round(float(tp_frac or 0), 4),
        "theta_reachable_frac": round(reachable, 4),
        "ratio": round(reachable / tp, 3),
        "directional_dependence": round(max(0.0, 1.0 - reachable / tp), 3),
    }


def round_trip_friction(leg_premium_sum: Optional[float], lot_size: Optional[int]) -> float:
    """Modeled round-trip friction for ONE lot of a two-leg spread.

    THE single definition of friction — the build-time enforcement and the Hermes
    `static.cost_floor` probe both call this, so they cannot drift apart (§21.5:
    when a probe and an enforcement point encode the same law they must share the
    arithmetic). A round trip crosses 4 leg-transactions, each paying
    SPREAD_SLIPPAGE_PCT of that leg's premium; the flat constant is the lower bound
    for the fixed part (brokerage/taxes) and for callers with no leg context.
    """
    if not leg_premium_sum or not lot_size:
        return SPREAD_ROUND_TRIP_COST_PER_LOT
    modeled = 2.0 * SPREAD_SLIPPAGE_PCT * float(leg_premium_sum) * int(lot_size)
    return max(SPREAD_ROUND_TRIP_COST_PER_LOT, modeled)


def min_bankable_profit(
    lot_size: Optional[int],
    *,
    leg_premium_sum: Optional[float] = None,
    lots: int = 1,
) -> float:
    """The rupee profit the cost-floor law PROMISED when it approved the trade.

    §21.1 lets a spread be built only when `tp_frac x credit x lot_size` clears
    SPREAD_COST_FLOOR_MULT x round-trip friction. That approval is meaningless if
    the exit engine then cashes out below it — and until 2026-07-29 it did:
    measured over the 22 trades of that week, 16 exited on `trail-lock` averaging
    Rs284 while the trail's own arm floor was a flat Rs300 and the cost floor had
    demanded Rs900+. The average WINNING trade did not cover its own friction.

    This is the same defect class as §22.3 (an exemption granted by one exit path
    and ignored by another) and §21.5 (a law encoded twice with two different
    arithmetics). There is now ONE number, defined here, that the builder demands
    and the trailing exit refuses to bank below.
    """
    if not lot_size:
        return SPREAD_COST_FLOOR_MULT * SPREAD_ROUND_TRIP_COST_PER_LOT * max(1, int(lots or 1))
    friction = round_trip_friction(leg_premium_sum, lot_size)
    return SPREAD_COST_FLOOR_MULT * friction * max(1, int(lots or 1))


def credit_cost_floor(
    net_credit: float,
    width_points: float,
    *,
    lot_size: Optional[int] = None,
    tp_frac: float = 1.0,
    leg_premium_sum: Optional[float] = None,
    cost_floor_mult: Optional[float] = None,
    max_credit_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """Judge a candidate credit spread against the ERP cost-floor law.

    Two independent tests, both structural (no signal opinion):
      1. credit/width ratio — a spread collecting a token fraction of the risk it
         takes needs an implausible win rate no matter how good the signal is.
      2. achievable gross profit vs friction — `tp_frac x credit x lot_size` is
         what the exit engine can actually bank; it must clear
         SPREAD_COST_FLOOR_MULT x round-trip friction.

    Returns {passed, credit_ratio, ...}. `lot_size=None` skips test 2 (the pure
    ratio test still applies), so callers without contract context still get the
    structural check.
    """
    width = max(float(width_points or 0), 1.0)
    credit = float(net_credit or 0)
    credit_ratio = credit / width
    out: Dict[str, Any] = {
        "credit_ratio": round(credit_ratio, 4),
        "min_credit_ratio": CREDIT_SPREAD_MIN_CREDIT_RATIO,
        "ratio_passed": bool(credit_ratio >= CREDIT_SPREAD_MIN_CREDIT_RATIO),
    }
    # Upper bound: too MUCH credit for the width means the short strike is at/near
    # the money. A floor without a ceiling was how the 0-DTE unlock walked straight
    # into ATM gamma (2026-07-30).
    _max_ratio = max_credit_ratio
    if _max_ratio is None and CREDIT_SPREAD_MAX_CREDIT_RATIO:
        try:
            _max_ratio = float(CREDIT_SPREAD_MAX_CREDIT_RATIO)
        except ValueError:
            _max_ratio = None
    out["max_credit_ratio"] = _max_ratio
    out["ratio_max_passed"] = True if _max_ratio is None else bool(credit_ratio <= _max_ratio)
    out["passed"] = bool(out["ratio_passed"] and out["ratio_max_passed"])
    if lot_size:
        # Proportional when the legs are known: 4 leg-transactions per round trip,
        # each crossing SPREAD_SLIPPAGE_PCT of that leg's premium. The flat constant
        # remains a lower bound (brokerage/taxes barely scale with premium).
        friction = round_trip_friction(leg_premium_sum, lot_size)
        out["friction_basis"] = (
            "premium_proportional" if friction > SPREAD_ROUND_TRIP_COST_PER_LOT
            else ("flat_floor" if leg_premium_sum else "flat_floor_no_leg_context")
        )
        # 2026-07-30: the multiple is per-trade overridable so the DTE policy can
        # relax it at near expiry. A flat 3x vetoed DTE 0 outright — the bucket with
        # the best realized record (n=56, WR 80%, avg +Rs123) — because 0-DTE credit
        # is structurally small. Never below 1.0x: banking under friction is a loss.
        _mult = SPREAD_COST_FLOOR_MULT if cost_floor_mult is None else max(1.0, float(cost_floor_mult))
        floor = _mult * friction
        achievable = max(0.0, min(1.0, float(tp_frac or 1.0))) * credit * int(lot_size)
        out.update({
            "achievable_gross_profit": round(achievable, 2),
            "round_trip_cost_per_lot": friction,
            "required_floor": round(floor, 2),
            "cost_floor_mult": round(_mult, 2),
            "cost_multiple": round(achievable / friction, 2) if friction else None,
            "floor_passed": bool(achievable >= floor),
        })
        out["passed"] = bool(out["ratio_passed"] and out["ratio_max_passed"]
                             and out["floor_passed"])
    return out


def dte_from_expiry(expiry: Any, *, today: Optional[str] = None) -> Optional[float]:
    """Calendar days from `today` (IST) to an option expiry, or None if unparseable.

    Tolerant of the shapes the chain hands back: "2026-07-28", an ISO datetime, or
    an epoch-millis integer. Returns a float so a same-day (0-DTE) contract lands
    on a small positive number rather than dividing by zero downstream.
    """
    from datetime import date, datetime as _dt, timedelta as _td, timezone as _tz

    if expiry in (None, ""):
        return None
    exp: Optional[date] = None
    if isinstance(expiry, (int, float)) or (isinstance(expiry, str) and expiry.isdigit()):
        try:
            exp = _dt.fromtimestamp(float(expiry) / 1000.0, tz=_tz.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            exp = _dt.fromisoformat(str(expiry).replace("Z", "+00:00")).date()
        except ValueError:
            return None
    ref = (_dt.fromisoformat(today).date() if today
           else _dt.now(_tz(_td(hours=5, minutes=30))).date())
    return max(0.0, float((exp - ref).days))


def _node_strike(node: Dict[str, Any]) -> Optional[float]:
    return _to_float(node.get("strike_price"))


def _leg_from_node(node: Dict[str, Any], option_type: str, side: str) -> Optional[Dict[str, Any]]:
    side_key = "call_options" if option_type == "CE" else "put_options"
    opt = node.get(side_key) or {}
    key = opt.get("instrument_key")
    if not key:
        return None
    md = opt.get("market_data") or {}
    greeks = opt.get("option_greeks") or {}
    premium = _to_float(md.get("ltp")) or _to_float(md.get("last_price"))
    if premium is None or premium <= 0:
        return None
    return {
        "role": "short" if side == "SELL" else "long",
        "side": side,
        "option_type": option_type,
        "strike": _node_strike(node),
        "instrument_key": key,
        "tradingsymbol": opt.get("trading_symbol"),
        "expiry": node.get("expiry"),
        "premium": premium,
        "delta": _to_float(greeks.get("delta")),
        "iv": _to_float(greeks.get("iv")),
        "theta": _to_float(greeks.get("theta")),
        "oi": _to_float(md.get("oi")),
    }


def _find_node_by_strike(nodes: List[Dict[str, Any]], strike: float, option_type: str) -> Optional[Dict[str, Any]]:
    """Exact strike match, else the nearest strike that has a usable leg."""
    exact = None
    nearest = None
    nearest_dist = None
    side_key = "call_options" if option_type == "CE" else "put_options"
    for node in nodes or []:
        ns = _node_strike(node)
        if ns is None:
            continue
        opt = node.get(side_key) or {}
        if not opt.get("instrument_key"):
            continue
        if int(ns) == int(strike):
            exact = node
            break
        dist = abs(ns - strike)
        if nearest_dist is None or dist < nearest_dist:
            nearest_dist = dist
            nearest = node
    return exact or nearest


def build_credit_spread(
    *,
    chain_nodes: List[Dict[str, Any]],
    direction: str,
    width_points: float,
    short_delta: float = CREDIT_SPREAD_SHORT_DELTA,
    lot_size: Optional[int] = None,
    tp_frac: float = 1.0,
    enforce_cost_floor: bool = True,
    hold_minutes: Optional[float] = None,
    enforce_reachability: Optional[bool] = None,
    cost_floor_mult: Optional[float] = None,
    max_credit_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a vertical credit spread.

    direction: "bullish" → bull put spread; "bearish" → bear call spread.
    width_points: absolute strike distance between short and long legs.
    hold_minutes: the hold window the exit engine will actually give this trade
    (time-exit, capped by the square-off). When supplied, the §21.2 reachability
    law is enforced off the REALIZED expiry — a take-profit that decay cannot
    reach inside the hold is a directional bet, so refuse to build it. Pass None
    (research paths, tests) to skip the test.

    Returns {ok, reason, structure, direction, option_type, short_leg, long_leg,
    net_credit, max_loss, max_profit, width_points, net_delta, net_theta}.
    All money figures are PER UNIT (premium points); caller multiplies by
    lot_size * lots.
    """
    direction = str(direction or "").lower()
    if direction not in ("bullish", "bearish"):
        return {"ok": False, "reason": "direction must be bullish or bearish"}
    option_type = "PE" if direction == "bullish" else "CE"

    short_pick = pick_delta_strike(chain_nodes, option_type, short_delta)
    if not short_pick or short_pick.get("strike") is None:
        return {"ok": False, "reason": "no short-leg strike near target delta"}
    short_strike = float(short_pick["strike"])

    # Bull put: long strike BELOW short. Bear call: long strike ABOVE short.
    long_strike = short_strike - width_points if direction == "bullish" else short_strike + width_points

    short_node = _find_node_by_strike(chain_nodes, short_strike, option_type)
    long_node = _find_node_by_strike(chain_nodes, long_strike, option_type)
    if not short_node or not long_node:
        return {"ok": False, "reason": "could not locate both spread legs in chain"}

    short_leg = _leg_from_node(short_node, option_type, "SELL")
    long_leg = _leg_from_node(long_node, option_type, "BUY")
    if not short_leg or not long_leg:
        return {"ok": False, "reason": "spread leg missing instrument_key or premium"}
    if short_leg["strike"] == long_leg["strike"]:
        return {"ok": False, "reason": "short and long legs resolved to the same strike"}

    net_credit = round(short_leg["premium"] - long_leg["premium"], 2)
    if net_credit <= 0:
        return {"ok": False, "reason": f"non-positive net credit ({net_credit})"}

    actual_width = abs(short_leg["strike"] - long_leg["strike"])
    max_loss = round(actual_width - net_credit, 2)
    if max_loss <= 0:
        return {"ok": False, "reason": f"non-positive max loss ({max_loss})"}

    # ERP cost-floor law: veto structurally negative-expectancy geometry here, at
    # the single choke point every credit spread passes through, rather than
    # letting it reach the order path and be discovered in the P&L.
    floor = credit_cost_floor(
        net_credit, actual_width, lot_size=lot_size, tp_frac=tp_frac,
        leg_premium_sum=short_leg["premium"] + long_leg["premium"],
        cost_floor_mult=cost_floor_mult, max_credit_ratio=max_credit_ratio,
    )
    if enforce_cost_floor and not floor["passed"]:
        if not floor.get("ratio_max_passed", True):
            return {
                "ok": False,
                "reason": ("credit_ratio_too_high: credit {:.2f} on width {:.0f} "
                           "(ratio {:.3f} > {:.3f} max) — short strike is at/near the "
                           "money; 0-DTE ATM measured WR 50%, avg -Rs206").format(
                    net_credit, actual_width, floor["credit_ratio"], floor["max_credit_ratio"]),
                "cost_floor": floor,
            }
        _detail = ""
        if "achievable_gross_profit" in floor and not floor.get("floor_passed"):
            _detail = ", achievable Rs{:.0f} < Rs{:.0f} floor".format(
                floor["achievable_gross_profit"], floor["required_floor"])
        return {
            "ok": False,
            "reason": "cost_floor: credit {:.2f} on width {:.0f} (ratio {:.3f} < {:.3f} min{})".format(
                net_credit, actual_width, floor["credit_ratio"], floor["min_credit_ratio"], _detail),
            "cost_floor": floor,
        }

    # §21.2 theta-reachability law, judged on the expiry actually resolved above
    # (not on the strategy's decorative `target_dte_days`) and on the hold window
    # the exit engine will actually grant.
    reach: Optional[Dict[str, Any]] = None
    _enforce_reach = (SPREAD_ENFORCE_REACHABILITY if enforce_reachability is None
                      else bool(enforce_reachability))
    _dte = dte_from_expiry(short_leg.get("expiry"))
    if hold_minutes is not None and _dte is not None:
        reach = tp_reachability(tp_frac, _dte, hold_minutes)
        reach.update({"dte_days": _dte, "hold_minutes": round(float(hold_minutes), 1),
                      "min_ratio": SPREAD_MIN_TP_REACHABILITY})
        reach["passed"] = bool(reach["ratio"] >= SPREAD_MIN_TP_REACHABILITY)
        if _enforce_reach and not reach["passed"]:
            return {
                "ok": False,
                "reason": (
                    "tp_reachability: {:.0f}-DTE contract over a {:.0f}min hold decays "
                    "{:.1%} of credit vs a {:.0%} target (ratio {:.2f} < {:.2f}) — "
                    "the clock, not theta, would close this"
                ).format(_dte, hold_minutes, reach["theta_reachable_frac"],
                         tp_frac, reach["ratio"], SPREAD_MIN_TP_REACHABILITY),
                "tp_reachability": reach,
            }

    sd = short_leg.get("delta") or 0.0
    ld = long_leg.get("delta") or 0.0
    st = short_leg.get("theta") or 0.0
    lt = long_leg.get("theta") or 0.0
    return {
        "ok": True,
        "reason": "ok",
        "structure": "credit_spread",
        "direction": direction,
        "option_type": option_type,
        "short_leg": short_leg,
        "long_leg": long_leg,
        "net_credit": net_credit,        # max profit per unit
        "max_profit": net_credit,
        "max_loss": max_loss,            # defined risk per unit
        "width_points": actual_width,
        "cost_floor": floor,
        "tp_reachability": reach,
        # Net greeks: short leg is a SOLD option, so its greeks flip sign.
        "net_delta": round(-sd + ld, 4),
        "net_theta": round(-st + lt, 4),
    }


def build_credit_spread_by_offset(
    *,
    chain_nodes: List[Dict[str, Any]],
    direction: str,
    spot: float,
    offset_strikes: int,
    width_points: float,
) -> Dict[str, Any]:
    """Build a credit spread by offset from ATM instead of target delta.

    Used by QG-O5's intraday credit scalp where the OOS lead was defined as:
    bullish ORB -> sell PE 2 strikes OTM, buy 1 strike lower.
    """
    direction = str(direction or "").lower()
    if direction not in ("bullish", "bearish"):
        return {"ok": False, "reason": "direction must be bullish or bearish"}
    option_type = "PE" if direction == "bullish" else "CE"
    strikes = sorted(s for s in (_node_strike(n) for n in chain_nodes or []) if s is not None)
    if not strikes:
        return {"ok": False, "reason": "no strikes in chain"}

    atm = min(strikes, key=lambda s: abs(s - float(spot)))
    sign = -1 if direction == "bullish" else 1
    short_strike = atm + sign * max(0, int(offset_strikes)) * abs(float(width_points))
    long_strike = short_strike + sign * abs(float(width_points))

    short_node = _find_node_by_strike(chain_nodes, short_strike, option_type)
    long_node = _find_node_by_strike(chain_nodes, long_strike, option_type)
    if not short_node or not long_node:
        return {"ok": False, "reason": "could not locate both spread legs in chain"}

    short_leg = _leg_from_node(short_node, option_type, "SELL")
    long_leg = _leg_from_node(long_node, option_type, "BUY")
    if not short_leg or not long_leg:
        return {"ok": False, "reason": "spread leg missing instrument_key or premium"}
    if short_leg["strike"] == long_leg["strike"]:
        return {"ok": False, "reason": "short and long legs resolved to the same strike"}

    net_credit = round(short_leg["premium"] - long_leg["premium"], 2)
    if net_credit <= 0:
        return {"ok": False, "reason": f"non-positive net credit ({net_credit})"}

    actual_width = abs(short_leg["strike"] - long_leg["strike"])
    max_loss = round(actual_width - net_credit, 2)
    if max_loss <= 0:
        return {"ok": False, "reason": f"non-positive max loss ({max_loss})"}

    sd = short_leg.get("delta") or 0.0
    ld = long_leg.get("delta") or 0.0
    st = short_leg.get("theta") or 0.0
    lt = long_leg.get("theta") or 0.0
    return {
        "ok": True,
        "reason": "ok",
        "structure": "credit_spread",
        "direction": direction,
        "option_type": option_type,
        "short_leg": short_leg,
        "long_leg": long_leg,
        "net_credit": net_credit,
        "max_profit": net_credit,
        "max_loss": max_loss,
        "width_points": actual_width,
        "net_delta": round(-sd + ld, 4),
        "net_theta": round(-st + lt, 4),
        "selection_method": "offset",
        "offset_strikes": int(offset_strikes),
    }


DEBIT_SPREADS_ENABLED = os.environ.get("DEBIT_SPREADS_ENABLED", "true").strip().lower() == "true"
DEBIT_SPREAD_LONG_DELTA = float(os.environ.get("DEBIT_SPREAD_LONG_DELTA", "0.50"))


def build_debit_spread(
    *,
    chain_nodes: List[Dict[str, Any]],
    direction: str,
    width_points: float,
    long_delta: float = DEBIT_SPREAD_LONG_DELTA,
) -> Dict[str, Any]:
    """Build a vertical debit spread.

    direction: "bullish" → bull call spread (BUY near-ATM CE, SELL further-OTM CE);
               "bearish" → bear put spread (BUY near-ATM PE, SELL further-OTM PE).
    width_points: absolute strike distance between long and short legs.

    Returns {ok, reason, structure, direction, option_type, short_leg, long_leg,
    net_debit, max_loss, max_profit, width_points, net_delta, net_theta}.
    All money figures are PER UNIT (premium points); caller multiplies by
    lot_size * lots.
    """
    direction = str(direction or "").lower()
    if direction not in ("bullish", "bearish"):
        return {"ok": False, "reason": "direction must be bullish or bearish"}
    option_type = "CE" if direction == "bullish" else "PE"

    long_pick = pick_delta_strike(chain_nodes, option_type, long_delta)
    if not long_pick or long_pick.get("strike") is None:
        return {"ok": False, "reason": "no long-leg strike near target delta"}
    long_strike = float(long_pick["strike"])

    # Bull call: short strike ABOVE long. Bear put: short strike BELOW long.
    short_strike = long_strike + width_points if direction == "bullish" else long_strike - width_points

    long_node = _find_node_by_strike(chain_nodes, long_strike, option_type)
    short_node = _find_node_by_strike(chain_nodes, short_strike, option_type)
    if not short_node or not long_node:
        return {"ok": False, "reason": "could not locate both spread legs in chain"}

    long_leg = _leg_from_node(long_node, option_type, "BUY")
    short_leg = _leg_from_node(short_node, option_type, "SELL")
    if not short_leg or not long_leg:
        return {"ok": False, "reason": "spread leg missing instrument_key or premium"}
    if short_leg["strike"] == long_leg["strike"]:
        return {"ok": False, "reason": "short and long legs resolved to the same strike"}

    net_debit = round(long_leg["premium"] - short_leg["premium"], 2)
    if net_debit <= 0:
        return {"ok": False, "reason": f"non-positive net debit ({net_debit})"}

    actual_width = abs(short_leg["strike"] - long_leg["strike"])
    max_profit = round(actual_width - net_debit, 2)
    if max_profit <= 0:
        return {"ok": False, "reason": f"non-positive max profit ({max_profit})"}

    sd = short_leg.get("delta") or 0.0
    ld = long_leg.get("delta") or 0.0
    st = short_leg.get("theta") or 0.0
    lt = long_leg.get("theta") or 0.0
    return {
        "ok": True,
        "reason": "ok",
        "structure": "debit_spread",
        "direction": direction,
        "option_type": option_type,
        "short_leg": short_leg,
        "long_leg": long_leg,
        "net_debit": net_debit,          # net cost per unit
        "max_profit": max_profit,
        "max_loss": net_debit,           # defined risk per unit
        "width_points": actual_width,
        # Net greeks: short leg is sold (-), long leg is bought (+)
        "net_delta": round(ld - sd, 4),
        "net_theta": round(lt - st, 4),
    }


def option_intrinsic(strike: Any, option_type: Any, underlying: Any) -> Optional[float]:
    """Intrinsic value of ONE option at expiry, or None if the inputs are unusable.

    At expiry an option is worth exactly its intrinsic value — every rupee of time
    value is gone by definition. This is therefore what a settled leg is worth,
    and the last traded premium is not: that number carries phantom time value and
    inherits whatever the feed happened to print last.
    """
    try:
        k = float(strike)
        s = float(underlying)
    except (TypeError, ValueError):
        return None
    if k <= 0 or s <= 0:
        return None
    t = str(option_type or "").strip().upper()
    if t.startswith("C"):
        return max(s - k, 0.0)
    if t.startswith("P"):
        return max(k - s, 0.0)
    return None


def settle_legs_at_intrinsic(
    short_leg: Optional[Dict[str, Any]],
    long_leg: Optional[Dict[str, Any]],
    underlying: Any,
) -> Optional[Dict[str, float]]:
    """{"short": x, "long": y} — both legs of an expiring spread priced at
    intrinsic against the underlying's settlement price.

    Returns None when either leg lacks a usable strike/option_type or the
    underlying price is not positive. That is deliberate: the caller must then
    fall back to its normal marks rather than settle real money against a
    fabricated number. Fail-closed, per §22.3 — a settlement path that invents a
    price is worse than one that admits it does not have one.
    """
    if not short_leg or not long_leg:
        return None
    s = option_intrinsic(short_leg.get("strike"), short_leg.get("option_type"), underlying)
    l = option_intrinsic(long_leg.get("strike"), long_leg.get("option_type"), underlying)
    if s is None or l is None:
        return None
    return {"short": round(s, 2), "long": round(l, 2)}


def lots_for_risk(max_loss_per_unit: float, lot_size: int, risk_budget: float) -> int:
    """Number of lots whose total defined risk stays within risk_budget.

    Also enforces the BOOK-WIDE absolute ceiling on rupee risk per trade
    (MAX_RISK_PER_TRADE_RUPEES). `risk_budget` is the strategy's own
    `required_capital`, which is per-strategy config with no upper bound — so a
    single row can quietly take many times the book's normal per-trade risk. On
    2026-08-04 `IDX NIFTY Mean-Reversion Fade` carried required_capital 20,000
    against 4,000-13,000 everywhere else, put Rs15,811 of defined risk on ONE
    0-DTE debit spread, and lost Rs8,077 — 129% of that day's entire loss.

    The cap belongs here because this is the single choke point every sized
    structure passes through (credit and debit, EdgeMath and plain capital cap),
    so no strategy config and no future caller can route around it. Set
    MAX_RISK_PER_TRADE_RUPEES=0 to disable.
    """
    per_lot = float(max_loss_per_unit) * int(lot_size)
    if per_lot <= 0 or risk_budget <= 0:
        return 0
    budget = float(risk_budget)
    cap = MAX_RISK_PER_TRADE_RUPEES
    if cap > 0:
        budget = min(budget, cap)
    return max(0, int(budget // per_lot))

"""Truthful capital model — how much REAL market money a trade ties up.

Display-only: this does NOT touch the paper wallet money-path (no debit/credit
change), it just answers two questions honestly for the UI/reports:

  * capital_blocked(position)  — real money a broker blocks while THIS position is
    open. Exact, not a budget:
      - option BUY (LONG single leg / debit spread net-debit) -> premium paid
      - defined-risk SPREAD (credit/debit) -> its max loss (max_loss_total), which
        is exactly what a broker blocks for the hedged pair
      - NAKED short -> SPAN+exposure margin per lot (estimate; no naked in the
        current book, so this is a fallback)

  * required_capital(strategy) — a structure-aware ESTIMATE of the money needed to
    run the strategy at its configured size, so the strategy card stops showing a
    flat ₹35k guess for everything.

Pure functions; no DB, no wallet. The paper wallet stays the realized-P&L tracker
it is (core/paper_broker.py) — this is the separate, honest "margin" lens.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from core.market_domains import resolve_domain_by_underlying
except Exception:  # pragma: no cover - defensive for isolated imports
    resolve_domain_by_underlying = None  # type: ignore

# Approx real Upstox overnight SPAN+exposure per lot for a NAKED short option.
# Only used as a fallback — the live book is all defined-risk spreads/buys, which
# are computed exactly from max-loss / premium and never hit this table.
NAKED_SPAN_PER_LOT: Dict[str, float] = {
    "NIFTY": 120000.0, "BANKNIFTY": 145000.0, "FINNIFTY": 120000.0,
    "MIDCPNIFTY": 90000.0, "SENSEX": 165000.0, "BANKEX": 150000.0,
}
DEFAULT_NAKED_SPAN_PER_LOT = 130000.0

_SPREAD_STRUCTURES = ("credit_spread", "debit_spread")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _underlying_of(row: Dict[str, Any]) -> str:
    vc = row.get("visual_config") or {}
    opt = vc.get("options") or {}
    u = (row.get("underlying") or opt.get("underlying") or vc.get("symbol")
         or row.get("target_symbol") or row.get("symbol") or "")
    u = str(u).upper()
    for known in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "BANKEX", "SENSEX", "NIFTY"):
        if known in u:
            return known
    return u


def _lots(row: Dict[str, Any]) -> int:
    vc = row.get("visual_config") or {}
    opt = vc.get("options") or {}
    for v in (row.get("lots"), opt.get("lots"), vc.get("lots")):
        if v:
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                pass
    return 1


def position_capital_blocked(row: Dict[str, Any]) -> float:
    """Exact real margin held while this position is open (₹)."""
    structure = row.get("structure")
    if structure in _SPREAD_STRUCTURES:
        # Defined-risk pair: broker blocks the max loss. Already computed at entry.
        mlt = row.get("max_loss_total")
        if mlt not in (None, ""):
            return round(_f(mlt), 2)
        qty = int(_f(row.get("open_quantity") or row.get("quantity") or 0))
        ml = row.get("max_loss")
        if ml not in (None, "") and qty:
            return round(_f(ml) * qty, 2)  # max_loss is per-unit (× quantity)
        return 0.0

    qty = int(_f(row.get("open_quantity") or row.get("quantity") or 0))
    avg = _f(row.get("average_buy_price") or row.get("average_price") or row.get("entry_price"))
    side = str(row.get("position_side") or "LONG").upper()
    if side != "SHORT":
        # Buyer: capital tied up == premium paid.
        return round(avg * qty, 2)
    # Naked short: SPAN+exposure per lot (estimate).
    return round(NAKED_SPAN_PER_LOT.get(_underlying_of(row), DEFAULT_NAKED_SPAN_PER_LOT) * _lots(row), 2)


def _strike_interval(underlying: str) -> float:
    if resolve_domain_by_underlying:
        try:
            return float(resolve_domain_by_underlying(underlying).get_strike_interval(underlying))
        except Exception:
            pass
    return {"BANKNIFTY": 100.0, "SENSEX": 100.0, "BANKEX": 100.0}.get(underlying, 50.0)


def _lot_size(underlying: str) -> int:
    if resolve_domain_by_underlying:
        try:
            return int(resolve_domain_by_underlying(underlying).get_lot_size(underlying))
        except Exception:
            pass
    return {"NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20}.get(underlying, 50)


def strategy_required_capital(row: Dict[str, Any], *, fallback: Optional[float] = None) -> float:
    """Structure-aware estimate of capital to run the strategy at configured size."""
    vc = row.get("visual_config") or {}
    opt = vc.get("options") or {}
    structure = str(row.get("structure") or opt.get("structure") or "single_leg").lower()
    underlying = _underlying_of(row)
    lots = _lots(row)
    lot_size = _lot_size(underlying)

    if structure in _SPREAD_STRUCTURES:
        width = int(_f(row.get("spread_width") or opt.get("spread_width") or 2)) or 2
        # Gross max-loss ceiling = width in points × lot × lots (ignores credit for a
        # conservative estimate of the margin a broker blocks on the hedged pair).
        est = width * _strike_interval(underlying) * lot_size * lots
        return round(est, 2)

    # Single-leg / debit buyer: premium is the capital. Estimate premium as a small
    # % of the underlying notional (ATM weekly ~1.2% of spot is a reasonable default).
    spot = _f(row.get("last_underlying_price") or opt.get("reference_price"))
    if spot > 0:
        return round(0.012 * spot * lot_size * lots, 2)
    return round(_f(fallback, 12000.0 * lots), 2)

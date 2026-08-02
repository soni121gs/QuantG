"""DTE-conditional seller policy — the measured replacement for blanket gates.

Derived 2026-07-30 from **259 real closed credit spreads** (every one in the book's
history), not from a model. Days-to-expiry at ENTRY is by far the strongest
discriminator found, and it is monotone:

    DTE 0 (expiry day)  n=56  WR 80%  avg +Rs123   <- the only strongly +ve bucket
    DTE 1-2             n=49  WR 63%  avg  -Rs75
    DTE 3-5             n=35  WR 23%  avg -Rs121
    DTE 5-7             n=31  WR 35%  avg -Rs380
    DTE 7+              n=81  WR 31%  avg -Rs235   <- biggest loss pool, -Rs19,015

Crossed with the intraday regime it explains what the blanket CHOP stand-down was
really seeing:

    HIGH_VOL_CHOP  DTE 0    n=7   WR 100%  avg  +Rs391
    HIGH_VOL_CHOP  DTE 1-2  n=15  WR  60%  avg  -Rs299
    HIGH_VOL_CHOP  DTE 3+   n=6   WR   0%  avg -Rs1143
    RANGE          all DTE                 avg -Rs123..-Rs204

So chop is not the enemy — **far expiry is**. Standing down on all of CHOP threw
away its best bucket while leaving DTE 3+ (where the real damage is) wide open.
This module makes the gate conditional on the dimension that actually separates.

Consequence for trade COUNT: near-expiry windows are NIFTY Mon/Tue (weekly Tuesday)
and SENSEX Wed/Thu (weekly Thursday) — about 4 tradeable days a week across the
book, and they are exactly the days the current geometry vetoes least. Unlocking
DTE 0 (today vetoed outright by a flat 3x cost floor, because 0-DTE credit is
structurally small) ADDS trades in the bucket with the best realized record.

Honest limits: these are realized paper trades spanning several geometries and
strategy generations, so the buckets are not a clean controlled experiment. The DTE
gradient is steep, monotone and large-sample (n=259) — trust that. CHOP@DTE-0 is
n=7 — do not trust the magnitude, only the direction, which the gradient supports.
Every threshold here is env-tunable and every default is reversible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


# Trade sellers at or below this DTE in ANY regime (incl. HIGH_VOL_CHOP).
DTE_ALL_REGIMES_MAX = _i("SELLER_DTE_ALL_REGIMES_MAX", 1)
# Above that and up to here, trade only the regimes a seller owns (RANGE/INSIDE).
DTE_OWNED_REGIME_MAX = _i("SELLER_DTE_OWNED_REGIME_MAX", 2)
# Beyond this, stand down: WR 23-41% and avg -Rs121..-Rs1143 across 147 trades.
DTE_STAND_DOWN_ABOVE = _i("SELLER_DTE_STAND_DOWN_ABOVE", 2)
# Cost-floor multiple applied at near expiry. The book-wide default is 3.0, which
# vetoes DTE 0 outright because the credit is small in absolute terms — and DTE 0 is
# the best-performing bucket. Lowered here, but never below 1.0x real friction (the
# trail's own floor is 1.5x, so a near-expiry winner still cannot bank below cost).
NEAR_EXPIRY_COST_FLOOR_MULT = _f("SELLER_NEAR_EXPIRY_COST_FLOOR_MULT", 1.5)
# Time-recycle is actively harmful at near expiry: DTE 0-1 spread-time-exit was
# n=8, WR 13%, avg -Rs661, while spread-tp was n=24, WR 100%, avg +Rs555. Let the
# near-expiry trade reach its target instead of clocking out mid-decay.
DISABLE_TIME_EXIT_NEAR_EXPIRY = _b("SELLER_DISABLE_TIME_EXIT_NEAR_EXPIRY", True)
# Concurrent spreads per (strategy, underlying). Was hard-coded to 1. Raising this
# is only safe because cross-strategy contract dedup (2026-07-29) now prevents the
# failure it was really protecting against — three strategies in ONE contract.
MAX_CONCURRENT_SPREADS = _i("SELLER_MAX_CONCURRENT_SPREADS", 2)
# Wing width (in strike intervals) at near expiry.
#
# Relaxing the cost-floor MULTIPLE alone does not unlock DTE 0 — the credit/width
# RATIO test fails independently, and it should: credit/width 0.12-0.16 was the only
# positive bucket in the 259-trade study (n=32, WR 69%, avg +Rs57), while >=0.25 was
# the worst (n=87, WR 33%, avg -Rs200). At DTE 0 the credit collapses (SENSEX 33.10
# observed 2026-07-30), so on the configured 6-8 strike wing the ratio is ~0.04 and
# the trade is correctly refused. The fix is not to weaken the law — it is to bring
# the WIDTH down to where a 0-DTE credit produces a lawful ratio: 33.10 needs a
# width near 275, i.e. 2-3 SENSEX strikes, not 6-8.
NEAR_EXPIRY_WIDTH_STRIKES = _i("SELLER_NEAR_EXPIRY_WIDTH_STRIKES", 3)
# Upper bound on credit/width at near expiry — keeps the short strike OUT of the
# money. 2 strikes was tried first and was wrong: SENSEX (lot 20, tp_frac 0.45)
# needs credit >= 50 to clear a 1.5x floor of Rs450, and credit 50 on a 200-wide
# wing IS the at-the-money strike (ratio 0.25). Three strikes puts the same credit
# at ratio 0.167 — inside the measured 0.10-0.22 band (n=34, WR 85-86%, +Rs182..
# +Rs210) instead of the >=0.22 band (n=4, WR 50%, -Rs206). The ceiling below is
# the backstop that makes it structural rather than a lucky arithmetic coincidence.
NEAR_EXPIRY_MAX_CREDIT_RATIO = _f("SELLER_NEAR_EXPIRY_MAX_CREDIT_RATIO", 0.22)

# Regimes a premium seller owns once past the near-expiry window.
OWNED_REGIMES = {"RANGE", "INSIDE_QUIET"}


@dataclass
class DtePolicy:
    dte: Optional[int]
    allow: bool
    reason: str
    cost_floor_mult: Optional[float] = None      # None = use the book-wide default
    width_strikes: Optional[int] = None          # None = use the strategy's config
    max_credit_ratio: Optional[float] = None     # None = no ceiling (book default)
    disable_time_exit: bool = False
    near_expiry: bool = False                    # inside the measured-best window
    telemetry: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"dte": self.dte, "allow": self.allow, "reason": self.reason,
                "cost_floor_mult": self.cost_floor_mult, "near_expiry": self.near_expiry,
                "width_strikes": self.width_strikes,
                "max_credit_ratio": self.max_credit_ratio,
                "disable_time_exit": self.disable_time_exit, **self.telemetry}


def dte_from_expiry(expiry: Any, *, today: Optional[date] = None) -> Optional[int]:
    """Calendar days from today to `expiry`. None when unparseable — callers must
    treat None as "unknown", never as 0 (an unknown expiry must not be granted the
    near-expiry exemptions)."""
    if not expiry:
        return None
    ref = today or datetime.now().date()
    if isinstance(expiry, datetime):
        return (expiry.date() - ref).days
    if isinstance(expiry, date):
        return (expiry - ref).days
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return (datetime.strptime(str(expiry)[:10], fmt).date() - ref).days
        except ValueError:
            continue
    return None


def evaluate(
    *,
    expiry: Any,
    regime: Optional[str],
    today: Optional[date] = None,
    enabled: bool = None,
    hold_to_expiry: bool = False,
) -> DtePolicy:
    """The seller's DTE gate. Pure; no I/O.

    Returns allow=True with per-trade exemptions for near expiry, allow=False only
    for the buckets the 259-trade study shows are loss-making.

    `hold_to_expiry` EXEMPTS the position from the stand-down. This is not a
    loophole — it is the scope of the underlying measurement. Every trade in the
    259-trade study exited EARLY (86% of the book's exits were clock-driven,
    §21.2), so "DTE 3+ loses" is really "entering far from expiry and then being
    force-exited on a clock loses" — the worst of both worlds, since you pay full
    round-trip friction without ever collecting the decay you entered for.

    The 1,869-day structure shootout (2019-2026) tests the other branch directly:
    same signal, same window, HELD to expiry -> +Rs661/trade for the credit spread
    and +Rs908 for the debit, while every early-exit variant lost Rs247-339. The
    DTE law and the hold-to-expiry evidence are not in conflict; they measure
    different exits.

    This mirrors the exemption spread_builder already grants hold-to-expiry on the
    §21.2 reachability law ("theta gets its full remaining life"). A gate
    calibrated on intraday behaviour that silently binds a hold-to-expiry sleeve
    is the §22.3 defect class: one exit engine grants an exemption and another
    ignores it, so the strategy can never actually run.
    """
    on = _b("SELLER_DTE_POLICY_ENABLED", True) if enabled is None else enabled
    dte = dte_from_expiry(expiry, today=today)
    reg = str(regime or "").upper()
    tele = {"regime": reg, "policy_enabled": on, "hold_to_expiry": bool(hold_to_expiry)}

    if not on:
        return DtePolicy(dte, True, "DTE policy disabled", telemetry=tele)
    if hold_to_expiry and _b("DTE_POLICY_EXEMPT_HOLD_TO_EXPIRY", True):
        # Still reject an already-expired contract below; otherwise let it ride.
        if dte is None or dte >= 0:
            return DtePolicy(dte, True,
                             f"hold-to-expiry sleeve exempt from the DTE stand-down "
                             f"({dte}d) — the DTE study measured early exits only",
                             telemetry=tele)
    if dte is None:
        # Unknown expiry → no exemption, but do not block: the geometry laws in
        # spread_builder still judge the contract on its own numbers.
        return DtePolicy(dte, True, "DTE unknown — no near-expiry exemption", telemetry=tele)
    if dte < 0:
        return DtePolicy(dte, False, f"expiry already passed ({dte}d)", telemetry=tele)

    if dte <= DTE_ALL_REGIMES_MAX:
        return DtePolicy(
            dte, True,
            f"near expiry ({dte}d) — best measured bucket (DTE0 n=56 WR80% +Rs123); "
            "all regimes allowed",
            cost_floor_mult=NEAR_EXPIRY_COST_FLOOR_MULT,
            width_strikes=NEAR_EXPIRY_WIDTH_STRIKES,
            max_credit_ratio=NEAR_EXPIRY_MAX_CREDIT_RATIO,
            disable_time_exit=DISABLE_TIME_EXIT_NEAR_EXPIRY,
            near_expiry=True,
            telemetry=tele,
        )
    if dte <= DTE_STAND_DOWN_ABOVE:
        if reg in OWNED_REGIMES:
            return DtePolicy(dte, True, f"{dte}d to expiry in owned regime {reg}",
                             telemetry=tele)
        return DtePolicy(
            dte, False,
            f"{dte}d to expiry and regime {reg or 'UNKNOWN'} is not seller-owned "
            f"(CHOP at DTE 1-2 measured WR60% avg -Rs299)",
            telemetry=tele,
        )
    return DtePolicy(
        dte, False,
        f"{dte}d to expiry is past the seller's edge window "
        f"(DTE 3+ measured: 147 trades, WR 23-41%, avg -Rs121..-Rs1143)",
        telemetry=tele,
    )


def nearest_expiry_first(expiries: List[Any], *, today: Optional[date] = None) -> List[Any]:
    """Order candidate expiries so the selector prefers the profitable bucket.

    `target_dte_days` on a strategy row is DECORATIVE — no selection code has ever
    read it (§21.5) — so contracts were taken in whatever order the chain offered.
    Sorting by |DTE| puts the measured-best expiry first without any strategy
    config change.
    """
    scored = []
    for e in expiries or []:
        d = dte_from_expiry(e, today=today)
        scored.append((999 if d is None or d < 0 else d, e))
    return [e for _, e in sorted(scored, key=lambda t: t[0])]

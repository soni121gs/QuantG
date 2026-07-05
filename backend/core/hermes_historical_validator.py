"""HSI Stage-4 extension: validate lessons against the 2-year bhavcopy OOS engine.

The live-attribution validator (core/hermes_validator) is honest but data-starved —
it needs weeks of held-out real trades before an OOS window carries signal. This
module gives STRUCTURE-dimension lessons real statistical power NOW by testing their
directional claim against ~2 years of real NSE option prices (core/eod_options_backtest
walk_forward), the same engine Edge Lab uses.

Scope: only the `structure` dimension (credit_spread / debit_spread / single_leg /
iron_condor) — that is what the bhavcopy engine can price. Regime / exit_reason /
exposure_bias lessons keep using the live-attribution validator until data matures.

A lesson claims a DIRECTION (good = positive edge, bad = negative edge). We run the
structure's canonical strategy over the full window, take the walk-forward `overall`
expectancy + sample, and confirm the lesson only if the sign matches AND the sample
and effect size clear the gate. Deterministic; no LLM.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("quantg.hermes_historical_validator")

OOS_MIN_TRADES = int(os.environ.get("HERMES_HIST_MIN_TRADES", "30"))
OOS_MIN_EFFECT_INR = float(os.environ.get("HERMES_HIST_MIN_EFFECT_INR", "50"))

PASS = "HISTORICAL_PASS"
FAIL = "HISTORICAL_FAIL"
INSUFFICIENT = "INSUFFICIENT_DATA"
UNSUPPORTED = "UNSUPPORTED_DIMENSION"

# Canonical strategy per structure the bhavcopy engine can price. Each reproduces
# the structure's characteristic mechanics (income held-to-expiry vs intraday buyer)
# so the OOS sign reflects the structure's real edge, not a tuned config.
_CANONICAL: Dict[str, Dict[str, Any]] = {
    "credit_spread": {
        "options": {"structure": "credit_spread", "short_otm_pct": 0.03, "wing_width": 6, "spread_width": 6, "exit_mode": "expiry"},
        "params": {"short_otm_pct": 0.03, "wing_width": 6, "width": 6, "min_dte": 2, "max_dte": 8, "max_hold_days": 8, "exit_mode": "expiry", "credit_tp": 0.5, "credit_sl": 1.0},
    },
    "debit_spread": {
        "options": {"structure": "debit_spread", "spread_width": 2, "exit_mode": ""},
        "params": {"width": 2, "min_dte": 2, "max_dte": 8, "max_hold_days": 5, "exit_mode": "", "debit_tp": 1.0, "debit_sl": 0.5},
    },
    "single_leg": {
        "options": {"structure": "single_leg", "strike_mode": "ATM_BUY", "exit_mode": ""},
        "params": {"min_dte": 2, "max_dte": 8, "max_hold_days": 5, "exit_mode": ""},
    },
    "iron_condor": {
        "options": {"structure": "iron_condor", "short_otm_pct": 0.02, "wing_width": 6, "exit_mode": "expiry"},
        "params": {"short_otm_pct": 0.02, "wing_width": 6, "width": 6, "min_dte": 2, "max_dte": 8, "max_hold_days": 8, "exit_mode": "expiry"},
    },
}

SUPPORTED_STRUCTURES = tuple(_CANONICAL.keys())


def canonical_strategy(structure: str, underlying: str = "NIFTY") -> Optional[Dict[str, Any]]:
    spec = _CANONICAL.get(structure)
    if not spec:
        return None
    return {
        "id": f"hist_{structure}", "name": f"canonical {structure}",
        "python_code": "def run(data):\n    return [{'date': c['date'], 'action': 'BUY'} for c in data]\n",
        "visual_config": {"symbol": underlying, "options": {"underlying": underlying, **spec["options"]}},
        "_params": spec["params"],
    }


def historical_verdict(expectancy: float, n: int, direction: str,
                       min_n: int = OOS_MIN_TRADES, min_effect: float = OOS_MIN_EFFECT_INR) -> Dict[str, Any]:
    """Pure: confirm a lesson's direction against a historical edge measurement."""
    actual = "good" if expectancy > 0 else "bad"
    if n < min_n:
        status, passed = INSUFFICIENT, False
    elif actual == direction and abs(expectancy) >= min_effect:
        status, passed = PASS, True
    else:
        status, passed = FAIL, False
    return {"status": status, "passed": passed, "expectancy": round(expectancy, 2),
            "n": n, "direction_claimed": direction, "direction_actual": actual}


def run_structure_backtest(store, structure: str, underlying: str = "NIFTY") -> Dict[str, Any]:
    """Run the bhavcopy walk-forward for a structure's canonical strategy (sync)."""
    from core.eod_options_backtest import EODOptionsBacktest, walk_forward
    strat = canonical_strategy(structure, underlying)
    if not strat:
        return {"verdict": UNSUPPORTED, "overall": {"n": 0, "expectancy": 0.0}}
    res = EODOptionsBacktest(store).run(strat, None, None, 100_000.0, strat["_params"])
    if res.get("error"):
        return {"verdict": INSUFFICIENT, "overall": {"n": 0, "expectancy": 0.0}, "error": res["error"]}
    return walk_forward(res)


async def validate_structure_lessons(
    db, user_id: str, *,
    underlying: str = "NIFTY",
    backtest_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Validate every structure-dimension lesson against the 2yr bhavcopy OOS engine
    and stamp oos_status / oos_passed / last_oos_result on it. `backtest_fn(structure)`
    is injectable for tests; the default runs the real bhavcopy backtester off-thread."""
    import asyncio

    if backtest_fn is None:
        from core.bhavcopy_store import BhavcopyStore
        store = BhavcopyStore()

        async def _bt(structure: str) -> Dict[str, Any]:
            return await asyncio.to_thread(run_structure_backtest, store, structure, underlying)
    else:
        async def _bt(structure: str) -> Dict[str, Any]:
            res = backtest_fn(structure)
            return res

    lessons = await db.hermes_lessons.find(
        {"user_id": user_id, "dimension": "structure"}).to_list(100)
    wf_cache: Dict[str, Dict[str, Any]] = {}
    updated, results = 0, []
    for lsn in lessons:
        structure = lsn.get("bucket")
        if structure not in SUPPORTED_STRUCTURES:
            continue
        if structure not in wf_cache:
            wf_cache[structure] = await _bt(structure)
        wf = wf_cache[structure]
        overall = wf.get("overall") or {}
        verdict = historical_verdict(
            float(overall.get("expectancy") or 0.0), int(overall.get("n") or 0), lsn.get("direction"))
        patch = {
            "oos_status": verdict["status"],
            "oos_passed": verdict["passed"],
            "oos_source": "bhavcopy_2yr",
            "oos_validated_at": datetime.now(timezone.utc).isoformat(),
            "last_oos_result": {**verdict, "wf_verdict": wf.get("verdict"),
                                "by_year": wf.get("by_year"), "underlying": underlying},
        }
        await db.hermes_lessons.update_one({"lesson_id": lsn["lesson_id"]}, {"$set": patch})
        updated += 1
        results.append({"lesson_id": lsn["lesson_id"], "bucket": structure,
                        "direction": lsn.get("direction"), **verdict})
    return {"validated": updated, "structures_tested": list(wf_cache.keys()), "results": results}

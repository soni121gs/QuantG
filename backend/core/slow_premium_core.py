"""ERP P3-3 slow premium core restore validators.

Research-only configs for QG-O1 held/gated geometry and a SENSEX monthly
defined-risk strangle candidate. No registry wake or broker path is touched.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.edge_research_ledger import deflated_sharpe
from core.eod_options_backtest import EODOptionsBacktest, walk_forward


def _richness_for(store: BhavcopyStore, underlying: str, day: str) -> Dict[str, Any]:
    try:
        from core.iv_surface import richness_zscore
        return richness_zscore(store, underlying, day)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc), "richness": {"available": False}}


def build_richness_signals(candles: List[Dict[str, Any]], *, store: BhavcopyStore,
                           underlying: str, min_z: float = 0.75,
                           direction: str = "CE", entry_stride_days: int = 5) -> Dict[str, Any]:
    signals: List[Dict[str, Any]] = []
    rejected = {"not_rich": 0, "surface_unavailable": 0, "stride_skip": 0}
    stride = max(1, int(entry_stride_days or 1))
    for idx, c in enumerate(candles):
        if idx % stride != 0:
            rejected["stride_skip"] += 1
            continue
        day = str(c.get("date"))[:10]
        surf = _richness_for(store, underlying, day)
        rich = surf.get("richness") or {}
        if not rich.get("available"):
            rejected["surface_unavailable"] += 1
            continue
        z = float(rich.get("zscore") or 0.0)
        if z < min_z:
            rejected["not_rich"] += 1
            continue
        signals.append({"date": c["date"], "action": "BUY", "direction": direction,
                        "iv_richness_z": round(z, 3), "gate": "iv_surface_rich"})
    return {"signals": signals, "audit": {"underlying": underlying.upper(), "min_z": min_z,
                                          "entry_stride_days": stride, "accepted": len(signals),
                                          "rejected": rejected}}


def qgo1_held_config() -> Dict[str, Any]:
    return {
        "id": "S4-QGO1-HELD",
        "name": "S4 QG-O1 Held Rich Put Spread Restore",
        "python_code": "def run(data):\n    return []\n",
        "visual_config": {"symbol": "NIFTY", "options": {
            "underlying": "NIFTY",
            "structure": "credit_spread",
            "short_otm_pct": 0.03,
            "wing_width": 10,
            "spread_width": 10,
            "exit_mode": "expiry",
            "lots": 1,
        }, "risk": {"exit_mode": "hold_to_expiry", "max_hold_days": 8}},
    }


def sensex_monthly_strangle_config() -> Dict[str, Any]:
    return {
        "id": "S4-SENSEX-MONTHLY-IC",
        "name": "S4 SENSEX Monthly Defined-Risk Strangle",
        "python_code": "def run(data):\n    return []\n",
        "visual_config": {"symbol": "SENSEX", "options": {
            "underlying": "SENSEX",
            "structure": "iron_condor",
            "short_otm_pct": 0.025,
            "wing_width": 4,
            "spread_width": 4,
            "exit_mode": "expiry",
            "lots": 1,
        }, "risk": {"exit_mode": "hold_to_expiry", "max_hold_days": 35}},
    }


def _grade(cfg: Dict[str, Any], *, store: BhavcopyStore, start: Optional[str],
           end: Optional[str], min_z: float, params: Dict[str, Any],
           direction: str = "CE", entry_stride_days: int = 5) -> Dict[str, Any]:
    underlying = ((cfg.get("visual_config") or {}).get("options") or {}).get("underlying")
    candles = store.underlying_daily(underlying, start, end)
    if len(candles) < 80:
        return {"name": cfg["name"], "underlying": underlying, "status": "error",
                "error": f"insufficient bhavcopy history for {underlying} ({len(candles)} days)"}
    sig = build_richness_signals(candles, store=store, underlying=underlying, min_z=min_z,
                                 direction=direction, entry_stride_days=entry_stride_days)
    res = EODOptionsBacktest(store).run(cfg, start=start, end=end, params=params, signals=sig["signals"])
    if res.get("error"):
        return {"name": cfg["name"], "underlying": underlying, "status": "error", "error": res["error"],
                "selector": sig["audit"]}
    wf = walk_forward(res)
    overall = wf.get("overall") or {}
    dsr = deflated_sharpe({
        "n": overall.get("n"),
        "expectancy": overall.get("expectancy"),
        "oos_expectancy": (wf.get("oos") or {}).get("expectancy"),
        "pct_green_months": wf.get("pct_green_months"),
    })
    return {
        "name": cfg["name"], "underlying": underlying, "status": "ready",
        "selector": sig["audit"], "structure": res.get("structure"),
        "verdict": wf.get("verdict"), "overall": overall,
        "oos": wf.get("oos"), "oos_year": wf.get("oos_year"),
        "pct_green_months": wf.get("pct_green_months"),
        "paper_gate": {"eligible_for_paper": bool(wf.get("verdict") == "CANDIDATE_EDGE" and dsr.get("passed")),
                       "deflated_sharpe": dsr, "iv_richness_gate": {"min_z": min_z}},
    }


def grade_slow_premium(*, store: Optional[BhavcopyStore] = None, start: Optional[str] = None,
                       end: Optional[str] = None, min_z: float = 0.75,
                       entry_stride_days: int = 5) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    qgo1 = _grade(qgo1_held_config(), store=store, start=start, end=end, min_z=min_z,
                  direction="CE", params={"short_otm_pct": 0.03, "wing_width": 10, "width": 10,
                                          "min_dte": 2, "max_dte": 8, "max_hold_days": 8,
                                          "exit_mode": "expiry"},
                  entry_stride_days=entry_stride_days)
    sensex = _grade(sensex_monthly_strangle_config(), store=store, start=start, end=end, min_z=min_z,
                    direction="SHORT_VOL", params={"short_otm_pct": 0.025, "wing_width": 4, "width": 4,
                                                    "min_dte": 21, "max_dte": 45, "max_hold_days": 35,
                                                   "exit_mode": "expiry"},
                    entry_stride_days=max(entry_stride_days, 20))
    rows = [qgo1, sensex]
    return {"count": len(rows), "eligible_for_paper": sum(
        1 for r in rows if (r.get("paper_gate") or {}).get("eligible_for_paper")), "rows": rows}

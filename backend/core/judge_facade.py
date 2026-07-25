"""ERP P2-4: one small facade over QuantG's research judges.

The existing EOD, intraday and regime judges stay as their domain engines. This
module normalizes their outputs so API/UI/Hermes code can ask for `grade(...)`
without learning each historical script's shape.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.bhavcopy_store import BhavcopyStore
from core.eod_options_backtest import EODOptionsBacktest, walk_forward


def _strategy_underlying(strategy_cfg: Dict[str, Any]) -> str:
    vc = strategy_cfg.get("visual_config") or {}
    opt = vc.get("options") or {}
    return str(opt.get("underlying") or vc.get("symbol") or "NIFTY").upper()


def _event_dates_for(strategy_cfg: Dict[str, Any], start: Optional[str], end: Optional[str]) -> list[str]:
    try:
        from core.earnings_calendar import events_for
        rows = events_for(_strategy_underlying(strategy_cfg), start or "1900-01-01", end or "2999-12-31")
        return [str(r.get("date"))[:10] for r in rows if r.get("date")]
    except Exception:  # noqa: BLE001
        return []


def grade(
    strategy_cfg: Dict[str, Any],
    *,
    mode: str = "eod",
    start: Optional[str] = None,
    end: Optional[str] = None,
    store: Optional[BhavcopyStore] = None,
    params: Optional[Dict[str, Any]] = None,
    signals: Optional[list[dict]] = None,
) -> Dict[str, Any]:
    """Return one normalized judge payload.

    Modes:
      - eod: held-to-theta daily bhavcopy judge
      - event: same EOD pricing engine, but only signals inside event windows
      - regime/intraday: normalized placeholders for their existing async/CLI
        engines; API callers should use the persisted endpoints until those
        runners are folded behind this pure facade.
    """
    m = str(mode or "eod").lower()
    now = datetime.now(timezone.utc).isoformat()
    if m in {"eod", "event"}:
        store = store or BhavcopyStore()
        run_params = dict(params or {})
        if m == "event":
            events = run_params.get("event_dates") or _event_dates_for(strategy_cfg, start, end)
            run_params["event_dates"] = events
            run_params.setdefault("event_window_days", 1)
        res = EODOptionsBacktest(store).run(
            strategy_cfg, start=start, end=end, params=run_params, signals=signals,
        )
        if res.get("error"):
            return {"mode": m, "status": "error", "strategy_id": strategy_cfg.get("id"),
                    "name": strategy_cfg.get("name"), "error": res["error"], "generated_at": now}
        wf = walk_forward(res)
        overall = wf.get("overall") or {}
        return {
            "mode": m,
            "status": "ready",
            "strategy_id": strategy_cfg.get("id"),
            "name": res.get("name") or strategy_cfg.get("name"),
            "underlying": res.get("underlying"),
            "structure": res.get("structure"),
            "verdict": wf.get("verdict"),
            "overall": overall,
            "oos": wf.get("oos"),
            "oos_year": wf.get("oos_year"),
            "oos_trades": wf.get("oos_trades") or [],
            "pct_green_months": wf.get("pct_green_months"),
            "signals": res.get("signals", 0),
            "signal_evaluation": res.get("signal_evaluation"),
            "event_filter": res.get("event_filter"),
            "regime_breakdown": res.get("regime_breakdown") or {},
            "trades": res.get("trades") or [],
            "generated_at": now,
        }
    if m in {"intraday", "regime"}:
        return {"mode": m, "status": "external_runner",
                "strategy_id": strategy_cfg.get("id"), "name": strategy_cfg.get("name"),
                "generated_at": now,
                "note": f"{m} judge is served by its existing persisted run endpoint."}
    return {"mode": m, "status": "error", "strategy_id": strategy_cfg.get("id"),
            "name": strategy_cfg.get("name"), "error": f"unknown judge mode: {mode}",
            "generated_at": now}

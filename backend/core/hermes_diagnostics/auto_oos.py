"""Auto-OOS — answer 'does this losing strategy have an edge?' automatically.

When the Diagnostician flags a strategy as an edge doubt (persistent live loss or
structurally bad geometry), we don't just file the question — we ANSWER it by
replaying the strategy over the REAL stored data and attaching the walk-forward
verdict to its research hypothesis, with zero manual backtest trigger.

Judge selection (correctness matters — §14/§15):
  • held-to-theta / expiry structures → EOD bhavcopy judge (daily settle, 2yr store)
  • intraday scalps (tp/sl/time exits, 1-min)  → IMD 1-minute judge (option+index minute stores)
Grading a scalp on the daily judge wrongly calls it negative, so we route by shape.

Data law (this is the whole point of the founder's ask): the judges return
INSUFFICIENT_DATA / DATA_QUALITY_FAIL when the real store is thin, holey, or absent
— we NEVER fabricate a verdict on missing data. The data COVERAGE (days, range,
missing-rate) is attached to every verdict so a result on partial data is labelled,
not trusted. NIFTY/BANKNIFTY have 1-min option data; SENSEX/BANKEX do not (§14.2),
so a SENSEX scalp honestly returns INSUFFICIENT_DATA rather than a made-up number.

Cost: bounded (HERMES_AUTO_OOS_MAX strategies/run) and run in a thread so it never
blocks the EOD loop. Only invoked from the EOD diagnostic pass, not the manual one.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger("quantg.hermes_diagnostics")

_ENABLED = os.environ.get("HERMES_AUTO_OOS", "true").lower() == "true"
_MAX = int(os.environ.get("HERMES_AUTO_OOS_MAX", "3"))
_EDGE_PROBES = {"strategy.persistent_live_loss", "static.reward_risk_geometry"}
_LEDGER_VERDICTS = {"CANDIDATE_EDGE", "FRAGILE", "NO_EDGE_NEGATIVE",
                    "INSUFFICIENT_DATA", "REJECTED"}


def _is_intraday(strat: Dict[str, Any]) -> bool:
    vc = strat.get("visual_config") or {}
    opt = vc.get("options") or {}
    risk = vc.get("risk") or {}
    if str(opt.get("exit_mode") or "").lower() == "expiry":
        return False
    if float(risk.get("time_exit_minutes") or 0) > 0:
        return True
    if str(opt.get("candle_interval") or "").lower() in ("1minute", "1min", "1"):
        return True
    # Default: a non-expiry credit/debit spread with tp/sl is an intraday structure.
    return str(opt.get("structure") or "").endswith("spread")


def _run_eod(strat: Dict[str, Any]) -> Dict[str, Any]:
    from core.eod_options_backtest import EODOptionsBacktest, walk_forward
    from core.bhavcopy_store import BhavcopyStore
    store = BhavcopyStore()
    days = store.trading_days()
    if not days:
        return {"verdict": "INSUFFICIENT_DATA", "judge": "eod_bhavcopy",
                "reason": "bhavcopy store empty", "coverage": {}}
    res = EODOptionsBacktest(store).run(strat)
    cov = {"days": len(days), "first": days[0], "last": days[-1]}
    if res.get("error"):
        return {"verdict": "INSUFFICIENT_DATA", "judge": "eod_bhavcopy",
                "reason": res["error"], "coverage": cov}
    wf = walk_forward(res)
    return {"verdict": wf["verdict"], "judge": "eod_bhavcopy",
            "n": wf["overall"]["n"], "overall": wf["overall"], "oos": wf.get("oos"),
            "pct_green_months": wf.get("pct_green_months"), "coverage": cov}


def _run_intraday(strat: Dict[str, Any]) -> Dict[str, Any]:
    from core.options_minute_store import OptionsMinuteStore
    from core.index_minute_store import IndexMinuteStore
    from core.intraday_options_backtest import run_day
    from core.intraday_options_oos import evaluate_strategy
    code = strat.get("python_code")
    if not code:
        return {"verdict": "INSUFFICIENT_DATA", "judge": "imd_1min", "reason": "no python_code"}
    vc = strat.get("visual_config") or {}
    opt = vc.get("options") or {}
    u = str(opt.get("underlying") or "NIFTY").upper()
    source = "upstox"
    store = OptionsMinuteStore()
    index_store = IndexMinuteStore()
    days = store.trading_days(source, u)
    if not days:
        return {"verdict": "INSUFFICIENT_DATA", "judge": "imd_1min",
                "reason": f"no 1-min option data for {u} (SENSEX/BANKEX not fetched)",
                "coverage": {"option_days": 0}}
    ns: Dict[str, Any] = {}
    try:
        exec(compile(code, "<strategy>", "exec"), ns)  # noqa: S102 - in-repo template
    except Exception as exc:
        return {"verdict": "INSUFFICIENT_DATA", "judge": "imd_1min",
                "reason": f"python_code compile error: {exc}"}
    run = ns.get("run")
    if not callable(run):
        return {"verdict": "INSUFFICIENT_DATA", "judge": "imd_1min",
                "reason": "python_code has no run(data)"}

    def signal_fn(history):
        try:
            out = run(history)
        except Exception:
            return None
        return out[-1] if isinstance(out, list) and out else None

    all_trades: List[Dict[str, Any]] = []
    missing = 0
    for d in days:
        umin = index_store.get_minutes(u, d)
        if not umin:
            missing += 1
            continue
        trades = run_day(
            underlying=u, date=d, underlying_minutes=umin, signal_fn=signal_fn,
            chain_at=(lambda ts, _u=u, _d=d: store.get_chain_at_time(source, _u, _d, ts)),
            option_series=store.all_series_for_day(source, u, d),
        )
        all_trades.extend(t.__dict__ for t in trades)
    missing_rate = missing / len(days) if days else 1.0
    ev = evaluate_strategy(strat.get("name") or u, all_trades, missing_rate=missing_rate)
    return {"verdict": ev["verdict"], "judge": "imd_1min", "n": ev["trades"],
            "overall": ev["overall"], "oos": ev.get("oos"),
            "green_month_pct": ev.get("green_month_pct"),
            "coverage": {"option_days": len(days), "first": days[0], "last": days[-1],
                         "missing_rate": round(missing_rate, 3)}}


def run_auto_oos_sync(strat: Dict[str, Any]) -> Dict[str, Any]:
    """Pick the right judge and run it on real stored data. Sync; call via thread."""
    try:
        return _run_intraday(strat) if _is_intraday(strat) else _run_eod(strat)
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto-OOS run failed for %s: %s", strat.get("id"), exc)
        return {"verdict": "INSUFFICIENT_DATA", "judge": "error", "reason": str(exc)}


async def attach_auto_oos(db, user_id: str, strat: Dict[str, Any]) -> Dict[str, Any]:
    """Run the judge and attach its verdict + coverage to the strategy's hypothesis."""
    from core.research_bridge import upsert_strategy_edge
    res = await asyncio.to_thread(run_auto_oos_sync, strat)
    verdict = res.get("verdict")
    if verdict not in _LEDGER_VERDICTS:
        return res
    sid = str(strat.get("id") or strat.get("strategy_id"))
    opt = (strat.get("visual_config") or {}).get("options") or {}
    metrics = {k: res.get(k) for k in ("overall", "oos", "pct_green_months",
                                       "green_month_pct", "judge", "coverage") if res.get(k) is not None}
    summary = f"{res.get('judge')} auto-OOS on real stored data: {verdict}"
    if res.get("reason"):
        summary += f" — {res['reason']}"
    await upsert_strategy_edge(
        db, user_id, strategy_id=sid, name=strat.get("name") or sid,
        instrument=opt.get("underlying"), structure=opt.get("structure"),
        timeframe=("1-min intraday" if res.get("judge") == "imd_1min" else "EOD / held-to-theta"),
        source=f"auto_oos:{res.get('judge')}",
        evidence={"source": f"auto_oos:{res.get('judge')}", "summary": summary,
                  "metrics": metrics, "sample_n": int(res.get("n") or 0)},
        verdict={"status": verdict, "summary": summary, "metrics": metrics,
                 "sample_n": int(res.get("n") or 0)},
    )
    return res


async def run_auto_oos_for_findings(db, user_id: str, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Grade the strategies behind edge-doubt findings, bounded and best-effort."""
    if not _ENABLED:
        return {"enabled": False, "graded": 0}
    graded = 0
    verdicts: Dict[str, str] = {}
    seen: set = set()
    for f in findings:
        if f.get("probe_id") not in _EDGE_PROBES:
            continue
        ev = f.get("evidence") or {}
        sid = ev.get("strategy_id") or f.get("entity")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if graded >= _MAX:
            break
        strat = await db.strategies.find_one({"$or": [{"id": sid}, {"strategy_id": sid}]})
        if not strat:
            continue
        try:
            res = await attach_auto_oos(db, user_id, strat)
            verdicts[sid] = res.get("verdict")
            graded += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto-OOS attach failed for %s: %s", sid, exc)
    if graded:
        logger.info("Hermes auto-OOS graded=%d verdicts=%s", graded, verdicts)
    return {"enabled": True, "graded": graded, "verdicts": verdicts}

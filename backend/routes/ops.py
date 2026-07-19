from __future__ import annotations

import os
import math
import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core import db, get_current_user

router = APIRouter(prefix="/ops", tags=["Operations"])


def _pymongo_db():
    """Synchronous pymongo handle for CPU-bound research jobs run in a thread
    (the app `db` is motor/async and cannot be used off the event loop)."""
    import pymongo
    url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    return pymongo.MongoClient(url, serverSelectionTimeoutMS=4000)[
        os.environ.get("DB_NAME", "quantg")]


def _json_safe(obj):
    """Recursively replace NaN/inf floats with None so FastAPI's JSON encoder
    (which rejects non-finite floats) can serialize research payloads. OOS/coverage
    docs can carry inf (division-by-zero profit factor) or NaN (empty-sample mean)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj

class OpsActionReq(BaseModel):
    note: Optional[str] = None
    confirm: Optional[bool] = False


class ResearchHypothesisReq(BaseModel):
    hypothesis: str
    market_premise: Optional[str] = None
    instrument: Optional[str] = None
    timeframe: Optional[str] = None
    entry_exit_idea: Optional[str] = None
    data_window: Optional[Dict[str, Any]] = None
    null_hypothesis: Optional[str] = None
    expected_edge: Optional[Dict[str, Any]] = None
    cost_model: Optional[Dict[str, Any]] = None
    risk_thesis: Optional[str] = None
    failure_modes: Optional[List[str]] = None
    evidence_links: Optional[List[Dict[str, Any]]] = None
    research_context: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None


class ResearchEvidenceReq(BaseModel):
    source: str
    summary: Optional[str] = ""
    metrics: Optional[Dict[str, Any]] = None
    sample_n: Optional[int] = 0
    confidence: Optional[float] = 0.0
    limitations: Optional[List[str]] = None


class ResearchVerdictReq(BaseModel):
    status: str
    summary: Optional[str] = ""
    confidence: Optional[float] = 0.0
    sample_n: Optional[int] = 0
    metrics: Optional[Dict[str, Any]] = None
    limitations: Optional[List[str]] = None


class FrozenEvidenceReq(BaseModel):
    source_type: str
    content: Any
    source_uri: Optional[str] = None
    observed_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None


RECONCILIATION_RESOLVE_PHRASE = "RESOLVE_RECONCILIATION_AFTER_MANUAL_BROKER_CHECK"


def _public_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    return out


async def _append_ops_audit_event(user_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    body = payload or {}
    event_id = f"ops_{uuid.uuid4().hex}"
    event = {
        "id": event_id,
        "user_id": user_id,
        "event_type": event_type,
        "payload": body,
        "created_at": now,
        "source": "ops",
    }
    await db["risk_events"].insert_one(event)
    await db["outbox_events"].insert_one({
        "id": f"outbox_{uuid.uuid4().hex}",
        "aggregate_type": "ops",
        "aggregate_id": user_id,
        "event_type": event_type,
        "payload": body,
        "user_id": user_id,
        "status": "pending",
        "created_at": now,
    })


async def _reconciliation_break_snapshot(user_id: str) -> Dict[str, Any]:
    unknown_orders = await db["orders"].find(
        {"user_id": user_id, "mode": "live", "status": "UNKNOWN_NEEDS_REVIEW"},
        {"_id": 0},
    ).to_list(500)
    review_reservations = await db["risk_reservations"].find(
        {"user_id": user_id, "status": "NEEDS_REVIEW"},
        {"_id": 0},
    ).to_list(500)
    active_reservations = await db["risk_reservations"].find(
        {"user_id": user_id, "status": "ACTIVE"},
        {"_id": 0},
    ).to_list(500)
    active_positions = await db["strategy_positions"].find(
        {
            "user_id": user_id,
            "mode": "live",
            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
        },
        {"_id": 0},
    ).to_list(500)
    recon_state = await db["risk_state"].find_one({
        "$or": [
            {"_id": f"position_reconciliation:{user_id}"},
            {"_id": "position_reconciliation", "$or": [{"user_id": user_id}, {"user_id": {"$exists": False}}]},
        ]
    }) or {}
    mismatch_detected = bool(recon_state.get("mismatch_detected"))
    blockers = {
        "position_reconciliation_mismatch": mismatch_detected,
        "unknown_live_orders": len(unknown_orders),
        "reservations_needing_review": len(review_reservations),
        "active_live_reservations": len(active_reservations),
    }
    can_resolve_position_reconciliation = (
        mismatch_detected
        and blockers["unknown_live_orders"] == 0
        and blockers["reservations_needing_review"] == 0
    )
    return {
        "ok": True,
        "user_id": user_id,
        "reconciliation_state": _public_doc(recon_state),
        "blockers": blockers,
        "unknown_live_orders": unknown_orders,
        "reservations_needing_review": review_reservations,
        "active_live_reservations": active_reservations,
        "active_live_strategy_positions": active_positions,
        "can_resolve_position_reconciliation": can_resolve_position_reconciliation,
        "resolve_required_note": RECONCILIATION_RESOLVE_PHRASE,
    }


@router.get("/diagnostics")
async def ops_diagnostics_route(user=Depends(get_current_user)):
    from server import ops_diagnostics
    return await ops_diagnostics(user=user)


@router.get("/hermes-diagnostics")
async def hermes_diagnostics_list(status: str = "open", user=Depends(get_current_user)):
    """§19 Hermes Diagnostician read surface: open findings (severe-first) + the
    latest run's narrated briefing. Read-only. status=open|resolved|all."""
    uid = user["id"]
    q: Dict[str, Any] = {"user_id": uid}
    if status in ("open", "resolved"):
        q["status"] = status
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings = await db.hermes_findings.find(q, {"_id": 0}).to_list(500)
    findings.sort(key=lambda f: (sev_rank.get(f.get("severity"), 9),
                                 str(f.get("last_seen_date") or "")))
    latest_run = await db.hermes_diagnostic_runs.find_one(
        {"user_id": uid}, {"_id": 0}, sort=[("date", -1)])
    return {
        "findings": findings,
        "counts": {
            "open": await db.hermes_findings.count_documents({"user_id": uid, "status": "open"}),
            "critical": await db.hermes_findings.count_documents(
                {"user_id": uid, "status": "open", "severity": "critical"}),
            "high": await db.hermes_findings.count_documents(
                {"user_id": uid, "status": "open", "severity": "high"}),
        },
        "latest_run": latest_run,
    }


@router.post("/hermes-diagnostics/run")
async def hermes_diagnostics_run(date: Optional[str] = None, user=Depends(get_current_user)):
    """Manually trigger a diagnostic audit for the caller (defaults to today IST).
    Runs all probes, persists findings, and narrates. Read-only w.r.t. trading."""
    from core.hermes_diagnostics import run_diagnostics
    from core.hermes_diagnostics.narrator import narrate_findings
    res = await run_diagnostics(db, user["id"], date)
    narrative = await narrate_findings(db, user["id"], res["date"], res.get("findings", []))
    return {**res, "narrative": narrative}


@router.get("/hermes-fix-tasks")
async def hermes_fix_tasks_list(status: str = "active", user=Depends(get_current_user)):
    """Auto-filed fix-tasks queue (high-severity findings promoted to actionable
    tickets). status=active (open+in_progress) | done | all. Read-only surface."""
    uid = user["id"]
    q: Dict[str, Any] = {"user_id": uid}
    if status == "active":
        q["status"] = {"$in": ["open", "in_progress"]}
    elif status in ("open", "in_progress", "done", "auto_closed", "dismissed"):
        q["status"] = status
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    tasks = await db.hermes_fix_tasks.find(q, {"_id": 0}).to_list(500)
    tasks.sort(key=lambda t: (sev_rank.get(t.get("severity"), 9),
                              str(t.get("created_at") or "")))
    return {
        "tasks": tasks,
        "counts": {
            "active": await db.hermes_fix_tasks.count_documents(
                {"user_id": uid, "status": {"$in": ["open", "in_progress"]}}),
        },
    }


class _FixTaskStatus(BaseModel):
    status: str  # open | in_progress | done | dismissed


@router.post("/hermes-fix-tasks/{key}/status")
async def hermes_fix_task_set_status(key: str, body: _FixTaskStatus,
                                     user=Depends(get_current_user)):
    """Founder moves a fix-task through its lifecycle (open→in_progress→done, or
    dismissed). Auto_closed is system-only and not settable here."""
    allowed = {"open", "in_progress", "done", "dismissed"}
    if body.status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(allowed)}")
    from datetime import datetime as _dt, timezone as _tz
    res = await db.hermes_fix_tasks.update_one(
        {"user_id": user["id"], "key": key},
        {"$set": {"status": body.status,
                  "status_changed_at": _dt.now(_tz.utc).isoformat()}},
    )
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="fix-task not found")
    return {"ok": True, "key": key, "status": body.status}


@router.get("/scorecard")
async def ops_scorecard(days: int = 7, scope: str = "me", user=Depends(get_current_user)):
    """Phase 0 eval harness: per-strategy signal funnel + realised P&L, with a
    health verdict (silent / throttled / lossy / healthy / idle). scope=all is
    owner-only (cross-account); default scope=me is the caller's strategies."""
    from research_scorecard import build_scorecard
    uid: Optional[str] = user["id"]
    if scope == "all":
        if user.get("role") != "owner":
            raise HTTPException(status_code=403, detail="scope=all requires owner role")
        uid = None
    days = max(1, min(int(days or 7), 90))
    return await build_scorecard(db, days=days, user_id=uid)


@router.get("/risk-scorecard")
async def ops_risk_scorecard(
    since: Optional[str] = None,
    scope: str = "me",
    clean: bool = True,
    include_empty: bool = True,
    user=Depends(get_current_user),
):
    """Risk-adjusted scorecard from REALIZED trades (db.trades): Sharpe, Sortino,
    profit-factor, expectancy, max-drawdown and an A–F grade per strategy — ranks
    strategies by EDGE, not raw P&L. `since` (ISO) scores only a clean window;
    scope=all is owner-only."""
    from core.strategy_scorecard import (
        CLEAN_STATS_DEFAULT_SINCE,
        build_scorecard as build_risk_scorecard,
        summarize_by_structure,
        summarize_verdicts,
    )
    uid: Optional[str] = user["id"]
    if scope == "all":
        if user.get("role") != "owner":
            raise HTTPException(status_code=403, detail="scope=all requires owner role")
        uid = None
    effective_since = since or (CLEAN_STATS_DEFAULT_SINCE if clean else None)
    rows = await build_risk_scorecard(
        db, user_id=uid, since_iso=effective_since, clean=clean, include_empty=include_empty
    )
    return {
        "rows": rows,
        "by_structure": summarize_by_structure(rows),
        "verdicts": summarize_verdicts(rows),
        "stats_window": {
            "mode": "clean" if effective_since else "lifetime",
            "since": effective_since,
            "clean_default_since": CLEAN_STATS_DEFAULT_SINCE,
            "lifetime_available": True,
        },
    }


@router.get("/hermes-oos-validation")
async def ops_hermes_oos_validation(user=Depends(get_current_user)):
    from core.hermes_validator import get_validation_summary

    return await get_validation_summary(db, user["id"])


@router.post("/hermes-oos-validation/run")
async def ops_run_hermes_oos_validation(limit: int = 25, user=Depends(get_current_user)):
    from core.hermes_validator import validate_lessons

    return await validate_lessons(db, user["id"], limit=limit)


@router.post("/hermes-oos-validation/historical")
async def ops_run_hermes_historical_validation(underlying: str = "NIFTY", user=Depends(get_current_user)):
    """Validate STRUCTURE-dimension lessons against the 2yr bhavcopy OOS engine (real
    statistical power now, vs waiting weeks for live attribution). Heavy (prices 2yr
    chains per structure) — run occasionally. Refreshes the Hermes advice surface after."""
    from core.hermes_historical_validator import validate_structure_lessons
    result = await validate_structure_lessons(db, user["id"], underlying=underlying)
    try:
        from core.hermes_advisor import compile_hermes_advice
        await compile_hermes_advice(db, user["id"])
    except Exception as exc:  # noqa: BLE001
        result["advice_refresh_error"] = str(exc)
    return result


@router.post("/research/hypotheses")
async def ops_create_research_hypothesis(req: ResearchHypothesisReq, user=Depends(get_current_user)):
    from core.research_ledger import create_hypothesis

    try:
        return await create_hypothesis(db, user["id"], req.model_dump(exclude_none=True), created_by="operator")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/research/hypotheses")
async def ops_list_research_hypotheses(
    status: Optional[str] = None,
    limit: int = 50,
    user=Depends(get_current_user),
):
    from core.research_ledger import list_hypotheses

    return await list_hypotheses(db, user["id"], status=status, limit=limit)


@router.get("/research/hypotheses/{hypothesis_id}")
async def ops_get_research_hypothesis(hypothesis_id: str, user=Depends(get_current_user)):
    from core.research_ledger import get_hypothesis

    try:
        return await get_hypothesis(db, user["id"], hypothesis_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Research hypothesis not found")


@router.post("/research/hypotheses/{hypothesis_id}/evidence")
async def ops_attach_research_evidence(
    hypothesis_id: str,
    req: ResearchEvidenceReq,
    user=Depends(get_current_user),
):
    from core.research_ledger import attach_evidence

    try:
        return await attach_evidence(db, user["id"], hypothesis_id, req.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(status_code=404, detail="Research hypothesis not found")


@router.post("/research/hypotheses/{hypothesis_id}/verdict")
async def ops_set_research_verdict(
    hypothesis_id: str,
    req: ResearchVerdictReq,
    user=Depends(get_current_user),
):
    from core.research_ledger import set_verdict

    try:
        return await set_verdict(db, user["id"], hypothesis_id, req.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(status_code=404, detail="Research hypothesis not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/research/evidence")
async def ops_freeze_research_evidence(req: FrozenEvidenceReq, user=Depends(get_current_user)):
    from core.evidence_store import freeze_evidence

    try:
        return await freeze_evidence(db, user["id"], req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/research/evidence")
async def ops_list_research_evidence(
    source_type: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 50,
    user=Depends(get_current_user),
):
    from core.evidence_store import list_evidence

    return await list_evidence(db, user["id"], source_type=source_type, as_of=as_of, limit=limit)


@router.get("/research/evidence/{evidence_snapshot_id}")
async def ops_get_research_evidence(evidence_snapshot_id: str, user=Depends(get_current_user)):
    from core.evidence_store import get_evidence

    try:
        return await get_evidence(db, user["id"], evidence_snapshot_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Frozen evidence snapshot not found")


# FE-03 (2026-07-04): the old in-sample option-priced backtester (/options-backtest
# + core/options_backtest.py) was RETIRED — it graded on limited collected chain
# history with no walk-forward and was proven misleading. Use /eod-options-backtest
# (2yr real bhavcopy, out-of-sample verdict) below; the Edge Lab UI already does.


@router.post("/eod-options-backtest")
async def ops_eod_options_backtest(
    strategy_id: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    user=Depends(get_current_user),
):
    """Out-of-sample backtest over the FREE NSE bhavcopy store (2 years of real EOD
    option settlement prices) — the engine that answers 'does this strategy have an
    edge?'. Prices single_leg/credit/debit spreads settle-to-settle with theta,
    expiry, brokerage + slippage; returns a per-year (OOS=latest) walk-forward
    verdict (CANDIDATE_EDGE / FRAGILE / NO_EDGE_NEGATIVE / INSUFFICIENT_DATA).
    Omit strategy_id to grade all the caller's option strategies. Read-only.

    Requires the bhavcopy store mounted at /app/data/bhavcopy_fo (docker-compose
    volume) — returns an empty result with `data_available=false` if absent."""
    import asyncio
    from core.eod_options_backtest import EODOptionsBacktest, walk_forward
    from core.bhavcopy_store import BhavcopyStore

    store = BhavcopyStore()
    days = await asyncio.to_thread(store.trading_days)
    if not days:
        return {"data_available": False, "count": 0, "results": [],
                "hint": "bhavcopy store empty — run scripts/bhavcopy_ingest.py and mount /app/data/bhavcopy_fo"}

    engine = EODOptionsBacktest(store)
    query: Dict[str, Any] = {"user_id": user["id"], "python_code": {"$nin": [None, ""]}}
    if strategy_id:
        query["id"] = strategy_id
    results: List[Dict[str, Any]] = []
    _oos_pairs: List[Any] = []   # (strat, wf) → research-ledger auto-write
    async for strat in db.strategies.find(query):
        res = await asyncio.to_thread(engine.run, strat, start, end)
        if res.get("error"):
            results.append({"name": strat.get("name"), "error": res["error"]})
            continue
        wf = walk_forward(res)
        _oos_pairs.append((strat, wf))
        row = {"name": res.get("name"), "underlying": res.get("underlying"),
               "structure": res.get("structure"), "verdict": wf["verdict"],
               "overall": wf["overall"], "oos_year": wf.get("oos_year"),
               "oos": wf.get("oos"), "pct_green_months": wf.get("pct_green_months"),
               "signals": res.get("signals", 0),
               "signal_evaluation": res.get("signal_evaluation")}
        if strategy_id:
            row["trades"] = res.get("trades")
            row["by_year"] = wf.get("by_year")
        results.append(row)

    order = {"CANDIDATE_EDGE": 0, "FRAGILE": 1, "INSUFFICIENT_DATA": 2, "NO_EDGE_NEGATIVE": 3}
    results.sort(key=lambda r: order.get(r.get("verdict", ""), 9))
    _window = {"start": days[0], "end": days[-1], "n_days": len(days)}
    # Research bridge: auto-write each OOS verdict onto the strategy's edge
    # hypothesis so the Research Ledger fills from real evidence. Best-effort.
    ledger_written = 0
    try:
        from core.research_bridge import record_oos_backtest_batch
        ledger_written = await record_oos_backtest_batch(db, user["id"], _oos_pairs, _window)
    except Exception as _rb_exc:  # noqa: BLE001
        pass
    return {"data_available": True, "window": _window,
            "count": len(results), "results": results,
            "research_hypotheses_written": ledger_written}


@router.get("/iv-surface")
async def ops_iv_surface(
    underlying: str = "NIFTY",
    day: Optional[str] = None,
    lookback_days: int = 60,
    user=Depends(get_current_user),
):
    """P2-1: read-only IV surface and ATM richness from stored bhavcopy chains."""
    from core.bhavcopy_store import BhavcopyStore
    from core.iv_surface import richness_zscore

    store = BhavcopyStore()
    days = await asyncio.to_thread(store.trading_days)
    if not days:
        return {"available": False, "reason": "bhavcopy store empty"}
    d = str(day or days[-1])[:10]
    return await asyncio.to_thread(
        richness_zscore, store, underlying.upper(), d, lookback_days=max(10, min(lookback_days, 252)),
    )


class JudgeGradeReq(BaseModel):
    strategy_id: str
    mode: str = "eod"
    start: Optional[str] = None
    end: Optional[str] = None
    event_window_days: int = 1


@router.post("/judge/grade")
async def ops_judge_grade(req: JudgeGradeReq, user=Depends(get_current_user)):
    """P2-4: unified read-only judge facade. `mode=event` uses earnings windows."""
    from core.bhavcopy_store import BhavcopyStore
    from core.judge_facade import grade as judge_grade

    strat = await db.strategies.find_one({"user_id": user["id"], "id": req.strategy_id})
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found")
    params = {}
    if req.mode.lower() == "event":
        params["event_window_days"] = max(0, min(int(req.event_window_days), 5))
    result = await asyncio.to_thread(
        judge_grade, strat, mode=req.mode, start=req.start, end=req.end,
        store=BhavcopyStore(), params=params,
    )
    return _json_safe(result)


# ---- Edge Lab: cached OOS research surface (coverage + base-rate + OOS + sweep) ----

_edge_lab_building = False


async def _run_edge_lab_build(user_id: str) -> None:
    """Heavy background compute → cache in db.edge_lab_snapshots(_id='latest').
    Runs off the request path so the Edge Lab tab always loads instantly."""
    global _edge_lab_building
    try:
        from core.edge_lab import build_snapshot
        from core.bhavcopy_store import BhavcopyStore

        strategies = await db.strategies.find(
            {"user_id": user_id, "python_code": {"$nin": [None, ""]}}
        ).to_list(500)
        snap = await asyncio.to_thread(build_snapshot, strategies, BhavcopyStore())
        from core.edge_research_ledger import persist_trials
        await persist_trials(db, user_id, snap)
        snap["_id"] = "latest"
        snap["built_by"] = user_id
        await db.edge_lab_snapshots.replace_one({"_id": "latest"}, snap, upsert=True)
    except Exception as exc:  # noqa: BLE001
        await db.edge_lab_snapshots.update_one(
            {"_id": "latest"},
            {"$set": {"status": "error", "error": str(exc),
                      "generated_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
    finally:
        _edge_lab_building = False


@router.get("/edge-lab")
async def ops_edge_lab(user=Depends(get_current_user)):
    """Serve the cached Edge Lab snapshot (data coverage, the short-OTM-vol base-rate
    finding, per-strategy OOS verdicts, and the credit-spread exit-geometry sweep).
    Read-only, instant — the heavy compute is done by POST /ops/edge-lab/refresh and
    cached. Returns {status:'empty'} until the first build runs."""
    doc = await db.edge_lab_snapshots.find_one({"_id": "latest"}, {"_id": 0})
    if not doc:
        return {"status": "empty", "building": _edge_lab_building,
                "hint": "press Refresh to build the first Edge Lab snapshot"}
    doc["building"] = _edge_lab_building
    return doc


@router.post("/edge-lab/refresh")
async def ops_edge_lab_refresh(user=Depends(get_current_user)):
    """Kick off a background rebuild of the Edge Lab snapshot (10s–several min over
    2yr of chains). Returns immediately; poll GET /ops/edge-lab for the fresh
    `generated_at`. Idempotent while a build is already running."""
    global _edge_lab_building
    if _edge_lab_building:
        return {"status": "building", "already_running": True}
    _edge_lab_building = True
    await db.edge_lab_snapshots.update_one(
        {"_id": "latest"},
        {"$set": {"status": "building",
                  "refresh_started_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    asyncio.create_task(_run_edge_lab_build(user["id"]))
    return {"status": "building", "already_running": False}


@router.get("/edge-lab/trials")
async def ops_edge_lab_trials(strategy: str = "", limit: int = 100, user=Depends(get_current_user)):
    query = {"user_id": user["id"]}
    if strategy:
        query["strategy_name"] = strategy
    rows = await db.strategy_trials.find(query, {"_id": 0}).sort(
        "last_run_at", -1
    ).limit(min(max(limit, 1), 500)).to_list(min(max(limit, 1), 500))
    return {"count": len(rows), "rows": rows}


class EdgeLabProposeReq(BaseModel):
    underlying: str = "NIFTY"
    structure: str = "credit_spread"        # single_leg / credit_spread / debit_spread / iron_condor
    direction: str = "bullish"              # bullish → BUY (put spread); bearish → SELL (call spread)
    short_otm_pct: float = 0.03
    wing_width: int = 6
    spread_width: int = 6
    min_dte: int = 2
    max_dte: int = 8
    exit_mode: str = "expiry"               # expiry (hold to expiry) | "" (intraday tp/sl)
    credit_tp: float = 0.5
    credit_sl: float = 1.0


@router.post("/edge-lab/propose")
async def ops_edge_lab_propose(req: EdgeLabProposeReq, user=Depends(get_current_user)):
    """FE-04: OOS-test a hypothetical option structure BEFORE seeding it live. Builds a
    synthetic strategy from the requested structure/strikes/DTE, runs it through the
    walk-forward bhavcopy backtester, and returns the honest verdict. This is the
    OOS-first discipline as a one-click tool — nothing is seeded, read-only."""
    from core.eod_options_backtest import EODOptionsBacktest, walk_forward
    from core.bhavcopy_store import BhavcopyStore

    store = BhavcopyStore()
    days = await asyncio.to_thread(store.trading_days)
    if not days:
        return {"error": "bhavcopy store empty — no data to backtest"}
    action = "BUY" if req.direction == "bullish" else "SELL"
    code = ("def run(data):\n"
            f"    return [{{'date': c['date'], 'action': '{action}'}} for c in data]\n")
    strat = {"id": "propose", "name": "Proposed", "python_code": code,
             "visual_config": {"symbol": req.underlying, "options": {
                 "underlying": req.underlying, "structure": req.structure,
                 "short_otm_pct": req.short_otm_pct, "wing_width": req.wing_width,
                 "spread_width": req.spread_width, "exit_mode": req.exit_mode}}}
    params = {"short_otm_pct": req.short_otm_pct, "wing_width": req.wing_width,
              "width": req.spread_width, "min_dte": req.min_dte, "max_dte": req.max_dte,
              "max_hold_days": req.max_dte, "exit_mode": req.exit_mode,
              "credit_tp": req.credit_tp, "credit_sl": req.credit_sl}
    res = await asyncio.to_thread(
        EODOptionsBacktest(store).run, strat, None, None, 100_000.0, params)
    if res.get("error"):
        return {"error": res["error"], "config": req.dict()}
    wf = walk_forward(res)
    return {"verdict": wf["verdict"], "overall": wf["overall"], "by_year": wf.get("by_year"),
            "oos": wf.get("oos"), "oos_year": wf.get("oos_year"),
            "pct_green_months": wf.get("pct_green_months"),
            "window": {"start": days[0], "end": days[-1]}, "config": req.dict()}


# ---- IMD-09: intraday 1-minute OOS (QG-O5..QG-O10) --------------------------
_intraday_oos_running = False


async def _run_intraday_oos(start: str, end: str) -> None:
    global _intraday_oos_running
    try:
        from scripts.run_intraday_options_validation import main as run_validation
        await asyncio.to_thread(run_validation, ["--from", start, "--to", end])
    except Exception as exc:  # noqa: BLE001
        await db.intraday_options_oos_runs.insert_one({
            "status": "error", "error": str(exc),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    finally:
        _intraday_oos_running = False


@router.get("/regime-status")
async def ops_regime_status(user=Depends(get_current_user)):
    """RAE-6: the ensemble watch view — the current regime per index, which live
    strategy the router would ACTIVATE vs STAND DOWN right now, whether enforcement
    is on, and realized P&L bucketed by the regime each trade was entered in.
    Read-only. `enforced=false` means the router is observe-only (default)."""
    from core.regime_router import route, enabled as _rae_enabled
    uid = user["id"]

    # current regime per index (from the live regime engine's state). RAE-1 fine
    # label + confidence when available (written each minute by the server loop),
    # else the coarse regime.
    regimes = {}
    async for r in db.market_regime_state.find(
        {}, {"_id": 0, "index": 1, "regime": 1, "bias": 1,
             "regime_fine": 1, "regime_fine_confidence": 1}):
        regimes[r.get("index")] = {"regime": r.get("regime"), "bias": r.get("bias"),
                                   "regime_fine": r.get("regime_fine"),
                                   "confidence": r.get("regime_fine_confidence")}

    # per live strategy: would the router activate or stand it down right now?
    strat_routing = []
    async for s in db.strategies.find(
        {"user_id": uid, "status": {"$in": ["live", "paused"]}},
        {"_id": 0, "name": 1, "visual_config.options.underlying": 1,
         "visual_config.options.structure": 1, "visual_config.options.specialist_role": 1},
    ):
        opt = (s.get("visual_config") or {}).get("options") or {}
        underlying = opt.get("underlying") or "NIFTY"
        structure = opt.get("structure")
        _rg = regimes.get(underlying) or {}
        reg = _rg.get("regime_fine") or _rg.get("regime") or "UNKNOWN"
        conf = float(_rg.get("confidence") or 0.5)
        # explicit RAE tag wins; fall back to the structure heuristic for legacy rows
        specialist = opt.get("specialist_role") or (
            "range_seller" if structure in ("credit_spread", "debit_spread") else None)
        d = route(str(reg), conf, specialist=specialist)
        strat_routing.append({"strategy": s.get("name"), "underlying": underlying,
                              "regime": reg, "specialist": specialist,
                              "action": "STAND_DOWN" if d.stand_down else "ACTIVE",
                              "size_mult": round(d.size_mult, 2), "why": (d.reasons or [""])[0]})

    # specialist role per strategy id (explicit RAE tag, else structure heuristic)
    # — needed to judge whether each closed trade was taken ON its owned regime.
    spec_by_sid = {}
    async for s in db.strategies.find(
        {"user_id": uid},
        {"_id": 0, "id": 1, "visual_config.options.specialist_role": 1,
         "visual_config.options.structure": 1},
    ):
        opt = (s.get("visual_config") or {}).get("options") or {}
        spec_by_sid[s.get("id")] = opt.get("specialist_role") or (
            "range_seller" if opt.get("structure") in ("credit_spread", "debit_spread") else None)

    # realized P&L bucketed by the regime each closed trade was entered in, PLUS
    # the RAE ensemble scorecard: was P&L earned ON the specialist's owned regime,
    # or LEAKED from off-regime trades the router would have stood down? This is
    # the core forward-paper judge — the ensemble adds value only if on-regime P&L
    # is positive and off-regime P&L is what standing down would have avoided.
    by_regime = {}
    scorecard = {
        "on_regime": {"trades": 0, "pnl": 0.0, "wins": 0},
        "off_regime": {"trades": 0, "pnl": 0.0, "wins": 0},
        "unknown_regime": {"trades": 0, "pnl": 0.0, "wins": 0},
    }
    for k in scorecard:
        scorecard[k]["conf_sum"] = 0.0
        scorecard[k]["conf_n"] = 0
    daily = {}  # date -> {pnl,on,off,trades} : the forward-paper accumulation series
    async for p in db.strategy_positions.find(
        {"user_id": uid, "status": {"$in": ["CLOSED", "EXITED"]}},
        {"_id": 0, "regime_at_entry": 1, "regime_fine_at_entry": 1,
         "regime_fine_confidence_at_entry": 1, "closed_at": 1,
         "realized_pnl": 1, "pnl": 1, "strategy_id": 1},
    ):
        # prefer the FINE regime (RAE taxonomy) when it was stamped at entry;
        # fall back to the coarse label for pre-upgrade / untagged trades.
        _fine = str(p.get("regime_fine_at_entry") or "").upper()
        reg = _fine if _fine and _fine != "UNKNOWN" else str(p.get("regime_at_entry") or "UNKNOWN")
        pnl = float(p.get("realized_pnl") or p.get("pnl") or 0.0)
        b = by_regime.setdefault(reg, {"trades": 0, "pnl": 0.0})
        b["trades"] += 1
        b["pnl"] = round(b["pnl"] + pnl, 2)

        specialist = spec_by_sid.get(p.get("strategy_id"))
        if reg in ("UNKNOWN", "") or specialist is None:
            key = "unknown_regime"
        else:
            # router decision at full confidence: stand_down => the trade was
            # off the specialist's owned regime (leakage the router would stop).
            key = "off_regime" if route(reg, 1.0, specialist=specialist).stand_down else "on_regime"
        bucket = scorecard[key]
        bucket["trades"] += 1
        bucket["pnl"] = round(bucket["pnl"] + pnl, 2)
        if pnl > 0:
            bucket["wins"] += 1
        _conf = p.get("regime_fine_confidence_at_entry")
        if _conf is not None:
            bucket["conf_sum"] += float(_conf)
            bucket["conf_n"] += 1

        # daily on/off series keyed by close date (forward-paper accumulation)
        _d = str(p.get("closed_at") or "")[:10]
        if _d:
            dd = daily.setdefault(_d, {"pnl": 0.0, "on": 0.0, "off": 0.0, "trades": 0})
            dd["pnl"] = round(dd["pnl"] + pnl, 2)
            dd["trades"] += 1
            if key == "on_regime":
                dd["on"] = round(dd["on"] + pnl, 2)
            elif key == "off_regime":
                dd["off"] = round(dd["off"] + pnl, 2)

    for k in scorecard:
        t = scorecard[k]["trades"]
        scorecard[k]["win_rate"] = round(scorecard[k]["wins"] / t, 3) if t else 0.0
        scorecard[k]["avg_pnl"] = round(scorecard[k]["pnl"] / t, 2) if t else 0.0
        scorecard[k]["avg_confidence"] = round(
            scorecard[k]["conf_sum"] / scorecard[k]["conf_n"], 3) if scorecard[k]["conf_n"] else None
        del scorecard[k]["conf_sum"], scorecard[k]["conf_n"]

    daily_series = [{"date": d, **v} for d, v in sorted(daily.items())][-20:]

    return _json_safe({
        "kind": "rae_regime_status",
        "enforced": _rae_enabled(),
        "note": "Router is observe-only until RAE_ROUTER_ENABLED=true (founder gate)." if not _rae_enabled()
                else "Router ENFORCED: off-regime strategies stand down.",
        "index_regimes": regimes,
        "strategy_routing": strat_routing,
        "pnl_by_entry_regime": by_regime,
        "ensemble_scorecard": scorecard,
        "daily_series": daily_series,
        "scorecard_note": "on_regime = P&L where the specialist owned the entry regime; "
                          "off_regime = trades the router would STAND DOWN (leakage the ensemble prevents); "
                          "unknown_regime = pre-tag / untagged trades. Buckets by the FINE RAE regime "
                          "stamped at entry when present (INSIDE_QUIET/HIGH_VOL_CHOP/TREND_*), else the "
                          "coarse label for older trades.",
    })


@router.get("/rae-live-readiness")
async def ops_rae_live_readiness(user=Depends(get_current_user)):
    """RAE-7: the founder-gated live-pilot GATE. Read-only status only — this route
    NEVER enables anything. It reports whether the preconditions for a live pilot are
    met; the founder alone flips the env flags. Stays NOT_READY until (a) the ensemble
    has forward-papered enough (RAE_ROUTER_ENABLED=true) with positive regime-
    conditional evidence, and (b) the live flags are set. Live requires
    CORE_ENGINE_LIVE_ENABLED=true AND LIVE_SPREADS_ENABLED=true AND an armed account —
    all founder actions, none automatable here."""
    uid = user["id"]
    router_on = os.environ.get("RAE_ROUTER_ENABLED", "false").lower() == "true"
    live_on = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
    live_spreads_on = os.environ.get("LIVE_SPREADS_ENABLED", "false").lower() == "true"
    min_trades = int(os.environ.get("RAE_PILOT_MIN_TRADES", "30"))

    # forward-paper evidence: closed trades bucketed by entry regime, positive?
    by_regime, total = {}, 0
    async for p in db.strategy_positions.find(
        {"user_id": uid, "status": {"$in": ["CLOSED", "EXITED"]}},
        {"_id": 0, "regime_at_entry": 1, "realized_pnl": 1, "pnl": 1},
    ):
        reg = str(p.get("regime_at_entry") or "UNKNOWN")
        b = by_regime.setdefault(reg, {"trades": 0, "pnl": 0.0})
        b["trades"] += 1
        b["pnl"] = round(b["pnl"] + float(p.get("realized_pnl") or p.get("pnl") or 0.0), 2)
        total += 1

    blocking = []
    if not router_on:
        blocking.append("RAE_ROUTER_ENABLED=false — forward-paper the ensemble first (founder flips in PAPER).")
    if total < min_trades:
        blocking.append(f"forward-paper sample too thin: {total} closed trades (<{min_trades}).")
    positive_regimes = [r for r, v in by_regime.items() if v["pnl"] > 0]
    if router_on and total >= min_trades and not positive_regimes:
        blocking.append("no regime shows positive forward-paper P&L yet.")
    if not live_on:
        blocking.append("CORE_ENGINE_LIVE_ENABLED=false (founder gate — not automatable).")
    if not live_spreads_on:
        blocking.append("LIVE_SPREADS_ENABLED=false (founder gate).")

    return _json_safe({
        "kind": "rae_live_readiness",
        "status": "READY" if not blocking else "NOT_READY",
        "flags": {"RAE_ROUTER_ENABLED": router_on, "CORE_ENGINE_LIVE_ENABLED": live_on,
                  "LIVE_SPREADS_ENABLED": live_spreads_on},
        "forward_paper": {"closed_trades": total, "pnl_by_entry_regime": by_regime,
                          "positive_regimes": positive_regimes},
        "blocking": blocking,
        "note": "This endpoint only REPORTS. Enabling live is a founder decision (flip the env flags + arm).",
    })


@router.get("/intraday-oos")
async def ops_intraday_oos(user=Depends(get_current_user)):
    """IMD-09: latest intraday 1-minute OOS verdicts for QG-O5..QG-O10 + minute-data
    coverage. Distinct from GET /edge-lab (EOD theta OOS) — this judges intraday
    option BUYERS on 1-minute history. Read-only; INSUFFICIENT_DATA until IMD-04
    forward capture / index-minute import matures the sample."""
    from core.options_minute_store import OptionsMinuteStore
    coverage = await asyncio.to_thread(OptionsMinuteStore().coverage)
    latest = await db.intraday_options_oos_runs.find_one(
        {"verdict_counts": {"$exists": True}}, {"_id": 0}, sort=[("generated_at", -1)])
    return _json_safe({
        "kind": "intraday_1m_oos",
        "coverage": coverage,
        "latest_run": latest,
        "running": _intraday_oos_running,
        "note": "Intraday 1m OOS judges option buyers; separate from the EOD theta OOS in Edge Lab.",
    })


@router.post("/intraday-oos/refresh")
async def ops_intraday_oos_refresh(start: str = "2025-01-01", end: str = "2025-12-31",
                                   user=Depends(get_current_user)):
    """Kick a background intraday OOS validation run. Returns immediately; poll
    GET /ops/intraday-oos for the fresh verdicts."""
    global _intraday_oos_running
    if _intraday_oos_running:
        return {"status": "running", "already_running": True}
    _intraday_oos_running = True
    asyncio.create_task(_run_intraday_oos(start, end))
    return {"status": "running", "already_running": False}


# ---- RAE re-judge: regime-conditional OOS for the whole option book ---------
_regime_oos_running = False


async def _run_regime_oos(start, end, status) -> None:
    global _regime_oos_running
    try:
        from scripts.run_regime_oos_validation import compute
        pdb = _pymongo_db()
        result = await asyncio.to_thread(
            compute, pdb, start=start, end=end, status=status)
        result["status"] = "error" if result.get("error") else "ok"
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        await db.regime_oos_runs.insert_one(_json_safe(result))
    except Exception as exc:  # noqa: BLE001
        await db.regime_oos_runs.insert_one({
            "status": "error", "error": str(exc),
            "generated_at": datetime.now(timezone.utc).isoformat()})
    finally:
        _regime_oos_running = False


@router.get("/regime-oos")
async def ops_regime_oos(user=Depends(get_current_user)):
    """RAE re-judge: every option strategy (new/old/live/archived) graded through the
    REFORMED regime-conditional judge (core/regime_conditional_oos) — on the regime it
    owns (credit/condor->RANGE sellers, debit/long->TREND_UP buyers), walk-forward,
    with off-regime give-back reported separately. Distinct from the blended EOD theta
    OOS (Edge Lab) and the intraday 1m OOS. Read-only; returns the latest stored run."""
    latest = await db.regime_oos_runs.find_one(
        {"rows": {"$exists": True}}, {"_id": 0}, sort=[("generated_at", -1)])
    return _json_safe({
        "kind": "regime_conditional_oos",
        "latest_run": latest,
        "running": _regime_oos_running,
        "note": "Graded on each strategy's OWNED regime, not blended all-days. "
                "SENSEX/BANKNIFTY need 1-min data before they can be regime-judged.",
    })


@router.post("/regime-oos/refresh")
async def ops_regime_oos_refresh(status: str = "all", start: Optional[str] = None,
                                 end: Optional[str] = None, user=Depends(get_current_user)):
    """Kick a background regime-conditional re-judge over ALL strategy statuses.
    Returns immediately; poll GET /ops/regime-oos for the fresh scorecard."""
    global _regime_oos_running
    if _regime_oos_running:
        return {"status": "running", "already_running": True}
    _regime_oos_running = True
    asyncio.create_task(_run_regime_oos(start, end, status))
    return {"status": "running", "already_running": False}


# ---- IA-7: research RAG — index our validated history for Hermes recall ------
@router.get("/research-rag")
async def ops_research_rag(user=Depends(get_current_user)):
    """IA-7: status of the research RAG index (wiki + Hermes lessons + OOS verdicts
    embedded into db.hermes_memory so `recall_memory` retrieves our OWN validated
    history). Read-only. POST /research-rag/reindex to (re)build."""
    uid = user["id"]
    total = await db.hermes_memory.count_documents({"user_id": uid})
    rag = await db.hermes_memory.count_documents({"user_id": uid, "_rag": True})
    by_type: Dict[str, int] = {}
    async for m in db.hermes_memory.find({"user_id": uid, "_rag": True}, {"type": 1}):
        t = m.get("type") or "?"
        by_type[t] = by_type.get(t, 0) + 1
    return _json_safe({"kind": "research_rag", "hermes_memory_total": total,
                       "rag_indexed": rag, "by_type": by_type,
                       "note": "Semantic recall over our own wiki/lessons/OOS verdicts; "
                               "the AlphaAgent anti-drift grounding for the IA-6 miner."})


@router.post("/research-rag/reindex")
async def ops_research_rag_reindex(user=Depends(get_current_user)):
    """Rebuild the research RAG index (idempotent; embeds only new/changed docs).
    Safe to run nightly via cron."""
    from core.research_rag import reindex_all
    result = await reindex_all(db, user["id"])
    return _json_safe({"kind": "research_rag_reindex", **result})


# ---- IA-8: research signals telemetry (honest — includes tested-dead status) ----
@router.get("/research-signals")
async def ops_research_signals(user=Depends(get_current_user)):
    """IA-8: compact research-data surface — latest FII/DII flow, RAG index size, and
    the HONEST validated status of each IA data signal (so the UI never implies a dead
    signal is live). Read-only."""
    uid = user["id"]
    # latest FII/DII from the store
    fii = None
    try:
        from core import india_flows as _if
        days = _if.available_days()
        if days:
            fii = _if.get_flows(days[-1])
    except Exception:  # noqa: BLE001
        fii = None
    rag = await db.hermes_memory.count_documents({"user_id": uid, "_rag": True})
    return _json_safe({
        "kind": "research_signals",
        "fii_dii_latest": fii,
        "rag_indexed_docs": rag,
        "signal_status": [
            {"signal": "OI / Gamma Exposure (GEX)", "verdict": "DEAD",
             "note": "near-random on NIFTY (retail sells options → OI = seller positioning)"},
            {"signal": "IV skew", "verdict": "MARGINAL",
             "note": "weak contrarian feature, not a standalone edge"},
            {"signal": "FII/DII flow", "verdict": "DEAD",
             "note": "9-yr test: corr≈0, per-year sign-flips, contrarian strat −44%"},
        ],
        "note": "IA data-signal sweep: no readily-available signal beat base rate on NIFTY. "
                "Edge lives in the RAE regime ensemble, not a single indicator.",
    })


@router.post("/backfill-candles")
async def ops_backfill_candles(
    days: int = 30,
    underlyings: Optional[str] = None,
    user=Depends(get_current_user),
):
    """Backfill real 5-min underlying OHLC into db.candles (TASK-052) so the
    options backtester evaluates breakout/range/VWAP strategies on real high/low,
    not flat chain-spot dots. Uses the caller's connected Upstox gateway. Pass
    `underlyings` as a comma list (default NIFTY,BANKNIFTY,SENSEX)."""
    from server import get_user_upstox_gateway
    from core.candle_store import backfill_underlying_candles, DEFAULT_UNDERLYINGS

    gw = await get_user_upstox_gateway(user["id"])
    if not gw or not getattr(gw, "connected", False):
        raise HTTPException(status_code=400, detail="Upstox gateway not connected — reconnect token first")
    uls = [u.strip().upper() for u in underlyings.split(",")] if underlyings else list(DEFAULT_UNDERLYINGS)
    days = max(1, min(int(days or 30), 30))
    summary = await backfill_underlying_candles(db, gw, underlyings=uls, days=days)
    return {"days": days, "underlyings": uls, "result": summary}


@router.get("/feature-flags")
async def ops_feature_flags(user=Depends(get_current_user)):
    """Read-only state of the Phase 2 options-alpha features (env-driven).
    Lets the UI show what's active without SSHing into the box."""
    from core.position_lifecycle import EXIT_THETA_AWARE_ENABLED
    from options_delta import OPTION_DELTA_SELECTION_ENABLED
    from iv_regime import IV_RANK_GATE_ENABLED, IV_RANK_GATE_SHADOW, IV_RANK_BUY_MAX
    from order_flow import ORDERFLOW_GATE_ENABLED, ORDERFLOW_GATE_SHADOW, ORDERFLOW_MIN_IMBALANCE
    from core.spread_builder import CREDIT_SPREADS_ENABLED

    def gate_state(enabled: bool, shadow: bool) -> str:
        return "enabled" if enabled else ("shadow" if shadow else "off")

    return {
        "features": [
            {"key": "theta_exit", "label": "Theta-aware exits",
             "state": "on" if EXIT_THETA_AWARE_ENABLED else "off",
             "detail": "Cuts stalled long-option buys before theta bleeds them."},
            {"key": "delta_selection", "label": "Delta strike selection",
             "state": "on" if OPTION_DELTA_SELECTION_ENABLED else "off",
             "detail": "Picks the strike nearest a per-style target delta."},
            {"key": "iv_rank_gate", "label": "IV-rank gate",
             "state": gate_state(IV_RANK_GATE_ENABLED, IV_RANK_GATE_SHADOW),
             "detail": f"Blocks buying when IV rank > {IV_RANK_BUY_MAX:.0f}."},
            {"key": "orderflow_gate", "label": "Order-flow gate",
             "state": gate_state(ORDERFLOW_GATE_ENABLED, ORDERFLOW_GATE_SHADOW),
             "detail": f"Needs net buying pressure ≥ {ORDERFLOW_MIN_IMBALANCE:.2f}."},
            {"key": "credit_spreads", "label": "Credit spreads",
             "state": "on" if CREDIT_SPREADS_ENABLED else "off",
             "detail": "Defined-risk spreads (per-strategy opt-in)."},
        ],
    }


@router.get("/runtime")
async def ops_runtime_route(user=Depends(get_current_user)):
    from server import (
        APP_VERSION, START_TIME, get_git_info, get_file_version, app, db,
        get_user_settings, get_user_upstox_status, get_user_upstox_gateway
    )
    import os
    import time
    
    commit, branch, dirty = get_git_info()
    file_version = get_file_version()
    up_time = (datetime.now(timezone.utc) - START_TIME).total_seconds()
    
    settings = await get_user_settings(user["id"])
    paper_mode = bool(settings.get("paper_mode", True))
    
    # Check broker credentials config status
    upstox_status = await get_user_upstox_status(user["id"])
    broker_configured = bool(upstox_status.get("keys_saved"))
    
    # Check if live-auto is armed in DB
    arm_state = await db.live_arm_state.find_one({"user_id": user["id"]})
    live_auto_enabled = bool(arm_state and arm_state.get("armed"))
    
    # Check loop tasks status
    def get_task_status(task_name):
        task = getattr(app.state, task_name, None)
        if task is None:
            return "missing"
        elif task.done():
            if task.cancelled():
                return "cancelled"
            try:
                if task.exception():
                    return f"failed: {task.exception()}"
            except Exception:
                pass
            return "done"
        return "running"

    runner_status = get_task_status("runner_task")
    sig_status = get_task_status("signal_manager_task")
    
    # Check feed status
    feed_status = "disconnected"
    upstox_gw = await get_user_upstox_gateway(user["id"])
    if upstox_gw:
        gw_status = upstox_gw.status()
        if gw_status.get("feed_running"):
            feed_status = "connected"
            
    # Frontend version
    frontend_version = "10.0.0"
    try:
        from server import ROOT_DIR
        frontend_pkg = ROOT_DIR.parent / "frontend" / "package.json"
        if frontend_pkg.exists():
            import json
            with open(frontend_pkg, "r") as f:
                pkg_data = json.load(f)
                frontend_version = pkg_data.get("version", "10.0.0")
    except Exception:
        pass

    return {
        "backend_version": APP_VERSION,
        "file_version": file_version,
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": dirty,
        "start_time": START_TIME.isoformat(),
        "up_time_seconds": round(up_time, 2),
        "runtime_mode": os.environ.get("NODE_ENV", "production"),
        "paper_live_status": "PAPER" if paper_mode else "LIVE",
        "broker_configured_status": broker_configured,
        "live_auto_enabled": live_auto_enabled,
        "active_runner_task_status": runner_status,
        "active_signal_manager_task_status": sig_status,
        "feed_status": feed_status,
        "frontend_version": frontend_version,
        "frontend_build_metadata": None
    }



@router.post("/ticker/restart")
async def ops_restart_ticker(req: OpsActionReq = None, user=Depends(get_current_user), request: Request = None):
    from server import _start_user_upstox_ticker
    return await _start_user_upstox_ticker(user["id"])


@router.post("/orders/sync")
async def ops_sync_orders(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import (
        _ORDER_SYNC_CACHE, 
        _sync_upstox_order_statuses,
        _sync_strategy_positions_with_broker
    )
    
    _ORDER_SYNC_CACHE.pop(user["id"], None)
    upstox_sync = await _sync_upstox_order_statuses(user["id"], force=True)
    position_sync = await _sync_strategy_positions_with_broker(user["id"])
    return {"ok": True, "upstox_sync": upstox_sync, "position_sync": position_sync}


@router.get("/reconciliation/breaks")
async def ops_reconciliation_breaks(user=Depends(get_current_user)):
    return await _reconciliation_break_snapshot(user["id"])


@router.post("/reconciliation/resolve")
async def ops_resolve_reconciliation(req: OpsActionReq = None, user=Depends(get_current_user)):
    confirm = bool(req and req.confirm)
    phrase = str((req.note if req else "") or "").strip()
    snapshot = await _reconciliation_break_snapshot(user["id"])

    if not confirm or phrase != RECONCILIATION_RESOLVE_PHRASE:
        return {
            "ok": False,
            "dry_run": True,
            "required_note": RECONCILIATION_RESOLVE_PHRASE,
            "detail": "No data changed. Confirm only after checking the broker account manually.",
            "snapshot": snapshot,
        }

    blockers = snapshot["blockers"]
    if blockers["unknown_live_orders"] or blockers["reservations_needing_review"]:
        raise HTTPException(
            status_code=409,
            detail="Cannot clear reconciliation while live orders or exposure reservations still need review.",
        )

    now = datetime.now(timezone.utc).isoformat()
    res = await db["risk_state"].update_one(
        {"_id": f"position_reconciliation:{user['id']}"},
        {"$set": {
            "user_id": user["id"],
            "scope": "position_reconciliation",
            "mismatch_detected": False,
            "mismatches": [],
            "resolved_at": now,
            "resolved_by": user["id"],
            "resolution_note": phrase,
            "resolution_source": "ops_manual_broker_check",
        }},
        upsert=True,
    )
    await _append_ops_audit_event(
        user["id"],
        "RECONCILIATION_MANUALLY_RESOLVED",
        {
            "resolved_at": now,
            "matched": getattr(res, "matched_count", None),
            "modified": getattr(res, "modified_count", None),
            "previous_blockers": blockers,
        },
    )
    return {
        "ok": True,
        "resolved": True,
        "resolved_at": now,
        "detail": "Position reconciliation block cleared after owner-confirmed manual broker review.",
    }


@router.post("/auto-recover")
async def ops_auto_recover(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import (
        _ORDER_SYNC_CACHE,
        _sync_strategy_positions_with_broker,
        _sync_upstox_order_statuses,
        _start_user_upstox_ticker,
        ops_diagnostics
    )
    
    actions: List[Dict[str, Any]] = []
    _ORDER_SYNC_CACHE.pop(user["id"], None)
    actions.append({"name": "upstox_order_sync", "result": await _sync_upstox_order_statuses(user["id"], force=True)})
    actions.append({"name": "upstox_position_sync", "result": await _sync_strategy_positions_with_broker(user["id"])})
    actions.append({"name": "upstox_market_ticker", "result": await _start_user_upstox_ticker(user["id"])})

    diagnostics = await ops_diagnostics(user=user)
    return {"ok": True, "actions": actions, "recovery_plan": diagnostics.get("recovery_plan")}


@router.post("/emergency-stop")
async def ops_emergency_stop(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import option_ledger

    now = datetime.now(timezone.utc).isoformat()
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "id": 1}).to_list(500)
    for row in strategies:
        option_ledger.set_kill_switch(True, strategy_id=row["id"])
    await db.users.update_one({"id": user["id"]}, {"$set": {"paper_mode": True, "ops_last_emergency_stop_at": now}})
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "live"},
        {"$set": {"status": "paused", "last_error": f"Emergency stop at {now}: switched to PAPER and paused automation."}},
    )
    return {"ok": True, "paper_mode": True, "paused_strategies": res.modified_count, "disabled_strategies": len(strategies), "at": now}


@router.post("/strategies/pause-all")
async def ops_pause_all(req: OpsActionReq = None, user=Depends(get_current_user)):
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "live"},
        {"$set": {"status": "paused", "last_error": None}},
    )
    return {"ok": True, "paused_strategies": res.modified_count}


@router.post("/strategies/enable-all")
async def ops_enable_all_strategies(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import option_ledger, _sync_option_ledger_strategy

    # Only re-enable strategies that were previously PAUSED — never touch draft or custom-stopped ones
    rows = await db.strategies.find({"user_id": user["id"], "status": "paused", "manual_paused": {"$ne": True}}, {"_id": 0}).to_list(500)
    for row in rows:
        _sync_option_ledger_strategy(row)
        option_ledger.set_kill_switch(False, strategy_id=row["id"])
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "paused", "manual_paused": {"$ne": True}},
        {"$set": {"status": "live"}, "$unset": {"last_error": "", "last_signal_validation": ""}},
    )
    return {"ok": True, "enabled_strategies": res.modified_count, "ledger_enabled": len(rows)}


@router.post("/strategies/clear-errors")
async def ops_clear_strategy_errors(req: OpsActionReq = None, user=Depends(get_current_user)):
    res = await db.strategies.update_many(
        {"user_id": user["id"]},
        {"$unset": {"last_error": "", "last_signal_validation": ""}},
    )
    return {"ok": True, "updated_strategies": res.modified_count}


@router.get("/pending-users")
async def get_pending_users(user=Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required.")
    pending = await db.users.find({"approved": False, "role": {"$ne": "owner"}}, {"_id": 0, "password_hash": 0}).to_list(100)
    return pending


@router.post("/users/{user_id}/approve")
async def approve_user(user_id: str, user=Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required.")
    res = await db.users.update_one({"id": user_id}, {"$set": {"approved": True, "status": "approved"}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Seed default strategies for the newly approved trader
    from server import seed_default_strategies_for_user
    await seed_default_strategies_for_user(user_id)
    
    return {"ok": True, "message": "User account approved and default strategies initialized."}


@router.post("/users/{user_id}/reject")
async def reject_user(user_id: str, user=Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required.")
    res = await db.users.delete_one({"id": user_id, "role": {"$ne": "owner"}})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found or cannot be rejected")
    return {"ok": True, "message": "User registration rejected and deleted."}


@router.post("/paper-orders/clear-stale")
async def ops_clear_stale_paper_orders(req: OpsActionReq = None, user=Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    from server import ORDER_ACTIVE_STATUSES, LEGACY_OPEN_STATUSES, ORDER_CANCELLED
    active_statuses = list(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)
    
    res = await db.orders.update_many(
        {"user_id": user["id"], "mode": "paper", "status": {"$in": active_statuses}},
        {"$set": {
            "status": ORDER_CANCELLED,
            "legacy_status": "CANCELLED",
            "broker_status": "CANCELLED",
            "status_message": "Paper order cleared manually via Ops console.",
            "updated_at": now
        }}
    )
    
    await db.strategy_positions.update_many(
        {"user_id": user["id"], "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}, "mode": "paper"},
        {"$set": {
            "status": "CANCELLED",
            "broker_status_message": "Paper position cleared manually via Ops console.",
            "updated_at": now
        },
         "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}}
    )

    # Drop the UI position mirror too. The mirror (db.positions) is only
    # auto-deleted on a full CLOSE — cancelling the strategy_positions above
    # without this leaves orphaned mirror rows that show up as phantom "ORPHAN"
    # holdings on the Positions page. All active paper positions were just
    # cancelled, so no valid paper mirror row remains.
    mirror_res = await db.positions.delete_many({"user_id": user["id"], "mode": "paper"})

    await db.strategy_position_locks.delete_many({"user_id": user["id"]})

    return {"ok": True, "cleared_orders": res.modified_count, "cleared_position_mirrors": mirror_res.deleted_count}


@router.post("/accounts/reset-all-trading-state")
async def ops_reset_all_accounts_trading_state(req: OpsActionReq = None, user=Depends(get_current_user)):
    """Owner-only full app trading-state reset.

    This preserves login accounts and saved broker credentials, but resets every
    account to PAPER, disarms live trading, pauses strategies, and clears local
    trading state/projections. It intentionally requires an exact phrase.
    """
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required.")
    confirm = bool(req and req.confirm)
    phrase = str((req.note if req else "") or "").strip()
    required_phrase = "RESET_ALL_ACCOUNTS_TO_PAPER"

    users = await db.users.find({}, {"_id": 0, "id": 1, "email": 1}).to_list(5000)
    user_ids = [u["id"] for u in users if u.get("id")]
    strategy_rows = await db.strategies.find({"user_id": {"$in": user_ids}}, {"_id": 0, "id": 1}).to_list(20000)
    strategy_ids = [s["id"] for s in strategy_rows if s.get("id")]

    trading_collections = {
        "orders": {"user_id": {"$in": user_ids}},
        "positions": {"user_id": {"$in": user_ids}},
        "strategy_positions": {"user_id": {"$in": user_ids}},
        "strategy_position_locks": {"user_id": {"$in": user_ids}},
        "signals": {"user_id": {"$in": user_ids}},
        "skipped_signals": {"user_id": {"$in": user_ids}},
        "paper_trading_history": {"user_id": {"$in": user_ids}},
        "trades": {"user_id": {"$in": user_ids}},
        "trade_fills": {"user_id": {"$in": user_ids}},
        "paper_wallets": {"user_id": {"$in": user_ids}},
        "risk_events": {"user_id": {"$in": user_ids}},
        "risk_reservations": {"user_id": {"$in": user_ids}},
        "risk_reservation_locks": {"user_id": {"$in": user_ids}},
        "order_events": {"user_id": {"$in": user_ids}},
        "outbox_events": {"user_id": {"$in": user_ids}},
        "broker_sync_state": {"user_id": {"$in": user_ids}},
        "live_arm_state": {"user_id": {"$in": user_ids}},
    }
    option_collections = {
        "option_open_positions": {"strategy_id": {"$in": strategy_ids}},
        "option_daily_pnl": {"strategy_id": {"$in": strategy_ids}},
        "option_trade_journal": {"strategy_id": {"$in": strategy_ids}},
    }
    risk_state_query = {
        "$or": [
            {"user_id": {"$in": user_ids}},
            {"_id": {"$in": [f"position_reconciliation:{uid}" for uid in user_ids]}},
        ]
    }

    preview = {}
    for coll, query in {**trading_collections, **option_collections, "risk_state": risk_state_query}.items():
        preview[coll] = await db[coll].count_documents(query)

    if not confirm or phrase != required_phrase:
        return {
            "ok": False,
            "dry_run": True,
            "required_note": required_phrase,
            "accounts": len(user_ids),
            "strategies": len(strategy_ids),
            "would_delete": preview,
            "detail": "No data changed. Send confirm=true and the required note to execute.",
        }

    now = datetime.now(timezone.utc).isoformat()
    purged = {}
    for coll, query in trading_collections.items():
        res = await db[coll].delete_many(query)
        purged[coll] = res.deleted_count
    for coll, query in option_collections.items():
        res = await db[coll].delete_many(query)
        purged[coll] = res.deleted_count
    risk_res = await db["risk_state"].delete_many(risk_state_query)
    purged["risk_state"] = risk_res.deleted_count

    users_res = await db.users.update_many(
        {"id": {"$in": user_ids}},
        {"$set": {
            "paper_mode": True,
            "data_broker": "upstox",
            "execution_broker": "upstox",
            "fallback_broker": "none",
            "reset_all_trading_state_at": now,
        }},
    )
    strategies_res = await db.strategies.update_many(
        {"user_id": {"$in": user_ids}},
        {"$set": {
            "mode": "paper",
            "status": "paused",
            "broker": "upstox",
            "halted": False,
            "is_halted": False,
            "last_error": "",
            "last_filter_reason": "Reset to PAPER by owner trading-state reset.",
            "reset_all_trading_state_at": now,
        },
         "$unset": {
             "halt_reason": "",
             "last_signal_validation": "",
             "last_fired_signal_date": "",
             "last_traded_symbol": "",
         }},
    )
    await db.option_strategy_states.update_many(
        {"strategy_id": {"$in": strategy_ids}},
        {"$set": {"state": "IDLE", "cooldown_until": None, "updated_at": now}},
    )

    try:
        from server import seed_default_strategies_for_user
        for uid in user_ids:
            await seed_default_strategies_for_user(uid)
    except Exception as exc:
        return {
            "ok": False,
            "partial": True,
            "detail": f"Trading state reset completed, but strategy reseeding failed: {exc}",
            "purged": purged,
            "updated_users": users_res.modified_count,
            "updated_strategies": strategies_res.modified_count,
        }

    return {
        "ok": True,
        "detail": "All accounts reset to PAPER trading state. Login accounts and saved Upstox credentials were preserved.",
        "accounts": len(user_ids),
        "strategies": len(strategy_ids),
        "updated_users": users_res.modified_count,
        "updated_strategies": strategies_res.modified_count,
        "purged": purged,
        "reset_at": now,
    }


@router.post("/positions/cleanup-orphans")
async def ops_cleanup_orphan_positions(req: OpsActionReq = None, user=Depends(get_current_user)):
    user_id = user["id"]
    
    # 1. Fetch all positions in db.positions
    positions = await db.positions.find({"user_id": user_id}).to_list(1000)
    
    # 2. Fetch active strategy positions
    active_sp = await db.strategy_positions.find({
        "user_id": user_id,
        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]}
    }).to_list(1000)
    
    active_sp_symbols = {str(sp.get("symbol")).upper() for sp in active_sp if sp.get("symbol")}
    active_sp_strategy_ids = {str(sp.get("strategy_id")) for sp in active_sp if sp.get("strategy_id")}
    
    orphans = []
    for pos in positions:
        qty = int(pos.get("qty") or 0)
        if qty == 0:
            continue
        symbol = str(pos.get("symbol")).upper()
        strategy_id = pos.get("strategy_id")
        
        is_orphan = True
        if strategy_id and str(strategy_id) in active_sp_strategy_ids:
            is_orphan = False
        elif symbol in active_sp_symbols:
            is_orphan = False
            
        if is_orphan:
            orphans.append(pos)
            
    # Default is always dry-run. Cleanup requires owner role AND confirm=True
    confirm = bool(req and req.confirm)
    is_owner = str(user.get("role")).lower() == "owner"
    
    cleaned = 0
    report = [{"symbol": o["symbol"], "qty": o["qty"], "strategy_id": o.get("strategy_id")} for o in orphans]
    
    if confirm:
        if not is_owner:
            raise HTTPException(status_code=403, detail="Access denied: Owner role required for deletion.")
        for orphan in orphans:
            delete_filter = {"user_id": user_id, "symbol": orphan["symbol"]}
            if orphan.get("strategy_id"):
                delete_filter["strategy_id"] = orphan["strategy_id"]
            await db.positions.delete_one(delete_filter)
            cleaned += 1
            
    return {
        "ok": True,
        "orphans_found": len(orphans),
        "orphans": report,
        "cleaned_count": cleaned,
        "mode": "executed" if (confirm and is_owner) else "dry-run",
        "message": "Orphan positions cleaned up successfully." if (confirm and is_owner) else "Dry-run completed. No records deleted."
    }


@router.get("/strategy-code-audit")
async def ops_strategy_code_audit(user=Depends(get_current_user)):
    from core.strategy_brain_schema import calculate_code_hash
    
    # Fetch all strategies for this user
    db_strategies = await db.strategies.find({"user_id": user["id"]}).to_list(1000)
    
    # Group by code hash to identify duplicate code groups
    hash_groups = {}
    for s in db_strategies:
        code = s.get("python_code") or ""
        ch = calculate_code_hash(code)
        hash_groups.setdefault(ch, []).append(s)
        
    duplicate_groups = []
    for ch, group_strats in hash_groups.items():
        if len(group_strats) > 1:
            duplicate_groups.append({
                "code_hash": ch,
                "strategy_ids": [s.get("id") or str(s.get("_id")) for s in group_strats],
                "names": [s.get("name") for s in group_strats],
                "count": len(group_strats)
            })
            
    # Process each strategy
    strategies_report = []
    live_count = 0
    for s in db_strategies:
        code = s.get("python_code") or ""
        ch = calculate_code_hash(code)
        is_dup = len(hash_groups[ch]) > 1
        
        status = s.get("status")
        if status == "live":
            live_count += 1
            
        underlying = s.get("underlying") or s.get("visual_config", {}).get("symbol") or s.get("visual_config", {}).get("options", {}).get("underlying") or ""
        underlying = str(underlying).upper()
        
        instrument_group = s.get("instrument_group") or s.get("visual_config", {}).get("exchange")
        if instrument_group:
            instrument_group = str(instrument_group).upper()
        else:
            instrument_group = "BFO" if underlying == "SENSEX" else "NFO"
            
        risk_style = s.get("risk_style") or s.get("visual_config", {}).get("risk", {}).get("risk_style") or s.get("risk", {}).get("risk_style")
        exit_mode = s.get("exit_mode") or s.get("risk", {}).get("exit_mode") or s.get("visual_config", {}).get("risk", {}).get("exit_mode") or "signal_or_tp_sl_trailing"
        
        strategies_report.append({
            "strategy_id": s.get("id") or str(s.get("_id")),
            "name": s.get("name"),
            "status": status,
            "mode": s.get("mode") or "paper",
            "broker": s.get("broker") or "upstox",
            "underlying": underlying,
            "instrument_group": instrument_group,
            "code_hash": ch,
            "duplicate_code_group": ch if is_dup else None,
            "risk_style": risk_style,
            "exit_mode": exit_mode,
            "default_strategy_version": s.get("default_strategy_version"),
            "strategy_logic_version": s.get("strategy_logic_version"),
            "signal_schema_status": s.get("signal_schema_status")
        })
        
    return {
        "ok": True,
        "total_strategies": len(db_strategies),
        "live_strategies": live_count,
        "duplicate_code_groups": duplicate_groups,
        "strategies": strategies_report
    }


@router.get("/default-strategy-catalog-audit")
async def ops_default_strategy_catalog_audit(user=Depends(get_current_user)):
    from server import DEFAULT_OPTION_STRATEGIES
    from core.strategy_brain_schema import calculate_code_hash
    
    # Group default strategies by code hash
    hash_groups = {}
    for t in DEFAULT_OPTION_STRATEGIES:
        code = t.get("python_code") or ""
        ch = calculate_code_hash(code)
        hash_groups.setdefault(ch, []).append(t.get("name"))
        
    strategies_report = []
    any_shares = False
    for t in DEFAULT_OPTION_STRATEGIES:
        code = t.get("python_code") or ""
        ch = calculate_code_hash(code)
        shares = len(hash_groups[ch]) > 1
        if shares:
            any_shares = True
            
        underlying = t.get("underlying") or t.get("visual_config", {}).get("options", {}).get("underlying") or ""
        underlying = str(underlying).upper()
        
        instrument_group = t.get("instrument_group") or t.get("visual_config", {}).get("exchange")
        if instrument_group:
            instrument_group = str(instrument_group).upper()
        else:
            instrument_group = "BFO" if underlying == "SENSEX" else "NFO"
            
        risk_style = t.get("risk_style") or t.get("risk", {}).get("risk_style") or t.get("visual_config", {}).get("risk", {}).get("risk_style")
        exit_mode = t.get("exit_mode") or t.get("risk", {}).get("exit_mode") or t.get("visual_config", {}).get("risk", {}).get("exit_mode") or "signal_or_tp_sl_trailing"
        
        strategies_report.append({
            "name": t.get("name"),
            "underlying": underlying,
            "instrument_group": instrument_group,
            "required_capital": t.get("required_capital"),
            "risk_style": risk_style,
            "exit_mode": exit_mode,
            "market_suitability": t.get("market_suitability"),
            "code_hash": ch,
            "shares_code": shares
        })
        
    mcx_excluded = all(
        str(t.get("instrument_group") or "").upper() != "MCX"
        and "CRUDE" not in str(t.get("name") or "").upper()
        and "NATURAL GAS" not in str(t.get("name") or "").upper()
        for t in DEFAULT_OPTION_STRATEGIES
    )
    
    return {
        "ok": True,
        "total_default_strategies": len(DEFAULT_OPTION_STRATEGIES),
        "names": [t.get("name") for t in DEFAULT_OPTION_STRATEGIES],
        "mcx_crude_naturalgas_excluded": mcx_excluded,
        "shares_code_with_another": any_shares,
        "strategies": strategies_report
    }


@router.post("/v13/strategy-brain/dry-run")
async def ops_v13_strategy_brain_dry_run(user=Depends(get_current_user)):
    from server import DEFAULT_OPTION_STRATEGIES
    from core.strategy_brain_schema import calculate_code_hash
    
    db_strategies = await db.strategies.find({"user_id": user["id"]}).to_list(1000)
    db_names = {s["name"] for s in db_strategies if s.get("name")}
    
    # 1. strategies_to_insert: names of default strategies not in DB
    strategies_to_insert = [
        t["name"] for t in DEFAULT_OPTION_STRATEGIES if t["name"] not in db_names
    ]
    
    # 2. strategies_to_update_by_name: names of default strategies that are in DB
    strategies_to_update_by_name = [
        t["name"] for t in DEFAULT_OPTION_STRATEGIES if t["name"] in db_names
    ]
    
    # 3. duplicate_strategy_names: names that appear > 1 time in DB for this user
    name_counts = {}
    for s in db_strategies:
        name = s.get("name")
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1
    duplicate_strategy_names = [name for name, count in name_counts.items() if count > 1]
    
    # 4. duplicate_code_hashes: code hashes that appear > 1 time in DB for this user
    hash_counts = {}
    for s in db_strategies:
        code = s.get("python_code") or ""
        ch = calculate_code_hash(code)
        hash_counts[ch] = hash_counts.get(ch, 0) + 1
    duplicate_code_hashes = [ch for ch, count in hash_counts.items() if count > 1]
    
    # 5. old_default_versions: mapping of default strategy name to its default_strategy_version
    default_names = {t["name"] for t in DEFAULT_OPTION_STRATEGIES}
    old_default_versions = {
        s["name"]: s.get("default_strategy_version")
        for s in db_strategies
        if s.get("name") in default_names and s.get("default_strategy_version")
    }
    
    # 6. missing_v13_signal_metadata: list of names in DB missing signal_schema_status == "V13_SIGNAL"
    missing_v13_signal_metadata = [
        s["name"] for s in db_strategies
        if s.get("signal_schema_status") != "V13_SIGNAL"
    ]
    
    # 7. strategies_that_use_signal_only_exit: list of strategy names using exit_mode == "signal_only"
    strategies_that_use_signal_only_exit = []
    for s in db_strategies:
        exit_mode = s.get("exit_mode") or s.get("risk", {}).get("exit_mode") or s.get("visual_config", {}).get("risk", {}).get("exit_mode")
        if exit_mode == "signal_only":
            strategies_that_use_signal_only_exit.append(s["name"])
            
    # 8. MCX_removed_confirmed: confirm no MCX/CRUDE/NATURALGAS strategies are in the catalog
    mcx_removed_confirmed = all(
        str(t.get("instrument_group") or "").upper() != "MCX"
        and "CRUDE" not in str(t.get("name") or "").upper()
        and "NATURAL GAS" not in str(t.get("name") or "").upper()
        for t in DEFAULT_OPTION_STRATEGIES
    )
    
    return {
        "ok": True,
        "strategies_to_insert": strategies_to_insert,
        "strategies_to_update_by_name": strategies_to_update_by_name,
        "duplicate_strategy_names": duplicate_strategy_names,
        "duplicate_code_hashes": duplicate_code_hashes,
        "old_default_versions": old_default_versions,
        "missing_v13_signal_metadata": missing_v13_signal_metadata,
        "strategies_that_use_signal_only_exit": strategies_that_use_signal_only_exit,
        "MCX_removed_confirmed": mcx_removed_confirmed
    }


@router.get("/r-exit-status")
async def ops_r_exit_status(user=Depends(get_current_user)):
    from server import _current_ltp_for_symbol
    user_id = user["id"]
    
    active_positions = await db.strategy_positions.find({
        "user_id": user_id,
        "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}
    }, {"_id": 0}).to_list(1000)
    
    positions_report = []
    for pos in active_positions:
        symbol = pos.get("trading_symbol") or pos.get("symbol")
        if not symbol:
            continue
            
        exchange = pos.get("exchange") or ("NFO" if symbol.endswith(("CE", "PE")) else "NSE")
        is_option_buy = (
            pos.get("position_side") == "LONG"
            and (
                exchange in {"NFO", "BFO"}
                or symbol.endswith(("CE", "PE"))
                or pos.get("option_type") in {"CE", "PE"}
            )
        )
        if not is_option_buy:
            continue
            
        current_price = await _current_ltp_for_symbol(user_id, symbol, exchange)
        
        strat = await db.strategies.find_one({"id": pos.get("strategy_id"), "user_id": user_id})
        strategy_name = strat.get("name") if strat else pos.get("strategy_id")
        
        entry_time_str = pos.get("entry_time") or pos.get("created_at")
        elapsed_minutes = 0.0
        if entry_time_str:
            try:
                entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                elapsed_minutes = round((datetime.now(timezone.utc) - entry_dt.astimezone(timezone.utc)).total_seconds() / 60.0, 2)
            except Exception:
                pass
                
        positions_report.append({
            "strategy_id": pos.get("strategy_id"),
            "strategy_name": strategy_name,
            "symbol": symbol,
            "position_side": pos.get("position_side"),
            "status": pos.get("status"),
            "ltp": current_price,
            "average_buy_price": pos.get("average_buy_price"),
            "r_initial_risk_amount": pos.get("r_initial_risk_amount"),
            "r_stop_loss_price": pos.get("r_stop_loss_price"),
            "r_take_profit_price": pos.get("r_take_profit_price"),
            "r_current_R": pos.get("r_current_R"),
            "r_max_R_seen": pos.get("r_max_R_seen"),
            "r_trailing_active": pos.get("r_trailing_active"),
            "r_trailing_stop_price": pos.get("r_trailing_stop_price"),
            "best_price_seen": pos.get("best_price_seen"),
            "elapsed_minutes": elapsed_minutes,
            "max_hold_minutes": pos.get("max_hold_minutes"),
        })
        
    return {
        "ok": True,
        "positions": positions_report
    }


@router.get("/option-selector-status")
async def option_selector_status(user=Depends(get_current_user)):
    """
    Returns the last 100 OptionSelector v2 decisions for the authenticated user.

    Each record contains:
        strategy_id, underlying, direction, preference, selected_strike_mode,
        quality_score, reason_code, mode, created_at,
        selected_contract.instrument_key / tradingsymbol / strike / ltp
    """
    user_id = user["id"]
    raw_rows = await db.option_selector_decisions.find(
        {"user_id": user_id},
        {"_id": 0},
    ).sort("created_at", -1).to_list(100)

    rows = []
    for row in raw_rows:
        contract = row.get("selected_contract") or {}
        rows.append({
            "strategy_id": row.get("strategy_id"),
            "underlying": row.get("underlying"),
            "direction": row.get("direction"),
            "preference": row.get("preference"),
            "selected_strike_mode": row.get("selected_strike_mode"),
            "quality_score": row.get("quality_score"),
            "reason_code": row.get("reason_code"),
            "mode": row.get("mode"),
            "created_at": row.get("created_at"),
            "instrument_key": contract.get("instrument_key"),
            "tradingsymbol": contract.get("tradingsymbol"),
            "strike": contract.get("strike"),
            "ltp": contract.get("ltp"),
        })

    blocked = [r for r in rows if r["reason_code"] != "SELECTED"]
    selected = [r for r in rows if r["reason_code"] == "SELECTED"]

    return {
        "ok": True,
        "total": len(rows),
        "selected_count": len(selected),
        "blocked_count": len(blocked),
        "decisions": rows,
    }


@router.get("/signal-priority-status")
async def signal_priority_status(user=Depends(get_current_user)):
    """
    Returns the last 100 ConflictResolver priority decisions for the authenticated user.

    Each record contains:
        strategy_name, symbol_group, action, confidence, option_quality_score,
        target_R, priority_score, decision (APPROVED/SKIPPED), reason_code, created_at

    user_id is intentionally omitted from the response.
    """
    user_id = user["id"]
    raw_rows = await db.signal_priority_decisions.find(
        {"user_id": user_id},
        {"_id": 0, "user_id": 0},   # exclude user_id from output
    ).sort("created_at", -1).to_list(100)

    approved_count = sum(1 for r in raw_rows if r.get("decision") == "APPROVED")
    skipped_count = sum(1 for r in raw_rows if r.get("decision") == "SKIPPED")

    return {
        "ok": True,
        "total": len(raw_rows),
        "approved_count": approved_count,
        "skipped_count": skipped_count,
        "decisions": raw_rows,
    }


@router.get("/live-readiness")
async def ops_live_readiness(user=Depends(get_current_user)):
    """
    Comprehensive live-readiness check for the QuantG ops dashboard.

    Returns status=READY only when all critical checks pass.
    Informational fields (counts, versions, feature flags) are always included.

    Critical checks (any failure → NOT_READY):
      1. CORE_ENGINE_LIVE_ENABLED env flag
      2. User armed + global_live_enabled
      3. Global kill-switch inactive
      4. Upstox token valid (not mock)
      5. Market data feed connected
    """
    from server import (
        APP_VERSION, get_git_info, get_user_upstox_status, get_user_upstox_gateway,
    )
    import os as _os

    user_id = user["id"]
    now_iso = datetime.now(timezone.utc).isoformat()
    reasons: list = []

    # ── git / version ──────────────────────────────────────────────────────
    try:
        commit, branch, _ = get_git_info()
    except Exception:
        commit, branch = "unknown", "unknown"

    # ── 1. CORE_ENGINE_LIVE_ENABLED ────────────────────────────────────────
    live_env = _os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
    if not live_env:
        reasons.append("CORE_ENGINE_LIVE_ENABLED not set to true")

    # ── 2. Arm state ───────────────────────────────────────────────────────
    arm_doc = await db.live_arm_state.find_one({"user_id": user_id})
    armed = bool(arm_doc and arm_doc.get("armed"))
    global_live_enabled = bool(arm_doc and arm_doc.get("global_live_enabled"))
    if not (armed and global_live_enabled):
        reasons.append("System not armed (armed=False or global_live_enabled=False)")

    # ── 3. Kill switch ─────────────────────────────────────────────────────
    ks_doc = await db.risk_state.find_one({"_id": "global_kill_switch"})
    kill_active = bool(ks_doc and ks_doc.get("active"))
    if kill_active:
        reasons.append("Global kill-switch is active")

    # ── 4. Upstox token valid ──────────────────────────────────────────────
    try:
        upstox_status = await get_user_upstox_status(user_id)
    except Exception:
        upstox_status = {}
    token_valid = bool(upstox_status.get("connected") and upstox_status.get("token_valid"))
    # Reject mock tokens explicitly
    broker_keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    mock_token = bool(broker_keys and str(broker_keys.get("access_token", "")).startswith("mock_"))
    if mock_token:
        token_valid = False
    if not token_valid:
        reasons.append("Upstox token missing, invalid, or is a mock token")

    # ── 5. Feed connected ──────────────────────────────────────────────────
    feed_connected = False
    latest_tick_age_seconds = None
    try:
        upstox_gw = await get_user_upstox_gateway(user_id)
        if upstox_gw:
            gw_status = upstox_gw.status()
            feed_connected = bool(gw_status.get("feed_running") or gw_status.get("ws_running"))
            last_tick = gw_status.get("last_tick_time") or gw_status.get("last_tick_at")
            if last_tick:
                try:
                    lt = datetime.fromisoformat(str(last_tick).replace("Z", "+00:00"))
                    lt_utc = lt if lt.tzinfo else lt.replace(tzinfo=timezone.utc)
                    latest_tick_age_seconds = round(
                        (datetime.now(timezone.utc) - lt_utc.astimezone(timezone.utc)).total_seconds(), 1
                    )
                except Exception:
                    pass
    except Exception:
        pass
    if not feed_connected:
        reasons.append("Upstox market data feed not connected")

    # ── Informational counts ───────────────────────────────────────────────
    live_strategy_count = await db.strategies.count_documents(
        {"user_id": user_id, "mode": "live", "status": "live"}
    )
    open_positions_count = await db.strategy_positions.count_documents(
        {"user_id": user_id, "status": {"$in": ["OPEN", "FILLED", "PENDING_BROKER", "EXITING"]}}
    )
    pending_orders_count = await db.orders.count_documents(
        {"user_id": user_id, "mode": "live", "status": {"$in": ["PLACED", "PENDING_BROKER", "OPEN"]}}
    )
    unknown_orders_count = await db.orders.count_documents(
        {"user_id": user_id, "status": "UNKNOWN_NEEDS_REVIEW"}
    )

    # ── Feature-active flags (based on recent decisions) ───────────────────
    cutoff_1h = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    option_selector_active = bool(
        await db.option_selector_decisions.find_one(
            {"user_id": user_id, "created_at": {"$gte": cutoff_1h}}
        )
    )
    r_exit_active = bool(
        await db.strategy_positions.find_one(
            {"user_id": user_id, "r_stop_loss_price": {"$exists": True},
             "status": {"$in": ["OPEN", "FILLED", "PENDING_BROKER"]}}
        )
    )
    signal_priority_active = bool(
        await db.signal_priority_decisions.find_one(
            {"user_id": user_id, "created_at": {"$gte": cutoff_1h}}
        )
    )

    # ── Default strategy version (from most recent live strategy) ──────────
    recent_strat = await db.strategies.find_one(
        {"user_id": user_id, "mode": "live"},
        sort=[("updated_at", -1)],
    )
    default_strategy_version = (
        (recent_strat or {}).get("default_strategy_version")
        or (recent_strat or {}).get("strategy_version")
        or "v13-live-brain-r1"
    )

    status = "READY" if not reasons else "NOT_READY"

    return {
        "status": status,
        "reasons": reasons,
        "app_version": APP_VERSION,
        "git_commit": commit,
        "git_branch": branch,
        "CORE_ENGINE_LIVE_ENABLED": live_env,
        "arm_state": {
            "armed": armed,
            "global_live_enabled": global_live_enabled,
        },
        "kill_switch_active": kill_active,
        "upstox_token_valid": token_valid,
        "feed_connected": feed_connected,
        "latest_tick_age_seconds": latest_tick_age_seconds,
        "live_strategy_count": live_strategy_count,
        "open_positions_count": open_positions_count,
        "pending_orders_count": pending_orders_count,
        "unknown_orders_count": unknown_orders_count,
        "default_strategy_version": default_strategy_version,
        "option_selector_active": option_selector_active,
        "r_exit_active": r_exit_active,
        "signal_priority_active": signal_priority_active,
        "timestamp": now_iso,
    }

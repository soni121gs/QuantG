"""ERP Phase 4 Hermes research analyst utilities.

Deterministic research probes and hypothesis-card helpers. Hermes may narrate
these results, but all evidence here is computed by code and remains read-only.
"""
from __future__ import annotations

import hashlib
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.earnings_calendar import events_for
from core.india_flows import get_participant_oi, participant_oi_days


def _default_research_root() -> Path:
    here = Path(__file__).resolve()
    for root in (
        here.parents[1] / "wiki" / "Research",
        here.parents[2] / "wiki" / "Research",
    ):
        if root.exists():
            return root
    return here.parents[2] / "wiki" / "Research"


RESEARCH_ROOT = _default_research_root()
REQUIRED_CARD_FIELDS = ("hypothesis", "who_pays", "universe", "horizon", "judge", "data_needed", "kill_criteria")
PROBES = ("vrp_by_strike", "earnings_iv_runup", "overnight_intraday_split", "term_structure_richness", "participant_oi_extremes")


def corpus_files(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    root = root or RESEARCH_ROOT
    out = []
    if not root.exists():
        return out
    for path in sorted(root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        out.append({"title": path.stem, "path": str(path), "chars": len(text),
                    "sha16": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]})
    return out


def corpus_status(root: Optional[Path] = None) -> Dict[str, Any]:
    files = corpus_files(root)
    return {"count": len(files), "files": files,
            "acceptance": {"required_min": 25, "passed": len(files) >= 25}}


def _pct(a: float, b: float) -> Optional[float]:
    return ((a - b) / b * 100.0) if b else None


def _latest_day(store: BhavcopyStore, start: Optional[str], end: Optional[str]) -> Optional[str]:
    days = store.trading_days(start, end)
    return days[-1] if days else None


def vrp_by_strike(store: Optional[BhavcopyStore] = None, *, underlying: str = "NIFTY",
                  start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    day = _latest_day(store, start, end)
    if not day:
        return {"probe": "vrp_by_strike", "status": "no_data"}
    try:
        from core.iv_surface import surface_for_day
        surf = surface_for_day(store, underlying, day)
    except Exception as exc:  # noqa: BLE001
        return {"probe": "vrp_by_strike", "status": "error", "error": str(exc)}
    points = surf.get("points") or []
    atm = surf.get("spot") or 0
    rows = sorted(points, key=lambda p: abs(float(p.get("strike") or 0) - atm))[:12]
    return {"probe": "vrp_by_strike", "status": "ready", "underlying": underlying,
            "date": day, "sample_n": len(rows), "rows": rows}


def earnings_iv_runup(store: Optional[BhavcopyStore] = None, *, symbol: str = "RELIANCE",
                      start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    events = events_for(symbol, start or "1900-01-01", end or "2999-12-31")
    days = set(store.trading_days(start, end))
    rows = []
    for ev in events:
        d = str(ev.get("date") or "")[:10]
        if d not in days:
            continue
        prev = sorted(x for x in days if x < d)
        if len(prev) < 5:
            continue
        rows.append({"symbol": symbol, "event_date": d, "pre_event_days": prev[-5:]})
    return {"probe": "earnings_iv_runup", "status": "ready", "symbol": symbol,
            "event_count": len(events), "sample_n": len(rows), "rows": rows[:20]}


def overnight_intraday_split(store: Optional[BhavcopyStore] = None, *, underlying: str = "NIFTY",
                             start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    candles = store.underlying_daily(underlying, start, end)
    overnight = []
    session = []
    for i in range(1, len(candles)):
        prev_close = float(candles[i - 1].get("close") or 0)
        open_px = float(candles[i].get("open") or 0)
        close = float(candles[i].get("close") or 0)
        if prev_close and open_px:
            overnight.append((open_px - prev_close) / prev_close * 100)
        if open_px and close:
            session.append((close - open_px) / open_px * 100)
    return {"probe": "overnight_intraday_split", "status": "ready", "underlying": underlying,
            "sample_n": min(len(overnight), len(session)),
            "overnight_mean_pct": round(mean(overnight), 4) if overnight else 0,
            "session_mean_pct": round(mean(session), 4) if session else 0}


def term_structure_richness(store: Optional[BhavcopyStore] = None, *, underlying: str = "NIFTY",
                            start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    day = _latest_day(store, start, end)
    if not day:
        return {"probe": "term_structure_richness", "status": "no_data"}
    chain = store.option_chain(underlying, day)
    expiries = sorted(chain.keys())
    return {"probe": "term_structure_richness", "status": "ready", "underlying": underlying,
            "date": day, "expiry_count": len(expiries), "near": expiries[:3], "far": expiries[-3:]}


def _participant_bias(row: Dict[str, Any]) -> float:
    rows = {str(r.get("participant") or "").upper(): r for r in (row.get("rows") or [])}
    fii = rows.get("FII") or {}
    client = rows.get("CLIENT") or {}
    def val(rec, key):
        try:
            return float(rec.get(key) or 0)
        except Exception:  # noqa: BLE001
            return 0.0
    return (val(fii, "future_index_long") - val(fii, "future_index_short")
            - 0.25 * (val(client, "future_index_long") - val(client, "future_index_short")))


def participant_oi_extremes(*, start: Optional[str] = None, end: Optional[str] = None,
                            top_n: int = 10) -> Dict[str, Any]:
    rows = []
    for day in participant_oi_days():
        if (start and day < start) or (end and day > end):
            continue
        row = get_participant_oi(day)
        if not row:
            continue
        rows.append({"date": day, "bias": round(_participant_bias(row), 2)})
    rows.sort(key=lambda r: abs(r["bias"]), reverse=True)
    return {"probe": "participant_oi_extremes", "status": "ready",
            "sample_n": len(rows), "rows": rows[:top_n]}


def run_opportunity_probes(*, store: Optional[BhavcopyStore] = None,
                           start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    rows = [
        vrp_by_strike(store, start=start, end=end),
        earnings_iv_runup(store, start=start, end=end),
        overnight_intraday_split(store, start=start, end=end),
        term_structure_richness(store, start=start, end=end),
        participant_oi_extremes(start=start, end=end),
    ]
    return {"kind": "research_signals", "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(rows), "rows": rows}


def persist_research_signals(db, result: Dict[str, Any]) -> int:
    # Sync helper for CLI pymongo clients.
    n = 0
    for row in result.get("rows") or []:
        key = f"{row.get('probe')}:{row.get('underlying') or row.get('symbol') or 'global'}"
        db.research_signals.update_one({"_id": key}, {"$set": {**row, "_id": key,
            "updated_at": result.get("generated_at")}}, upsert=True)
        n += 1
    return n


def _card_to_research_doc(card: Dict[str, Any], *, user_id: str) -> Dict[str, Any]:
    import uuid

    now = datetime.now(timezone.utc).isoformat()
    canonical = {
        "hypothesis": card["hypothesis"],
        "who_pays": card["who_pays"],
        "universe": card["universe"],
        "horizon": card["horizon"],
        "judge": card["judge"],
        "data_needed": card["data_needed"],
        "kill_criteria": card["kill_criteria"],
        "citations": card["citations"],
    }
    return {
        "hypothesis_id": card.get("hypothesis_id") or f"rh_{uuid.uuid4().hex[:16]}",
        "user_id": user_id,
        "created_by": "hermes_phase4",
        "created_at": card.get("created_at") or now,
        "updated_at": now,
        "status": "draft",
        "hypothesis": card["hypothesis"],
        "market_premise": card["who_pays"],
        "instrument": card["universe"],
        "timeframe": card["horizon"],
        "entry_exit_idea": card["hypothesis"],
        "data_window": {},
        "null_hypothesis": card["kill_criteria"],
        "expected_edge": {"judge": card["judge"]},
        "cost_model": {"law": "expected edge must clear 3x modeled friction before design promotion"},
        "risk_thesis": card["who_pays"],
        "failure_modes": [card["kill_criteria"]],
        "evidence_links": card["citations"],
        "verdict": {
            "status": "UNTESTED",
            "confidence": 0.0,
            "summary": "Phase 4 hypothesis card proposed; no OOS verdict attached yet.",
        },
        "research_context": {
            "kind": "phase4_hypothesis_card",
            "data_needed": card["data_needed"],
            "judge": card["judge"],
            "calibration": card.get("calibration") or {"outcome": "untested"},
        },
        "tags": ["phase4", "hypothesis_card", "PROPOSED"],
        "hypothesis_hash": hashlib.sha256(
            repr(sorted(canonical.items())).encode("utf-8")
        ).hexdigest()[:24],
    }


def persist_hypothesis_cards(db, user_id: str, cards: Iterable[Dict[str, Any]]) -> int:
    n = 0
    for card in cards:
        doc = _card_to_research_doc(card, user_id=user_id)
        db.research_hypotheses.update_one(
            {"user_id": user_id, "hypothesis_hash": doc["hypothesis_hash"]},
            {"$setOnInsert": doc, "$set": {"updated_at": doc["updated_at"]}},
            upsert=True,
        )
        n += 1
    return n


def build_hypothesis_card(payload: Dict[str, Any]) -> Dict[str, Any]:
    missing = [f for f in REQUIRED_CARD_FIELDS if not payload.get(f)]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    citations = payload.get("citations") or []
    if not citations:
        raise ValueError("hypothesis card requires at least one corpus or probe citation")
    return {"kind": "hypothesis_card", "status": "PROPOSED",
            "created_at": datetime.now(timezone.utc).isoformat(),
            **{k: payload[k] for k in REQUIRED_CARD_FIELDS},
            "citations": citations,
            "trial_status": "UNTESTED",
            "calibration": {"outcome": "untested"}}


def default_weekly_cards(probes: Dict[str, Any], corpus: Dict[str, Any]) -> List[Dict[str, Any]]:
    citations = []
    if corpus.get("files"):
        citations.append({"type": "corpus", "ref": corpus["files"][0]["title"]})
    if probes.get("rows"):
        citations.append({"type": "probe", "ref": probes["rows"][0]["probe"]})
    return [
        build_hypothesis_card({
            "hypothesis": "NIFTY premium selling should stand down unless IV surface richness is positive.",
            "who_pays": "Option buyers overpay when implied volatility is rich versus recent surface history.",
            "universe": "NIFTY weekly defined-risk credit spreads",
            "horizon": "2-8 DTE held or recycled under EOD/RAE judge",
            "judge": "EOD options OOS plus IV-surface-gated comparison",
            "data_needed": "bhavcopy option chains, IV surface richness, trade costs",
            "kill_criteria": "OOS expectancy <= 0 or DSR fails after gating",
            "citations": citations,
        }),
        build_hypothesis_card({
            "hypothesis": "Participant-OI futures bias is only useful as a sizing overlay, not an entry signal.",
            "who_pays": "Crowded client positioning may create weak contrarian or confirmation effects.",
            "universe": "NIFTY S3/S4 sleeves",
            "horizon": "5 trading days",
            "judge": "participant-OI overlay validator against base sleeve results",
            "data_needed": "participant OI store and underlying futures returns",
            "kill_criteria": "Overlay expectancy near zero or sign flips by period",
            "citations": citations,
        }),
    ]


def calibration_summary(cards: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(cards)
    counts: Dict[str, int] = {}
    for card in rows:
        outcome = ((card.get("calibration") or {}).get("outcome")
                   or card.get("trial_status") or card.get("status") or "unknown")
        counts[outcome] = counts.get(outcome, 0) + 1
    tested = sum(v for k, v in counts.items() if k in {"validated", "rejected", "abandoned"})
    hit = counts.get("validated", 0)
    return {"total_cards": len(rows), "counts": counts, "tested_cards": tested,
            "hit_rate": round(hit / tested, 3) if tested else 0.0}


def paid_lane_status() -> Dict[str, Any]:
    model = os.environ.get("HERMES_RESEARCH_MODEL") or os.environ.get("PAID_RESEARCH_MODEL")
    approved = os.environ.get("HERMES_PAID_RESEARCH_ENABLED", "false").lower() == "true"
    return {"task": "P4-6", "approved": approved, "model": model,
            "weekly_synthesis_uses_paid_lane": bool(approved and model),
            "note": "D-3 founder decision required before enabling paid weekly synthesis."}

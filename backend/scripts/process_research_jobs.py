#!/usr/bin/env python3
"""Claim and execute durable research jobs outside the trading web process."""
from __future__ import annotations

import os
import asyncio
import socket
import sys
import traceback
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymongo  # noqa: E402
from pymongo import ReturnDocument  # noqa: E402


LEASE_SECONDS = int(os.environ.get("RESEARCH_JOB_LEASE_SECONDS", "10800"))
WORKER = f"{socket.gethostname()}:{os.getpid()}"


def _db():
    return pymongo.MongoClient(
        os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
        serverSelectionTimeoutMS=4000,
    )[os.environ.get("DB_NAME", "quantg")]


def _claim(db):
    now = datetime.now(timezone.utc)
    return db.research_jobs.find_one_and_update(
        {
            "$or": [
                {"status": "queued"},
                {"status": "running", "lease_expires_at": {"$lt": now}},
            ]
        },
        {
            "$set": {
                "status": "running",
                "worker": WORKER,
                "started_at": now,
                "updated_at": now,
                "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS),
            },
            "$inc": {"attempts": 1},
        },
        sort=[("queued_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _edge_lab(db, job):
    from scripts.build_edge_lab_snapshot import build_for_user
    build_for_user(db, str(job["user_id"]))


def _intraday_oos(db, job):
    from scripts.run_intraday_options_validation import main
    params = job.get("params") or {}
    main(["--from", params.get("start", "2025-01-01"), "--to", params.get("end", "2025-12-31")])


def _regime_oos(db, job):
    from scripts.run_regime_oos_validation import compute
    params = job.get("params") or {}
    result = compute(db, start=params.get("start"), end=params.get("end"),
                     status=params.get("status", "all"))
    result.update({
        "status": "error" if result.get("error") else "ok",
        "scope": "global",
        "requested_by": job.get("user_id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })
    db.regime_oos_runs.insert_one(result)


def _hermes_validation(db, job):
    from motor.motor_asyncio import AsyncIOMotorClient
    from core.hermes_advisor import compile_hermes_advice
    from core.hermes_historical_validator import validate_structure_lessons

    async def run():
        adb = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))[
            os.environ.get("DB_NAME", "quantg")
        ]
        result = await validate_structure_lessons(adb, str(job["user_id"]))
        await compile_hermes_advice(adb, str(job["user_id"]))
        return result

    print(asyncio.run(run()))


def _earnings_calendar(db, job):
    from datetime import timedelta
    from scripts.earnings_calendar_fetch_nse import collect_events, fno_universe_from_store

    today = datetime.now(timezone.utc).date()
    universe = fno_universe_from_store()
    print(collect_events(
        universe, today - timedelta(days=45), today + timedelta(days=45),
        chunk_days=120, sleep_sec=0.5,
    ))


def _phase4_research(db, job):
    from core.phase4_research import (
        corpus_status,
        default_weekly_cards,
        persist_hypothesis_cards,
        persist_research_signals,
        run_opportunity_probes,
    )
    user_id = str(job["user_id"])
    corpus = corpus_status()
    probes = run_opportunity_probes()
    cards = default_weekly_cards(probes, corpus)
    print({
        "signals": persist_research_signals(db, probes, user_id=user_id),
        "cards": persist_hypothesis_cards(db, user_id, cards),
        "corpus_count": corpus.get("count"),
    })


HANDLERS = {
    "edge_lab": _edge_lab,
    "intraday_oos": _intraday_oos,
    "regime_oos": _regime_oos,
    "hermes_validation": _hermes_validation,
    "earnings_calendar": _earnings_calendar,
    "phase4_research": _phase4_research,
}


def main():
    db = _db()
    processed = 0
    while True:
        job = _claim(db)
        if not job:
            break
        now = datetime.now(timezone.utc)
        try:
            handler = HANDLERS[job["kind"]]
            handler(db, job)
            db.research_jobs.update_one(
                {"_id": job["_id"], "worker": WORKER},
                {"$set": {"status": "complete", "completed_at": now,
                          "updated_at": now, "lease_expires_at": None, "error": None}},
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            db.research_jobs.update_one(
                {"_id": job["_id"], "worker": WORKER},
                {"$set": {"status": "failed", "failed_at": now, "updated_at": now,
                          "lease_expires_at": None, "error": str(exc)[:2000]}},
            )
        processed += 1
    print(f"research jobs processed={processed}")


if __name__ == "__main__":
    main()

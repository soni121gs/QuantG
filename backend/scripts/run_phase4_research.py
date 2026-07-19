"""Run ERP Phase 4 Hermes research analyst jobs.

This script is deterministic and trading-read-only. It can be used locally,
inside the backend container, or from cron for corpus/probe/card maintenance.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.phase4_research import (  # noqa: E402
    calibration_summary,
    corpus_status,
    default_weekly_cards,
    paid_lane_status,
    persist_hypothesis_cards,
    persist_research_signals,
    run_opportunity_probes,
)


def _db():
    import pymongo

    url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    name = os.environ.get("DB_NAME", "quantg")
    return pymongo.MongoClient(url, serverSelectionTimeoutMS=5000)[name]


def _cards_from_db(user_id: str):
    db = _db()
    return list(db.research_hypotheses.find(
        {"user_id": user_id, "research_context.kind": "phase4_hypothesis_card"},
        {"_id": 0, "status": 1, "verdict": 1, "research_context": 1},
    ))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=["all", "corpus", "probes", "cards", "calibration", "paid-lane"],
                        default="all")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--user-id", default=os.environ.get("QUANTG_OWNER_USER_ID", "phase4-research"))
    args = parser.parse_args()

    output = {}
    corpus = corpus_status()
    probes = None

    if args.task in ("all", "corpus", "cards"):
        output["corpus"] = corpus

    if args.task in ("all", "probes", "cards"):
        probes = run_opportunity_probes(start=args.start, end=args.end)
        output["probes"] = probes
        if args.persist:
            output["persisted_signals"] = persist_research_signals(_db(), probes)

    if args.task in ("all", "cards"):
        probes = probes or run_opportunity_probes(start=args.start, end=args.end)
        cards = default_weekly_cards(probes, corpus)
        output["cards"] = cards
        if args.persist:
            output["persisted_cards"] = persist_hypothesis_cards(_db(), args.user_id, cards)

    if args.task in ("all", "calibration"):
        rows = _cards_from_db(args.user_id) if args.persist else output.get("cards", [])
        output["calibration"] = calibration_summary(rows)

    if args.task in ("all", "paid-lane"):
        output["paid_lane"] = paid_lane_status()

    print(json.dumps(output, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build the Edge Lab snapshot and cache it in db.edge_lab_snapshots (_id='latest').

The Edge Lab tab (frontend) reads the cached doc via GET /ops/edge-lab — this
script (or POST /ops/edge-lab/refresh) is what populates it. Heavy: it re-prices
two years of option chains, so run it occasionally, not on a schedule tighter
than the data updates.

Run inside the backend container (needs Mongo + the bhavcopy store at /app/data):
    docker exec quantg-backend python /app/scripts/build_edge_lab_snapshot.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app

import pymongo  # noqa: E402
from core.edge_lab import build_snapshot  # noqa: E402
from core.bhavcopy_store import BhavcopyStore  # noqa: E402
from core.edge_research_ledger import trial_document  # noqa: E402


def main():
    import traceback
    from datetime import datetime, timezone

    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
                             serverSelectionTimeoutMS=4000)[os.environ.get("DB_NAME", "quantg")]
    strategies = list(db.strategies.find({"python_code": {"$nin": [None, ""]}}))
    print(f"building edge-lab snapshot over {len(strategies)} strategies…", flush=True)
    try:
        snap = build_snapshot(strategies, BhavcopyStore())
    except Exception as exc:  # noqa: BLE001 — persist the failure so it is visible in the UI/collection
        traceback.print_exc()
        db.edge_lab_snapshots.replace_one(
            {"_id": "latest"},
            {"_id": "latest", "status": "error", "error": str(exc),
             "generated_at": datetime.now(timezone.utc).isoformat()}, upsert=True)
        raise
    snap["_id"] = "latest"
    db.edge_lab_snapshots.replace_one({"_id": "latest"}, snap, upsert=True)
    for row in snap.get("oos", {}).get("rows") or []:
        user_id = next((str(s.get("user_id")) for s in strategies if s.get("name") == row.get("name")), "system")
        trial = trial_document(user_id=user_id, row=row, snapshot=snap)
        db.strategy_trials.update_one(
            {"_id": trial["_id"]},
            {"$set": {**trial, "last_run_at": snap.get("generated_at")},
             "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
             "$inc": {"run_count": 1}},
            upsert=True,
        )
    print(f"done: status={snap.get('status')} generated_at={snap.get('generated_at')}", flush=True)
    if snap.get("oos"):
        print("OOS verdict counts:", snap["oos"]["counts"])
    for sw in (snap.get("sweep") or []):
        print(f"sweep {sw['name']}: positive-OOS {sw['positive_oos']}/{sw['configs']}, "
              f"candidate-edges {sw['candidate_edges']}")


if __name__ == "__main__":
    main()

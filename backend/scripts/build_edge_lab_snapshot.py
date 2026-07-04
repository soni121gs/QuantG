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


def main():
    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
                             serverSelectionTimeoutMS=4000)[os.environ.get("DB_NAME", "quantg")]
    strategies = list(db.strategies.find({"python_code": {"$nin": [None, ""]}}))
    print(f"building edge-lab snapshot over {len(strategies)} strategies…")
    snap = build_snapshot(strategies, BhavcopyStore())
    snap["_id"] = "latest"
    db.edge_lab_snapshots.replace_one({"_id": "latest"}, snap, upsert=True)
    print(f"done: status={snap.get('status')} generated_at={snap.get('generated_at')}")
    if snap.get("oos"):
        print("OOS verdict counts:", snap["oos"]["counts"])
    for sw in (snap.get("sweep") or []):
        print(f"sweep {sw['name']}: positive-OOS {sw['positive_oos']}/{sw['configs']}, "
              f"candidate-edges {sw['candidate_edges']}")


if __name__ == "__main__":
    main()

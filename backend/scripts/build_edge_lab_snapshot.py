#!/usr/bin/env python3
"""Build user-scoped Edge Lab snapshots.

Run inside the backend container:
    docker exec quantg-backend python /app/scripts/build_edge_lab_snapshot.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymongo  # noqa: E402
from core.bhavcopy_store import BhavcopyStore  # noqa: E402
from core.edge_lab import build_snapshot  # noqa: E402
from core.edge_research_ledger import trial_document  # noqa: E402


def main():
    import traceback
    from datetime import datetime, timezone

    db = pymongo.MongoClient(
        os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
        serverSelectionTimeoutMS=4000,
    )[os.environ.get("DB_NAME", "quantg")]
    users = list(db.users.find({}, {"_id": 0, "id": 1})) or [{"id": "system"}]

    for user in users:
        user_id = str(user.get("id") or "system")
        query = {
            "status": {"$ne": "archived"},
            "python_code": {"$nin": [None, ""]},
        }
        if user_id != "system":
            query["user_id"] = user_id
        strategies = list(db.strategies.find(query))
        print(f"building edge-lab snapshot for {user_id} over {len(strategies)} strategies...", flush=True)
        try:
            snap = build_snapshot(strategies, BhavcopyStore())
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            db.edge_lab_snapshots.replace_one(
                {"_id": f"latest:{user_id}"},
                {
                    "_id": f"latest:{user_id}",
                    "status": "error",
                    "error": str(exc),
                    "built_by": user_id,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                upsert=True,
            )
            raise

        snap["_id"] = f"latest:{user_id}"
        snap["built_by"] = user_id
        snap["book_scope"] = "erp_current_non_archived"
        snap["strategy_names"] = [s.get("name") for s in strategies]
        db.edge_lab_snapshots.replace_one({"_id": f"latest:{user_id}"}, snap, upsert=True)

        for row in snap.get("oos", {}).get("rows") or []:
            trial = trial_document(user_id=user_id, row=row, snapshot=snap)
            trial.pop("created_at", None)
            db.strategy_trials.update_one(
                {"_id": trial["_id"]},
                {
                    "$set": {**trial, "last_run_at": snap.get("generated_at")},
                    "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
                    "$inc": {"run_count": 1},
                },
                upsert=True,
            )

        print(f"done {user_id}: status={snap.get('status')} generated_at={snap.get('generated_at')}", flush=True)
        if snap.get("oos"):
            print("OOS verdict counts:", snap["oos"]["counts"])
        for sw in snap.get("sweep") or []:
            print(
                f"sweep {sw['name']}: positive-OOS {sw['positive_oos']}/{sw['configs']}, "
                f"candidate-edges {sw['candidate_edges']}"
            )


if __name__ == "__main__":
    main()

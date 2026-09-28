#!/usr/bin/env python3
"""Seed the five approved strategies into the real paper execution path."""
import copy
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymongo
from core.diversified_paper_strategies import STRATEGIES


def main() -> int:
    apply = "--apply" in sys.argv
    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"), serverSelectionTimeoutMS=5000)[os.environ.get("DB_NAME", "quantg")]
    template = db.strategies.find_one({"status": "live", "user_id": {"$exists": True}})
    if not template:
        print("ERROR: no strategy template found")
        return 1
    for spec in STRATEGIES:
        doc = copy.deepcopy(template)
        doc.pop("_id", None)
        doc.update({"id": spec["id"], "name": spec["name"], "symbol": spec["symbol"],
                    "description": "Executable diversified sleeve; paper-only until OOS and forward-paper gates pass.",
                    "python_code": spec["code"], "status": "live", "mode": "paper",
                    "paper_only": True, "founder_forced_live": False,
                    "created_at": datetime.now(timezone.utc).isoformat(), "archived_at": None})
        doc["visual_config"] = {"symbol": spec["symbol"], "exchange": spec["exchange"],
                                 "options": spec["options"], "risk": spec["risk"]}
        if apply:
            old = db.strategies.find_one({"id": spec["id"]})
            if old:
                db.strategies.update_one({"_id": old["_id"]}, {"$set": {k: doc[k] for k in ("name", "symbol", "description", "python_code", "status", "mode", "paper_only", "founder_forced_live", "visual_config", "archived_at")}})
            else:
                doc["_id"] = str(uuid.uuid4())
                db.strategies.insert_one(doc)
        print(("APPLY" if apply else "DRY-RUN"), spec["id"], spec["name"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

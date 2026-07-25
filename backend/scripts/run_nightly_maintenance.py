#!/usr/bin/env python3
"""Production-safe nightly maintenance; no test runner assumptions."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from core.hermes_diagnostics import run_diagnostics  # noqa: E402
from core.hermes_lessons import requalify_legacy_active_lessons  # noqa: E402
from core.research_jobs import enqueue_research_job  # noqa: E402
from core.research_rag import reindex_all  # noqa: E402
from scripts.generate_measured_wiki import main as generate_measured_wiki  # noqa: E402


async def main():
    generated = True
    try:
        generate_measured_wiki()
    except Exception as exc:  # noqa: BLE001
        generated = False
        print({"step": "generate_measured_wiki", "status": "failed", "error": str(exc)})
    db = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))[
        os.environ.get("DB_NAME", "quantg")
    ]
    users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000)
    for row in users:
        user_id = row.get("id")
        if not user_id:
            continue
        result = {"user_id": user_id, "measured_wiki_generated": generated}
        for name, operation in (
            ("legacy_lessons_downgraded", requalify_legacy_active_lessons(db, user_id)),
            ("diagnostics", run_diagnostics(db, user_id, persist=True, auto_oos=False)),
            ("rag", reindex_all(db, user_id)),
            ("edge_lab", enqueue_research_job(db, kind="edge_lab", user_id=user_id)),
        ):
            try:
                value = await operation
                result[name] = value.get("summary") if name == "diagnostics" else value
            except Exception as exc:  # noqa: BLE001
                result[name] = {"status": "failed", "error": str(exc)}
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        print({
            **result,
        })


if __name__ == "__main__":
    asyncio.run(main())

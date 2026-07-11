#!/usr/bin/env python3
"""IA-7: nightly research-RAG refresh — re-embed new/changed wiki notes, Hermes
lessons and OOS verdicts into db.hermes_memory (idempotent; skips unchanged).

Run via host cron post-EOD:
    docker exec quantg-backend python /app/scripts/research_rag_reindex.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient
from core.research_rag import reindex_all


async def main():
    db = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))[
        os.environ.get("DB_NAME", "quantg")]
    async for u in db.users.find({}, {"id": 1}):
        uid = u.get("id")
        if not uid:
            continue
        r = await reindex_all(db, uid)
        print(f"user {uid}: {r['stats']} {r['by_source']}")


if __name__ == "__main__":
    asyncio.run(main())

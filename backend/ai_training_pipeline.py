"""Export QuantG strategy context for Gemini prompt grounding.

This is not a broker-facing process. It creates JSONL records from MongoDB that
can be used as retrieval/context examples for Gemini prompts or for future
fine-tuning experiments where the provider supports it.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")


def _json_default(value: Any) -> str:
    return str(value)


def _context_record(strategy: Dict[str, Any], score: Dict[str, Any] | None) -> Dict[str, Any]:
    visual_config = strategy.get("visual_config") or {}
    return {
        "type": "quantg_strategy_context",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": {
            "name": strategy.get("name"),
            "description": strategy.get("description"),
            "asset_class": strategy.get("asset_class"),
            "strategy_type": strategy.get("strategy_type"),
            "required_capital": strategy.get("required_capital"),
            "instrument_group": strategy.get("instrument_group"),
            "visual_config": visual_config,
            "recent_runtime": {
                "evaluations": strategy.get("evaluations", 0),
                "signals_fired": strategy.get("signals_fired", 0),
                "last_error": strategy.get("last_error"),
                "last_data_source": strategy.get("last_data_source"),
            },
        },
        "ai_score": score or {},
    }


async def export_context(output_path: str = "ai_training_context.jsonl") -> Path:
    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ["DB_NAME"]
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    out = ROOT_DIR / output_path
    strategies = await db.strategies.find({}, {"_id": 0}).to_list(5000)
    scores = await db.strategy_ai_scores.find({}, {"_id": 0}).to_list(5000)
    score_by_strategy = {row.get("strategy_id"): row for row in scores}
    with out.open("w", encoding="utf-8") as fh:
        for strategy in strategies:
            record = _context_record(strategy, score_by_strategy.get(strategy.get("id")))
            fh.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    client.close()
    return out


if __name__ == "__main__":
    path = asyncio.run(export_context())
    print(f"Exported Gemini context records to {path}")

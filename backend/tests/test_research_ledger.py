import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.research_ledger import (
    attach_evidence,
    create_hypothesis,
    get_hypothesis,
    list_hypotheses,
    set_verdict,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_args):
        self.rows = sorted(self.rows, key=lambda row: row.get("updated_at", ""), reverse=True)
        return self

    async def to_list(self, limit):
        return [dict(row) for row in self.rows[:limit]]


class FakeCollection:
    def __init__(self):
        self.rows = []

    async def create_index(self, *_args, **_kwargs):
        return None

    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        return SimpleNamespace(inserted_id=doc["hypothesis_id"])

    async def find_one(self, query, projection=None):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                doc = dict(row)
                if projection and projection.get("_id") == 0:
                    doc.pop("_id", None)
                return doc
        return None

    def find(self, query, projection=None):
        rows = []
        for row in self.rows:
            matched = True
            for key, value in query.items():
                if row.get(key) != value:
                    matched = False
                    break
            if matched:
                doc = dict(row)
                if projection and projection.get("_id") == 0:
                    doc.pop("_id", None)
                rows.append(doc)
        return FakeCursor(rows)

    async def update_one(self, query, update):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                for key, value in (update.get("$set") or {}).items():
                    row[key] = value
                for key, value in (update.get("$push") or {}).items():
                    row.setdefault(key, []).append(value)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)


class FakeDB:
    def __init__(self):
        self.research_hypotheses = FakeCollection()


@pytest.mark.anyio
async def test_research_hypothesis_lifecycle():
    db = FakeDB()
    created = await create_hypothesis(db, "user-1", {
        "hypothesis": "IV rich range days support defined-risk put spread selling.",
        "market_premise": "Vol risk premium is positive when implied vol exceeds realized vol.",
        "instrument": "NIFTY options",
        "timeframe": "weekly",
        "entry_exit_idea": "Sell 3% OTM put spread and hold to expiry.",
        "data_window": {"start": "2024-01-01", "end": "2025-12-31"},
        "null_hypothesis": "Cost-adjusted expectancy is <= 0.",
        "expected_edge": {"expectancy_inr": 100},
        "cost_model": {"slippage_pct": 3},
        "risk_thesis": "Defined risk caps trend-down losses.",
        "failure_modes": ["trend_down", "event_gap"],
        "tags": ["short_vol", "oos"],
    })

    assert created["hypothesis_id"].startswith("rh_")
    assert created["status"] == "draft"
    assert created["verdict"]["status"] == "UNTESTED"

    deduped = await create_hypothesis(db, "user-1", {
        "hypothesis": "IV rich range days support defined-risk put spread selling.",
        "market_premise": "Vol risk premium is positive when implied vol exceeds realized vol.",
        "instrument": "NIFTY options",
        "timeframe": "weekly",
        "entry_exit_idea": "Sell 3% OTM put spread and hold to expiry.",
        "data_window": {"start": "2024-01-01", "end": "2025-12-31"},
        "null_hypothesis": "Cost-adjusted expectancy is <= 0.",
        "expected_edge": {"expectancy_inr": 100},
        "cost_model": {"slippage_pct": 3},
        "risk_thesis": "Defined risk caps trend-down losses.",
        "failure_modes": ["trend_down", "event_gap"],
    })
    assert deduped["deduped"] is True

    with_evidence = await attach_evidence(db, "user-1", created["hypothesis_id"], {
        "source": "edge_lab",
        "summary": "Walk-forward passed",
        "metrics": {"expectancy": 214},
        "sample_n": 106,
        "confidence": 0.8,
    })
    assert with_evidence["status"] == "testing"
    assert with_evidence["evidence_links"][0]["source"] == "edge_lab"

    with_verdict = await set_verdict(db, "user-1", created["hypothesis_id"], {
        "status": "CANDIDATE_EDGE",
        "summary": "Positive OOS after costs",
        "confidence": 0.82,
        "sample_n": 106,
    })
    assert with_verdict["status"] == "validated"
    assert with_verdict["verdict"]["status"] == "CANDIDATE_EDGE"

    fetched = await get_hypothesis(db, "user-1", created["hypothesis_id"])
    assert fetched["hypothesis_id"] == created["hypothesis_id"]

    rows = await list_hypotheses(db, "user-1")
    assert len(rows) == 1


@pytest.mark.anyio
async def test_research_ledger_missing_and_invalid_verdict():
    db = FakeDB()
    with pytest.raises(ValueError):
        await create_hypothesis(db, "user-1", {"hypothesis": ""})
    with pytest.raises(KeyError):
        await get_hypothesis(db, "user-1", "missing")

    created = await create_hypothesis(db, "user-1", {"hypothesis": "Testable idea"})
    with pytest.raises(ValueError):
        await set_verdict(db, "user-1", created["hypothesis_id"], {"status": "MAGIC"})

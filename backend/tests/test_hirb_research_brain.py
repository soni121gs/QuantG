import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.evidence_store import freeze_evidence, get_evidence, list_evidence
from core.research_agents import run_research_desk
from core.research_critic import critique_hypothesis
from routes.ai import _run_agent_tool, classify_playbook_by_query


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, key, direction=-1):
        if isinstance(key, list):
            field = key[0][0]
        else:
            field = key
        self.rows = sorted(self.rows, key=lambda row: row.get(field, ""), reverse=direction < 0)
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
        return SimpleNamespace(inserted_id=doc.get("evidence_snapshot_id"))

    async def find_one(self, query, projection=None):
        for row in self.rows:
            if self._matches(row, query):
                doc = dict(row)
                if projection and projection.get("_id") == 0:
                    doc.pop("_id", None)
                return doc
        return None

    def find(self, query, projection=None):
        rows = []
        for row in self.rows:
            if self._matches(row, query):
                doc = dict(row)
                if projection and projection.get("_id") == 0:
                    doc.pop("_id", None)
                rows.append(doc)
        return FakeCursor(rows)

    def _matches(self, row, query):
        for key, value in query.items():
            if isinstance(value, dict) and "$lte" in value:
                if row.get(key) > value["$lte"]:
                    return False
            elif row.get(key) != value:
                return False
        return True


class FakeDB:
    def __init__(self):
        self.frozen_evidence = FakeCollection()


def _validated_hypothesis():
    return {
        "hypothesis_id": "rh_1",
        "hypothesis": "IV-RV rich RANGE days support NIFTY defined-risk put spread selling.",
        "market_premise": "Short volatility earns premium when IV exceeds RV.",
        "entry_exit_idea": "Sell 3 percent OTM put spread and hold to expiry.",
        "null_hypothesis": "Cost-adjusted expectancy is <= 0.",
        "failure_modes": ["TREND_DOWN", "event_gap"],
        "evidence_links": [
            {
                "evidence_id": "ev_oos",
                "source": "edge_lab_oos",
                "summary": "OOS walk-forward passed",
                "sample_n": 106,
                "confidence": 0.82,
                "metrics": {"expectancy": 214, "slippage_stress": {"50": {"passes": True}}},
            },
            {
                "evidence_id": "ev_forward",
                "source": "forward_paper",
                "summary": "Forward-paper is directionally consistent",
                "sample_n": 31,
                "confidence": 0.7,
                "metrics": {"cost_model": "paper slippage enabled"},
            },
        ],
        "verdict": {
            "status": "CANDIDATE_EDGE",
            "confidence": 0.82,
            "sample_n": 106,
            "metrics": {"max_drawdown": 9000, "cvar_95": 4000, "slippage_stress": {"50": {"passes": True}}},
        },
    }


def test_research_critic_refuses_unproven_claims():
    critique = critique_hypothesis({
        "hypothesis_id": "rh_thin",
        "hypothesis": "A beautiful idea",
        "evidence_links": [],
        "verdict": {"status": "UNTESTED", "confidence": 0.0, "sample_n": 0},
    })

    assert critique["can_call_proven"] is False
    assert critique["allowed_claim"] == "not_proven"
    assert "no evidence links attached" in critique["blockers"]
    assert "out-of-sample or walk-forward evidence" in critique["missing_data"]


def test_research_critic_allows_only_validated_candidates():
    critique = critique_hypothesis(_validated_hypothesis())

    assert critique["can_call_proven"] is True
    assert critique["allowed_claim"] == "validated_research_candidate"
    assert critique["falsification_tests"]


@pytest.mark.anyio
async def test_frozen_evidence_store_dedupes_and_lists_as_of():
    db = FakeDB()
    first = await freeze_evidence(db, "user-1", {
        "source_type": "external_news",
        "source_uri": "https://example.test/rbi",
        "observed_at": "2026-07-09T09:00:00+00:00",
        "content": "RBI event schedule snapshot",
        "metadata": {"title": "RBI calendar"},
    })
    second = await freeze_evidence(db, "user-1", {
        "source_type": "external_news",
        "source_uri": "https://example.test/rbi",
        "observed_at": "2026-07-09T09:00:00+00:00",
        "content": "RBI event schedule snapshot",
        "metadata": {"title": "RBI calendar"},
    })
    await freeze_evidence(db, "user-1", {
        "source_type": "external_news",
        "observed_at": "2026-07-10T09:00:00+00:00",
        "content": "Future-updated snapshot",
    })

    assert second["deduped"] is True
    fetched = await get_evidence(db, "user-1", first["evidence_snapshot_id"])
    assert fetched["content_hash"] == first["content_hash"]
    assert fetched["anti_leakage"]["frozen"] is True

    rows = await list_evidence(db, "user-1", source_type="external_news", as_of="2026-07-09T23:59:00+00:00")
    assert len(rows) == 1
    assert rows[0]["content"] == "RBI event schedule snapshot"


def test_research_desk_outputs_specialist_roles():
    hypothesis = _validated_hypothesis()
    review = run_research_desk(
        hypothesis,
        evidence=hypothesis["evidence_links"],
        metrics=hypothesis["verdict"]["metrics"],
    )

    roles = {row["role"]: row for row in review["outputs"]}
    assert review["allowed_claim"] == "validated_research_candidate"
    assert roles["regime_analyst"]["status"] == "ok"
    assert roles["volatility_analyst"]["status"] == "ok"
    assert roles["execution_cost_auditor"]["status"] == "ok"
    assert roles["founder_briefing_agent"]["status"] == "ready"


def test_hirb_keywords_select_research_tools():
    tools = classify_playbook_by_query("Critique this hypothesis and tell me what is missing to prove it")
    assert "get_research_hypotheses" in tools
    assert "get_research_critique" in tools
    assert "get_research_desk" in tools


@pytest.mark.anyio
async def test_run_agent_tool_research_desk_envelope():
    mock_db = MagicMock()
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[_validated_hypothesis()])
    mock_db.research_hypotheses.find.return_value = cursor
    mock_db.agent_tool_audit.insert_one = AsyncMock()

    with patch("routes.ai.db", mock_db):
        res = await _run_agent_tool("get_research_desk", {"id": "user-1"}, query="review research desk")

    assert res["status"] == "ok"
    assert res["source"] == "db.research_hypotheses / core.hirb"
    assert res["data"]["desk_reviews"][0]["allowed_claim"] == "validated_research_candidate"

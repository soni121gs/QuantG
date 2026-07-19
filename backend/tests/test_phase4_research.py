import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.phase4_research import (
    build_hypothesis_card,
    calibration_summary,
    corpus_status,
    default_weekly_cards,
    paid_lane_status,
    participant_oi_extremes,
    persist_hypothesis_cards,
)


def test_phase4_research_corpus_has_required_breadth():
    status = corpus_status()
    assert status["acceptance"]["passed"] is True
    assert status["count"] >= 25


def test_hypothesis_cards_require_citations():
    payload = {
        "hypothesis": "Test idea",
        "who_pays": "Known market friction",
        "universe": "NIFTY options",
        "horizon": "weekly",
        "judge": "OOS validator",
        "data_needed": "bhavcopy",
        "kill_criteria": "expectancy <= 0",
    }
    with pytest.raises(ValueError, match="citation"):
        build_hypothesis_card(payload)

    card = build_hypothesis_card({**payload, "citations": [{"type": "corpus", "ref": "Cost Floor Law"}]})
    assert card["status"] == "PROPOSED"
    assert card["trial_status"] == "UNTESTED"


class FakeSyncCollection:
    def __init__(self):
        self.rows = {}

    def update_one(self, query, update, upsert=False):
        key = query["hypothesis_hash"]
        row = dict(update.get("$setOnInsert") or {})
        row.update(update.get("$set") or {})
        self.rows[key] = row
        return SimpleNamespace(upserted_id=key if upsert else None)


class FakeSyncDB:
    def __init__(self):
        self.research_hypotheses = FakeSyncCollection()


def test_default_cards_persist_and_calibrate():
    corpus = {"files": [{"title": "Cost Floor Law"}]}
    probes = {"rows": [{"probe": "vrp_by_strike"}]}
    cards = default_weekly_cards(probes, corpus)
    assert len(cards) == 2
    assert all(card["citations"] for card in cards)

    db = FakeSyncDB()
    assert persist_hypothesis_cards(db, "user-1", cards) == 2
    summary = calibration_summary(db.research_hypotheses.rows.values())
    assert summary["total_cards"] == 2
    assert summary["hit_rate"] == 0.0


def test_paid_lane_status_is_founder_gated(monkeypatch):
    monkeypatch.delenv("HERMES_RESEARCH_MODEL", raising=False)
    monkeypatch.delenv("PAID_RESEARCH_MODEL", raising=False)
    monkeypatch.setenv("HERMES_PAID_RESEARCH_ENABLED", "false")
    off = paid_lane_status()
    assert off["approved"] is False
    assert off["weekly_synthesis_uses_paid_lane"] is False

    monkeypatch.setenv("HERMES_PAID_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("HERMES_RESEARCH_MODEL", "gemini-pro")
    on = paid_lane_status()
    assert on["weekly_synthesis_uses_paid_lane"] is True


def test_participant_oi_extremes_uses_available_store(monkeypatch):
    monkeypatch.setattr("core.phase4_research.participant_oi_days", lambda: ["2024-01-01", "2024-01-02"])
    monkeypatch.setattr("core.phase4_research.get_participant_oi", lambda _day: {
        "rows": [
            {"participant": "FII", "future_index_long": 200, "future_index_short": 50},
            {"participant": "CLIENT", "future_index_long": 20, "future_index_short": 100},
        ]
    })
    result = participant_oi_extremes(start="2024-01-01", end="2024-01-02", top_n=1)
    assert result["status"] == "ready"
    assert result["sample_n"] == 2
    assert result["rows"][0]["bias"] == 170.0

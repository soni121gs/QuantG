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
    persist_research_signals,
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

    with pytest.raises(ValueError, match="unknown corpus"):
        build_hypothesis_card({**payload, "citations": [{"type": "corpus", "ref": "Made Up Source"}]})

    with pytest.raises(ValueError, match="invalid citation type"):
        build_hypothesis_card({**payload, "citations": [{"type": "blog", "ref": "Cost Floor Law"}]})


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
    corpus = {"files": [{"title": "IV Surface Richness"}, {"title": "Participant OI Caveats"}]}
    probes = {"rows": [
        {"probe": "vrp_by_strike", "status": "ready"},
        {"probe": "participant_oi_extremes", "status": "ready"},
    ]}
    cards = default_weekly_cards(probes, corpus)
    assert len(cards) == 2
    assert all(card["citations"] for card in cards)
    assert cards[0]["citations"] == [
        {"type": "corpus", "ref": "IV Surface Richness"},
        {"type": "probe", "ref": "vrp_by_strike"},
    ]
    assert cards[1]["citations"] == [
        {"type": "corpus", "ref": "Participant OI Caveats"},
        {"type": "probe", "ref": "participant_oi_extremes"},
    ]

    db = FakeSyncDB()
    assert persist_hypothesis_cards(db, "user-1", cards) == 2
    summary = calibration_summary(db.research_hypotheses.rows.values())
    assert summary["total_cards"] == 2
    assert summary["outcome_hit_rate"] == 0.0
    assert summary["calibration_type"] == "outcome_tally"
    assert summary["is_probability_calibration"] is False


def test_calibration_prefers_actual_verdict_over_stale_outcome():
    summary = calibration_summary([
        {
            "status": "draft",
            "trial_status": "CANDIDATE_EDGE",
            "calibration": {"outcome": "untested"},
        },
        {
            "status": "draft",
            "trial_status": "REJECTED",
            "calibration": {"outcome": "untested"},
        },
    ])
    assert summary["counts"] == {"validated": 1, "rejected": 1}
    assert summary["tested_cards"] == 2
    assert summary["outcome_hit_rate"] == 0.5


class FakeSignalCollection:
    def __init__(self):
        self.rows = {}

    def update_one(self, query, update, upsert=False):
        self.rows[query["_id"]] = dict(update["$set"])
        return SimpleNamespace(upserted_id=query["_id"] if upsert else None)


class FakeSignalDB:
    def __init__(self):
        self.research_signals = FakeSignalCollection()


def test_research_signals_are_user_scoped():
    db = FakeSignalDB()
    persist_research_signals(db, {
        "generated_at": "2026-07-19T00:00:00+00:00",
        "rows": [{"probe": "vrp_by_strike", "underlying": "NIFTY", "status": "ready"}],
    }, user_id="user-1")
    assert list(db.research_signals.rows) == ["user-1:vrp_by_strike:NIFTY"]
    assert db.research_signals.rows["user-1:vrp_by_strike:NIFTY"]["user_id"] == "user-1"


def test_root_dockerignore_excludes_nested_env_files():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    text = open(os.path.join(root, ".dockerignore"), encoding="utf-8").read()
    assert "**/.env" in text
    assert "**/.env.*" in text


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

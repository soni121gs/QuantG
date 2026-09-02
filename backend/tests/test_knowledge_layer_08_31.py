import pytest

from core.knowledge_layer import (
    build_daily_founder_brief,
    build_daily_learning_report,
    build_profit_giveback_lab,
    build_strategy_dossier,
    promotion_stage,
    search_wiki_knowledge,
    strategy_dossier_markdown,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, n):
        return self.rows[:n]

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self, rows=None, one=None):
        self.rows = rows or []
        self.one = one
        self.updated = []

    def find(self, *args, **kwargs):
        return FakeCursor(self.rows)

    async def find_one(self, *args, **kwargs):
        return self.one

    async def update_one(self, *args, **kwargs):
        self.updated.append((args, kwargs))

    async def create_index(self, *args, **kwargs):
        return "idx"


class FakeDB:
    def __init__(self):
        self.strategies = FakeCollection([
            {"id": "s1", "name": "QG Test Strategy", "status": "live", "enabled": True,
             "visual_config": {"options": {"structure": "credit_spread"}}}
        ], {"id": "s1", "name": "QG Test Strategy", "status": "live", "enabled": True})
        self.strategy_positions = FakeCollection([
            {"id": "p1", "strategy_id": "s1", "status": "CLOSED", "realized_pnl": 500,
             "peak_pnl": 700, "exit_reason": "spread-tp", "created_at": "2099-01-01T00:00:00+00:00",
             "closed_at": "2099-01-01T01:00:00+00:00"},
            {"id": "p-loss-1", "strategy_id": "s1", "status": "CLOSED", "realized_pnl": -300,
             "peak_pnl": 450, "exit_reason": "spread-sl", "created_at": "2099-01-01T00:00:00+00:00",
             "closed_at": "2099-01-01T01:30:00+00:00", "target_symbol": "NIFTY 24000/23800 PE CREDIT"},
            {"id": "p2", "strategy_id": "s1", "status": "OPEN", "unrealized_pnl": 100,
             "created_at": "2099-01-01T00:00:00+00:00"},
        ])
        self.trade_attribution = FakeCollection([
            {"trade_id": "t1", "strategy_id": "s1", "strategy_name": "QG Test Strategy",
             "date_ist": "2099-01-01", "realized_pnl": 500, "exit_reason": "spread-tp",
             "regime_at_entry": "RANGE", "structure": "credit_spread", "is_win": True}
        ])
        self.wiki_docs = FakeCollection([
            {"title": "QG Test Strategy", "topic": "Strategies", "tags": ["measured"],
             "content": "Measured dossier note", "links": [], "backlinks": []}
        ])
        self.edge_research_runs = FakeCollection([], {"strategy_id": "s1", "verdict": "CANDIDATE_EDGE"})
        self.hermes_hypothesis_tests = FakeCollection([])
        self.hermes_findings = FakeCollection([
            {"probe_id": "data.store_coverage", "domain": "data", "status": "open",
             "title": "earnings_dates (event calendar): stale", "suggested_fix": "refresh earnings"},
            {"probe_id": "exec.regime_organ_disagreement", "domain": "execution", "status": "open",
             "title": "Coarse and fine regime disagreed", "suggested_fix": "run regime replay"},
        ])
        self.research_hypotheses = FakeCollection([
            {"hypothesis_id": "rh_1", "hypothesis": "NIFTY premium selling only when IV rich",
             "status": "draft", "updated_at": "2099-01-01T00:00:00+00:00"}
        ])
        self.trades = FakeCollection([])
        self.daily_learning_reports = FakeCollection([])
        self.daily_founder_briefs = FakeCollection([])


def test_promotion_stage_blocks_thin_negative_strategy():
    out = promotion_stage("pause", "NO_EDGE_NEGATIVE", 24, -1200)
    assert out["stage"] == "paused_for_review"
    assert any("OOS verdict" in b for b in out["blockers"])
    assert any("governor label" in b for b in out["blockers"])


def test_promotion_stage_candidate_live_requires_clean_evidence():
    out = promotion_stage("scale_candidate", "CANDIDATE_EDGE", 42, 2500)
    assert out["stage"] == "candidate_live"
    assert out["blockers"] == []


@pytest.mark.asyncio
async def test_search_wiki_knowledge_marks_wiki_as_context():
    data = await search_wiki_knowledge(FakeDB(), "u1", "QG Test")
    assert data["kind"] == "wiki_knowledge_search"
    assert data["count"] == 1
    assert "Trading truth" in data["warning"]


@pytest.mark.asyncio
async def test_strategy_dossier_joins_governor_attribution_oos_and_wiki():
    data = await build_strategy_dossier(FakeDB(), "u1", "s1")
    assert data["kind"] == "strategy_dossier"
    assert data["strategy"]["name"] == "QG Test Strategy"
    assert data["governor"]["strategy_id"] == "s1"
    assert data["attribution"]["by_exit_reason"]["spread-tp"] == 1
    assert data["promotion"]["oos_status"] == "CANDIDATE_EDGE"
    md = strategy_dossier_markdown(data)
    assert "# QG Test Strategy" in md
    assert "[[QG Test Strategy]]" in md


@pytest.mark.asyncio
async def test_daily_learning_report_is_read_only_and_persistable():
    db = FakeDB()
    data = await build_daily_learning_report(db, "u1", date="2099-01-01", persist=True)
    assert data["kind"] == "daily_learning_report"
    assert data["realized_pnl"] == 500
    assert data["closed_trades"] == 1
    assert data["governor_summary"]
    assert db.daily_learning_reports.updated


@pytest.mark.asyncio
async def test_profit_giveback_lab_ranks_green_then_red_leaks():
    data = await build_profit_giveback_lab(FakeDB(), "u1", days=30)
    assert data["kind"] == "profit_giveback_lab"
    assert data["summary"]["green_then_loss"] == 1
    assert data["summary"]["loss_after_peak"] == 750
    assert data["by_strategy"][0]["strategy_id"] == "s1"
    assert "exit replay" in data["next_action"].lower()


@pytest.mark.asyncio
async def test_daily_founder_brief_combines_actions_and_persists():
    db = FakeDB()
    data = await build_daily_founder_brief(db, "u1", date="2099-01-01", persist=True)
    assert data["kind"] == "daily_founder_brief"
    themes = {row["theme"] for row in data["recommended_actions"]}
    assert "profit_giveback" in themes
    assert "data_freshness" in themes
    assert "regime_disagreement" in themes
    assert data["research_hypotheses"][0]["hypothesis_id"] == "rh_1"
    assert db.daily_founder_briefs.updated

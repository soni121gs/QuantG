"""Tests for the research bridge — OOS verdicts and Diagnostician findings
auto-populating the Research Ledger on ONE per-strategy edge hypothesis."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import research_bridge as rb


class _Coll:
    def __init__(self): self.docs = []
    async def create_index(self, *a, **k): return None
    def find(self, q=None, proj=None):
        docs = [d for d in self.docs if _match(d, q or {})]
        class _C:
            async def to_list(self, n=None): return list(docs[: n or len(docs)])
            def __aiter__(self):
                self._i = 0; return self
            async def __anext__(self):
                if self._i >= len(docs): raise StopAsyncIteration
                d = docs[self._i]; self._i += 1; return d
        return _C()
    async def find_one(self, q, proj=None):
        for d in self.docs:
            if _match(d, q): return d
        return None
    async def update_one(self, q, update, upsert=False):
        # Mirror Mongo: the same field path may not appear in $setOnInsert and $push.
        clash = set((update.get("$setOnInsert") or {})) & set((update.get("$push") or {}))
        if clash:
            raise RuntimeError(f"conflict at {clash} ($setOnInsert + $push)")
        target = next((d for d in self.docs if _match(d, q)), None)
        if target is None:
            if not upsert: return
            target = dict(q); self.docs.append(target)
            for k, v in (update.get("$setOnInsert") or {}).items():
                target[k] = v
        for k, v in (update.get("$set") or {}).items():
            target[k] = v
        for k, v in (update.get("$push") or {}).items():
            target.setdefault(k, []).append(v)


def _match(doc, q):
    for k, v in q.items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v): return False
        elif doc.get(k) != v:
            return False
    return True


class _DB:
    def __init__(self): self._c = {}
    def __getattr__(self, n):
        if n.startswith("_"): raise AttributeError(n)
        return self._c.setdefault(n, _Coll())


@pytest.mark.asyncio
async def test_oos_verdict_creates_and_updates_one_edge_hypothesis():
    db = _DB()
    strat = {"id": "s1", "name": "NIFTY Theta", "visual_config": {"options": {
        "underlying": "NIFTY", "structure": "credit_spread"}}}
    # First OOS run: CANDIDATE_EDGE → validated.
    hid = await rb.record_oos_result(db, "u1", strat,
        {"verdict": "CANDIDATE_EDGE", "oos": 112, "pct_green_months": 74, "n": 36})
    row = db.research_hypotheses.docs[0]
    assert row["hypothesis_id"] == hid and row["status"] == "validated"
    assert row["verdict"]["status"] == "CANDIDATE_EDGE"
    assert len(row["evidence_links"]) == 1

    # Second run on the SAME strategy flips to NO_EDGE_NEGATIVE → rejected, same row.
    await rb.record_oos_result(db, "u1", strat,
        {"verdict": "NO_EDGE_NEGATIVE", "oos": -145, "pct_green_months": 30, "n": 50})
    assert len(db.research_hypotheses.docs) == 1           # no duplicate row
    row = db.research_hypotheses.docs[0]
    assert row["status"] == "rejected"
    assert len(row["evidence_links"]) == 2                 # evidence accrues


@pytest.mark.asyncio
async def test_finding_and_oos_converge_on_same_row():
    db = _DB()
    db.strategies.docs.append({"id": "s1", "name": "Range Seller",
        "visual_config": {"options": {"underlying": "BANKNIFTY", "structure": "credit_spread"}}})
    finding = {"probe_id": "strategy.persistent_live_loss", "entity": "s1",
               "title": "Net-negative over 20 trades", "evidence": {
                   "strategy_id": "s1", "name": "Range Seller", "trades": 20,
                   "realized_pnl": -6800}}
    n = await rb.sync_findings_to_research(db, "u1", [finding])
    assert n == 1
    hid_finding = db.research_hypotheses.docs[0]["hypothesis_id"]

    # An OOS verdict for the SAME strategy must land on the SAME hypothesis row.
    await rb.record_oos_result(db, "u1",
        {"id": "s1", "name": "Range Seller", "visual_config": {"options": {
            "underlying": "BANKNIFTY", "structure": "credit_spread"}}},
        {"verdict": "NO_EDGE_NEGATIVE", "oos": -200, "pct_green_months": 20, "n": 40})
    assert len(db.research_hypotheses.docs) == 1
    row = db.research_hypotheses.docs[0]
    assert row["hypothesis_id"] == hid_finding
    assert row["status"] == "rejected" and row["verdict"]["status"] == "NO_EDGE_NEGATIVE"
    assert len(row["evidence_links"]) == 2   # live-loss evidence + OOS evidence


@pytest.mark.asyncio
async def test_bug_findings_are_not_researched():
    db = _DB()
    findings = [{"probe_id": "exec.intent_vs_execution_side", "entity": "s1",
                 "evidence": {"strategy_id": "s1"}},
                {"probe_id": "infra.feed_regime_artifact", "entity": "NIFTY", "evidence": {}}]
    n = await rb.sync_findings_to_research(db, "u1", findings)
    assert n == 0 and db.research_hypotheses.docs == []

"""Tests for auto-OOS judge selection, bounded orchestration, and the data-
coverage probe. The OOS engines themselves need the real stores, so here we test
routing/orchestration and the coverage flags (which are the data-truth guards)."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hermes_diagnostics import auto_oos
from core.hermes_diagnostics.probe_sdk import ProbeContext
from core.hermes_diagnostics import probes_data


def _strat(**opts):
    risk = opts.pop("_risk", {})
    return {"id": opts.pop("_id", "s1"), "name": "S",
            "visual_config": {"options": opts, "risk": risk}}


def test_judge_selection():
    assert auto_oos._is_intraday(_strat(exit_mode="expiry")) is False
    assert auto_oos._is_intraday(_strat(structure="credit_spread", exit_mode="")) is True
    assert auto_oos._is_intraday(_strat(structure="single_leg", _risk={"time_exit_minutes": 45})) is True
    assert auto_oos._is_intraday(_strat(structure="single_leg", candle_interval="1minute")) is True
    assert auto_oos._is_intraday(_strat(structure="single_leg")) is False


class _Coll:
    def __init__(self, docs): self.docs = docs
    async def find_one(self, q, proj=None):
        ids = []
        for sub in q.get("$or", [q]):
            ids += list(sub.values())
        for d in self.docs:
            if d.get("id") in ids: return d
        return None


class _DB:
    def __init__(self, strategies): self.strategies = _Coll(strategies)


@pytest.mark.asyncio
async def test_auto_oos_bounded_and_edge_only(monkeypatch):
    calls = []
    async def _stub(db, uid, strat):
        calls.append(strat["id"]); return {"verdict": "NO_EDGE_NEGATIVE"}
    monkeypatch.setattr(auto_oos, "attach_auto_oos", _stub)
    monkeypatch.setattr(auto_oos, "_MAX", 2)
    monkeypatch.setattr(auto_oos, "_ENABLED", True)

    db = _DB([{"id": f"s{i}"} for i in range(5)])
    findings = (
        [{"probe_id": "strategy.persistent_live_loss", "entity": f"s{i}",
          "evidence": {"strategy_id": f"s{i}"}} for i in range(4)]
        + [{"probe_id": "exec.intent_vs_execution_side", "entity": "s9",
            "evidence": {"strategy_id": "s9"}}]  # bug finding, not graded
    )
    res = await auto_oos.run_auto_oos_for_findings(db, "u1", findings)
    assert res["graded"] == 2                 # bounded to _MAX
    assert "s9" not in calls                   # bug finding not OOS-graded


@pytest.mark.asyncio
async def test_auto_oos_disabled(monkeypatch):
    monkeypatch.setattr(auto_oos, "_ENABLED", False)
    res = await auto_oos.run_auto_oos_for_findings(_DB([]), "u1", [])
    assert res == {"enabled": False, "graded": 0}


# ── data-coverage probe ──────────────────────────────────────────────────────

def _ctx():
    return ProbeContext(db=None, user_id="u1", date_str="2026-07-18")


def _fake_store(days):
    class _S:
        def trading_days(self, *a, **k): return list(days)
    return _S


@pytest.mark.asyncio
async def test_store_coverage_flags_empty_and_stale(monkeypatch):
    import core.bhavcopy_store as bs
    import core.index_minute_store as ims
    import core.options_minute_store as oms
    monkeypatch.setattr(bs, "BhavcopyStore", _fake_store([]))                       # empty
    monkeypatch.setattr(ims, "IndexMinuteStore", _fake_store(["2026-07-01", "2026-07-17"]))  # fresh
    monkeypatch.setattr(oms, "OptionsMinuteStore", _fake_store(["2025-06-01", "2025-06-25"]))  # stale

    out = await probes_data.store_coverage(_ctx())
    titles = {f.title.split(":")[0]: f for f in out}
    empties = [f for f in out if "EMPTY" in f.title]
    stales = [f for f in out if "stale" in f.title]
    assert any("bhavcopy" in f.entity for f in empties)   # empty flagged HIGH
    assert empties[0].severity == "high"
    assert any("options_1m" in f.entity for f in stales)  # stale flagged
    # index store is fresh (last day 2026-07-17 vs 07-18) → no finding for it
    assert not any("index_1m" in f.entity for f in out)

"""Tests for the Hermes Diagnostician.

Two tiers:
  1. Direct probe tests — build a ProbeContext by hand, assert the RC-1/RC-2/RC-3
     detection logic (no DB).
  2. Runner lifecycle — a minimal in-memory async DB, assert persist + auto-resolve.
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hermes_diagnostics.probe_sdk import ProbeContext
from core.hermes_diagnostics.probes_execution import (
    intent_vs_execution_side, exit_reason_mix, no_op_stop, specialist_regime_fit,
)
from core.hermes_diagnostics.probes_infra import feed_regime_artifact
from core.hermes_diagnostics.probes_static import reward_risk_geometry
from core.hermes_diagnostics import runner


def _ctx(**kw):
    base = dict(db=None, user_id="u1", date_str="2026-07-17",
                strategies=[], closed_today=[], open_positions=[],
                signals_today=[], daily_report=None, now_ist_hm="16:00",
                in_market_hours=False)
    base.update(kw)
    return ProbeContext(**base)


# ── Tier 1: probe correctness ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rc1_side_inversion_detected():
    # Signal action BUY (bullish → expect short PE) but the built spread is short CE.
    sig = {"id": "sig1", "action": "BUY", "entry_reason": "sell OTM put spread"}
    pos = {"id": "pos1", "strategy_id": "rae-bn", "symbol": "BANKNIFTY",
           "structure": "credit_spread", "signal_id": "sig1",
           "realized_pnl": -3810.0,
           "legs": [{"role": "short", "side": "SELL", "option_type": "CE", "strike": 58600},
                    {"role": "long", "side": "BUY", "option_type": "CE", "strike": 59200}]}
    out = await intent_vs_execution_side(_ctx(closed_today=[pos], signals_today=[sig]))
    assert len(out) == 1 and out[0].severity == "critical"
    assert out[0].evidence["expected_short"] == "PE"
    assert out[0].evidence["actual_short"] == "CE"


@pytest.mark.asyncio
async def test_rc1_correct_side_is_silent():
    sig = {"id": "s", "action": "BUY"}
    pos = {"id": "p", "strategy_id": "x", "structure": "credit_spread", "signal_id": "s",
           "legs": [{"role": "short", "side": "SELL", "option_type": "PE"}]}
    assert await intent_vs_execution_side(_ctx(closed_today=[pos], signals_today=[sig])) == []


@pytest.mark.asyncio
async def test_rc2_no_op_stop_detected():
    # sl_value == width (net_credit+max_loss) → cosmetic stop.
    pos = {"id": "p", "strategy_id": "s1", "structure": "credit_spread",
           "net_credit": 172.0, "max_loss": 428.0, "spread_sl_value": 600.0}
    out = await no_op_stop(_ctx(closed_today=[pos]))
    assert len(out) == 1 and out[0].severity == "high"
    assert out[0].evidence["width"] == 600.0


@pytest.mark.asyncio
async def test_rc2_reachable_stop_is_silent():
    pos = {"id": "p", "strategy_id": "s1", "structure": "credit_spread",
           "net_credit": 172.0, "max_loss": 428.0, "spread_sl_value": 540.0}  # 0.9*600
    assert await no_op_stop(_ctx(closed_today=[pos])) == []


@pytest.mark.asyncio
async def test_rc3_exit_mix_zero_price_exits():
    spreads = [{"structure": "credit_spread", "exit_reason": r} for r in
               ["spread-time-exit", "intraday-squareoff-1525", "spread-time-exit",
                "eod-square-off", "intraday-squareoff-1525"]]
    out = await exit_reason_mix(_ctx(closed_today=spreads))
    assert len(out) == 1 and out[0].severity == "critical"
    assert out[0].evidence["price_exits"] == 0


@pytest.mark.asyncio
async def test_exit_mix_with_price_exit_is_silent():
    spreads = [{"structure": "credit_spread", "exit_reason": r} for r in
               ["spread-tp", "spread-time-exit", "intraday-squareoff-1525", "spread-sl"]]
    assert await exit_reason_mix(_ctx(closed_today=spreads)) == []


@pytest.mark.asyncio
async def test_specialist_off_regime_detected():
    strat = {"id": "rae-bn", "owned_regimes": ["RANGE", "INSIDE_QUIET"]}
    pos = {"strategy_id": "rae-bn", "regime_at_entry": "TREND_UP", "symbol": "BANKNIFTY"}
    out = await specialist_regime_fit(_ctx(strategies=[strat], closed_today=[pos]))
    assert len(out) == 1 and out[0].evidence["entry_regime"] == "TREND_UP"


@pytest.mark.asyncio
async def test_feed_regime_artifact_detected():
    sigs = [{"symbol": "NIFTY",
             "regime_snapshot": {"index": "NIFTY", "intraday_return_pct": -57.8,
                                 "regime": "CRASH", "computed_at": "2026-07-17T08:43"}}]
    out = await feed_regime_artifact(_ctx(signals_today=sigs))
    assert len(out) == 1 and out[0].severity == "high"
    assert out[0].evidence["intraday_return_pct"] == -57.8


@pytest.mark.asyncio
async def test_reward_risk_geometry_flagged():
    # tp 0.35, sl 1.5 → break-even WR = 1.5/1.85 ≈ 81% ≥ 75% warn.
    strat = {"id": "s1", "name": "Range Seller", "status": "live",
             "visual_config": {"options": {"structure": "credit_spread",
                                           "credit_tp_frac": 0.35, "credit_sl_mult": 1.5}}}
    out = await reward_risk_geometry(_ctx(strategies=[strat]))
    assert len(out) == 1 and out[0].evidence["breakeven_win_rate"] >= 0.75


@pytest.mark.asyncio
async def test_reward_risk_geometry_sane_is_silent():
    strat = {"id": "s1", "status": "live",
             "visual_config": {"options": {"structure": "credit_spread",
                                           "credit_tp_frac": 0.5, "credit_sl_mult": 0.8}}}
    assert await reward_risk_geometry(_ctx(strategies=[strat])) == []


# ── Tier 2: runner lifecycle with a minimal fake async DB ────────────────────

class _Cursor:
    def __init__(self, docs): self._docs = docs
    async def to_list(self, n=None): return list(self._docs[: n or len(self._docs)])
    def __aiter__(self):
        self._i = 0
        return self
    async def __anext__(self):
        if self._i >= len(self._docs):
            raise StopAsyncIteration
        d = self._docs[self._i]; self._i += 1
        return d


def _match(doc, q):
    for k, v in q.items():
        if k in ("$or", "$and"):
            continue
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif isinstance(v, dict) and "$regex" in v:
            if not str(doc.get(k, "")).startswith(v["$regex"].lstrip("^")):
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Coll:
    def __init__(self): self.docs = []
    def find(self, q=None, proj=None): return _Cursor([d for d in self.docs if _match(d, q or {})])
    async def find_one(self, q, proj=None):
        for d in self.docs:
            if _match(d, q):
                return d
        return None
    async def count_documents(self, q): return len([d for d in self.docs if _match(d, q)])
    async def update_one(self, q, update, upsert=False):
        target = None
        for d in self.docs:
            if _match(d, q):
                target = d; break
        if target is None:
            if not upsert:
                return
            target = dict(q); self.docs.append(target)
            for k, v in (update.get("$setOnInsert") or {}).items():
                target[k] = v
        for k, v in (update.get("$set") or {}).items():
            target[k] = v
        for k, v in (update.get("$inc") or {}).items():
            target[k] = target.get(k, 0) + v


class _DB:
    def __init__(self): self._c = {}
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())


@pytest.mark.asyncio
async def test_runner_persists_and_auto_resolves():
    db = _DB()
    bad = {"id": "s1", "name": "Bad Geo", "status": "live",
           "visual_config": {"options": {"structure": "credit_spread",
                                         "credit_tp_frac": 0.35, "credit_sl_mult": 1.5}}}
    db.strategies.docs.append(bad)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)

    res = await runner.run_diagnostics(db, "u1", "2026-07-17", kinds=["static"], now=now)
    keys = [f["key"] for f in res["findings"]]
    assert any("reward_risk_geometry" in k for k in keys)
    stored = [d for d in db.hermes_findings.docs if d["status"] == "open"]
    assert len(stored) >= 1

    # A high-severity finding was auto-filed as a fix-task.
    tasks = [t for t in db.hermes_fix_tasks.docs if "reward_risk_geometry" in t["key"]]
    assert len(tasks) == 1 and tasks[0]["status"] == "open"

    # "Fix" the geometry → re-run → the finding auto-resolves AND the task auto-closes.
    bad["visual_config"]["options"]["credit_sl_mult"] = 0.8
    await runner.run_diagnostics(db, "u1", "2026-07-18", kinds=["static"], now=now)
    geo = [d for d in db.hermes_findings.docs if "reward_risk_geometry" in d["key"]][0]
    assert geo["status"] == "resolved"
    task = [t for t in db.hermes_fix_tasks.docs if "reward_risk_geometry" in t["key"]][0]
    assert task["status"] == "auto_closed"

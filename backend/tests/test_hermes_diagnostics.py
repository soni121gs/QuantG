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
from core.hermes_diagnostics.contract import Severity
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


# ── 2026-07-22: geometry-change epoch split on persistent_live_loss ──────────

class _FakeAgg:
    """Minimal aggregate() stand-in returning canned group rows."""
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, n):
        return self._rows


class _FakeDB:
    def __init__(self, group_rows, split_rows):
        self._group, self._split = group_rows, split_rows
        self.strategy_positions = self

    def aggregate(self, pipeline):
        # the split query pins a single strategy_id in its $match
        pinned = any("strategy_id" in (st.get("$match") or {}) for st in pipeline)
        return _FakeAgg(self._split if pinned else self._group)


def _loss_ctx(strat, split_rows):
    return _ctx(db=_FakeDB([{"_id": "s1", "pnl": -7720.0, "trades": 35, "wins": 12}],
                           split_rows),
                strategies=[strat])


@pytest.mark.asyncio
async def test_persistent_loss_without_epoch_keeps_plain_verdict():
    from core.hermes_diagnostics.probes_strategy import persistent_live_loss
    out = await persistent_live_loss(_loss_ctx({"id": "s1", "name": "X"}, []))
    assert len(out) == 1
    assert "geometry_changed_at" not in out[0].evidence
    assert "SAMPLE SPLIT" not in out[0].detail


@pytest.mark.asyncio
async def test_epoch_split_reports_both_sides_but_never_resolves():
    """A re-cut must add context, not silence the finding — otherwise changing a
    parameter becomes a way to launder a losing strategy."""
    from core.hermes_diagnostics.probes_strategy import persistent_live_loss
    strat = {"id": "s1", "name": "X",
             "geometry_changed_at": "2026-07-22T00:00:00+00:00",
             "geometry_change_note": "reachability guard"}
    split = [{"_id": False, "pnl": -7720.0, "trades": 35},
             {"_id": True, "pnl": 0.0, "trades": 0}]
    out = await persistent_live_loss(_loss_ctx(strat, split))
    assert len(out) == 1                      # still fires
    f = out[0]
    assert f.severity == Severity.MEDIUM      # not downgraded
    assert f.evidence["trades_since_change"] == 0
    assert f.evidence["trades_before_change"] == 35
    assert f.evidence["realized_pnl"] == -7720.0   # blended number still present
    assert "too thin to judge" in f.detail


@pytest.mark.asyncio
async def test_epoch_split_flags_a_mature_post_change_sample():
    from core.hermes_diagnostics.probes_strategy import persistent_live_loss
    strat = {"id": "s1", "name": "X",
             "geometry_changed_at": "2026-07-22T00:00:00+00:00"}
    split = [{"_id": False, "pnl": -6000.0, "trades": 15},
             {"_id": True, "pnl": -1720.0, "trades": 20}]
    out = await persistent_live_loss(_loss_ctx(strat, split))
    assert "large enough to judge on its own" in out[0].detail


# ── 2026-07-22: cost floor must measure BANKABLE profit, not gross credit ────

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    def __aiter__(self):
        async def gen():
            for r in self._rows:
                yield r
        return gen()


class _CreditDB:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None
        self.strategy_positions = self

    def find(self, q, proj=None):
        self.last_query = q
        return _FakeCursor(self._rows)


@pytest.mark.asyncio
async def test_cost_floor_scales_realized_credit_by_take_profit():
    """A seller booking 45% of credit banks 45% of it. Measuring gross made the
    probe ~1/tp_frac too permissive (§21.1 defect class)."""
    from core.hermes_diagnostics.probes_static import _realized_credit_per_lot
    strat = {"id": "s1", "visual_config": {"options": {
        "structure": "credit_spread", "underlying": "NIFTY", "credit_tp_frac": 0.45}}}
    db = _CreditDB([{"net_credit": 18.97}] * 5)
    bankable, n = await _realized_credit_per_lot(db, "u1", strat)
    assert n == 5
    assert round(bankable) == round(18.97 * 65 * 0.45)   # 555, not the gross 1233


@pytest.mark.asyncio
async def test_cost_floor_only_counts_the_current_geometry_epoch():
    from core.hermes_diagnostics.probes_static import _realized_credit_per_lot
    strat = {"id": "s1", "geometry_changed_at": "2026-07-22T00:00:00+00:00",
             "visual_config": {"options": {"structure": "credit_spread",
                                           "underlying": "NIFTY", "credit_tp_frac": 0.45}}}
    db = _CreditDB([{"net_credit": 20.0}] * 4)
    await _realized_credit_per_lot(db, "u1", strat)
    assert db.last_query["created_at"] == {"$gte": "2026-07-22T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_cost_floor_stays_silent_on_thin_post_recut_evidence():
    from core.hermes_diagnostics.probes_static import _realized_credit_per_lot
    strat = {"id": "s1", "visual_config": {"options": {
        "structure": "credit_spread", "underlying": "NIFTY", "credit_tp_frac": 0.45}}}
    assert await _realized_credit_per_lot(_CreditDB([{"net_credit": 20.0}] * 2), "u1", strat) is None


@pytest.mark.asyncio
async def test_debit_spread_max_profit_is_not_scaled_again():
    from core.hermes_diagnostics.probes_static import _realized_credit_per_lot
    strat = {"id": "s1", "visual_config": {"options": {
        "structure": "debit_spread", "underlying": "NIFTY", "credit_tp_frac": 0.45}}}
    bankable, _ = await _realized_credit_per_lot(_CreditDB([{"max_profit": 10.0}] * 5), "u1", strat)
    assert round(bankable) == 650   # 10 x 65, no tp scaling


# --- 2026-07-23: structure mismatch (spread strategy → naked single leg) -----

@pytest.mark.asyncio
async def test_structure_mismatch_flags_spread_strategy_holding_single_leg():
    """The 7-naked-buys bug: a credit-spread SELLER opened single-leg option buys
    when the spread build was vetoed. Must be CRITICAL."""
    from core.hermes_diagnostics.probes_execution import structure_mismatch
    strat = {"id": "s1", "name": "QG Credit",
             "visual_config": {"options": {"structure": "credit_spread"}}}
    naked = [{"strategy_id": "s1", "structure": "single_leg", "target_symbol": "NIFTY 24000 CE"},
             {"strategy_id": "s1", "structure": "single_leg", "target_symbol": "NIFTY 24050 CE"}]
    out = await structure_mismatch(_ctx(strategies=[strat], open_positions=naked))
    assert len(out) == 1
    assert out[0].severity == Severity.CRITICAL
    assert out[0].evidence["naked_single_leg_count"] == 2


@pytest.mark.asyncio
async def test_structure_mismatch_silent_when_spread_holds_a_spread():
    from core.hermes_diagnostics.probes_execution import structure_mismatch
    strat = {"id": "s1", "name": "QG Credit",
             "visual_config": {"options": {"structure": "credit_spread"}}}
    ok = [{"strategy_id": "s1", "structure": "credit_spread", "target_symbol": "NIFTY spread"}]
    assert await structure_mismatch(_ctx(strategies=[strat], closed_today=ok)) == []


@pytest.mark.asyncio
async def test_structure_mismatch_ignores_single_leg_declared_strategies():
    """A trend delta-1 buyer legitimately holds single legs — must not fire."""
    from core.hermes_diagnostics.probes_execution import structure_mismatch
    strat = {"id": "s2", "name": "Trend",
             "visual_config": {"options": {"structure": "single_leg"}}}
    pos = [{"strategy_id": "s2", "structure": "single_leg", "target_symbol": "NIFTY 24000 CE"}]
    assert await structure_mismatch(_ctx(strategies=[strat], open_positions=pos)) == []


# --- 2026-07-23: unexplained-skip observability guard ------------------------

@pytest.mark.asyncio
async def test_unexplained_skips_flags_blank_reason():
    from core.hermes_diagnostics.probes_execution import unexplained_skips
    sigs = [{"strategy_id": "s1", "status": "SKIPPED_SIGNAL"} for _ in range(4)]  # no reason
    out = await unexplained_skips(_ctx(strategies=[{"id": "s1", "name": "X"}], signals_today=sigs))
    assert len(out) == 1 and out[0].severity == Severity.HIGH
    assert out[0].evidence["unexplained_skips"] == 4


@pytest.mark.asyncio
async def test_unexplained_skips_silent_when_reason_recorded():
    from core.hermes_diagnostics.probes_execution import unexplained_skips
    sigs = [{"strategy_id": "s1", "status": "SKIPPED_SIGNAL", "rejection_reason": "RES2_GATE_BLOCKED",
             "rejection_detail": {"human_reason": "IV not rich"}} for _ in range(5)]
    assert await unexplained_skips(_ctx(strategies=[{"id": "s1"}], signals_today=sigs)) == []

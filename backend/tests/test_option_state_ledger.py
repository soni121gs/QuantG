from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from option_state_ledger import OptionStateLedger


def test_duplicate_buy_is_dropped_atomically(tmp_path):
    ledger = OptionStateLedger(tmp_path / "runtime.sqlite3", pool_size=4)
    ledger.initialize()
    ledger.upsert_strategy_state("s1", target_pct=0.25, stoploss_pct=0.10)

    def buy_once():
        return ledger.try_open_position(
            strategy_id="s1",
            symbol="NIFTY24MAY22000CE",
            option_type="CE",
            entry_price=100.0,
            quantity=1,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        decisions = list(executor.map(lambda _: buy_once(), range(8)))

    accepted = [d for d in decisions if d.accepted]
    rejected = [d for d in decisions if not d.accepted]
    assert len(accepted) == 1
    assert len(rejected) == 7
    assert {d.reason for d in rejected} == {"pending-entry-exists"}
    snapshot = ledger.snapshot()["s1"]
    assert snapshot["state"] == "OPEN"
    assert snapshot["max_lots"] == 1
    assert snapshot["active_position"]["symbol"] == "NIFTY24MAY22000CE"
    assert snapshot["active_position"]["status"] == "PENDING"
    assert ledger.confirm_position_fill(strategy_id="s1", entry_price=100.0, quantity=1).accepted
    assert ledger.snapshot()["s1"]["active_position"]["status"] == "FILLED"
    ledger.close()


def test_close_moves_to_cooldown_and_journals_trade(tmp_path):
    ledger = OptionStateLedger(tmp_path / "runtime.sqlite3", pool_size=2)
    ledger.initialize()
    ledger.upsert_strategy_state("s1", cooldown_minutes=5)
    assert ledger.try_open_position(
        strategy_id="s1",
        symbol="SENSEX24MAY74000PE",
        option_type="PE",
        entry_price=200.0,
        quantity=1,
        broker_confirmed=True,
    ).accepted

    decision = ledger.close_position("s1", exit_price=230.0, exit_reason="target-hit")
    assert decision.accepted
    snapshot = ledger.snapshot()["s1"]
    assert snapshot["state"] == "COOLDOWN"
    assert snapshot["cooldown_until"]
    assert snapshot["active_position"] is None
    ledger.close()


def test_adaptive_micro_scalp_brackets_are_tight():
    target, stop, trail = OptionStateLedger._brackets(
        100.0,
        target_pct=0.08,
        stoploss_pct=0.055,
        trailing_enabled=True,
        trail_trigger_pct=0.035,
        trail_step_pct=0.02,
        position_side="LONG",
        risk_style="micro_scalp",
        adaptive_enabled=True,
        target_r_multiple=1.25,
    )

    assert target == 107.42
    assert stop == 94.5
    assert trail == 101.5


def test_adaptive_volatile_breakout_allows_more_room():
    target, stop, trail = OptionStateLedger._brackets(
        100.0,
        target_pct=0.17,
        stoploss_pct=0.10,
        trailing_enabled=True,
        trail_trigger_pct=0.075,
        trail_step_pct=0.042,
        position_side="LONG",
        risk_style="volatile_breakout",
        adaptive_enabled=True,
        target_r_multiple=1.60,
    )

    assert target == 117.0
    assert stop == 90.0
    assert trail == 103.3

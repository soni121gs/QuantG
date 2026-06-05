from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk_controls import SizeInputs, compute_position_size, evaluate_market_data_quality


def test_position_size_reduces_to_lot_multiple_under_risk_caps():
    result = compute_position_size(SizeInputs(
        equity=25_000,
        free_margin=20_000,
        requested_qty=150,
        lot_size=75,
        entry_price=100,
        stop_loss_price=94.5,
        max_position_value=50_000,
        daily_loss_limit=2_000,
        risk_style="micro_scalp",
    ))

    assert result.allowed
    assert result.quantity == 75
    assert result.caps["risk"] == 75


def test_position_size_blocks_when_less_than_one_lot_allowed():
    result = compute_position_size(SizeInputs(
        equity=5_000,
        free_margin=5_000,
        requested_qty=75,
        lot_size=75,
        entry_price=100,
        stop_loss_price=90,
        max_position_value=50_000,
        daily_loss_limit=500,
        risk_style="volatile_breakout",
    ))

    assert not result.allowed
    assert result.quantity == 0


def test_position_size_allows_one_option_lot_when_stop_risk_fits_daily_limit():
    result = compute_position_size(SizeInputs(
        equity=25_000,
        free_margin=500_000,
        requested_qty=65,
        lot_size=65,
        entry_price=126.1,
        stop_loss_price=119.16,
        max_position_value=50_000,
        daily_loss_limit=450,
        risk_style="micro_scalp",
    ))

    assert result.allowed
    assert result.quantity == 65
    assert result.order_value == 8196.5


def test_market_quality_blocks_stale_micro_scalp_tick():
    old_tick = datetime.now(timezone.utc) - timedelta(seconds=45)
    quality = evaluate_market_data_quality(
        ltp=100,
        tick_time=old_tick,
        received_at=old_tick,
        instrument_token="NSE_FO|1",
        risk_style="micro_scalp",
    )

    assert not quality["ok"]
    assert "stale" in quality["reason"]


def test_market_quality_blocks_wide_spread():
    quality = evaluate_market_data_quality(
        ltp=100,
        received_at=datetime.now(timezone.utc),
        instrument_token="NSE_FO|1",
        bid=99,
        ask=101,
        risk_style="momentum",
    )

    assert not quality["ok"]
    assert "spread" in quality["reason"]


def test_market_quality_accepts_fresh_tick():
    quality = evaluate_market_data_quality(
        ltp=100,
        tick_time=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        instrument_token="NSE_FO|1",
        bid=99.9,
        ask=100.1,
        risk_style="balanced",
    )

    assert quality["ok"]


def test_mcx_quote_with_timestamp_passes():
    now = datetime.now(timezone.utc)
    quality = evaluate_market_data_quality(
        ltp=6500,
        tick_time=now,
        received_at=now,
        instrument_token="MCX_FO|123",
        exchange="MCX",
        market_open=True,
        risk_style="balanced",
    )

    assert quality["ok"]


def test_mcx_quote_without_broker_timestamp_uses_received_at():
    now = datetime.now(timezone.utc)
    quality = evaluate_market_data_quality(
        ltp=6500,
        tick_time=None,
        received_at=now,
        instrument_token="MCX_FO|123",
        exchange="MCX",
        market_open=True,
        risk_style="balanced",
    )

    assert quality["ok"]


def test_mcx_stale_quote_blocks():
    old_tick = datetime.now(timezone.utc) - timedelta(seconds=180)
    quality = evaluate_market_data_quality(
        ltp=6500,
        tick_time=old_tick,
        received_at=old_tick,
        instrument_token="MCX_FO|123",
        exchange="MCX",
        market_open=True,
        risk_style="balanced",
    )

    assert not quality["ok"]
    assert "stale" in quality["reason"]


def test_mcx_missing_token_blocks():
    now = datetime.now(timezone.utc)
    quality = evaluate_market_data_quality(
        ltp=6500,
        tick_time=now,
        received_at=now,
        instrument_token="",
        exchange="MCX",
        market_open=True,
    )

    assert not quality["ok"]
    assert quality["reason"] == "instrument token unresolved"


def test_mcx_market_closed_blocks_with_clear_message():
    now = datetime.now(timezone.utc)
    quality = evaluate_market_data_quality(
        ltp=6500,
        tick_time=now,
        received_at=now,
        instrument_token="MCX_FO|123",
        exchange="MCX",
        market_open=False,
    )

    assert not quality["ok"]
    assert quality["reason"] == "MCX market is closed"

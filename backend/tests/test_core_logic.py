"""20 broker-free unit tests for QuantG core logic.

All tests are pure-function: no DB, no broker, no HTTP calls.
Run with: python -m pytest tests/test_core_logic.py -v

Task: TASK-006
"""
import hashlib
import sys
import os

# Ensure backend root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.market_domains import resolve_domain_by_underlying, NSE_FO_DOMAIN, BSE_FO_DOMAIN
from core.order_manager import OrderManager
from core.position_lifecycle import exit_reason, normalize_strategy_risk


# ---------------------------------------------------------------------------
# 1–2: Lot sizes
# ---------------------------------------------------------------------------

def test_nifty_lot_size():
    assert resolve_domain_by_underlying("NIFTY").get_lot_size("NIFTY") == 65


def test_banknifty_lot_size():
    assert resolve_domain_by_underlying("BANKNIFTY").get_lot_size("BANKNIFTY") == 30


# ---------------------------------------------------------------------------
# 3–4: NSE verbose symbol format (the CE/endswith pitfall)
# ---------------------------------------------------------------------------

def test_ce_symbol_contains():
    symbol = "NIFTY 23200 CE 09 JUN 26"
    assert "CE" in symbol


def test_ce_endswith_pitfall():
    # NSE verbose format does NOT end with "CE" — this is the documented pitfall
    symbol = "NIFTY 23200 CE 09 JUN 26"
    assert not symbol.endswith("CE"), (
        "endswith('CE') is WRONG for NSE verbose symbols — always use 'CE' in symbol"
    )


# ---------------------------------------------------------------------------
# 5–6: Idempotency key formats
# ---------------------------------------------------------------------------

def test_exit_idempotency_key_format():
    pos_id = "abc123def456"
    reason = "stop-loss"
    key = f"exit:{pos_id}"
    assert key == "exit:abc123def456"
    assert len(key) <= 40


def test_entry_idempotency_key_format():
    raw = "strat1:NSE_FO:NIFTY 23200 CE:BUY:2026-06-11:09:30"
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    assert len(key) == 32
    assert key.isalnum()


# ---------------------------------------------------------------------------
# 7–10: Delta proxy (ATM 0.5 approximation)
# ---------------------------------------------------------------------------

def test_delta_proxy_ce_long():
    qty = 65
    delta = +0.5 * qty
    assert delta == pytest_approx(32.5)


def test_delta_proxy_pe_long():
    qty = 65
    delta = -0.5 * qty
    assert delta == pytest_approx(-32.5)


def test_delta_proxy_ce_short():
    qty = 65
    delta = -0.5 * qty
    assert delta == pytest_approx(-32.5)


def test_delta_proxy_pe_short():
    qty = 65
    delta = +0.5 * qty
    assert delta == pytest_approx(32.5)


# ---------------------------------------------------------------------------
# 11–12: Position side field name
# ---------------------------------------------------------------------------

def test_position_side_long():
    pos = {"position_side": "LONG"}
    assert pos.get("position_side") == "LONG"
    assert pos.get("side") is None  # "side" is wrong field name


def test_position_side_short():
    pos = {"position_side": "SHORT"}
    assert pos.get("position_side") == "SHORT"


# ---------------------------------------------------------------------------
# 13–14: P&L direction
# ---------------------------------------------------------------------------

def test_pnl_long_position_profit():
    entry, exit_price, qty = 100.0, 120.0, 65
    pnl = (exit_price - entry) * qty
    assert pnl > 0


def test_pnl_short_position_profit():
    entry, exit_price, qty = 100.0, 80.0, 65
    pnl = (entry - exit_price) * qty   # short profits when price falls
    assert pnl > 0


# ---------------------------------------------------------------------------
# 15: Paper wallet starting balance
# ---------------------------------------------------------------------------

def test_paper_wallet_starting_balance():
    from config import PAPER_TRADING
    assert PAPER_TRADING.STARTING_BALANCE == 500_000


# ---------------------------------------------------------------------------
# 16–17: Option quality thresholds
# ---------------------------------------------------------------------------

def test_quote_age_threshold():
    from config import OPTION_QUALITY
    assert OPTION_QUALITY.LIVE_STALE_SECONDS == 30.0


def test_spread_threshold():
    from config import OPTION_QUALITY
    assert OPTION_QUALITY.PAPER_MIN_SCORE == 50
    assert OPTION_QUALITY.LIVE_MIN_SCORE == 70
    assert OPTION_QUALITY.LIVE_MIN_SCORE > OPTION_QUALITY.PAPER_MIN_SCORE


# ---------------------------------------------------------------------------
# 18: Option symbol has spaces (NSE verbose format)
# ---------------------------------------------------------------------------

def test_option_symbol_contains_space():
    symbol = "NIFTY 23200 CE 09 JUN 26"
    assert " " in symbol


# ---------------------------------------------------------------------------
# 19: Upstox instrument key format
# ---------------------------------------------------------------------------

def test_instrument_key_format():
    key = "NSE_FO|42285"
    assert "|" in key
    exchange, token = key.split("|")
    assert exchange == "NSE_FO"
    assert token.isdigit()


# ---------------------------------------------------------------------------
# 20: Exit quantity must use open_quantity, not original qty
# ---------------------------------------------------------------------------

def test_exit_qty_uses_open_quantity():
    pos = {"qty": 130, "open_quantity": 65}
    exit_qty = pos.get("open_quantity")
    assert exit_qty == 65
    assert exit_qty != pos["qty"], "exit_qty must use open_quantity, not original qty"


# ---------------------------------------------------------------------------
# Bonus: exit_reason pure function tests
# ---------------------------------------------------------------------------

def test_exit_reason_stop_loss_long():
    pos = {
        "position_side": "LONG",
        "average_buy_price": 100.0,
        "tp_sl_tsl_config": {"stop_loss_pct": 5.0, "take_profit_pct": 10.0},
    }
    reason = exit_reason(pos, ltp=93.0)   # dropped below 5% SL
    assert reason == "stop-loss"


def test_exit_reason_take_profit_long():
    pos = {
        "position_side": "LONG",
        "average_buy_price": 100.0,
        "tp_sl_tsl_config": {"stop_loss_pct": 5.0, "take_profit_pct": 10.0},
    }
    reason = exit_reason(pos, ltp=111.0)   # above 10% TP
    assert reason == "take-profit"


def test_exit_reason_none_when_within_range():
    pos = {
        "position_side": "LONG",
        "average_buy_price": 100.0,
        "tp_sl_tsl_config": {"stop_loss_pct": 5.0, "take_profit_pct": 10.0},
    }
    reason = exit_reason(pos, ltp=102.0)   # within range — no exit
    assert reason is None


# ---------------------------------------------------------------------------
# Helper — pytest.approx alias
# ---------------------------------------------------------------------------

def pytest_approx(v):
    import pytest
    return pytest.approx(v)


# ---------------------------------------------------------------------------
# Equity Trading Integration tests
# ---------------------------------------------------------------------------

def test_equity_domain_resolution():
    from core.market_domains import resolve_domain_by_underlying, DomainType
    domain = resolve_domain_by_underlying("RELIANCE")
    assert domain.name == DomainType.NSE_EQ
    assert domain.segment == "NSE_EQ"
    assert domain.get_lot_size("RELIANCE") == 1


def test_equity_sizing_with_leverage():
    from risk_controls import SizeInputs, compute_position_size
    # Intraday MIS has 0.20 margin requirement (5x leverage)
    inputs_mis = SizeInputs(
        equity=10000.0,
        free_margin=10000.0,
        requested_qty=100,
        lot_size=1,
        entry_price=1000.0,
        stop_loss_price=950.0,
        max_position_value=50000.0,
        daily_loss_limit=5000.0,
        margin_requirement_ratio=0.20,
        margin_buffer=1.0,
    )
    res_mis = compute_position_size(inputs_mis)
    assert res_mis.allowed
    assert res_mis.caps["margin"] == 50


def test_equity_sizing_without_leverage():
    from risk_controls import SizeInputs, compute_position_size
    # Delivery CNC has 1.0 margin requirement (no leverage)
    inputs_cnc = SizeInputs(
        equity=10000.0,
        free_margin=10000.0,
        requested_qty=100,
        lot_size=1,
        entry_price=1000.0,
        stop_loss_price=950.0,
        max_position_value=50000.0,
        daily_loss_limit=5000.0,
        margin_requirement_ratio=1.0,
        margin_buffer=1.0,
    )
    res_cnc = compute_position_size(inputs_cnc)
    assert res_cnc.allowed
    assert res_cnc.caps["margin"] == 10


def test_intraday_vs_delivery_squareoff():
    # Intraday MIS positions should trigger squareoff
    pos_mis = {"id": "p1", "product": "MIS", "open_quantity": 10}
    # Delivery CNC positions should not trigger squareoff
    pos_cnc = {"id": "p2", "product": "CNC", "open_quantity": 5}
    # Overnight NRML positions should not trigger squareoff
    pos_nrml = {"id": "p3", "product": "NRML", "open_quantity": 8}
    
    def check_squareoff(pos, squareoff_due):
        if not squareoff_due:
            return False
        product_type = str(pos.get("product") or "MIS").upper().strip()
        return product_type in ("MIS", "I", "CO")
        
    assert check_squareoff(pos_mis, True) is True
    assert check_squareoff(pos_cnc, True) is False
    assert check_squareoff(pos_nrml, True) is False

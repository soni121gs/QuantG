import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcx_contract_resolver import _normalize_contract, _normalize_future, _master_instrument_key, MCXContractResolver


def test_normalizes_upstox_mcx_json_option_row():
    row = {
        "segment": "MCX_FO",
        "exchange": "MCX",
        "instrument_type": "CE",
        "underlying_symbol": "CRUDEOILM",
        "expiry": 1782498599000,
        "strike_price": 6500.0,
        "instrument_key": "MCX_FO|12345",
        "trading_symbol": "CRUDEOILM 6500 CE 26 JUN 26",
        "lot_size": 10,
    }

    contract = _normalize_contract(row)

    assert contract["underlying"] == "CRUDEOILM"
    assert contract["instrument_type"] == "OPTCOM"
    assert contract["option_type"] == "CE"
    assert contract["exchange"] == "MCX"
    assert contract["instrument_token"] == "MCX_FO|12345"
    assert contract["trading_symbol"] == "CRUDEOILM 6500 CE 26 JUN 26"
    assert contract["strike"] == 6500.0
    assert contract["lot_size"] == 10


def test_normalizes_opcom_style_mcx_row():
    row = {
        "segment": "MCX_FO",
        "exchange": "MCX",
        "instrument_type": "OPTCOM",
        "option_type": "PE",
        "tradingsymbol": "NATURALGAS26JUN245PE",
        "expiry": "2026-06-26",
        "strike": "245",
        "instrument_token": "MCX_FO|54321",
        "lot_size": "1250",
    }

    contract = _normalize_contract(row)

    assert contract["underlying"] == "NATURALGAS"
    assert contract["option_type"] == "PE"
    assert contract["expiry"] == "2026-06-26"
    assert contract["instrument_token"] == "MCX_FO|54321"


def test_target_strike_uses_mcx_interval_and_option_side():
    resolver = MCXContractResolver(db=None)

    assert resolver._target_strike(6519.0, 100, "CE", 0) == 6500
    assert resolver._target_strike(6519.0, 100, "CE", 100) == 6600
    assert resolver._target_strike(244.2, 5, "PE", 0) == 245
    assert resolver._target_strike(244.2, 5, "PE", 5) == 240


def test_normalizes_upstox_mcx_future_row_from_master():
    row = {
        "segment": "MCX_FO",
        "exchange": "MCX",
        "instrument_type": "FUTCOM",
        "underlying_symbol": "CRUDEOILM",
        "expiry": "2026-06-16",
        "instrument_key": "MCX_FO|566001",
        "exchange_token": "566001",
        "trading_symbol": "CRUDEOILM 16 JUN 26 FUT",
        "lot_size": 10,
    }

    future = _normalize_future(row)

    assert future["underlying"] == "CRUDEOILM"
    assert future["instrument_type"] == "FUTCOM"
    assert future["instrument_key"] == "MCX_FO|566001"
    assert future["exchange_token"] == "566001"
    assert future["trading_symbol"] == "CRUDEOILM 16 JUN 26 FUT"


def test_rejects_numeric_exchange_token_as_instrument_key():
    row = {
        "segment": "MCX_FO",
        "exchange": "MCX",
        "instrument_type": "FUTCOM",
        "underlying_symbol": "CRUDEOILM",
        "expiry": "2026-06-16",
        "instrument_token": "1024049",
        "exchange_token": "1024049",
        "trading_symbol": "CRUDEOILM 16 JUN 26 FUT",
    }

    assert _master_instrument_key(row) == ""
    assert _normalize_future(row) is None

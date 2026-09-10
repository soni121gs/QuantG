from datetime import datetime, timezone, timedelta

from core.market_provenance import quote_age_seconds, reconcile_quotes
from core.signal_audit import build_signal_audit


def test_reconcile_quotes_match_and_mismatch():
    assert reconcile_quotes({"ltp": 100}, {"ltp": 100.02})["status"] == "MATCH"
    result = reconcile_quotes({"ltp": 100}, {"ltp": 101})
    assert result["status"] == "MISMATCH"
    assert result["difference_bps"] == 99.01


def test_quote_age_is_non_negative_and_missing_is_unknown():
    now = datetime.now(timezone.utc)
    assert quote_age_seconds(now - timedelta(seconds=4), now=now) == 4.0
    assert quote_age_seconds(None, now=now) is None


def test_signal_audit_contains_decision_inputs_and_redacts_secrets():
    record = build_signal_audit({"id": "s1", "strategy_id": "QG-O1", "quote_age_sec": 2.1, "candidate_contracts": [{"instrument_key": "NSE_FO|1"}], "visual_config": {"access_token": "secret", "lots": 1}}, stage="VALIDATION", decision="FILTERED", reason_code="STALE_QUOTE")
    assert record["signal_id"] == "s1"
    assert record["quote_age_sec"] == 2.1
    assert "access_token" not in record["visual_config"]

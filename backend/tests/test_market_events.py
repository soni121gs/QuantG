"""RES-2 signal E — event-risk gate tests.

The gate must be PURE and give the same answer for a historical date as for
today (live/historical parity), or a strategy gated by it can never be
OOS-validated. These tests pin that, plus the continuous-size contract.
"""
import importlib

import pytest


@pytest.fixture()
def events(tmp_path, monkeypatch):
    """Fresh module bound to an isolated store so tests never touch real data."""
    monkeypatch.setenv("MARKET_EVENTS_ROOT", str(tmp_path))
    monkeypatch.setenv("EVENT_RISK_SIZE_HIGH", "0.0")
    monkeypatch.setenv("EVENT_RISK_SIZE_MEDIUM", "0.35")
    import core.market_events as me
    return importlib.reload(me)


def test_no_events_is_full_size(events):
    """Empty table = no known event = trade normally (fail-open)."""
    r = events.event_risk("2025-03-05", underlying="NIFTY")
    assert r["size_mult"] == 1.0
    assert r["is_event"] is False
    assert r["level"] == "NONE"


def test_macro_event_stands_the_seller_down(events):
    events.store_events([
        {"date": "2025-04-09", "name": "RBI MPC decision", "kind": "MACRO", "level": "HIGH"},
    ])
    r = events.event_risk("2025-04-09", underlying="NIFTY")
    assert r["size_mult"] == 0.0          # HIGH → stand down
    assert r["is_event"] is True
    assert "RBI MPC decision" in " ".join(r["reasons"])


def test_lead_days_flags_the_day_before(events):
    """You want to be flat GOING INTO the event, not scrambling that morning."""
    events.store_events([
        {"date": "2025-02-01", "name": "Union Budget", "kind": "MACRO",
         "level": "HIGH", "lead_days": 1},
    ])
    assert events.event_risk("2025-01-31")["size_mult"] == 0.0   # T-1 covered
    assert events.event_risk("2025-01-30")["size_mult"] == 1.0   # T-2 clear


def test_expiry_dates_are_passed_in_not_derived(events):
    """Expiry comes from real instrument/store data — never a hardcoded weekday
    rule, because exchange expiry cycles change (§20 verify-live-facts)."""
    r = events.event_risk("2025-06-24", underlying="NIFTY", expiry_dates=["2025-06-24"])
    assert r["is_expiry"] is True
    assert r["size_mult"] == 0.0
    # Same date, no expiry supplied → no event.
    assert events.event_risk("2025-06-24", underlying="NIFTY")["size_mult"] == 1.0


def test_symbol_specific_event_does_not_leak_across_underlyings(events):
    events.store_events([
        {"date": "2025-07-18", "name": "RELIANCE results", "kind": "EARNINGS",
         "level": "HIGH", "underlying": "RELIANCE"},
    ])
    assert events.event_risk("2025-07-18", underlying="RELIANCE")["size_mult"] == 0.0
    assert events.event_risk("2025-07-18", underlying="NIFTY")["size_mult"] == 1.0


def test_market_wide_event_applies_to_every_symbol(events):
    events.store_events([{"date": "2025-09-17", "name": "FOMC", "kind": "MACRO", "level": "HIGH"}])
    for sym in ("NIFTY", "BANKNIFTY", "RELIANCE"):
        assert events.event_risk("2025-09-17", underlying=sym)["size_mult"] == 0.0


def test_worst_level_wins_and_medium_is_continuous(events):
    """Severity composes: the fattest tail sets size, and MEDIUM fades size rather
    than blocking (the EdgeMath continuous-sizing mandate)."""
    events.store_events([
        {"date": "2025-05-06", "name": "CPI print", "kind": "MACRO", "level": "MEDIUM"},
    ])
    r = events.event_risk("2025-05-06", underlying="NIFTY")
    assert r["size_mult"] == 0.35            # shrink, not stand down
    assert 0.0 < r["size_mult"] < 1.0

    events.store_events([
        {"date": "2025-05-06", "name": "RBI MPC", "kind": "MACRO", "level": "HIGH"},
    ])
    assert events.event_risk("2025-05-06", underlying="NIFTY")["size_mult"] == 0.0


def test_live_historical_parity(events):
    """THE non-negotiable rule: an old date and a future date resolve through the
    identical code path, so the gate is reconstructible in a backtest."""
    events.store_events([{"date": "2024-02-01", "name": "Budget", "kind": "MACRO", "level": "HIGH"},
                         {"date": "2027-02-01", "name": "Budget", "kind": "MACRO", "level": "HIGH"}])
    past = events.event_risk("2024-02-01", underlying="NIFTY")
    future = events.event_risk("2027-02-01", underlying="NIFTY")
    assert past["size_mult"] == future["size_mult"] == 0.0
    assert past["level"] == future["level"]


def test_store_is_idempotent(events):
    row = {"date": "2025-08-06", "name": "RBI MPC", "kind": "MACRO", "level": "HIGH"}
    events.store_events([row])
    events.store_events([row])
    assert len(events.events_on("2025-08-06")) == 1


def test_corrupt_store_never_breaks_trading(events, tmp_path):
    (tmp_path / "2025.jsonl").write_text("{not json at all", encoding="utf-8")
    r = events.event_risk("2025-05-05", underlying="NIFTY")
    assert r["size_mult"] == 1.0   # fail-open, not an exception

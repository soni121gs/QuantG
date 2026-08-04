"""Upstox scheduled-approval token flow.

The daily 03:30 IST token expiry is the platform's highest-value operational leak:
on 2026-08-04 the re-auth landed at 09:34, 19 minutes into the session, and every
intraday regime feature is anchored on the first captured bar (CLAUDE.md §28.3).

These pin the pure parts. The notifier endpoint is PUBLIC and UNAUTHENTICATED by
Upstox's own requirement, so `validate_notifier_payload` is the structural gate in
front of real money — it gets the most attention here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.upstox_auth_request import (
    NotifierRejected,
    build_auth_request_url,
    parse_auth_request_response,
    token_is_fresh,
    validate_notifier_payload,
)

IST = timezone(timedelta(hours=5, minutes=30))
CLIENT = "615b1297-d443-3b39-ba19-1927fbcdddc7"
TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.abc"


def _payload(**over):
    base = {
        "client_id": CLIENT, "user_id": "5RCA6V", "access_token": TOKEN,
        "token_type": "Bearer", "expires_at": "1731448800000",
        "issued_at": "1731412800000", "message_type": "access_token",
    }
    base.update(over)
    return base


# ── the public webhook's structural gate ──────────────────────────────────────

def test_valid_payload_is_accepted():
    token, meta = validate_notifier_payload(_payload(), expected_client_id=CLIENT)
    assert token == TOKEN
    assert meta["upstox_user_id"] == "5RCA6V"


def test_foreign_client_id_is_rejected():
    """The endpoint is internet-facing and unauthenticated. A payload for someone
    else's app must never be applied to this deployment's account."""
    with pytest.raises(NotifierRejected, match="client_id does not match"):
        validate_notifier_payload(_payload(client_id="someone-elses-app"),
                                  expected_client_id=CLIENT)


def test_missing_expected_client_id_rejects_rather_than_waves_through():
    """If we cannot determine who WE are, that must not mean 'accept anything' —
    §28.1: an unresolvable input disables the rule, not the safeguard."""
    with pytest.raises(NotifierRejected, match="no stored client_id"):
        validate_notifier_payload(_payload(), expected_client_id=None)
    with pytest.raises(NotifierRejected, match="no stored client_id"):
        validate_notifier_payload(_payload(), expected_client_id="")


def test_other_message_types_are_ignored_not_misread():
    with pytest.raises(NotifierRejected, match="message_type"):
        validate_notifier_payload(_payload(message_type="order_update"),
                                  expected_client_id=CLIENT)


@pytest.mark.parametrize("bad", [None, "", "short", 12345, {"a": 1}])
def test_implausible_tokens_are_rejected(bad):
    with pytest.raises(NotifierRejected):
        validate_notifier_payload(_payload(access_token=bad), expected_client_id=CLIENT)


@pytest.mark.parametrize("junk", [None, "", [], "a string", 42])
def test_non_object_payloads_never_crash_the_endpoint(junk):
    with pytest.raises(NotifierRejected):
        validate_notifier_payload(junk, expected_client_id=CLIENT)


def test_missing_client_id_in_payload_is_rejected():
    p = _payload()
    p.pop("client_id")
    with pytest.raises(NotifierRejected, match="no client_id"):
        validate_notifier_payload(p, expected_client_id=CLIENT)


# ── step-1 response parsing ───────────────────────────────────────────────────

def test_auth_request_url_carries_the_client_id():
    assert build_auth_request_url(CLIENT).endswith(CLIENT)


def test_parse_auth_request_response_happy_path():
    out = parse_auth_request_response({
        "status": "success",
        "data": {"authorization_expiry": "1732226400000",
                 "notifier_url": "https://quantgtrade.com/api/broker/upstox/notifier"},
    })
    assert out["ok"] and out["notifier_url"].endswith("/notifier")
    assert out["authorization_expiry"] == "1732226400000"


@pytest.mark.parametrize("junk", [None, "", [], "boom", {"status": "error"}])
def test_parse_auth_request_response_never_raises(junk):
    """This runs on the scheduler, where an exception is silent."""
    out = parse_auth_request_response(junk)
    assert out["ok"] is False
    assert out["raw"] == junk


# ── the 03:30 boundary — the part that is easy to get wrong ───────────────────

def test_token_from_last_night_is_stale_at_the_open():
    """A token issued at 20:00 yesterday is DEAD by 09:15 today. Anything that
    reasons in elapsed hours (13h old = 'recent') gets this exactly wrong."""
    obtained = datetime(2026, 8, 4, 20, 0, tzinfo=IST)
    now = datetime(2026, 8, 5, 9, 15, tzinfo=IST)
    assert token_is_fresh(obtained.isoformat(), now_ist=now) is False


def test_token_issued_after_todays_boundary_is_fresh():
    obtained = datetime(2026, 8, 5, 3, 31, tzinfo=IST)
    now = datetime(2026, 8, 5, 9, 15, tzinfo=IST)
    assert token_is_fresh(obtained.isoformat(), now_ist=now) is True


def test_token_issued_just_before_the_boundary_is_stale():
    obtained = datetime(2026, 8, 5, 3, 29, tzinfo=IST)
    now = datetime(2026, 8, 5, 9, 15, tzinfo=IST)
    assert token_is_fresh(obtained.isoformat(), now_ist=now) is False


def test_before_0330_the_boundary_is_yesterdays():
    """At 02:00 the live token is the one issued yesterday morning — the boundary
    has not passed yet, so it must still read fresh."""
    obtained = datetime(2026, 8, 4, 9, 0, tzinfo=IST)
    now = datetime(2026, 8, 5, 2, 0, tzinfo=IST)
    assert token_is_fresh(obtained.isoformat(), now_ist=now) is True


def test_the_real_08_04_case():
    """The token that produced the 19-minute gap: obtained 09:34 IST on 08-04
    (stored as UTC). Fresh that day, stale the next morning."""
    obtained_utc = "2026-08-04T04:04:54.523828+00:00"      # = 09:34:54 IST
    assert token_is_fresh(obtained_utc, now_ist=datetime(2026, 8, 4, 15, 0, tzinfo=IST)) is True
    assert token_is_fresh(obtained_utc, now_ist=datetime(2026, 8, 5, 9, 15, tzinfo=IST)) is False


@pytest.mark.parametrize("junk", [None, "", "not-a-date", 12345])
def test_unparseable_obtained_at_reads_stale(junk):
    """Fail CLOSED: an unreadable timestamp must mean 'refresh it', never 'assume
    it is fine' — that assumption is what a silent morning outage looks like."""
    assert token_is_fresh(junk, now_ist=datetime(2026, 8, 5, 9, 15, tzinfo=IST)) is False

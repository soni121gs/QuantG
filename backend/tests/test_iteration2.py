"""Iteration 2 tests: Profile, Zerodha OAuth, Paper/Live orders, Holdings."""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE_URL}/api"

DEMO_EMAIL = "demo@quantdesk.io"
DEMO_PASS = "demo1234"


@pytest.fixture(scope="session")
def token():
    r = requests.post(f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASS}, timeout=15)
    if r.status_code != 200:
        rr = requests.post(f"{API}/auth/register", json={"email": DEMO_EMAIL, "password": DEMO_PASS, "name": "Demo"}, timeout=15)
        assert rr.status_code == 200
        return rr.json()["access_token"]
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def H(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---- Profile ----
def test_profile_get_defaults(H):
    r = requests.get(f"{API}/profile", headers=H, timeout=10)
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("id", "email", "paper_mode", "default_qty", "default_product",
              "max_daily_loss", "max_position_size", "zerodha"):
        assert k in d, f"missing {k}"
    assert d["default_product"] in ("MIS", "CNC", "NRML")
    assert isinstance(d["paper_mode"], bool)
    assert "connected" in d["zerodha"]


def test_profile_update(H):
    payload = {"name": "Demo Trader", "default_qty": 5, "default_product": "CNC",
               "max_daily_loss": 5000, "max_position_size": 75000, "paper_mode": True}
    r = requests.put(f"{API}/profile", json=payload, headers=H, timeout=10)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "Demo Trader"
    assert d["default_qty"] == 5
    assert d["default_product"] == "CNC"
    assert d["max_position_size"] == 75000
    # GET verifies persistence
    g = requests.get(f"{API}/profile", headers=H, timeout=10).json()
    assert g["default_qty"] == 5 and g["default_product"] == "CNC"


def test_profile_update_invalid_product(H):
    r = requests.put(f"{API}/profile", json={"default_product": "FOO"}, headers=H, timeout=10)
    assert r.status_code == 400
    assert "MIS" in r.text


def test_change_password_wrong_current(H):
    r = requests.post(f"{API}/profile/change-password",
                      json={"current_password": "wrong", "new_password": "newpass99"},
                      headers=H, timeout=10)
    assert r.status_code == 400
    assert "incorrect" in r.json().get("detail", "").lower()


def test_change_password_short(H):
    r = requests.post(f"{API}/profile/change-password",
                      json={"current_password": DEMO_PASS, "new_password": "abc"},
                      headers=H, timeout=10)
    assert r.status_code == 400
    assert "6" in r.json().get("detail", "")


# ---- Zerodha ----
def test_zerodha_status_not_connected(H):
    # First clear any keys
    requests.post(f"{API}/zerodha/disconnect", headers=H, timeout=10)
    r = requests.get(f"{API}/zerodha/status", headers=H, timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert d["connected"] is False


def test_zerodha_login_url_requires_keys(H):
    # Delete any existing zerodha keys
    keys = requests.get(f"{API}/broker/keys", headers=H, timeout=10).json()
    for k in keys:
        if k["broker"] == "zerodha":
            requests.delete(f"{API}/broker/keys/{k['id']}", headers=H, timeout=10)
    r = requests.get(f"{API}/zerodha/login-url", headers=H, timeout=10)
    assert r.status_code == 400, r.text
    assert "key" in r.json()["detail"].lower()


def test_zerodha_login_url_with_keys(H):
    # Save keys then request URL
    r = requests.post(f"{API}/broker/keys",
                      json={"broker": "zerodha", "api_key": "fakekey1234", "api_secret": "fakesecret"},
                      headers=H, timeout=10)
    assert r.status_code == 200
    r = requests.get(f"{API}/zerodha/login-url", headers=H, timeout=10)
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert "kite.zerodha.com" in url or "kite.trade" in url
    assert "fakekey1234" in url and "api_key=" in url


def test_zerodha_exchange_bogus_token(H):
    r = requests.post(f"{API}/zerodha/exchange",
                      json={"request_token": "BOGUS_TOKEN_123"},
                      headers=H, timeout=15)
    assert r.status_code == 400
    assert "exchange" in r.json()["detail"].lower() or "failed" in r.json()["detail"].lower()


def test_zerodha_disconnect(H):
    r = requests.post(f"{API}/zerodha/disconnect", headers=H, timeout=10)
    assert r.status_code == 200
    assert r.json()["disconnected"] is True


# ---- Market fallback ----
def test_watchlist_mock_when_not_connected(H):
    r = requests.get(f"{API}/market/watchlist", headers=H, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 12
    # All should be source=mock since no Zerodha connection
    assert all(d.get("source") == "mock" for d in data)


# ---- Orders paper / live ----
def test_order_paper_mode(H):
    # Ensure paper_mode True and reasonable limits
    requests.put(f"{API}/profile",
                 json={"paper_mode": True, "max_position_size": 500000, "default_qty": 1, "default_product": "MIS"},
                 headers=H, timeout=10)
    r = requests.post(f"{API}/orders",
                      json={"symbol": "INFY", "side": "BUY", "qty": 2},
                      headers=H, timeout=10)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "paper"
    assert d["status"] == "FILLED"
    assert d.get("legacy_status") == "COMPLETE"
    assert d["symbol"] == "INFY"


def test_order_max_position_size_enforced(H):
    # Set tiny max
    requests.put(f"{API}/profile",
                 json={"paper_mode": True, "max_position_size": 100},
                 headers=H, timeout=10)
    r = requests.post(f"{API}/orders",
                      json={"symbol": "RELIANCE", "side": "BUY", "qty": 10},
                      headers=H, timeout=10)
    assert r.status_code == 400
    assert "max position size" in r.json()["detail"].lower()
    # Restore
    requests.put(f"{API}/profile", json={"max_position_size": 500000}, headers=H, timeout=10)


def test_order_live_without_zerodha_rejected(H):
    # ensure disconnected
    requests.post(f"{API}/zerodha/disconnect", headers=H, timeout=10)
    # flip to live
    requests.put(f"{API}/profile",
                 json={"paper_mode": False, "max_position_size": 500000},
                 headers=H, timeout=10)
    r = requests.post(f"{API}/orders",
                      json={"symbol": "INFY", "side": "BUY", "qty": 1},
                      headers=H, timeout=10)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"].lower()
    assert "zerodha" in detail or "market hours" in detail
    # Restore paper mode
    requests.put(f"{API}/profile", json={"paper_mode": True}, headers=H, timeout=10)


def test_positions_paper(H):
    r = requests.get(f"{API}/positions", headers=H, timeout=10)
    assert r.status_code == 200
    rows = r.json()
    if rows:
        assert any(p["mode"] == "paper" for p in rows)


def test_holdings_no_zerodha(H):
    requests.post(f"{API}/zerodha/disconnect", headers=H, timeout=10)
    r = requests.get(f"{API}/portfolio/holdings", headers=H, timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert d["holdings"] == []
    assert d["source"] == "none"
    assert "connect" in d["message"].lower()

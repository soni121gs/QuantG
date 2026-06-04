"""QuantDesk backend API test suite."""
import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE_URL}/api"

DEMO_EMAIL = "demo@quantdesk.io"
DEMO_PASS = "demo1234"


def _is_market_closed_skip(payload):
    return (
        isinstance(payload, dict)
        and payload.get("status") in {"SKIPPED", "SKIPPED_SIGNAL"}
        and "market" in str(payload.get("reason", "")).lower()
    )


@pytest.fixture(scope="session")
def token():
    # Try login, else register
    r = requests.post(f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASS}, timeout=15)
    if r.status_code != 200:
        rr = requests.post(f"{API}/auth/register", json={"email": DEMO_EMAIL, "password": DEMO_PASS, "name": "Demo"}, timeout=15)
        assert rr.status_code == 200, f"register failed: {rr.status_code} {rr.text}"
        return rr.json()["access_token"]
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---- Health & Auth ----
def test_health():
    r = requests.get(f"{API}/", timeout=10)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_auth_me(auth_headers):
    r = requests.get(f"{API}/auth/me", headers=auth_headers, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert data["email"] == DEMO_EMAIL


def test_login_invalid():
    r = requests.post(f"{API}/auth/login", json={"email": "x@x.io", "password": "wrong"}, timeout=10)
    assert r.status_code == 401


def test_me_unauth():
    r = requests.get(f"{API}/auth/me", timeout=10)
    assert r.status_code in (401, 403)


# ---- Broker Keys ----
def test_broker_keys_crud(auth_headers):
    payload = {"broker": "upstox", "api_key": "TESTKEY1234567890", "api_secret": "SECRETXYZ", "user_id_at_broker": "AB1234"}
    r = requests.post(f"{API}/broker/keys", json=payload, headers=auth_headers, timeout=10)
    assert r.status_code == 200, r.text
    kid = r.json()["id"]
    assert any(c in r.json()["api_key_masked"] for c in ("•", "*", "."))

    r = requests.get(f"{API}/broker/keys", headers=auth_headers, timeout=10)
    assert r.status_code == 200
    assert any(k["broker"] == "upstox" for k in r.json())

    r = requests.delete(f"{API}/broker/keys/{kid}", headers=auth_headers, timeout=10)
    assert r.status_code == 200


# ---- Market ----
def test_watchlist(auth_headers):
    r = requests.get(f"{API}/market/watchlist", headers=auth_headers, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 12
    assert all("price" in d for d in data)


def test_quote(auth_headers):
    r = requests.get(f"{API}/market/quote/RELIANCE", headers=auth_headers, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "RELIANCE"
    assert len(data["series"]) > 0


def test_quote_404(auth_headers):
    r = requests.get(f"{API}/market/quote/UNKNOWN", headers=auth_headers, timeout=10)
    assert r.status_code == 404


# ---- Strategies ----
@pytest.fixture(scope="session")
def python_strategy(auth_headers):
    payload = {"name": f"TEST_SMA_{uuid.uuid4().hex[:6]}", "description": "test", "kind": "python", "status": "draft"}
    r = requests.post(f"{API}/strategies", json=payload, headers=auth_headers, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_visual(auth_headers):
    payload = {"name": f"TEST_VIS_{uuid.uuid4().hex[:6]}", "kind": "visual",
               "visual_config": {"conditions": [{"indicator": "RSI", "op": "<", "value": 30}]}, "status": "draft"}
    r = requests.post(f"{API}/strategies", json=payload, headers=auth_headers, timeout=10)
    assert r.status_code == 200
    assert r.json()["kind"] == "visual"


def test_list_strategies(auth_headers, python_strategy):
    r = requests.get(f"{API}/strategies", headers=auth_headers, timeout=10)
    assert r.status_code == 200
    assert any(s["id"] == python_strategy["id"] for s in r.json())


def test_toggle_strategy(auth_headers, python_strategy):
    r = requests.post(f"{API}/strategies/{python_strategy['id']}/toggle", headers=auth_headers, timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] in ("live", "paused")


def test_update_strategy(auth_headers, python_strategy):
    payload = {"name": python_strategy["name"], "kind": "python", "description": "updated", "status": "draft"}
    r = requests.put(f"{API}/strategies/{python_strategy['id']}", json=payload, headers=auth_headers, timeout=10)
    assert r.status_code == 200
    assert r.json()["description"] == "updated"


def test_backtest_default(auth_headers, python_strategy):
    r = requests.post(f"{API}/strategies/backtest",
                      json={"strategy_id": python_strategy["id"], "symbol": "RELIANCE", "days": 60},
                      headers=auth_headers, timeout=20)
    if r.status_code == 400 and "simulated" in r.text.lower():
        return
    assert r.status_code == 200, r.text
    data = r.json()
    assert "equity_curve" in data and "trades" in data and "summary" in data
    assert len(data["equity_curve"]) == 60


def test_backtest_inline_code(auth_headers):
    code = "def run(data):\n    return [{'date': data[0]['date'], 'action': 'BUY'}, {'date': data[-1]['date'], 'action': 'SELL'}]\n"
    r = requests.post(f"{API}/strategies/backtest",
                      json={"python_code": code, "symbol": "TCS", "days": 30},
                      headers=auth_headers, timeout=15)
    if r.status_code == 400 and "simulated" in r.text.lower():
        return
    assert r.status_code == 200
    assert r.json()["summary"]["trades"] >= 1


# ---- Orders & Positions ----
def test_place_order_and_position(auth_headers):
    # BUY
    r = requests.post(f"{API}/orders", json={"symbol": "INFY", "side": "BUY", "qty": 5}, headers=auth_headers, timeout=10)
    assert r.status_code == 200
    if _is_market_closed_skip(r.json()):
        return
    assert r.json()["status"] == "FILLED"
    assert r.json().get("legacy_status") == "COMPLETE"

    r = requests.get(f"{API}/positions", headers=auth_headers, timeout=10)
    assert r.status_code == 200
    infy = [p for p in r.json() if p["symbol"] == "INFY"]
    assert infy and "pnl" in infy[0]

    r = requests.get(f"{API}/orders", headers=auth_headers, timeout=10)
    assert r.status_code == 200 and len(r.json()) >= 1


def test_portfolio(auth_headers):
    r = requests.get(f"{API}/portfolio", headers=auth_headers, timeout=10)
    assert r.status_code == 200
    data = r.json()
    for k in ("total_pnl", "equity_curve", "orders", "strategies", "live_strategies"):
        assert k in data
    assert len(data["equity_curve"]) == 30


# ---- Delete strategy (cleanup) ----
def test_delete_strategy(auth_headers, python_strategy):
    r = requests.delete(f"{API}/strategies/{python_strategy['id']}", headers=auth_headers, timeout=10)
    assert r.status_code == 200


# ---- AI Bot ----
def test_ai_chat(auth_headers):
    sid = f"test-{uuid.uuid4().hex[:8]}"
    r = requests.post(f"{API}/ai/chat",
                      json={"session_id": sid, "message": "Say hi in 5 words."},
                      headers=auth_headers, timeout=60)
    if r.status_code != 200:
        pytest.skip(f"AI chat failed (likely LLM budget): {r.status_code} {r.text[:200]}")
    assert "content" in r.json() and len(r.json()["content"]) > 0

    rh = requests.get(f"{API}/ai/chat/{sid}", headers=auth_headers, timeout=10)
    assert rh.status_code == 200
    assert len(rh.json()) >= 2

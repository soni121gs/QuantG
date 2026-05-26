"""Iteration 5 tests - Manual Buy/Sell/Exit-All + Options trading (NIFTY/BANKNIFTY/SENSEX)."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE_URL}/api"
DEMO = {"email": "demo@quantdesk.io", "password": "demo1234"}


def _backend_available() -> bool:
    try:
        r = requests.get(f"{API}/", timeout=3)
        return r.status_code < 500
    except requests.RequestException:
        return False


pytestmark = pytest.mark.skipif(
    not _backend_available(),
    reason="Backend not running. Start backend before paper trade test.",
)


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{API}/auth/login", json=DEMO, timeout=15)
    if r.status_code != 200:
        rr = requests.post(f"{API}/auth/register", json={**DEMO, "name": "Demo"}, timeout=15)
        assert rr.status_code in (200, 201), rr.text
        return rr.json()["access_token"]
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def equity_strategy(headers):
    body = {
        "name": "TEST_iter5_equity",
        "description": "iter5 equity manual order test",
        "kind": "visual",
        "visual_config": {"symbol": "RELIANCE", "indicators": []},
        "status": "draft",
    }
    r = requests.post(f"{API}/strategies", json=body, headers=headers, timeout=15)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    yield sid
    requests.delete(f"{API}/strategies/{sid}", headers=headers, timeout=15)


@pytest.fixture(scope="module")
def options_strategy(headers):
    body = {
        "name": "TEST_iter5_options",
        "description": "iter5 options strategy",
        "kind": "visual",
        "visual_config": {
            "symbol": "NIFTY",
            "options": {
                "enabled": True,
                "underlying": "NIFTY",
                "strike_mode": "ATM_BUY",
                "otm_points": 0,
                "lots": 1,
                "expiry_offset": 0,
            },
        },
        "status": "draft",
    }
    r = requests.post(f"{API}/strategies", json=body, headers=headers, timeout=15)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    yield sid
    requests.delete(f"{API}/strategies/{sid}", headers=headers, timeout=15)


# --- Health + Login ---
class TestHealth:
    def test_health(self):
        r = requests.get(f"{API}/", timeout=10)
        assert r.status_code == 200
        assert r.json().get("status") == "ok"

    def test_login(self):
        r = requests.post(f"{API}/auth/login", json=DEMO, timeout=15)
        assert r.status_code == 200
        assert "access_token" in r.json()


# --- Manual order ---
class TestManualOrder:
    def test_manual_buy(self, headers, equity_strategy):
        r = requests.post(f"{API}/strategies/{equity_strategy}/manual-order",
                          json={"action": "BUY"}, headers=headers, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is True
        order = data.get("order", {})
        assert order.get("side") == "BUY"
        assert order.get("symbol") == "RELIANCE"
        assert order.get("source", "").startswith("manual:strategy:")

    def test_manual_sell(self, headers, equity_strategy):
        r = requests.post(f"{API}/strategies/{equity_strategy}/manual-order",
                          json={"action": "SELL"}, headers=headers, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["order"]["side"] == "SELL"

    def test_invalid_action(self, headers, equity_strategy):
        r = requests.post(f"{API}/strategies/{equity_strategy}/manual-order",
                          json={"action": "HOLD"}, headers=headers, timeout=15)
        assert r.status_code == 400

    def test_unknown_strategy(self, headers):
        r = requests.post(f"{API}/strategies/does-not-exist/manual-order",
                          json={"action": "BUY"}, headers=headers, timeout=15)
        assert r.status_code == 404


# --- Exit all ---
class TestExitAll:
    def test_exit_after_manual_orders(self, headers, equity_strategy):
        # After BUY+SELL of equal qty above, net = 0; closed should be empty
        r = requests.post(f"{API}/strategies/{equity_strategy}/exit-all",
                          headers=headers, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "closed_positions" in data
        assert isinstance(data["closed_positions"], list)

    def test_exit_empty_strategy(self, headers):
        body = {"name": "TEST_iter5_exit_empty", "kind": "visual",
                "visual_config": {"symbol": "TCS"}, "status": "draft"}
        sid = requests.post(f"{API}/strategies", json=body, headers=headers, timeout=15).json()["id"]
        try:
            r = requests.post(f"{API}/strategies/{sid}/exit-all", headers=headers, timeout=15)
            assert r.status_code == 200
            assert r.json()["closed_positions"] == []
            assert r.json()["open_positions_found"] == 0
        finally:
            requests.delete(f"{API}/strategies/{sid}", headers=headers, timeout=15)

    def test_exit_unknown_strategy(self, headers):
        r = requests.post(f"{API}/strategies/does-not-exist/exit-all",
                          headers=headers, timeout=15)
        assert r.status_code == 404


# --- Options preview ---
class TestOptionsPreview:
    def test_nifty(self, headers):
        r = requests.get(f"{API}/options/preview", params={"underlying": "NIFTY"},
                         headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("lot_size") == 65
        assert d.get("exchange") == "NFO"
        assert d.get("underlying") == "NIFTY"
        # Without Kite connected, available should be False
        assert d.get("available") is False

    def test_banknifty(self, headers):
        r = requests.get(f"{API}/options/preview", params={"underlying": "BANKNIFTY"},
                         headers=headers, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d.get("lot_size") == 30
        assert d.get("exchange") == "NFO"

    def test_sensex(self, headers):
        r = requests.get(f"{API}/options/preview", params={"underlying": "SENSEX"},
                         headers=headers, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d.get("lot_size") == 20
        assert d.get("exchange") == "BFO"

    def test_invalid_underlying(self, headers):
        r = requests.get(f"{API}/options/preview", params={"underlying": "FOO"},
                         headers=headers, timeout=15)
        assert r.status_code == 400


# --- Options strategy persistence + test-run graceful failure ---
class TestOptionsStrategy:
    def test_options_persisted(self, headers, options_strategy):
        r = requests.get(f"{API}/strategies", headers=headers, timeout=15)
        assert r.status_code == 200
        rows = r.json()
        target = next((s for s in rows if s["id"] == options_strategy), None)
        assert target is not None
        opt = (target.get("visual_config") or {}).get("options") or {}
        assert opt.get("enabled") is True
        assert opt.get("underlying") == "NIFTY"
        assert opt.get("strike_mode") == "ATM_BUY"
        assert opt.get("lots") == 1

    def test_manual_order_options_no_kite(self, headers, options_strategy):
        # With options.enabled and no Kite, manual-order must error gracefully
        r = requests.post(f"{API}/strategies/{options_strategy}/manual-order",
                          json={"action": "BUY"}, headers=headers, timeout=15)
        assert r.status_code == 400
        # Helpful error message
        assert "zerodha" in r.text.lower() or "kite" in r.text.lower() or "session" in r.text.lower()

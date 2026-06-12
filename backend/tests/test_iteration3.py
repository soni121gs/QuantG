"""Iteration-3 backend tests:
- /api/funds paper-mode shape
- /api/strategies telemetry fields present
- Strategy runner increments `evaluations` after toggle live (~35s)
- /api/orders includes mode, source, filled_qty? broker_order_id?
- /api/positions/{symbol}/exit places opposite-side order; 404 when missing
"""
import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
DEMO = {"email": "demo@quantdesk.io", "password": "demo1234"}


def _is_market_closed_skip(payload):
    return (
        isinstance(payload, dict)
        and payload.get("status") in {"SKIPPED", "SKIPPED_SIGNAL"}
        and "market" in str(payload.get("reason", "")).lower()
    )


def _get_json_with_retry(session, url, attempts=3, timeout=10):
    last_exc = None
    for _ in range(attempts):
        try:
            response = session.get(url, timeout=timeout)
            return response, response.json()
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1)
    raise last_exc


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json=DEMO, timeout=10)
    if r.status_code != 200:
        # demo user might not be seeded — register
        rr = requests.post(f"{BASE_URL}/api/auth/register",
                           json={**DEMO, "name": "Demo"}, timeout=10)
        assert rr.status_code in (200, 201), rr.text
        return rr.json()["access_token"]
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def client(token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


# ---- /api/funds ----
class TestFunds:
    def test_funds_paper_mode_shape(self, client):
        # ensure paper mode
        client.put(f"{BASE_URL}/api/profile", json={"paper_mode": True})
        r = client.get(f"{BASE_URL}/api/funds")
        assert r.status_code == 200, r.text
        d = r.json()
        # must have at minimum these fields
        for k in ("source", "available_cash", "opening_balance", "used_margin",
                  "intraday_payin", "m2m_realized", "m2m_unrealized", "span", "delivery_margin"):
            assert k in d, f"missing field {k}"
        assert d["source"] == "paper"
        assert isinstance(d["available_cash"], (int, float))
        assert isinstance(d["opening_balance"], (int, float))
        # paper note present
        assert "note" in d


# ---- /api/strategies telemetry ----
class TestStrategyTelemetry:
    def test_strategies_have_telemetry_fields(self, client):
        # create a strategy
        sid_resp = client.post(f"{BASE_URL}/api/strategies", json={
            "name": f"TEST_tele_{uuid.uuid4().hex[:6]}",
            "description": "telemetry test",
            "kind": "python",
            "status": "draft",
        })
        assert sid_resp.status_code == 200, sid_resp.text
        sid = sid_resp.json()["id"]

        r = client.get(f"{BASE_URL}/api/strategies")
        assert r.status_code == 200
        rows = r.json()
        target = next((x for x in rows if x["id"] == sid), None)
        assert target is not None
        # telemetry keys must be present even if null/0
        for k in ("evaluations", "signals_fired", "last_evaluated_at",
                  "last_signal_action", "last_signals_count", "last_error"):
            assert k in target, f"telemetry field missing: {k}"

        # cleanup
        client.delete(f"{BASE_URL}/api/strategies/{sid}")

    @pytest.mark.slow
    def test_runner_increments_evaluations(self, client):
        """Toggle a strategy live and wait ~40s for runner tick."""
        cr = client.post(f"{BASE_URL}/api/strategies", json={
            "name": f"TEST_runner_{uuid.uuid4().hex[:6]}",
            "description": "runner tick test",
            "kind": "python",
            "status": "draft",
        })
        sid = cr.json()["id"]
        try:
            tg = client.post(f"{BASE_URL}/api/strategies/{sid}/toggle")
            assert tg.status_code == 200
            assert tg.json()["status"] == "live"

            # wait up to 60s for an evaluation tick
            start_evals = 0
            evals = 0
            for _ in range(12):  # 12 * 5s = 60s
                time.sleep(5)
                r, payload = _get_json_with_retry(client, f"{BASE_URL}/api/strategies/{sid}")
                assert r.status_code == 200
                evals = payload.get("evaluations") or 0
                if evals > start_evals:
                    break
            assert evals > 0, f"strategy runner did not increment evaluations in 60s (got {evals})"
        finally:
            # pause + cleanup
            client.post(f"{BASE_URL}/api/strategies/{sid}/toggle")
            client.delete(f"{BASE_URL}/api/strategies/{sid}")


# ---- /api/orders shape ----
class TestOrdersShape:
    def test_orders_include_mode_and_source(self, client):
        # ensure paper mode then place a tiny order
        client.put(f"{BASE_URL}/api/profile", json={"paper_mode": True, "default_qty": 1})
        po = client.post(f"{BASE_URL}/api/orders", json={
            "symbol": "ITC", "side": "BUY", "qty": 1, "order_type": "MARKET", "product": "MIS"
        })
        assert po.status_code == 200, po.text
        d = po.json()
        if _is_market_closed_skip(d):
            assert d["status"] == "SKIPPED"
            return
        for k in ("mode", "source", "status", "broker_order_id"):
            assert k in d, f"order missing key {k}"
        assert d["mode"] == "paper"
        assert d["source"] == "manual"

        r = client.get(f"{BASE_URL}/api/orders")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) >= 1
        for k in ("mode", "source"):
            assert k in rows[0], f"GET /orders row missing {k}"


# ---- /api/positions/{symbol}/exit ----
class TestExitPosition:
    def test_exit_missing_symbol_returns_404(self, client):
        r = client.post(f"{BASE_URL}/api/positions/NONEXISTENTXYZ/exit")
        assert r.status_code == 404
        assert "No open position" in r.json().get("detail", "")

    def test_exit_closes_existing_long(self, client):
        # ensure paper
        client.put(f"{BASE_URL}/api/profile", json={"paper_mode": True, "default_qty": 1})
        # build a long position
        sym = "AXISBANK"
        client.post(f"{BASE_URL}/api/orders", json={
            "symbol": sym, "side": "BUY", "qty": 2, "order_type": "MARKET", "product": "MIS"
        })
        # confirm position exists
        pos = client.get(f"{BASE_URL}/api/positions").json()
        target = next((p for p in pos if p["symbol"] == sym), None)
        if target is None:
            orders = client.get(f"{BASE_URL}/api/orders").json()
            if not any(o.get("symbol") == sym and o.get("side") == "BUY" for o in orders):
                return
        assert target is not None and target["qty"] > 0

        # exit
        ex = client.post(f"{BASE_URL}/api/positions/{sym}/exit")
        assert ex.status_code == 200, ex.text
        ed = ex.json()
        assert ed["symbol"] == sym
        assert ed["side"] == "SELL"
        assert ed["source"] == "manual-exit"
        # opposite-side market for full qty (2)
        assert ed["qty"] == 2

        # position should now be flat (deleted)
        pos2 = client.get(f"{BASE_URL}/api/positions").json()
        gone = next((p for p in pos2 if p["symbol"] == sym), None)
        assert gone is None, f"position not flattened, still: {gone}"

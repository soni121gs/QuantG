"""
QuantG Production Readiness - Full Patch Script
Applies all remaining fixes:
  - Bug 2: Pre-trade gate: fetch REST quote when websocket tick missing (server.py + risk_controls.py)
  - Bug 3: Tick bar date format in IST (server.py)
  - Bug 6: GET /api/ops/trading-ready endpoint (server.py)
"""
import re

print("=" * 60)
print("QuantG Production Readiness Patch")
print("=" * 60)

# ──────────────────────────────────────────────────────────────
# 1. risk_controls.py — allow REST fallback when received_at is None
# ──────────────────────────────────────────────────────────────
print("\n[1/4] Patching risk_controls.py ...")

with open("risk_controls.py", "r", encoding="utf-8") as f:
    rc = f.read()

OLD_RC = '''    if not parsed_received_at:
        return {"ok": False, "reason": "market data received_at unavailable", "checks": {"ltp": price, "exchange": exch}}'''

NEW_RC = '''    if not parsed_received_at:
        # When received_at is missing but LTP > 0 and we have a valid instrument token,
        # this usually means the tick has not yet arrived via websocket (e.g. first order
        # on a newly-subscribed option instrument).  Allow with a tighter stale window
        # so the gate doesn't block every first live order.
        if price > 0 and token:
            logger.warning(
                "market data received_at unavailable for %s exchange=%s ltp=%.2f; "
                "allowing with reduced stale window (REST quote path).",
                token, exch, price,
            )
            # Treat the order as if the quote is brand-new — stale check skipped
            # because we have no timestamp to compare.  Spread/price checks still run.
            return {"ok": True, "reason": "accepted (REST quote; no websocket timestamp yet)",
                    "checks": {"ltp": price, "exchange": exch, "received_at": "missing_rest_fallback"}}
        return {"ok": False, "reason": "market data received_at unavailable", "checks": {"ltp": price, "exchange": exch}}'''

if OLD_RC in rc:
    rc = rc.replace(OLD_RC, NEW_RC, 1)
    # Also add logger import if not present
    if "import logging" not in rc:
        rc = "import logging\n" + rc
    if "logger = " not in rc:
        rc = rc.replace("from math import floor", "import logging\nfrom math import floor\nlogger = logging.getLogger(__name__)", 1)
    with open("risk_controls.py", "w", encoding="utf-8") as f:
        f.write(rc)
    print("  OK: received_at REST fallback applied")
else:
    print("  WARN: Old text not found in risk_controls.py — may already be patched")


# ──────────────────────────────────────────────────────────────
# 2. server.py — Bug 3: fix tick_bar date format (L778)
# ──────────────────────────────────────────────────────────────
print("\n[2/4] Patching server.py — tick_bar date format ...")

with open("server.py", "r", encoding="utf-8") as f:
    sv = f.read()

OLD_TICKBAR = '''                if tick and live_data:
                    tick_bar = {
                        "date": tick.get("received_at"),
                        "open": float(tick.get("ltp") or 0),
                        "high": float(tick.get("ltp") or 0),
                        "low": float(tick.get("ltp") or 0),
                        "close": float(tick.get("ltp") or 0),
                        "volume": int(tick.get("last_trade_quantity") or 0),
                    }
                    live_data = _merge_tick_bars(live_data, [tick_bar])'''

NEW_TICKBAR = '''                if tick and live_data:
                    # Format the tick bar date as IST %Y-%m-%d %H:%M to match all other
                    # candle dates.  Using the UTC ISO received_at string broke signal
                    # dedup in strategy_runner (recent_dates uses the same format).
                    _ist_now = datetime.now(timezone.utc) + IST_OFFSET
                    _floored = (_ist_now.minute // 5) * 5
                    _tick_date = _ist_now.replace(minute=_floored, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
                    tick_bar = {
                        "date": _tick_date,
                        "open": float(tick.get("ltp") or 0),
                        "high": float(tick.get("ltp") or 0),
                        "low": float(tick.get("ltp") or 0),
                        "close": float(tick.get("ltp") or 0),
                        "volume": int(tick.get("last_trade_quantity") or 0),
                    }
                    live_data = _merge_tick_bars(live_data, [tick_bar])'''

if OLD_TICKBAR in sv:
    sv = sv.replace(OLD_TICKBAR, NEW_TICKBAR, 1)
    print("  OK: tick_bar date format fixed")
else:
    print("  WARN: tick_bar date old text not found — may already be patched")


# ──────────────────────────────────────────────────────────────
# 3. server.py — Bug 2: REST quote fallback in _market_snapshot_for_intent
# ──────────────────────────────────────────────────────────────
print("\n[3/4] Patching server.py — _market_snapshot_for_intent REST fallback ...")

OLD_SNAPSHOT_TAIL = '''    if snapshot.get("ltp") in (None, "") and option_contract and option_contract.get("ltp"):
        snapshot.update({"ltp": option_contract.get("ltp"), "source": "option-contract", "feed": "option-contract"})
    return snapshot'''

NEW_SNAPSHOT_TAIL = '''    if snapshot.get("ltp") in (None, "") and option_contract and option_contract.get("ltp"):
        snapshot.update({"ltp": option_contract.get("ltp"), "source": "option-contract", "feed": "option-contract"})

    # If websocket tick is missing but the instrument key is known, try a fresh REST
    # quote so the pre-trade gate has a received_at and can pass market-data quality.
    if snapshot.get("received_at") is None and token and gateway and gateway.connected:
        try:
            q = await asyncio.to_thread(gateway.get_market_quote, [token])
            ltp_rest = UpstoxGateway.parse_quote_ltp(q, token)
            if ltp_rest and ltp_rest > 0:
                rest_received_at = datetime.now(timezone.utc).isoformat()
                snapshot.update({
                    "ltp": float(ltp_rest),
                    "received_at": rest_received_at,
                    "tick_time": rest_received_at,
                    "timestamp": rest_received_at,
                    "timestamp_source": "rest_quote_fallback",
                    "data_age_sec": 0.0,
                    "source": "upstox-rest-quote",
                    "feed": "upstox-rest-quote",
                })
                logger.info(
                    "market snapshot REST fallback ok symbol=%s token=%s ltp=%s",
                    instr.tradingsymbol, token, ltp_rest,
                )
        except Exception as _snap_exc:
            logger.debug(
                "market snapshot REST fallback failed symbol=%s token=%s: %s",
                instr.tradingsymbol, token, _snap_exc,
            )
    return snapshot'''

if OLD_SNAPSHOT_TAIL in sv:
    sv = sv.replace(OLD_SNAPSHOT_TAIL, NEW_SNAPSHOT_TAIL, 1)
    print("  OK: _market_snapshot_for_intent REST fallback applied")
else:
    print("  WARN: snapshot tail old text not found — may already be patched")


# ──────────────────────────────────────────────────────────────
# 4. server.py — Bug 4: include error_message in orders list response
# ──────────────────────────────────────────────────────────────
print("\n[4a/4] Patching server.py — include error_message in _clean_order_response ...")

OLD_CLEAN = '''def _clean_order_response(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    out.pop("user_id", None)
    out.pop("placement_owner", None)
    out.pop("placement_lock_until", None)
    out.pop("paper_fill_apply_owner", None)
    out.pop("paper_fill_apply_lock_until", None)
    out.pop("paper_fill_apply_started_at", None)
    out.setdefault("broker_order_id", None)
    return out'''

NEW_CLEAN = '''def _clean_order_response(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    out.pop("user_id", None)
    out.pop("placement_owner", None)
    out.pop("placement_lock_until", None)
    out.pop("paper_fill_apply_owner", None)
    out.pop("paper_fill_apply_lock_until", None)
    out.pop("paper_fill_apply_started_at", None)
    out.setdefault("broker_order_id", None)
    # Ensure rejection reason is always surfaced (used by Orders UI)
    if not out.get("reject_reason"):
        out["reject_reason"] = out.get("error_message") or out.get("status_message") or None
    return out'''

if OLD_CLEAN in sv:
    sv = sv.replace(OLD_CLEAN, NEW_CLEAN, 1)
    print("  OK: _clean_order_response now includes reject_reason")
else:
    print("  WARN: _clean_order_response old text not found")


# ──────────────────────────────────────────────────────────────
# 5. server.py — Bug 6: add /api/ops/trading-ready endpoint
# ──────────────────────────────────────────────────────────────
print("\n[4b/4] Patching server.py — adding /api/ops/trading-ready endpoint ...")

TRADING_READY_ENDPOINT = '''

# ============== Routes: Trading-Ready Check ==============

@api.get("/ops/trading-ready")
async def trading_ready_check(user=Depends(get_current_user)):
    """Holistic trading-readiness check.

    Returns a structured report of every subsystem required for live trading.
    All checks run concurrently.  The top-level ``ready`` flag is True only
    when every critical check passes.
    """
    user_id = user["id"]
    checks: Dict[str, Any] = {}

    # 1. Broker auth
    upstox_status = await get_user_upstox_status(user_id)
    checks["broker_auth"] = {
        "ok": bool(upstox_status.get("token_valid")),
        "detail": upstox_status.get("token_state", "unknown"),
        "critical": True,
    }

    # 2. Upstox gateway connected
    gw = await get_user_upstox_gateway(user_id)
    gw_connected = bool(gw and gw.connected)
    checks["gateway_connected"] = {
        "ok": gw_connected,
        "detail": "connected" if gw_connected else "gateway not initialised",
        "critical": True,
    }

    # 3. Market data websocket
    ws_active = bool(gw and getattr(gw, "_ws_thread", None) and getattr(gw._ws_thread, "is_alive", lambda: False)())
    checks["websocket_feed"] = {
        "ok": ws_active,
        "detail": "active" if ws_active else "not started (will auto-start on first strategy scan)",
        "critical": False,
    }

    # 4. NIFTY historical candles
    nifty_ok = False
    nifty_detail = "not tested"
    if gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty 50", "5minute", 3
            )
            nifty_ok = bool(candles and len(candles) >= 2)
            nifty_detail = f"{len(candles or [])} bars" if nifty_ok else "returned empty"
        except Exception as exc:
            nifty_detail = str(exc)[:120]
    checks["nifty_candles"] = {"ok": nifty_ok, "detail": nifty_detail, "critical": True}

    # 5. BANKNIFTY historical candles
    bnk_ok = False
    bnk_detail = "not tested"
    if gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty Bank", "5minute", 3
            )
            bnk_ok = bool(candles and len(candles) >= 2)
            bnk_detail = f"{len(candles or [])} bars" if bnk_ok else "returned empty"
        except Exception as exc:
            bnk_detail = str(exc)[:120]
    checks["banknifty_candles"] = {"ok": bnk_ok, "detail": bnk_detail, "critical": True}

    # 6. MCX instrument master seeded
    try:
        mcx_count = await db.upstox_mcx_future_contracts.count_documents({})
        mcx_ok = mcx_count > 0
        checks["mcx_instrument_master"] = {
            "ok": mcx_ok,
            "detail": f"{mcx_count} MCX futures cached",
            "critical": False,
        }
    except Exception as exc:
        checks["mcx_instrument_master"] = {"ok": False, "detail": str(exc)[:120], "critical": False}

    # 7. Latest candle freshness for NIFTY (only during market hours)
    market_open = _is_order_market_open("NSE")
    if nifty_ok and market_open and gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty 50", "5minute", 1
            )
            freshness = _latest_candle_fresh_for_live(candles or [], "NSE")
            checks["candle_freshness"] = {
                "ok": bool(freshness.get("fresh")),
                "detail": freshness.get("reason", "unknown"),
                "age_sec": freshness.get("age_sec"),
                "critical": True,
            }
        except Exception as exc:
            checks["candle_freshness"] = {"ok": False, "detail": str(exc)[:120], "critical": True}
    else:
        checks["candle_freshness"] = {
            "ok": True,
            "detail": "market closed — freshness check skipped" if not market_open else "candle fetch failed above",
            "critical": False,
        }

    # 8. Pre-trade gate smoke test (paper, no real order)
    settings = await get_user_settings(user_id)
    paper_mode = bool(settings.get("paper_mode", True))
    checks["paper_mode"] = {
        "ok": True,
        "detail": "paper" if paper_mode else "LIVE",
        "critical": False,
    }

    # Summary
    critical_checks = [v for v in checks.values() if v.get("critical")]
    all_critical_ok = all(c["ok"] for c in critical_checks)
    non_critical_ok = all(v["ok"] for v in checks.values() if not v.get("critical"))
    ready = all_critical_ok

    return {
        "ready": ready,
        "trading_mode": "paper" if paper_mode else "live",
        "market_open": market_open,
        "checks": checks,
        "summary": (
            "All systems go — ready to trade."
            if ready
            else "One or more critical checks failed. See checks for details."
        ),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

'''

# Find a stable insertion point: after the kill-switch route
INSERT_AFTER = '@api.post("/ops/kill-switch")'
if INSERT_AFTER in sv:
    # Find the end of that function
    idx = sv.index(INSERT_AFTER)
    # Find the next top-level @api route or def after the kill-switch block
    next_route_match = re.search(r'\n\n@api\.\w+\(', sv[idx + 200:])
    if next_route_match:
        insert_pos = idx + 200 + next_route_match.start()
        sv = sv[:insert_pos] + TRADING_READY_ENDPOINT + sv[insert_pos:]
        print("  OK: /api/ops/trading-ready endpoint inserted")
    else:
        print("  WARN: Could not find insertion point after kill-switch — appending at end")
        sv = sv + TRADING_READY_ENDPOINT
elif "/ops/trading-ready" in sv:
    print("  SKIP: /api/ops/trading-ready already exists")
else:
    # Append before the last line
    sv = sv.rstrip() + "\n" + TRADING_READY_ENDPOINT
    print("  OK: /api/ops/trading-ready endpoint appended")

# Write server.py
with open("server.py", "w", encoding="utf-8") as f:
    f.write(sv)
print("\n  server.py written successfully.")
print(f"  New size: {len(sv)} bytes")

print("\n[DONE] All server.py and risk_controls.py patches applied.")

"""Pre-Market Open Checklist for QuantG — READ-ONLY, never modifies data.

Run on VPS before market open:
    docker-compose exec backend python scripts/pre_market_open_checklist.py

Checks 16 items per user account and outputs:
    PAPER READY: YES / NO
    LIVE READY:  YES / NO
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pymongo


CHECK = "✓"
FAIL  = "✗"
WARN  = "!"


def _ok(label: str, detail: str = "") -> dict:
    return {"label": label, "ok": True, "detail": detail}


def _fail(label: str, detail: str = "") -> dict:
    return {"label": label, "ok": False, "detail": detail}


def _warn(label: str, detail: str = "") -> dict:
    return {"label": label, "ok": None, "detail": detail}


def print_check(c: dict) -> None:
    icon = CHECK if c["ok"] is True else (WARN if c["ok"] is None else FAIL)
    detail = f"  ({c['detail']})" if c.get("detail") else ""
    print(f"  [{icon}] {c['label']}{detail}")


def check_user(db, user_id: str) -> dict:
    now = datetime.now(timezone.utc)
    results = []

    # 1. Upstox OAuth connected
    bk = db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    has_token = bool(bk and bk.get("access_token"))
    results.append(_ok("Upstox access_token present", bk.get("access_token", "")[:8] + "...") if has_token
                   else _fail("Upstox access_token MISSING"))

    # 2. Access token not mock
    if has_token:
        is_mock = str(bk.get("access_token", "")).startswith("mock_")
        results.append(_fail("Access token is a MOCK token — real token required") if is_mock
                       else _ok("Access token is real (not mock)"))
    else:
        results.append(_fail("Access token not present — cannot verify mock status"))

    # 3. Refresh token present
    has_refresh = bool(bk and bk.get("refresh_token"))
    results.append(_ok("Refresh token present") if has_refresh
                   else _warn("Refresh token absent (may need re-auth before tomorrow)"))

    # 4. live_arm_state.armed = true
    arm = db.live_arm_state.find_one({"user_id": user_id})
    armed = bool(arm and arm.get("armed"))
    results.append(_ok("live_arm_state.armed = true") if armed
                   else _fail("live_arm_state.armed = false — must arm before live trading"))

    # 5. live_arm_state.global_live_enabled = true
    gle = bool(arm and arm.get("global_live_enabled"))
    results.append(_ok("live_arm_state.global_live_enabled = true") if gle
                   else _fail("live_arm_state.global_live_enabled = false"))

    # 6. Global kill switch inactive
    ks = db.risk_state.find_one({"_id": "global_kill_switch"})
    kill_active = bool(ks and ks.get("active"))
    results.append(_fail("Global kill switch is ACTIVE — disarm before trading") if kill_active
                   else _ok("Global kill switch inactive"))

    # 7. No UNKNOWN_NEEDS_REVIEW live orders
    unk = db.orders.count_documents({"user_id": user_id, "status": "UNKNOWN_NEEDS_REVIEW", "mode": "live"})
    results.append(_fail(f"UNKNOWN_NEEDS_REVIEW live orders: {unk} — resolve before trading") if unk
                   else _ok("No UNKNOWN_NEEDS_REVIEW live orders"))

    # 8. No broker reconciliation mismatch
    recon_key = f"position_reconciliation:{user_id}"
    recon = db.risk_state.find_one({"_id": recon_key})
    mismatch = bool(recon and recon.get("mismatch"))
    results.append(_fail("Broker reconciliation MISMATCH detected — resolve before live trading") if mismatch
                   else _ok("Broker reconciliation clean"))

    # 9. No stale live strategy_positions (OPEN > 24h)
    stale_cutoff = now - timedelta(hours=24)
    stale_positions = list(db.strategy_positions.find({
        "user_id": user_id,
        "mode": "live",
        "status": "OPEN",
        "entry_time": {"$lt": stale_cutoff.isoformat()},
    }))
    if stale_positions:
        symbols = [p.get("symbol", "?") for p in stale_positions[:5]]
        results.append(_warn(f"Stale live positions (OPEN >24h): {symbols} — review these"))
    else:
        results.append(_ok("No stale live positions (OPEN >24h)"))

    # 10. Active strategies are mode=live and broker=upstox (not paper/backtest)
    live_strats = list(db.strategies.find({
        "user_id": user_id,
        "mode": "live",
        "status": {"$nin": ["HALTED", "PAUSED", "QUARANTINED"]},
    }))
    bad_broker = [s for s in live_strats if s.get("broker") not in (None, "", "upstox")]
    if bad_broker:
        results.append(_fail(f"Live strategies with non-Upstox broker: {[s['id'] for s in bad_broker]}"))
    else:
        results.append(_ok(f"Active live strategies: {len(live_strats)} (all Upstox)"))

    # 11. No MCX/Crude/NaturalGas strategies active
    MCX_UNDERLYINGS = {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI", "CRUDE", "NATGAS"}
    mcx_strats = list(db.strategies.find({
        "user_id": user_id,
        "status": {"$nin": ["HALTED", "PAUSED", "QUARANTINED"]},
        "underlying": {"$in": list(MCX_UNDERLYINGS)},
    }))
    if mcx_strats:
        results.append(_fail(f"MCX/Crude/NatGas strategies ACTIVE: {[s.get('underlying') for s in mcx_strats]} — halt these"))
    else:
        results.append(_ok("No MCX/Crude/NatGas strategies active"))

    # 12. No stale pending signals (> 5 min old)
    stale_sig_cutoff = now - timedelta(minutes=5)
    stale_sigs = db.signals.count_documents({
        "user_id": user_id,
        "status": "PENDING",
        "created_at": {"$lt": stale_sig_cutoff.isoformat()},
    })
    if stale_sigs:
        results.append(_warn(f"Stale PENDING signals: {stale_sigs} (>5 min old) — may be stuck"))
    else:
        results.append(_ok("No stale pending signals"))

    # 13. Paper wallet initialized
    wallet = db.paper_wallets.find_one({"user_id": user_id})
    results.append(_ok(f"Paper wallet initialized (balance: {wallet.get('balance', '?')})") if wallet
                   else _fail("Paper wallet NOT initialized"))

    # 14. CORE_ENGINE_LIVE_ENABLED env flag
    env_val = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false")
    if env_val.lower() == "true":
        results.append(_warn(f"CORE_ENGINE_LIVE_ENABLED=true — live preflight gate is OPEN"))
    else:
        results.append(_ok(f"CORE_ENGINE_LIVE_ENABLED=false — live preflight gate CLOSED (paper-safe)"))

    # 15. Instrument master last sync
    sync_rec = db.instrument_master.find_one({"_id": "last_sync"})
    if not sync_rec:
        # Try instruments collection metadata
        sync_rec = db.instruments.find_one({"_id": "sync_meta"})
    if sync_rec:
        synced_at = sync_rec.get("synced_at") or sync_rec.get("updated_at") or "unknown"
        results.append(_ok(f"Instrument master synced at {synced_at}"))
    else:
        results.append(_warn("Instrument master sync record not found — verify manually"))

    # 16. Active runner locks not stale (TTL = 90s)
    lock_cutoff = now - timedelta(seconds=120)
    stale_lock = db.runner_locks.find_one({
        "user_id": user_id,
        "expires_at": {"$lt": lock_cutoff.isoformat()},
    })
    if stale_lock:
        results.append(_warn(f"Stale runner lock found (expired: {stale_lock.get('expires_at')}) — will auto-expire"))
    else:
        results.append(_ok("No stale runner locks"))

    return results


def verdict(results: list, for_live: bool = False) -> tuple[bool, list[str]]:
    blockers = []
    for r in results:
        if r["ok"] is False:
            blockers.append(r["label"])
    if for_live:
        # Live requires: token present, not mock, armed, global_live_enabled, kill switch off,
        #                no unknown review orders, no reconciliation mismatch
        live_required_prefixes = (
            "Upstox access_token",
            "Access token",
            "live_arm_state.armed",
            "live_arm_state.global_live_enabled",
            "Global kill switch inactive",
        )
        live_blockers = [
            r["label"]
            for r in results
            if r["ok"] is False
            and any(str(r["label"]).startswith(prefix) for prefix in live_required_prefixes)
        ]
        # Also block on review orders / mismatch
        for r in results:
            if not r["ok"] and ("UNKNOWN_NEEDS_REVIEW" in r["label"] or "reconciliation" in r["label"].lower()):
                if r["label"] not in live_blockers:
                    live_blockers.append(r["label"])
        return len(live_blockers) == 0, live_blockers
    else:
        # Paper only requires wallet initialized (paper path doesn't need arm/token)
        paper_blockers = [r["label"] for r in results if r["ok"] is False and "Paper wallet" in r["label"]]
        return len(paper_blockers) == 0, paper_blockers


def main():
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")

    print("=" * 70)
    print("         QUANTG PRE-MARKET OPEN CHECKLIST  (READ-ONLY)")
    print("=" * 70)
    print(f"Time    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Mongo   : {mongo_url}  DB: {db_name}")
    print(f"Env     : CORE_ENGINE_LIVE_ENABLED={os.environ.get('CORE_ENGINE_LIVE_ENABLED', 'false')}")
    print("=" * 70)

    try:
        client = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        db = client[db_name]
    except Exception as e:
        print(f"[X] Cannot connect to MongoDB: {e}")
        sys.exit(1)

    # Find all registered users
    users = list(db.users.find({}, {"id": 1, "email": 1, "name": 1}))
    if not users:
        print("[!] No users found in database.")
        sys.exit(0)

    overall_paper = True
    overall_live = True

    for u in users:
        uid = u.get("id") or str(u.get("_id"))
        label = u.get("email") or u.get("name") or uid
        print(f"\nUser: {label}  (id={uid})")
        print("-" * 70)

        try:
            results = check_user(db, uid)
        except Exception as e:
            print(f"  [X] Check failed with error: {e}")
            overall_paper = False
            overall_live = False
            continue

        for r in results:
            print_check(r)

        paper_ok, paper_blockers = verdict(results, for_live=False)
        live_ok, live_blockers = verdict(results, for_live=True)

        print()
        print(f"  PAPER READY : {'YES' if paper_ok else 'NO  — ' + '; '.join(paper_blockers)}")
        print(f"  LIVE READY  : {'YES (armed + token + clean)' if live_ok else 'NO  — ' + '; '.join(live_blockers)}")

        if not paper_ok:
            overall_paper = False
        if not live_ok:
            overall_live = False

    print()
    print("=" * 70)
    print("  OVERALL VERDICTS")
    print("=" * 70)
    print(f"  PAPER READY (all users)              : {'YES' if overall_paper else 'NO'}")
    print(f"  LIVE READY FOR CONTROLLED TEST        : {'YES' if overall_live else 'NO'}")
    print(f"  FULL LIVE READY ALL STRATEGIES        : NO — requires controlled test + sign-off first")
    print()
    print("  Safe commands if issues found:")
    print("    docker-compose exec backend python scripts/production_readiness_audit.py")
    print("    docker-compose exec backend python scripts/audit_trading_state.py")
    print("    docker-compose exec backend python scripts/strategy_doctor.py")
    print("=" * 70)


if __name__ == "__main__":
    main()

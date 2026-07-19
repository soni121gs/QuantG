"""IMD-03: bounded Upstox 1-minute options importer.

Fetches ONLY the contracts QuantG's intraday buyers (QG-O5..QG-O10) could trade —
NIFTY/BANKNIFTY weekly, ATM ± N strikes, CE+PE — not every strike in India. Uses
the IMD-02 resolver for the expired instrument key, the IMD-01 schema to normalize,
and the IMD-03/05 store to persist gzipped contract-days. Idempotent: a clean
existing contract-day is skipped unless --force.

The strike centre for each historical date is the EOD index close from the free
bhavcopy store (core/bhavcopy_store) — no extra API calls just to find ATM.

    python scripts/options_1m_ingest_upstox.py --from 2025-01-06 --to 2025-01-10 \
        --underlyings NIFTY --strikes-around-atm 2 --dry-run

The planner is pure logic (injected fns) so it is unit-tested with no broker.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.expired_option_resolver import ExpiredOptionResolver, OK, atm_strike, select_weekly_expiry
from core.market_domains import resolve_domain_by_underlying
from core.options_minute_schema import OptionContractRef, build_manifest, normalize_candles
from core.options_minute_store import OptionsMinuteStore

logger = logging.getLogger("quantg.options_1m_ingest")

DEFAULT_UNDERLYINGS = ["NIFTY", "BANKNIFTY"]

SpotFn = Callable[[str, str], Optional[float]]      # (underlying, date) -> spot
ExpiryFn = Callable[[str, str], Optional[str]]      # (underlying, date) -> expiry


def plan_contract_days(
    underlyings: List[str],
    dates: List[str],
    *,
    spot_fn: SpotFn,
    expiry_fn: ExpiryFn,
    resolver: ExpiredOptionResolver,
    strikes_around_atm: int,
) -> Dict[str, Any]:
    """Enumerate the exact contract-days to fetch. No broker calls beyond resolver."""
    plans: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for u in underlyings:
        u = u.upper()
        if not ExpiredOptionResolver.is_supported(u):
            skipped.append({"underlying": u, "reason": "UNSUPPORTED_OR_BLOCKED"})
            continue
        step = resolve_domain_by_underlying(u).get_strike_interval(u)
        for date in dates:
            spot = spot_fn(u, date)
            expiry = expiry_fn(u, date)
            if spot is None or not expiry:
                skipped.append({"underlying": u, "date": date, "reason": "NO_SPOT_OR_EXPIRY"})
                continue
            centre = atm_strike(u, spot)
            for k in range(-strikes_around_atm, strikes_around_atm + 1):
                strike = centre + k * step
                for opt in ("CE", "PE"):
                    res = resolver.resolve(u, date, expiry, strike, opt)
                    if res.ok and res.reason == OK:
                        plans.append({
                            "underlying": u, "date": date, "expiry": res.contract.expiry,
                            "strike": res.contract.strike, "option_type": opt,
                            "expired_instrument_key": res.contract.expired_instrument_key,
                            "instrument_key": res.contract.expired_instrument_key,
                            "lot_size": res.contract.lot_size,
                        })
                    else:
                        skipped.append({"underlying": u, "date": date, "strike": strike,
                                        "option_type": opt, "reason": res.reason})
    return {"plans": plans, "skipped": skipped, "planned_count": len(plans)}


def ingest(
    plans: List[Dict[str, Any]],
    gateway: Any,
    store: OptionsMinuteStore,
    *,
    source: str = "upstox",
    force: bool = False,
    sleep_sec: float = 0.0,
    fetched_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch, normalize and persist each planned contract-day. Idempotent."""
    fetched_at = fetched_at or datetime.now(timezone.utc).astimezone().isoformat()
    summary = {"fetched": 0, "skipped_clean": 0, "empty": 0, "errors": 0, "rows": 0}
    for p in plans:
        if not force and store.is_clean(source, p["underlying"], p["date"], p["expiry"], p["strike"], p["option_type"]):
            summary["skipped_clean"] += 1
            continue
        try:
            raw = gateway.get_expired_historical_candles_v2(
                p["expired_instrument_key"], unit="minutes", interval=1,
                from_date=p["date"], to_date=p["date"],
            )
        except Exception as err:
            logger.warning("fetch failed %s %s: %s", p["underlying"], p["date"], err)
            summary["errors"] += 1
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            continue
        if not raw:
            summary["empty"] += 1
            continue
        rows = [[c["date"], c["open"], c["high"], c["low"], c["close"], c.get("volume", 0), c.get("oi", 0)] for c in raw]
        ref = OptionContractRef(
            underlying=p["underlying"], expiry=p["expiry"], strike=p["strike"],
            option_type=p["option_type"], instrument_key=p["instrument_key"],
            expired_instrument_key=p["expired_instrument_key"],
        )
        res = normalize_candles(rows, ref, source=source, fetched_at=fetched_at)
        if not res.candles:
            summary["empty"] += 1
            continue
        manifest = build_manifest(res.candles, ref, source=source, from_date=p["date"],
                                  to_date=p["date"], fetched_at=fetched_at, flags=res.flags)
        store.write_contract_day(res.candles, manifest)
        summary["fetched"] += 1
        summary["rows"] += len(res.candles)
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    return summary


# ---- CLI wiring -------------------------------------------------------------
def _date_range(start: str, end: str, trading_days: List[str]) -> List[str]:
    return [d for d in trading_days if start <= d <= end]


def _build_real_deps(source: str):  # pragma: no cover - needs broker + DB
    from core.bhavcopy_store import BhavcopyStore
    bhav = BhavcopyStore()

    def spot_fn(u: str, date: str) -> Optional[float]:
        bars = bhav.underlying_daily(u, date, date)
        return bars[-1]["close"] if bars else None

    gateway = _gateway_from_broker_keys()
    resolver = ExpiredOptionResolver.from_gateway(
        gateway, cache_dir=os.path.join(OptionsMinuteStore().root, "_contracts_cache"))

    def expiry_fn(u: str, date: str) -> Optional[str]:
        return select_weekly_expiry(resolver.available_expiries(u), date)

    return bhav, gateway, resolver, spot_fn, expiry_fn


def _gateway_from_broker_keys():  # pragma: no cover - needs DB + crypto
    from pymongo import MongoClient
    from brokers.upstox_gateway import UpstoxGateway
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    import server  # reuse decrypt_secret + Fernet setup
    db = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017")).quantg
    bk = db.broker_keys.find_one({"broker": "upstox"}) or {}
    token = server.decrypt_secret(bk.get("access_token"))
    if not token:
        raise RuntimeError("No usable Upstox access token in broker_keys — reconnect first.")
    return UpstoxGateway(access_token=token)


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI glue
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Bounded Upstox 1-minute options importer (IMD-03)")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--underlyings", default=",".join(DEFAULT_UNDERLYINGS))
    ap.add_argument("--strikes-around-atm", type=int, default=5)
    ap.add_argument("--expiry-mode", default="weekly", choices=["weekly"])
    ap.add_argument("--source", default="upstox")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep-sec", type=float, default=0.0,
                    help="polite delay after each broker fetch/write attempt to reduce Upstox 429s")
    args = ap.parse_args(argv)

    underlyings = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]
    bhav, gateway, resolver, spot_fn, expiry_fn = _build_real_deps(args.source)
    dates = _date_range(args.start, args.end, bhav.trading_days(args.start, args.end))

    plan = plan_contract_days(underlyings, dates, spot_fn=spot_fn, expiry_fn=expiry_fn,
                              resolver=resolver, strikes_around_atm=args.strikes_around_atm)
    print(f"Planned {plan['planned_count']} contract-days across {len(dates)} trading days "
          f"({underlyings}); {len(plan['skipped'])} skipped in planning.")
    if args.dry_run:
        return 0

    store = OptionsMinuteStore()
    summary = ingest(plan["plans"], gateway, store, source=args.source, force=args.force,
                     sleep_sec=max(0.0, args.sleep_sec))
    print(f"Ingest: {summary}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

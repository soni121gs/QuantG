"""QG-O1 -> intraday only (founder-directed 2026-07-25).

QG-O1 was the ONLY strategy carrying `visual_config.options.exit_mode = "expiry"`.
That flag does two things, and the second was never true in production:

  1. position_monitor spares the spread at the 15:25 intraday square-off (EDR-11);
  2. build_credit_spread WAIVES the §21.2 theta-reachability veto for it, on the
     grounds that theta gets its full remaining life.

Until 2026-07-24 the 15:26 EOD backstop closed those spreads anyway -- 283 closed
spreads, ZERO overnight holds -- so QG-O1 was collecting the reachability waiver
while actually trading an intraday horizon. It was consequently the only strategy
able to build a spread on 2026-07-24 (1 trade out of 360 signals).

Making it genuinely intraday removes the waiver. Verified against 96 real sessions
(bhavcopy, short leg picked by Black-Scholes delta 0.30): at NIFTY 1 DTE the
current width-4 / tp-0.45 geometry clears BOTH laws in 72% of sessions, so this
does not mute the strategy on Mondays -- but it WILL now correctly stand down
Wed-Fri (4-6 DTE), where reachability is 0.30-0.44 against a 0.55 floor.

The CODE template (server.py ~3593) already says options.exit_mode "" and
risk.time_exit_minutes 300; only the DB row diverged, and ERP Phase 0 disabled
startup template sync (CLAUDE.md §20.1), so the row must be migrated explicitly.

Idempotent. Dry-run by default; pass --apply to write.
"""
import argparse
import asyncio
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

STRATEGY_ID = "f390da9d-8a38-4bf6-87d9-0e560eb852e5"
NOTE = ("QG-O1 made intraday-only (founder-directed): options.exit_mode expiry -> \"\". "
        "Drops the §21.2 reachability waiver it was silently relying on; the 15:26 EOD "
        "backstop never honoured the hold-to-expiry promise anyway (0 overnight holds "
        "in 283 closed spreads).")

TARGET = {
    "visual_config.options.exit_mode": "",
    "visual_config.risk.time_exit_minutes": 300,
    "visual_config.risk.exit_mode": "signal_or_tp_sl_trailing",
}


def _get(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


async def main(apply: bool) -> None:
    url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    db = AsyncIOMotorClient(url)[os.environ.get("DB_NAME", "quantg")]
    doc = await db.strategies.find_one({"id": STRATEGY_ID})
    if not doc:
        print("STRATEGY NOT FOUND: %s" % STRATEGY_ID)
        return

    print("strategy: %s  (status=%s)" % (doc.get("name"), doc.get("status")))
    print()
    changes = {}
    for path, want in TARGET.items():
        have = _get(doc, path)
        state = "ok" if have == want else "CHANGE"
        print("  %-46s %-28r -> %r   [%s]" % (path, have, want, state))
        if have != want:
            changes[path] = want

    if not changes:
        print("\nAlready intraday — nothing to do.")
        return

    if not apply:
        print("\nDRY RUN — %d field(s) would change. Re-run with --apply." % len(changes))
        return

    changes["geometry_changed_at"] = datetime.now(timezone.utc).isoformat()
    changes["geometry_change_note"] = NOTE
    res = await db.strategies.update_one({"id": STRATEGY_ID}, {"$set": changes})
    print("\nAPPLIED — matched=%d modified=%d" % (res.matched_count, res.modified_count))

    after = await db.strategies.find_one({"id": STRATEGY_ID})
    print("verify:")
    for path in TARGET:
        print("  %-46s %r" % (path, _get(after, path)))
    print("  geometry_changed_at %s" % after.get("geometry_changed_at"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.apply))

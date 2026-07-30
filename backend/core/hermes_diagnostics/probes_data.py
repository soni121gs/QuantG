"""Data-integrity probes — is the research data actually present and current?

The OOS judges are only as truthful as their stores. These probes report, every
day, the REAL coverage of each data store (EOD bhavcopy, 1-min index, 1-min option
chains) and flag when a store is empty or has gone stale (collection stopped) — so
a verdict is never trusted on absent data, and a broken capture/ingest cron is
surfaced the day it breaks, not months later.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from core.hermes_diagnostics.contract import Finding, Domain, Severity
from core.hermes_diagnostics.probe_sdk import register, ProbeContext

# A store is "stale" if its newest day is this many CALENDAR days behind the audit
# date (weekends/holidays give slack; a working cron never lags this much).
# Was 6, which spans a whole weekend plus two working days: on 2026-07-24 the live
# 1-min capture had been failing on a filesystem permission for days and options_1m
# sat 3 days behind, still under the bar. 3 covers a Fri->Mon gap and nothing more.
_STALE_DAYS = int(__import__("os").environ.get("HERMES_STORE_STALE_DAYS", "3"))


def _days_behind(last_day: Optional[str], today: str) -> Optional[int]:
    if not last_day:
        return None
    try:
        return (date.fromisoformat(today) - date.fromisoformat(last_day[:10])).days
    except Exception:
        return None


@register("data.store_coverage", kind="static")
async def store_coverage(ctx: ProbeContext) -> List[Finding]:
    """Report empty/stale research stores. Uses cheap dir-glob day listings, never
    the row-level coverage scan."""
    out: List[Finding] = []
    today = ctx.date_str

    stores: List[Dict[str, Any]] = []
    # EOD bhavcopy (held-to-theta judge).
    try:
        from core.bhavcopy_store import BhavcopyStore
        d = BhavcopyStore().trading_days()
        stores.append({"name": "bhavcopy_fo (EOD option settle)", "days": d,
                       "judge": "EOD OOS", "critical": True})
    except Exception as exc:  # noqa: BLE001
        out.append(_probe_note("bhavcopy_fo", exc))
    # 1-min index (intraday judge underlying).
    try:
        from core.index_minute_store import IndexMinuteStore
        d = IndexMinuteStore().trading_days()
        stores.append({"name": "index_1m (1-min underlying)", "days": d,
                       "judge": "IMD intraday OOS", "critical": True})
    except Exception as exc:  # noqa: BLE001
        out.append(_probe_note("index_1m", exc))
    # 1-min option chains (intraday judge options).
    try:
        from core.options_minute_store import OptionsMinuteStore
        d = OptionsMinuteStore().trading_days("upstox")
        stores.append({"name": "options_1m (1-min option chains)", "days": d,
                       "judge": "IMD intraday OOS", "critical": True})
    except Exception as exc:  # noqa: BLE001
        out.append(_probe_note("options_1m", exc))
    # Earnings dates (event-conditional stock-option judge).
    try:
        from core.earnings_calendar import available_days
        d = available_days()
        stores.append({"name": "earnings_dates (event calendar)", "days": d,
                       "judge": "event-conditional OOS", "critical": True})
    except Exception as exc:  # noqa: BLE001
        out.append(_probe_note("earnings_dates", exc))
    # Participant-wise F&O OI (derivatives positioning overlay).
    try:
        from core.india_flows import participant_oi_days
        d = participant_oi_days()
        stores.append({"name": "participant_oi (F&O positioning)", "days": d,
                       "judge": "participant-OI overlay", "critical": False,
                       # NSE discontinued the participant-wise OI report on 2024-07-08
                       # (§20.2). A store current up to that date is COMPLETE, not stale —
                       # there is nothing upstream left to ingest, so don't flag it.
                       "discontinued_after": "2024-07-08"})
    except Exception as exc:  # noqa: BLE001
        out.append(_probe_note("participant_oi", exc))

    for s in stores:
        days = s["days"] or []
        if not days:
            out.append(Finding(
                probe_id="data.store_coverage", domain=Domain.DATA,
                severity=Severity.HIGH if s["critical"] else Severity.MEDIUM,
                entity=s["name"],
                title=f"{s['name']}: store is EMPTY — {s['judge']} is blind",
                detail=("This research store has no data, so the OOS judge that relies "
                        "on it cannot produce a real verdict (it will correctly return "
                        "INSUFFICIENT_DATA). Backfill/ingest the store before trusting "
                        "any edge decision that needs it."),
                evidence={"store": s["name"], "days": 0, "judge": s["judge"]},
                reproduction="store.trading_days() returns []",
                suggested_fix="Run the store's ingest/backfill (bhavcopy_ingest / index_1m_ingest / options_1m_ingest) and check the ./data mount.",
            ))
            continue
        disc = s.get("discontinued_after")
        if disc:
            gap = _days_behind(days[-1], disc)
            if gap is not None and gap <= 15:
                continue  # store is current up to the discontinued source's last publication
        behind = _days_behind(days[-1], today)
        if behind is not None and behind > _STALE_DAYS:
            out.append(Finding(
                probe_id="data.store_coverage", domain=Domain.DATA, severity=Severity.MEDIUM,
                entity=s["name"],
                title=f"{s['name']}: stale — newest data {days[-1]} ({behind}d behind)",
                detail=("Daily collection appears to have stopped: the newest day in "
                        "this store is well behind today. New trades won't be judged on "
                        "current data and the capture/ingest cron may be broken."),
                evidence={"store": s["name"], "total_days": len(days),
                          "first_day": days[0], "last_day": days[-1], "days_behind": behind},
                reproduction="compare store.trading_days()[-1] to today",
                suggested_fix="Check the daily capture/ingest cron (bhavcopy freshness cron; IMD-04 live capture flush at 15:35 IST).",
            ))
    return out


def _probe_note(store: str, exc: Exception) -> Finding:
    return Finding(
        probe_id="data.store_coverage", domain=Domain.DATA, severity=Severity.LOW,
        entity=store,
        title=f"{store}: coverage unreadable ({type(exc).__name__})",
        detail="Could not read this store's coverage — the store module errored.",
        evidence={"store": store, "error": str(exc)},
        reproduction=f"instantiate the {store} store and call trading_days()",
        suggested_fix="Verify the ./data mount and the store module.",
    )


# Sibling locations a store's data has actually been found in. `data/` resolved
# relative to a different working directory is the recurring one: the container
# reads /app/data (bind-mounted from ./data at the repo ROOT), but a script run
# with cwd=backend/ writes ./data → backend/data, which nothing ever reads.
_SIBLING_DATA_ROOTS = ("/app/backend/data", "/opt/QuantG/backend/data", "backend/data", "data")


@register("data.store_path_mismatch", kind="static")
async def store_path_mismatch(ctx: ProbeContext) -> List[Finding]:
    """The data EXISTS — just not where the app reads it.

    2026-07-30: `data.store_coverage` had been correctly reporting bhavcopy_fo /
    index_1m / options_1m as EMPTY for up to 13 days. It was right, and the reason
    was invisible to it: 102 MB of bhavcopy (2019-2023, 1,234 days) plus 1,359
    participant-OI files were sitting in `/opt/QuantG/backend/data/`, while the
    container's bind mount maps the repo-root `./data` to `/app/data`. A store that
    is empty because ingest never ran and a store that is empty because the files
    are one directory away need completely different fixes, and "EMPTY" alone does
    not distinguish them — so the finding read as a backfill chore and sat there.

    This probe answers the question the coverage probe cannot: is there data
    nearby? Cheap (a few globs), and only speaks when the answer is yes.
    """
    import glob
    import os

    out: List[Finding] = []
    roots: List[Dict[str, Any]] = []
    for mod, attr, label, pattern in (
        ("core.bhavcopy_store", "STORE_ROOT", "bhavcopy_fo", "*/*.csv.gz"),
        ("core.index_minute_store", "STORE_ROOT", "index_1m", "**/*.csv.gz"),
        ("core.options_minute_store", "STORE_ROOT", "options_1m", "**/*.csv.gz"),
    ):
        try:
            m = __import__(mod, fromlist=[attr])
            roots.append({"label": label, "root": getattr(m, attr), "pattern": pattern})
        except Exception:  # noqa: BLE001
            continue

    for r in roots:
        try:
            live = len(glob.glob(os.path.join(r["root"], r["pattern"]), recursive=True))
        except Exception:  # noqa: BLE001
            continue
        if live > 0:
            continue  # the store the app reads has data — nothing to say
        leaf = os.path.basename(str(r["root"]).rstrip("/"))
        for sib in _SIBLING_DATA_ROOTS:
            cand = os.path.join(sib, leaf)
            if os.path.abspath(cand) == os.path.abspath(str(r["root"])):
                continue
            try:
                found = len(glob.glob(os.path.join(cand, r["pattern"]), recursive=True))
            except Exception:  # noqa: BLE001
                found = 0
            if found <= 0:
                continue
            out.append(Finding(
                probe_id="data.store_path_mismatch", domain=Domain.DATA,
                severity=Severity.CRITICAL, entity=r["label"],
                title=(f"{r['label']}: {found} data files exist at {cand} but the app "
                       f"reads {r['root']} (which is EMPTY)"),
                detail=("The judges are blind even though the data was downloaded. This is "
                        "a PATH problem, not a backfill problem: a script run with cwd set "
                        "to backend/ resolves a relative 'data/' path to backend/data, "
                        "while the container bind-mounts the repo-root ./data to /app/data. "
                        "Moving the files into the store root fixes it instantly — but "
                        "check freshness first: a store whose newest day is months old is "
                        "worse than an empty one, because the entry gate will believe it."),
                evidence={"store": r["label"], "store_root": r["root"],
                          "files_in_store_root": live, "sibling_path": cand,
                          "files_at_sibling": found},
                reproduction=(f"glob {r['root']}/{r['pattern']} -> 0 files; "
                              f"glob {cand}/{r['pattern']} -> {found} files"),
                suggested_fix=(f"Move {cand}/* into {r['root']}/ and chown to the container "
                               "user (uid 999), then confirm the newest day is current "
                               "before trusting any gate that reads it."),
            ))
            break
    return out

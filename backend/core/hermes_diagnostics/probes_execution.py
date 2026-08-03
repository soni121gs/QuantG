"""Execution-integrity probes — trade-logic invariants.

These are the permanent regression guards for the three loopholes that cost
₹6,821 on 2026-07-17. Each one is a deterministic check against the day's real
positions/signals; once a bug is caught here it is caught forever.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.hermes_diagnostics.contract import Finding, Domain, Severity, Confidence
from core.hermes_diagnostics.probe_sdk import register, ProbeContext

_PRICE_EXIT_REASONS = {"spread-tp", "spread-sl", "trail-lock", "dynamic-exit"}
_SPREAD_STRUCTS = {"credit_spread", "debit_spread"}


def _short_leg(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for l in (pos.get("legs") or []):
        if l.get("role") == "short" or l.get("side") == "SELL":
            return l
    return None


def _width(pos: Dict[str, Any]) -> Optional[float]:
    # Premium-terms width = net_credit + max_loss (max_loss = width − credit).
    nc, ml = pos.get("net_credit"), pos.get("max_loss")
    if nc is None or ml is None:
        return None
    try:
        return float(nc) + float(ml)
    except (TypeError, ValueError):
        return None


def _spreads_today(ctx: ProbeContext) -> List[Dict[str, Any]]:
    return [p for p in ctx.closed_today if str(p.get("structure")) in _SPREAD_STRUCTS]


@register("exec.intent_vs_execution_side", kind="dynamic")
async def intent_vs_execution_side(ctx: ProbeContext) -> List[Finding]:
    """RC-1 guard. A credit spread's SHORT side must match the strategy's intent:
    action BUY → bullish → short PE (bull-put); action SELL → bearish → short CE
    (bear-call). A mismatch means the contract selector overrode the strategy's
    directional decision (sold CE into a rally on 2026-07-17, −₹3,810)."""
    out: List[Finding] = []
    sig_by_id = {s.get("id"): s for s in ctx.signals_today}
    for pos in _spreads_today(ctx):
        if str(pos.get("structure")) != "credit_spread":
            continue
        short = _short_leg(pos)
        if not short or not short.get("option_type"):
            continue
        sig = sig_by_id.get(pos.get("signal_id"))
        action = (sig or {}).get("action")
        if action not in ("BUY", "SELL"):
            continue  # can't establish intent → say nothing (never guess)
        expected = "PE" if action == "BUY" else "CE"
        actual = str(short.get("option_type"))
        if actual != expected:
            out.append(Finding(
                probe_id="exec.intent_vs_execution_side",
                domain=Domain.EXECUTION, severity=Severity.CRITICAL,
                entity=str(pos.get("strategy_id") or pos.get("symbol")),
                title=f"Spread sold the WRONG side: intent {expected}, executed {actual}",
                detail=("The strategy's action implies a "
                        f"{'bull-put (short PE)' if expected=='PE' else 'bear-call (short CE)'} "
                        f"but the built spread is short {actual}. The contract selector "
                        "overrode the strategy's directional decision — selling the wrong "
                        "side is an automatic loss when the market moves as the strategy expected."),
                evidence={
                    "position_id": pos.get("id"), "symbol": pos.get("symbol"),
                    "signal_action": action, "entry_reason": (sig or {}).get("entry_reason"),
                    "expected_short": expected, "actual_short": actual,
                    "short_strike": short.get("strike"), "realized_pnl": pos.get("realized_pnl"),
                },
                reproduction=("db.strategy_positions.findOne({id:'%s'}).legs vs "
                              "db.signals.findOne({id:'%s'}).action"
                              % (pos.get("id"), pos.get("signal_id"))),
                suggested_fix="core/dynamic_contract_selector.select_dynamic_credit_spread — preferred_direction must be a hard side gate.",
            ))
    return out


@register("exec.exit_reason_mix", kind="dynamic")
async def exit_reason_mix(ctx: ProbeContext) -> List[Finding]:
    """RC-2/RC-3 guard. If a meaningful number of spreads closed today but ~none
    exited on a PRICE trigger (TP/SL/trailing) — every exit was a timer or the
    15:25 bell — the stop engine is effectively dead (either marks aren't live or
    the thresholds can't be reached). This is the single highest-value probe: it
    is exactly the signal that was sitting in plain sight on 2026-07-17."""
    spreads = _spreads_today(ctx)
    n = len(spreads)
    if n < 4:
        return []  # too few closes to assert anything — INSUFFICIENT_DATA, stay silent
    dist: Dict[str, int] = {}
    price_exits = 0
    for p in spreads:
        reason = str(p.get("exit_reason") or "unknown")
        dist[reason] = dist.get(reason, 0) + 1
        if reason in _PRICE_EXIT_REASONS:
            price_exits += 1
    if price_exits == 0:
        return [Finding(
            probe_id="exec.exit_reason_mix", domain=Domain.EXECUTION,
            severity=Severity.CRITICAL, entity="spread-book",
            title=f"Stop engine produced 0 price-based exits across {n} spreads today",
            detail=("Not one spread exited on TP, SL or trailing-lock — every close "
                    "was a time-exit or the 15:25 square-off. The take-profit / "
                    "stop-loss / trailing engine is not protecting positions: either "
                    "spread legs aren't being live-marked, or the thresholds sit "
                    "beyond reach. Losses ride to a time/bell exit at full damage."),
            evidence={"closed_spreads": n, "price_exits": 0, "exit_distribution": dist},
            reproduction=("db.strategy_positions.aggregate([{$match:{structure:{$in:['credit_spread','debit_spread']},"
                          "status:'CLOSED',created_at:{$regex:'^%s'}}},{$group:{_id:'$exit_reason',n:{$sum:1}}}])"
                          % ctx.date_str),
            suggested_fix="position_monitor._process_spread_position (live marks + time-exit gate) and spread_lifecycle.compute_exit_levels (stop reachability).",
        )]
    return []


@register("exec.no_op_stop", kind="dynamic")
async def no_op_stop(ctx: ProbeContext) -> List[Finding]:
    """RC-2 guard. A spread's stop value must sit BELOW its max-loss value (width);
    at/above width the 'stop' can never trigger before expiry-max — a cosmetic
    stop. Checks the actual SL levels stamped on today's spreads."""
    out: List[Finding] = []
    seen: set = set()
    for pos in _spreads_today(ctx):
        sid = str(pos.get("strategy_id"))
        if sid in seen:
            continue
        sl = pos.get("spread_sl_value")
        width = _width(pos)
        if sl is None or width is None or width <= 0:
            continue
        if float(sl) >= float(width) - 0.01:
            seen.add(sid)
            out.append(Finding(
                probe_id="exec.no_op_stop", domain=Domain.EXECUTION,
                severity=Severity.HIGH, entity=sid,
                title="Stop-loss is set at (or beyond) max loss — a no-op stop",
                detail=("spread_sl_value ≥ width, so the stop equals the spread's own "
                        "maximum loss and can never fire before expiry-max. The stop "
                        "provides no protection; the position can only exit on a timer "
                        "or square-off."),
                evidence={"strategy_id": sid, "spread_sl_value": sl, "width": width,
                          "net_credit": pos.get("net_credit"), "max_loss": pos.get("max_loss")},
                reproduction="compare spread_sl_value vs (net_credit+max_loss) on the position doc",
                suggested_fix="spread_lifecycle.compute_exit_levels — cap sl_value strictly below width (SPREAD_SL_MAX_FRAC_OF_WIDTH).",
            ))
    return out


@register("exec.specialist_regime_fit", kind="dynamic")
async def specialist_regime_fit(ctx: ProbeContext) -> List[Finding]:
    """A regime specialist must trade only in the regime(s) it owns (RAE §18.4).
    Flags entries taken outside the strategy's owned_regimes — e.g. a RANGE seller
    that opened on TREND_UP (2026-07-17)."""
    out: List[Finding] = []
    reported: set = set()
    for pos in ctx.closed_today:
        sid = str(pos.get("strategy_id"))
        strat = ctx.strategy_by_id(sid) or {}
        owned = (strat.get("owned_regimes")
                 or ((strat.get("visual_config") or {}).get("options") or {}).get("owned_regimes")
                 or [])
        if not owned:
            continue
        entry_regime = (pos.get("regime_at_entry")
                        or (pos.get("regime_snapshot") or {}).get("regime"))
        if not entry_regime or entry_regime in owned:
            continue
        if sid in reported:
            continue
        reported.add(sid)
        out.append(Finding(
            probe_id="exec.specialist_regime_fit", domain=Domain.EXECUTION,
            severity=Severity.MEDIUM, entity=sid,
            title=f"Specialist traded off-regime: entered on {entry_regime}, owns {owned}",
            detail=("A regime specialist is designed to stand down outside its owned "
                    "regime(s). It entered in a regime it does not own — either its own "
                    "entry filter fires off-regime or the classifier/router mislabelled "
                    "the day. Off-regime entries are the ensemble's blind spot."),
            evidence={"strategy_id": sid, "entry_regime": entry_regime,
                      "owned_regimes": owned, "symbol": pos.get("symbol"),
                      "realized_pnl": pos.get("realized_pnl")},
            reproduction="compare position.regime_at_entry vs strategy.owned_regimes",
            suggested_fix="Router/classifier regime gating, or the strategy's entry filter (seed_regime_specialists).",
        ))
    return out


@register("exec.spread_mark_staleness", kind="dynamic")
async def spread_mark_staleness(ctx: ProbeContext) -> List[Finding]:
    """RC-3 latent guard (intraday). An OPEN spread whose mark isn't advancing
    during market hours means the exit engine is blind on it — the failure that
    let a spread ride to the bell. Silent at EOD (no open positions)."""
    if not ctx.in_market_hours:
        return []
    from datetime import datetime, timezone
    from core.position_lifecycle import parse_iso_dt
    out: List[Finding] = []
    now = datetime.now(timezone.utc)
    for pos in ctx.open_positions:
        if str(pos.get("structure")) not in _SPREAD_STRUCTS:
            continue
        tick = parse_iso_dt(pos.get("last_tick_at") or pos.get("last_fresh_tick_at"))
        if tick is None:
            age = None
        else:
            age = (now - tick).total_seconds()
        if age is None or age > 180:
            out.append(Finding(
                probe_id="exec.spread_mark_staleness", domain=Domain.EXECUTION,
                severity=Severity.HIGH, entity=str(pos.get("id")),
                title="Open spread mark is stale — exit engine may be blind",
                detail=("This spread's last live mark is stale during market hours. "
                        "Price-based SL/TP/trailing only evaluate on live marks, so a "
                        "stale mark means the position can only exit on a timer or the "
                        "bell — the RC-3 failure mode. Check leg WS subscription."),
                evidence={"position_id": pos.get("id"), "symbol": pos.get("symbol"),
                          "last_tick_at": pos.get("last_tick_at"), "mark_age_sec": age,
                          "legs": [l.get("instrument_key") for l in (pos.get("legs") or [])]},
                reproduction="check last_tick_at advancing on OPEN spread positions",
                suggested_fix="Subscribe legs[].instrument_key to the WS feed at intraday open; position_monitor._leg_ltp.",
            ))
    return out


@register("exec.structure_mismatch", kind="dynamic")
async def structure_mismatch(ctx: ProbeContext) -> List[Finding]:
    """CRITICAL guard (2026-07-23). A strategy that DECLARES a spread structure must
    only ever hold spread positions. A SINGLE-LEG position under a credit/debit-spread
    strategy means the spread build failed (a geometry veto — cost-floor / reachability
    — or a thin chain) and the signal degraded to a NAKED directional option instead of
    standing down: a defined-risk seller turned into the #1 dead strategy (option buyers
    dead across 5 studies). This is exactly what the P5 reachability veto exposed — 7
    naked single-leg buys opened by credit-spread sellers in one morning. Deterministic:
    today's opened positions vs the owning strategy's declared structure."""
    out: List[Finding] = []
    by_strat: Dict[str, Dict[str, Any]] = {}
    for pos in list(ctx.closed_today) + list(ctx.open_positions):
        strat = ctx.strategy_by_id(str(pos.get("strategy_id") or "")) or {}
        declared = str(
            ((strat.get("visual_config") or {}).get("options") or {}).get("structure") or ""
        ).lower()
        if declared not in _SPREAD_STRUCTS:
            continue
        if str(pos.get("structure") or "single_leg").lower() in _SPREAD_STRUCTS:
            continue  # correctly a spread
        sid = str(pos.get("strategy_id"))
        agg = by_strat.setdefault(
            sid, {"name": strat.get("name"), "declared": declared, "symbols": [], "count": 0})
        agg["count"] += 1
        sym = pos.get("target_symbol")
        if sym and sym not in agg["symbols"]:
            agg["symbols"].append(sym)
    for sid, agg in by_strat.items():
        out.append(Finding(
            probe_id="exec.structure_mismatch", domain=Domain.EXECUTION,
            severity=Severity.CRITICAL, entity=sid,
            title=f"{agg['declared']} strategy opened {agg['count']} naked single-leg position(s) today",
            detail=("A strategy declared as a defined-risk spread produced naked single-leg "
                    "option positions. This happens when the spread build fails and the "
                    "signal path degrades to a single-leg order instead of standing down, "
                    "turning a defined-risk seller into a naked directional buyer (the #1 "
                    "dead strategy). A spread strategy must stand down when its spread cannot "
                    "be built — never place a single leg."),
            evidence={"strategy_id": sid, "name": agg["name"],
                      "declared_structure": agg["declared"],
                      "naked_single_leg_count": agg["count"], "symbols": agg["symbols"][:10]},
            reproduction=("positions opened today WHERE strategy.visual_config.options.structure "
                          "in (credit_spread,debit_spread) AND position.structure is single_leg"),
            suggested_fix=("signal_manager: a spread-declared strategy must return SKIPPED "
                           "(SPREAD_BUILD_FAILED) when no spread payload is present, never fall "
                           "through to the single-leg path."),
        ))
    return out


@register("exec.unexplained_skips", kind="dynamic")
async def unexplained_skips(ctx: ProbeContext) -> List[Finding]:
    """Observability regression guard (2026-07-23). A signal skipped with NO recorded
    reason is invisible on the dashboard — the strategy looks silently idle. QG-O1 was
    skipped 18x in one session with last_filter_reason '(none)', hiding both a
    legitimate gate (RES-2) and an over-block (router). Every skip MUST carry a
    reason_code + human_reason. Flags a strategy with skipped signals today whose reason
    is blank/unrecorded."""
    by_strat: Dict[str, int] = {}
    for g in ctx.signals_today:
        if str(g.get("status")) not in ("SKIPPED_SIGNAL", "SKIPPED"):
            continue
        rc = g.get("rejection_reason") or (g.get("rejection_detail") or {}).get("reason_code")
        hr = (g.get("rejection_detail") or {}).get("human_reason")
        if rc and hr:
            continue  # properly recorded — nothing to flag
        sid = str(g.get("strategy_id") or "")
        by_strat[sid] = by_strat.get(sid, 0) + 1
    out: List[Finding] = []
    for sid, n in by_strat.items():
        if n < 3:
            continue
        strat = ctx.strategy_by_id(sid) or {}
        out.append(Finding(
            probe_id="exec.unexplained_skips", domain=Domain.EXECUTION,
            severity=Severity.HIGH, entity=sid,
            title=f"{n} signals skipped today with NO recorded reason",
            detail=("Signals were skipped without a reason_code or human_reason, so the "
                    "strategy shows no explanation for being idle — it looks silently "
                    "stuck when it may be legitimately gated or wrongly over-blocked. "
                    "Every skip must record why, on the signal and on the strategy's "
                    "last_filter_reason, or a real block hides in plain sight."),
            evidence={"strategy_id": sid, "name": strat.get("name"), "unexplained_skips": n},
            reproduction=("signals today status SKIPPED_SIGNAL WHERE rejection_reason is blank "
                          "AND rejection_detail.human_reason is blank"),
            suggested_fix=("signal_manager dispatch boundary must set rejection_reason + "
                           "rejection_detail.human_reason AND strategy.last_filter_reason on "
                           "every skip path."),
        ))
    return out


@register("exec.exit_below_cost_floor", kind="dynamic")
async def exit_below_cost_floor(ctx: ProbeContext) -> List[Finding]:
    """§21 law-reconciliation guard (2026-07-29).

    The cost-floor law lets a spread be BUILT only when it can bank
    SPREAD_COST_FLOOR_MULT x round-trip friction. Nothing checked that the EXIT
    engine honoured that promise, and it did not: over 2026-07-27..29, 16 of 22
    trades exited on `trail-lock` averaging Rs284 — below the ~Rs300 friction of
    the trade itself — while the losing side paid a full Rs1,669 stop. 82% of
    trades won and the book still lost money.

    A winning exit that banks less than the trade cost to place is a loss wearing
    a green label, so this probe judges REALIZED wins against the same function
    the builder enforces. Same defect class as §22.3 and §21.5: a law encoded at
    one end of the trade and ignored at the other.
    """
    from core.spread_builder import round_trip_friction

    wins = [p for p in _spreads_today(ctx)
            if str(p.get("structure")) == "credit_spread"
            and float(p.get("realized_pnl") or 0) > 0]
    if len(wins) < 4:
        return []  # thin sample → silence, never a guess (§19)

    short_changed: List[Dict[str, Any]] = []
    for p in wins:
        legs = p.get("legs") or []
        try:
            prem = sum(float(l.get("entry_price") or l.get("premium") or 0) for l in legs)
        except (TypeError, ValueError):
            prem = 0.0
        # 1x REAL friction, not the builder's ex-ante 3x: the unambiguous
        # question ex-post is whether the win covered what the trade cost to
        # place. Judging exits by the 3x design criterion would fire on almost
        # every trade and become noise (§19: a probe that cries wolf is muted).
        floor = round_trip_friction(prem or None, p.get("lot_size")) * int(p.get("lots") or 1)
        pnl = float(p.get("realized_pnl") or 0)
        if pnl < floor:
            short_changed.append({
                "position_id": p.get("id"), "strategy_id": p.get("strategy_id"),
                "symbol": p.get("symbol"), "exit_reason": p.get("exit_reason"),
                "realized_pnl": round(pnl, 2), "friction_paid": round(floor, 2),
                "peak_pnl": round(float(p.get("peak_pnl") or 0), 2),
            })
    if not short_changed:
        return []
    frac = len(short_changed) / len(wins)
    if frac < 0.5:
        return []
    avg = sum(r["realized_pnl"] for r in short_changed) / len(short_changed)
    avg_floor = sum(r["friction_paid"] for r in short_changed) / len(short_changed)
    return [Finding(
        probe_id="exec.exit_below_cost_floor", domain=Domain.EXECUTION,
        severity=Severity.HIGH, entity="spread-book",
        title=(f"{len(short_changed)} of {len(wins)} winning spreads banked LESS than "
               f"they cost to place (avg Rs{avg:.0f} vs Rs{avg_floor:.0f} friction)"),
        detail=("These exits are labelled green but did not cover their own round-trip "
                "friction, so they are losses wearing a win's colour. The cost-floor law "
                "approved the trades on the basis of banking 3x this number. When "
                "wins are this small the book needs an implausible win rate to break "
                "even — 2026-07-27..29 won 82% of trades and still lost money. Check "
                "the trailing-lock arm/giveback against dynamic_exit.TRAIL_HONOUR_COST_FLOOR."),
        evidence={"winners": len(wins), "below_floor": len(short_changed),
                  "fraction": round(frac, 3), "avg_realized": round(avg, 2),
                  "avg_friction_paid": round(avg_floor, 2), "positions": short_changed[:10]},
        reproduction=("closed credit_spread positions today WHERE realized_pnl > 0 "
                      "AND realized_pnl < spread_builder.round_trip_friction(legs, lot_size) * lots"),
        suggested_fix=("Raise DYN_EXIT_TRAIL_ARM_COST_MULT / lower the giveback so the lock "
                       "cannot trigger below real friction. If winners genuinely cannot reach "
                       "it, the GEOMETRY is wrong (credit too thin for the width), not the exit."),
    )]


@register("exec.squareoff_after_segment_close", kind="dynamic")
async def squareoff_after_segment_close(ctx: ProbeContext) -> List[Finding]:
    """A position must never be squared off AFTER its own exchange has closed.

    2026-08-03 (SEBI session change, §25.1): NSE derivatives moved to a 15:40 close
    while BSE stayed at 15:30, but the spread square-off was a SINGLE global minute
    derived from the NSE close — 15:35, five minutes after BSE had shut. A SENSEX
    spread would have been closed against a stale mark in paper and rejected outright
    in live. The previous 15:25 value hid it because it preceded BOTH closes, so the
    bug shipped and armed itself on the day the session changed.

    That is the general hazard: a single global time is correct only while every
    segment shares a close. This probe reads the REALISED close timestamp against the
    position's OWN segment window, so any future divergence (BSE following NSE, a
    muhurat session, another SEBI phase) is caught by measurement rather than by
    someone remembering to update a constant.
    """
    from datetime import datetime, timedelta, timezone

    import session_times

    late: List[Dict[str, Any]] = []
    for p in ctx.closed_today:
        closed_at = p.get("closed_at") or p.get("exit_time")
        if not closed_at:
            continue
        try:
            dt = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist = dt.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
        minute = ist.hour * 60 + ist.minute
        underlying = p.get("underlying") or str(p.get("target_symbol") or "").split(" ")[0]
        segment = session_times.segment_for_underlying(underlying)
        close_m = session_times.close_minute_for(segment)
        if minute <= close_m:
            continue
        late.append({
            "position_id": p.get("id"), "strategy_id": p.get("strategy_id"),
            "underlying": underlying, "segment": segment,
            "closed_at_ist": f"{ist.hour:02d}:{ist.minute:02d}",
            "segment_close": session_times.hhmm_str(close_m),
            "exit_reason": p.get("exit_reason"),
            "realized_pnl": round(float(p.get("realized_pnl") or 0), 2),
        })
    if not late:
        return []
    return [Finding(
        probe_id="exec.squareoff_after_segment_close", domain=Domain.EXECUTION,
        severity=Severity.HIGH, entity="session-times",
        title=(f"{len(late)} position(s) closed AFTER their exchange had shut "
               f"({late[0]['underlying']} {late[0]['closed_at_ist']} vs "
               f"{late[0]['segment']} close {late[0]['segment_close']})"),
        detail=("The exit ran past the segment close, so its mark came from a market "
                "that was no longer trading — the P&L booked against it is stale in "
                "paper and the order would be rejected in live. NSE F&O and BSE F&O no "
                "longer share a close (15:40 vs 15:30), so any square-off, force-close "
                "or backstop sweep must take its minute from the position's OWN segment."),
        evidence={"count": len(late), "positions": late[:10]},
        reproduction=("closed positions today WHERE closed_at (IST) > "
                      "session_times.close_minute_for(segment_for_underlying(underlying))"),
        suggested_fix=("Use session_times.spread_squareoff_minute_for(segment) / "
                       "close_minute_for(segment) — never a single global constant "
                       "derived from one exchange's close."),
    )]

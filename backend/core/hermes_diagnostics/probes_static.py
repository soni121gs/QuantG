"""Static probes — config & logic invariants.

These need NO trades. They run against strategy configs and catch structural
defects (bad exit geometry, inconsistent specialist tags) before a losing day
ever exercises them. Highest value-per-risk in the whole system.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from core.market_domains import contract_spec_for_underlying
from core.hermes_diagnostics.contract import Finding, Domain, Severity
from core.hermes_diagnostics.probe_sdk import register, ProbeContext

_DEF_TP = float(os.environ.get("CREDIT_SPREAD_TP_FRAC", "0.5"))
_DEF_SL = float(os.environ.get("CREDIT_SPREAD_SL_MULT", "2.0"))
# A seller can run a wide stop IF its win rate is high, so this is a WARNING
# threshold, not a hard error: flag when the break-even win rate implied by the
# geometry exceeds this. R:R (profit:loss units) = tp_frac : sl_mult →
# break-even WR = sl_mult / (sl_mult + tp_frac).
_BE_WR_WARN = float(os.environ.get("HERMES_GEOMETRY_BE_WR_WARN", "0.75"))
_COST_FLOOR_MULT = float(os.environ.get("HERMES_COST_FLOOR_MULT", "3.0"))
_DEFAULT_ROUND_TRIP_COST = float(os.environ.get("HERMES_DEFAULT_ROUND_TRIP_COST_PER_LOT", "85.0"))


def _opts(strat: Dict[str, Any]) -> Dict[str, Any]:
    return ((strat.get("visual_config") or {}).get("options") or {})


def _is_active(strat: Dict[str, Any]) -> bool:
    return str(strat.get("status")) in ("live", "active", "paused")  # paused RAE rows still fire via allowlist


def _underlying(strat: Dict[str, Any]) -> str:
    visual = strat.get("visual_config") or {}
    opts = visual.get("options") or {}
    return str(opts.get("underlying") or visual.get("symbol") or strat.get("underlying") or "").upper()


def _round_trip_cost(strat: Dict[str, Any]) -> float:
    o = _opts(strat)
    for key in ("modeled_round_trip_cost", "round_trip_cost_per_lot", "friction_per_lot"):
        try:
            value = float(o.get(key))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return _DEFAULT_ROUND_TRIP_COST


@register("static.reward_risk_geometry", kind="static")
async def reward_risk_geometry(ctx: ProbeContext) -> List[Finding]:
    """Flag credit-spread strategies whose TP/SL geometry needs an implausibly
    high win rate to break even (the 1:4.3 reward:risk trap — book +0.35×credit,
    risk 1.5×credit → need ~81% WR)."""
    out: List[Finding] = []
    for strat in ctx.strategies:
        o = _opts(strat)
        if str(o.get("structure")) != "credit_spread" or not _is_active(strat):
            continue
        tp = o.get("credit_tp_frac")
        sl = o.get("credit_sl_mult")
        tp = float(tp) if tp is not None else _DEF_TP
        sl = float(sl) if sl is not None else _DEF_SL
        if tp <= 0 or sl <= 0:
            continue
        be_wr = sl / (sl + tp)
        if be_wr >= _BE_WR_WARN:
            out.append(Finding(
                probe_id="static.reward_risk_geometry", domain=Domain.STRATEGY,
                severity=Severity.HIGH, entity=str(strat.get("id")),
                title=f"Exit geometry needs {be_wr*100:.0f}% win rate to break even",
                detail=("This credit spread books a small fraction of credit as profit "
                        "but risks a large multiple as loss (TP {:.2f}×credit vs SL "
                        "{:.2f}×credit). The structural break-even win rate is very high; "
                        "unless the strategy genuinely wins that often, expectancy is "
                        "negative regardless of the signal.").format(tp, sl),
                evidence={"strategy_id": strat.get("id"), "name": strat.get("name"),
                          "credit_tp_frac": tp, "credit_sl_mult": sl,
                          "reward_risk": f"{tp:.2f} : {sl:.2f}",
                          "breakeven_win_rate": round(be_wr, 3)},
                reproduction="visual_config.options.credit_tp_frac / credit_sl_mult on the strategy doc",
                suggested_fix="Re-derive TP/SL vs the strategy's actual OOS win rate; do not deploy negative-expectancy geometry (CLAUDE.md §13.4).",
            ))
    return out


@register("static.specialist_tag_consistency", kind="static")
async def specialist_tag_consistency(ctx: ProbeContext) -> List[Finding]:
    """A RAE specialist must carry BOTH a role and the regime(s) it owns; a half-
    tagged strategy either never gets routed or gets routed with no regime gate."""
    out: List[Finding] = []
    for strat in ctx.strategies:
        o = _opts(strat)
        role = strat.get("specialist_role") or o.get("specialist_role")
        owned = strat.get("owned_regimes") or o.get("owned_regimes")
        if not role and not owned:
            # 2026-07-21 blind-spot fix: a FULLY-untagged spread is not "not a
            # specialist" — signal_manager defaults it to 'range_seller', so it is
            # silently gated as a premium seller whatever it actually is. That is how
            # the IDX long-gamma sleeve ended up permitted only on calm days (0 trades
            # ever). Flag untagged spreads instead of skipping them.
            if str(o.get("structure")) in ("credit_spread", "debit_spread"):
                out.append(Finding(
                    probe_id="static.specialist_tag_consistency", domain=Domain.STRATEGY,
                    severity=Severity.MEDIUM, entity=str(strat.get("id")),
                    title="Spread has no specialist_role — silently routed as range_seller",
                    detail=("This spread declares neither `specialist_role` nor "
                            "`owned_regimes`, so signal_manager falls back to the "
                            "'range_seller' default and the RAE router gates it to "
                            "RANGE/INSIDE. If the strategy is not a premium seller "
                            "(e.g. long-gamma, mean-reversion, trend), it is being "
                            "activated in the wrong regime and stood down in its own."),
                    evidence={"strategy_id": strat.get("id"), "name": strat.get("name"),
                              "structure": o.get("structure"),
                              "specialist_role": None, "owned_regimes": None,
                              "routed_as": "range_seller (default)"},
                    reproduction="strategies where visual_config.options.specialist_role is missing and structure is a spread",
                    suggested_fix="Set both specialist_role and owned_regimes (see core/index_alpha_sleeves._ROLE_BY_MODE).",
                ))
            continue
        if bool(role) != bool(owned):
            out.append(Finding(
                probe_id="static.specialist_tag_consistency", domain=Domain.STRATEGY,
                severity=Severity.MEDIUM, entity=str(strat.get("id")),
                title="Specialist tag is half-set (role/owned_regimes mismatch)",
                detail=("A regime specialist needs both `specialist_role` and "
                        "`owned_regimes` for the router to activate it on the right "
                        "days and stand it down elsewhere. One is set without the other, "
                        "so regime gating is undefined for this strategy."),
                evidence={"strategy_id": strat.get("id"), "name": strat.get("name"),
                          "specialist_role": role, "owned_regimes": owned},
                reproduction="strategy.specialist_role and strategy.owned_regimes",
                suggested_fix="Set both fields (seed_regime_specialists) or clear both.",
            ))
    return out


@register("static.spread_capital_sanity", kind="static")
async def spread_capital_sanity(ctx: ProbeContext) -> List[Finding]:
    """A defined-risk spread whose required_capital is smaller than a single lot's
    max loss can never size — it silently trades 0 lots or mis-sizes."""
    out: List[Finding] = []
    for strat in ctx.strategies:
        o = _opts(strat)
        if str(o.get("structure")) not in ("credit_spread", "debit_spread") or not _is_active(strat):
            continue
        rc = o.get("required_capital")
        if rc is None:
            continue
        try:
            rc = float(rc)
        except (TypeError, ValueError):
            continue
        if rc <= 0:
            out.append(Finding(
                probe_id="static.spread_capital_sanity", domain=Domain.STRATEGY,
                severity=Severity.MEDIUM, entity=str(strat.get("id")),
                title="Spread required_capital is zero/invalid — cannot size",
                detail="lots_for_risk divides the risk budget by per-lot max loss; a "
                       "non-positive budget yields 0 lots (the strategy never trades) "
                       "or a mis-size.",
                evidence={"strategy_id": strat.get("id"), "name": strat.get("name"),
                          "required_capital": rc},
                reproduction="visual_config.options.required_capital",
                suggested_fix="Set required_capital ≥ one lot's max loss for the underlying.",
            ))
    return out


async def _realized_credit_per_lot(db: Any, user_id: str, strat: Dict[str, Any]):
    """REAL gross credit/profit per lot from recent closed spreads — deterministic
    evidence, not a config guess. Returns (per_lot_rupees, sample_size) or None when
    there is no history to judge (so the probe can stay silent — §19)."""
    struct = str(_opts(strat).get("structure"))
    field = {"credit_spread": "net_credit", "debit_spread": "max_profit"}.get(struct)
    if not field:
        return None  # cost-floor credit test is only meaningful for spreads
    try:
        cur = db.strategy_positions.find(
            {"user_id": user_id, "strategy_id": str(strat.get("id")),
             "status": "CLOSED", field: {"$gt": 0}},
            {field: 1},
        ).sort("closed_at", -1).limit(40)
        vals = [float(p[field]) async for p in cur if p.get(field)]
    except Exception:  # noqa: BLE001
        return None
    if len(vals) < 3:
        return None  # thin evidence -> silence, never a HIGH finding on a guess
    per_unit = sum(vals) / len(vals)
    try:
        spec = contract_spec_for_underlying(_underlying(strat)) or {}
        lot = float(spec.get("lot_size") or 0) or 1.0
    except Exception:  # noqa: BLE001
        lot = 1.0
    return per_unit * lot, len(vals)


@register("static.cost_floor", kind="static")
async def cost_floor(ctx: ProbeContext) -> List[Finding]:
    """Reject designs whose expected edge is too small versus modeled friction.

    Evidence order (never fire on a guess — §19): (1) a design number the strategy
    explicitly declares; (2) else the REALIZED average gross credit-per-lot from
    recent closed spreads; (3) else stay silent. The old probe read an
    expected_*_per_lot field that no strategy ever sets, so it fired HIGH on an
    implicit zero for every active option strategy — a false positive on no
    evidence. Now it judges real economics or says nothing."""
    out: List[Finding] = []
    for strat in ctx.strategies:
        o = _opts(strat)
        if str(o.get("structure")) not in ("credit_spread", "debit_spread", "single_leg") or not _is_active(strat):
            continue
        source, sample = "declared", None
        edge = o.get("expected_edge_per_lot") or o.get("expected_credit_per_lot") or o.get("avg_credit_per_lot")
        try:
            edge = float(edge)
        except (TypeError, ValueError):
            measured = await _realized_credit_per_lot(ctx.db, ctx.user_id, strat)
            if measured is None:
                continue  # no declared design number AND no realized evidence
            edge, sample, source = measured[0], measured[1], "realized"
        friction = _round_trip_cost(strat)
        floor = _COST_FLOOR_MULT * friction
        if edge < floor:
            out.append(Finding(
                probe_id="static.cost_floor", domain=Domain.STRATEGY,
                severity=Severity.HIGH, entity=str(strat.get("id")),
                title=f"{source.title()} credit Rs{edge:.0f}/lot is below {floor:.0f} cost floor",
                detail=("The design does not clear the ERP cost-floor law: expected edge "
                        "must be at least 3x modeled round-trip friction before paper or "
                        "promotion. This catches width-1 low-credit designs before they "
                        "spend their edge on brokerage, taxes and slippage."),
                evidence={"strategy_id": strat.get("id"), "name": strat.get("name"),
                          "underlying": _underlying(strat), "expected_edge_per_lot": round(edge, 1),
                          "source": source, "sample_size": sample,
                          "round_trip_cost_per_lot": friction, "required_floor": floor,
                          "multiple": round(edge / friction, 2) if friction else None},
                reproduction="avg net_credit/max_profit of last <=40 CLOSED positions x lot_size vs 3x modeled round-trip friction",
                suggested_fix="Reject or redesign the geometry until realized credit is at least 3x modeled friction.",
            ))
    return out


@register("infra.contract_spec_drift", kind="static")
async def contract_spec_drift(ctx: ProbeContext) -> List[Finding]:
    """Flag lot/expiry metadata that disagrees with the central market-domain helper."""
    out: List[Finding] = []
    for strat in ctx.strategies:
        underlying = _underlying(strat)
        if underlying not in {"NIFTY", "BANKNIFTY", "SENSEX"}:
            continue
        spec = contract_spec_for_underlying(underlying)
        o = _opts(strat)
        evidence = {"strategy_id": strat.get("id"), "name": strat.get("name"), "underlying": underlying}
        mismatches = []
        configured_lot = o.get("lot_size") or strat.get("lot_size")
        if configured_lot is not None:
            try:
                configured_lot = int(configured_lot)
                if configured_lot != spec["lot_size"]:
                    mismatches.append("lot_size")
                    evidence["configured_lot_size"] = configured_lot
                    evidence["expected_lot_size"] = spec["lot_size"]
            except (TypeError, ValueError):
                mismatches.append("lot_size_invalid")
                evidence["configured_lot_size"] = configured_lot
                evidence["expected_lot_size"] = spec["lot_size"]
        configured_expiry = str(o.get("weekly_expiry_day") or strat.get("weekly_expiry_day") or "").upper()
        expected_expiry = spec.get("weekly_expiry_day")
        if configured_expiry and expected_expiry and configured_expiry != expected_expiry:
            mismatches.append("weekly_expiry_day")
            evidence["configured_weekly_expiry_day"] = configured_expiry
            evidence["expected_weekly_expiry_day"] = expected_expiry
        if mismatches:
            out.append(Finding(
                probe_id="infra.contract_spec_drift", domain=Domain.INFRA,
                severity=Severity.HIGH, entity=str(strat.get("id")),
                title=f"{underlying} contract spec drift: {', '.join(mismatches)}",
                detail=("Strategy metadata disagrees with the central market-domain contract spec. "
                        "Lot and expiry facts must come from the instrument master/domain resolver, "
                        "not stale strategy config."),
                evidence=evidence,
                reproduction="compare strategy visual_config.options lot/expiry fields to core.market_domains.contract_spec_for_underlying",
                suggested_fix="Refresh strategy contract metadata from the Upstox instrument master/domain resolver.",
            ))
    return out

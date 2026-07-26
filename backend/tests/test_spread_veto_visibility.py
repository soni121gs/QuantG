"""Regression guards for the spread-veto visibility fix (2026-07-26).

On 2026-07-24, 279 of 360 signals died with `SPREAD_BUILD_FAILED` and the reason
"credit_spread not buildable (geometry veto / no candidate)". None of them carried a
`spread_veto`, because the veto was only ever set on ONE of six exit paths — an actual
builder veto. A missing gateway, a failed chain fetch, an empty chain, a disabled
structure flag, or either of two swallowed `except`s all left the field absent, and the
stand-down message then asserted a geometry veto it had never observed.

These tests pin the contract signal_manager depends on. See CLAUDE.md §21.5/§22.6: a
reason string must never claim a cause it did not verify.
"""
import pytest

from signal_manager import _spread_stand_down_result


def _veto(**kw):
    return {"reason": kw.get("reason"), "law": kw.get("law"),
            "stage": kw.get("stage"), "counts": kw.get("counts")}


def test_builder_veto_reason_is_carried_into_the_skip():
    res = _spread_stand_down_result(
        "credit_spread",
        _veto(reason="cost_floor: credit 27.55 on width 200 (ratio 0.138 < 0.120 min)",
              law="cost_floor", stage="builder_veto"),
    )
    assert res["reason_code"] == "SPREAD_BUILD_FAILED"
    assert "cost_floor" in res["reason"]
    assert res["detail"]["spread_veto_stage"] == "builder_veto"
    assert res["detail"]["spread_veto_law"] == "cost_floor"


@pytest.mark.parametrize("stage,reason", [
    ("not_attempted", "spread build not attempted (option chain never fetched)"),
    ("preconditions_missing", "spread build not attempted (missing upstox_gateway)"),
    ("chain_fetch_failed", "option chain fetch failed (status=error)"),
    ("empty_chain", "option chain returned 0 strikes for NIFTY 2026-07-28"),
    ("structure_disabled", "credit_spreads are disabled by env flag"),
    ("builder_exception", "spread builder raised KeyError: 'strike_price'"),
    ("chain_fetch_exception", "option chain fetch raised TimeoutError: "),
])
def test_every_non_builder_exit_reports_its_own_cause(stage, reason):
    """The five silent exits must each name themselves, and none of them may be
    described as a geometry veto."""
    res = _spread_stand_down_result("credit_spread", _veto(reason=reason, stage=stage))
    assert res["detail"]["spread_veto_stage"] == stage
    assert reason in res["reason"]
    assert "geometry veto" not in res["reason"]


def test_missing_veto_admits_ignorance_instead_of_blaming_geometry():
    """The 07-24 signature: no veto recorded at all. The fallback must say so rather
    than assert the geometry was at fault."""
    res = _spread_stand_down_result("credit_spread", {})
    assert "cause not recorded" in res["reason"]
    assert "geometry veto" not in res["reason"]
    assert res["detail"]["spread_veto_stage"] == "unknown"


def test_detail_is_persistable_as_rejection_detail():
    """signal_manager only persists `order_res["detail"]` into rejection_detail
    (signal_manager.py ~1533); extra top-level keys are silently dropped."""
    res = _spread_stand_down_result("debit_spread", _veto(reason="x", stage="empty_chain"))
    assert "detail" in res
    for key in ("reason_code", "human_reason", "status",
                "spread_veto_stage", "spread_veto_law", "spread_veto_counts"):
        assert key in res["detail"]

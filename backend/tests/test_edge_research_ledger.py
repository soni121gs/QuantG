from core.edge_research_ledger import (
    deflated_sharpe, enrich_snapshot, evidence_allocation, reject_reasons, stable_hash,
    trial_document,
)


def test_hash_is_deterministic():
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def test_reject_reasons_are_explanatory():
    reasons = reject_reasons({"n": 4, "oos_expectancy": -10, "pct_green_months": 20})
    assert "too few trades" in reasons
    assert "negative OOS expectancy" in reasons


def test_enrichment_builds_heatmap_and_allocation():
    snap = {
        "oos": {"rows": [{"name": "A", "n": 50, "oos_expectancy": 100,
                           "pct_green_months": 70, "verdict": "CANDIDATE_EDGE"}]},
        "sweep": [{"name": "A", "configs": 3, "positive_oos": 2,
                   "cells": [{"oos_expectancy": 1}, {"oos_expectancy": 2}, {"oos_expectancy": -1}]}],
    }
    out = enrich_snapshot(snap)
    assert out["erl"]["heatmaps"][0]["plateau_score"] > 0.5
    assert out["erl"]["allocation"][0]["auto_apply"] is False
    row = out["oos"]["rows"][0]
    assert row["trials_count"] == 3
    assert row["deflated_sharpe"]["method"] == "insufficient_oos_returns"
    assert "warning" in row["deflated_sharpe"]


def test_deflated_sharpe_uses_return_vector_when_present():
    row = {
        "n": 40,
        "oos_expectancy": 120,
        "pct_green_months": 70,
        "oos_returns": [0.0012, 0.0014, 0.0009, 0.0016, -0.0003, 0.0011, 0.0013, 0.001] * 5,
    }
    dsr = deflated_sharpe(row, trials_tested=3)
    assert dsr["method"] == "oos_normalized_dsr_v2"
    assert dsr["sample_n"] == 40
    assert "observed_sharpe" in dsr


def test_deflated_sharpe_penalizes_many_trials():
    row = {
        "n": 60, "oos_expectancy": 150, "pct_green_months": 70,
        "oos_returns": [0.0012, 0.0014, 0.0009, 0.0016, -0.0003, 0.0011] * 10,
    }
    one = deflated_sharpe(row, trials_tested=1)
    many = deflated_sharpe(row, trials_tested=100)
    assert many["trials_count"] == 100
    assert many["deflated_sharpe"] < one["deflated_sharpe"]


def test_trial_identity_separates_users_and_strategy_versions():
    snapshot = {"coverage": {"start": "2024"}, "base_rate": {"slippage_pct": 0.1}}
    row = {"strategy_id": "s1", "name": "A", "strategy_config": {"python_code": "v1"}}
    first = trial_document(user_id="u1", row=row, snapshot=snapshot)
    other_user = trial_document(user_id="u2", row=row, snapshot=snapshot)
    changed = trial_document(
        user_id="u1", row={**row, "strategy_config": {"python_code": "v2"}}, snapshot=snapshot,
    )
    assert len({first["_id"], other_user["_id"], changed["_id"]}) == 3


def test_allocation_never_auto_applies():
    rows = [{"name": "A", "promotion_stage": "paper-forward", "oos_expectancy": 100,
             "robustness": {"score": 0.8}}]
    assert evidence_allocation(rows)[0]["auto_apply"] is False

from core.edge_research_ledger import (
    deflated_sharpe, enrich_snapshot, evidence_allocation, reject_reasons, stable_hash,
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
    assert row["deflated_sharpe"]["method"] == "edge_lab_proxy_v1"
    assert "warning" in row["deflated_sharpe"]


def test_deflated_sharpe_uses_return_vector_when_present():
    row = {
        "n": 40,
        "oos_expectancy": 120,
        "pct_green_months": 70,
        "trade_returns": [120, 140, 90, 160, -30, 110, 130, 100] * 5,
    }
    dsr = deflated_sharpe(row, trials_tested=3)
    assert dsr["method"] == "return_vector_dsr_v1"
    assert dsr["sample_n"] == 40
    assert "observed_sharpe" in dsr


def test_deflated_sharpe_penalizes_many_trials():
    row = {"n": 60, "oos_expectancy": 150, "pct_green_months": 70}
    one = deflated_sharpe(row, trials_tested=1)
    many = deflated_sharpe(row, trials_tested=100)
    assert many["trials_count"] == 100
    assert many["deflated_sharpe"] < one["deflated_sharpe"]


def test_allocation_never_auto_applies():
    rows = [{"name": "A", "promotion_stage": "paper-forward", "oos_expectancy": 100,
             "robustness": {"score": 0.8}}]
    assert evidence_allocation(rows)[0]["auto_apply"] is False

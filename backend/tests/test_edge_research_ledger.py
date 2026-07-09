from core.edge_research_ledger import (
    enrich_snapshot, evidence_allocation, reject_reasons, stable_hash,
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


def test_allocation_never_auto_applies():
    rows = [{"name": "A", "promotion_stage": "paper-forward", "oos_expectancy": 100,
             "robustness": {"score": 0.8}}]
    assert evidence_allocation(rows)[0]["auto_apply"] is False

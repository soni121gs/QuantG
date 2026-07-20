"""Edge Lab Research Ledger: reproducible trials, robustness, rejects and allocation."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def trial_document(*, user_id: str, row: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    config = {
        "strategy": row.get("name"), "underlying": row.get("underlying"),
        "structure": row.get("structure"), "window": snapshot.get("coverage"),
        "costs": snapshot.get("base_rate", {}).get("slippage_pct"),
    }
    trial_hash = stable_hash(config)
    return {
        "_id": trial_hash, "trial_hash": trial_hash, "user_id": user_id,
        "strategy_name": row.get("name"), "config": config,
        "metrics": {k: row.get(k) for k in (
            "n", "pnl", "expectancy", "oos_expectancy", "win_rate", "pct_green_months"
        )},
        "verdict": row.get("verdict") or "ERROR",
        "reject_reasons": row.get("reject_reasons") or [],
        "primary_reject_reason": row.get("primary_reject_reason"),
        "last_run_at": snapshot.get("generated_at"),
        "created_at": datetime.now(timezone.utc),
    }


def reject_reasons(row: Dict[str, Any]) -> List[str]:
    reasons = []
    if row.get("error"):
        reasons.append("data-quality gap or unsupported historical domain")
    if int(row.get("n") or 0) < 30:
        reasons.append("too few trades")
    if float(row.get("oos_expectancy") or 0) <= 0:
        reasons.append("negative OOS expectancy")
    if row.get("all_years_positive") is False:
        reasons.append("only one lucky year")
    if float(row.get("pct_green_months") or 0) < 50:
        reasons.append("inconsistent across months")
    if str(row.get("signal_evaluation") or "").startswith("intraday"):
        reasons.append("daily EOD data cannot fairly judge intraday strategy")
    return list(dict.fromkeys(reasons))


def robustness(row: Dict[str, Any], *, trials_tested: int = 1, plateau: float = 0.0) -> Dict[str, Any]:
    n = int(row.get("n") or 0)
    exp = float(row.get("oos_expectancy") or 0)
    green = float(row.get("pct_green_months") or 0) / 100.0
    penalty = min(0.75, math.log10(max(1, trials_tested)) / 4)
    score = max(0.0, min(1.0, (min(n / 100, 1) * 0.25 + max(0, min(exp / 500, 1)) * 0.35
                               + green * 0.2 + plateau * 0.2) * (1 - penalty)))
    warning = "OVERFIT_RISK" if exp > 0 and plateau < 0.34 else None
    return {"score": round(score, 3), "multiple_testing_penalty": round(penalty, 3),
            "plateau_score": round(plateau, 3), "warning": warning}


def deflated_sharpe(row: Dict[str, Any], *, trials_tested: int = 1) -> Dict[str, Any]:
    """Conservative Deflated-Sharpe proxy for Edge Lab rows.

    Edge Lab snapshots do not persist per-trade returns yet, so this is not a
    Bailey/Lopez de Prado full DSR. It is an explicit, auditable penalty that
    deflates the observed OOS signal by sample size, month breadth, and number
    of tried configs. When trade-return vectors are stored, this function is the
    single replacement point for the exact DSR formula.
    """
    n = int(row.get("n") or 0)
    exp = float(row.get("oos_expectancy") or row.get("expectancy") or 0.0)
    green = max(0.0, min(1.0, float(row.get("pct_green_months") or 0.0) / 100.0))
    sample_scale = math.sqrt(min(n, 250) / 30.0) if n > 0 else 0.0
    observed = (exp / 500.0) * sample_scale * (0.5 + 0.5 * green)
    penalty = min(1.5, math.sqrt(math.log(max(2, trials_tested))) / 2.0)
    dsr = observed - penalty
    return {
        "trials_count": int(max(1, trials_tested)),
        "observed_sharpe_proxy": round(observed, 3),
        "deflated_sharpe": round(dsr, 3),
        "multiple_testing_penalty": round(penalty, 3),
        "method": "edge_lab_proxy_v1",
        "passed": bool(dsr > 0 and n >= 30 and exp > 0),
    }


def enrich_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    sweeps = {s.get("name"): s for s in (snapshot.get("sweep") or [])}
    rows = snapshot.get("oos", {}).get("rows") or []
    for row in rows:
        sweep = sweeps.get(row.get("name")) or {}
        configs = max(1, int(sweep.get("configs") or 1))
        plateau = float(sweep.get("positive_oos") or 0) / configs
        row["trials_count"] = configs
        row["deflated_sharpe"] = deflated_sharpe(row, trials_tested=configs)
        row["robustness"] = robustness(row, trials_tested=configs, plateau=plateau)
        row["reject_reasons"] = reject_reasons(row)
        row["primary_reject_reason"] = row["reject_reasons"][0] if row["reject_reasons"] else None
        if (
            row.get("verdict") == "CANDIDATE_EDGE"
            and (row["robustness"].get("warning") or not row["deflated_sharpe"].get("passed"))
        ):
            row["verdict"] = "OVERFIT_RISK"
        row["promotion_stage"] = (
            "paper-forward" if row.get("verdict") == "CANDIDATE_EDGE"
            else "watch" if row.get("verdict") in {"FRAGILE", "INSUFFICIENT_DATA"}
            else "rejected"
        )
    heatmaps = []
    for name, sweep in sweeps.items():
        cells = sweep.get("cells") or []
        heatmaps.append({"strategy": name, "cells": cells,
                         "plateau_score": round(sum(float(c.get("oos_expectancy") or 0) > 0 for c in cells)
                                                / max(1, len(cells)), 3)})
    allocation = evidence_allocation(rows)
    snapshot["erl"] = {"heatmaps": heatmaps, "allocation": allocation,
                       "promotion_ladder": ["rejected", "watch", "paper-forward", "founder-gated-live"]}
    return snapshot


def evidence_allocation(rows: Iterable[Dict[str, Any]], total_paper_capital: float = 100_000.0) -> List[Dict[str, Any]]:
    out = []
    weights = []
    for row in rows:
        r = float((row.get("robustness") or {}).get("score") or 0)
        exp = max(0.0, float(row.get("oos_expectancy") or 0))
        weight = r * math.sqrt(exp + 1) if row.get("promotion_stage") == "paper-forward" else r * 0.1
        weights.append(weight)
    denom = sum(weights) or 1.0
    for row, weight in zip(rows, weights):
        out.append({
            "strategy": row.get("name"), "verdict": row.get("verdict"),
            "evidence_weight": round(weight / denom, 4),
            "recommended_paper_capital": round(total_paper_capital * weight / denom, 2),
            "promotion_stage": row.get("promotion_stage"),
            "auto_apply": False,
        })
    return sorted(out, key=lambda r: r["recommended_paper_capital"], reverse=True)


async def persist_trials(db, user_id: str, snapshot: Dict[str, Any]) -> int:
    count = 0
    for row in snapshot.get("oos", {}).get("rows") or []:
        doc = trial_document(user_id=user_id, row=row, snapshot=snapshot)
        # created_at is owned by $setOnInsert below; it must NOT also appear in
        # $set or Mongo rejects the update ("would create a conflict at
        # 'created_at'") — which failed the whole Edge Lab build every run.
        doc.pop("created_at", None)
        await db.strategy_trials.update_one(
            {"_id": doc["_id"]},
            {"$set": {**doc, "last_run_at": snapshot.get("generated_at")},
             "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
             "$inc": {"run_count": 1}},
            upsert=True,
        )
        count += 1
    return count

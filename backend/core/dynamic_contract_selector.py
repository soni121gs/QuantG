"""Continuous option-chain ranking for credit-spread entries."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.spread_builder import build_credit_spread


def spread_signature(spread: Dict[str, Any]) -> str:
    short = spread.get("short_leg") or {}
    long = spread.get("long_leg") or {}
    return f"{spread.get('option_type')}:{short.get('expiry')}:{short.get('strike')}:{long.get('strike')}"


def _score(
    spread: Dict[str, Any],
    *,
    preferred_direction: str,
    previous_signature: Optional[str],
    minutes_to_close: int,
) -> Dict[str, Any]:
    short = spread["short_leg"]
    width = max(float(spread.get("width_points") or 0), 1.0)
    credit_ratio = float(spread.get("net_credit") or 0) / width
    theta_ratio = max(0.0, float(spread.get("net_theta") or 0)) / width
    delta = abs(float(short.get("delta") or 0))
    oi = max(0.0, float(short.get("oi") or 0))
    liquidity = min(1.0, oi / 1_000_000.0)
    directional_fit = 1.0 if spread.get("direction") == preferred_direction else 0.72
    time_scale = max(0.15, min(1.0, float(minutes_to_close) / 120.0))
    signature = spread_signature(spread)
    reuse_mult = 0.55 if previous_signature and signature == previous_signature else 1.0
    score = (
        0.36 * min(1.0, credit_ratio / 0.35)
        + 0.20 * min(1.0, theta_ratio / 0.08)
        + 0.18 * liquidity
        + 0.16 * max(0.0, 1.0 - abs(delta - 0.22) / 0.30)
        + 0.10 * directional_fit
    ) * time_scale * reuse_mult
    spread = dict(spread)
    spread.update({
        "contract_edge_score": round(max(0.0, min(1.0, score)), 4),
        "contract_size_mult": round(max(0.10, min(1.0, score)), 4),
        "selection_method": "dynamic-chain-rank",
        "selection_signature": signature,
        "selection_factors": {
            "credit_ratio": round(credit_ratio, 4),
            "theta_ratio": round(theta_ratio, 4),
            "liquidity": round(liquidity, 4),
            "short_delta": round(delta, 4),
            "directional_fit": directional_fit,
            "time_scale": round(time_scale, 4),
            "reuse_mult": reuse_mult,
        },
    })
    return spread


def select_dynamic_credit_spread(
    *,
    chain_nodes: List[Dict[str, Any]],
    preferred_direction: str,
    width_points: float,
    previous_signature: Optional[str] = None,
    minutes_to_close: int = 120,
) -> Dict[str, Any]:
    """Rank both sides and several deltas; always return the best valid candidate."""
    candidates = []
    for direction in ("bullish", "bearish"):
        for target_delta in (0.12, 0.18, 0.24, 0.30, 0.38):
            spread = build_credit_spread(
                chain_nodes=chain_nodes,
                direction=direction,
                width_points=width_points,
                short_delta=target_delta,
            )
            if spread.get("ok"):
                candidates.append(_score(
                    spread,
                    preferred_direction=preferred_direction,
                    previous_signature=previous_signature,
                    minutes_to_close=minutes_to_close,
                ))
    if not candidates:
        return {"ok": False, "reason": "no valid dynamic spread candidates"}
    best = max(candidates, key=lambda row: row["contract_edge_score"])
    best["candidate_count"] = len(candidates)
    return best

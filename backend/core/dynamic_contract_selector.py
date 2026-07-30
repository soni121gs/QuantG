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
    iv_surface: Optional[Dict[str, Any]] = None,
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
    richness = (iv_surface or {}).get("richness") or {}
    z = float(richness.get("zscore") or 0.0) if richness.get("available") else 0.0
    surface_mult = max(0.70, min(1.25, 1.0 + (z * 0.08))) if richness.get("available") else 1.0
    signature = spread_signature(spread)
    reuse_mult = 0.55 if previous_signature and signature == previous_signature else 1.0
    score = (
        0.36 * min(1.0, credit_ratio / 0.35)
        + 0.20 * min(1.0, theta_ratio / 0.08)
        + 0.18 * liquidity
        + 0.16 * max(0.0, 1.0 - abs(delta - 0.22) / 0.30)
        + 0.10 * directional_fit
    ) * time_scale * reuse_mult * surface_mult
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
            "surface_mult": round(surface_mult, 4),
            "iv_richness_z": round(z, 3) if richness.get("available") else None,
        },
        "vol_ratio": round(max(0.7, min(1.5, float(short.get("iv") or 15.0) / 15.0)), 4),
    })
    return spread


def select_dynamic_credit_spread(
    *,
    chain_nodes: List[Dict[str, Any]],
    preferred_direction: str,
    width_points: float,
    previous_signature: Optional[str] = None,
    minutes_to_close: int = 120,
    iv_surface: Optional[Dict[str, Any]] = None,
    lot_size: Optional[int] = None,
    tp_frac: float = 1.0,
    hold_minutes: Optional[float] = None,
    cost_floor_mult: Optional[float] = None,
) -> Dict[str, Any]:
    """Rank several deltas on the requested side and return the best candidate.

    RC-1 fix (2026-07-17): `preferred_direction` is a HARD side gate, not a 10%
    scoring nudge. The strategy's brain decides CE-vs-PE from market context
    (e.g. "uptrend → sell OTM put spread"); the chain ranker only optimizes the
    strike/delta *within* that side. Ranking both sides and picking max edge-score
    let a fatter-credit bear-call outrank the intended bull-put and silently sell
    the OPPOSITE side the strategy chose (sold CE into a meltup, −₹3,810 on
    2026-07-17). We only fall back to the other side when the requested side has
    no buildable candidate at all (so we never return nothing), and we tag the
    fallback so it is visible/auditable rather than silent.
    """
    _pref = str(preferred_direction).lower()
    _valid = _pref in ("bullish", "bearish")
    # Requested side first; the opposite side is a last-resort fallback only.
    side_order = ([_pref, ("bearish" if _pref == "bullish" else "bullish")]
                  if _valid else ["bullish", "bearish"])

    # Every rejected delta's reason, so a stand-down is diagnosable. build_credit_spread
    # returns a precise reason per candidate (cost_floor with the actual ratio,
    # tp_reachability with DTE/hold/ratio) and this function used to throw all of them
    # away and report "no valid dynamic spread candidates" — which is how 279 of 360
    # signals died on 2026-07-24 with no way to tell WHICH law vetoed them, or by how far.
    vetoes: List[Dict[str, Any]] = []

    def _build_side(direction: str) -> List[Dict[str, Any]]:
        out = []
        for target_delta in (0.12, 0.18, 0.24, 0.30, 0.38):
            # Cost-floor vetoes are applied per candidate: a delta that produces a
            # too-thin credit for this width is dropped from the ladder entirely
            # rather than merely scoring low (a low score still opened the trade,
            # just at the 0.10 size floor — which is how QG-O1 kept trading a
            # structure its own edge math had already condemned).
            spread = build_credit_spread(
                chain_nodes=chain_nodes,
                direction=direction,
                width_points=width_points,
                short_delta=target_delta,
                lot_size=lot_size,
                tp_frac=tp_frac,
                hold_minutes=hold_minutes,
                cost_floor_mult=cost_floor_mult,
            )
            if spread.get("ok"):
                out.append(_score(
                    spread,
                    preferred_direction=preferred_direction,
                    previous_signature=previous_signature,
                    minutes_to_close=minutes_to_close,
                    iv_surface=iv_surface,
                ))
            else:
                vetoes.append({
                    "direction": direction,
                    "short_delta": target_delta,
                    "reason": spread.get("reason"),
                    "law": ("cost_floor" if spread.get("cost_floor")
                            else "tp_reachability" if spread.get("tp_reachability")
                            else "chain"),
                })
        return out

    used_fallback_side = False
    candidates = _build_side(side_order[0])
    if not candidates and len(side_order) > 1:
        # Requested side unbuildable on this chain — fall back to the other side
        # rather than skip the trade, but mark it so it is never mistaken for a
        # freely-chosen side.
        candidates = _build_side(side_order[1])
        used_fallback_side = bool(candidates)
    if not candidates:
        by_law: Dict[str, int] = {}
        for v in vetoes:
            by_law[v["law"]] = by_law.get(v["law"], 0) + 1
        dominant = max(by_law.items(), key=lambda kv: kv[1])[0] if by_law else "none"
        closest = next((v["reason"] for v in vetoes if v["law"] == dominant), None)
        return {
            "ok": False,
            "reason": "no valid dynamic spread candidates — {} of {} deltas vetoed by {} ({})".format(
                by_law.get(dominant, 0), len(vetoes), dominant, closest or "no candidate built"),
            "veto_law": dominant,
            "veto_counts": by_law,
            "vetoes": vetoes,
        }
    best = max(candidates, key=lambda row: row["contract_edge_score"])
    best["candidate_count"] = len(candidates)
    best["side_gated"] = True
    best["used_fallback_side"] = used_fallback_side
    return best

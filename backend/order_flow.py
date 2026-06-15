"""Phase 2 #3 — order-flow confirmation.

Uses the total-buy-qty / total-sell-qty (tbq/tsq) pressure that the Upstox V3
feed now carries on each option contract to confirm an entry: only take a long
option when there is net buying pressure on that contract. Built but default
OFF (off → shadow → enabled).

Caveat: a single option's tbq/tsq can be thin and noisy, so a minimum-volume
guard suppresses the gate rather than blocking on near-empty books.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("quantg.order_flow")

ORDERFLOW_GATE_ENABLED = os.environ.get("ORDERFLOW_GATE_ENABLED", "false").strip().lower() == "true"
ORDERFLOW_GATE_SHADOW = os.environ.get("ORDERFLOW_GATE_SHADOW", "false").strip().lower() == "true"
ORDERFLOW_MIN_IMBALANCE = float(os.environ.get("ORDERFLOW_MIN_IMBALANCE", "0.15"))
ORDERFLOW_MIN_VOLUME = float(os.environ.get("ORDERFLOW_MIN_VOLUME", "500"))


def _to_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def orderflow_imbalance(contract: Dict[str, Any]) -> Optional[float]:
    """Net order-flow imbalance in [-1, 1] from a contract's tbq/tsq:
    (tbq - tsq) / (tbq + tsq). Positive = buyers dominating. Returns None when
    total quantity is below ORDERFLOW_MIN_VOLUME (book too thin to trust)."""
    tbq = _to_float((contract or {}).get("tbq"))
    tsq = _to_float((contract or {}).get("tsq"))
    if tbq is None or tsq is None:
        return None
    total = tbq + tsq
    if total < ORDERFLOW_MIN_VOLUME or total <= 0:
        return None
    return (tbq - tsq) / total


def orderflow_gate(imbalance: Optional[float], side: str) -> Dict[str, Any]:
    """Decision for an order of the given side ("BUY"/"SELL") given the contract
    imbalance. A BUY wants positive imbalance ≥ +threshold (net buying pressure);
    a SELL wants ≤ -threshold. Returns {block, shadow, reason}.

    No data (imbalance None) never blocks — we don't gate on a thin book."""
    if imbalance is None:
        return {"block": False, "shadow": False, "reason": None}
    side_u = str(side or "BUY").upper()
    if side_u == "SELL":
        would_block = imbalance > -ORDERFLOW_MIN_IMBALANCE
    else:
        would_block = imbalance < ORDERFLOW_MIN_IMBALANCE
    if not would_block:
        return {"block": False, "shadow": False, "reason": None}
    reason = (
        f"ORDERFLOW_WEAK: imbalance={imbalance:+.2f} fails {side_u} "
        f"threshold ±{ORDERFLOW_MIN_IMBALANCE:.2f}"
    )
    if ORDERFLOW_GATE_ENABLED:
        return {"block": True, "shadow": False, "reason": reason}
    if ORDERFLOW_GATE_SHADOW:
        return {"block": False, "shadow": True, "reason": reason}
    return {"block": False, "shadow": False, "reason": reason}

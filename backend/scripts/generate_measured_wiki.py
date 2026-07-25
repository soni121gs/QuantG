#!/usr/bin/env python3
"""Generate drift-resistant measured wiki notes from code/runtime facts.

This does not query the broker or mutate trading state. It writes markdown under
wiki/Measured so Hermes can retrieve measured facts separately from literature or
aspirational research notes.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.market_domains import contract_spec_for_underlying  # noqa: E402
from core.spread_builder import SPREAD_ROUND_TRIP_COST_PER_LOT  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "wiki" / "Measured"


def _front(title: str, reproduction: str) -> str:
    return (
        "---\n"
        "claim_type: measured\n"
        f"verified: {datetime.now(timezone.utc).date().isoformat()}\n"
        f"reproduction: {reproduction}\n"
        "---\n\n"
        f"# {title}\n\n"
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    specs = []
    for underlying in ("NIFTY", "BANKNIFTY", "SENSEX"):
        spec = contract_spec_for_underlying(underlying)
        specs.append(f"- {underlying}: lot_size={spec.get('lot_size')}, weekly_expiry_day={spec.get('weekly_expiry_day')}")
    (OUT / "Contract Specs.md").write_text(
        _front("Contract Specs", "python backend/scripts/generate_measured_wiki.py")
        + "\n".join(specs)
        + "\n",
        encoding="utf-8",
    )
    (OUT / "Friction Constants.md").write_text(
        _front("Friction Constants", "python backend/scripts/generate_measured_wiki.py")
        + f"- SPREAD_ROUND_TRIP_COST_PER_LOT={SPREAD_ROUND_TRIP_COST_PER_LOT}\n"
        + "- Cost-floor law requires expected edge >= 3x modeled round-trip friction.\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

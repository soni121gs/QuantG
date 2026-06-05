"""Index option metadata for QuantG's Upstox-only execution path."""
from __future__ import annotations

import logging

logger = logging.getLogger("quantg.options")

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}
STRIKE_INTERVALS = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}
OPT_EXCHANGE = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "SENSEX": "BFO"}
INDEX_SPOT_SYMBOL = {
    "NIFTY": ("NSE", "NIFTY 50"),
    "BANKNIFTY": ("NSE", "NIFTY BANK"),
    "SENSEX": ("BSE", "SENSEX"),
}
SUPPORTED = list(LOT_SIZES.keys())


def round_to_strike(spot: float, interval: int) -> int:
    """Round spot to the nearest configured strike interval."""
    return int(round(float(spot) / int(interval)) * int(interval))

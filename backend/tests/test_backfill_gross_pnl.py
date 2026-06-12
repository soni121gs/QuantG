import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scratch.backfill_gross_pnl import fallback_gross_pnl, order_needs_backfill


def test_order_needs_backfill_for_missing_or_zero_gross():
    assert order_needs_backfill({"id": "o1"}) is True
    assert order_needs_backfill({"id": "o1", "gross_pnl": 0}) is True
    assert order_needs_backfill({"id": "o1", "gross_pnl": 0.0}) is True
    assert order_needs_backfill({"id": "o1", "gross_pnl": 125.5}) is False


def test_fallback_gross_pnl_uses_net_plus_exit_order_charges():
    order = {"id": "o1", "net_pnl": 118.25, "charges": 6.75}
    assert fallback_gross_pnl(order) == 125.0


def test_fallback_gross_pnl_accepts_realized_spellings():
    assert fallback_gross_pnl({"realized_pnl": -110, "charges": 10}) == -100.0
    assert fallback_gross_pnl({"realized_pnl": 90, "charges": 5}) == 95.0


def test_fallback_gross_pnl_skips_zero_or_missing_net():
    assert fallback_gross_pnl({"net_pnl": 0, "charges": 5}) is None
    assert fallback_gross_pnl({"charges": 5}) is None

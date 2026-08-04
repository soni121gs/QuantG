"""The runner's resolver must honour the configured DTE window.

THE BUG (2026-08-05). `InstrumentResolver._lookup_index_option_chain` called
`get_option_chain(spot_key, None)` — whatever expiry Upstox returns by default —
and `expiry_rule` was passed only into a diagnostic. §25.4b added `select_expiry`
to `_resolve_option_for_strategy`, but server.py:18168 states plainly that
function is "only used by manual routes": the LIVE runner resolves through here.

So both `expiry_offset` AND `min_dte_days`/`max_dte_days` were decorative on the
path that actually trades. The HTE sleeve was configured 5-15 DTE and opened
0-DTE spreads on 2026-08-04 — a same-session trade wearing a hold-to-expiry
label, graded against OOS evidence drawn from multi-day holds.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.instrument_resolver import InstrumentResolver


def _iso(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


class FakeGateway:
    """Minimal Upstox stand-in. Records which expiry the chain was asked for."""

    connected = True

    def __init__(self, expiries, *, strike=24600, contracts_raises=False):
        self._expiries = expiries
        self._strike = strike
        self._contracts_raises = contracts_raises
        self.chain_called_with = "__never__"

    def get_option_contracts(self, spot_key, expiry_date=None):
        if self._contracts_raises:
            raise RuntimeError("broker down")
        return {"data": [{"expiry": e} for e in self._expiries]}

    def get_option_chain(self, spot_key, expiry_date=None):
        self.chain_called_with = expiry_date
        return {"status": "success", "data": [{
            "strike_price": self._strike,
            "expiry": expiry_date or self._expiries[0],
            "call_options": {"instrument_key": "NSE_FO|1", "trading_symbol": "X CE", "lot_size": 65},
            "put_options": {"instrument_key": "NSE_FO|2", "trading_symbol": "X PE", "lot_size": 65},
        }]}

    def search_instruments(self, *a, **k):          # the fallback that must NOT run
        raise AssertionError("search fallback must not run under a DTE window")


async def _resolve(gw, **kw):
    r = InstrumentResolver(db=None, upstox_gateway=gw)
    inst = await r.resolve_instrument_with_source(
        underlying="NIFTY", instrument_type="INDEX_OPTION", option_side="PE",
        strike_rule="ATM", spot_price_hint=24600.0, mode="paper", **kw)
    return inst, r.last_diagnostics


@pytest.mark.asyncio
async def test_window_picks_the_qualifying_expiry_not_the_nearest():
    """THE FIX. Nearest is 0 DTE; a 5-15 window must take the 7-DTE weekly."""
    gw = FakeGateway([_iso(0), _iso(7), _iso(14), _iso(35)])
    inst, diag = await _resolve(gw, min_dte=5, max_dte=15)
    assert gw.chain_called_with == _iso(7), f"chain fetched for {gw.chain_called_with}"
    assert inst is not None


@pytest.mark.asyncio
async def test_no_window_keeps_the_old_behaviour():
    """Every existing strategy (QG-O1 has no window) must be untouched: positional
    expiry_offset=0 against the ascending list."""
    gw = FakeGateway([_iso(0), _iso(7), _iso(14)])
    inst, _ = await _resolve(gw, expiry_rule=0)
    assert gw.chain_called_with == _iso(0)
    assert inst is not None


@pytest.mark.asyncio
async def test_expiry_offset_is_no_longer_decorative():
    """offset=1 must take the SECOND expiry. Previously ignored entirely."""
    gw = FakeGateway([_iso(0), _iso(7), _iso(14)])
    await _resolve(gw, expiry_rule=1)
    assert gw.chain_called_with == _iso(7)


@pytest.mark.asyncio
async def test_unsatisfiable_window_stands_down_and_does_not_fall_through():
    """THE FAIL-OPEN THAT WOULD HAVE MADE THIS FIX INERT.

    With only near expiries available, a 5-15 window matches nothing. The old
    control flow then fell through to `_search_index_option` (candidates start at
    "current_week") and finally the paper simulator — either of which hands back
    exactly the near-expiry contract the window exists to refuse. FakeGateway
    asserts if the search fallback runs.
    """
    gw = FakeGateway([_iso(0), _iso(1)])
    inst, diag = await _resolve(gw, min_dte=5, max_dte=15)
    assert inst is None, "an unsatisfiable DTE window must stand down"
    assert gw.chain_called_with == "__never__", "must not fetch a chain it cannot use"
    assert "DTE_WINDOW" in str(diag.get("reason")), diag


@pytest.mark.asyncio
async def test_window_refuses_rather_than_guessing_when_expiries_cannot_be_listed():
    """If the expiry list is unavailable we cannot prove the window is satisfied.
    Refuse — silently taking the default chain is the original bug."""
    gw = FakeGateway([_iso(7)], contracts_raises=True)
    inst, diag = await _resolve(gw, min_dte=5, max_dte=15)
    assert inst is None
    assert "DTE_WINDOW" in str(diag.get("reason")), diag


@pytest.mark.asyncio
async def test_no_window_still_resolves_when_expiries_cannot_be_listed():
    """The same failure must NOT block a strategy that never asked for a window."""
    gw = FakeGateway([_iso(0)], contracts_raises=True)
    inst, _ = await _resolve(gw)
    assert inst is not None
    assert gw.chain_called_with is None       # broker default chain, as before


class LegacyGateway(FakeGateway):
    """An adapter that cannot enumerate expiries at all (no get_option_contracts).
    A real possibility for any future broker adapter, so both branches are pinned."""
    get_option_contracts = None                 # attribute exists but is not callable


@pytest.mark.asyncio
async def test_gateway_without_contract_enumeration_is_handled_both_ways():
    inst, _ = await _resolve(LegacyGateway([_iso(0)]))
    assert inst is not None                     # no window -> broker default chain
    inst2, diag2 = await _resolve(LegacyGateway([_iso(0)]), min_dte=5, max_dte=15)
    assert inst2 is None                        # window -> refuse rather than guess
    assert "DTE_WINDOW" in str(diag2.get("reason")), diag2


@pytest.mark.asyncio
async def test_the_real_hte_case():
    """HTE on 2026-08-04: NIFTY weeklies at 0/7/14 DTE plus a monthly. Configured
    5-15, it opened the 0-DTE. It must now take the 7-DTE."""
    gw = FakeGateway([_iso(0), _iso(7), _iso(14), _iso(21)])
    inst, _ = await _resolve(gw, min_dte=5, max_dte=15)
    assert gw.chain_called_with == _iso(7)
    assert inst is not None

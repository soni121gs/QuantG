"""IMD-02: resolve the exact historical option contract the backtester would trade.

Maps ``(underlying, trade_date, expiry, strike, option_type)`` to the Upstox
``expired_instrument_key`` that IMD-03's importer feeds to the expired historical
candle endpoint — WITHOUT downloading the whole exchange, and WITHOUT ever guessing
a symbol string. If the broker has not confirmed the contract, the resolver returns
a typed reason (``STRIKE_NOT_FOUND`` etc.), never a fabricated key.

Pure logic + injected fetchers so it is unit-testable with no broker. Real callers
pass gateway-backed fetchers (see ``from_gateway``). Resolved contract metadata is
cached per (underlying, expiry); an optional on-disk cache persists it — tokens are
never written.

Coverage: NIFTY + BANKNIFTY (NSE). SENSEX/BANKEX (BSE) stay BLOCKED until a legal
BSE expired-data path is confirmed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from core.market_domains import resolve_domain_by_underlying

# Index instrument keys Upstox expects for expired-option lookups.
UNDERLYING_INDEX_KEY = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}
# BSE — no confirmed legal expired 1-minute path yet (Akamai-gated).
BLOCKED_UNDERLYINGS = {"SENSEX", "BANKEX"}

# Typed reasons.
OK = "OK"
BAD_INPUT = "BAD_INPUT"
OPTION_TYPE_INVALID = "OPTION_TYPE_INVALID"
UNSUPPORTED_UNDERLYING = "UNSUPPORTED_UNDERLYING"
BLOCKED_UNDERLYING = "BLOCKED_UNDERLYING"
FETCH_ERROR = "FETCH_ERROR"
EXPIRY_NOT_FOUND = "EXPIRY_NOT_FOUND"
STRIKE_NOT_FOUND = "STRIKE_NOT_FOUND"

_STRIKE_EPS = 0.5

# fetcher(underlying_key, expiry_date) -> list of raw contract dicts
ContractFetcher = Callable[[str, str], List[Dict[str, Any]]]
# fetcher(underlying_key) -> list of "YYYY-MM-DD" expiry strings
ExpiryFetcher = Callable[[str], List[str]]


@dataclass(frozen=True)
class ResolvedContract:
    underlying: str
    trade_date: str
    expiry: str
    strike: float
    option_type: str
    expired_instrument_key: str
    trading_symbol: str
    lot_size: int


@dataclass(frozen=True)
class ResolveResult:
    ok: bool
    reason: str
    contract: Optional[ResolvedContract] = None
    detail: str = ""


def _to_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _contract_key(item: Dict[str, Any]) -> str:
    return str(item.get("expired_instrument_key") or item.get("instrument_key") or "")


def select_weekly_expiry(expiries: List[str], trade_date: str) -> Optional[str]:
    """Nearest expiry on/after ``trade_date`` (the weekly a strategy would trade)."""
    td = _to_date(trade_date)
    if td is None:
        return None
    future = sorted(e for e in expiries if (_to_date(e) is not None and _to_date(e) >= td))
    return future[0] if future else None


def atm_strike(underlying: str, spot: float) -> float:
    """Round a spot price to the nearest listed strike for the underlying."""
    step = resolve_domain_by_underlying(underlying).get_strike_interval(underlying)
    return float(round(spot / step) * step)


class ExpiredOptionResolver:
    def __init__(
        self,
        contract_fetcher: ContractFetcher,
        expiry_fetcher: Optional[ExpiryFetcher] = None,
        *,
        cache_dir: Optional[str] = None,
    ):
        self._fetch_contracts = contract_fetcher
        self._fetch_expiries = expiry_fetcher
        self._cache_dir = cache_dir
        self._mem: Dict[str, List[Dict[str, Any]]] = {}

    # -- classification -------------------------------------------------------
    @staticmethod
    def is_blocked(underlying: str) -> bool:
        return str(underlying).upper() in BLOCKED_UNDERLYINGS

    @staticmethod
    def is_supported(underlying: str) -> bool:
        return str(underlying).upper() in UNDERLYING_INDEX_KEY

    # -- cache ----------------------------------------------------------------
    def _cache_path(self, underlying: str, expiry: str) -> Optional[str]:
        if not self._cache_dir:
            return None
        return os.path.join(self._cache_dir, underlying.upper(), f"{expiry}.json")

    def _load_contracts(self, underlying: str, expiry: str) -> Optional[List[Dict[str, Any]]]:
        mem_key = f"{underlying.upper()}|{expiry}"
        if mem_key in self._mem:
            return self._mem[mem_key]
        path = self._cache_path(underlying, expiry)
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._mem[mem_key] = data
                return data
            except (OSError, ValueError):
                return None
        return None

    def _store_contracts(self, underlying: str, expiry: str, contracts: List[Dict[str, Any]]) -> None:
        mem_key = f"{underlying.upper()}|{expiry}"
        self._mem[mem_key] = contracts
        path = self._cache_path(underlying, expiry)
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(contracts, fh)
        except OSError:
            pass

    def _contracts_for(self, underlying: str, expiry: str) -> List[Dict[str, Any]]:
        cached = self._load_contracts(underlying, expiry)
        if cached is not None:
            return cached
        raw = self._fetch_contracts(UNDERLYING_INDEX_KEY[underlying.upper()], expiry) or []
        # Keep only the identity fields we need — never persist tokens.
        contracts = [
            {
                "expired_instrument_key": _contract_key(item),
                "trading_symbol": str(item.get("trading_symbol") or ""),
                "strike_price": float(item.get("strike_price") or 0.0),
                "instrument_type": str(item.get("instrument_type") or "").upper(),
                "lot_size": int(item.get("lot_size") or 0),
                "expiry": str(item.get("expiry") or expiry)[:10],
            }
            for item in raw
            if _contract_key(item)
        ]
        self._store_contracts(underlying, expiry, contracts)
        return contracts

    # -- public ---------------------------------------------------------------
    def available_expiries(self, underlying: str) -> List[str]:
        if not self.is_supported(underlying) or self._fetch_expiries is None:
            return []
        raw = self._fetch_expiries(UNDERLYING_INDEX_KEY[underlying.upper()]) or []
        return sorted(str(e)[:10] for e in raw if _to_date(e) is not None)

    def resolve(
        self,
        underlying: str,
        trade_date: str,
        expiry: str,
        strike: float,
        option_type: str,
    ) -> ResolveResult:
        und = str(underlying).upper()
        opt = str(option_type).upper()

        if opt not in ("CE", "PE"):
            return ResolveResult(False, OPTION_TYPE_INVALID, detail=option_type)
        if self.is_blocked(und):
            return ResolveResult(False, BLOCKED_UNDERLYING, detail=und)
        if not self.is_supported(und):
            return ResolveResult(False, UNSUPPORTED_UNDERLYING, detail=und)
        if _to_date(expiry) is None:
            return ResolveResult(False, BAD_INPUT, detail=f"expiry={expiry}")
        try:
            target = float(strike)
        except (TypeError, ValueError):
            return ResolveResult(False, BAD_INPUT, detail=f"strike={strike}")

        try:
            contracts = self._contracts_for(und, str(expiry)[:10])
        except Exception as err:  # broker/network — typed, no fabricated key
            return ResolveResult(False, FETCH_ERROR, detail=str(err))

        if not contracts:
            return ResolveResult(False, EXPIRY_NOT_FOUND, detail=str(expiry)[:10])

        match = next(
            (
                c for c in contracts
                if c["instrument_type"] == opt and abs(c["strike_price"] - target) <= _STRIKE_EPS
            ),
            None,
        )
        if match is None:
            return ResolveResult(False, STRIKE_NOT_FOUND, detail=f"{target} {opt}")

        lot = match["lot_size"] or resolve_domain_by_underlying(und).get_lot_size(und)
        return ResolveResult(
            True,
            OK,
            ResolvedContract(
                underlying=und,
                trade_date=str(trade_date)[:10],
                expiry=match["expiry"],
                strike=match["strike_price"],
                option_type=opt,
                expired_instrument_key=match["expired_instrument_key"],
                trading_symbol=match["trading_symbol"],
                lot_size=int(lot),
            ),
        )

    def resolve_atm(
        self,
        underlying: str,
        trade_date: str,
        expiry: str,
        spot: float,
        option_type: str,
    ) -> ResolveResult:
        return self.resolve(underlying, trade_date, expiry, atm_strike(underlying, spot), option_type)

    # -- wiring ---------------------------------------------------------------
    @classmethod
    def from_gateway(cls, gateway: Any, *, cache_dir: Optional[str] = None) -> "ExpiredOptionResolver":
        def fetch_contracts(underlying_key: str, expiry_date: str) -> List[Dict[str, Any]]:
            res = gateway.get_expired_option_contracts(underlying_key, expiry_date)
            data = res.get("data") if isinstance(res, dict) else None
            return data if isinstance(data, list) else []

        def fetch_expiries(underlying_key: str) -> List[str]:
            res = gateway.get_expired_option_expiries(underlying_key)
            data = res.get("data") if isinstance(res, dict) else None
            return data if isinstance(data, list) else []

        return cls(fetch_contracts, fetch_expiries, cache_dir=cache_dir)

"""Upstox-only instrument resolver used by the unified strategy pipeline."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.market_domains import DomainType, resolve_domain_by_underlying
from core.models import Instrument, InstrumentSource

logger = logging.getLogger("quantg.instrument_resolver")


INDEX_SPOT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

PAPER_SPOT_HINTS = {
    "NIFTY": 24850.0,
    "BANKNIFTY": 54000.0,
    "SENSEX": 80500.0,
}


def _round_to_strike(price: float, interval: int) -> int:
    interval = max(1, int(interval or 1))
    return int(round(float(price) / interval) * interval)


def _source_value(source: InstrumentSource | str) -> str:
    return source.value if isinstance(source, InstrumentSource) else str(source)


class InstrumentResolver:
    def __init__(self, db, upstox_gateway: Optional[Any] = None):
        self.db = db
        self.upstox_gateway = upstox_gateway
        self.last_diagnostics: Dict[str, Any] = {}

    def _diag(self, *, stage: str, reason: str = "", **extra: Any) -> None:
        self.last_diagnostics = {
            "stage": stage,
            "reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **{k: v for k, v in extra.items() if v is not None},
        }

    async def resolve_instrument_with_source(
        self,
        underlying: str,
        instrument_type: str,
        option_side: str = "NONE",
        strike_rule: str = "NONE",
        expiry_rule: int = 0,
        spot_price_hint: Optional[float] = None,
        upstox_gateway: Optional[Any] = None,
        mode: str = "paper",
        allow_simulated: bool = True,
        itm_offset_pct: float = 0.0,
        **_ignored: Any,
    ) -> Optional[Instrument]:
        """Resolve a tradable Upstox instrument with source tracking.

        LIVE never returns simulated instruments. PAPER can return a clearly
        marked simulated instrument when the real Upstox master path is absent.
        """
        und = str(underlying or "").upper()
        inst_type = str(instrument_type or "").upper()
        side = str(option_side or "NONE").upper()
        strike = str(strike_rule or "NONE").upper()
        mode = str(mode or "paper").lower()
        self.upstox_gateway = upstox_gateway or self.upstox_gateway
        self._diag(stage="start", underlying=und, instrument_type=inst_type, mode=mode)

        domain = resolve_domain_by_underlying(und)
        if inst_type == "INDEX_OPTION":
            return await self._resolve_index_option(und, side, strike, expiry_rule, spot_price_hint,
                                                    mode, allow_simulated, float(itm_offset_pct or 0.0))

        self._diag(stage="unsupported_domain", reason=f"{domain.name}/{inst_type} is unsupported")
        return None

    async def _resolve_index_option(
        self,
        underlying: str,
        option_side: str,
        strike_rule: str,
        expiry_rule: int,
        spot_price_hint: Optional[float],
        mode: str,
        allow_simulated: bool,
        itm_offset_pct: float = 0.0,
    ) -> Optional[Instrument]:
        gw = self.upstox_gateway
        domain = resolve_domain_by_underlying(underlying)
        interval = int(domain.get_strike_interval(underlying) or 1)
        spot = float(spot_price_hint or 0)
        if spot <= 0:
            spot = await self._upstox_spot(underlying)
        if spot <= 0:
            if mode == "live" or not allow_simulated:
                self._diag(stage="index_spot_price", reason="real_upstox_spot_required_simulation_disabled")
                return None
            spot = await self._paper_spot(underlying)
            self._diag(stage="index_spot_price", reason="using_paper_spot_hint", spot=spot)

        target_strike = _round_to_strike(spot, interval)
        # Strike offset MUST account for option side. Moneyness is inverted for puts:
        #   CE: ITM1 = ATM - interval (lower strike), OTM1 = ATM + interval
        #   PE: ITM1 = ATM + interval (higher strike), OTM1 = ATM - interval
        # The previous offset was direction-blind (always the CE convention), so every
        # PE/bearish trade got the inverted strike — an ITM1_BUY put was actually bought
        # OTM (the WR-22 "config=ITM1_BUY but trades OTM" bug, fixed 2026-06-23).
        is_pe = str(option_side or "").upper() == "PE"
        if itm_offset_pct and itm_offset_pct > 0 and strike_rule in ("ITM1", "ITM"):
            # deep-ITM (delta-1) selection: move `itm_offset_pct` into the money,
            # side-aware (CE lower strike / PE higher strike), snapped to the grid.
            steps = max(1, round((spot * itm_offset_pct) / interval))
            target_strike += (steps * interval) if is_pe else (-steps * interval)
        elif strike_rule == "OTM1":
            target_strike += (-interval if is_pe else interval)
        elif strike_rule == "ITM1":
            target_strike += (interval if is_pe else -interval)
        target_strike = _round_to_strike(target_strike, interval)

        if gw and getattr(gw, "connected", True):
            inst = await self._lookup_index_option_chain(gw, underlying, option_side, target_strike, domain)
            if inst:
                return inst
            inst = await self._search_index_option(gw, underlying, option_side, target_strike, domain)
            if inst:
                return inst

        self._diag(
            stage="index_option_lookup",
            reason="no_upstox_option_match",
            underlying=underlying,
            option_side=option_side,
            target_strike=target_strike,
            expiry_offset=int(expiry_rule or 0),
        )
        if mode == "live" or not allow_simulated:
            return None
        return await self._paper_simulated_index_option(underlying, option_side, target_strike)

    async def _lookup_index_option_chain(self, gw: Any, underlying: str, option_side: str, target_strike: int, domain: Any) -> Optional[Instrument]:
        spot_key = INDEX_SPOT_KEYS.get(underlying)
        if not spot_key or not hasattr(gw, "get_option_chain"):
            self._diag(stage="index_option_lookup", reason="upstox_option_chain_unavailable", spot_key=spot_key)
            return None
        try:
            chain = gw.get_option_chain(spot_key, None)
            rows = (chain or {}).get("data") or []
            for row in rows:
                try:
                    row_strike = int(float(row.get("strike_price") or row.get("strike") or 0))
                except Exception:
                    continue
                if row_strike != int(target_strike):
                    continue
                opt_node = row.get("call_options" if option_side == "CE" else "put_options") or {}
                key = opt_node.get("instrument_key")
                symbol = opt_node.get("trading_symbol") or opt_node.get("tradingsymbol")
                if not key:
                    continue
                self._diag(
                    stage="index_option_lookup",
                    reason="matched_upstox_option_chain",
                    instrument_key=key,
                    trading_symbol=symbol,
                    strike=target_strike,
                )
                return Instrument(
                    symbol=symbol or str(key),
                    underlying=underlying,
                    exchange=domain.exchange,
                    segment=domain.segment,
                    instrument_key=str(key),
                    lot_size=int(opt_node.get("lot_size") or domain.get_lot_size(underlying)),
                    tick_size=float(opt_node.get("tick_size") or 0.05),
                    expiry=row.get("expiry"),
                    strike=float(target_strike),
                    option_type=option_side,
                    source=InstrumentSource.UPSTOX_MASTER,
                )
        except Exception as exc:
            self._diag(stage="index_option_lookup", reason=f"upstox_option_chain_error:{exc}")
        return None

    async def _search_index_option(self, gw: Any, underlying: str, option_side: str, target_strike: int, domain: Any) -> Optional[Instrument]:
        search = getattr(gw, "search_instruments", None)
        if not callable(search):
            self._diag(stage="index_option_search", reason="upstox_search_unavailable")
            return None
        exchange = "BSE" if underlying == "SENSEX" else "NSE"
        queries = [f"{underlying} {int(target_strike)}", underlying]
        segment_candidates = ("FO", "OPT", "ALL")
        expiry_candidates = ("current_week", "next_week", "current_month", None)
        candidates = []
        try:
            for query in dict.fromkeys(queries):
                for segment in segment_candidates:
                    for expiry in expiry_candidates:
                        result = search(
                            query,
                            exchanges=exchange,
                            segments=segment,
                            instrument_types=option_side,
                            expiry=expiry,
                            atm_offset=0,
                            records=40,
                        )
                        batch = result.get("data") if isinstance(result, dict) else []
                        if batch:
                            candidates.extend(batch)
                if candidates:
                    break
        except Exception as exc:
            self._diag(stage="index_option_search", reason=f"upstox_search_error:{exc}")
            return None

        best = None
        best_distance = None
        for row in candidates:
            if not isinstance(row, dict):
                continue
            row_side = str(row.get("instrument_type") or row.get("option_type") or "").upper()
            if row_side and row_side != option_side:
                continue
            key = row.get("instrument_key")
            if not key or "|" not in str(key):
                continue
            try:
                row_strike = float(row.get("strike_price") or row.get("strike") or 0)
            except Exception:
                continue
            distance = abs(row_strike - float(target_strike))
            if best is None or distance < float(best_distance or 0):
                best = row
                best_distance = distance

        if not best:
            self._diag(
                stage="index_option_search",
                reason="no_search_match",
                underlying=underlying,
                option_side=option_side,
                target_strike=target_strike,
                candidate_count=len(candidates),
            )
            return None

        key = str(best.get("instrument_key"))
        symbol = best.get("trading_symbol") or best.get("tradingsymbol") or str(key)
        try:
            strike = float(best.get("strike_price") or best.get("strike") or target_strike)
        except Exception:
            strike = float(target_strike)
        self._diag(
            stage="index_option_search",
            reason="matched_upstox_search",
            instrument_key=key,
            trading_symbol=symbol,
            strike=strike,
            target_strike=target_strike,
        )
        return Instrument(
            symbol=symbol,
            underlying=underlying,
            exchange=domain.exchange,
            segment=domain.segment,
            instrument_key=key,
            lot_size=int(float(best.get("lot_size") or domain.get_lot_size(underlying))),
            tick_size=float(best.get("tick_size") or 0.05),
            expiry=best.get("expiry"),
            strike=strike,
            option_type=option_side,
            source=InstrumentSource.UPSTOX_MASTER,
        )

    async def _upstox_spot(self, underlying: str) -> float:
        gw = self.upstox_gateway
        key = INDEX_SPOT_KEYS.get(underlying)
        if not gw or not key:
            return 0.0
        try:
            latest_tick = getattr(gw, "latest_tick", None)
            if callable(latest_tick):
                tick = latest_tick(key)
                if isinstance(tick, dict):
                    tick_ltp = tick.get("ltp") or tick.get("last_price")
                    if tick_ltp:
                        return float(tick_ltp)
            latest_tick_by_symbol = getattr(gw, "latest_tick_by_symbol", None)
            if callable(latest_tick_by_symbol):
                for alias in (key.split("|")[-1], underlying, underlying.replace("BANKNIFTY", "NIFTY BANK")):
                    tick = latest_tick_by_symbol(alias)
                    if isinstance(tick, dict):
                        tick_ltp = tick.get("ltp") or tick.get("last_price")
                        if tick_ltp:
                            return float(tick_ltp)
            if not hasattr(gw, "get_market_quote"):
                return 0.0
            quote = gw.get_market_quote([key])
            parser = getattr(gw, "parse_quote_ltp", None)
            value = parser(quote, key) if parser else None
            if not value:
                node = (quote.get("data") or {}).get(key) or {}
                value = node.get("last_price") or node.get("ltp")
            if not value and isinstance(quote, dict):
                for node in (quote.get("data") or {}).values():
                    if isinstance(node, dict):
                        value = node.get("last_price") or node.get("ltp")
                        if value:
                            break
            return float(value or 0)
        except Exception as exc:
            self._diag(stage="index_spot_price", reason=f"upstox_quote_error:{exc}", instrument_key=key)
            return 0.0

    async def _paper_spot(self, underlying: str) -> float:
        try:
            cache = await self.db.paper_quote_cache.find_one({"symbol": underlying})
            value = float((cache or {}).get("ltp") or 0)
            if value > 0:
                return value
        except Exception:
            pass
        return float(PAPER_SPOT_HINTS.get(underlying, 100.0))

    async def _paper_simulated_index_option(self, underlying: str, option_side: str, strike: int) -> Instrument:
        domain = resolve_domain_by_underlying(underlying)
        key = f"PAPER_{underlying}_{option_side}_{strike}"
        self._diag(stage="paper_simulated", reason="index_option_master_missing", instrument_key=key, strike=strike)
        return Instrument(
            symbol=f"{underlying}{int(strike)}{option_side}_PAPER",
            underlying=underlying,
            exchange=domain.exchange,
            segment=domain.segment,
            instrument_key=key,
            lot_size=domain.get_lot_size(underlying),
            tick_size=0.05,
            strike=float(strike),
            option_type=option_side,
            source=InstrumentSource.PAPER_SIMULATED,
        )

    async def resolve_instrument(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper returning the legacy dict shape."""
        inst = await self.resolve_instrument_with_source(*args, **kwargs)
        if not inst:
            return None
        return {
            "tradingsymbol": inst.symbol,
            "trading_symbol": inst.symbol,
            "exchange": inst.exchange,
            "segment": inst.segment,
            "instrument_token": inst.instrument_key,
            "instrument_key": inst.instrument_key,
            "asset_class": "OPTION_LONG" if inst.option_type else "DIRECT",
            "asset_type": "option" if inst.option_type else "commodity",
            "option_type": inst.option_type,
            "strike": inst.strike,
            "lot_size": inst.lot_size,
            "tick_size": inst.tick_size,
            "expiry": inst.expiry,
            "underlying": inst.underlying,
            "source": _source_value(inst.source),
            "simulated": inst.source == InstrumentSource.PAPER_SIMULATED,
            "resolver_stage": self.last_diagnostics.get("stage"),
            "resolver_reason": self.last_diagnostics.get("reason"),
        }

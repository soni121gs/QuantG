"""QuantG platform risk manager.

This is intentionally not a strategy-quality filter. It enforces account,
session, wallet/margin and optional kill-switch constraints before execution.
"""
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone, timedelta
import logging
import os
import re

from core.market_domains import resolve_domain_by_underlying, DomainType
from core.market_session_service import MarketSessionService
from risk_controls import SizeInputs, SizeResult, compute_position_size, evaluate_market_data_quality
from core.models import PositionDoc, OrderDoc, StrategyDoc

logger = logging.getLogger("quantg.risk_manager")

# Option symbols end with strike digits + CE/PE (e.g. NIFTY26JUN24850CE).
# A substring check like `"CE" in symbol` wrongly matches equities (RELIANCE).
_OPTION_SUFFIX_RE = re.compile(r"\d(CE|PE)$")


def _option_type_of(symbol: str) -> Optional[str]:
    """Return 'CE' / 'PE' if the symbol is an option tradingsymbol, else None."""
    m = _OPTION_SUFFIX_RE.search((symbol or "").upper().strip())
    return m.group(1) if m else None

class RiskManager:
    def __init__(self, db):
        self.db = db

    async def evaluate_order(
        self,
        user_id: str,
        strategy_id: str,
        symbol: str,                   # Underlying (e.g. NIFTY, CRUDEOILM)
        target_symbol: str,            # Real traded instrument (e.g. NIFTY26FEB24850CE)
        side: str,                     # "BUY" | "SELL"
        requested_qty: int,
        price: float,
        mode: str,                     # "paper" | "live" | "backtest"
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        lot_size: int = 1,
        risk_style: str = "balanced",
        product: Optional[str] = None,
    ) -> Dict[str, Any]:  # returns {ok, status, reason, quantity}
        """Performs pre-trade risk and parameter evaluations for a potential order placement.
        
        Returns a dict: {"ok": bool, "status": str, "reason": str, "quantity": int}
        """
        # 1. Global Kill-Switch Check
        kill_switch = await self.db.risk_state.find_one({"_id": "global_kill_switch"})
        if kill_switch and kill_switch.get("active"):
            return {
                "ok": False,
                "status": "REJECTED_KILL_SWITCH",
                "reason": "Global trade kill-switch is active.",
                "quantity": 0
            }

        # 2. Live Arming Firewall
        if mode == "live":
            arm_state = await self.db.live_arm_state.find_one({"user_id": user_id})
            if not arm_state or not arm_state.get("global_live_enabled") or not arm_state.get("armed"):
                return {
                    "ok": False,
                    "status": "REJECTED_ARM_FIREWALL",
                    "reason": "Live trading is not armed or enabled.",
                    "quantity": 0
                }

        # 2b. Broker reconciliation mismatch gate (live entry orders only).
        # Blocks new entries when the local ledger disagrees with the broker's live
        # portfolio. Exits are still permitted so open positions can be closed.
        if mode == "live" and side.upper() == "BUY":
            recon = await self.db.risk_state.find_one({"_id": f"position_reconciliation:{user_id}"})
            if recon and recon.get("mismatch_detected"):
                return {
                    "ok": False,
                    "status": "REJECTED_RECONCILIATION_MISMATCH",
                    "reason": (
                        "Live entry blocked: broker position reconciliation mismatch detected. "
                        f"Mismatches: {recon.get('mismatches', [])}. "
                        "Resolve positions and re-run reconciliation before placing new entries."
                    ),
                    "quantity": 0,
                }

        # 3. Strategy config lookup for sizing only. Do not block because a
        # strategy is "bad", halted, low confidence, or otherwise app-judged.
        strategy = await self.db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if not strategy:
            return {
                "ok": False,
                "status": "REJECTED_STRATEGY_MISSING",
                "reason": "Strategy not found.",
                "quantity": 0
            }

        # 4. Market schedule domain checking (applies uniformly to all modes)
        # Backtest mode bypasses market hours checks via readiness_checker
        domain = resolve_domain_by_underlying(symbol)
        if mode != "backtest":
            segment_open = MarketSessionService.is_segment_open(domain.name)
            if not segment_open:
                return {
                    "ok": False,
                    "status": "REJECTED_MARKET_CLOSED",
                    "reason": f"Market domain {domain.name.value} is closed. Check trading hours.",
                    "quantity": 0
                }

        # 5. User Account and Sizing Gates
        user = await self.db.users.find_one({"id": user_id})
        settings = dict((user or {}).get("settings") or {})
        if user:
            for key in ("per_strategy_capital", "max_position_size", "max_daily_loss"):
                if key in user and key not in settings:
                    settings[key] = user.get(key)
        
        # Optional user-enabled daily loss kill switch.
        visual_risk_cfg = strategy.get("visual_config", {}).get("risk", {})
        daily_loss_enabled = bool(
            settings.get("daily_loss_kill_switch_enabled")
            or settings.get("daily_loss_guard_enabled")
            or visual_risk_cfg.get("daily_loss_enabled")
            or visual_risk_cfg.get("daily_loss_kill_switch_enabled")
        )
        daily_loss_limit = float(visual_risk_cfg.get("daily_loss_limit") or settings.get("max_daily_loss") or 0.0)
        daily_loss = float(strategy.get("today_pnl") or 0.0)
        if daily_loss_enabled and daily_loss_limit > 0 and daily_loss < -daily_loss_limit:
            return {
                "ok": False,
                "status": "REJECTED_DAILY_LOSS_LIMIT",
                "reason": f"Strategy daily loss limit breached: {daily_loss} < -{daily_loss_limit}",
                "quantity": 0,
                "detail": {
                    "reason_code": "REJECTED_DAILY_LOSS_LIMIT",
                    "human_reason": f"today_pnl {daily_loss} below -{daily_loss_limit}",
                    "today_pnl": daily_loss,
                    "daily_loss_limit": daily_loss_limit,
                },
            }

        # 6. Sizing computation
        free_margin = 100000.0
        if mode == "paper":
            wallet = await self.db.paper_wallets.find_one({"user_id": user_id})
            if isinstance(wallet, dict):
                free_margin = float(wallet.get("balance") or wallet.get("available_balance") or free_margin)
        elif user and "funds" in user:
            free_margin = float(user["funds"].get("free_margin") or free_margin)

        visual = strategy.get("visual_config") or {}
        visual_risk = visual.get("risk") or {}
        visual_options = visual.get("options") or {}
        # required_capital is reference-only metadata (display/planning); sizing always
        # uses the actual wallet balance so a low required_capital never blocks orders.
        equity = free_margin
        max_pos_value = float(settings.get("max_position_size") or 0) or max(equity, requested_qty * price)

        # Hard lot ceiling: explicit max_lot if set, else the configured base lots.
        # Neither equity-based sizing nor the adaptive allocator may exceed this —
        # it is the user's intended per-trade size, independent of wallet balance.
        configured_lots = int(float(visual_options.get("lots") or 1) or 1)
        max_lot = int(float(visual_risk.get("max_lot") or 0) or 0)
        ceiling_lots = max_lot if max_lot > 0 else max(1, configured_lots)

        margin_requirement_ratio = 1.0
        if domain.name in (DomainType.NSE_EQ, DomainType.BSE_EQ):
            resolved_product = str(product or "MIS").upper().strip()
            if resolved_product in ("MIS", "I", "CO"):
                margin_requirement_ratio = 0.20
            else:
                margin_requirement_ratio = 1.0
        else:
            margin_requirement_ratio = 1.0

        size_inputs = SizeInputs(
            equity=equity,
            free_margin=free_margin,
            requested_qty=requested_qty,
            lot_size=lot_size,
            entry_price=price,
            stop_loss_price=stop_loss,
            max_position_value=max_pos_value,
            daily_loss_limit=daily_loss_limit,
            risk_style=risk_style,
            max_lot=ceiling_lots,
            margin_requirement_ratio=margin_requirement_ratio,
        )
        
        size_res = compute_position_size(size_inputs)
        if not size_res.allowed:
            return {
                "ok": False,
                "status": "REJECTED_RISK_SIZING",
                "reason": f"Position sizing failed: {size_res.reason}",
                "quantity": 0,
                # Structured detail so a sizing rejection is diagnosable from the
                # signal record: which cap bound (caps), and the inputs that drove it.
                "detail": {
                    "reason_code": "REJECTED_RISK_SIZING",
                    "human_reason": size_res.reason,
                    "caps": size_res.caps,
                    "lot_size": lot_size,
                    "entry_price": price,
                    "equity": round(equity, 2),
                    "free_margin": round(free_margin, 2),
                    "risk_budget": round(size_res.risk_budget, 2),
                    "unit_loss_at_stop": round(size_res.unit_loss_at_stop, 4),
                },
            }

        # 6b. Adaptive capital allocation — scale quantity by performance + regime
        if mode != "backtest" and side.upper() == "BUY":
            try:
                from core.capital_allocator import get_strategy_allocation_multiplier
                from risk_controls import SizeResult as _SizeResult
                alloc_mult = await get_strategy_allocation_multiplier(self.db, strategy_id, user_id)
                if alloc_mult != 1.0:
                    raw_qty = size_res.quantity
                    scaled = max(lot_size, int(raw_qty * alloc_mult))
                    if lot_size > 1:
                        scaled = (scaled // lot_size) * lot_size
                        scaled = max(lot_size, scaled)
                    size_res = _SizeResult(
                        allowed=size_res.allowed,
                        quantity=scaled,
                        reason=size_res.reason,
                        risk_budget=size_res.risk_budget,
                        unit_loss_at_stop=size_res.unit_loss_at_stop,
                        order_value=size_res.order_value,
                        caps=size_res.caps,
                    )
            except Exception as alloc_exc:
                logger.warning("[ALLOC] multiplier error for %s: %s — using unscaled qty", strategy_id, alloc_exc)

        # 6c. Authoritative max-lot ceiling — final guard so neither equity-based
        # sizing nor the adaptive allocator can place more than the configured lots.
        ceiling_qty = max(lot_size, ceiling_lots * lot_size)
        if size_res.quantity > ceiling_qty:
            logger.info(
                "[SIZING] strategy=%s qty capped %d -> %d (max_lot=%d, lot_size=%d)",
                strategy_id, size_res.quantity, ceiling_qty, ceiling_lots, lot_size,
            )
            size_res = SizeResult(
                allowed=size_res.allowed,
                quantity=ceiling_qty,
                reason=f"{size_res.reason}; capped to max_lot={ceiling_lots}",
                risk_budget=size_res.risk_budget,
                unit_loss_at_stop=size_res.unit_loss_at_stop,
                order_value=round(ceiling_qty * price, 2),
                caps=size_res.caps,
            )

        # 7. Options Greeks exposure check (applies when trading options)
        sym_upper = (target_symbol or "").upper()
        if _option_type_of(sym_upper):
            greeks_result = await self._check_greeks_exposure(
                user_id=user_id,
                target_symbol=sym_upper,
                side=side,
                quantity=size_res.quantity,
                lot_size=lot_size,
                mode=mode,
                settings=settings,
                visual_risk=visual_risk,
                strategy_id=strategy_id,
            )
            if not greeks_result["ok"]:
                return {
                    "ok": False,
                    "status": "REJECTED_GREEKS_LIMIT",
                    "reason": greeks_result["reason"],
                    "quantity": 0,
                    "detail": {
                        "reason_code": "REJECTED_GREEKS_LIMIT",
                        "human_reason": greeks_result.get("reason"),
                        **{k: v for k, v in greeks_result.items() if k not in ("ok", "reason")},
                    },
                }

        return {
            "ok": True,
            "status": "APPROVED",
            "reason": "Risk verification passed.",
            "quantity": size_res.quantity
        }

    async def _check_greeks_exposure(
        self,
        *,
        user_id: str,
        target_symbol: str,
        side: str,
        quantity: int,
        lot_size: int,
        mode: str,
        settings: dict,
        visual_risk: dict,
        strategy_id: Optional[str] = None,
    ) -> dict:
        """Check net delta exposure across all open option positions.

        Uses a 0.5 ATM delta proxy — adequate for a pre-trade exposure cap.
        CE long = +delta, CE short = -delta, PE long = -delta, PE short = +delta.
        """
        DELTA_PROXY = 0.5

        is_call = _option_type_of(target_symbol) == "CE"
        if is_call:
            order_delta = DELTA_PROXY * quantity if side == "BUY" else -DELTA_PROXY * quantity
        else:
            order_delta = -DELTA_PROXY * quantity if side == "BUY" else DELTA_PROXY * quantity

        # Resolve account equity/free margin to dynamically scale delta limits
        equity = 100000.0
        if mode == "paper":
            wallet = await self.db.paper_wallets.find_one({"user_id": user_id})
            if isinstance(wallet, dict):
                equity = float(wallet.get("balance") or wallet.get("available_balance") or equity)
        else:
            user = await self.db.users.find_one({"id": user_id})
            if user and "funds" in user:
                equity = float(user["funds"].get("free_margin") or equity)

        # Dynamic cap: 0.1% of account equity, floor of 50.0, overridable by settings or env.
        try:
            _env_delta_cap = float(os.environ.get("QUANTG_MAX_NET_DELTA", "") or 0)
        except ValueError:
            _env_delta_cap = 0.0
        max_net_delta = float(
            visual_risk.get("max_net_delta")
            or settings.get("max_net_delta")
            or _env_delta_cap
            or max(50.0, equity * 0.001)
        )

        try:
            open_positions = await self.db.strategy_positions.find(
                {"user_id": user_id, "mode": mode, "status": {"$in": ["OPEN", "FILLED", "EXITING"]}}
            ).to_list(length=200)
        except Exception:
            return {"ok": True, "net_delta": order_delta}

        portfolio_delta = 0.0
        for pos in open_positions:
            sym = (pos.get("target_symbol") or "").upper()
            pos_opt_type = _option_type_of(sym)
            if not pos_opt_type:
                continue
            pos_qty = int(pos.get("open_quantity") or pos.get("quantity") or 0)
            raw_side = (pos.get("position_side") or pos.get("side") or "BUY").upper()
            pos_side = "BUY" if raw_side in ("LONG", "BUY") else "SELL"
            pos_is_call = pos_opt_type == "CE"
            if pos_is_call:
                portfolio_delta += DELTA_PROXY * pos_qty if pos_side == "BUY" else -DELTA_PROXY * pos_qty
            else:
                portfolio_delta += -DELTA_PROXY * pos_qty if pos_side == "BUY" else DELTA_PROXY * pos_qty

        projected_delta = portfolio_delta + order_delta

        # Check if this order is reducing/closing an existing position in the same strategy
        # or if it is reducing the absolute net delta of the portfolio.
        # Exit/reduction orders should never be blocked by Greeks limits.
        is_exit_or_reduction = False
        matching_pos = next(
            (p for p in open_positions if p.get("target_symbol") == target_symbol),
            None
        )
        if matching_pos:
            pos_side_raw = (matching_pos.get("position_side") or matching_pos.get("side") or "").upper()
            if (pos_side_raw in ("LONG", "BUY") and side.upper() == "SELL") or \
               (pos_side_raw in ("SHORT", "SELL") and side.upper() == "BUY"):
                is_exit_or_reduction = True

        if is_exit_or_reduction or abs(projected_delta) <= abs(portfolio_delta):
            return {"ok": True, "net_delta": projected_delta}

        if abs(projected_delta) > max_net_delta:
            return {
                "ok": False,
                "reason": (
                    f"Net delta exposure limit breached: projected delta {projected_delta:+.1f} "
                    f"exceeds cap of ±{max_net_delta:.0f}. "
                    f"Current portfolio delta: {portfolio_delta:+.1f}, order delta: {order_delta:+.1f}."
                ),
                "net_delta": projected_delta,
            }

        return {"ok": True, "net_delta": projected_delta}

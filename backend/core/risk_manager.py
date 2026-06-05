"""QuantG platform risk manager.

This is intentionally not a strategy-quality filter. It enforces account,
session, wallet/margin and optional kill-switch constraints before execution.
"""
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone, timedelta
import logging

from core.market_domains import resolve_domain_by_underlying, DomainType
from core.market_session_service import MarketSessionService
from risk_controls import SizeInputs, compute_position_size, evaluate_market_data_quality

logger = logging.getLogger("quantg.risk_manager")

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
        risk_style: str = "balanced"
    ) -> Dict[str, Any]:
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
                "quantity": 0
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
        configured_capital = max(
            float(settings.get("per_strategy_capital") or 0),
            float(strategy.get("required_capital") or 0),
            float(visual_risk.get("required_capital") or 0),
            float(visual_options.get("required_capital") or 0),
        )
        equity = configured_capital or free_margin
        max_pos_value = float(settings.get("max_position_size") or 0) or max(equity, requested_qty * price)
        
        size_inputs = SizeInputs(
            equity=equity,
            free_margin=free_margin,
            requested_qty=requested_qty,
            lot_size=lot_size,
            entry_price=price,
            stop_loss_price=stop_loss,
            max_position_value=max_pos_value,
            daily_loss_limit=daily_loss_limit,
            risk_style=risk_style
        )
        
        size_res = compute_position_size(size_inputs)
        if not size_res.allowed:
            return {
                "ok": False,
                "status": "REJECTED_RISK_SIZING",
                "reason": f"Position sizing failed: {size_res.reason}",
                "quantity": 0
            }

        return {
            "ok": True,
            "status": "APPROVED",
            "reason": "Risk verification passed.",
            "quantity": size_res.quantity
        }

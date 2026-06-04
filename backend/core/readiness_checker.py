"""QuantG Readiness Checker.

Pre-execution validation before running strategies.
Ensures systems are ready before trading.
"""

from typing import Dict, Any, List
import logging

from core.market_domains import resolve_domain_by_underlying, DomainType
from core.market_session_service import MarketSessionService

logger = logging.getLogger("quantg.readiness_checker")


class ReadinessChecker:
    """Pre-execution validation before running strategies.
    
    Checks:
    - Live arm status (for live mode)
    - NSE/MCX master availability
    - Paper wallet initialized (for paper mode)
    - User permissions
    - Market session open
    
    Example:
        checker = ReadinessChecker(db)
        
        # Check if user can trade live
        live_ready = await checker.check_live_readiness(user_id)
        if not live_ready["ready"]:
            print(f"Live trading blocked: {live_ready['checks']}")
        
        # Check if strategy can run
        strat_ready = await checker.check_strategy_readiness(
            user_id, strategy_id, "NIFTY", mode="paper"
        )
        if not strat_ready["ready"]:
            if strat_ready.get("can_skip"):
                print("Market closed, skip this tick")
    """
    
    def __init__(self, db):
        """Initialize ReadinessChecker.
        
        Args:
            db: MongoDB database connection
        """
        self.db = db
    
    async def check_live_readiness(self, user_id: str) -> Dict[str, Any]:
        """Check if user is ready for live trading.
        
        Args:
            user_id: User ID
            
        Returns:
            dict: {
                "ready": bool,
                "checks": {
                    "live_armed": bool,
                    "upstox_authenticated": bool,
                    "nse_fo_permission": bool,
                    "mcx_fo_permission": bool
                }
            }
        """
        checks = {}
        
        try:
            # 1. Check live arm status
            arm_state = await self.db.live_arm_state.find_one({"user_id": user_id})
            checks["live_armed"] = bool(arm_state and arm_state.get("armed"))
            
            # 2. Check Upstox OAuth
            user = await self.db.users.find_one({"id": user_id})
            checks["upstox_authenticated"] = bool(
                user and user.get("upstox_token")
            )
            
            # 3. Check NSE_FO permission
            allowed_segments = user.get("allowed_segments") or [] if user else []
            checks["nse_fo_permission"] = "NSE_FO" in allowed_segments
            
            # 4. Check MCX_FO permission
            checks["mcx_fo_permission"] = "MCX_FO" in allowed_segments
            
            all_ready = all(checks.values())
            return {
                "ready": all_ready,
                "checks": checks
            }
        except Exception as e:
            logger.error(f"Live readiness check failed: {e}")
            return {
                "ready": False,
                "checks": checks,
                "error": str(e)
            }
    
    async def check_paper_readiness(self, user_id: str) -> Dict[str, Any]:
        """Check if user is ready for paper trading.
        
        Args:
            user_id: User ID
            
        Returns:
            dict: {
                "ready": bool,
                "checks": {
                    "paper_wallet_initialized": bool,
                    "user_profile_exists": bool
                }
            }
        """
        checks = {}
        
        try:
            # 1. Check paper wallet initialized
            wallet = await self.db.paper_wallets.find_one({"user_id": user_id})
            checks["paper_wallet_initialized"] = wallet is not None
            
            # 2. Check user profile exists
            user = await self.db.users.find_one({"id": user_id})
            checks["user_profile_exists"] = user is not None
            
            all_ready = all(checks.values())
            return {
                "ready": all_ready,
                "checks": checks
            }
        except Exception as e:
            logger.error(f"Paper readiness check failed: {e}")
            return {
                "ready": False,
                "checks": checks,
                "error": str(e)
            }
    
    async def check_strategy_readiness(
        self,
        user_id: str,
        strategy_id: str,
        underlying: str,
        mode: str
    ) -> Dict[str, Any]:
        """Check if a specific strategy can run.
        
        Args:
            user_id: User ID
            strategy_id: Strategy ID
            underlying: Underlying symbol (e.g., NIFTY, CRUDEOILM)
            mode: "paper" or "live"
            
        Returns:
            dict: {
                "ready": bool,
                "checks": {
                    "strategy_exists": bool,
                    "strategy_not_halted": bool,
                    "segment_open": bool,
                    ...
                },
                "can_skip": bool  # If True, skip this tick (market closed)
            }
        """
        checks = {}
        
        try:
            # 1. Check strategy exists
            strategy = await self.db.strategies.find_one({
                "id": strategy_id,
                "user_id": user_id
            })
            checks["strategy_exists"] = strategy is not None
            
            if not strategy:
                return {
                    "ready": False,
                    "checks": checks,
                    "can_skip": False
                }
            
            # 2. Check strategy not halted
            checks["strategy_not_halted"] = not strategy.get("halted")
            
            # 3. Check market segment availability
            domain = resolve_domain_by_underlying(underlying.upper())
            segment_open = MarketSessionService.is_segment_open(domain)
            checks["segment_open"] = segment_open
            
            all_ready = all(checks.values())
            
            return {
                "ready": all_ready,
                "checks": checks,
                "can_skip": not segment_open  # Skip if market closed
            }
        except Exception as e:
            logger.error(f"Strategy readiness check failed: {e}")
            return {
                "ready": False,
                "checks": checks,
                "can_skip": False,
                "error": str(e)
            }
    
    async def check_batch_strategies(
        self,
        user_id: str,
        strategy_ids: List[str],
        mode: str
    ) -> Dict[str, Any]:
        """Check readiness for multiple strategies.
        
        Args:
            user_id: User ID
            strategy_ids: List of strategy IDs
            mode: "paper" or "live"
            
        Returns:
            dict: Readiness status for each strategy
        """
        try:
            mode_ready = (
                await self.check_live_readiness(user_id)
                if mode == "live"
                else await self.check_paper_readiness(user_id)
            )
            
            if not mode_ready["ready"]:
                return {
                    "mode_ready": False,
                    "mode_checks": mode_ready["checks"],
                    "strategies": {}
                }
            
            strategy_statuses = {}
            for strat_id in strategy_ids:
                try:
                    strategy = await self.db.strategies.find_one({
                        "id": strat_id,
                        "user_id": user_id
                    })
                    
                    if not strategy:
                        strategy_statuses[strat_id] = {
                            "exists": False
                        }
                        continue
                    
                    underlying = strategy.get("underlying", "NIFTY")
                    readiness = await self.check_strategy_readiness(
                        user_id, strat_id, underlying, mode
                    )
                    strategy_statuses[strat_id] = readiness
                except Exception as e:
                    logger.error(f"Failed to check strategy {strat_id}: {e}")
                    strategy_statuses[strat_id] = {
                        "error": str(e)
                    }
            
            return {
                "mode_ready": True,
                "mode_checks": mode_ready["checks"],
                "strategies": strategy_statuses
            }
        except Exception as e:
            logger.error(f"Batch readiness check failed: {e}")
            return {
                "mode_ready": False,
                "error": str(e),
                "strategies": {}
            }

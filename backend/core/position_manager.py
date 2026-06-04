"""QuantG Position Manager.

Position lifecycle management: create, open, close, reconcile.
Prevents duplicate entries and tracks position state for cleanup.
"""

from typing import Optional
import uuid
from datetime import datetime, timezone, timedelta
import logging

from core.models import Position

logger = logging.getLogger("quantg.position_manager")


class PositionManager:
    """Manages position lifecycle: create, open, close, reconcile.
    
    Key responsibilities:
    - Create positions for new entries
    - Track active positions per strategy/symbol
    - Prevent duplicate entries
    - Close positions on exit
    - Clean up stale locks
    
    Example:
        mgr = PositionManager(db)
        
        # Create position on entry
        pos = await mgr.create_position(
            user_id="user123",
            strategy_id="strat456",
            symbol="NIFTY",
            target_symbol="NIFTY26FEB24850CE",
            side="BUY",
            qty=1,
            entry_price=450.50,
            mode="paper"
        )
        
        # Check for duplicates
        has_dup = await mgr.check_duplicate_entry(
            user_id="user123",
            strategy_id="strat456",
            symbol="NIFTY",
            side="BUY"
        )
        
        # Close position on exit
        await mgr.close_position(pos.id, exit_price=460.00)
    """
    
    def __init__(self, db):
        """Initialize PositionManager.
        
        Args:
            db: MongoDB database connection
        """
        self.db = db
    
    async def create_position(
        self,
        user_id: str,
        strategy_id: str,
        symbol: str,
        target_symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        mode: str
    ) -> Position:
        """Create a new position.
        
        Args:
            user_id: User ID
            strategy_id: Strategy ID
            symbol: Underlying symbol (e.g., NIFTY)
            target_symbol: Traded instrument (e.g., NIFTY26FEB24850CE)
            side: LONG or SHORT
            qty: Position quantity
            entry_price: Entry execution price
            mode: "paper" or "live"
            
        Returns:
            Position: Created position object
        """
        position = Position(
            id=str(uuid.uuid4()),
            user_id=user_id,
            strategy_id=strategy_id,
            symbol=symbol,
            target_symbol=target_symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            entry_time=datetime.now(timezone.utc).isoformat(),
            mode=mode,
            status="OPEN"
        )
        
        try:
            await self.db.positions.insert_one({
                "id": position.id,
                "user_id": user_id,
                "strategy_id": strategy_id,
                "symbol": symbol,
                "target_symbol": target_symbol,
                "side": side,
                "qty": qty,
                "entry_price": entry_price,
                "entry_time": position.entry_time,
                "mode": mode,
                "status": "OPEN",
                "created_at": datetime.now(timezone.utc).isoformat()
            })
            logger.info(
                f"Created position {position.id} for {strategy_id} "
                f"({side} {qty}x {symbol} @ {entry_price})"
            )
        except Exception as e:
            logger.error(f"Failed to create position: {e}")
            raise
        
        return position
    
    async def get_active_positions(
        self,
        user_id: str,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None
    ) -> list[Position]:
        """Get active (OPEN) positions for user, optionally filtered.
        
        Args:
            user_id: User ID
            strategy_id: Optional strategy filter
            symbol: Optional symbol filter
            
        Returns:
            list[Position]: Active positions
        """
        query = {"user_id": user_id, "status": "OPEN"}
        if strategy_id:
            query["strategy_id"] = strategy_id
        if symbol:
            query["symbol"] = symbol
        
        try:
            docs = await self.db.positions.find(query).to_list(None)
            return [Position(**doc) for doc in docs]
        except Exception as e:
            logger.error(f"Failed to get active positions: {e}")
            return []
    
    async def close_position(
        self,
        position_id: str,
        exit_price: float
    ) -> bool:
        """Mark a position as closed.
        
        Args:
            position_id: Position ID
            exit_price: Exit execution price
            
        Returns:
            bool: True if closed successfully
        """
        try:
            result = await self.db.positions.update_one(
                {"id": position_id},
                {
                    "$set": {
                        "status": "CLOSED",
                        "exit_price": exit_price,
                        "exit_time": datetime.now(timezone.utc).isoformat()
                    }
                }
            )
            success = result.modified_count > 0
            if success:
                logger.info(f"Closed position {position_id} @ {exit_price}")
            return success
        except Exception as e:
            logger.error(f"Failed to close position {position_id}: {e}")
            return False
    
    async def clear_stale_locks(self, user_id: str, mode: str = "paper") -> int:
        """Clear stale position locks that block execution.
        
        Removes positions stuck in RESERVED state for >1 hour.
        These typically occur when orders are placed but never filled.
        
        Args:
            user_id: User ID
            mode: "paper" or "live"
            
        Returns:
            int: Number of stale locks removed
        """
        try:
            cutoff_time = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat()
            
            result = await self.db.positions.delete_many({
                "user_id": user_id,
                "mode": mode,
                "status": "RESERVED",
                "created_at": {"$lt": cutoff_time}
            })
            
            if result.deleted_count > 0:
                logger.warning(
                    f"Cleared {result.deleted_count} stale position locks for {user_id}"
                )
            
            return result.deleted_count
        except Exception as e:
            logger.error(f"Failed to clear stale locks: {e}")
            return 0
    
    async def check_duplicate_entry(
        self,
        user_id: str,
        strategy_id: str,
        symbol: str,
        side: str
    ) -> bool:
        """Check if an active position already exists for this strategy/symbol/side.
        
        Prevents duplicate entries in the same direction.
        
        Args:
            user_id: User ID
            strategy_id: Strategy ID
            symbol: Underlying symbol
            side: LONG or SHORT
            
        Returns:
            bool: True if duplicate exists
        """
        try:
            existing = await self.db.positions.find_one({
                "user_id": user_id,
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": side,
                "status": "OPEN"
            })
            return existing is not None
        except Exception as e:
            logger.error(f"Failed to check duplicate entry: {e}")
            return False

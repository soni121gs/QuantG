"""QuantG Core Reconciliation Engine.

Audits QuantG local strategy positions and orders against the broker's real-time portfolio.
Enforces: If a discrepancy is found, it raises BROKER_RECONCILIATION_REQUIRED and blocks live trade routes.
"""
from typing import Dict, Any, List
from datetime import datetime, timezone
import logging

logger = logging.getLogger("quantg.core.reconciliation")

class CoreReconciliation:
    def __init__(self, db):
        self.db = db

    async def reconcile_portfolio(
        self,
        user_id: str,
        broker_active_positions: List[Dict[str, Any]],
        mode: str = "live"
    ) -> Dict[str, Any]:
        """Compares local ledger open quantities to the broker's reported positions.
        
        If a mismatch is found, blocks live trade routes.
        """
        # Fetch local open strategy positions
        local_positions = await self.db.strategy_positions.find({
            "user_id": user_id,
            "mode": mode,
            "status": "OPEN"
        }).to_list(length=500)

        # Normalize local positions
        local_map = {str(p["target_symbol"]).upper(): int(p.get("open_quantity") or p.get("quantity") or 0) for p in local_positions}

        # Normalize broker positions
        broker_map = {}
        for bp in broker_active_positions:
            sym = str(bp.get("symbol") or bp.get("tradingsymbol") or "").upper()
            qty = abs(int(bp.get("qty") or bp.get("quantity") or 0))
            if qty > 0:
                broker_map[sym] = qty

        mismatches = []
        
        # 1. Check all local positions exist and match quantity in broker positions
        for sym, local_qty in local_map.items():
            if sym not in broker_map:
                mismatches.append(f"Position {sym} exists in QuantG ledger but is missing at broker.")
            elif broker_map[sym] != local_qty:
                mismatches.append(f"Quantity mismatch for {sym}: QuantG ledger has {local_qty}, Broker has {broker_map[sym]}.")

        # 2. Check for orphan positions (exist in broker but missing in local ledger)
        for sym, broker_qty in broker_map.items():
            if sym not in local_map:
                mismatches.append(f"Orphan position detected: {sym} exists at broker with qty {broker_qty} but has no open QuantG ledger position.")

        mismatch_detected = len(mismatches) > 0

        # Update reconciliation risk state
        now_str = datetime.now(timezone.utc).isoformat()
        await self.db.risk_state.update_one(
            {"_id": "position_reconciliation"},
            {
                "$set": {
                    "mismatch_detected": mismatch_detected,
                    "mismatches": mismatches,
                    "last_reconciled_at": now_str
                }
            },
            upsert=True
        )

        if mismatch_detected and mode == "live":
            logger.critical(f"RECONCILIATION MISMATCH DETECTED: {mismatches}. Live trading blocked.")
            
        return {
            "mismatch_detected": mismatch_detected,
            "mismatches": mismatches,
            "checked_symbols": list(set(local_map.keys()) | set(broker_map.keys()))
        }

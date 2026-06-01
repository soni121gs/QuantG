"""QuantG Strategy Signal Service.

Exposes a sandboxed interface that compiles and runs strategy code against historical/intraday candles.
Strategies are strictly read-only and are allowed ONLY to emit signals.
"""
from typing import List, Dict, Any, Tuple
from enum import Enum
import logging
from safe_exec import safe_run_strategy

logger = logging.getLogger("quantg.strategy_signal_service")

class CoreSignalType(str, Enum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    EXIT = "EXIT"
    HOLD = "HOLD"

class StrategySignalService:
    @staticmethod
    def evaluate_strategy(
        python_code: str,
        price_history: List[Dict[str, Any]],
        strategy_metadata: Dict[str, Any]
    ) -> Tuple[CoreSignalType, Dict[str, Any]]:
        """Executes the strategy inside the AST-validated sandbox.
        
        Returns a tuple: (CoreSignalType, SignalMetadata)
        """
        if not python_code:
            return CoreSignalType.HOLD, {"reason": "Empty strategy code."}
            
        try:
            # Execute in sandbox
            signals = safe_run_strategy(python_code, price_history)
            if not signals:
                return CoreSignalType.HOLD, {"reason": "No signals emitted by strategy runner."}
                
            last_sig = signals[-1]
            action = str(last_sig.get("action", "HOLD")).upper()
            
            # Map legacy BUY/SELL signals based on option visual configurations
            # default mapping:
            # - BUY signal = BUY_CALL or BUY option premium
            # - SELL signal = EXIT (if options options.enabled) or BUY_PUT
            vc = strategy_metadata.get("visual_config") or {}
            opt_cfg = vc.get("options") or {}
            
            mapped_signal = CoreSignalType.HOLD
            
            if action == "BUY":
                # Option premium buying CE or bullish option writing PE
                strike_mode = str(opt_cfg.get("strike_mode") or "").upper()
                if "SELL" in strike_mode:
                    mapped_signal = CoreSignalType.BUY_PUT  # Option writing bullish PE (represented as PE side)
                else:
                    mapped_signal = CoreSignalType.BUY_CALL
            elif action == "SELL":
                if opt_cfg.get("enabled"):
                    mapped_signal = CoreSignalType.EXIT
                else:
                    mapped_signal = CoreSignalType.BUY_PUT  # Reverse direction
            elif action == "EXIT":
                mapped_signal = CoreSignalType.EXIT
                
            return mapped_signal, {
                "raw_signal": last_sig,
                "confidence": float(last_sig.get("confidence", 85.0)),
                "date": last_sig.get("date"),
                "close": last_sig.get("close"),
                "indicators": {k: v for k, v in last_sig.items() if k not in ("action", "date", "close", "confidence")}
            }
            
        except Exception as e:
            logger.warning(f"Strategy sandboxed execution error: {e}")
            return CoreSignalType.HOLD, {"reason": f"Execution error: {str(e)[:200]}"}

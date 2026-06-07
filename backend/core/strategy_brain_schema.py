"""QuantG Strategy Brain Schema and Utilities.

Defines the V13 Strategy Brain signal schema, code hash normalizer, and hashing utilities.
"""
import re
import hashlib
from typing import Dict, Any

REQUIRED_V13_FIELDS = [
    "action",
    "direction",
    "setup_type",
    "confidence",
    "entry_reason",
    "target_R",
    "initial_stop_R",
    "trail_after_R",
    "max_hold_minutes",
    "invalidation_rule",
    "regime_required",
    "option_selection_preference",
    "signal_version",
    "strategy_logic_version",
]


def normalize_python_code(code: str) -> str:
    """Normalizes whitespace and blank lines in python code for stable hashing."""
    if not code:
        return ""
    lines = []
    for line in code.splitlines():
        line_stripped = " ".join(line.strip().split())
        if line_stripped:
            lines.append(line_stripped)
    return "\n".join(lines)


def calculate_code_hash(code: str) -> str:
    """Calculates a stable SHA256 hash of the normalized Python code."""
    normalized = normalize_python_code(code)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_signal(
    raw_signal: Dict[str, Any],
    default_strategy_version: str = "1.0",
    strategy_logic_version: str = "1.0",
) -> Dict[str, Any]:
    """Normalizes a raw strategy signal into the V13 signal schema.

    If fields are missing, fills in defaults but marks:
        signal_schema_status = "LEGACY_SIGNAL"
    If all required fields are present in raw_signal, marks:
        signal_schema_status = "V13_SIGNAL"
    """
    # Check if all required fields exist in raw_signal
    all_exist = all(field in raw_signal for field in REQUIRED_V13_FIELDS)

    normalized: Dict[str, Any] = {}

    # 1. Action: BUY or SELL
    action = raw_signal.get("action")
    if action is not None:
        action_str = str(action).upper()
        if action_str in ("BUY", "SELL"):
            normalized["action"] = action_str
        else:
            normalized["action"] = "BUY"
    else:
        normalized["action"] = "BUY"

    # 2. Direction: CE or PE
    direction = raw_signal.get("direction")
    if direction is not None:
        direction_str = str(direction).upper()
        if direction_str in ("CE", "PE"):
            normalized["direction"] = direction_str
        else:
            normalized["direction"] = "CE"
    else:
        # Infer direction from legacy fields
        reason = str(raw_signal.get("reason") or raw_signal.get("entry_reason") or "").upper()
        action_val = normalized["action"]
        if any(token in reason for token in ("PE", "PUT", "SHORT", "BEARISH", "BREAKDOWN", "DOWN")):
            normalized["direction"] = "PE"
        elif any(token in reason for token in ("CE", "CALL", "LONG", "BULLISH", "BREAKOUT", "UP")):
            normalized["direction"] = "CE"
        elif action_val == "SELL":
            normalized["direction"] = "PE"
        else:
            normalized["direction"] = "CE"

    # 3. Setup Type: default "breakout"
    normalized["setup_type"] = str(raw_signal.get("setup_type", "breakout"))

    # 4. Confidence: 0 to 100
    try:
        conf = float(raw_signal.get("confidence", 85.0))
        if not (0 <= conf <= 100):
            conf = 85.0
    except (ValueError, TypeError):
        conf = 85.0
    normalized["confidence"] = conf

    # 5. Entry Reason
    normalized["entry_reason"] = str(
        raw_signal.get("entry_reason")
        or raw_signal.get("reason")
        or "Legacy entry signal"
    )

    # 6. Target R: default 2.0
    try:
        normalized["target_R"] = float(raw_signal.get("target_R", 2.0))
    except (ValueError, TypeError):
        normalized["target_R"] = 2.0

    # 7. Initial Stop R: default 1.0
    try:
        normalized["initial_stop_R"] = float(raw_signal.get("initial_stop_R", 1.0))
    except (ValueError, TypeError):
        normalized["initial_stop_R"] = 1.0

    # 8. Trail After R: default 1.5
    try:
        normalized["trail_after_R"] = float(raw_signal.get("trail_after_R", 1.5))
    except (ValueError, TypeError):
        normalized["trail_after_R"] = 1.5

    # 9. Max Hold Minutes: default 60
    try:
        normalized["max_hold_minutes"] = int(raw_signal.get("max_hold_minutes", 60))
    except (ValueError, TypeError):
        normalized["max_hold_minutes"] = 60

    # 10. Invalidation Rule: default "time_or_stop"
    normalized["invalidation_rule"] = str(raw_signal.get("invalidation_rule", "time_or_stop"))

    # 11. Regime Required: default "any"
    normalized["regime_required"] = str(raw_signal.get("regime_required", "any"))

    # 12. Option Selection Preference: ATM, ITM1, OTM1, AUTO
    pref = raw_signal.get("option_selection_preference")
    if pref is not None:
        pref_str = str(pref).upper()
        if pref_str in ("ATM", "ITM1", "OTM1", "AUTO"):
            normalized["option_selection_preference"] = pref_str
        else:
            normalized["option_selection_preference"] = "ATM"
    else:
        normalized["option_selection_preference"] = "ATM"

    # 13. Signal Version: default 1.0
    normalized["signal_version"] = str(raw_signal.get("signal_version", default_strategy_version))

    # 14. Strategy Logic Version: default strategy_logic_version or 1.0
    normalized["strategy_logic_version"] = str(raw_signal.get("strategy_logic_version", strategy_logic_version))

    # Set status
    normalized["signal_schema_status"] = "V13_SIGNAL" if all_exist else "LEGACY_SIGNAL"

    # Maintain raw signal fields like date, close, reason for compatibility
    for k in ("date", "close", "reason"):
        if k in raw_signal:
            normalized[k] = raw_signal[k]

    return normalized

#!/usr/bin/env python3
"""
Fix false-positive in _reconcile_stale_orders_for_user.

After _sync_upstox_order_statuses() runs, valid broker-confirmed orders
have updated_at refreshed to now. This fix adds an updated_at check:
if the order was recently synced (updated_at >= stale_before), skip it
— the broker just confirmed it's alive.

Changes:
1. Add "updated_at": 1 to the MongoDB projection
2. Parse updated_at safely (same pattern as created_at)
3. If updated_at is recent (>= stale_before), skip stale marking
"""

import re

path = "backend/server.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# =========================================================
# Change 1: Add "updated_at": 1 to the MongoDB projection
# =========================================================
old_proj = (
    '"mode": 1}).to_list(500)'
)
new_proj = (
    '"mode": 1, "updated_at": 1}).to_list(500)'
)
if old_proj in content:
    content = content.replace(old_proj, new_proj, 1)
    print("Edit 1: OK - added updated_at to projection")
else:
    print("Edit 1: FAILED - projection pattern not found")
    # Debug: find what's around ".to_list(500)"
    idx = content.find('to_list(500)')
    if idx >= 0:
        print(f"  Found 'to_list(500)' at offset {idx}")
        print(f"  Context: ...{content[idx-40:idx+40]}...")

# =========================================================
# Change 2: Add updated_at parsing and check after the
# created_at check. The pattern to find:
#
#         if not created_dt or created_dt >= stale_before:
#             continue
#
#         broker_order_id = ...
# =========================================================
old_check = (
    "        if not created_dt or created_dt >= stale_before:\n"
    "            continue\n"
    "\n"
    "        broker_order_id = str(row.get(\"broker_order_id\") or \"\").strip()"
)

new_check = (
    "        if not created_dt or created_dt >= stale_before:\n"
    "            continue\n"
    "\n"
    "        # Parse updated_at — if the broker sync recently confirmed this order\n"
    "        # (updated_at >= stale_before), skip it: it's still alive at the broker.\n"
    "        updated_at = row.get(\"updated_at\")\n"
    "        updated_dt = None\n"
    "        if isinstance(updated_at, datetime):\n"
    "            updated_dt = updated_at.astimezone(timezone.utc)\n"
    "        elif isinstance(updated_at, str):\n"
    "            try:\n"
    "                updated_dt = datetime.fromisoformat(updated_at.replace(\"Z\", \"+00:00\")).astimezone(timezone.utc)\n"
    "            except Exception:\n"
    "                updated_dt = None\n"
    "        if updated_dt and updated_dt >= stale_before:\n"
    "            continue\n"
    "\n"
    "        broker_order_id = str(row.get(\"broker_order_id\") or \"\").strip()"
)

if old_check in content:
    content = content.replace(old_check, new_check, 1)
    print("Edit 2: OK - added updated_at check")
else:
    print("Edit 2: FAILED - created_at check pattern not found")
    # Debug
    idx = content.find("if not created_dt or created_dt >= stale_before")
    if idx >= 0:
        print(f"  Found 'created_dt >= stale_before' at offset {idx}")
        print(f"  Context: ...{content[idx:idx+120]}...")
        print(f"  Next 100 chars: ...{content[idx+120:idx+220]}...")
    # Try to find the line after the continue
    idx2 = content.find("        broker_order_id = str(row.get(")
    if idx2 >= 0:
        print(f"  Found 'broker_order_id' at offset {idx2}")
        print(f"  Context before: ...{content[idx2-60:idx2]}...")

# =========================================================
# Write back
# =========================================================
with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("\n========================================")
print("Fix script completed.")
print("========================================")

# =========================================================
# Verification
# =========================================================
verification_checks = [
    ("updated_at projection", '"updated_at": 1' in content and '"mode": 1, "updated_at": 1' in content),
    ("updated_at parsing", "updated_at = row.get(\"updated_at\")" in content),
    ("updated_dt check", "if updated_dt and updated_dt >= stale_before:" in content),
    ("created_at check preserved", "if not created_dt or created_dt >= stale_before:" in content),
    ("broker_order_id preserved", 'stale_status = "BROKER_NOT_FOUND"' in content),
    ("STALE preserved", 'stale_status = "STALE"' in content),
    ("lock release preserved", "strategy_position_locks.delete_many" in content),
    ("strategy_positions preserved", "strategy_positions.update_many" in content),
]

for label, ok in verification_checks:
    print(f"  {'✓' if ok else '✗'} {label}")

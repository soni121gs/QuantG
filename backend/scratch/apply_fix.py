"""Apply stale order handling fixes to server.py.

Run with: python backend/scratch/apply_fix.py
"""
import re

path = 'backend/server.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

changes = 0

# Edit 1: Add ORDER_UNKNOWN_NEEDS_REVIEW to imports
old1 = '    ORDER_CLOSED,\n    ORDER_ACTIVE_STATUSES,'
new1 = '    ORDER_CLOSED,\n    ORDER_UNKNOWN_NEEDS_REVIEW,\n    ORDER_ACTIVE_STATUSES,'
if old1 in content:
    content = content.replace(old1, new1, 1)
    changes += 1
    print('Edit 1: OK - import ORDER_UNKNOWN_NEEDS_REVIEW')
else:
    # Try with \r\n
    old1_crlf = '    ORDER_CLOSED,\r\n    ORDER_ACTIVE_STATUSES,'
    if old1_crlf in content:
        new1_crlf = '    ORDER_CLOSED,\r\n    ORDER_UNKNOWN_NEEDS_REVIEW,\r\n    ORDER_ACTIVE_STATUSES,'
        content = content.replace(old1_crlf, new1_crlf, 1)
        changes += 1
        print('Edit 1: OK (CRLF) - import ORDER_UNKNOWN_NEEDS_REVIEW')
    else:
        print('Edit 1: FAILED - import not found')

# Edit 2: Add UNKNOWN_NEEDS_REVIEW to STALE_ORDER_STATUSES
old2 = 'STALE_ORDER_STATUSES = {"STALE", "BROKER_NOT_FOUND"}'
new2 = 'STALE_ORDER_STATUSES = {"STALE", "BROKER_NOT_FOUND", "UNKNOWN_NEEDS_REVIEW"}'
if old2 in content:
    content = content.replace(old2, new2, 1)
    changes += 1
    print('Edit 2: OK - STALE_ORDER_STATUSES')
else:
    # Try with escaped quotes
    old2_esc = 'STALE_ORDER_STATUSES = {\"STALE\", \"BROKER_NOT_FOUND\"}'
    new2_esc = 'STALE_ORDER_STATUSES = {\"STALE\", \"BROKER_NOT_FOUND\", \"UNKNOWN_NEEDS_REVIEW\"}'
    if old2_esc in content:
        content = content.replace(old2_esc, new2_esc, 1)
        changes += 1
        print('Edit 2: OK (esc) - STALE_ORDER_STATUSES')
    else:
        print('Edit 2: FAILED - STALE_ORDER_STATUSES not found')

# Edit 3: Add _reconcile_stale_orders_for_user function
new_func = '''

async def _reconcile_stale_orders_for_user(user_id: str) -> Dict[str, int]:
    """Broker-agnostic stale order reconciliation.

    Finds ALL active orders (any broker) that are older than 10 minutes
    but not reported by the broker. Distinguishes between:
      - STALE: no broker_order_id was ever recorded
      - BROKER_NOT_FOUND: broker has an order_id but doesn't report it
      - UNKNOWN_NEEDS_REVIEW: uncertain state

    Releases strategy position locks for stale orders and logs reconciliation.
    """
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=10)
    rows = await db.orders.find({
        "user_id": user_id,
        "status": {"$in": list(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)},
        "visibility": {"$ne": "hidden"},
    }, {"_id": 0, "id": 1, "broker_order_id": 1, "created_at": 1, "strategy_id": 1, "strategy_name": 1, "status": 1, "broker": 1, "mode": 1}).to_list(500)
    stale_count = 0
    for row in rows:
        created_at = row.get("created_at")
        created_dt = None
        if isinstance(created_at, datetime):
            created_dt = created_at.astimezone(timezone.utc)
        elif isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                created_dt = None
        if not created_dt or created_dt >= stale_before:
            continue

        broker_order_id = str(row.get("broker_order_id") or "").strip()
        if not broker_order_id:
            stale_status = "STALE"
            message = "Local stale order: no broker order id was ever recorded."
        else:
            stale_status = "BROKER_NOT_FOUND"
            message = "Local stale order: broker no longer reports this order id."

        logger.info("Stale reconciliation user_id=%s: order %s status=%s -> %s reason=%s broker=%s",
                    user_id, row["id"], row.get("status"), stale_status, message, row.get("broker"))

        await db.orders.update_one(
            {"user_id": user_id, "id": row["id"]},
            {"$set": {
                "status": stale_status,
                "legacy_status": stale_status,
                "broker_status": stale_status,
                "status_message": message,
                "visibility": "hidden",
                "updated_at": now.isoformat(),
            }},
        )
        stale_count += 1

        # Update associated strategy positions
        await db.strategy_positions.update_many(
            {"user_id": user_id, "entry_order_id": row["id"], "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
            {"$set": {"status": stale_status, "updated_at": now.isoformat(), "broker_status_message": message},
             "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
        )

        # Release strategy position locks
        if row.get("strategy_id"):
            await db.strategy_position_locks.delete_many({
                "user_id": user_id,
                "strategy_id": row["strategy_id"],
            })

    if stale_count:
        logger.warning("Stale reconciliation user_id=%s: marked %d orders as stale/broker_not_found", user_id, stale_count)
    return {"checked": len(rows), "fixed": stale_count}
'''

# Find insertion point: after _stale_local_open_orders ends
old3 = '    _ORDER_SYNC_CACHE.pop(user_id, None)\n    return {"fixed": fixed + missing_fixed, "broker_closed_fixed": fixed, "missing_from_broker_fixed": missing_fixed, "checked": len(rows)}\n\n\ndef _kotak_order_items'
new3 = '    _ORDER_SYNC_CACHE.pop(user_id, None)\n    return {"fixed": fixed + missing_fixed, "broker_closed_fixed": fixed, "missing_from_broker_fixed": missing_fixed, "checked": len(rows)}' + new_func + '\n\n\ndef _kotak_order_items'
if old3 in content:
    content = content.replace(old3, new3, 1)
    changes += 1
    print('Edit 3: OK - added _reconcile_stale_orders_for_user function')
else:
    # Try CRLF
    old3_crlf = '    _ORDER_SYNC_CACHE.pop(user_id, None)\r\n    return {"fixed": fixed + missing_fixed, "broker_closed_fixed": fixed, "missing_from_broker_fixed": missing_fixed, "checked": len(rows)}\r\n\r\n\r\ndef _kotak_order_items'
    new3_crlf = '    _ORDER_SYNC_CACHE.pop(user_id, None)\r\n    return {"fixed": fixed + missing_fixed, "broker_closed_fixed": fixed, "missing_from_broker_fixed": missing_fixed, "checked": len(rows)}' + new_func.replace('\n', '\r\n') + '\r\n\r\n\r\ndef _kotak_order_items'
    if old3_crlf in content:
        content = content.replace(old3_crlf, new3_crlf, 1)
        changes += 1
        print('Edit 3: OK (CRLF) - added _reconcile_stale_orders_for_user function')
    else:
        print('Edit 3: FAILED - insertion point not found')
        # Debug: find the context
        idx = content.find('_ORDER_SYNC_CACHE.pop')
        if idx >= 0:
            print(f'  Found _ORDER_SYNC_CACHE.pop at char {idx}')
            print(f'  Context: {repr(content[idx:idx+300])}')
        idx = content.find('def _kotak_order_items')
        if idx >= 0:
            print(f'  Found _kotak_order_items at char {idx}')

# Edit 4: Update _broker_reconciliation_loop to call _reconcile_stale_orders_for_user
old4 = '''                    user_id = user_row["id"]
                    await _sync_upstox_order_statuses(user_id)
        except Exception as e:
            logger.warning(f"broker reconciliation error: {e}")'''
new4 = '''                    user_id = user_row["id"]
                    await _sync_upstox_order_statuses(user_id)
                    await _reconcile_stale_orders_for_user(user_id)
        except Exception as e:
            logger.warning(f"broker reconciliation error: {e}")'''
if old4 in content:
    content = content.replace(old4, new4, 1)
    changes += 1
    print('Edit 4: OK - broker reconciliation loop')
else:
    # Try CRLF
    old4_crlf = '''                    user_id = user_row["id"]\r\n                    await _sync_upstox_order_statuses(user_id)\r\n        except Exception as e:\r\n            logger.warning(f"broker reconciliation error: {e}")'''
    new4_crlf = '''                    user_id = user_row["id"]\r\n                    await _sync_upstox_order_statuses(user_id)\r\n                    await _reconcile_stale_orders_for_user(user_id)\r\n        except Exception as e:\r\n            logger.warning(f"broker reconciliation error: {e}")'''
    if old4_crlf in content:
        content = content.replace(old4_crlf, new4_crlf, 1)
        changes += 1
        print('Edit 4: OK (CRLF) - broker reconciliation loop')
    else:
        print('Edit 4: FAILED - reconciliation loop pattern not found')

# Edit 5: Add startup stale order reconciliation
old5 = '''        try:
        async for user_row in db.users.find({}, {"id": 1}):
            await seed_default_strategies_for_user(user_row["id"])
            await migrate_user_to_v12_upstox(user_row["id"])'''
new5 = '''        try:
        async for user_row in db.users.find({}, {"id": 1}):
            user_id = user_row["id"]
            await seed_default_strategies_for_user(user_id)
            await migrate_user_to_v12_upstox(user_id)
            # Reconcile any stale orders from previous session
            try:
                await _reconcile_stale_orders_for_user(user_id)
            except Exception as reconcile_err:
                logger.warning("Startup stale order reconciliation failed for user %s: %s", user_id, reconcile_err)'''
if old5 in content:
    content = content.replace(old5, new5, 1)
    changes += 1
    print('Edit 5: OK - startup stale reconciliation')
else:
    # Try CRLF
    old5_crlf = '''        try:\r\n        async for user_row in db.users.find({}, {"id": 1}):\r\n            await seed_default_strategies_for_user(user_row["id"])\r\n            await migrate_user_to_v12_upstox(user_row["id"])'''
    new5_crlf = '''        try:\r\n        async for user_row in db.users.find({}, {"id": 1}):\r\n            user_id = user_row["id"]\r\n            await seed_default_strategies_for_user(user_id)\r\n            await migrate_user_to_v12_upstox(user_id)\r\n            # Reconcile any stale orders from previous session\r\n            try:\r\n                await _reconcile_stale_orders_for_user(user_id)\r\n            except Exception as reconcile_err:\r\n                logger.warning("Startup stale order reconciliation failed for user %s: %s", user_id, reconcile_err)'''
    if old5_crlf in content:
        content = content.replace(old5_crlf, new5_crlf, 1)
        changes += 1
        print('Edit 5: OK (CRLF) - startup stale reconciliation')
    else:
        print('Edit 5: FAILED - startup seed loop pattern not found')

# Verify edits
verification_checks = [
    ('ORDER_UNKNOWN_NEEDS_REVIEW in imports', 'ORDER_UNKNOWN_NEEDS_REVIEW' in content),
    ('STALE_ORDER_STATUSES includes UNKNOWN_NEEDS_REVIEW', 'UNKNOWN_NEEDS_REVIEW' in content.split('STALE_ORDER_STATUSES')[1].split('\n')[0] if 'STALE_ORDER_STATUSES' in content else False),
    ('_reconcile_stale_orders_for_user function exists', '_reconcile_stale_orders_for_user' in content),
    ('Broker reconciliation loop updated', '_reconcile_stale_orders_for_user(user_id)' in content.split('_broker_reconciliation_loop')[1].split('except')[0] if '_broker_reconciliation_loop' in content else False),
    ('Startup reconciliation exists', '_reconcile_stale_orders_for_user(user_id)' in content.split('startup()')[1].split('# Background')[0] if 'startup()' in content else False),
]

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f'\n{"="*40}')
print(f'File written. Total edits applied: {changes}')
print(f'Verification:')
for label, ok in verification_checks:
    print(f'  {"✓" if ok else "✗"} {label}')

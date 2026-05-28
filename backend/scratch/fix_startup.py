"""Apply only the startup stale order reconciliation fix.
Run: python backend/scratch/fix_startup.py
"""
path = 'backend/server.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

old = '''    try:
        async for user_row in db.users.find({}, {"id": 1}):
            await seed_default_strategies_for_user(user_row["id"])
            await migrate_user_to_v12_upstox(user_row["id"])
            try:
                await _sync_upstox_order_statuses(user_row["id"], force=True)
            except Exception as sync_err:
                logger.warning("Startup order sync failed for user %s: %s", user_row["id"], sync_err)'''

new = '''    try:
        async for user_row in db.users.find({}, {"id": 1}):
            user_id = user_row["id"]
            await seed_default_strategies_for_user(user_id)
            await migrate_user_to_v12_upstox(user_id)
            try:
                await _sync_upstox_order_statuses(user_id, force=True)
            except Exception as sync_err:
                logger.warning("Startup order sync failed for user %s: %s", user_id, sync_err)
            try:
                await _reconcile_stale_orders_for_user(user_id)
            except Exception as reconcile_err:
                logger.warning("Startup stale order reconciliation failed for user %s: %s", user_id, reconcile_err)'''

if old in content:
    content = content.replace(old, new, 1)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print('✓ Startup stale reconciliation applied successfully')
else:
    # Try CRLF version
    old_crlf = old.replace('\n', '\r\n')
    new_crlf = new.replace('\n', '\r\n')
    if old_crlf in content:
        content = content.replace(old_crlf, new_crlf, 1)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print('✓ Startup stale reconciliation applied (CRLF)')
    else:
        print('✗ Could not find the target code. Showing context:')
        idx = content.find('seed_default_strategies_for_user')
        if idx >= 0:
            print(content[max(0,idx-50):idx+300])

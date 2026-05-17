#!/usr/bin/env python3
import re

file_path = r"D:\Quant\QuantG\backend\server.py"

with open(file_path, 'r') as f:
    content = f.read()

# 1. Add paper trading stats endpoint before get_profile
paper_stats_code = '''@api.get("/profile/paper-trading-stats")
async def paper_trading_stats(user=Depends(get_current_user)):
    """Get aggregated P&L from paper trading backtests."""
    trades = await db.paper_trading_history.find(
        {"user_id": user["id"]},
        {"_id": 0}
    ).sort("created_at", -1).to_list(1000)
    
    total_pnl = round(sum(float(t.get("pnl", 0)) for t in trades), 2)
    total_trades = sum(int(t.get("trades_count", 0)) for t in trades)
    total_wins = sum(int(t.get("wins", 0)) for t in trades)
    total_losses = sum(int(t.get("losses", 0)) for t in trades)
    
    return {
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "win_rate": round(total_wins / max(1, total_wins + total_losses) * 100, 2),
        "recent_backtests": trades[:10],
    }


'''

# Find position before get_profile and insert
pos = content.find('@api.get("/profile")')
if pos > 0:
    content = content[:pos] + paper_stats_code + content[pos:]

# 2. Update get_profile to include paper_trading_stats
old_get_profile = '''@api.get("/profile")
async def get_profile(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    _, kite_status = await get_user_kite(user["id"])
    return {
        "id": user["id"],
        "email": user["email"],
        "created_at": user["created_at"],
        **settings,
        "zerodha": kite_status,
    }'''

new_get_profile = '''@api.get("/profile")
async def get_profile(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    _, kite_status = await get_user_kite(user["id"])
    paper_stats = await paper_trading_stats(user=user)
    return {
        "id": user["id"],
        "email": user["email"],
        "created_at": user["created_at"],
        **settings,
        "zerodha": kite_status,
        "paper_trading_stats": paper_stats,
    }'''

content = content.replace(old_get_profile, new_get_profile)

# 3. Add paper trading save to backtest endpoint (before the return statement)
backtest_save_code = '''    if req.strategy_id:
        await db.strategies.update_one({"id": req.strategy_id}, {"$set": {
            "last_pnl": total_pnl,
            "last_data_source": history.get("source"),
            "last_data_live": bool(history.get("is_live")),
        }})
    
    # Save to paper trading history for profile stats
    paper_trade_doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "strategy_id": req.strategy_id,
        "symbol": target_symbol,
        "mode": "options" if options_mode else "equity",
        "pnl": total_pnl,
        "trades_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "return_pct": round(total_pnl / (starting_capital / 100), 2),
        "starting_capital": starting_capital,
        "final_equity": final_equity,
        "days_backtested": req.days,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.paper_trading_history.insert_one(paper_trade_doc)
    '''

# Find the backtest return statement and insert save code before it
pattern = r'(\s+if req\.strategy_id:\s+await db\.strategies\.update_one\(\{"id": req\.strategy_id\}, \{\"\$set\": \{\s+"last_pnl": total_pnl,\s+"last_data_source": history\.get\("source"\),\s+"last_data_live": bool\(history\.get\("is_live"\),\s+\}\}\))'
old_return = '''    if req.strategy_id:
        await db.strategies.update_one({"id": req.strategy_id}, {"$set": {
            "last_pnl": total_pnl,
            "last_data_source": history.get("source"),
            "last_data_live": bool(history.get("is_live")),
        }})
    return {'''

new_return = backtest_save_code + '''
    return {'''

if old_return in content:
    content = content.replace(old_return, new_return)

# 4. Add index for paper_trading_history
old_indexes = '''    indexes = [
        ("users", "email", {"unique": True}),
        ("broker_keys", [("user_id", 1), ("broker", 1)], {"unique": True}),
        ("strategies", "user_id", {}),
        ("orders", [("user_id", 1), ("created_at", -1)], {}),
        ("positions", [("user_id", 1), ("symbol", 1)], {"unique": True}),
    ]'''

new_indexes = '''    indexes = [
        ("users", "email", {"unique": True}),
        ("broker_keys", [("user_id", 1), ("broker", 1)], {"unique": True}),
        ("strategies", "user_id", {}),
        ("orders", [("user_id", 1), ("created_at", -1)], {}),
        ("positions", [("user_id", 1), ("symbol", 1)], {"unique": True}),
        ("paper_trading_history", [("user_id", 1), ("created_at", -1)], {}),
    ]'''

content = content.replace(old_indexes, new_indexes)

with open(file_path, 'w') as f:
    f.write(content)

print("[OK] Patched server.py with paper trading P&L tracking")

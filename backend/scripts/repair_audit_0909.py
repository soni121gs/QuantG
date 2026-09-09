"""Audited paper-only repair. Defaults to a read-only plan; --apply saves a backup first."""
import argparse
import asyncio
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient
from core.paper_broker import PaperWallet
from core.spread_lifecycle import settlement_charges
from core.hermes_diagnostics.probes_strategy import geometry_epoch
from core.hermes_diagnostics.runner import run_diagnostics
from core.hermes_diagnostics.narrator import narrate_findings


def settlement_plan(pos):
    legs = pos.get("settlement_legs") or {}
    if pos.get("settlement_source") != "intrinsic" or not {"short", "long"} <= legs.keys():
        return None
    qty = int(pos.get("quantity") or pos.get("qty") or 0)
    short, long = float(legs["short"]), float(legs["long"])
    if qty <= 0 or not all(math.isfinite(v) and v >= 0 for v in (short, long)):
        raise ValueError(f"Invalid settlement metadata: {pos.get('id')}")
    if pos.get("structure") not in ("credit_spread", "debit_spread"):
        raise ValueError("Unsupported settlement structure")
    credit = pos.get("structure") == "credit_spread"
    value = round(short - long if credit else long - short, 2)
    entry = float(pos.get("net_credit" if credit else "net_debit") or 0)
    gross = round(((entry - value) if credit else (value - entry)) * qty, 2)
    charges = round(float(pos.get("entry_charges") or 0) + settlement_charges(long, qty, pos["expiry"]), 2)
    net = round(gross - charges, 2)
    return {"exit_value": value, "gross_pnl": gross, "charges": charges,
            "realized_pnl": net, "delta": round(net - float(pos.get("realized_pnl") or 0), 2)}


async def main(args):
    if args.apply and os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() != "false":
        raise RuntimeError("Repair requires live trading disabled")
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
    db = client[os.environ.get("DB_NAME", "quantg")]
    positions = await db.strategy_positions.find({
        "mode": "paper", "status": "CLOSED", "exit_reason": "expiry-settlement",
        "settlement_source": "intrinsic", "closed_at": {"$gte": "2026-08-01"},
    }).to_list(10000)
    plans = [(p, settlement_plan(p)) for p in positions]
    plans = [(p, plan) for p, plan in plans if plan and (plan["delta"] or p.get("audit_0909"))]
    for p, plan in plans:
        uid, pid = p["user_id"], p["id"]
        wallet = await db.paper_wallets.find_one({"user_id": uid})
        epoch = (wallet or {}).get("epoch_at") or (wallet or {}).get("reset_at") or (wallet or {}).get("created_at")
        if not wallet or not epoch or str(epoch) > str(p["closed_at"]):
            raise RuntimeError(f"Wallet epoch does not cover settlement {pid}")
        fills = await db.trade_fills.find({"user_id": uid, "position_id": pid, "action": "CLOSE"}).to_list(3)
        if len(fills) != 1:
            raise RuntimeError(f"Expected one canonical close fill for {pid}, got {len(fills)}")
        if not p.get("audit_0909") and abs(float(fills[0].get("realized_pnl") or 0) - float(p.get("realized_pnl") or 0)) > 0.01:
            raise RuntimeError(f"Pre-existing fill/position disagreement for {pid}")
    summary = {"settlements": len(plans), "net_correction": round(sum(x["delta"] for _, x in plans), 2),
               "mode": "apply" if args.apply else "plan"}
    if not args.apply:
        print(json.dumps(summary))
        client.close()
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(args.backup_dir) / f"paper-audit0909-{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    collections = ["strategy_positions", "trades", "trade_fills", "orders", "trade_attribution",
                   "paper_wallets", "paper_margin_blocks", "strategies", "daily_reports",
                   "hermes_findings", "hermes_diagnostic_runs"]
    snapshot = {n: await db[n].find({}).to_list(100000) for n in collections}
    backup.write_text(json_util.dumps(snapshot), encoding="utf-8")
    backup.chmod(0o600)
    now = datetime.now(timezone.utc).isoformat()
    for p, calculated in plans:
        pid, uid, sid = p["id"], p["user_id"], p["strategy_id"]
        key = f"audit0909:{pid}"
        await db.paper_state_audit.update_one({"repair_id": key}, {"$setOnInsert": {
            "repair_id": key, "before": p, "plan": calculated, "created_at": now,
            "source": "NSE/FATAX/73524; intrinsic settlement, no trading slippage"}}, upsert=True)
        journal = await db.paper_state_audit.find_one({"repair_id": key})
        plan = journal["plan"]
        net, delta = plan["realized_pnl"], plan["delta"]
        marker = f"accounting_repairs.{pid}"
        # The balance adjustment and its idempotency marker share one Mongo write.
        await db.paper_wallets.update_one({"user_id": uid, marker: {"$exists": False}}, {
            "$inc": {"balance": delta, "total_credited": delta},
            "$set": {marker: {"repair_id": key, "delta": delta, "at": now}}})
        await db.strategy_positions.update_one({"id": pid, "user_id": uid}, {"$set": {
            "exit_value": plan["exit_value"], "gross_pnl": plan["gross_pnl"],
            "realized_pnl": net, "pnl": net, "audit_0909": key, "charges_estimated": True}})
        common = {"gross_pnl": plan["gross_pnl"], "realized_pnl": net, "net_pnl": net,
                  "charges": plan["charges"], "audit_0909": key, "charges_estimated": True}
        await db.trade_fills.update_many({"position_id": pid, "user_id": uid, "action": "CLOSE"},
                                       {"$set": {**common, "price": plan["exit_value"]}})
        await db.trades.update_many({"position_id": pid, "user_id": uid},
                                   {"$set": {**common, "pnl": net, "exit_price": plan["exit_value"], "is_win": net > 0}})
        await db.trade_attribution.update_many({"position_id": pid, "user_id": uid}, {"$set": {
            "realized_pnl": net, "exit_price": plan["exit_value"], "is_win": net > 0,
            "R_multiple": round(net / p["planned_risk"], 3) if p.get("planned_risk") else None,
            "profit_giveback": round(float(p.get("peak_pnl") or 0) - net, 2), "audit_0909": key}})
        for role in ("short", "long"):
            await db.orders.update_many({"position_id": pid, "user_id": uid,
                                         "spread_role": role, "exit_reason": "expiry-settlement"},
                                        {"$set": {"price": p["settlement_legs"][role], "audit_0909": key,
                                                  "charges": settlement_charges(p["settlement_legs"][role], int(p["quantity"]), p["expiry"]) if role == "long" else 0,
                                                  "realized_pnl": net if role == "short" else None,
                                                  "net_pnl": net if role == "short" else None}})
        await db.strategies.update_one({"id": sid, "user_id": uid, marker: {"$exists": False}}, {
            "$inc": {"total_pnl": delta}, "$set": {marker: key}})
        await db.paper_state_audit.update_one({"repair_id": key}, {"$set": {"completed_at": now}})
    users = await db.paper_wallets.distinct("user_id")
    for uid in users:
        wallet = PaperWallet(db)
        released = await wallet.release_terminal_margin(uid)
        audit = await wallet.audit_blocked_margin(uid)
        print(json.dumps({"margin": audit, **released}, default=str))
        for report in await db.daily_reports.find({"user_id": uid}).to_list(1000):
            date = report["date"]
            rows = await db.trade_fills.find({"user_id": uid, "created_at": {"$regex": "^" + date}}).to_list(10000)
            pnls = {}
            for row in rows:
                sid = row.get("strategy_id")
                pnls[sid] = pnls.get(sid, 0) + float(row.get("realized_pnl") or 0)
            if not any(str(p.get("closed_at", "")).startswith(date) for p, _ in plans):
                continue
            strategies = report.get("strategies", [])
            for row in strategies:
                row["realized_pnl"] = round(pnls.get(row.get("strategy_id"), 0), 2)
            ranked = sorted(strategies, key=lambda r: r.get("realized_pnl", 0))
            best = {"name": ranked[-1]["name"], "pnl": ranked[-1]["realized_pnl"]} if ranked and ranked[-1]["realized_pnl"] > 0 else None
            worst = {"name": ranked[0]["name"], "pnl": ranked[0]["realized_pnl"]} if ranked and ranked[0]["realized_pnl"] < 0 else None
            await db.daily_reports.update_one({"_id": report["_id"]}, {"$set": {
                "strategies": strategies, "total_realized_pnl": round(sum(pnls.values()), 2),
                "best_strategy": best, "worst_strategy": worst, "accounting_corrected_at": now}})
        for s in await db.strategies.find({"user_id": uid}).to_list(500):
            epoch = geometry_epoch(s)
            if epoch:
                await db.strategies.update_one({"id": s["id"], "user_id": uid}, {"$set": {
                    "geometry_changed_at": epoch, "visual_config.options.geometry_changed_at": epoch}})
        # Measured expiry-tail failure: preserve the idea for research without new exposure.
        await db.strategies.update_one({"user_id": uid, "id": "f390da9d-8a38-4bf6-87d9-0e560eb852e5"}, {"$set": {
            "status": "paused", "manual_paused": True, "schedule_paused": False, "founder_forced_live": False,
            "last_filter_reason": "Audit 0909: expiry tail-loss review; matched OOS and forward-paper required", "audit_review_at": now}})
        result = await run_diagnostics(db, uid)
        await narrate_findings(db, uid, result["date"], result["findings"])
    print(json.dumps({**summary, "backup": str(backup)}))
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="/data/audit-backups")
    asyncio.run(main(parser.parse_args()))

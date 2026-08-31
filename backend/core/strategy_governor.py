from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    n = len(values)
    if n % 2:
        return values[n // 2]
    return (values[n // 2 - 1] + values[n // 2]) / 2.0


def classify_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    closed = int(row.get("closed") or 0)
    open_count = int(row.get("open") or 0)
    pnl = _f(row.get("pnl"))
    avg = row.get("avg")
    avg = _f(avg) if avg is not None else None
    pf = row.get("profit_factor")
    pf = _f(pf) if pf is not None else None
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    avg_win = row.get("avg_win")
    avg_loss = row.get("avg_loss")
    avg_win = _f(avg_win) if avg_win is not None else None
    avg_loss = _f(avg_loss) if avg_loss is not None else None
    green_then_loss = int(row.get("green_then_loss") or 0)
    green_closed = int(row.get("green_closed") or 0)
    worst = row.get("worst")
    worst = _f(worst) if worst is not None else None
    reasons: List[str] = []

    if closed == 0:
        return {
            "label": "observe",
            "confidence": "none",
            "reasons": ["no closed trades in the review window"],
        }

    if closed >= 20 and (pf is None or pf < 1.05):
        reasons.append(f"profit factor {pf if pf is not None else 'n/a'} below 1.05 over {closed} closes")
    if closed >= 20 and avg is not None and avg < 0:
        reasons.append(f"negative expectancy {avg:.2f} over {closed} closes")
    if losses and avg_win and worst is not None and abs(worst) > 4.0 * avg_win:
        reasons.append(f"worst loss {worst:.2f} exceeds 4x average win {avg_win:.2f}")
    if green_then_loss >= 5 and (losses == 0 or green_then_loss / max(1, losses) > 0.5):
        denom = losses if losses else green_closed
        reasons.append(f"{green_then_loss}/{denom} losers were green before closing red")

    if closed <= 5 and pnl < 0 and worst is not None and abs(worst) > 3000:
        return {
            "label": "kill_candidate",
            "confidence": "thin_but_severe",
            "reasons": reasons or [f"thin sample but catastrophic worst loss {worst:.2f}"],
        }
    if closed >= 20 and pnl < 0:
        return {"label": "pause", "confidence": "medium", "reasons": reasons or [f"negative P&L {pnl:.2f}"]}
    if closed >= 30 and pnl > 0 and pf is not None and pf >= 1.2 and not reasons:
        return {"label": "scale_candidate", "confidence": "medium", "reasons": [f"positive {closed}-trade sample with PF {pf:.2f}"]}
    if closed < 30:
        return {
            "label": "observe",
            "confidence": "thin_sample",
            "reasons": [f"sample too thin: {closed} closes (<30)"] + reasons,
        }
    return {
        "label": "observe",
        "confidence": "medium",
        "reasons": reasons or ["positive but not strong enough to scale"],
    }


async def build_strategy_governor_report(
    db: Any,
    user_id: str,
    *,
    days: int = 30,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    days = max(1, min(int(days or 30), 120))
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_iso = since.isoformat()
    strategies = await db.strategies.find(
        {"user_id": user_id},
        {"_id": 0, "id": 1, "name": 1, "status": 1, "enabled": 1, "visual_config": 1},
    ).to_list(1000)
    by_id = {str(s.get("id")): s for s in strategies if s.get("id")}
    positions = await db.strategy_positions.find({
        "user_id": user_id,
        "$or": [
            {"created_at": {"$gte": since_iso}},
            {"closed_at": {"$gte": since_iso}},
            {"created_at": {"$gte": since}},
            {"closed_at": {"$gte": since}},
        ],
    }, {"_id": 0}).to_list(5000)
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for pos in positions:
        grouped[str(pos.get("strategy_id") or "UNKNOWN")].append(pos)

    rows: List[Dict[str, Any]] = []
    for sid in sorted(set(by_id) | set(grouped)):
        strat = by_id.get(sid, {})
        ps = grouped.get(sid, [])
        closed = [p for p in ps if p.get("status") in ("CLOSED", "EXITED") or p.get("exit_reason")]
        open_positions = [
            p for p in ps
            if p.get("status") in ("RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING")
            and not p.get("exit_reason")
        ]
        pnls = [_f(p.get("realized_pnl") if p.get("realized_pnl") is not None else p.get("pnl")) for p in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        peak_givebacks = []
        green_then_loss = 0
        green_closed = 0
        for p in closed:
            peak = _f(p.get("peak_pnl"))
            pnl = _f(p.get("realized_pnl") if p.get("realized_pnl") is not None else p.get("pnl"))
            if peak > 0:
                green_closed += 1
                peak_givebacks.append(peak - pnl)
                if pnl < 0:
                    green_then_loss += 1
        exit_reasons = Counter(str(p.get("exit_reason") or "unknown") for p in closed)
        row = {
            "strategy_id": sid,
            "name": strat.get("name") or (ps[0].get("strategy_name") if ps else sid),
            "status": strat.get("status"),
            "enabled": strat.get("enabled"),
            "structure": ((strat.get("visual_config") or {}).get("options") or {}).get("structure"),
            "trades_total": len(ps),
            "closed": len(closed),
            "open": len(open_positions),
            "pnl": round(sum(pnls), 2),
            "avg": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
            "wins": len(wins),
            "losses": len(losses),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if wins and losses else None,
            "best": round(max(pnls), 2) if pnls else None,
            "worst": round(min(pnls), 2) if pnls else None,
            "green_closed": green_closed,
            "green_then_loss": green_then_loss,
            "giveback_total": round(sum(peak_givebacks), 2),
            "giveback_median": round(_median(peak_givebacks), 2) if peak_givebacks else None,
            "exit_reasons": dict(exit_reasons.most_common()),
        }
        row["governor"] = classify_strategy(row)
        rows.append(row)

    label_counts = Counter(str(r["governor"]["label"]) for r in rows)
    rows.sort(key=lambda r: (r["governor"]["label"] != "scale_candidate", r["pnl"]), reverse=True)
    return {
        "kind": "strategy_governor",
        "days": days,
        "since": since_iso,
        "generated_at": now.isoformat(),
        "summary": dict(label_counts),
        "rules": [
            "scale_candidate requires n>=30, positive P&L, PF>=1.2, and no major risk flags",
            "pause if n>=20 with PF<1.05, negative expectancy, high green-then-loss, or outsized worst loss",
            "observe if sample is thin, inactive, or positive but not strong enough to scale",
        ],
        "strategies": rows,
        "note": "Read-only evidence. It recommends status/size actions but does not mutate strategy config or live flags.",
    }

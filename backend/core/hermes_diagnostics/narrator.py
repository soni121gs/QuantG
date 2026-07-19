"""Layer 4 — the Hermes narrator.

The ONLY place an LLM touches the diagnostician, and it may only EXPLAIN and RANK
the CONFIRMED findings it is handed. It cannot invent findings: it receives the
exact deterministic list and returns prose keyed to those findings' probe_ids. If
the model is unavailable or returns junk, a deterministic briefing is produced
from the same findings — the system is fully usable with no LLM at all.

`code computes, LLM narrates` — the narrative is never a source of truth; the
findings and their evidence are.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

import requests

logger = logging.getLogger("quantg.hermes_diagnostics")

_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _deterministic_briefing(findings: List[Dict[str, Any]], date_str: str) -> str:
    """A useful daily briefing with zero LLM involvement (the fallback + the floor)."""
    if not findings:
        return f"Hermes diagnostics {date_str}: no open findings. Book invariants hold."
    by_sev: Dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    head = ", ".join(f"{n} {s}" for s, n in sorted(by_sev.items(), key=lambda kv: _SEV_ORDER.get(kv[0], 9)))
    lines = [f"Hermes diagnostics {date_str}: {len(findings)} open finding(s) — {head}.", ""]
    for f in findings[:12]:
        lines.append(f"[{f['severity'].upper()}] {f['title']}")
        lines.append(f"    ↳ {f['detail']}")
        if f.get("suggested_fix"):
            lines.append(f"    fix: {f['suggested_fix']}")
    return "\n".join(lines)


def _gemini_narrate(findings: List[Dict[str, Any]], date_str: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return ""
    # Hand the model ONLY the fields it needs to explain — never let it add items.
    slim = [{"probe_id": f["probe_id"], "severity": f["severity"], "domain": f["domain"],
             "title": f["title"], "detail": f["detail"], "evidence": f.get("evidence", {}),
             "suggested_fix": f.get("suggested_fix", "")} for f in findings[:20]]
    prompt = f"""You are Hermes, QuantG's quantitative disciplinarian. Below is a
DETERMINISTIC list of system findings produced by code probes for {date_str}. Every
finding is already verified — numbers are exact.

Write a short daily diagnosis for the founder:
1. One opening sentence: the single most important thing to fix today, or "clean" if none are critical/high.
2. A ranked bullet per finding (most severe first) — plain English, say WHY it costs money or risk.
3. If several findings share a root cause, group them and say so.

HARD RULES:
- Use ONLY the findings below. NEVER invent a finding, number, or file. If you
  suspect something not listed, do not state it as fact — say it needs a new probe.
- Be blunt and specific. No hedging, no filler. Reference the probe_id in each bullet.

Findings JSON:
{json.dumps(slim, default=str)}
"""
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1200}}
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload, timeout=20.0,
        )
        res.raise_for_status()
        parts = res.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    except Exception as exc:  # noqa: BLE001 — narration is best-effort; never breaks the audit
        logger.warning("Hermes narrator LLM failed, using deterministic briefing: %s", exc)
        return ""


async def narrate_findings(db, user_id: str, date_str: str,
                           findings: List[Dict[str, Any]]) -> str:
    """Produce and persist the daily narrative. Always returns usable prose."""
    import asyncio
    confirmed = [f for f in findings if f.get("confidence", "confirmed") == "confirmed"]
    confirmed.sort(key=lambda f: _SEV_ORDER.get(f["severity"], 9))
    llm = await asyncio.to_thread(_gemini_narrate, confirmed, date_str)
    narrative = llm or _deterministic_briefing(confirmed, date_str)
    try:
        await db.hermes_diagnostic_runs.update_one(
            {"user_id": user_id, "date": date_str},
            {"$set": {"narrative": narrative,
                      "narrative_source": "llm" if llm else "deterministic"}},
            upsert=True,
        )
    except Exception as exc:
        logger.warning("failed to persist diagnostic narrative: %s", exc)
    return narrative

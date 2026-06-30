import os
import json
import requests
import logging
import asyncio
from typing import List

logger = logging.getLogger("quantg.embeddings")

DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_CHAT_MODEL = "gemini-2.5-flash"

def _generate_gemini_embedding_sync(text: str) -> List[float]:
    """Sends a synchronous HTTP POST request to Google Gemini's embedContent endpoint."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set. Returning a zero vector placeholder.")
        return [0.0] * 768  # gemini-embedding-001 is set to 768-dimensional

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{DEFAULT_EMBEDDING_MODEL}:embedContent"
    payload = {
        "content": {
            "parts": [{"text": text}]
        },
        "output_dimensionality": 768
    }
    try:
        res = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=10.0
        )
        res.raise_for_status()
        data = res.json()
        embedding_vals = data.get("embedding", {}).get("values", [])
        if len(embedding_vals) == 768:
            return embedding_vals
        else:
            logger.warning(f"Unexpected embedding dimension: {len(embedding_vals)}. Expected 768.")
            return [0.0] * 768
    except Exception as e:
        logger.error(f"Failed to generate Gemini embedding for text: {e}")
        return [0.0] * 768


async def generate_gemini_embedding(text: str) -> List[float]:
    """Generates a text embedding vector asynchronously by delegation to a thread pool."""
    return await asyncio.to_thread(_generate_gemini_embedding_sync, text)


def _gemini_distill_memory_sync(summary_data: str) -> List[str]:
    """Sends a daily summary to Gemini to distill it into 1-3 atomic facts in JSON format."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set. Returning empty facts.")
        return []

    model = os.environ.get("GEMINI_MODEL", DEFAULT_CHAT_MODEL)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    
    prompt = f"""
You are the QuantG memory compiler. Analyze the daily performance report, signals, and alerts below.
Distill them into 1 to 3 atomic, high-signal facts to remember for long-term quantitative research.

CRITICAL INSTRUCTIONS:
- Each fact must be a single sentence.
- Concise, objective, and contain actual numbers or specific reasons.
- Do not include meta-commentary, introductory phrases, or markdown formatting (like bolding).
- Focus on what worked, what failed, drawdown limit events, feed/token failures, or signal blocks.
- Format the response as a JSON array of strings: ["Fact 1", "Fact 2"]

Data to analyze:
{summary_data}
"""
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 600,
            "responseMimeType": "application/json"
        },
    }
    
    try:
        res = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=15.0
        )
        res.raise_for_status()
        data = res.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
        
        try:
            facts = json.loads(text)
            if isinstance(facts, list):
                return [str(f) for f in facts]
        except Exception:
            logger.warning(f"Failed to parse Gemini JSON output: {text}")
            # Fallback line extraction
            lines = [l.strip("-* ").strip() for l in text.split("\n") if l.strip()]
            return [l for l in lines if len(l) > 10][:3]
            
    except Exception as e:
        logger.error(f"Failed to distill daily memory: {e}")
        
    return []


async def distill_daily_report_to_facts(summary_data: str) -> List[str]:
    """Distills a daily report summary into facts asynchronously."""
    return await asyncio.to_thread(_gemini_distill_memory_sync, summary_data)


# ── HSI-22: structured, sample-size-honest distillation from Stage-1 attribution ──
_MIN_SAMPLE_FOR_FACT = 5


def _gemini_distill_observations_sync(attribution_json: str) -> List[dict]:
    """Distill deterministic attribution rollups into STRUCTURED observations.

    Unlike the prose distiller, every observation must cite a metric + value +
    sample_size + dimension, and any bucket with n<5 must be labelled an
    'insufficient sample' (never asserted as a confident fact). The numbers come
    from code (Stage 1) — the LLM only chooses what is worth remembering and
    phrases the claim. Returns a list of
    {claim, dimension, metric, value, sample_size, confidence}.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set. Returning empty observations.")
        return []

    model = os.environ.get("GEMINI_MODEL", DEFAULT_CHAT_MODEL)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    prompt = f"""You are the QuantG quantitative memory compiler. Below is a DETERMINISTIC
trade-attribution rollup (computed by code, every number is exact) grouping today's and
this week's closed trades by dimension (structure, regime, exposure_bias, exit_reason,
strategy). Each bucket has: n (sample size), net_pnl, win_rate, avg_R, expectancy.

Produce 2 to 6 high-signal STRUCTURED observations worth remembering for long-term research.

HARD RULES (sample-size honesty — non-negotiable):
- Every observation MUST reference a real bucket and cite its exact metric, value, and n.
- If a bucket has n < {_MIN_SAMPLE_FOR_FACT}, you may still note it but you MUST set
  "confidence": "insufficient_sample" and phrase the claim as tentative ("early signal,
  small sample"). NEVER state an n<{_MIN_SAMPLE_FOR_FACT} bucket as a confident fact.
- Prefer the worst and best expectancy buckets, and week-to-date trends.
- Do not invent numbers. Only use values present in the data.
- "value" is the headline metric's numeric value (e.g. the expectancy or net_pnl).
- "dimension" is one of: structure, regime, exposure_bias, exit_reason, strategy, overall.
- "metric" is one of: expectancy, net_pnl, win_rate, avg_R.

Return ONLY a JSON array of objects, each:
{{"claim": str, "dimension": str, "metric": str, "value": number, "sample_size": int, "confidence": "high"|"insufficient_sample"}}

Attribution data:
{attribution_json}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1000,
            "responseMimeType": "application/json",
        },
    }

    try:
        res = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=20.0,
        )
        res.raise_for_status()
        data = res.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
        try:
            obs = json.loads(text)
        except Exception:
            logger.warning(f"Failed to parse Gemini observations JSON: {text[:300]}")
            return []
        if not isinstance(obs, list):
            return []
        clean = []
        for o in obs:
            if not isinstance(o, dict) or not o.get("claim"):
                continue
            try:
                n = int(o.get("sample_size") or 0)
            except Exception:
                n = 0
            # Enforce the honesty rule even if the model ignored it.
            conf = str(o.get("confidence") or "").lower()
            if n < _MIN_SAMPLE_FOR_FACT:
                conf = "insufficient_sample"
            elif conf not in ("high", "insufficient_sample"):
                conf = "high"
            clean.append({
                "claim": str(o.get("claim")),
                "dimension": str(o.get("dimension") or "overall"),
                "metric": str(o.get("metric") or "expectancy"),
                "value": o.get("value"),
                "sample_size": n,
                "confidence": conf,
            })
        return clean
    except Exception as e:
        logger.error(f"Failed to distill attribution observations: {e}")
        return []


async def distill_attribution_to_observations(attribution_json: str) -> List[dict]:
    """Async wrapper for the structured, sample-size-honest attribution distiller."""
    return await asyncio.to_thread(_gemini_distill_observations_sync, attribution_json)

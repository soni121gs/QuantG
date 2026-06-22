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
        "config": {
            "output_dimensionality": 768
        }
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

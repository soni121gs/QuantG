"""Hermes Diagnostician (CLAUDE.md §19 / HSI Stage 6).

A daily, deterministic system auditor. It finds problems — across trading logic,
strategy edge, infrastructure and data — and files them with evidence so agents
(or the founder) can fix them. It NEVER trades and NEVER edits code; it finds and
files, exactly like the rest of Hermes.

Governing law (same as HSI): **code computes every finding, the LLM only
narrates**. A `Finding` is the output of a deterministic probe with attached raw
evidence and a reproduction query. The narrator (Layer 4) may explain and rank
CONFIRMED findings but may NEVER invent one — if it suspects something new it must
propose a *probe*, which gets codified and verified, never trusted raw. This is
the anti-hallucination spine: an LLM can be confidently wrong; a probe that counts
`exits where reason in (TP, SL)` cannot.

Layers:
  1 PROBES     deterministic detectors (probes_*.py) → Finding + evidence
  2 RUNNER     runs all probes, dedups vs open findings, scores, persists
  3 VERIFIER   sample-size / freshness gate → CONFIRMED | PLAUSIBLE | INSUFFICIENT_DATA
  4 NARRATOR   Hermes LLM explains & ranks CONFIRMED findings (narrator.py)
  5 LOOP       re-run next cycle auto-verifies a fix (finding disappears → resolved)
"""
from core.hermes_diagnostics.contract import (
    Finding, Severity, Confidence, Domain, finding_key,
)
from core.hermes_diagnostics.runner import run_diagnostics

__all__ = [
    "Finding", "Severity", "Confidence", "Domain", "finding_key",
    "run_diagnostics",
]

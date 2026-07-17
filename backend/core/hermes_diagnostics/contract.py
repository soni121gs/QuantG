"""The Finding contract — the unit of truth the diagnostician produces.

A Finding is a deterministic fact with attached evidence. It is NOT an opinion.
Every field except `narrative` is filled by code; `narrative` (set later by the
LLM narrator) only explains what the numbers already prove.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


class Domain:
    EXECUTION = "execution"   # trade-logic / order execution invariants
    STRATEGY = "strategy"     # strategy & edge integrity
    INFRA = "infra"           # feed / loops / system health
    DATA = "data"             # data-store integrity


class Severity:
    CRITICAL = "critical"     # losing money now / correctness broken (e.g. stop engine dead)
    HIGH = "high"             # will lose money under a plausible regime (e.g. no-op stop)
    MEDIUM = "medium"         # degraded / risky but bounded
    LOW = "low"               # hygiene / observability

    _ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}

    @classmethod
    def rank(cls, sev: str) -> int:
        return cls._ORDER.get(sev, 9)


class Confidence:
    CONFIRMED = "confirmed"                 # deterministic + enough evidence
    PLAUSIBLE = "plausible"                 # deterministic but thin/edge evidence
    INSUFFICIENT_DATA = "insufficient_data" # not enough data to assert — never a guess


def finding_key(probe_id: str, entity: str) -> str:
    """Stable dedup key. One open finding per (probe, entity) — a recurring issue
    bumps occurrences/last_seen instead of spawning duplicates each day."""
    return f"{probe_id}::{entity or 'system'}"


@dataclass
class Finding:
    probe_id: str                 # stable slug, e.g. "exec.intent_vs_execution_side"
    domain: str                   # Domain.*
    severity: str                 # Severity.*
    title: str                    # one-line statement of the defect
    detail: str                   # what it is + why it matters (still code-authored)
    entity: str = "system"        # what it's about: strategy_id / underlying / "system"
    confidence: str = Confidence.CONFIRMED
    evidence: Dict[str, Any] = field(default_factory=dict)   # raw numbers/docs that PROVE it
    reproduction: str = ""        # exact query/command to re-verify independently
    suggested_fix: str = ""       # file:line hint / direction — NOT applied
    narrative: str = ""           # LLM-authored explanation (Layer 4 only); never a source of truth

    @property
    def key(self) -> str:
        return finding_key(self.probe_id, self.entity)

    def to_doc(self) -> Dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        return d

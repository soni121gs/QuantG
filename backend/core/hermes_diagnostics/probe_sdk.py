"""Probe SDK — registration, context, and safe execution.

A probe is `async def probe(ctx: ProbeContext) -> list[Finding]`. It is pure-read
and MUST NOT write to the DB or place orders. It registers itself with
`@register(...)`. The runner preloads the day's common data once into the context
so probes don't each re-query.

A probe that raises does NOT break the run: the runner catches it and turns it
into a single low-severity `infra.probe_error` finding, so a bug in the auditor is
itself surfaced rather than silently swallowing the whole audit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.hermes_diagnostics.contract import Finding

logger = logging.getLogger("quantg.hermes_diagnostics")

ProbeFn = Callable[["ProbeContext"], Awaitable[List[Finding]]]


@dataclass
class _ProbeSpec:
    probe_id: str
    kind: str          # "static" (config/logic) | "dynamic" (needs the day's trades)
    fn: ProbeFn


_REGISTRY: Dict[str, _ProbeSpec] = {}


def register(probe_id: str, kind: str = "dynamic"):
    """Decorator registering a probe under a stable id. Duplicate ids overwrite
    (so a reload during dev doesn't multiply probes)."""
    def _wrap(fn: ProbeFn) -> ProbeFn:
        _REGISTRY[probe_id] = _ProbeSpec(probe_id=probe_id, kind=kind, fn=fn)
        return fn
    return _wrap


def all_probes() -> List[_ProbeSpec]:
    return list(_REGISTRY.values())


@dataclass
class ProbeContext:
    """Preloaded, read-only snapshot the probes share. Built once per run by the
    runner so N probes don't issue N× the queries."""
    db: Any
    user_id: str
    date_str: str                                  # IST YYYY-MM-DD of the audited day
    strategies: List[Dict[str, Any]] = field(default_factory=list)      # all strategy docs
    closed_today: List[Dict[str, Any]] = field(default_factory=list)    # CLOSED positions opened/closed today
    open_positions: List[Dict[str, Any]] = field(default_factory=list)  # currently OPEN/FILLED
    signals_today: List[Dict[str, Any]] = field(default_factory=list)   # today's signals (any status)
    daily_report: Optional[Dict[str, Any]] = None
    now_ist_hm: str = ""                            # "HH:MM" IST at run time
    in_market_hours: bool = False

    def strategy_by_id(self, sid: str) -> Optional[Dict[str, Any]]:
        for s in self.strategies:
            if s.get("id") == sid or s.get("strategy_id") == sid:
                return s
        return None


async def run_probe_safe(spec: _ProbeSpec, ctx: ProbeContext) -> List[Finding]:
    """Execute one probe, converting any exception into a surfaced finding."""
    try:
        out = await spec.fn(ctx)
        return out or []
    except Exception as exc:  # noqa: BLE001 — a probe crash must never abort the audit
        logger.warning("hermes probe %s crashed: %s", spec.probe_id, exc)
        return [Finding(
            probe_id="infra.probe_error",
            domain="infra",
            severity="low",
            title=f"Diagnostic probe {spec.probe_id} crashed",
            detail=("A probe raised while auditing — the auditor itself has a bug, "
                    "so this issue is surfaced rather than hidden."),
            entity=spec.probe_id,
            evidence={"error": str(exc)},
            reproduction=f"run probe {spec.probe_id} for {ctx.user_id} {ctx.date_str}",
            suggested_fix="Inspect the named probe in backend/core/hermes_diagnostics/",
        )]

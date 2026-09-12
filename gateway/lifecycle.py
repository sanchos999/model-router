"""R4 lifecycle — canonical model statuses, dominance and auto-transitions.

Statuses (spec R4 §4):
  CORE           — normal routing pool
  SPECIALIST     — strong for a specific task class
  WATCH          — frontier/new, insufficient quality evidence
  FALLBACK_ONLY  — not primary, useful on failover
  DOMINATED      — another canonical is proven not-worse on every axis AND
                   strictly better on at least one (evidence-backed)
  DEPRECATED     — provider marked deprecated, or proven successor exists
  SUNSET         — concrete shutdown date known
  UNAVAILABLE    — currently no eligible (>=80%) route
  DISABLED       — manual ban (policy, never automatic)

Transitions (spec R4 §9) are evidence-driven and conservative:
  CORE→WATCH/UNAVAILABLE on route loss; CORE→DEPRECATED on deprecation
  evidence; DEPRECATED→SUNSET on a date; UNAVAILABLE→CORE/WATCH on recovery.
  DISABLED is NEVER set automatically.

Historical records are never deleted — state changes append to ``history``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORE = "CORE"
SPECIALIST = "SPECIALIST"
WATCH = "WATCH"
FALLBACK_ONLY = "FALLBACK_ONLY"
DOMINATED = "DOMINATED"
DEPRECATED = "DEPRECATED"
SUNSET = "SUNSET"
UNAVAILABLE = "UNAVAILABLE"
DISABLED = "DISABLED"

ALL_STATUSES = (CORE, SPECIALIST, WATCH, FALLBACK_ONLY, DOMINATED,
                DEPRECATED, SUNSET, UNAVAILABLE, DISABLED)

# Statuses that exclude a canonical from normal routing.
EXCLUDED_FROM_ROUTING = frozenset({DOMINATED, DEPRECATED, SUNSET, DISABLED})
# Statuses where existing warm sessions may keep their route (spec R4 §10).
CACHE_SAFE_STATUSES = frozenset({CORE, SPECIALIST, WATCH, FALLBACK_ONLY, UNAVAILABLE})

from gateway.state_paths import state_file

LIFECYCLE_PATH = os.environ.get(
    "GW_MODEL_LIFECYCLE",
    state_file("model-lifecycle.json"),
)


@dataclass
class LifecycleRecord:
    canonical: str
    status: str = WATCH
    reason: str = ""
    since_ts: float = field(default_factory=time.time)
    manual: bool = False                 # True only for DISABLED / manual overrides
    deprecation_source: str = ""         # "provider" | "successor" | ""
    sunset_date: str = ""                # ISO date when known
    successor: str = ""                  # canonical id of a proven successor
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "canonical": self.canonical,
            "status": self.status,
            "reason": self.reason,
            "since_ts": self.since_ts,
            "manual": self.manual,
            "deprecation_source": self.deprecation_source,
            "sunset_date": self.sunset_date,
            "successor": self.successor,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LifecycleRecord":
        rec = cls(
            canonical=d.get("canonical", ""),
            status=d.get("status", WATCH),
            reason=d.get("reason", ""),
            since_ts=float(d.get("since_ts") or time.time()),
            manual=bool(d.get("manual", False)),
            deprecation_source=d.get("deprecation_source", ""),
            sunset_date=d.get("sunset_date", ""),
            successor=d.get("successor", ""),
            history=list(d.get("history") or []),
        )
        return rec


class LifecycleRegistry:
    """Thread-safe lifecycle store. Persists to state/model-lifecycle.json.
    Rebuilds are merged, never destructive: unknown canonicals get WATCH,
    known canonicals keep their status and append transitions to history."""

    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path or LIFECYCLE_PATH
        self._records: dict[str, LifecycleRecord] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data.get("models") or {}).items():
                self._records[k] = LifecycleRecord.from_dict(v)
        except FileNotFoundError:
            pass
        except Exception:
            pass  # corrupt file: start fresh, never crash routing

    def persist(self) -> None:
        with self._lock:
            payload = {
                "updated_at": time.time(),
                "models": {k: v.to_dict() for k, v in self._records.items()},
            }
        try:
            p = Path(self._path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    # ── access ───────────────────────────────────────────────────────────
    def get(self, canonical: str) -> LifecycleRecord | None:
        with self._lock:
            return self._records.get(canonical)

    def status(self, canonical: str, default: str = WATCH) -> str:
        rec = self.get(canonical)
        return rec.status if rec else default

    def all(self) -> dict[str, LifecycleRecord]:
        with self._lock:
            return dict(self._records)

    def snapshot(self) -> dict:
        with self._lock:
            return {k: v.to_dict() for k, v in self._records.items()}

    # ── mutation (evidence-driven, conservative) ─────────────────────────
    def set_status(
        self,
        canonical: str,
        status: str,
        reason: str = "",
        *,
        manual: bool = False,
        deprecation_source: str = "",
        sunset_date: str = "",
        successor: str = "",
    ) -> LifecycleRecord:
        assert status in ALL_STATUSES, status
        with self._lock:
            rec = self._records.get(canonical)
            if rec is None:
                rec = LifecycleRecord(canonical=canonical, status=status)
                self._records[canonical] = rec
            if rec.manual and not manual:
                # manual states (DISABLED) are never auto-overridden (spec R4 §9)
                return rec
            if rec.status == status and not reason:
                return rec
            if rec.status != status:
                rec.history.append({
                    "ts": time.time(),
                    "from": rec.status,
                    "to": status,
                    "reason": reason,
                })
            rec.status = status
            rec.reason = reason
            if manual:
                rec.manual = True
            if deprecation_source:
                rec.deprecation_source = deprecation_source
            if sunset_date:
                rec.sunset_date = sunset_date
            if successor:
                rec.successor = successor
            return rec

    # ── refresh-driven transitions (spec R4 §9) ──────────────────────────
    def apply_refresh(
        self,
        *,
        eligible_canonicals: set[str],
        known_canonicals: set[str],
        deprecated_map: dict[str, dict] | None = None,
    ) -> list[dict]:
        """Auto transitions after a registry/refresh cycle.

        eligible_canonicals — canonicals with >=1 route passing discount/health gates.
        known_canonicals    — canonicals present in any provider catalog.
        deprecated_map      — canonical -> {source, sunset_date, successor}.
        Returns the list of transitions performed. Never sets DISABLED.
        Manual records are left untouched.
        """
        deprecated_map = deprecated_map or {}
        transitions: list[dict] = []
        with self._lock:
            records = list(self._records.values())
        for rec in records:
            c = rec.canonical
            if rec.manual:
                continue
            dep = deprecated_map.get(c)
            if dep:
                new_status = SUNSET if dep.get("sunset_date") else DEPRECATED
                if rec.status != new_status:
                    self.set_status(
                        c, new_status,
                        reason=dep.get("reason") or "deprecation evidence",
                        deprecation_source=dep.get("source") or "provider",
                        sunset_date=dep.get("sunset_date") or "",
                        successor=dep.get("successor") or "",
                    )
                    transitions.append({"canonical": c, "from": rec.status, "to": new_status})
                continue
            if c in eligible_canonicals:
                if rec.status in (UNAVAILABLE, SUNSET, DEPRECATED, DOMINATED):
                    # recovery (spec R4 §9) — sunset/deprecated/dominated do NOT
                    # auto-return; only UNAVAILABLE auto-recovers.
                    if rec.status == UNAVAILABLE:
                        self.set_status(c, WATCH, reason="eligible route recovered; awaiting evidence")
                        transitions.append({"canonical": c, "from": UNAVAILABLE, "to": WATCH})
                continue
            # not eligible right now
            if rec.status in (CORE, SPECIALIST, WATCH, FALLBACK_ONLY):
                if c not in known_canonicals:
                    new_status = UNAVAILABLE
                    reason = "model disappeared from all provider catalogs"
                else:
                    new_status = UNAVAILABLE
                    reason = "no eligible (>=80% discount) route currently"
                if rec.status != new_status:
                    self.set_status(c, new_status, reason=reason)
                    transitions.append({"canonical": c, "from": rec.status, "to": new_status})
        self.persist()
        return transitions

    # ── dominance (spec R4 §5) ───────────────────────────────────────────
    def apply_dominance(
        self,
        dominated: str,
        by: str,
        evidence: dict,
    ) -> bool:
        """Mark ``dominated`` DOMINATED by ``by`` ONLY on full evidence:
        quality >=, context >=, reliability >=, effective cost <= (with at
        least one strict improvement). Insufficient evidence is rejected."""
        req = ("quality_gte", "context_gte", "reliability_gte", "cost_lte")
        if not all(k in evidence for k in req):
            return False
        if not (evidence["quality_gte"] and evidence["context_gte"]
                and evidence["reliability_gte"] and evidence["cost_lte"]):
            return False
        if not any(evidence.get(s) for s in ("quality_strict", "context_strict", "reliability_strict", "cost_strict")):
            return False
        rec = self.get(dominated)
        prev = rec.status if rec else WATCH
        self.set_status(
            dominated, DOMINATED,
            reason=f"dominated by {by}: {json.dumps(evidence, sort_keys=True)}",
        )
        self.persist()
        return True

"""Canonical model registry — persistent/generated, evidence-driven.

Canonical identity is provider-agnostic: routes reference canonicals, never
the reverse. Quality evidence is imported from the quality registry (PERF
artifacts) at load time; tiers are DERIVED from evidence, not hard-coded.

Tier derivation (configurable, refreshed after each benchmark run):
  T4  — quality VERIFIED and score >= 0.75
  T3  — quality >= 0.55 (VERIFIED/PROVISIONAL) or score >= 0.75 INCOMPLETE
  T2  — quality >= 0.40 with any evidence
  T1  — default simple tier
UNKNOWN (no calibrated evidence): tier = None → eligible only via explicit
policy (canary/fallback), never auto-selected for T3/T4 tasks. UNKNOWN is
NOT treated as weak (spec G2 §5/§15).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import quality as qreg
from .quality import QualityRegistry, UNKNOWN, VERIFIED, PROVISIONAL, INCOMPLETE


from gateway.state_paths import state_file

REGISTRY_PATH = os.environ.get(
    "GW_CANONICAL_REGISTRY",
    state_file("canonical-registry.json"),
)

# Families recognized as frontier-class even without benchmark evidence
# (availability evidence exists; capability demonstrated in catalog/soak).
FRONTIER_FAMILIES = ("gpt-5", "gpt-6", "grok-4", "kimi-k", "minimax-m", "qwen3", "gemini-3", "glm-5", "claude-", "deepseek-v4")


@dataclass
class CanonicalProfile:
    canonical_id: str
    display_name: str
    family: str
    generation: str
    capabilities: frozenset[str]
    quality_evidence: list[dict] = field(default_factory=list)
    quality_score: float = 0.0
    category_scores: dict[str, float] = field(default_factory=dict)
    confidence: str = UNKNOWN                 # VERIFIED | PROVISIONAL | INCOMPLETE | UNKNOWN
    tier_eligibility: str | None = None       # T1..T4 or None when UNKNOWN
    context_requirement_support: str = "standard"
    reasoning: bool = False
    coding: bool = False
    vision: bool = False
    tools: bool = False
    benchmark_version: str | None = None
    benchmark_timestamp: str | None = None
    quality_frontier: bool = False
    availability: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "display_name": self.display_name,
            "family": self.family,
            "generation": self.generation,
            "capabilities": sorted(self.capabilities),
            "quality_evidence": self.quality_evidence,
            "quality_score": self.quality_score,
            "category_scores": self.category_scores,
            "confidence": self.confidence,
            "tier_eligibility": self.tier_eligibility,
            "context_requirement_support": self.context_requirement_support,
            "reasoning": self.reasoning,
            "coding": self.coding,
            "vision": self.vision,
            "tools": self.tools,
            "benchmark_version": self.benchmark_version,
            "benchmark_timestamp": self.benchmark_timestamp,
            "quality_frontier": self.quality_frontier,
        }


def _family_of(canonical: str) -> str:
    n = canonical.lower()
    for f in ("gpt-6", "gpt-5", "grok", "kimi", "minimax", "qwen", "gemini", "glm", "claude", "deepseek"):
        if n.startswith(f):
            return f
    return n.split("-", 1)[0] if "-" in n else n


def _generation_of(canonical: str) -> str:
    import re
    m = re.search(r"(\d+(?:\.\d+)?)", canonical)
    return m.group(1) if m else "0"


def _derive_tier(confidence: str, score: float) -> str | None:
    if confidence == UNKNOWN:
        return None
    if confidence == VERIFIED:
        if score >= 0.75:
            return "T4"
        if score >= 0.55:
            return "T3"
        if score >= 0.40:
            return "T2"
        return "T1"
    if confidence == PROVISIONAL:
        if score >= 0.75:
            return "T3"
        if score >= 0.55:
            return "T3"
        if score >= 0.40:
            return "T2"
        return "T1"
    # INCOMPLETE: observed quality but coverage/transport problems
    if score >= 0.75:
        return "T3"
    if score >= 0.55:
        return "T2"
    return "T1"


class CanonicalRegistry:
    """Built from the quality evidence registry; refreshable without restart."""

    def __init__(self, quality: QualityRegistry | None = None) -> None:
        self._lock = threading.RLock()
        self._models: dict[str, CanonicalProfile] = {}
        self._quality = quality or QualityRegistry()
        self._built_at: float = 0.0

    @property
    def quality(self) -> QualityRegistry:
        return self._quality

    def rebuild(self) -> dict:
        imported = self._quality.load()
        with self._lock:
            self._models = {}
            # Recursive stripping: providers nest prefixes (e.g. cmc/moonshotai/kimi-k3,
            # qwen/qwen3.6-max-preview). Strip repeatedly until the canonical key is
            # stable so we never carry a provider-specific prefix into the registry.
            _PROVIDER_HEADS = {"cb", "cx", "cmc", "ocg", "ali", "ag", "cbcn", "moonshotai", "qwen", "zai"}
            for canonical, prof in self._quality.profiles().items():
                canonical = canonical.lower()
                while "/" in canonical and canonical.split("/", 1)[0] in _PROVIDER_HEADS:
                    canonical = canonical.split("/", 1)[1]
                if canonical in self._models:
                    continue
                cp = CanonicalProfile(
                    canonical_id=canonical,
                    display_name=canonical,
                    family=_family_of(canonical),
                    generation=_generation_of(canonical),
                    capabilities=frozenset({"text", "streaming"}),
                )
                cp.quality_score = prof.quality_score
                cp.category_scores = dict(prof.category_scores)
                cp.confidence = prof.confidence
                cp.quality_evidence = [e.to_dict() for e in prof.evidence]
                cp.availability = dict(prof.availability_evidence)
                cp.quality_frontier = prof.quality_frontier or any(
                    canonical.startswith(f) for f in FRONTIER_FAMILIES
                )
                if prof.evidence:
                    best = max(prof.evidence, key=lambda e: e.coverage)
                    cp.benchmark_version = best.benchmark_id
                    cp.benchmark_timestamp = best.timestamp
                cp.reasoning = cp.quality_frontier
                cp.coding = prof.quality_score >= 0.4 or cp.quality_frontier
                cp.tools = cp.coding
                # Capability set must include derived flags, otherwise
                # candidates_for_task rejects tool-capable canonicals on a
                # static {'text','streaming'} set (R5 cache-economics would
                # then never see the plan's real same-canonical pool).
                caps = {"text", "streaming"}
                if cp.tools:
                    caps.add("tool_call")
                cp.capabilities = frozenset(caps)
                cp.tier_eligibility = _derive_tier(prof.confidence, prof.quality_score)
                self._models[canonical] = cp
            import time as _t
            self._built_at = _t.time()
        self._persist()
        return imported

    def _persist(self) -> None:
        try:
            p = Path(REGISTRY_PATH)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.snapshot(), indent=1), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass  # persistence is best-effort; runtime registry remains authoritative

    def get(self, canonical: str) -> CanonicalProfile | None:
        with self._lock:
            return self._models.get(canonical)

    def all(self) -> dict[str, CanonicalProfile]:
        with self._lock:
            return dict(self._models)

    def candidates_for_task(
        self,
        *,
        task_class: str,
        capabilities: frozenset[str],
        quality_floor: float,
        allow_unknown: bool = False,
    ) -> tuple[list[CanonicalProfile], list[tuple[str, str]]]:
        """LEVEL A. Returns (accepted, rejected_with_reason)."""
        accepted: list[CanonicalProfile] = []
        rejected: list[tuple[str, str]] = []
        with self._lock:
            models = list(self._models.values())
        for m in models:
            if not capabilities.issubset(m.capabilities):
                rejected.append((m.canonical_id, f"capability_gap:{sorted(capabilities - m.capabilities)}"))
                continue
            if m.confidence == UNKNOWN:
                if not allow_unknown:
                    rejected.append((m.canonical_id, "quality_unknown:no_calibrated_evidence"))
                    continue
            else:
                if m.quality_score < quality_floor:
                    rejected.append((m.canonical_id, f"quality_floor:{m.quality_score:.3f}<{quality_floor:.2f}"))
                    continue
            if m.tier_eligibility is None and task_class.upper() in {"CRITICAL", "ARCHITECTURE"}:
                rejected.append((m.canonical_id, "tier_unknown:critical_task_requires_evidence"))
                continue
            accepted.append(m)
        return accepted, rejected

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "built_at": self._built_at,
                "quality_sources": self._quality.sources(),
                "models": {k: v.to_dict() for k, v in self._models.items()},
            }

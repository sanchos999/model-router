"""Quality evidence registry — imports PERF benchmark artifacts as evidence.

NOT a hard-coded ranking. Each canonical model carries evidence records;
the aggregate confidence/score is derived, never authored by hand.

Sources imported (validated methodology only):
  * fq-v2-20260910-final-c   (exec contract 9658c2be...; manifest bfce7157...)
  * frontier availability rescue soak (frontier-avail-2e.jsonl)
Excluded by policy: tainted concurrent run fq-v2-20260909-a, clean-b (benchmark
affected), uncalibrated Stage B.

Confidence levels (spec G2 §5):
  VERIFIED     — calibrated run, full/near-full coverage, provider errors ~0
  PROVISIONAL  — calibrated run with gaps or alpha-floor only
  INCOMPLETE   — scored but coverage/transport problems
  UNKNOWN      — no calibrated quality evidence (does NOT mean weak)
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERIFIED = "VERIFIED"
PROVISIONAL = "PROVISIONAL"
INCOMPLETE = "INCOMPLETE"
UNKNOWN = "UNKNOWN"

# Frontier families with availability/capability evidence but no calibrated
# quality run yet. UNKNOWN != weak (spec G2 §15).
FRONTIER_UNBENCHMARKED = frozenset({
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5", "gpt-5.4",
    "minimax-m3", "qwen3.8-max", "gemini-3.8-flash",
})

# OBE rate above this drops a model from VERIFIED to INCOMPLETE.
_OBE_RATE_MAX = 0.05
# Coverage below this drops to INCOMPLETE even with a good score.
_COVERAGE_MIN_VERIFIED = 0.99
_COVERAGE_MIN_PROVISIONAL = 0.55

_PERF_REPORT = os.environ.get(
    "GW_QUALITY_SOURCE_FQV2",
    str(Path.home() / "hermes-perf-lab" / "reports" / "fq-v2-20260910-final-c.json"),
)
_AVAIL_SOAK = os.environ.get(
    "GW_QUALITY_SOURCE_AVAIL",
    str(Path.home() / "hermes-perf-lab" / "raw" / "frontier-avail-2e.jsonl"),
)

FQV2_EXECUTION_CONTRACT = "9658c2bebea4559834a7603c4653f920d0228024eda1118e70c55d20978b1b59"
FQV2_MANIFEST = "bfce7157cd8252eb27145b96a9f1b69b33ed1fbeacbc5cb7e5ae97bdc2f8295c"


@dataclass
class EvidenceRecord:
    benchmark_id: str
    canonical: str
    quality_score: float
    coverage: float
    status: str                    # VERIFIED | PROVISIONAL | INCOMPLETE | TRANSPORT_UNAVAILABLE
    categories: dict[str, float] = field(default_factory=dict)
    execution_contract: str | None = None
    timestamp: str | None = None
    obe_rate: float = 0.0
    provider_errors: int = 0
    planned: int = 0
    valid: int = 0

    def to_dict(self) -> dict:
        return {
            "benchmark_id": self.benchmark_id,
            "canonical": self.canonical,
            "quality_score": self.quality_score,
            "coverage": self.coverage,
            "status": self.status,
            "categories": self.categories,
            "execution_contract": self.execution_contract,
            "timestamp": self.timestamp,
            "obe_rate": self.obe_rate,
            "provider_errors": self.provider_errors,
            "planned": self.planned,
            "valid": self.valid,
        }


@dataclass
class QualityProfile:
    canonical: str
    confidence: str                # VERIFIED | PROVISIONAL | INCOMPLETE | UNKNOWN
    quality_score: float
    category_scores: dict[str, float] = field(default_factory=dict)
    availability_evidence: dict[str, Any] = field(default_factory=dict)
    quality_frontier: bool = False  # strong family, quality unbenchmarked yet
    evidence: list[EvidenceRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "canonical": self.canonical,
            "confidence": self.confidence,
            "quality_score": self.quality_score,
            "category_scores": self.category_scores,
            "availability_evidence": self.availability_evidence,
            "quality_frontier": self.quality_frontier,
            "evidence": [e.to_dict() for e in self.evidence],
        }


def _verdict_to_confidence(verdict: str, coverage: float, obe_rate: float) -> str:
    if verdict == "QUALITY_VERIFIED" and coverage >= _COVERAGE_MIN_VERIFIED and obe_rate <= _OBE_RATE_MAX:
        return VERIFIED
    if verdict == "TRANSPORT_UNAVAILABLE":
        return UNKNOWN
    if verdict == "QUALITY_INCOMPLETE":
        if coverage >= _COVERAGE_MIN_PROVISIONAL and obe_rate <= _OBE_RATE_MAX:
            return PROVISIONAL
        return INCOMPLETE
    return INCOMPLETE


class QualityRegistry:
    """Thread-safe in-memory quality evidence store. Reloadable."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[str, QualityProfile] = {}
        self._loaded_at: float = 0.0
        self._sources: list[str] = []

    def load(self, fqv2_path: str | None = None, avail_path: str | None = None) -> dict:
        with self._lock:
            self._profiles = {}
            self._sources = []
            imported = {"fq_v2": 0, "avail_soak": 0, "skipped": []}
            p = fqv2_path or _PERF_REPORT
            try:
                imported["fq_v2"] = self._import_fqv2(p)
                self._sources.append(Path(p).name)
            except FileNotFoundError:
                imported["skipped"].append(f"fqv2 missing: {p}")
            a = avail_path or _AVAIL_SOAK
            try:
                imported["avail_soak"] = self._import_avail(a)
                self._sources.append(Path(a).name)
            except FileNotFoundError:
                imported["skipped"].append(f"avail soak missing: {a}")
            import time as _t
            self._loaded_at = _t.time()
            imported["profiles"] = len(self._profiles)
            return imported

    # ── importers ──────────────────────────────────────────────────────
    def _import_fqv2(self, path: str) -> int:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("integrity", {}).get("execution_contract_sha256") != FQV2_EXECUTION_CONTRACT:
            raise ValueError("fq-v2 execution contract mismatch — refusing import")
        ts = data.get("generated")
        n = 0
        for route_id, stats in (data.get("route_stats") or {}).items():
            canonical = route_id.split(":", 1)[1]
            for prefix in ("moonshotai/", "Qwen/", "zai/"):
                if canonical.startswith(prefix):
                    canonical = canonical.split("/", 1)[1]
            canonical = canonical.lower()
            score = float(stats.get("quality_score") or 0.0)
            coverage = float(stats.get("coverage") or 0.0)
            verdict = stats.get("verdict", "")
            obe = float(stats.get("obe_rate") or 0.0)
            conf = _verdict_to_confidence(verdict, coverage, obe)
            cats = {}
            for cat, ranking in (data.get("category_rankings") or {}).items():
                entry = ranking.get(route_id)
                if isinstance(entry, dict) and "score" in entry:
                    cats[cat] = float(entry["score"])
            rec = EvidenceRecord(
                benchmark_id="fq-v2-20260910-final-c",
                canonical=canonical,
                quality_score=score,
                coverage=coverage,
                status=verdict or "UNKNOWN",
                categories=cats,
                execution_contract=FQV2_EXECUTION_CONTRACT[:16],
                timestamp=ts,
                obe_rate=obe,
                provider_errors=int(stats.get("provider_error") or 0),
                planned=int(stats.get("planned") or 0),
                valid=int(stats.get("valid") or 0),
            )
            self._merge_profile(canonical, conf, score, cats, rec)
            n += 1
        return n

    def _import_avail(self, path: str) -> int:
        from collections import defaultdict
        agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"ok": 0, "tot": 0, "ttft": [], "errors": defaultdict(int)})
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                a = agg[r["route_id"]]
                a["tot"] += 1
                if r.get("semantic_success"):
                    a["ok"] += 1
                if r.get("ttft_ms"):
                    a["ttft"].append(float(r["ttft_ms"]))
                if not r.get("semantic_success") and r.get("error_category"):
                    a["errors"][r["error_category"]] += 1
        n = 0
        for route_id, a in agg.items():
            canonical = route_id.split(":", 1)[1]
            if "/" in canonical and canonical.split("/")[0] in {"cb", "cx", "cmc", "ocg", "ali", "ag", "cbcn"}:
                canonical = canonical.split("/", 1)[1]
            canonical = canonical.lower()
            tt = sorted(a["ttft"])
            ev = {
                "route_id": route_id,
                "semantic_success": a["ok"],
                "probes": a["tot"],
                "rate": round(a["ok"] / a["tot"], 3) if a["tot"] else 0.0,
                "ttft_p50_ms": tt[len(tt) // 2] if tt else None,
                "errors": dict(a["errors"]),
            }
            self._merge_profile(canonical, None, None, None, None, availability=ev)
            n += 1
        return n

    # ── merge / read ───────────────────────────────────────────────────
    def _merge_profile(
        self,
        canonical: str,
        conf: str | None,
        score: float | None,
        cats: dict | None,
        rec: EvidenceRecord | None,
        availability: dict | None = None,
    ) -> None:
        prof = self._profiles.get(canonical)
        if prof is None:
            prof = QualityProfile(canonical=canonical, confidence=UNKNOWN, quality_score=0.0)
            prof.quality_frontier = canonical in FRONTIER_UNBENCHMARKED
            self._profiles[canonical] = prof
        if rec is not None:
            prof.evidence.append(rec)
            # best-evidence wins for score; never regress confidence upward
            # beyond what evidence supports, but allow improvement.
            if conf is not None:
                if prof.confidence == UNKNOWN or _conf_rank(conf) > _conf_rank(prof.confidence):
                    prof.confidence = conf
                elif _conf_rank(conf) == _conf_rank(prof.confidence) and score is not None and score > prof.quality_score:
                    pass  # keep first score at same confidence; deterministic
            if score is not None and (prof.quality_score == 0.0 or conf == VERIFIED):
                prof.quality_score = score
            if cats:
                prof.category_scores.update(cats)
        if availability is not None:
            prof.availability_evidence[availability["route_id"]] = {
                k: v for k, v in availability.items() if k != "route_id"
            }

    def profile(self, canonical: str) -> QualityProfile | None:
        with self._lock:
            return self._profiles.get(canonical)

    def profiles(self) -> dict[str, QualityProfile]:
        with self._lock:
            return dict(self._profiles)

    def confidence(self, canonical: str) -> str:
        p = self.profile(canonical)
        return p.confidence if p else UNKNOWN

    def score(self, canonical: str) -> float | None:
        p = self.profile(canonical)
        return p.quality_score if p else None

    def sources(self) -> list[str]:
        with self._lock:
            return list(self._sources)

    def loaded_at(self) -> float:
        with self._lock:
            return self._loaded_at

    def snapshot(self) -> dict:
        with self._lock:
            return {k: v.to_dict() for k, v in self._profiles.items()}


_CONF_ORDER = {UNKNOWN: 0, INCOMPLETE: 1, PROVISIONAL: 2, VERIFIED: 3}


def _conf_rank(c: str) -> int:
    return _CONF_ORDER.get(c, 0)

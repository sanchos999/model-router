"""Canonical model mapping and tier registry.

A canonical model (e.g., ``gpt-5.6-luna``) is a logical name shared across
providers. The same canonical can have one or more physical slugs across
Provider A and Provider B; the mapping here is the single source of truth.

Quality is canonical-level; transport metrics are route-level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CanonicalModel:
    """Logical model — marketplace-agnostic."""
    canonical: str                         # "gpt-5.6-luna"
    official_input_per_mtok: float
    official_output_per_mtok: float
    quality: float                         # canonical-level; default 0.5 if unknown
    tier: str                              # "T1" | "T2" | "T3" | "T4"
    metadata: dict[str, Any] = field(default_factory=dict)
    task_classes: frozenset[str] = field(default_factory=frozenset)  # which task classes this canonical is eligible for


@dataclass(frozen=True)
class CanonicalMapping:
    """Maps a (provider, provider_model_id) pair to a canonical name."""
    provider: str
    provider_model_id: str
    canonical: str
    context_length: int


CANONICAL_MODELS: dict[str, CanonicalModel] = {
    "gpt-5.6-luna": CanonicalModel(
        canonical="gpt-5.6-luna",
        official_input_per_mtok=0.2,
        official_output_per_mtok=1.2,
        quality=0.8095,
        tier="T4",
        task_classes=frozenset({"NORMAL_CODING", "DEBUG", "REFACTOR", "ARCHITECTURE", "REVIEW", "RESEARCH", "CRITICAL", "COMPRESSION", "SEARCH", "SUMMARIZE", "SIMPLE_EDIT"}),
    ),
    "grok-4.3": CanonicalModel(
        canonical="grok-4.3",
        official_input_per_mtok=1.5,
        official_output_per_mtok=6.0,
        quality=0.7619,
        tier="T4",
        task_classes=frozenset({"NORMAL_CODING", "DEBUG", "REFACTOR", "REVIEW", "RESEARCH", "CRITICAL"}),
    ),
    "glm-5.3": CanonicalModel(
        canonical="glm-5.3",
        official_input_per_mtok=1.4,
        official_output_per_mtok=4.4,
        quality=0.8478,
        tier="T3",
        task_classes=frozenset({"NORMAL_CODING", "DEBUG", "REFACTOR", "REVIEW", "RESEARCH"}),
    ),
    "gemini-3.7-flash": CanonicalModel(
        canonical="gemini-3.7-flash",
        official_input_per_mtok=0.5,
        official_output_per_mtok=2.4,
        quality=0.8448,
        tier="T3",
        task_classes=frozenset({"NORMAL_CODING", "DEBUG", "REVIEW", "RESEARCH"}),
    ),
    "deepseek-v4-pro": CanonicalModel(
        canonical="deepseek-v4-pro",
        official_input_per_mtok=0.66,
        official_output_per_mtok=1.98,
        quality=0.7568,
        tier="T3",
        task_classes=frozenset({"NORMAL_CODING", "DEBUG", "REFACTOR", "REVIEW", "RESEARCH"}),
    ),
    "minimax-m3": CanonicalModel(
        canonical="minimax-m3",
        official_input_per_mtok=0.3,
        official_output_per_mtok=1.2,
        quality=0.50,
        tier="T2",
        task_classes=frozenset({"NORMAL_CODING", "DEBUG", "RESEARCH"}),
    ),
    "kimi-k3": CanonicalModel(
        canonical="kimi-k3",
        official_input_per_mtok=3.0,
        official_output_per_mtok=15.0,
        quality=0.70,
        tier="T3",
        task_classes=frozenset({"NORMAL_CODING", "DEBUG", "REFACTOR", "ARCHITECTURE", "REVIEW", "RESEARCH"}),
    ),
}


# Mapping entries — only those proved live in GATEWAY-0/PERF-1 / Phase 3B audits.
# Adding a new mapping is just a new entry; selector picks on health, cost, reliability.
CANONICAL_MAPPING: tuple[CanonicalMapping, ...] = (
    CanonicalMapping("provider_a", "cx/gpt-5.6-luna",    "gpt-5.6-luna",   272000),
    CanonicalMapping("provider_a", "cb/gpt-5.6-luna",    "gpt-5.6-luna",  1050000),
    CanonicalMapping("provider_a", "ocg/gpt-5.6-luna",   "gpt-5.6-luna",   272000),
    CanonicalMapping("provider_b",  "gpt-5.6-luna",        "gpt-5.6-luna",  1050000),
    CanonicalMapping("provider_b",  "grok-4.3",            "grok-4.3",       132000),
    CanonicalMapping("provider_a", "cb/glm-5.3",          "glm-5.3",       1000000),
    CanonicalMapping("provider_a", "cbcn/glm-5.3",        "glm-5.3",       1000000),
    CanonicalMapping("provider_a", "ocg/glm-5.3",         "glm-5.3",       1000000),
    CanonicalMapping("provider_a", "zai/glm-5.3",         "glm-5.3",       1000000),
    CanonicalMapping("provider_a", "cb/glm-5.3-flash",    "glm-5.3-flash", 1000000),
    CanonicalMapping("provider_a", "cbcn/glm-5.3-flash",  "glm-5.3-flash", 1000000),
    CanonicalMapping("provider_a", "ocg/glm-5.3-flash",   "glm-5.3-flash", 1000000),
    CanonicalMapping("provider_a", "cb/glm-5.2",          "glm-5.2",       1000000),
    CanonicalMapping("provider_a", "ali/glm-5.2",         "glm-5.2",       1000000),
    CanonicalMapping("provider_a", "cb/gemini-3.7-flash", "gemini-3.7-flash", 1000000),
    CanonicalMapping("provider_a", "cb/deepseek-v4-pro",  "deepseek-v4-pro", 1000000),
    CanonicalMapping("provider_a", "cbcn/deepseek-v4-pro","deepseek-v4-pro", 1000000),
    CanonicalMapping("provider_a", "cb/deepseek-v4.1-flash","deepseek-v4.1-flash", 1000000),
    CanonicalMapping("provider_a", "cb/minimax-m3",       "minimax-m3",     1000000),
    CanonicalMapping("provider_a", "cbcn/minimax-m3",     "minimax-m3",     1000000),
    CanonicalMapping("provider_a", "ocg/minimax-m3",      "minimax-m3",     1000000),
    CanonicalMapping("provider_a", "cb/kimi-k3",          "kimi-k3",        1000000),
    CanonicalMapping("provider_a", "ali/kimi-k3",         "kimi-k3",        1000000),
    CanonicalMapping("provider_a", "cbcn/kimi-k3",        "kimi-k3",        1000000),
    CanonicalMapping("provider_a", "ocg/kimi-k3",         "kimi-k3",        1000000),
    CanonicalMapping("provider_a", "cmc/moonshotai/Kimi-K3","kimi-k3",      1000000),
    CanonicalMapping("provider_b",  "kimi-k3",             "kimi-k3",        1000000),
    # R4: live-proven frontier routes (PERF-1B.2E soak 6/6 unless noted)
    CanonicalMapping("provider_a", "cb/gpt-5.6-sol",      "gpt-5.6-sol",    272000),
    CanonicalMapping("provider_a", "cx/gpt-5.6-sol",      "gpt-5.6-sol",    272000),  # 5/6
    CanonicalMapping("provider_a", "cb/gpt-5.6-terra",    "gpt-5.6-terra",  272000),
    CanonicalMapping("provider_a", "cx/gpt-5.6-terra",    "gpt-5.6-terra",  272000),  # 4/6
    CanonicalMapping("provider_a", "cb/gpt-6-astra",      "gpt-6-astra",    272000),
    CanonicalMapping("provider_a", "cx/gpt-6-astra",      "gpt-6-astra",    272000),  # 5/6 flaky timeout
    CanonicalMapping("provider_a", "cb/gpt-5.5",          "gpt-5.5",        272000),
    CanonicalMapping("provider_a", "cx/gpt-5.5",          "gpt-5.5",        272000),  # 5/6
    CanonicalMapping("provider_a", "cb/gpt-5.4",          "gpt-5.4",        272000),
    CanonicalMapping("provider_a", "cx/gpt-5.4-mini",     "gpt-5.4-mini",   272000),
    CanonicalMapping("provider_a", "ag/gemini-3.8-flash-high","gemini-3.8-flash-high", 1000000),
    CanonicalMapping("provider_a", "ali/qwen3.8-max",     "qwen3.8-max",    1000000),
    CanonicalMapping("provider_a", "ocg/qwen3.8-max",     "qwen3.8-max",    1000000),
    CanonicalMapping("provider_a", "cmc/Qwen/Qwen3.6-Max-Preview","qwen3.6-max-preview", 1000000),
    CanonicalMapping("provider_a", "ocg/grok-4.5",        "grok-4.5",       500000),  # 0/6 in 2E, recheck on refresh
)


def lookup_canonical(provider: str, provider_model_id: str) -> str | None:
    for m in CANONICAL_MAPPING:
        if m.provider == provider and m.provider_model_id == provider_model_id:
            return m.canonical
    return None


# Logical alias -> canonical (G3.1 contract: compression-auto is a FIXED
# gpt-5.6-luna compressor; Hermes auxiliary.compression sends this model name
# to :4101). Same semantics as production shadow_router CHOOSE ALIASES.
MODEL_ALIASES: dict[str, str] = {
    "compression-auto": "gpt-5.6-luna",
}


def mapping_for_canonical(canonical: str) -> list[CanonicalMapping]:
    return [m for m in CANONICAL_MAPPING if m.canonical == canonical]


def get_canonical(canonical: str) -> CanonicalModel | None:
    return CANONICAL_MODELS.get(canonical)

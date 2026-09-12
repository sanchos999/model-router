"""R4 canonical normalization — data-driven, no fuzzy merging.

Rules (spec R4 §2/§3):
  1. Provider/vendor prefixes are NEVER part of the canonical id:
     Provider A routes carry vendor/cluster heads (``cb/``, ``cx/``, ``cmc/``,
     ``ocg/``, ``ali/``, ``ag/``, ``cbcn/``, ``z/``...) plus optional nested
     vendor namespaces (``cmc/moonshotai/Kimi-K3``). All are stripped
     recursively, case-insensitively.
  2. Case is normalized to lowercase.
  3. ONLY exact (case/prefix-normalized) matches merge. There is NO fuzzy
     normalization: ``gpt-5.4`` and ``gpt-5.4-mini`` never merge (distinct
     variants), ``glm-5.3`` and ``glm-5.3-flash`` never merge, ``gemini-3.8``
     and ``gemini-3.8-flash`` never merge.
  4. Known cross-provider alias exceptions live in ALIAS_TABLE (data-driven;
     each entry maps a provider-local id to a canonical id). Everything not
     covered by the table follows rules 1-3.

Provider parity (spec R4 §9): the normalize function is provider-agnostic —
``normalize("provider_a", "cb/GPT-5.6-Luna") ==
normalize("provider_b", "gpt-5.6-luna") == "gpt-5.6-luna"``.
"""
from __future__ import annotations

import re

# Provider A route/vendor heads (lowercase). Recursively stripped from the head
# of a slug until the remainder no longer starts with a known head.
PROVIDER_HEADS = frozenset({
    "cb", "cx", "cc", "cmc", "ocg", "ali", "ag", "cbcn", "cp", "zai",
})

# Nested vendor namespaces inside a slug (e.g. cmc/moonshotai/Kimi-K3,
# cmc/zai-org/GLM-5.2, cmc/Qwen/Qwen3.6-Max-Preview).
VENDOR_HEADS = frozenset({
    "moonshotai", "zai-org", "z-ai", "qwen", "deepseek", "minimaxai",
    "xai", "meta", "nvidia", "mistralai",
})

# Data-driven alias exceptions: (provider | "*") -> {provider_local_id_lower: canonical}.
# ONLY for ids where mechanical stripping produces a DIFFERENT model than the
# marketplace's own naming. Every entry needs a real observation behind it.
ALIAS_TABLE: dict[str, dict[str, str]] = {
    "*": {
        # Provider B user-facing aliases for [OI]-renamed frontier models.
        # ("the model 5" etc. are Anthropic names masked upstream; they map to
        # their real claude ids where the marketplace itself uses the id.)
        "claude-opus-5": "claude-opus-5",
        "claude-sonnet-5": "claude-sonnet-5",
    },
    "provider_a": {},
    "provider_b": {
        # Provider B spells Qwen 3.8 Max with dashes; Provider A as qwen3.8-max.
        "qwen-3-8-max": "qwen3.8-max",
        # Provider B Qwen 3.7 spellings.
        "qwen-3-7-max": "qwen3.7-max",
        "qwen-3-7-plus": "qwen3.7-plus",
    },
}

_DATE_TAIL = re.compile(r"-\d{4}$")  # e.g. deepseek-v4-pro-0813 — NOT stripped (real variant)


def _strip_heads(slug: str) -> str:
    """Recursively strip provider/vendor heads from 'a/b/c' slugs."""
    parts = slug.split("/")
    # walk from the left while heads are present; keep at least the last part
    i = 0
    while i < len(parts) - 1 and (parts[i].lower() in PROVIDER_HEADS or parts[i].lower() in VENDOR_HEADS):
        i += 1
    return "/".join(parts[i:])


def normalize(provider: str, provider_model_id: str) -> str:
    """Canonical id for a provider-local model id. Deterministic, no fuzz."""
    if not provider_model_id:
        return ""
    slug = str(provider_model_id).strip()
    # Drop ':web' / ':free'-style suffixes only when the base id also exists
    # as its own entry — otherwise keep (they are genuinely different SKUs).
    # Conservative: we do NOT strip suffixes mechanically (R4 §2: no dangerous
    # merges). ':web' variants are search-augmented SKUs, distinct models.
    slug = _strip_heads(slug)
    canonical = slug.lower()
    # Alias exceptions (exact match only, after mechanical normalization).
    table = ALIAS_TABLE.get(provider) or {}
    star = ALIAS_TABLE.get("*") or {}
    return table.get(canonical) or star.get(canonical) or canonical


def canonical_display_name(canonical: str) -> str:
    """Human display name derived mechanically from the canonical id."""
    family_map = {
        "gpt": "GPT", "glm": "GLM", "kimi": "Kimi", "minimax": "MiniMax",
        "qwen": "Qwen", "grok": "Grok", "gemini": "Gemini",
        "deepseek": "DeepSeek", "claude": "Claude",
    }
    parts = canonical.split("-")
    if not parts:
        return canonical
    head = family_map.get(parts[0], parts[0].upper() if len(parts[0]) <= 3 else parts[0].capitalize())
    return " ".join([head] + parts[1:])


def family_of(canonical: str) -> str:
    n = canonical.lower()
    for f in ("gpt-6", "gpt-5", "gpt-4", "grok", "kimi", "minimax", "qwen",
              "gemini", "glm", "claude", "deepseek"):
        if n.startswith(f):
            return f.split("-")[0]
    return n.split("-", 1)[0] if "-" in n else n


def generation_of(canonical: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)", canonical)
    return m.group(1) if m else "0"


def variant_of(canonical: str) -> str:
    """Variant suffix (mini/nano/flash/pro/preview/thinking/...) or ''. """
    n = canonical.lower()
    known = ("mini", "nano", "flash", "flash-high", "pro", "preview",
             "thinking", "high", "turbo", "lite", "fast", "code", "highspeed")
    tail = "-".join(n.split("-")[1:])
    for k in sorted(known, key=len, reverse=True):
        if tail == k or tail.endswith("-" + k) or n.endswith(k):
            # ensure it is really a suffix variant, not the base id
            if tail == k:
                return k
            if tail.endswith("-" + k):
                return k
            if n.endswith("-" + k) and "-" in n:
                return k
    return ""

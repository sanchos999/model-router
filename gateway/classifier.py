"""Gateway V2 task classifier (T0..T4 + class labels).

Standalone — no dependency on hermes-agent source. Semantics mirror the
existing deterministic classifier family (task_classifier.py / task_tracker.py):
ephemeral features only, class label propagated, raw text never persisted.

Tier mapping (spec G2 §6):
  T0 deterministic   — arithmetic / explicit structured lookup
  T1 simple/search/summarize/compression
  T2 coding/debug/multifile
  T3 hard debug/refactor/research/review/architecture
  T4 critical architecture/security/hard reasoning
"""
from __future__ import annotations

import re


CLASSES = (
    "SIMPLE", "SEARCH", "SUMMARIZE", "COMPRESSION", "SIMPLE_EDIT",
    "REPOSITORY_INSPECTION", "NORMAL_CODING", "DEBUG", "REFACTOR",
    "ARCHITECTURE", "REVIEW", "RESEARCH", "CRITICAL",
)

_TIER_OF = {
    "SIMPLE": "T1", "SEARCH": "T1", "SUMMARIZE": "T1",
    "COMPRESSION": "T1", "SIMPLE_EDIT": "T1", "REPOSITORY_INSPECTION": "T1",
    "NORMAL_CODING": "T2", "DEBUG": "T2", "REFACTOR": "T2",
    "ARCHITECTURE": "T3", "REVIEW": "T3", "RESEARCH": "T3",
    "CRITICAL": "T4",
}


def _words(text: str) -> set:
    return set(re.findall(r"[\wа-яё]+", str(text or "").lower()))


def classify_tier(text: str) -> str:
    return tier_for_class(classify(text))


def classify(text: str) -> str:
    """Deterministic class from raw task text (features discarded)."""
    t = str(text or "").lower()
    words = _words(t)
    has_digits = bool(re.search(r"\d+", t))
    has_math = has_digits and any(w in words for w in {
        "сколько", "умножить", "умнож", "плюс", "минус", "разделить",
        "вычисли", "произведение", "сумма", "посчитать", "рассчитай"})
    has_critical = any(w in words for w in {
        "критичн", "critical", "security", "безопасн", "vulnerab", "уязвим",
        "архитектурн", "architecture", "hard reasoning", "миграция базы", "migrate db"})
    has_hard = any(w in words for w in {
        "исследуй", "изуч", "сравни", "research", "проанализируй",
        "рефактор", "refactor", "ревью", "review", "обзор"})
    has_coding = any(w in words for w in {
        "исправь", "реализуй", "добавь", "измен", "напиши", "создай",
        "почини", "задеплой", "fix", "implement", "debug", "отлад"})
    has_search = any(w in words for w in {
        "найди", "найти", "документац", "документация", "википед",
        "официальн", "поиск", "search", "lookup", "справк", "назначение"})
    has_compress = any(w in words for w in {"сожми", "сжать", "compress", "сократи", "summarize", "саммари", "перескажи"})
    if has_math:
        return "SIMPLE"
    if has_critical:
        return "CRITICAL"
    if has_coding and not has_hard:
        return "NORMAL_CODING"
    if "рефактор" in words or "refactor" in words:
        return "REFACTOR"
    if "отлад" in t or "debug" in words or "почини баг" in t:
        return "DEBUG"
    if has_hard:
        return "RESEARCH"
    if has_compress:
        return "SUMMARIZE"
    if has_search:
        return "SEARCH"
    return "SIMPLE"


def tier_for_class(task_class: str, config=None) -> str:
    """Tier for a task class. R14 §17: an active config revision may override
    the mapping (task_classes) or disable a class (enabled=false → the class
    is treated as SIMPLE, lowest tier — never a hard request failure)."""
    c = (task_class or "").upper()
    overrides = None
    if config is not None:
        try:
            overrides = getattr(config, "task_classes", None) or {}
        except Exception:
            overrides = None
    if overrides:
        tc = overrides.get(c)
        if isinstance(tc, dict):
            if tc.get("enabled") is False:
                return _TIER_OF.get("SIMPLE")
            t = tc.get("tier")
            if t in _TIER_OF.values():
                return t
    return _TIER_OF.get(c, "T2")


def capabilities_for_class(task_class: str) -> frozenset:
    """Minimum capability set a canonical must advertise for this class."""
    c = (task_class or "").upper()
    if c in {"NORMAL_CODING", "DEBUG", "REFACTOR"}:
        return frozenset({"text", "streaming", "tool_call"})
    return frozenset({"text", "streaming"})

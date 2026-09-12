"""Adapter plugin registry (spec R6 §F).

adapter_type -> adapter factory. Future Provider N is wired by registering a
factory here (or using the generic OpenAI-compatible adapter), never by
changing the routing core. Control plane reads provider rows from control.db
and instantiates adapters through this registry.
"""
from __future__ import annotations

from ..providers.provider_a import ProviderAAdapter
from ..providers.provider_b import ProviderBAdapter
from ..providers.openai_compat import OpenAICompatibleAdapter

ADAPTER_TYPES = {
    "provider_a": ProviderAAdapter,
    "provider_b": ProviderBAdapter,
    # generic [OI]-compatible adapter — usable by any custom provider whose
    # API speaks chat/completions + models.
    "openai-compatible": OpenAICompatibleAdapter,
}


def build_adapter(name: str, adapter_type: str, base_url: str | None,
                  secret_ref: str | None, settings: dict | None = None):
    """Instantiate an adapter from a control-plane provider row.

    settings may carry: model_map {slug: canonical}, defaults {slug: {...}}.
    Returns None for unknown adapter types (logged upstream).
    """
    cls = ADAPTER_TYPES.get(adapter_type)
    if cls is None:
        return None
    s = settings or {}
    if adapter_type == "openai-compatible":
        return OpenAICompatibleAdapter(
            name=name,
            base_url=base_url or s.get("base_url") or "",
            secret_ref=secret_ref,
            model_map=s.get("model_map") or {},
            defaults=s.get("defaults") or {},
        )
    return cls()
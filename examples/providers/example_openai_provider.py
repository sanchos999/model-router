"""Example provider plugin — minimal, fake endpoint/config only (R8 §6).

This demonstrates adding "Provider N" WITHOUT touching the selector,
economics, context manager or model lifecycle core. The adapter implements
the ProviderAdapter contract (gateway/providers/base.py):

    discover()          -> catalog models           (= discover_models)
    price()             -> pricing refresh          (= get_pricing)
    request()           -> non-streaming completion (= chat_completion)
    stream()            -> streaming completion     (= stream_chat_completion)
    usage()             -> transport metrics        (= extract_billing source)
    health()            -> probe                    (= health_check)
    normalize_error()   -> ProviderError mapping

It is a thin wrapper over the generic OpenAI-compatible adapter shipped in
gateway/providers/openai_compat.py (resolved dynamically so this example
stays decoupled from its exact class name). Everything below uses a FAKE
base_url and a FAKE key env name — nothing here can talk to a real provider.
"""
from __future__ import annotations

import os

import gateway.providers.openai_compat as _compat

GenericCompatAdapter = next(
    cls for name, cls in vars(_compat).items()
    if isinstance(cls, type) and name.endswith("CompatibleAdapter")
)

# Fake, non-routable example values.
EXAMPLE_BASE_URL = "https://provider-n.example/v1"
EXAMPLE_SECRET_REF = "PROVIDER_N_API_KEY"

# Fake catalog mapping: {provider slug: canonical model}.
# In a real deployment this comes from the control plane provider record.
EXAMPLE_MODEL_MAP = {
    "provider-n/standard": "example-standard",
    "provider-n/fast": "example-fast",
}


def build_example_provider() -> GenericCompatAdapter:
    """Construct the adapter from env/config only.

    Returns an adapter that will fail health checks until real values are
    supplied via private config — by design.
    """
    return GenericCompatAdapter(
        name="provider-n",
        base_url=os.environ.get("PROVIDER_N_BASE_URL", EXAMPLE_BASE_URL),
        secret_ref=EXAMPLE_SECRET_REF,          # env NAME, never the key
        model_map=dict(EXAMPLE_MODEL_MAP),
        defaults={
            "*": {
                "context_length": 128000,
                "input_price": 0.0,             # UNKNOWN real pricing -> free
                "output_price": 0.0,
                "discount": None,               # uncertified
                "price_state": "UNKNOWN",
                "certification_status": "UNVERIFIED",
            },
        },
    )


if __name__ == "__main__":
    # Smoke: construct only; no network calls.
    adapter = build_example_provider()
    print(f"provider plugin ready: name={adapter.name} "
          f"base_url={adapter.base_url} models={sorted(EXAMPLE_MODEL_MAP)}")

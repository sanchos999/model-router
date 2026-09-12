# Providers

Model Router talks to model marketplaces through adapters implementing the
ProviderAdapter protocol (gateway/providers/base.py):

    discover()        catalog models + canonical mapping
    price()           pricing refresh (price evidence, discount)
    capabilities()    capability advertisement (text/streaming/json/tool_call)
    request()         non-streaming chat completion
    stream()          streaming chat completion (SSE chunks)
    health()          live probe
    usage()           transport metrics (billing extraction source)
    normalize_error() normalized ProviderError mapping

Shipped adapters:

- gateway/providers/provider_a.py — Provider A (provider-a in examples)
- gateway/providers/provider_b.py — Provider B (provider-b in examples); the
  discount floor is encoded in the base_url /minN/ path segment
- gateway/providers/openai_compat.py — generic [OI]-compatible adapter
  used for custom provider plugins

## Public/private boundary (this repository)

Adapters contain GENERIC protocol logic and public API semantics only:
public API endpoints, env-driven base URLs, no keys, no account IDs, no
negotiated pricing. Commercially sensitive data (real keys, account
details, negotiated discounts, billing history, raw catalog dumps) lives
in the private EnvironmentFile / control DB on the deployment host and is
never committed.

Discount policy is expressed generically: a certified discount floor
(min_discount, default 0.80), price evidence with EXACT /
ESTIMATED_UPPER_BOUND / UNKNOWN states, and cost-per-success economics.
The repository does not contain our actual marketplace arrangements.

## Adding a provider

See docs/provider-plugin.md. Provider N is added via an adapter + a
control-plane provider record (name, adapter_type, base_url, secret_ref —
an env var NAME, never the key) without touching selector, economics,
context manager or lifecycle code.

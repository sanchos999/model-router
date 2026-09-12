# Provider plugin contract

Implement the ProviderAdapter protocol (gateway/providers/base.py):

```python
class ProviderAdapter(Protocol):
    name: str
    async def discover(self) -> list[ProviderModel]: ...
    async def price(self, model) -> ProviderModel: ...
    def capabilities(self, model) -> frozenset[str]: ...
    async def request(self, model, req: UpstreamRequest) -> tuple[int, dict, ProviderError | None]: ...
    def stream(self, model, req) -> AsyncIterator[UpstreamChunk]: ...
    async def health(self, model) -> dict: ...
    def record_success(self, model, latency_ms) -> None: ...
    def record_failure(self, model, error) -> None: ...
    def usage(self, model) -> dict: ...
    def normalize_error(self, status, message) -> ProviderError: ...
```

Conceptual mapping to the spec's naming:

- discover_models() == discover()
- get_pricing()     == price()
- chat_completion() == request()
- stream_chat_completion() == stream()
- extract_billing() == usage() (+ transport-level cost feedback hooks)
- health_check()    == health()
- normalize_error() == normalize_error()

## Requirements

- No routing-core changes: adding Provider N must not modify selector,
  economics, context manager or lifecycle code.
- Secrets by reference: the adapter reads keys from an env var NAME
  (secret_ref); the value lives only in the private EnvironmentFile.
- Normalized errors: map HTTP statuses to the stable codes
  (timeout / rate_limit / no_healthy_sellers / payment_required / server /
  context_too_large / protocol / unknown) with a retries_safe hint.

## Minimal example

examples/providers/example_openai_provider.py builds a plugin over the
generic [OI]-compatible adapter with a FAKE endpoint and FAKE key env
name. Wiring in production is via the control plane:

    POST /admin/providers {"name": "provider-n",
                           "adapter_type": "openai_compat",
                           "base_url": "https://provider-n.example/v1",
                           "secret_ref": "PROVIDER_N_API_KEY",
                           "model_map": {...}}

Registry refresh (POST /registry/refresh or the periodic task) then
discovers its models; the selector ranks them like any other route.

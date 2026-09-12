# Hermes as a Model Router client (optional example)

Model Router is standalone: it does not import or require Hermes. Hermes is
just one [OI]-compatible client among many.

In `~/.hermes/config.yaml` (or the relevant profile), add the router as an
additive provider:

```yaml
providers:
  router:
    base_url: http://127.0.0.1:4100/v1
    key_env: ROUTER_API_KEY        # env var NAME; value lives in private .env
    api: chat
    prompt_cache_key: true

models:
  main-auto: ...
```

Set the env var in the Hermes private env file (never in the repo):

```
ROUTER_API_KEY=<inference token, if auth.mode=bearer>
```

Then run Hermes with the routed models, e.g. `custom:router:main-auto`.

The router's `/v1` surface is a standard [OI]-compatible API; any other
agent framework that accepts a custom base_url works the same way.

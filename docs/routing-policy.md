# Routing policy

## Selection pipeline

1. Auth (disabled | bearer).
2. Alias resolution: model name -> canonical (main-auto = selector-level:
   LEVEL A picks the canonical by task class).
3. Task classification (header x-hermes-taCHANGE_ME or message content)
   -> tier T0..T4.
4. Route eligibility: canonical match, discount floor (min_discount,
   default 0.80), capabilities, required context vs route context_length,
   certification status.
5. Ranking: health -> reliability -> cost_per_success. There is NO static
   provider priority; PREFER_PROVIDER exists only as an explicit override.
6. Cache affinity: prompt_cache_key stickiness; switching away from a
   healthy WARM route requires a projected saving over
   cache_switch_horizon requests with cache_switch_margin.
7. Failover plan built once per request: same-canonical alternates first;
   no-match -> controlled HTTP 502 model_selection_unavailable.

## Economics

- cost states: EXACT (observed billing) / ESTIMATED_UPPER_BOUND (catalog
  math) / UNKNOWN (sentinel, never free)
- expected-retry cost and expected cache-loss cost enter the comparison
- reliability_min_samples: cost_per_success becomes EXACT only after
  enough samples
- free_route_min_success_rate guards zero-cost routes

## Context / compression

- safe_context = min(0.90 x advertised, advertised - output_reserve)
- soft threshold 65% (compress), hard threshold 78% (protect), target
  post-compression utilization ~22-25%
- anti-thrash: no normal compression until max(100k tokens, 20% of
  safe_context) new context accumulated
- compressor failure order: same-canonical alternate provider -> alternate
  certified compressor -> chunked fallback -> controlled failure
  (conversation history is never lost)

## Model lifecycle

CORE / WATCH / UNAVAILABLE per canonical, driven by observed reliability;
quality floors per tier (T2 0.40 / T3 0.55 / T4 0.75); tier restrictions
and per-model policy via the control plane.

## Metrics

GET /metrics — per-model/route/provider requests, success rates, p50/p95,
cost (estimated vs actual). GET /router/decision — last routing decision
trace. POST /router/simulate — offline simulation (no upstream call).
Telemetry is privacy-safe: no prompts, no keys, no content.

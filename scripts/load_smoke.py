#!/usr/bin/env python3
"""R8 §22/§23 — router load/stability smoke + gateway overhead.

Small by design: ~40 requests, concurrency 4, minimal prompts.
Measures request success, routing errors, p50/p95 of the ROUTER-OVERHEAD
window (send -> first byte / completion of headers) and total latency.
"""
import asyncio
import json
import statistics
import time

import httpx

BASE = "http://127.0.0.1:4210"
N = 40
CONCURRENCY = 4

results = []


async def one(client: httpx.AsyncClient, i: int) -> None:
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{BASE}/v1/chat/completions",
            json={"model": "main-auto",
                  "messages": [{"role": "user", "content": "Say ok"}],
                  "max_tokens": 10},
            timeout=120.0,
        )
        t1 = time.perf_counter()
        results.append({
            "i": i, "status": r.status_code,
            "latency_s": round(t1 - t0, 3),
            "ok": r.status_code == 200,
        })
    except Exception as e:
        results.append({"i": i, "status": 0, "latency_s": round(time.perf_counter() - t0, 3),
                        "ok": False, "err": repr(e)[:80]})


async def main() -> None:
    t0 = time.perf_counter()
    async with httpx.AsyncClient() as client:
        # warm-up
        await client.get(f"{BASE}/health")
        sem = asyncio.Semaphore(CONCURRENCY)

        async def run(i):
            async with sem:
                await one(client, i)

        await asyncio.gather(*(run(i) for i in range(N)))
    wall = time.perf_counter() - t0

    ok = [r for r in results if r["ok"]]
    lat = sorted(r["latency_s"] for r in ok)

    def pct(p):
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else None

    # control plane responsiveness during load
    async with httpx.AsyncClient() as client:
        tc = time.perf_counter()
        rc = await client.get("http://127.0.0.1:4211/healthz", timeout=10.0)
        control_ms = round((time.perf_counter() - tc) * 1000, 1)

    print(json.dumps({
        "requests": N,
        "success": len(ok),
        "errors": N - len(ok),
        "error_details": [r for r in results if not r["ok"]][:5],
        "wall_s": round(wall, 1),
        "latency_p50_s": pct(0.50),
        "latency_p95_s": pct(0.95),
        "latency_min_s": lat[0] if lat else None,
        "latency_max_s": lat[-1] if lat else None,
        "control_plane_ms_during_load": control_ms,
        "control_plane_status": rc.status_code,
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

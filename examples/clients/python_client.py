#!/usr/bin/env python3
"""Generic [OI]-compatible client example for Model Router.

Requires: pip install httpx   (or use requests and adapt)

Router advertises a standard [OI] surface at /v1. Any client that speaks
that protocol works. No provider keys are needed client-side — the router
holds provider credentials privately.
"""
from __future__ import annotations

import asyncio

import httpx

BASE_URL = "http://127.0.0.1:4100/v1"
MODEL = "main-auto"          # or "compression-auto", or a concrete canonical


async def main() -> None:
    async with httpx.AsyncClient(timeout=120.0) as client:
        # Inference auth (if enabled): private config sets auth.mode=bearer
        headers = {"Authorization": "Bearer $MODEL_ROUTER_PROVIDER_A_TOKEN"}

        # 1. Non-streaming completion
        r = await client.post(
            f"{BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "Say 'ok' and nothing else."}],
            },
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]["content"]
        print("non-stream:", msg.strip())

        # 2. Streaming completion (SSE)
        async with client.stream(
            "POST",
            f"{BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": MODEL,
                "stream": True,
                "messages": [{"role": "user", "content": "Count 1 to 3."}],
            },
        ) as resp:
            buf = []
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                import json
                delta = (json.loads(payload)
                         .get("choices", [{}])[0]
                         .get("delta", {})
                         .get("content"))
                if delta:
                    buf.append(delta)
            print("stream:", "".join(buf))

        # 3. Tool calling
        r = await client.post(
            f"{BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "What is the weather in Moscow?"}],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }],
                "tool_choice": "auto",
            },
        )
        r.raise_for_status()
        print("tool-call:", r.json()["choices"][0]["message"].get("tool_calls"))


if __name__ == "__main__":
    asyncio.run(main())

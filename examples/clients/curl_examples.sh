#!/usr/bin/env bash
# Model Router — curl client examples (R8 §19).
# Replace $TOKEN with the inference token from private config if auth.mode=bearer.

BASE=http://127.0.0.1:4100

# Health / version
curl -s $BASE/health | python3 -m json.tool
curl -s $BASE/version | python3 -m json.tool

# Model list
curl -s $BASE/v1/models | python3 -m json.tool | head -40

# Non-streaming completion
curl -s $BASE/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "model": "main-auto",
    "messages": [{"role": "user", "content": "Say ok"}]
  }' | python3 -m json.tool

# Streaming completion
curl -N $BASE/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "model": "main-auto",
    "stream": true,
    "messages": [{"role": "user", "content": "Count 1 to 3"}]
  }'

# Tool calling
curl -s $BASE/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "main-auto",
    "messages": [{"role": "user", "content": "Weather in Moscow?"}],
    "tools": [{"type": "function", "function": {
      "name": "get_weather",
      "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}
    }}]
  }' | python3 -m json.tool

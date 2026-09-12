"""Timeout policy — single source of truth for the V2 Gateway.

connect           = 10s   TCP connect / TLS handshake
first_response     = 45s   TTFT (time-to-first-token / first byte)
stream_idle        = 60s   gap between two consecutive SSE chunks
normal_total       = 180s  end-to-end budget for non-T4 requests
t4_total           = 240s  end-to-end budget for T4 / CRITICAL tasks
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TimeoutPolicy:
    connect_s: float
    first_response_s: float
    stream_idle_s: float
    normal_total_s: float
    t4_total_s: float
    pre_first_failover_s: float = 20.0


DEFAULT = TimeoutPolicy(
    connect_s=10.0,
    first_response_s=45.0,
    stream_idle_s=60.0,
    normal_total_s=180.0,
    t4_total_s=240.0,
    pre_first_failover_s=20.0,
)


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_policy() -> TimeoutPolicy:
    """Allow per-environment overrides without code changes."""
    return TimeoutPolicy(
        connect_s=_envf("GW_CONNECT_TIMEOUT_S", DEFAULT.connect_s),
        first_response_s=_envf("GW_FIRST_RESPONSE_TIMEOUT_S", DEFAULT.first_response_s),
        stream_idle_s=_envf("GW_STREAM_IDLE_TIMEOUT_S", DEFAULT.stream_idle_s),
        normal_total_s=_envf("GW_NORMAL_TOTAL_TIMEOUT_S", DEFAULT.normal_total_s),
        t4_total_s=_envf("GW_T4_TOTAL_TIMEOUT_S", DEFAULT.t4_total_s),
        pre_first_failover_s=_envf("GW_PRE_FIRST_FAILOVER_TIMEOUT_S", DEFAULT.pre_first_failover_s),
    )


def total_for(tier: str, policy: TimeoutPolicy | None = None) -> float:
    """Return the total timeout applicable for the given tier."""
    p = policy or DEFAULT
    return p.t4_total_s if tier.upper() == "T4" else p.normal_total_s

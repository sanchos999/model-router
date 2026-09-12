"""Gateway V2 Context Manager — per-session context lifecycle + anti-thrash.

Spec G3 §5, §11, §12. Privacy-safe: stores only token counts, hashes, and
derived state — never raw prompt content, never tool arguments.

Responsibilities:
  * Track per-session context metadata (session_hash, ctx_tokens, last_compress).
  * Drive soft/hard compression triggers at FRACTION-OF-SAFE-CONTEXT (not fixed
    threshold_tokens). Hermes keeps the actual compactor call (out of scope);
    this manager PROVIDES the policy decision endpoints that a future thin-
    client Hermes (G5) will call.
  * Anti-thrash: after a compression event, suppress the NEXT normal compression
    until the session accumulates >= max(MIN_RECLAIM_TOKENS, FRACTION_OF_SAFE*ctx)
    of NEW context. Hard threshold (78%) bypasses anti-thrash and fires
    immediately — its sole job is to keep the request fitting safe_context.
  * Cache state tracking: per-session WARM/LIKELY_WARM/COLD/UNKNOWN, decayed by
    idle time. Used by the selector's cache-aware switch penalty.
  * Compression telemetry: writes a single line per compression event with the
    fields from spec G3 §11. No prompt content.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


# -- Tunable policy (env-overridable) -----------------------------------------
# Defaults from spec G3 §3: soft=65%, hard=78%, target=22-25%.
# Anti-thrash: max(100k tokens, 20% safe_context) of new context required.
SOFT_TRIGGER_FRACTION = float(os.environ.get("GW_CTX_SOFT_FRACTION", "0.65"))
HARD_TRIGGER_FRACTION = float(os.environ.get("GW_CTX_HARD_FRACTION", "0.78"))
TARGET_RATIO_FRACTION = float(os.environ.get("GW_CTX_TARGET_FRACTION", "0.25"))
TARGET_RATIO_MIN = float(os.environ.get("GW_CTX_TARGET_MIN", "0.22"))
TARGET_RATIO_MAX = float(os.environ.get("GW_CTX_TARGET_MAX", "0.25"))
ANTI_THRASH_MIN_TOKENS = int(os.environ.get("GW_CTX_ANTI_THRASH_MIN", "100000"))
ANTI_THRASH_FRACTION = float(os.environ.get("GW_CTX_ANTI_THRASH_FRACTION", "0.20"))

# Reserved output (mirrors Hermes / selector convention).
RESERVED_OUTPUT_DEFAULT = 8192
SAFE_CONTEXT_RATIO = 0.90

# Cache state TTL — WARM decays after this much idle time (sec).
CACHE_WARM_TTL_S = float(os.environ.get("GW_CACHE_WARM_TTL_S", "900"))
CACHE_LIKELY_WARM_TTL_S = float(os.environ.get("GW_CACHE_LIKELY_TTL_S", "300"))

# Per-session context TTL after last seen — older state is dropped.
SESSION_TTL_S = float(os.environ.get("GW_CTX_SESSION_TTL_S", "7200"))

# Conservative cache-switch saving threshold — only switch warm→cold when
# candidate is at least this much cheaper than the warm route, or the warm route
# is unhealthy. Spec G3 §6: configurable, conservative.
CACHE_SWITCH_MIN_SAVING_USD = float(os.environ.get("GW_CACHE_SWITCH_MIN_SAVING_USD", "0.002"))
CACHE_SWITCH_MIN_SAVING_PCT = float(os.environ.get("GW_CACHE_SWITCH_MIN_SAVING_PCT", "0.20"))

# Telemetry log path.
from gateway.state_paths import state_file

TELEMETRY_LOG = os.environ.get(
    "GW_CTX_TELEMETRY_LOG",
    state_file("context-compression.jsonl"),
)


def route_safe_context(context_length: int, reserved_output: int = RESERVED_OUTPUT_DEFAULT) -> int:
    """Mirror of gateway/selector.safe_context — kept local to avoid cross-imports."""
    if context_length <= 0:
        return 0
    return min(int(context_length * SAFE_CONTEXT_RATIO), context_length - reserved_output)


@dataclass
class SessionContextState:
    """Per-session privacy-safe context lifecycle record."""
    session_hash: str
    first_seen_ts: float
    last_seen_ts: float
    context_tokens: int = 0
    selected_canonical: str = ""
    selected_provider: str = ""
    selected_route_key: str = ""
    safe_context: int = 0
    utilization_pct: float = 0.0
    cache_state: str = "UNKNOWN"
    cache_state_updated_ts: float = 0.0
    cache_state_route_key: str = ""
    last_compression_tokens: int = 0
    tokens_since_compression: int = 0
    compression_count: int = 0
    last_compression_event_id: str = ""

    def touch(self) -> None:
        self.last_seen_ts = time.time()


@dataclass
class CompressionEvent:
    """Spec G3 §11 telemetry line. No raw content."""
    event_id: str
    ts: float
    session_hash: str
    before_tokens: int
    trigger_threshold: int
    trigger_kind: str
    safe_context: int
    utilization_pct: float
    compressor_canonical: str
    compressor_provider: str
    compressor_route_key: str
    compressor_kind: str
    compressor_api_ms: float
    total_compression_ms: float
    after_tokens: int
    summary_tokens: int
    retained_tail_tokens: int
    tokens_since_previous: int
    cost_usd: float
    cost_state: str
    cache_state_before: str
    cache_state_after: str
    token_counters: dict = field(default_factory=dict)
    failure_reason: str = ""

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass
class CacheSwitchDecision:
    switch: bool
    reason: str
    estimated_saving_usd: float
    cache_state_for_warm: str


class ContextManager:
    """Thread-safe per-session context lifecycle + anti-thrash + cache state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionContextState] = {}
        self._events: list[CompressionEvent] = []
        self._events_keep = int(os.environ.get("GW_CTX_EVENTS_KEEP", "500"))
        self._telemetry_handle = None
        self._telemetry_open = False
        self._ensure_telemetry_open()

    # -----------------------------------------------------------------------
    # Telemetry persistence
    # -----------------------------------------------------------------------
    def _ensure_telemetry_open(self) -> None:
        p = Path(TELEMETRY_LOG)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            self._telemetry_handle = open(p, "a", encoding="utf-8")
            self._telemetry_open = True
        except Exception:
            self._telemetry_handle = None
            self._telemetry_open = False

    def _emit_event(self, ev: CompressionEvent) -> None:
        with self._lock:
            self._events.append(ev)
            if len(self._events) > self._events_keep:
                self._events = self._events[-self._events_keep:]
            line = ev.to_line()
        if self._telemetry_open and self._telemetry_handle is not None:
            try:
                self._telemetry_handle.write(line + "\n")
                self._telemetry_handle.flush()
            except Exception:
                pass

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in self._events[-limit:]]

    # -----------------------------------------------------------------------
    # Session registry
    # -----------------------------------------------------------------------
    @staticmethod
    def _session_hash(raw_id: str) -> str:
        salt = os.environ.get("GW_CTX_SESSION_SALT", "gwctx-default-salt")
        h = hashlib.sha256((salt + "::" + str(raw_id)).encode("utf-8")).hexdigest()
        return "sha256:" + h[:32]

    def get_or_create(self, raw_session_id: str) -> SessionContextState:
        sid = self._session_hash(raw_session_id)
        with self._lock:
            st = self._sessions.get(sid)
            if st is None:
                now = time.time()
                st = SessionContextState(
                    session_hash=sid,
                    first_seen_ts=now,
                    last_seen_ts=now,
                )
                self._sessions[sid] = st
            else:
                st.touch()
            return st

    def cleanup_expired(self) -> int:
        with self._lock:
            cutoff = time.time() - SESSION_TTL_S
            before = len(self._sessions)
            self._sessions = {
                sid: st for sid, st in self._sessions.items()
                if st.last_seen_ts >= cutoff
            }
            return before - len(self._sessions)

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [asdict(st) for st in self._sessions.values()]

    # -----------------------------------------------------------------------
    # Cache-state helpers
    # -----------------------------------------------------------------------
    def _decay_cache_state(self, st: SessionContextState) -> str:
        if not st.cache_state_route_key:
            return "UNKNOWN"
        age = time.time() - st.cache_state_updated_ts
        if age <= CACHE_LIKELY_WARM_TTL_S:
            return st.cache_state or "WARM"
        if age <= CACHE_WARM_TTL_S:
            return "LIKELY_WARM"
        return "COLD"

    def cache_state_for(self, raw_session_id: str, route_key: str) -> str:
        sid = self._session_hash(raw_session_id)
        with self._lock:
            st = self._sessions.get(sid)
            if st is None:
                return "UNKNOWN"
            if st.cache_state_route_key != route_key:
                return "UNKNOWN"
            return self._decay_cache_state(st)

    # -----------------------------------------------------------------------
    # Observation hook from /v1/chat/completions
    # -----------------------------------------------------------------------
    def observe(
        self,
        raw_session_id: str,
        tokens_used: int,
        canonical: str,
        provider: str,
        route_key: str,
        safe_ctx: int,
    ) -> SessionContextState:
        sid = self._session_hash(raw_session_id)
        with self._lock:
            st = self._sessions.get(sid) or SessionContextState(
                session_hash=sid,
                first_seen_ts=time.time(),
                last_seen_ts=time.time(),
            )
            st.touch()
            st.context_tokens = max(0, int(tokens_used))
            st.selected_canonical = canonical or ""
            st.selected_provider = provider or ""
            st.selected_route_key = route_key or ""
            st.safe_context = max(0, int(safe_ctx))
            if st.safe_context > 0:
                st.utilization_pct = min(100.0, 100.0 * st.context_tokens / st.safe_context)
            else:
                st.utilization_pct = 0.0
            st.tokens_since_compression = max(0, st.context_tokens - st.last_compression_tokens)
            if route_key and route_key == st.cache_state_route_key:
                st.cache_state = "WARM"
                st.cache_state_updated_ts = time.time()
            elif route_key and route_key != st.cache_state_route_key:
                st.cache_state = "COLD"
                st.cache_state_updated_ts = time.time()
                st.cache_state_route_key = route_key
            self._sessions[sid] = st
            return st

    # -----------------------------------------------------------------------
    # Compression decision
    # -----------------------------------------------------------------------
    def should_compress(
        self,
        raw_session_id: str,
        context_tokens: int,
        safe_ctx: int,
    ) -> tuple[bool, str, str]:
        if safe_ctx <= 0:
            return False, "none", "no_safe_context"
        util = context_tokens / safe_ctx if safe_ctx > 0 else 0.0
        soft_thr = int(safe_ctx * SOFT_TRIGGER_FRACTION)
        hard_thr = int(safe_ctx * HARD_TRIGGER_FRACTION)
        if context_tokens >= hard_thr:
            return True, "hard", f"util={util:.3f}>={HARD_TRIGGER_FRACTION:.2f}"
        if context_tokens < soft_thr:
            return False, "none", f"util={util:.3f}<{SOFT_TRIGGER_FRACTION:.2f}"
        with self._lock:
            sid = self._session_hash(raw_session_id)
            st = self._sessions.get(sid)
            if st is None or st.last_compression_tokens <= 0:
                return True, "soft", f"first_soft util={util:.3f}"
            since = context_tokens - st.last_compression_tokens
            required = max(ANTI_THRASH_MIN_TOKENS, int(safe_ctx * ANTI_THRASH_FRACTION))
            if since < required:
                return (
                    False,
                    "anti_thrash_skipped",
                    f"since={since}<required={required}",
                )
            return True, "soft", f"since={since}>=required={required}"

    def target_tokens(self, safe_ctx: int) -> tuple[int, int]:
        lo = int(safe_ctx * TARGET_RATIO_MIN)
        hi = int(safe_ctx * TARGET_RATIO_MAX)
        return lo, hi

    # -----------------------------------------------------------------------
    # Compression bookkeeping
    # -----------------------------------------------------------------------
    def record_compression(
        self,
        raw_session_id: str,
        before_tokens: int,
        after_tokens: int,
        summary_tokens: int,
        retained_tail_tokens: int,
        compressor_canonical: str,
        compressor_provider: str,
        compressor_route_key: str,
        compressor_kind: str,
        trigger_kind: str,
        trigger_threshold: int,
        compressor_api_ms: float,
        total_compression_ms: float,
        cost_usd: float,
        cost_state: str,
        token_counters: dict | None = None,
        failure_reason: str = "",
    ) -> CompressionEvent:
        sid = self._session_hash(raw_session_id)
        with self._lock:
            st = self._sessions.get(sid) or SessionContextState(
                session_hash=sid,
                first_seen_ts=time.time(),
                last_seen_ts=time.time(),
            )
            st.touch()
            st.compression_count += 1
            prev_last_compression = st.last_compression_tokens
            st.last_compression_tokens = int(before_tokens)
            st.tokens_since_compression = max(0, int(before_tokens) - int(after_tokens))
            cache_before = self._decay_cache_state(st) if st.cache_state_route_key else "UNKNOWN"
            st.cache_state = "WARM"
            st.cache_state_route_key = compressor_route_key
            st.cache_state_updated_ts = time.time()
            cache_after = "WARM"
            self._sessions[sid] = st
            ev = CompressionEvent(
                event_id="ctx-" + hashlib.sha256(
                    (sid + str(time.time()) + compressor_route_key).encode()
                ).hexdigest()[:16],
                ts=time.time(),
                session_hash=sid,
                before_tokens=int(before_tokens),
                trigger_threshold=int(trigger_threshold),
                trigger_kind=trigger_kind,
                safe_context=st.safe_context,
                utilization_pct=100.0 * int(before_tokens) / st.safe_context if st.safe_context > 0 else 0.0,
                compressor_canonical=compressor_canonical,
                compressor_provider=compressor_provider,
                compressor_route_key=compressor_route_key,
                compressor_kind=compressor_kind,
                compressor_api_ms=compressor_api_ms,
                total_compression_ms=total_compression_ms,
                after_tokens=int(after_tokens),
                summary_tokens=int(summary_tokens),
                retained_tail_tokens=int(retained_tail_tokens),
                tokens_since_previous=max(0, int(before_tokens) - int(prev_last_compression or before_tokens)),
                cost_usd=cost_usd,
                cost_state=cost_state,
                cache_state_before=cache_before,
                cache_state_after=cache_after,
                token_counters=token_counters or {},
                failure_reason=failure_reason,
            )
            self._emit_event(ev)
            return ev


# -----------------------------------------------------------------------
# Cache-aware switch helper (spec G3 §6)
# -----------------------------------------------------------------------
def evaluate_cache_switch(
    *,
    warm_route_saving_usd: float | None,
    cold_route_saving_usd: float | None,
    warm_cache_state: str,
    warm_unhealthy: bool,
) -> CacheSwitchDecision:
    """Decide whether to switch from a warm route to a colder candidate.

    Rules (spec G3 §6):
      * Never switch when warm is healthy and warm is WARM unless savings clearly
        beat CACHE_SWITCH_MIN_SAVING_USD and CACHE_SWITCH_MIN_SAVING_PCT.
      * Switch freely when warm is unhealthy.
      * LIKELY_WARM halves the protection threshold.
      * COLD/UNKNOWN: free to switch on any positive saving.
      * Unknown savings: conservative (assume no saving, no switch unless warm unhealthy).
    """
    if warm_route_saving_usd is None or cold_route_saving_usd is None:
        return CacheSwitchDecision(
            switch=bool(warm_unhealthy),
            reason="unknown_savings",
            estimated_saving_usd=0.0,
            cache_state_for_warm=warm_cache_state,
        )
    delta_saving = warm_route_saving_usd - cold_route_saving_usd
    pct = (delta_saving / warm_route_saving_usd) if warm_route_saving_usd > 0 else 0.0

    if warm_unhealthy:
        return CacheSwitchDecision(
            switch=True,
            reason="warm_unhealthy",
            estimated_saving_usd=delta_saving,
            cache_state_for_warm=warm_cache_state,
        )

    if warm_cache_state == "WARM":
        big_usd = delta_saving >= CACHE_SWITCH_MIN_SAVING_USD
        big_pct = pct >= CACHE_SWITCH_MIN_SAVING_PCT
        if big_usd and big_pct:
            return CacheSwitchDecision(
                switch=True,
                reason="warm_with_big_saving",
                estimated_saving_usd=delta_saving,
                cache_state_for_warm=warm_cache_state,
            )
        return CacheSwitchDecision(
            switch=False,
            reason="warm_protected",
            estimated_saving_usd=delta_saving,
            cache_state_for_warm=warm_cache_state,
        )

    if warm_cache_state == "LIKELY_WARM":
        big_usd = delta_saving >= (CACHE_SWITCH_MIN_SAVING_USD * 0.5)
        big_pct = pct >= (CACHE_SWITCH_MIN_SAVING_PCT * 0.5)
        if big_usd and big_pct:
            return CacheSwitchDecision(
                switch=True,
                reason="likely_warm_with_saving",
                estimated_saving_usd=delta_saving,
                cache_state_for_warm=warm_cache_state,
            )
        return CacheSwitchDecision(
            switch=False,
            reason="likely_warm_protected",
            estimated_saving_usd=delta_saving,
            cache_state_for_warm=warm_cache_state,
        )

    if delta_saving > 0:
        return CacheSwitchDecision(
            switch=True,
            reason="cold_free_switch",
            estimated_saving_usd=delta_saving,
            cache_state_for_warm=warm_cache_state,
        )
    return CacheSwitchDecision(
        switch=False,
        reason="no_positive_saving",
        estimated_saving_usd=delta_saving,
        cache_state_for_warm=warm_cache_state,
    )


# Singleton for app-level reuse.
_singleton: ContextManager | None = None
_singleton_lock = threading.Lock()


def get_context_manager() -> ContextManager:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ContextManager()
        return _singleton

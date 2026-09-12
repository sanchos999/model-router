"""Gateway V2 configuration (spec G2 §19).

providers:
  provider_a: {enabled, min_discount}
  provider_b:  {enabled, min_discount}
Global defaults: min_discount=80, quality_first=true.
A new provider is added by adapter + one config block — no routing changes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:  # yaml optional — JSON fallback
    yaml = None

# R8 §3: default under the product data root; GW_CONFIG overrides (legacy).
CONFIG_PATH = os.environ.get(
    "GW_CONFIG",
    os.path.join(
        os.environ.get("GATEWAY_STATE_DIR",
                       os.environ.get("MODEL_ROUTER_STATE_DIR",
                                      str(Path.home() / "model-router-data"))),
        "gateway-v2.json",
    ),
)

_DEFAULTS = {
    "min_discount": 0.80,
    "quality_first": True,
    "health_ttl_success_s": 300.0,
    "health_ttl_failure_s": 45.0,
    # R5 economics policy (single source of truth — never duplicated elsewhere)
    "cache_switch_horizon": 3.0,        # projected-saving horizon (requests)
    "cache_switch_margin": 1.25,        # safety margin over rebuild penalty
    "reliability_min_samples": 10,      # samples before observed cost_per_success is EXACT
    "price_evidence_ttl_s": 86400.0,    # max age of price evidence
    "cache_affinity_ttl_s": 900.0,      # warm-cache affinity window (matches ctx mgr TTL)
    "canonical_switch_margin_factor": 2.0,  # extra caution when switching canonical in warm session
    "free_route_min_success_rate": 0.90,    # non-monetary gate for FREE route vs warm
    "providers": {
        "provider_a": {"enabled": True, "min_discount": 0.80},
        "provider_b": {"enabled": True, "min_discount": 0.80},
    },
}


@dataclass
class ProviderConfig:
    name: str
    enabled: bool = True
    min_discount: float = 0.80


@dataclass
class GatewayConfig:
    min_discount: float = 0.80
    quality_first: bool = True
    health_ttl_success_s: float = 300.0
    health_ttl_failure_s: float = 45.0
    # R5 economics policy
    cache_switch_horizon: float = 3.0
    cache_switch_margin: float = 1.25
    reliability_min_samples: int = 10
    price_evidence_ttl_s: float = 86400.0
    cache_affinity_ttl_s: float = 900.0
    canonical_switch_margin_factor: float = 2.0
    free_route_min_success_rate: float = 0.90
    # R6 §H: tier quality floors are runtime-editable routing policy.
    quality_floors: dict[str, float] = field(default_factory=lambda: {
        "T4": 0.75, "T3": 0.55, "T2": 0.40, "T1": 0.0})
    # R14 §17: taCHANGE_ME → tier overrides (runtime-editable). Missing
    # entries fall back to the classifier's code-level mapping.
    task_classes: dict[str, dict] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    def provider(self, name: str) -> ProviderConfig:
        pc = self.providers.get(name)
        if pc is None:
            pc = ProviderConfig(name=name, min_discount=self.min_discount)
            self.providers[name] = pc
        return pc

    def enabled_providers(self) -> list[str]:
        return [n for n, p in self.providers.items() if p.enabled]

    def apply_fields(self, config: dict) -> None:
        """R6 §D: hot-apply mutable policy IN PLACE (no object replacement —
        selector/registry hold this instance). Unknown keys are ignored here;
        validation happens upstream in the control plane."""
        scalar = {
            "min_discount": float, "quality_first": bool,
            "health_ttl_success_s": float, "health_ttl_failure_s": float,
            "cache_switch_horizon": float, "cache_switch_margin": float,
            "reliability_min_samples": int, "price_evidence_ttl_s": float,
            "cache_affinity_ttl_s": float, "canonical_switch_margin_factor": float,
            "free_route_min_success_rate": float,
        }
        for k, t in scalar.items():
            if k in config:
                setattr(self, k, t(config[k]))
        if isinstance(config.get("quality_floors"), dict):
            for t, v in config["quality_floors"].items():
                if t in {"T1", "T2", "T3", "T4"} and isinstance(v, (int, float)):
                    self.quality_floors[str(t)] = float(v)
        if isinstance(config.get("task_classes"), dict):
            # R14 §17: {CLASS: {"enabled": bool, "tier": "T1".."T4"}}
            for cls, tc in config["task_classes"].items():
                if not isinstance(tc, dict):
                    continue
                cur = dict(self.task_classes.get(cls) or {})
                if "enabled" in tc:
                    cur["enabled"] = bool(tc["enabled"])
                if tc.get("tier") in {"T1", "T2", "T3", "T4"}:
                    cur["tier"] = str(tc["tier"])
                self.task_classes[str(cls)] = cur
        provs = config.get("providers")
        if isinstance(provs, dict):
            for name, pc in provs.items():
                if not isinstance(pc, dict):
                    continue
                p = self.provider(name)
                if "enabled" in pc:
                    p.enabled = bool(pc["enabled"])
                if "min_discount" in pc:
                    p.min_discount = float(pc["min_discount"])


def load_config(path: str | None = None) -> GatewayConfig:
    p = Path(path or CONFIG_PATH)
    raw: dict = {}
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8")
            if p.suffix in {".yaml", ".yml"} and yaml is not None:
                raw = yaml.safe_load(text) or {}
            else:
                raw = json.loads(text)
        except Exception:
            raw = {}
    gw = raw.get("gateway", raw) if isinstance(raw, dict) else {}
    cfg = GatewayConfig(
        min_discount=float(gw.get("min_discount", _DEFAULTS["min_discount"])),
        quality_first=bool(gw.get("quality_first", _DEFAULTS["quality_first"])),
        health_ttl_success_s=float(gw.get("health_ttl_success_s", _DEFAULTS["health_ttl_success_s"])),
        health_ttl_failure_s=float(gw.get("health_ttl_failure_s", _DEFAULTS["health_ttl_failure_s"])),
        cache_switch_horizon=float(gw.get("cache_switch_horizon", _DEFAULTS["cache_switch_horizon"])),
        cache_switch_margin=float(gw.get("cache_switch_margin", _DEFAULTS["cache_switch_margin"])),
        reliability_min_samples=int(gw.get("reliability_min_samples", _DEFAULTS["reliability_min_samples"])),
        price_evidence_ttl_s=float(gw.get("price_evidence_ttl_s", _DEFAULTS["price_evidence_ttl_s"])),
        cache_affinity_ttl_s=float(gw.get("cache_affinity_ttl_s", _DEFAULTS["cache_affinity_ttl_s"])),
        canonical_switch_margin_factor=float(gw.get("canonical_switch_margin_factor", _DEFAULTS["canonical_switch_margin_factor"])),
        free_route_min_success_rate=float(gw.get("free_route_min_success_rate", _DEFAULTS["free_route_min_success_rate"])),
    )
    provs = gw.get("providers") or _DEFAULTS["providers"]
    for name, pc in provs.items():
        if not isinstance(pc, dict):
            continue
        cfg.providers[name] = ProviderConfig(
            name=name,
            enabled=bool(pc.get("enabled", True)),
            min_discount=float(pc.get("min_discount", cfg.min_discount)),
        )
    # ensure defaults exist even without config file
    for name in ("provider_a", "provider_b"):
        if name not in cfg.providers:
            cfg.providers[name] = ProviderConfig(name=name, min_discount=cfg.min_discount)
    return cfg


def write_default_config(path: str | None = None) -> str:
    p = Path(path or CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(json.dumps(_DEFAULTS, indent=2), encoding="utf-8")
    return str(p)

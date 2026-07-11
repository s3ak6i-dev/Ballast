"""Configuration dataclasses and defaults.

Defaults come from TechSpec §2.2 (breaker), §2.3 (backpressure), and §5 (API).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BreakerConfig:
    """Per-dependency circuit breaker thresholds."""

    #: Trip when failure rate in the rolling window exceeds this (0..1).
    failure_threshold: float = 0.5
    #: Trip when window p95 latency exceeds this multiple of the healthy baseline.
    latency_multiplier: float = 3.0
    #: Open→half-open cooldown; doubles on repeated trips (exponential backoff).
    cooldown_s: float = 10.0
    #: Backoff ceiling for the cooldown.
    max_cooldown_s: float = 300.0
    #: Rolling window: keep at most this many calls...
    window_max_calls: int = 20
    #: ...and drop entries older than this many seconds (whichever is smaller).
    window_max_age_s: float = 30.0
    #: Minimum calls in the window before trip conditions are evaluated.
    min_calls: int = 5
    #: Trial calls allowed through in half-open before deciding close/reopen.
    half_open_probes: int = 3

    def validate(self) -> None:
        if not 0.0 < self.failure_threshold <= 1.0:
            raise ValueError("failure_threshold must be in (0, 1]")
        if self.latency_multiplier <= 1.0:
            raise ValueError("latency_multiplier must be > 1")
        if self.cooldown_s <= 0 or self.max_cooldown_s < self.cooldown_s:
            raise ValueError("require 0 < cooldown_s <= max_cooldown_s")
        if self.window_max_calls < 1 or self.min_calls < 1:
            raise ValueError("window_max_calls and min_calls must be >= 1")
        if self.half_open_probes < 1:
            raise ValueError("half_open_probes must be >= 1")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BreakerConfig":
        """Build from the plain-dict form accepted by ballast.configure()."""
        cfg = cls(**raw)
        cfg.validate()
        return cfg


@dataclass(slots=True)
class BallastConfig:
    """Top-level runtime configuration (ballast.configure())."""

    dependencies: dict[str, BreakerConfig] = field(default_factory=dict)
    #: Global ceiling on concurrent in-flight guarded calls.
    max_concurrency: int = 100
    #: Waiting requests beyond max_concurrency queue up to this depth; beyond it they are shed.
    max_queue_depth: int = 500
    #: Cost budget (P1). None disables budget logic.
    budget_usd_per_hour: float | None = None
    #: Chaos injection master switch; BALLAST_CHAOS=1 in the environment also enables it.
    chaos_enabled: bool = False
    #: Breaker config applied to dependencies not listed explicitly.
    default_breaker: BreakerConfig = field(default_factory=BreakerConfig)

    def validate(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.max_queue_depth < 0:
            raise ValueError("max_queue_depth must be >= 0")
        if self.budget_usd_per_hour is not None and self.budget_usd_per_hour <= 0:
            raise ValueError("budget_usd_per_hour must be > 0 or None")
        for name, dep in self.dependencies.items():
            try:
                dep.validate()
            except ValueError as e:
                raise ValueError(f"dependency '{name}': {e}") from e

    @property
    def chaos_active(self) -> bool:
        """Chaos is available only when explicitly enabled (config or env)."""
        return self.chaos_enabled or os.environ.get("BALLAST_CHAOS") == "1"

    def breaker_config_for(self, dependency: str) -> BreakerConfig:
        return self.dependencies.get(dependency, self.default_breaker)

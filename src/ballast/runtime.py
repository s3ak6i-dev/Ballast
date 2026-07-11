"""Global runtime: owns the config, event bus, controller, breakers, and chaos
injector, and backs the module-level API (ballast.configure / status / chaos).

This is wiring, not core logic — fully implemented. The components it wires
(breaker, backpressure, chaos) gain their behavior in phase 2.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from ._clock import Clock, monotonic
from .backpressure import BackpressureController
from .breaker import CircuitBreaker
from .budget import BudgetTracker
from .chaos import ChaosInjector
from .config import BallastConfig, BreakerConfig
from .events import Event, EventBus
from .fallback import ResponseCache


class Runtime:
    """One per process. Rebuilt by configure(); breakers are created lazily for
    dependencies not declared up front (using the default breaker config)."""

    def __init__(self, config: BallastConfig | None = None, *, clock: Clock = monotonic) -> None:
        self.config = config or BallastConfig()
        self.config.validate()
        self.session_id = uuid.uuid4().hex[:12]
        self.clock = clock
        self.bus = EventBus()
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}
        self.controller = BackpressureController(
            self.config.max_concurrency,
            self.config.max_queue_depth,
            clock=clock,
            emit=self.emit,
        )
        self.chaos = ChaosInjector(
            is_enabled=lambda: self.config.chaos_active,
            clock=clock,
            emit=self.emit,
        )
        self.budget = BudgetTracker(
            self.config.budget_usd_per_hour,
            self.config.budget_soft_margin,
            self.config.budget_hard_behavior,
            clock=clock,
            emit=self.emit,
        )
        self.cache = ResponseCache(clock=clock)
        for name in self.config.dependencies:
            self.breaker(name)

    def emit(self, event: Event) -> None:
        """Stamp the session id onto component events and publish."""
        if event.session_id is None:
            event = Event(
                event_type=event.event_type,
                dependency=event.dependency,
                detail=event.detail,
                timestamp=event.timestamp,
                session_id=self.session_id,
            )
        self.bus.publish(event)

    def breaker(self, dependency: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(dependency)
            if breaker is None:
                breaker = CircuitBreaker(
                    dependency,
                    self.config.breaker_config_for(dependency),
                    clock=self.clock,
                    emit=self.emit,
                )
                self._breakers[dependency] = breaker
            return breaker

    def status(self) -> dict[str, Any]:
        """TechSpec §5 introspection: breaker states, queue depth, cost burn."""
        with self._lock:
            breakers = dict(self._breakers)
        return {
            "session_id": self.session_id,
            "dependencies": {name: b.status() for name, b in breakers.items()},
            "backpressure": self.controller.status(),
            "budget": self.budget.status(),
            "cache": self.cache.stats(),
            "chaos": {"enabled": self.config.chaos_active, "active": self.chaos.active()},
        }


# -- module-level singleton ----------------------------------------------------

_runtime_lock = threading.Lock()
_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """Return the process-wide runtime, creating a default-config one on first use."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = Runtime()
        return _runtime


def configure(
    *,
    dependencies: dict[str, dict[str, Any] | BreakerConfig] | None = None,
    max_concurrency: int = 100,
    max_queue_depth: int = 500,
    budget_usd_per_hour: float | None = None,
    budget_soft_margin: float = 0.8,
    budget_hard_behavior: str = "downgrade",
    chaos_enabled: bool = False,
) -> Runtime:
    """Build and install a fresh runtime (TechSpec §5 API).

    Replaces any existing runtime: breaker windows, queue state, and event-bus
    subscriptions are reset. Call once at startup, before wrapping calls.
    """
    global _runtime
    deps: dict[str, BreakerConfig] = {}
    for name, raw in (dependencies or {}).items():
        deps[name] = raw if isinstance(raw, BreakerConfig) else BreakerConfig.from_dict(raw)
    config = BallastConfig(
        dependencies=deps,
        max_concurrency=max_concurrency,
        max_queue_depth=max_queue_depth,
        budget_usd_per_hour=budget_usd_per_hour,
        budget_soft_margin=budget_soft_margin,
        budget_hard_behavior=budget_hard_behavior,
        chaos_enabled=chaos_enabled,
    )
    with _runtime_lock:
        _runtime = Runtime(config)
        return _runtime


def reset() -> None:
    """Drop the runtime (tests use this to isolate state between cases)."""
    global _runtime
    with _runtime_lock:
        _runtime = None


def status() -> dict[str, Any]:
    return get_runtime().status()


def record_cost(usd: float, dependency: str | None = None) -> None:
    """Report spend against the budget (call after a metered API call)."""
    get_runtime().budget.record(usd, dependency)

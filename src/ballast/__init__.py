"""Ballast — resilience and cost-control plane for multi-agent AI systems.

Public API (TechSpec §5):

    import ballast
    from ballast import guarded

    ballast.configure(dependencies={...}, max_concurrency=100)

    @guarded(dependency="openai_api", timeout=10, fallback="cached")
    def call_llm(prompt): ...

    with ballast.guard("postgres_db"):
        ...

    ballast.chaos.inject_failure("openai_api", rate=0.8)   # BALLAST_CHAOS=1 only
    ballast.status()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .breaker import BreakerState, CircuitBreaker
from .backpressure import BackpressureController
from .budget import BudgetTracker
from .config import BallastConfig, BreakerConfig
from .fallback import ResponseCache, RulesClassifier
from .events import Event, EventBus, EventType
from .exceptions import (
    BallastError,
    BudgetExceededError,
    ChaosError,
    CircuitOpenError,
    NotConfiguredError,
    QueueTimeoutError,
    RequestShedError,
)
from .interceptor import guard, guarded
from .runtime import Runtime, configure, get_runtime, record_cost, reset, status

if TYPE_CHECKING:
    from .chaos import ChaosInjector

__version__ = "0.1.0"

__all__ = [
    "configure",
    "status",
    "reset",
    "record_cost",
    "guarded",
    "guard",
    "chaos",
    "subscribe",
    "BudgetTracker",
    "ResponseCache",
    "RulesClassifier",
    "Event",
    "EventType",
    "EventBus",
    "BreakerState",
    "CircuitBreaker",
    "BackpressureController",
    "BallastConfig",
    "BreakerConfig",
    "Runtime",
    "BallastError",
    "ChaosError",
    "CircuitOpenError",
    "RequestShedError",
    "QueueTimeoutError",
    "BudgetExceededError",
    "NotConfiguredError",
]


def subscribe(fn):
    """Subscribe to the runtime's event stream; returns an unsubscribe callable."""
    return get_runtime().bus.subscribe(fn)


class _ChaosProxy:
    """Delegates to the *current* runtime's injector so `ballast.chaos` stays
    correct after re-configure(). Assigned below to shadow the `ballast.chaos`
    submodule binding created when the package imports it."""

    def __getattr__(self, name: str):
        return getattr(get_runtime().chaos, name)

    def __repr__(self) -> str:
        return f"<ballast.chaos proxy enabled={get_runtime().chaos.enabled}>"


chaos = _ChaosProxy()

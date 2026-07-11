"""Ballast exception hierarchy."""

from __future__ import annotations


class BallastError(Exception):
    """Base class for all Ballast errors."""


class CircuitOpenError(BallastError):
    """Raised when a call is refused because the dependency's breaker is open
    and no fallback was configured."""

    def __init__(self, dependency: str, cooldown_remaining_s: float | None = None):
        self.dependency = dependency
        self.cooldown_remaining_s = cooldown_remaining_s
        msg = f"circuit for '{dependency}' is open"
        if cooldown_remaining_s is not None:
            msg += f" (retry in ~{cooldown_remaining_s:.1f}s)"
        super().__init__(msg)


class RequestShedError(BallastError):
    """Raised when a request is rejected because the backpressure queue is full."""

    def __init__(self, queue_depth: int, max_queue_depth: int):
        self.queue_depth = queue_depth
        self.max_queue_depth = max_queue_depth
        super().__init__(
            f"request shed: queue depth {queue_depth} at ceiling {max_queue_depth}"
        )


class QueueTimeoutError(BallastError):
    """Raised when a queued request waited longer than its timeout for capacity."""

    def __init__(self, waited_s: float):
        self.waited_s = waited_s
        super().__init__(f"timed out after {waited_s:.1f}s waiting for capacity")


class BudgetExceededError(BallastError):
    """Raised when a call is refused because the hard cost ceiling was reached
    and the configured behavior is 'refuse'. (P1 — budget tracking.)"""

    def __init__(self, spent_usd: float, ceiling_usd: float):
        self.spent_usd = spent_usd
        self.ceiling_usd = ceiling_usd
        super().__init__(f"budget exceeded: ${spent_usd:.2f} of ${ceiling_usd:.2f} ceiling")


class ChaosError(BallastError):
    """Synthetic failure raised by the chaos injector's failure rule.

    Distinct from real dependency exceptions so logs and tests can always tell
    injected faults from genuine ones."""

    def __init__(self, dependency: str):
        self.dependency = dependency
        super().__init__(f"chaos-injected failure for '{dependency}'")


class NotConfiguredError(BallastError):
    """Raised for operations that require ballast.configure() to have run first."""

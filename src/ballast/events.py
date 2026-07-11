"""Event model and in-process event bus.

MVP per TechSpec §2.7: simple in-process pub/sub. The dashboard, SQLite audit
log, and terminal demo all attach as subscribers. Redis pub/sub is a P3 swap
behind the same interface.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

logger = logging.getLogger("ballast.events")


class EventType(StrEnum):
    """Event vocabulary (TechSpec §4 schema + UISpec manual overrides)."""

    BREAKER_TRIP = "breaker_trip"
    BREAKER_HALF_OPEN = "breaker_half_open"
    BREAKER_CLOSE = "breaker_close"
    FALLBACK_USED = "fallback_used"
    REQUEST_SHED = "request_shed"
    REQUEST_QUEUED = "request_queued"
    CHAOS_INJECTED = "chaos_injected"
    CHAOS_CLEARED = "chaos_cleared"
    MANUAL_OVERRIDE = "manual_override"


@dataclass(frozen=True, slots=True)
class Event:
    event_type: EventType
    dependency: str | None = None
    #: Free-form payload: latency, error message, cost delta, queue depth, etc.
    detail: dict[str, Any] = field(default_factory=dict)
    #: Wall-clock time (epoch seconds) — for display/audit, not for breaker logic.
    timestamp: float = field(default_factory=time.time)
    session_id: str | None = None


Subscriber = Callable[[Event], None]


class EventBus:
    """Thread-safe, synchronous, in-process pub/sub.

    Subscriber exceptions are logged and swallowed: an observability failure
    must never take down the guarded call path.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Register a subscriber; returns an unsubscribe callable."""
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(fn)
                except ValueError:
                    pass  # already unsubscribed

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception:
                logger.exception("event subscriber failed for %s", event.event_type)

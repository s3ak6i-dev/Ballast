"""Global backpressure controller (TechSpec §2.3).

Tracks total in-flight guarded calls. At max_concurrency, new requests queue
(strict FIFO — no barging past waiters); past max_queue_depth they are shed
with RequestShedError. Priority queueing is a later enhancement.

Wait timeouts use real wall time (threading primitives can't be driven by a
fake clock); the injected clock is used only for event timestamps/metrics.
Events are published outside the condition lock.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from ._clock import Clock, monotonic
from .events import Event, EventType
from .exceptions import QueueTimeoutError, RequestShedError

EventSink = Callable[[Event], None]


class BackpressureController:
    """Thread-safe concurrency gate shared by all guarded calls."""

    def __init__(
        self,
        max_concurrency: int = 100,
        max_queue_depth: int = 500,
        *,
        clock: Clock = monotonic,
        emit: EventSink | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if max_queue_depth < 0:
            raise ValueError("max_queue_depth must be >= 0")
        self.max_concurrency = max_concurrency
        self.max_queue_depth = max_queue_depth
        self._clock = clock
        self._emit = emit or (lambda event: None)
        self._cond = threading.Condition()
        self._in_flight = 0
        self._waiters: deque[int] = deque()  # FIFO of ticket numbers
        self._next_ticket = 0
        self._shed_total = 0

    def acquire(self, timeout_s: float | None = None) -> None:
        """Take an in-flight slot, blocking in FIFO order if at capacity.

        Raises RequestShedError when the queue is at max_queue_depth, or
        QueueTimeoutError when timeout_s elapses while queued.
        """
        events: list[Event] = []
        ticket: int | None = None
        with self._cond:
            if self._in_flight < self.max_concurrency and not self._waiters:
                self._in_flight += 1
                return
            if len(self._waiters) >= self.max_queue_depth:
                self._shed_total += 1
                depth = len(self._waiters)
                events.append(Event(
                    event_type=EventType.REQUEST_SHED,
                    detail={"queue_depth": depth, "max_queue_depth": self.max_queue_depth},
                ))
                shed = RequestShedError(depth, self.max_queue_depth)
            else:
                ticket = self._next_ticket
                self._next_ticket += 1
                self._waiters.append(ticket)
                events.append(Event(
                    event_type=EventType.REQUEST_QUEUED,
                    detail={"queue_depth": len(self._waiters)},
                ))
                shed = None
        self._publish(events)
        if ticket is None:
            raise shed

        # Queued: wait until we're at the head of the line with capacity free.
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        events = []
        try:
            with self._cond:
                while not (
                    self._waiters
                    and self._waiters[0] == ticket
                    and self._in_flight < self.max_concurrency
                ):
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        self._waiters.remove(ticket)
                        self._shed_total += 1
                        self._cond.notify_all()
                        events.append(Event(
                            event_type=EventType.REQUEST_SHED,
                            detail={"reason": "queue_timeout", "waited_s": timeout_s},
                        ))
                        raise QueueTimeoutError(timeout_s)
                    self._cond.wait(remaining)
                self._waiters.popleft()
                self._in_flight += 1
                self._cond.notify_all()  # capacity may remain for the next head
        finally:
            self._publish(events)

    def release(self) -> None:
        """Free an in-flight slot and wake the longest-waiting queued request."""
        with self._cond:
            if self._in_flight <= 0:
                raise RuntimeError("release() without matching acquire()")
            self._in_flight -= 1
            self._cond.notify_all()

    @contextmanager
    def slot(self, timeout_s: float | None = None) -> Iterator[None]:
        """``with controller.slot(): ...`` — acquire/release pairing."""
        self.acquire(timeout_s)
        try:
            yield
        finally:
            self.release()

    def status(self) -> dict[str, Any]:
        with self._cond:
            return {
                "in_flight": self._in_flight,
                "queue_depth": len(self._waiters),
                "shed_total": self._shed_total,
                "max_concurrency": self.max_concurrency,
                "max_queue_depth": self.max_queue_depth,
            }

    def _publish(self, events: list[Event]) -> None:
        for event in events:
            self._emit(event)

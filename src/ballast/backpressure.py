"""Global backpressure controller (TechSpec §2.3).

Tracks total in-flight guarded calls. At max_concurrency, new requests queue
(strict FIFO — no barging past waiters); past max_queue_depth they are shed
with RequestShedError. Priority queueing is a later enhancement.

Sync and async callers share one FIFO. Sync callers wait on the condition
variable; async callers wait on a per-waiter asyncio.Event that release()
sets via ``loop.call_soon_threadsafe`` — the event loop is never blocked.

Wait timeouts use real wall time (waiting primitives can't be driven by a
fake clock); the injected clock is used only for event timestamps/metrics.
Events are published outside the condition lock.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Callable, Iterator

from ._clock import Clock, monotonic
from .events import Event, EventType
from .exceptions import QueueTimeoutError, RequestShedError

EventSink = Callable[[Event], None]


class _Waiter:
    __slots__ = ("ticket", "kind", "event", "loop")

    def __init__(self, ticket: int, kind: str,
                 event: "asyncio.Event | None" = None,
                 loop: "asyncio.AbstractEventLoop | None" = None) -> None:
        self.ticket = ticket
        self.kind = kind  # "sync" | "async"
        self.event = event
        self.loop = loop


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
        self._waiters: deque[_Waiter] = deque()
        self._next_ticket = 0
        self._shed_total = 0

    # -- shared admission (returns a waiter to wait on, or raises/grants) ------

    def _admit_or_enqueue(self, kind: str) -> "_Waiter | None":
        """Fast-grant (returns None), enqueue (returns waiter), or shed (raises).
        Emits its events after releasing the lock."""
        events: list[Event] = []
        shed: RequestShedError | None = None
        waiter: _Waiter | None = None
        granted = False
        with self._cond:
            if self._in_flight < self.max_concurrency and not self._waiters:
                self._in_flight += 1
                granted = True
            elif len(self._waiters) >= self.max_queue_depth:
                self._shed_total += 1
                depth = len(self._waiters)
                events.append(Event(
                    event_type=EventType.REQUEST_SHED,
                    detail={"queue_depth": depth, "max_queue_depth": self.max_queue_depth},
                ))
                shed = RequestShedError(depth, self.max_queue_depth)
            else:
                self._next_ticket += 1
                if kind == "async":
                    waiter = _Waiter(self._next_ticket, kind,
                                     asyncio.Event(), asyncio.get_running_loop())
                else:
                    waiter = _Waiter(self._next_ticket, kind)
                self._waiters.append(waiter)
                events.append(Event(
                    event_type=EventType.REQUEST_QUEUED,
                    detail={"queue_depth": len(self._waiters)},
                ))
        self._publish(events)
        if shed is not None:
            raise shed
        return None if granted else waiter

    def _abandon(self, waiter: _Waiter, *, count_as_shed: bool) -> None:
        """Remove a waiter that gave up (timeout/cancellation) and pass the
        wake-up along if capacity had already been signalled to it."""
        with self._cond:
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass  # already popped by a racing grant path
            if count_as_shed:
                self._shed_total += 1
            self._wake_head_locked()

    def _grant_queued_locked(self, waiter: _Waiter) -> None:
        if self._waiters and self._waiters[0] is waiter:
            self._waiters.popleft()
        else:  # defensive: shouldn't happen, but never leave a ghost entry
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass
        self._in_flight += 1
        self._wake_head_locked()

    def _wake_head_locked(self) -> None:
        """Wake whichever waiter is now at the head, if capacity allows."""
        if self._in_flight >= self.max_concurrency or not self._waiters:
            return
        head = self._waiters[0]
        if head.kind == "async":
            head.loop.call_soon_threadsafe(head.event.set)  # type: ignore[union-attr]
        else:
            self._cond.notify_all()

    # -- sync path ---------------------------------------------------------------

    def acquire(self, timeout_s: float | None = None) -> None:
        """Take an in-flight slot, blocking in FIFO order if at capacity.

        Raises RequestShedError when the queue is at max_queue_depth, or
        QueueTimeoutError when timeout_s elapses while queued.
        """
        waiter = self._admit_or_enqueue("sync")
        if waiter is None:
            return

        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        events: list[Event] = []
        try:
            with self._cond:
                while not (
                    self._waiters
                    and self._waiters[0] is waiter
                    and self._in_flight < self.max_concurrency
                ):
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        self._waiters.remove(waiter)
                        self._shed_total += 1
                        self._wake_head_locked()
                        events.append(Event(
                            event_type=EventType.REQUEST_SHED,
                            detail={"reason": "queue_timeout", "waited_s": timeout_s},
                        ))
                        raise QueueTimeoutError(timeout_s)
                    self._cond.wait(remaining)
                self._grant_queued_locked(waiter)
        finally:
            self._publish(events)

    def release(self) -> None:
        """Free an in-flight slot and wake the longest-waiting queued request."""
        with self._cond:
            if self._in_flight <= 0:
                raise RuntimeError("release() without matching acquire()")
            self._in_flight -= 1
            self._wake_head_locked()

    @contextmanager
    def slot(self, timeout_s: float | None = None) -> Iterator[None]:
        """``with controller.slot(): ...`` — acquire/release pairing."""
        self.acquire(timeout_s)
        try:
            yield
        finally:
            self.release()

    # -- async path ----------------------------------------------------------------

    async def acquire_async(self, timeout_s: float | None = None) -> None:
        """Async twin of acquire(): awaits a slot without blocking the event
        loop, in the same FIFO as sync callers."""
        waiter = self._admit_or_enqueue("async")
        if waiter is None:
            return
        try:
            if timeout_s is None:
                await waiter.event.wait()  # type: ignore[union-attr]
            else:
                await asyncio.wait_for(waiter.event.wait(), timeout_s)  # type: ignore[union-attr]
        except TimeoutError:
            self._abandon(waiter, count_as_shed=True)
            self._emit(Event(
                event_type=EventType.REQUEST_SHED,
                detail={"reason": "queue_timeout", "waited_s": timeout_s},
            ))
            raise QueueTimeoutError(timeout_s) from None
        except asyncio.CancelledError:
            self._abandon(waiter, count_as_shed=False)
            raise
        with self._cond:
            self._grant_queued_locked(waiter)

    @asynccontextmanager
    async def slot_async(self, timeout_s: float | None = None) -> AsyncIterator[None]:
        """``async with controller.slot_async(): ...`` — acquire/release pairing."""
        await self.acquire_async(timeout_s)
        try:
            yield
        finally:
            self.release()

    # -- introspection ---------------------------------------------------------------

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

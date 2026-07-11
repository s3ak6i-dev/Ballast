"""Per-dependency circuit breaker (TechSpec §2.2).

State machine:
    CLOSED    — normal operation; rolling window of success/failure/latency.
    OPEN      — tripped (failure rate or latency); calls fail fast / go to fallback.
    HALF_OPEN — after cooldown, a few trial calls decide close vs. reopen.

Latency baseline: an EWMA of healthy success latencies. Latencies above
``latency_multiplier × baseline`` are excluded from the EWMA once the baseline
is established (>= min_calls samples) so a degrading dependency cannot drag
the baseline up and mask its own degradation.

Events are collected under the lock and published after it is released, so a
subscriber that re-enters the breaker cannot deadlock.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from enum import StrEnum
from typing import Any, Callable, NamedTuple

from ._clock import Clock, monotonic
from .config import BreakerConfig
from .events import Event, EventType

EventSink = Callable[[Event], None]

#: EWMA smoothing factor for the healthy-latency baseline.
_BASELINE_ALPHA = 0.2


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _WindowEntry(NamedTuple):
    t: float
    ok: bool
    latency_s: float | None


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[idx]


class CircuitBreaker:
    """Thread-safe breaker for one named dependency.

    Call protocol (used by the interceptor):
        1. ``try_acquire()`` — may this call proceed to the real dependency?
        2. run the call
        3. ``record_success(latency_s)`` or ``record_failure(latency_s, error)``
    """

    def __init__(
        self,
        name: str,
        config: BreakerConfig | None = None,
        *,
        clock: Clock = monotonic,
        emit: EventSink | None = None,
    ) -> None:
        self.name = name
        self.config = config or BreakerConfig()
        self.config.validate()
        self._clock = clock
        self._emit = emit or (lambda event: None)
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._window: deque[_WindowEntry] = deque()
        self._baseline = 0.0
        self._baseline_samples = 0
        self._consecutive_trips = 0
        self._opened_at = 0.0
        self._current_cooldown = self.config.cooldown_s
        self._probes_admitted = 0
        self._probe_successes = 0

    @property
    def state(self) -> BreakerState:
        return self._state

    # -- call protocol ----------------------------------------------------

    def try_acquire(self) -> bool:
        events: list[Event] = []
        with self._lock:
            if self._state is BreakerState.CLOSED:
                admitted = True
            elif self._state is BreakerState.OPEN:
                if self._clock() - self._opened_at >= self._current_cooldown:
                    self._state = BreakerState.HALF_OPEN
                    self._probes_admitted = 1  # this call is the first probe
                    self._probe_successes = 0
                    events.append(self._event(EventType.BREAKER_HALF_OPEN))
                    admitted = True
                else:
                    admitted = False
            else:  # HALF_OPEN
                if self._probes_admitted < self.config.half_open_probes:
                    self._probes_admitted += 1
                    admitted = True
                else:
                    admitted = False
        self._publish(events)
        return admitted

    def record_success(self, latency_s: float) -> None:
        events: list[Event] = []
        with self._lock:
            if self._state is BreakerState.OPEN:
                pass  # straggler finishing after a trip — window already reset
            elif self._state is BreakerState.HALF_OPEN:
                self._probe_successes += 1
                if self._probe_successes >= self.config.half_open_probes:
                    self._close(events, reason="probes_succeeded")
            else:  # CLOSED
                now = self._clock()
                self._window.append(_WindowEntry(now, True, latency_s))
                established = self._baseline_samples >= self.config.min_calls
                anomalous = (
                    established
                    and latency_s > self.config.latency_multiplier * self._baseline
                )
                if not anomalous:
                    if self._baseline_samples == 0:
                        self._baseline = latency_s
                    else:
                        self._baseline = (
                            (1 - _BASELINE_ALPHA) * self._baseline
                            + _BASELINE_ALPHA * latency_s
                        )
                    self._baseline_samples += 1
                self._trim(now)
                self._evaluate(events)
        self._publish(events)

    def record_failure(self, latency_s: float | None = None, error: str | None = None) -> None:
        events: list[Event] = []
        with self._lock:
            if self._state is BreakerState.OPEN:
                pass  # straggler — ignore
            elif self._state is BreakerState.HALF_OPEN:
                self._trip(events, reason="probe_failure", extra={"error": error})
            else:  # CLOSED
                now = self._clock()
                self._window.append(_WindowEntry(now, False, latency_s))
                self._trim(now)
                self._evaluate(events)
        self._publish(events)

    # -- manual overrides (UISpec §2.1) -------------------------------------

    def force_open(self) -> None:
        events: list[Event] = []
        with self._lock:
            self._trip(events, reason="manual", manual=True)
        self._publish(events)

    def reset(self) -> None:
        events: list[Event] = []
        with self._lock:
            self._close(events, reason="manual", manual=True)
        self._publish(events)

    # -- introspection ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            self._trim(now)
            calls = len(self._window)
            failures = sum(1 for e in self._window if not e.ok)
            latencies = [e.latency_s for e in self._window if e.latency_s is not None]
            cooldown_remaining = None
            if self._state is BreakerState.OPEN:
                cooldown_remaining = max(0.0, self._opened_at + self._current_cooldown - now)
            baseline_established = self._baseline_samples >= self.config.min_calls
            return {
                "name": self.name,
                "state": str(self._state),
                "window": {
                    "calls": calls,
                    "failures": failures,
                    "failure_rate": (failures / calls) if calls else 0.0,
                    "p95_latency_s": _p95(latencies) if latencies else None,
                },
                "baseline_latency_s": self._baseline if baseline_established else None,
                "consecutive_trips": self._consecutive_trips,
                "cooldown_remaining_s": cooldown_remaining,
            }

    # -- internals (all called with the lock held) ---------------------------

    def _trim(self, now: float) -> None:
        window = self._window
        while len(window) > self.config.window_max_calls:
            window.popleft()
        while window and now - window[0].t > self.config.window_max_age_s:
            window.popleft()

    def _evaluate(self, events: list[Event]) -> None:
        n = len(self._window)
        if n < self.config.min_calls:
            return
        failures = sum(1 for e in self._window if not e.ok)
        rate = failures / n
        if rate > self.config.failure_threshold:
            self._trip(events, reason="failure_rate", extra={"failure_rate": round(rate, 3)})
            return
        if self._baseline_samples >= self.config.min_calls and self._baseline > 0:
            latencies = [e.latency_s for e in self._window if e.latency_s is not None]
            if latencies:
                p95 = _p95(latencies)
                if p95 > self.config.latency_multiplier * self._baseline:
                    self._trip(
                        events,
                        reason="latency",
                        extra={"p95_s": round(p95, 4), "baseline_s": round(self._baseline, 4)},
                    )

    def _trip(self, events: list[Event], reason: str, extra: dict | None = None, manual: bool = False) -> None:
        self._consecutive_trips += 1
        self._current_cooldown = min(
            self.config.cooldown_s * 2 ** (self._consecutive_trips - 1),
            self.config.max_cooldown_s,
        )
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._window.clear()
        self._probes_admitted = self._probe_successes = 0
        detail = {"reason": reason, "cooldown_s": self._current_cooldown}
        if extra:
            detail.update({k: v for k, v in extra.items() if v is not None})
        event_type = EventType.MANUAL_OVERRIDE if manual else EventType.BREAKER_TRIP
        if manual:
            detail["action"] = "force_open"
        events.append(self._event(event_type, detail))

    def _close(self, events: list[Event], reason: str, manual: bool = False) -> None:
        self._state = BreakerState.CLOSED
        self._window.clear()
        self._consecutive_trips = 0
        self._current_cooldown = self.config.cooldown_s
        self._probes_admitted = self._probe_successes = 0
        detail: dict[str, Any] = {"reason": reason}
        event_type = EventType.MANUAL_OVERRIDE if manual else EventType.BREAKER_CLOSE
        if manual:
            detail["action"] = "reset"
        events.append(self._event(event_type, detail))

    def _event(self, event_type: EventType, detail: dict | None = None) -> Event:
        return Event(event_type=event_type, dependency=self.name, detail=detail or {})

    def _publish(self, events: list[Event]) -> None:
        for event in events:
            self._emit(event)

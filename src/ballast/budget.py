"""Budget tracking (TechSpec §2.4 trigger, PRD P1).

Tracks spend over a rolling one-hour window against a USD-per-hour budget.
Two ceilings:
    soft — ``soft_margin × budget``: eligible calls should downgrade to a
           cheaper model (the interceptor consults ``soft_exceeded``).
    hard — the budget itself: behavior is ``downgrade`` (force the fallback
           chain) or ``refuse`` (raise BudgetExceededError).

Ceiling crossings emit edge-triggered events (once per crossing, re-armed when
spend decays back under).
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Callable

from ._clock import Clock, monotonic
from .events import Event, EventType

EventSink = Callable[[Event], None]

WINDOW_S = 3600.0

HARD_BEHAVIORS = ("downgrade", "refuse")


class BudgetTracker:
    """Thread-safe rolling-window spend tracker. Inert when no budget is set."""

    def __init__(
        self,
        budget_usd_per_hour: float | None = None,
        soft_margin: float = 0.8,
        hard_behavior: str = "downgrade",
        *,
        clock: Clock = monotonic,
        emit: EventSink | None = None,
    ) -> None:
        if budget_usd_per_hour is not None and budget_usd_per_hour <= 0:
            raise ValueError("budget_usd_per_hour must be > 0 or None")
        if not 0.0 < soft_margin < 1.0:
            raise ValueError("soft_margin must be in (0, 1)")
        if hard_behavior not in HARD_BEHAVIORS:
            raise ValueError(f"hard_behavior must be one of {HARD_BEHAVIORS}")
        self.budget_usd_per_hour = budget_usd_per_hour
        self.soft_margin = soft_margin
        self.hard_behavior = hard_behavior
        self._clock = clock
        self._emit = emit or (lambda event: None)
        self._lock = threading.Lock()
        self._window: deque[tuple[float, float]] = deque()  # (t, usd)
        self._spent = 0.0
        self._soft_active = False
        self._hard_active = False

    @property
    def enabled(self) -> bool:
        return self.budget_usd_per_hour is not None

    @property
    def ceiling(self) -> float | None:
        return self.budget_usd_per_hour

    @property
    def soft_ceiling(self) -> float | None:
        if self.budget_usd_per_hour is None:
            return None
        return self.soft_margin * self.budget_usd_per_hour

    def record(self, usd: float, dependency: str | None = None) -> None:
        """Add spend to the window and fire ceiling-crossing events."""
        if usd < 0:
            raise ValueError("usd must be >= 0")
        events: list[Event] = []
        with self._lock:
            now = self._clock()
            self._window.append((now, usd))
            self._spent += usd
            self._refresh(now, events, dependency)
        for event in events:
            self._emit(event)

    def spent(self) -> float:
        """USD spent in the trailing hour."""
        with self._lock:
            self._refresh(self._clock(), [])
            return self._spent

    @property
    def soft_exceeded(self) -> bool:
        with self._lock:
            self._refresh(self._clock(), [])
            return self._soft_active

    @property
    def hard_exceeded(self) -> bool:
        with self._lock:
            self._refresh(self._clock(), [])
            return self._hard_active

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh(self._clock(), [])
            return {
                "enabled": self.enabled,
                "budget_usd_per_hour": self.budget_usd_per_hour,
                "soft_ceiling_usd": self.soft_ceiling,
                "spent_window_usd": round(self._spent, 6),
                "soft_exceeded": self._soft_active,
                "hard_exceeded": self._hard_active,
                "hard_behavior": self.hard_behavior,
            }

    # -- internals (lock held) ------------------------------------------------

    def _refresh(self, now: float, events: list[Event], dependency: str | None = None) -> None:
        while self._window and now - self._window[0][0] > WINDOW_S:
            _, usd = self._window.popleft()
            self._spent -= usd
        if not self.enabled:
            return
        soft = self._spent >= self.soft_ceiling  # type: ignore[operator]
        hard = self._spent >= self.budget_usd_per_hour  # type: ignore[operator]
        if soft and not self._soft_active:
            events.append(Event(
                event_type=EventType.BUDGET_SOFT_CEILING,
                dependency=dependency,
                detail={"spent_usd": round(self._spent, 6), "ceiling_usd": self.soft_ceiling},
            ))
        if hard and not self._hard_active:
            events.append(Event(
                event_type=EventType.BUDGET_HARD_CEILING,
                dependency=dependency,
                detail={"spent_usd": round(self._spent, 6), "ceiling_usd": self.budget_usd_per_hour},
            ))
        self._soft_active = soft
        self._hard_active = hard

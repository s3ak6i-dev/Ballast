"""Injectable clock so breaker/backpressure logic is testable without sleeping.

All timing-sensitive logic takes a ``Clock`` (a zero-arg callable returning
monotonic seconds). Production code uses ``time.monotonic``; tests pass a
``FakeClock`` they can advance manually.
"""

from __future__ import annotations

import time
from typing import Callable

Clock = Callable[[], float]

monotonic: Clock = time.monotonic


class FakeClock:
    """Deterministic clock for tests: ``clock.advance(5.0)``."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

"""Circuit breaker contract (TechSpec §2.2, PRD success metrics §7).

All timing goes through FakeClock — no sleeps.
"""

from ballast import BreakerConfig, BreakerState, EventType
from ballast._clock import FakeClock
from ballast.breaker import CircuitBreaker


def make_breaker(clock: FakeClock | None = None, **cfg) -> tuple[CircuitBreaker, list, FakeClock]:
    clock = clock or FakeClock()
    events: list = []
    breaker = CircuitBreaker("dep", BreakerConfig(**cfg), clock=clock, emit=events.append)
    return breaker, events, clock


def event_types(events) -> list[EventType]:
    return [e.event_type for e in events]


def fail_n(breaker: CircuitBreaker, n: int, latency: float = 0.01) -> None:
    for _ in range(n):
        breaker.record_failure(latency)


def trip(breaker: CircuitBreaker) -> None:
    """Drive a default-config breaker from CLOSED to OPEN via failure rate."""
    fail_n(breaker, 5)
    assert breaker.state is BreakerState.OPEN


class TestClosedToOpen:
    def test_trips_on_failure_rate_over_threshold(self):
        breaker, events, _ = make_breaker()
        fail_n(breaker, 4)
        assert breaker.state is BreakerState.CLOSED  # min_calls not reached
        breaker.record_failure(0.01)  # 5th failure: rate 1.0 > 0.5
        assert breaker.state is BreakerState.OPEN
        trips = [e for e in events if e.event_type is EventType.BREAKER_TRIP]
        assert len(trips) == 1
        assert trips[0].detail["reason"] == "failure_rate"
        assert trips[0].dependency == "dep"

    def test_no_trip_below_min_calls(self):
        breaker, events, _ = make_breaker()
        fail_n(breaker, 4)
        assert breaker.state is BreakerState.CLOSED
        assert events == []

    def test_trips_on_p95_latency_vs_baseline(self):
        breaker, events, _ = make_breaker()
        for _ in range(10):
            breaker.record_success(0.1)  # healthy baseline ~100ms
        assert breaker.state is BreakerState.CLOSED
        # Slow *successes* at 4× baseline (> multiplier 3×) must trip.
        for _ in range(3):
            breaker.record_success(0.4)
        assert breaker.state is BreakerState.OPEN
        trips = [e for e in events if e.event_type is EventType.BREAKER_TRIP]
        assert trips[0].detail["reason"] == "latency"
        assert trips[0].detail["baseline_s"] < trips[0].detail["p95_s"]

    def test_no_latency_trip_without_baseline(self):
        # Uniformly slow from the start: the baseline reflects reality, no trip.
        breaker, _, _ = make_breaker()
        for _ in range(10):
            breaker.record_success(0.5)
        assert breaker.state is BreakerState.CLOSED

    def test_window_trims_by_count_and_age(self):
        # Age: 4 old failures fall out of the 30s window and stop counting.
        breaker, _, clock = make_breaker()
        fail_n(breaker, 4)
        clock.advance(40)  # > window_max_age_s=30
        for _ in range(5):
            breaker.record_success(0.01)
        fail_n(breaker, 2)  # rate 2/7 ≈ 0.29 without the aged-out failures
        assert breaker.state is BreakerState.CLOSED

        # Count: a small window drops old successes, letting recent failures trip.
        breaker2, _, _ = make_breaker(window_max_calls=4, min_calls=2)
        for _ in range(10):
            breaker2.record_success(0.01)
        fail_n(breaker2, 3)  # window is [s,f,f,f] → rate 0.75 > 0.5
        assert breaker2.state is BreakerState.OPEN


class TestOpen:
    def test_open_refuses_calls_fast(self):
        breaker, _, _ = make_breaker()
        trip(breaker)
        assert breaker.try_acquire() is False
        before = breaker.status()["window"]
        breaker.record_failure(0.01)  # straggler: must not write to window
        assert breaker.status()["window"] == before

    def test_transitions_to_half_open_after_cooldown(self):
        breaker, events, clock = make_breaker()
        trip(breaker)
        clock.advance(10)  # default cooldown_s
        assert breaker.try_acquire() is True
        assert breaker.state is BreakerState.HALF_OPEN
        assert EventType.BREAKER_HALF_OPEN in event_types(events)


class TestHalfOpen:
    def enter_half_open(self, breaker, clock):
        trip(breaker)
        clock.advance(10)
        assert breaker.try_acquire() is True  # probe 1

    def test_admits_only_probe_quota(self):
        breaker, _, clock = make_breaker()  # half_open_probes=3
        self.enter_half_open(breaker, clock)
        assert breaker.try_acquire() is True  # probe 2
        assert breaker.try_acquire() is True  # probe 3
        assert breaker.try_acquire() is False  # quota exhausted

    def test_all_probes_succeed_closes(self):
        breaker, events, clock = make_breaker()
        self.enter_half_open(breaker, clock)
        breaker.try_acquire(), breaker.try_acquire()
        for _ in range(3):
            breaker.record_success(0.05)
        assert breaker.state is BreakerState.CLOSED
        assert EventType.BREAKER_CLOSE in event_types(events)
        assert breaker.status()["window"]["calls"] == 0  # window cleared

    def test_probe_failure_reopens_with_backoff(self):
        breaker, _, clock = make_breaker()
        self.enter_half_open(breaker, clock)
        breaker.record_failure(0.05)
        assert breaker.state is BreakerState.OPEN
        clock.advance(10)  # base cooldown is not enough anymore (doubled to 20)
        assert breaker.try_acquire() is False
        clock.advance(10)
        assert breaker.try_acquire() is True
        assert breaker.state is BreakerState.HALF_OPEN

    def test_backoff_resets_after_clean_close(self):
        breaker, _, clock = make_breaker()
        # Trip → full recovery.
        self.enter_half_open(breaker, clock)
        breaker.try_acquire(), breaker.try_acquire()
        for _ in range(3):
            breaker.record_success(0.05)
        assert breaker.state is BreakerState.CLOSED
        # Second trip must use the base cooldown again, not the doubled one.
        trip(breaker)
        clock.advance(10)
        assert breaker.try_acquire() is True


class TestManualOverrides:
    def test_force_open(self):
        breaker, events, _ = make_breaker()
        breaker.force_open()
        assert breaker.state is BreakerState.OPEN
        assert breaker.try_acquire() is False
        overrides = [e for e in events if e.event_type is EventType.MANUAL_OVERRIDE]
        assert overrides and overrides[0].detail["action"] == "force_open"

    def test_reset(self):
        breaker, events, _ = make_breaker()
        trip(breaker)
        breaker.reset()
        assert breaker.state is BreakerState.CLOSED
        assert breaker.try_acquire() is True
        assert breaker.status()["window"]["calls"] == 0
        assert breaker.status()["consecutive_trips"] == 0
        overrides = [e for e in events if e.event_type is EventType.MANUAL_OVERRIDE]
        assert overrides and overrides[-1].detail["action"] == "reset"


class TestStatus:
    def test_status_snapshot_fields(self):
        breaker, _, _ = make_breaker()
        status = breaker.status()
        assert status["state"] == "closed"
        assert status["window"] == {
            "calls": 0, "failures": 0, "failure_rate": 0.0, "p95_latency_s": None,
        }
        assert status["baseline_latency_s"] is None  # not yet established

        breaker.record_success(0.1)
        fail_n(breaker, 2)
        status = breaker.status()
        assert status["window"]["calls"] == 3
        assert status["window"]["failures"] == 2
        assert abs(status["window"]["failure_rate"] - 2 / 3) < 1e-9
        assert status["window"]["p95_latency_s"] is not None

        trip(breaker)
        status = breaker.status()
        assert status["state"] == "open"
        assert 0 < status["cooldown_remaining_s"] <= 10

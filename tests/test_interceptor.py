"""Interceptor SDK contract (TechSpec §2.1): @guarded and guard()."""

import time

import pytest

import ballast
from ballast import CircuitOpenError, EventType, RequestShedError, guard, guarded


def fallback_events(seen):
    return [e for e in seen if e.event_type is EventType.FALLBACK_USED]


class TestGuardedHappyPath:
    def test_success_records_latency_on_breaker(self):
        rt = ballast.configure()

        @guarded(dependency="api")
        def call(x):
            return x * 2

        assert call(21) == 42
        window = rt.breaker("api").status()["window"]
        assert window["calls"] == 1 and window["failures"] == 0
        assert window["p95_latency_s"] >= 0
        assert rt.controller.status()["in_flight"] == 0  # slot released

    def test_exception_records_failure_and_propagates(self):
        rt = ballast.configure()

        @guarded(dependency="api")
        def call():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            call()
        window = rt.breaker("api").status()["window"]
        assert window["failures"] == 1
        assert rt.controller.status()["in_flight"] == 0  # slot released on failure


class TestFallback:
    def test_open_breaker_uses_static_fallback(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()
        seen: list = []
        ballast.subscribe(seen.append)
        called = []

        @guarded(dependency="api", fallback="cached")
        def call():
            called.append(True)

        assert call() == "cached"
        assert not called  # real callable never touched
        assert fallback_events(seen)[0].detail["reason"] == "breaker_open"

    def test_open_breaker_uses_callable_fallback(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()

        @guarded(dependency="api", fallback=lambda x, suffix="": f"fb:{x}{suffix}")
        def call(x, suffix=""):
            raise AssertionError("must not run")

        assert call(7, suffix="!") == "fb:7!"

    def test_open_breaker_without_fallback_raises(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()

        @guarded(dependency="api")
        def call():
            raise AssertionError("must not run")

        with pytest.raises(CircuitOpenError) as exc_info:
            call()
        assert exc_info.value.dependency == "api"

    def test_none_is_a_valid_fallback(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()

        @guarded(dependency="api", fallback=None)
        def call():
            raise AssertionError("must not run")

        assert call() is None


class TestFallbackOnError:
    def test_failed_call_serves_chain_when_enabled(self):
        rt = ballast.configure()
        seen: list = []
        ballast.subscribe(seen.append)

        @guarded(dependency="api", fallback="degraded", fallback_on_error=True)
        def call():
            raise ValueError("boom")

        assert call() == "degraded"
        assert rt.breaker("api").status()["window"]["failures"] == 1  # still recorded
        assert fallback_events(seen)[0].detail == {"reason": "call_failed", "rung": "static"}

    def test_default_still_propagates(self):
        ballast.configure()

        @guarded(dependency="api", fallback="degraded")
        def call():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            call()

    def test_empty_chain_reraises_original(self):
        ballast.configure()

        @guarded(dependency="api", fallback_on_error=True)
        def call():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            call()


class TestTimeout:
    def test_slow_call_recorded_as_failure(self):
        rt = ballast.configure()

        @guarded(dependency="api", timeout=0.01)
        def slow():
            time.sleep(0.03)
            return "late but valid"

        assert slow() == "late but valid"  # sync MVP: result still returned
        window = rt.breaker("api").status()["window"]
        assert window["failures"] == 1


class TestBackpressureIntegration:
    def test_shed_request_without_fallback_raises(self):
        rt = ballast.configure(max_concurrency=1, max_queue_depth=0)
        rt.controller.acquire()  # exhaust capacity
        try:
            @guarded(dependency="api")
            def call():
                return "unreachable"

            with pytest.raises(RequestShedError):
                call()
            assert rt.breaker("api").status()["window"]["calls"] == 0  # breaker untouched
        finally:
            rt.controller.release()

    def test_shed_request_with_fallback_returns_it(self):
        rt = ballast.configure(max_concurrency=1, max_queue_depth=0)
        rt.controller.acquire()
        try:
            seen: list = []
            ballast.subscribe(seen.append)

            @guarded(dependency="api", fallback="degraded")
            def call():
                return "unreachable"

            assert call() == "degraded"
            assert fallback_events(seen)[0].detail["reason"] == "shed"
        finally:
            rt.controller.release()


class TestGuardContextManager:
    def test_block_success_records_latency(self):
        rt = ballast.configure()
        with guard("db"):
            pass
        window = rt.breaker("db").status()["window"]
        assert window["calls"] == 1 and window["failures"] == 0
        assert rt.controller.status()["in_flight"] == 0

    def test_block_exception_records_failure(self):
        rt = ballast.configure()
        with pytest.raises(KeyError):
            with guard("db"):
                raise KeyError("missing")
        window = rt.breaker("db").status()["window"]
        assert window["failures"] == 1
        assert rt.controller.status()["in_flight"] == 0

    def test_open_breaker_raises_before_block(self):
        rt = ballast.configure()
        rt.breaker("db").force_open()
        entered = []
        with pytest.raises(CircuitOpenError):
            with guard("db"):
                entered.append(True)
        assert not entered
        assert rt.controller.status()["in_flight"] == 0

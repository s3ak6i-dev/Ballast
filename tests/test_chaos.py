"""Chaos injector contract (TechSpec §2.6)."""

import time

import pytest

from ballast import ChaosError, EventType
from ballast._clock import FakeClock, monotonic
from ballast.chaos import MAX_INJECTION_S, ChaosInjector


def make_injector(enabled=True, clock=None):
    events: list = []
    injector = ChaosInjector(
        is_enabled=lambda: enabled, clock=clock or monotonic, emit=events.append
    )
    return injector, events


def event_types(events):
    return [e.event_type for e in events]


class TestEnablement:
    def test_disabled_injector_is_passthrough(self):
        injector, events = make_injector(enabled=False)
        injector.inject_failure("api", rate=1.0)  # warning no-op
        assert injector.apply("api", lambda: "ok") == "ok"
        assert events == []

    def test_rules_apply_only_to_named_dependency(self):
        injector, _ = make_injector()
        injector.inject_failure("api_a", rate=1.0)
        with pytest.raises(ChaosError):
            injector.apply("api_a", lambda: "ok")
        assert injector.apply("api_b", lambda: "ok") == "ok"


class TestFaults:
    def test_inject_failure_rate(self):
        injector, events = make_injector()
        injector.inject_failure("api", rate=1.0)
        for _ in range(5):
            with pytest.raises(ChaosError):
                injector.apply("api", lambda: "ok")
        injected = [e for e in events if e.event_type is EventType.CHAOS_INJECTED]
        assert len(injected) == 1  # once when the rule is set, not per call
        assert injected[0].detail["kind"] == "failure"

    def test_inject_latency_multiplier(self):
        injector, _ = make_injector()
        injector.inject_latency("api", multiplier=3.0)

        def real_call():
            time.sleep(0.05)
            return "ok"

        start = time.monotonic()
        assert injector.apply("api", real_call) == "ok"
        wall = time.monotonic() - start
        assert wall >= 0.13  # ~3× the real ~0.05s

    def test_inject_corruption_mangles_result(self):
        injector, _ = make_injector()
        injector.inject_corruption("api", rate=1.0)
        result = injector.apply("api", lambda: "hello world")
        assert result != "hello world"
        assert result.endswith("…")
        # Non-string results degrade to None rather than a mangled string.
        assert injector.apply("api", lambda: {"a": 1}) is None

    def test_duration_cap_enforced(self):
        clock = FakeClock()
        injector, events = make_injector(clock=clock)
        injector.inject_failure("api", rate=1.0, duration_s=10_000)  # clamped to 600
        injected = [e for e in events if e.event_type is EventType.CHAOS_INJECTED]
        assert injected[0].detail["duration_s"] == MAX_INJECTION_S

        clock.advance(599)
        with pytest.raises(ChaosError):
            injector.apply("api", lambda: "ok")
        clock.advance(2)  # past expiry — rule lapses on its own
        assert injector.apply("api", lambda: "ok") == "ok"
        cleared = [e for e in events if e.event_type is EventType.CHAOS_CLEARED]
        assert cleared and cleared[0].detail["reason"] == "expired"

    def test_clear_removes_rules(self):
        injector, events = make_injector()
        injector.inject_failure("api_a", rate=1.0)
        injector.inject_failure("api_b", rate=1.0)
        injector.clear("api_a")
        assert injector.apply("api_a", lambda: "ok") == "ok"
        with pytest.raises(ChaosError):
            injector.apply("api_b", lambda: "ok")
        injector.clear()  # clear all
        assert injector.apply("api_b", lambda: "ok") == "ok"
        assert EventType.CHAOS_CLEARED in event_types(events)


class TestScenarios:
    def test_api_outage_preset_lifecycle(self):
        clock = FakeClock()
        injector, events = make_injector(clock=clock)
        injector.scenario("api_outage").run("api", duration_s=30)
        with pytest.raises(ChaosError):
            injector.apply("api", lambda: "ok")  # 100% failure while active
        clock.advance(31)
        assert injector.apply("api", lambda: "ok") == "ok"  # recovered
        assert EventType.CHAOS_CLEARED in event_types(events)

    def test_unknown_scenario_raises(self):
        injector, _ = make_injector()
        with pytest.raises(KeyError, match="api_outage"):  # message lists presets
            injector.scenario("nope")

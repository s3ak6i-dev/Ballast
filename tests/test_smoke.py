"""Scaffold smoke tests — these run (and must pass) before phase 2."""

import ballast
from ballast import BreakerConfig, Event, EventType
from ballast.config import BallastConfig

import pytest


def test_public_api_importable():
    for name in ballast.__all__:
        assert getattr(ballast, name, None) is not None, name


def test_breaker_defaults_match_techspec():
    cfg = BreakerConfig()
    assert cfg.failure_threshold == 0.5
    assert cfg.latency_multiplier == 3.0
    assert cfg.cooldown_s == 10.0
    assert cfg.window_max_calls == 20
    assert cfg.window_max_age_s == 30.0
    assert cfg.min_calls == 5


def test_configure_builds_runtime_from_dicts():
    rt = ballast.configure(
        dependencies={
            "openai_api": {"failure_threshold": 0.5, "latency_multiplier": 3, "cooldown_s": 10},
            "postgres_db": {"failure_threshold": 0.3, "latency_multiplier": 2, "cooldown_s": 5},
        },
        max_concurrency=100,
        max_queue_depth=500,
        budget_usd_per_hour=10.0,
    )
    assert rt.config.dependencies["postgres_db"].failure_threshold == 0.3
    assert rt.breaker("openai_api").config.cooldown_s == 10
    # undeclared dependency gets the default breaker config, lazily
    assert rt.breaker("surprise_dep").config.failure_threshold == 0.5


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        BreakerConfig.from_dict({"failure_threshold": 1.5})
    with pytest.raises(ValueError):
        BallastConfig(max_concurrency=0).validate()


def test_event_bus_pub_sub_and_unsubscribe():
    rt = ballast.configure()
    seen: list[Event] = []
    unsubscribe = rt.bus.subscribe(seen.append)

    event = Event(event_type=EventType.BREAKER_TRIP, dependency="x")
    rt.bus.publish(event)
    assert seen == [event]

    unsubscribe()
    rt.bus.publish(event)
    assert len(seen) == 1


def test_event_bus_survives_bad_subscriber():
    rt = ballast.configure()
    seen: list[Event] = []

    def bad(_event: Event) -> None:
        raise RuntimeError("subscriber bug")

    rt.bus.subscribe(bad)
    rt.bus.subscribe(seen.append)
    rt.bus.publish(Event(event_type=EventType.REQUEST_SHED))
    assert len(seen) == 1  # bad subscriber didn't block delivery


def test_chaos_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BALLAST_CHAOS", raising=False)
    rt = ballast.configure()
    assert rt.chaos.enabled is False
    assert ballast.configure(chaos_enabled=True).chaos.enabled is True
    monkeypatch.setenv("BALLAST_CHAOS", "1")
    assert ballast.configure().chaos.enabled is True
    # module-level accessor must be the injector proxy, not the submodule
    assert ballast.chaos.enabled is True

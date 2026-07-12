"""Chaos injector (TechSpec §2.6).

Wraps the *real* dependency call with deliberate fault injection so the breaker
and backpressure logic under test is the production code path, not a mock.
Only active when chaos is explicitly enabled (BALLAST_CHAOS=1 or
configure(chaos_enabled=True)); a plain pass-through otherwise.

Rules are time-boxed: every injection carries an expiry (clock-based, lazily
enforced) hard-capped at MAX_INJECTION_S — no indefinite faults (UISpec §3.3).
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from ._clock import Clock, monotonic
from .events import Event, EventType
from .exceptions import ChaosError

logger = logging.getLogger("ballast.chaos")

EventSink = Callable[[Event], None]

#: UI/API-level ceiling on a single injection's duration (seconds).
MAX_INJECTION_S = 600.0

#: Preset scenario names → description (TechSpec §2.6 presets, UISpec §3.2).
SCENARIO_PRESETS: dict[str, str] = {
    "api_outage": "100% failure on the target dependency, then recovery",
    "slow_api": "3× latency on the target dependency, then recovery",
    "flaky_api": "50% failure on the target dependency, then recovery",
}


@dataclass(slots=True)
class _Rule:
    kind: str  # "failure" | "latency" | "corruption"
    value: float  # rate (0..1) or latency multiplier
    expires_at: float


class ChaosInjector:
    """Holds active fault rules per dependency and applies them around calls."""

    def __init__(
        self,
        *,
        is_enabled: Callable[[], bool],
        clock: Clock = monotonic,
        emit: EventSink | None = None,
    ) -> None:
        #: Deferred so a config change (or env var) takes effect immediately.
        self._is_enabled = is_enabled
        self._clock = clock
        self._emit = emit or (lambda event: None)
        self._lock = threading.Lock()
        self._rules: dict[str, dict[str, _Rule]] = {}
        self._rng = random.Random()

    @property
    def enabled(self) -> bool:
        return self._is_enabled()

    # -- fault rules --------------------------------------------------------

    def inject_latency(self, dependency: str, multiplier: float, duration_s: float = MAX_INJECTION_S) -> None:
        """Slow calls to `dependency` by `multiplier`× their real elapsed time."""
        if multiplier <= 1.0:
            raise ValueError("multiplier must be > 1")
        self._set_rule(dependency, "latency", multiplier, duration_s)

    def inject_failure(self, dependency: str, rate: float, duration_s: float = MAX_INJECTION_S) -> None:
        """Raise a synthetic ChaosError on `rate` (0..1) of calls to `dependency`."""
        if not 0.0 < rate <= 1.0:
            raise ValueError("rate must be in (0, 1]")
        self._set_rule(dependency, "failure", rate, duration_s)

    def inject_corruption(self, dependency: str, rate: float, duration_s: float = MAX_INJECTION_S) -> None:
        """Return a truncated/mangled response on `rate` (0..1) of calls."""
        if not 0.0 < rate <= 1.0:
            raise ValueError("rate must be in (0, 1]")
        self._set_rule(dependency, "corruption", rate, duration_s)

    def clear(self, dependency: str | None = None) -> None:
        """Remove fault rules for one dependency (or all). Emits CHAOS_CLEARED."""
        with self._lock:
            if dependency is None:
                cleared = list(self._rules)
                self._rules.clear()
            else:
                cleared = [dependency] if self._rules.pop(dependency, None) else []
        for name in cleared:
            self._emit(Event(
                event_type=EventType.CHAOS_CLEARED,
                dependency=name,
                detail={"reason": "manual"},
            ))

    # -- call-path hook (used by the interceptor) ----------------------------

    def apply(self, dependency: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Invoke `func`, applying any active fault rules for `dependency`."""
        if not self.enabled:
            return func(*args, **kwargs)
        rules = self._active_rules(dependency)
        if not rules:
            return func(*args, **kwargs)

        failure = rules.get("failure")
        if failure is not None and self._rng.random() < failure.value:
            raise ChaosError(dependency)

        latency = rules.get("latency")
        if latency is not None:
            start = self._clock()
            result = func(*args, **kwargs)
            elapsed = self._clock() - start
            extra = max(0.0, elapsed * (latency.value - 1.0))
            if extra:
                time.sleep(extra)
        else:
            result = func(*args, **kwargs)

        corruption = rules.get("corruption")
        if corruption is not None and self._rng.random() < corruption.value:
            result = self._corrupt(result)
        return result

    async def apply_async(
        self, dependency: str, func: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any, **kwargs: Any,
    ) -> Any:
        """Async twin of apply(): awaits `func` and uses asyncio.sleep for
        injected latency so the event loop is never blocked."""
        if not self.enabled:
            return await func(*args, **kwargs)
        rules = self._active_rules(dependency)
        if not rules:
            return await func(*args, **kwargs)

        failure = rules.get("failure")
        if failure is not None and self._rng.random() < failure.value:
            raise ChaosError(dependency)

        latency = rules.get("latency")
        if latency is not None:
            start = self._clock()
            result = await func(*args, **kwargs)
            elapsed = self._clock() - start
            extra = max(0.0, elapsed * (latency.value - 1.0))
            if extra:
                await asyncio.sleep(extra)
        else:
            result = await func(*args, **kwargs)

        corruption = rules.get("corruption")
        if corruption is not None and self._rng.random() < corruption.value:
            result = self._corrupt(result)
        return result

    # -- introspection (dashboard chaos banner) --------------------------------

    def active(self) -> dict[str, list[dict[str, Any]]]:
        """Snapshot of live rules: {dependency: [{kind, value, remaining_s}]}."""
        now = self._clock()
        with self._lock:
            return {
                dep: [
                    {
                        "kind": rule.kind,
                        "value": rule.value,
                        "remaining_s": round(rule.expires_at - now, 1),
                    }
                    for rule in rules.values()
                    if rule.expires_at > now
                ]
                for dep, rules in self._rules.items()
                if any(rule.expires_at > now for rule in rules.values())
            }

    # -- scenarios ------------------------------------------------------------

    def scenario(self, name: str) -> "Scenario":
        """Look up a preset by name (see SCENARIO_PRESETS)."""
        if name not in SCENARIO_PRESETS:
            raise KeyError(f"unknown scenario '{name}'; available: {sorted(SCENARIO_PRESETS)}")
        return Scenario(name, self)

    # -- internals -------------------------------------------------------------

    def _set_rule(self, dependency: str, kind: str, value: float, duration_s: float) -> None:
        if not self.enabled:
            logger.warning(
                "chaos is disabled — ignoring %s injection for '%s' "
                "(set BALLAST_CHAOS=1 or configure(chaos_enabled=True))",
                kind, dependency,
            )
            return
        duration = min(duration_s, MAX_INJECTION_S)
        with self._lock:
            self._rules.setdefault(dependency, {})[kind] = _Rule(
                kind=kind, value=value, expires_at=self._clock() + duration
            )
        self._emit(Event(
            event_type=EventType.CHAOS_INJECTED,
            dependency=dependency,
            detail={"kind": kind, "value": value, "duration_s": duration},
        ))

    def _active_rules(self, dependency: str) -> dict[str, _Rule]:
        """Return live rules for `dependency`, lazily expiring stale ones."""
        now = self._clock()
        expired: list[str] = []
        with self._lock:
            rules = self._rules.get(dependency)
            if not rules:
                return {}
            for kind, rule in list(rules.items()):
                if now >= rule.expires_at:
                    del rules[kind]
                    expired.append(kind)
            if not rules:
                self._rules.pop(dependency, None)
            active = dict(rules)
        for kind in expired:
            self._emit(Event(
                event_type=EventType.CHAOS_CLEARED,
                dependency=dependency,
                detail={"kind": kind, "reason": "expired"},
            ))
        return active

    @staticmethod
    def _corrupt(result: Any) -> Any:
        if isinstance(result, str):
            return result[: max(1, len(result) // 2)] + "…"  # truncation marker
        return None


class Scenario:
    """A named, timed bundle of fault rules, e.g. chaos.scenario('api_outage')."""

    def __init__(self, name: str, injector: ChaosInjector) -> None:
        self.name = name
        self.description = SCENARIO_PRESETS[name]
        self._injector = injector

    def run(self, dependency: str, duration_s: float = 30.0) -> None:
        """Apply the scenario's rules to `dependency` for `duration_s` seconds.

        Non-blocking: rules expire on their own (clock-based, capped at
        MAX_INJECTION_S)."""
        duration = min(duration_s, MAX_INJECTION_S)
        if self.name == "api_outage":
            self._injector.inject_failure(dependency, 1.0, duration)
        elif self.name == "slow_api":
            self._injector.inject_latency(dependency, 3.0, duration)
        elif self.name == "flaky_api":
            self._injector.inject_failure(dependency, 0.5, duration)
        else:  # pragma: no cover — scenario() validates names
            raise KeyError(self.name)

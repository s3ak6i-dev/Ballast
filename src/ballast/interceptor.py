"""Interceptor SDK: @guarded decorator and guard() context manager (TechSpec §2.1).

The single integration point for users. Flow per call:
    1. Backpressure: acquire a slot (queue / shed per controller policy).
    2. Breaker: if the dependency's breaker refuses, use the fallback
       (emit FALLBACK_USED) or raise CircuitOpenError.
    3. Chaos: invoke the real callable through ChaosInjector.apply().
    4. Record success/failure + latency on the breaker.
    5. Release the backpressure slot (always).

`fallback` may be a plain value or a callable invoked with the original
arguments. `timeout` (sync MVP): a call whose latency exceeds it is recorded
as a *failure* for breaker purposes even though its result is still returned;
true cancellation arrives with the async variant.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from .events import Event, EventType
from .exceptions import CircuitOpenError, QueueTimeoutError, RequestShedError
from .runtime import Runtime, get_runtime

F = TypeVar("F", bound=Callable[..., Any])

#: Sentinel: distinguishes "no fallback configured" from "fallback is None".
_UNSET: Any = object()


def _resolve_fallback(fallback: Any, args: tuple, kwargs: dict) -> Any:
    return fallback(*args, **kwargs) if callable(fallback) else fallback


def _emit_fallback(rt: Runtime, dependency: str, reason: str) -> None:
    rt.emit(Event(
        event_type=EventType.FALLBACK_USED,
        dependency=dependency,
        detail={"reason": reason},
    ))


def guarded(
    dependency: str,
    *,
    timeout: float | None = None,
    fallback: Any = _UNSET,
    queue_timeout_s: float | None = None,
) -> Callable[[F], F]:
    """Decorator wrapping a callable with backpressure + breaker + chaos logic.

    Usage:
        @guarded(dependency="openai_api", timeout=10, fallback="cached")
        def call_llm(prompt: str) -> str: ...
    """

    def decorate(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            rt = get_runtime()
            breaker = rt.breaker(dependency)
            try:
                rt.controller.acquire(queue_timeout_s)
            except (RequestShedError, QueueTimeoutError):
                if fallback is not _UNSET:
                    _emit_fallback(rt, dependency, reason="shed")
                    return _resolve_fallback(fallback, args, kwargs)
                raise
            try:
                if not breaker.try_acquire():
                    if fallback is not _UNSET:
                        _emit_fallback(rt, dependency, reason="breaker_open")
                        return _resolve_fallback(fallback, args, kwargs)
                    raise CircuitOpenError(
                        dependency, breaker.status()["cooldown_remaining_s"]
                    )
                start = rt.clock()
                try:
                    result = rt.chaos.apply(dependency, func, *args, **kwargs)
                except Exception as exc:
                    breaker.record_failure(rt.clock() - start, error=repr(exc))
                    raise
                latency = rt.clock() - start
                if timeout is not None and latency > timeout:
                    breaker.record_failure(
                        latency, error=f"timeout: {latency:.3f}s > {timeout}s"
                    )
                else:
                    breaker.record_success(latency)
                return result
            finally:
                rt.controller.release()

        return wrapper  # type: ignore[return-value]

    return decorate


@contextmanager
def guard(dependency: str, *, queue_timeout_s: float | None = None) -> Iterator[None]:
    """Context-manager form for non-function-shaped call sites.

    Usage:
        with ballast.guard("postgres_db"):
            result = db.query(...)

    The block's wall time is recorded as the call latency; an exception
    escaping the block records a failure. No fallback in this form — an open
    breaker raises CircuitOpenError before the block runs.
    """
    rt = get_runtime()
    breaker = rt.breaker(dependency)
    rt.controller.acquire(queue_timeout_s)
    try:
        if not breaker.try_acquire():
            raise CircuitOpenError(dependency, breaker.status()["cooldown_remaining_s"])
        start = rt.clock()
        try:
            yield
        except Exception as exc:
            breaker.record_failure(rt.clock() - start, error=repr(exc))
            raise
        breaker.record_success(rt.clock() - start)
    finally:
        rt.controller.release()

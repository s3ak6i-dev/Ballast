"""Interceptor SDK: @guarded decorator and guard() context manager (TechSpec §2.1).

The single integration point for users. Flow per call:
    1. Budget: at the hard ceiling, refuse (BudgetExceededError) or force the
       fallback chain, per ``budget_hard_behavior``.
    2. Backpressure: acquire a slot (queue / shed per controller policy).
    3. Breaker: if the dependency's breaker refuses, run the fallback chain.
    4. Soft budget: past the soft ceiling, eligible calls route straight to the
       cheaper model.
    5. Chaos: invoke the real callable through ChaosInjector.apply().
    6. Record success/failure + latency on the breaker; cache the result and
       record its cost when configured.
    7. Release the backpressure slot (always).

Fallback chain (TechSpec §2.4), tried in order when a call can't reach the
real dependency:
    cache (if ``cache_ttl_s`` set and a fresh entry exists)
    → cheaper model (if ``cheaper`` set and the classifier deems it safe)
    → static ``fallback`` value/callable
    → re-raise the original refusal.

`timeout` (sync MVP): a call whose latency exceeds it is recorded as a
*failure* for breaker purposes even though its result is still returned; true
cancellation arrives with the async variant.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from .events import Event, EventType
from .exceptions import (
    BudgetExceededError,
    CircuitOpenError,
    QueueTimeoutError,
    RequestShedError,
)
from .fallback import RulesClassifier
from .runtime import Runtime, get_runtime

F = TypeVar("F", bound=Callable[..., Any])

#: Sentinel: distinguishes "no fallback configured" from "fallback is None".
_UNSET: Any = object()

_default_classifier = RulesClassifier()


def _emit_fallback(rt: Runtime, dependency: str, reason: str, rung: str) -> None:
    rt.emit(Event(
        event_type=EventType.FALLBACK_USED,
        dependency=dependency,
        detail={"reason": reason, "rung": rung},
    ))


def guarded(
    dependency: str,
    *,
    timeout: float | None = None,
    fallback: Any = _UNSET,
    queue_timeout_s: float | None = None,
    cache_ttl_s: float | None = None,
    cheaper: Callable[..., Any] | None = None,
    classifier: Any = None,
    cost_fn: Callable[[Any], float] | None = None,
) -> Callable[[F], F]:
    """Decorator wrapping a callable with backpressure + breaker + chaos logic.

    Fallback rungs (all optional): ``cache_ttl_s`` caches healthy results for
    reuse during outages; ``cheaper`` is a callable (same signature) invoked
    for requests the ``classifier`` deems simple enough; ``fallback`` is the
    static last resort. ``cost_fn(result) -> usd`` reports spend to the budget.

    Usage:
        @guarded(dependency="openai_api", timeout=10,
                 cache_ttl_s=300, cheaper=call_small_llm, fallback="cached")
        def call_llm(prompt: str) -> str: ...
    """
    chosen_classifier = classifier or _default_classifier

    def decorate(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            rt = get_runtime()
            breaker = rt.breaker(dependency)

            def run_chain(reason: str, original: BaseException) -> Any:
                if cache_ttl_s is not None:
                    hit, value = rt.cache.get(dependency, args, kwargs)
                    if hit:
                        _emit_fallback(rt, dependency, reason, rung="cache")
                        return value
                if cheaper is not None and chosen_classifier.eligible_for_cheaper(args, kwargs):
                    _emit_fallback(rt, dependency, reason, rung="cheaper_model")
                    return cheaper(*args, **kwargs)
                if fallback is not _UNSET:
                    _emit_fallback(rt, dependency, reason, rung="static")
                    return fallback(*args, **kwargs) if callable(fallback) else fallback
                raise original

            # 1. Hard budget ceiling — checked before consuming any capacity.
            if rt.budget.hard_exceeded:
                exceeded = BudgetExceededError(rt.budget.spent(), rt.budget.ceiling)
                if rt.config.budget_hard_behavior == "refuse":
                    raise exceeded
                return run_chain("budget_hard", exceeded)

            # 2. Backpressure.
            try:
                rt.controller.acquire(queue_timeout_s)
            except (RequestShedError, QueueTimeoutError) as exc:
                return run_chain("shed", exc)

            try:
                # 3. Breaker.
                if not breaker.try_acquire():
                    return run_chain(
                        "breaker_open",
                        CircuitOpenError(dependency, breaker.status()["cooldown_remaining_s"]),
                    )

                # 4. Soft budget ceiling — downgrade eligible calls proactively.
                if (
                    rt.budget.soft_exceeded
                    and cheaper is not None
                    and chosen_classifier.eligible_for_cheaper(args, kwargs)
                ):
                    _emit_fallback(rt, dependency, "budget_soft", rung="cheaper_model")
                    return cheaper(*args, **kwargs)

                # 5–6. The real call, breaker recording, cache + cost.
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
                    if cache_ttl_s is not None:
                        rt.cache.put(dependency, args, kwargs, result, cache_ttl_s)
                if cost_fn is not None:
                    rt.budget.record(cost_fn(result), dependency)
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

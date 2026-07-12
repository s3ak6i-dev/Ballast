"""Async interceptor contract: async @guarded, aguard(), async backpressure,
async chaos. Mirrors the sync tests where semantics match; diverges where they
deliberately don't (real cancelling timeouts)."""

import asyncio
import time

import pytest

import ballast
from ballast import (
    ChaosError,
    CircuitOpenError,
    EventType,
    QueueTimeoutError,
    RequestShedError,
    aguard,
    guarded,
)

pytestmark = pytest.mark.asyncio


async def wait_until(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class TestAsyncGuarded:
    async def test_happy_path_records_success(self):
        rt = ballast.configure()

        @guarded(dependency="api")
        async def call(x):
            await asyncio.sleep(0.01)
            return x * 2

        assert await call(21) == 42
        window = rt.breaker("api").status()["window"]
        assert window["calls"] == 1 and window["failures"] == 0
        assert rt.controller.status()["in_flight"] == 0

    async def test_exception_records_failure_and_propagates(self):
        rt = ballast.configure()

        @guarded(dependency="api")
        async def call():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await call()
        assert rt.breaker("api").status()["window"]["failures"] == 1
        assert rt.controller.status()["in_flight"] == 0

    async def test_open_breaker_uses_async_fallback(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()
        seen: list = []
        ballast.subscribe(seen.append)

        async def fb(x):
            return f"fb:{x}"

        @guarded(dependency="api", fallback=fb)
        async def call(x):
            raise AssertionError("must not run")

        assert await call(7) == "fb:7"
        fallbacks = [e for e in seen if e.event_type is EventType.FALLBACK_USED]
        assert fallbacks[0].detail == {"reason": "breaker_open", "rung": "static"}

    async def test_open_breaker_without_fallback_raises(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()

        @guarded(dependency="api")
        async def call():
            raise AssertionError("must not run")

        with pytest.raises(CircuitOpenError):
            await call()

    async def test_cache_rung_served_when_open(self):
        rt = ballast.configure()

        @guarded(dependency="api", cache_ttl_s=300)
        async def call(prompt):
            return f"live:{prompt}"

        assert await call("q") == "live:q"  # healthy call populates cache
        rt.breaker("api").force_open()
        assert await call("q") == "live:q"  # cache rung, real coro untouched

    async def test_fallback_on_error_serves_chain(self):
        rt = ballast.configure()

        @guarded(dependency="api", fallback="degraded", fallback_on_error=True)
        async def call():
            raise ValueError("boom")

        assert await call() == "degraded"
        assert rt.breaker("api").status()["window"]["failures"] == 1

    async def test_timeout_truly_cancels(self):
        rt = ballast.configure()
        finished = []

        @guarded(dependency="api", timeout=0.05)
        async def slow():
            await asyncio.sleep(5)
            finished.append(True)
            return "never"

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await slow()
        assert time.monotonic() - start < 1.0  # cancelled, not waited out
        assert not finished
        assert rt.breaker("api").status()["window"]["failures"] == 1
        assert rt.controller.status()["in_flight"] == 0


class TestAsyncBackpressure:
    async def test_fifo_across_async_waiters(self):
        rt = ballast.configure(max_concurrency=1, max_queue_depth=10)
        rt.controller.acquire()  # occupy from the sync side
        order: list[str] = []

        async def worker(name):
            await rt.controller.acquire_async()
            order.append(name)

        task_a = asyncio.create_task(worker("A"))
        assert await wait_until(lambda: rt.controller.status()["queue_depth"] == 1)
        task_b = asyncio.create_task(worker("B"))
        assert await wait_until(lambda: rt.controller.status()["queue_depth"] == 2)

        rt.controller.release()
        assert await wait_until(lambda: order == ["A"])
        rt.controller.release()  # A's slot
        assert await wait_until(lambda: order == ["A", "B"])
        rt.controller.release()  # B's slot
        await asyncio.gather(task_a, task_b)

    async def test_shed_when_queue_full(self):
        rt = ballast.configure(max_concurrency=1, max_queue_depth=0)
        rt.controller.acquire()
        try:
            with pytest.raises(RequestShedError):
                await rt.controller.acquire_async()
        finally:
            rt.controller.release()

    async def test_queue_timeout(self):
        rt = ballast.configure(max_concurrency=1, max_queue_depth=5)
        rt.controller.acquire()
        try:
            with pytest.raises(QueueTimeoutError):
                await rt.controller.acquire_async(timeout_s=0.05)
            assert rt.controller.status()["queue_depth"] == 0  # left cleanly
        finally:
            rt.controller.release()

    async def test_cancelled_waiter_leaves_queue(self):
        rt = ballast.configure(max_concurrency=1, max_queue_depth=5)
        rt.controller.acquire()
        task = asyncio.create_task(rt.controller.acquire_async())
        assert await wait_until(lambda: rt.controller.status()["queue_depth"] == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rt.controller.status()["queue_depth"] == 0
        rt.controller.release()


class TestAguard:
    async def test_success_records_latency(self):
        rt = ballast.configure()
        async with aguard("db"):
            await asyncio.sleep(0.01)
        window = rt.breaker("db").status()["window"]
        assert window["calls"] == 1 and window["failures"] == 0
        assert rt.controller.status()["in_flight"] == 0

    async def test_exception_records_failure(self):
        rt = ballast.configure()
        with pytest.raises(KeyError):
            async with aguard("db"):
                raise KeyError("missing")
        assert rt.breaker("db").status()["window"]["failures"] == 1
        assert rt.controller.status()["in_flight"] == 0

    async def test_open_breaker_raises_before_block(self):
        rt = ballast.configure()
        rt.breaker("db").force_open()
        entered = []
        with pytest.raises(CircuitOpenError):
            async with aguard("db"):
                entered.append(True)
        assert not entered
        assert rt.controller.status()["in_flight"] == 0


class TestAsyncChaos:
    async def test_failure_injection_raises_through_await(self):
        ballast.configure(chaos_enabled=True)
        ballast.chaos.inject_failure("api", rate=1.0)

        @guarded(dependency="api")
        async def call():
            return "ok"

        with pytest.raises(ChaosError):
            await call()

    async def test_latency_injection_uses_async_sleep(self):
        ballast.configure(chaos_enabled=True)
        ballast.chaos.inject_latency("api", multiplier=3.0)

        @guarded(dependency="api")
        async def call():
            await asyncio.sleep(0.05)
            return "ok"

        start = time.monotonic()
        assert await call() == "ok"
        assert time.monotonic() - start >= 0.12  # ~3× the real 0.05s

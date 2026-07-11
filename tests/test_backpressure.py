"""Backpressure controller contract (TechSpec §2.3).

Concurrency cases use real threads with generous join timeouts; capacity math
and shedding are deterministic.
"""

import threading
import time

import pytest

from ballast import EventType, QueueTimeoutError, RequestShedError
from ballast.backpressure import BackpressureController


def make_controller(max_concurrency=2, max_queue_depth=5):
    events: list = []
    controller = BackpressureController(max_concurrency, max_queue_depth, emit=events.append)
    return controller, events


def wait_for(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class TestCapacity:
    def test_acquire_below_capacity_is_immediate(self):
        controller, _ = make_controller(max_concurrency=3)
        for expected in (1, 2, 3):
            controller.acquire()
            assert controller.status()["in_flight"] == expected

    def test_release_frees_slot(self):
        controller, _ = make_controller(max_concurrency=1)
        controller.acquire()
        controller.release()
        assert controller.status()["in_flight"] == 0
        # slot() pairs acquire/release even when the block raises.
        with pytest.raises(ValueError):
            with controller.slot():
                assert controller.status()["in_flight"] == 1
                raise ValueError("boom")
        assert controller.status()["in_flight"] == 0


class TestQueueing:
    def test_at_capacity_queues_fifo(self):
        controller, events = make_controller(max_concurrency=1)
        controller.acquire()  # fill capacity
        order: list[str] = []

        def worker(name: str):
            controller.acquire()
            order.append(name)

        t_a = threading.Thread(target=worker, args=("A",))
        t_a.start()
        assert wait_for(lambda: controller.status()["queue_depth"] == 1)
        t_b = threading.Thread(target=worker, args=("B",))
        t_b.start()
        assert wait_for(lambda: controller.status()["queue_depth"] == 2)

        controller.release()  # wakes A (head of queue), not B
        assert wait_for(lambda: order == ["A"])
        controller.release()
        assert wait_for(lambda: order == ["A", "B"])
        t_a.join(2), t_b.join(2)
        assert EventType.REQUEST_QUEUED in [e.event_type for e in events]

    def test_queue_timeout_raises(self):
        controller, _ = make_controller(max_concurrency=1)
        controller.acquire()
        caught: list[Exception] = []

        def worker():
            try:
                controller.acquire(timeout_s=0.1)
            except Exception as exc:
                caught.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join(2)
        assert not t.is_alive()
        assert len(caught) == 1 and isinstance(caught[0], QueueTimeoutError)
        assert controller.status()["queue_depth"] == 0  # left the queue cleanly


class TestShedding:
    def test_shed_when_queue_full(self):
        controller, events = make_controller(max_concurrency=1, max_queue_depth=1)
        controller.acquire()  # capacity full
        t = threading.Thread(target=controller.acquire)
        t.start()
        assert wait_for(lambda: controller.status()["queue_depth"] == 1)  # queue full

        with pytest.raises(RequestShedError):
            controller.acquire()  # immediate — no waiting
        assert controller.status()["shed_total"] == 1
        assert EventType.REQUEST_SHED in [e.event_type for e in events]

        controller.release()  # let the queued thread through, then clean up
        t.join(2)
        controller.release()

    def test_zero_queue_depth_sheds_at_capacity(self):
        controller, _ = make_controller(max_concurrency=1, max_queue_depth=0)
        controller.acquire()
        with pytest.raises(RequestShedError):
            controller.acquire()


class TestStatus:
    def test_status_snapshot_fields(self):
        controller, _ = make_controller(max_concurrency=7, max_queue_depth=9)
        status = controller.status()
        assert status == {
            "in_flight": 0,
            "queue_depth": 0,
            "shed_total": 0,
            "max_concurrency": 7,
            "max_queue_depth": 9,
        }

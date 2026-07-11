"""Built-in demo swarm (TechSpec §7): the full trip → fallback → recovery loop
with zero external dependencies, driven from the dashboard's "Run demo" button.

Timeline (~30s):
    t+0   30 workers hammer mock_llm (cached + cheaper + static fallbacks,
          cost-metered) and vector_db.
    t+6   chaos injects 85% failure into mock_llm for 7s (time-boxed rule).
    ...   breaker trips → fallback chain serves → half-open → close.
    t+30  workers drain; the swarm can be started again.
"""

from __future__ import annotations

import random
import threading
import time

import ballast
from ballast import guarded

DEMO_DURATION_S = 30.0
WORKERS = 30

#: Short, keyword-free prompts so the difficulty classifier routes them to the
#: cheaper model when budget/breaker pressure calls for it.
PROMPTS = (
    "What is the capital of France?",
    "Summarize this support ticket",
    "Translate hello to Spanish",
    "Name three healthy breakfast ideas",
    "What day of the week is it?",
)


def _mock_llm(prompt: str) -> str:
    time.sleep(random.uniform(0.02, 0.05))
    return f"answer:{abs(hash(prompt)) % 1000}"


def _mock_cheap_llm(prompt: str) -> str:
    time.sleep(random.uniform(0.008, 0.015))
    return f"cheap-answer:{abs(hash(prompt)) % 1000}"


@guarded(
    dependency="mock_llm",
    cache_ttl_s=60.0,
    cheaper=_mock_cheap_llm,
    fallback=lambda prompt: "cached_response",
    # ~$0.25 per 30s demo run: the cost tile ticks visibly, and the soft
    # ceiling ($1.60 of $2/hr) arrives only after several consecutive runs.
    cost_fn=lambda result: 0.00005,
)
def ask_llm(prompt: str) -> str:
    return _mock_llm(prompt)


@guarded(dependency="vector_db", fallback=lambda query: [])
def query_db(query: str) -> list[str]:
    time.sleep(random.uniform(0.005, 0.02))
    return ["doc-1", "doc-2"]


class DemoSwarm:
    """One demo at a time; start() is a no-op (False) while one is running."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._run, name="ballast-demo", daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            start = time.monotonic()

            def worker(worker_id: int) -> None:
                while time.monotonic() - start < DEMO_DURATION_S:
                    try:
                        ask_llm(random.choice(PROMPTS))
                    except Exception:
                        pass  # chaos-injected failures are the point
                    if worker_id % 4 == 0:
                        try:
                            query_db("similar incidents")
                        except Exception:
                            pass
                    time.sleep(random.uniform(0.03, 0.1))

            def director() -> None:
                time.sleep(6.0)
                ballast.chaos.inject_failure("mock_llm", rate=0.85, duration_s=7.0)

            threads = [
                threading.Thread(target=worker, args=(i,), daemon=True)
                for i in range(WORKERS)
            ]
            threads.append(threading.Thread(target=director, daemon=True))
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            self._running = False

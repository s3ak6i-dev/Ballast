"""M2 terminal demo (PRD §8): swarm -> chaos -> trip -> fallback -> recovery.

Flow:
    1. ~50 worker threads hammer a mock API through @guarded.
    2. t+3s : chaos injects 85% failure for 4 seconds (time-boxed rule).
    3. The breaker trips within a handful of failed calls; while OPEN, every
       call is served by the fallback instead of hard-failing.
    4. After the injection expires, cooldown -> half-open probes -> close.
    5. Summary: detection latency, calls served live vs. fallback vs. errored.

Run:  python examples/demo.py        (chaos enabled via configure, no env needed)
"""

import collections
import random
import threading
import time

import ballast
from ballast import EventType, guarded

DURATION_S = 14
WORKERS = 50

START = time.monotonic()


def stamp() -> float:
    return time.monotonic() - START


# --- 1. configure + event printer -------------------------------------------

# latency_multiplier is generous here: 50 threads of sleep jitter make p95
# spiky under contention, and this demo's story is the failure-rate trip.
ballast.configure(
    dependencies={"mock_api": {"cooldown_s": 2.0, "failure_threshold": 0.5,
                               "latency_multiplier": 10.0}},
    max_concurrency=25,
    max_queue_depth=200,
    chaos_enabled=True,
)

LABELS = {
    EventType.BREAKER_TRIP: "!! BREAKER TRIP",
    EventType.BREAKER_HALF_OPEN: "?? HALF-OPEN",
    EventType.BREAKER_CLOSE: "OK BREAKER CLOSE",
    EventType.CHAOS_INJECTED: ">> CHAOS INJECTED",
    EventType.CHAOS_CLEARED: "<< CHAOS CLEARED",
    EventType.REQUEST_SHED: "XX REQUEST SHED",
}

events: list = []
first_fallback_printed = threading.Event()


def printer(event) -> None:
    events.append(event)
    if event.event_type is EventType.FALLBACK_USED:
        if not first_fallback_printed.is_set():
            first_fallback_printed.set()
            print(f"[{stamp():6.2f}s] -> FALLBACK ACTIVE   mock_api (serving cached responses)")
        return
    label = LABELS.get(event.event_type)
    if label:
        detail = {k: v for k, v in event.detail.items() if k in
                  ("reason", "kind", "value", "duration_s", "cooldown_s",
                   "failure_rate", "p95_s", "baseline_s")}
        print(f"[{stamp():6.2f}s] {label:<19} {event.dependency or '-'} {detail}")


ballast.subscribe(printer)

# --- 2. the guarded dependency ------------------------------------------------


def mock_api(task_id: int) -> str:
    time.sleep(random.uniform(0.02, 0.05))
    return "live"


@guarded(dependency="mock_api", fallback=lambda task_id: "cached")
def call_api(task_id: int) -> str:
    return mock_api(task_id)


# --- 3. the swarm ---------------------------------------------------------------

counts: collections.Counter = collections.Counter()
counts_lock = threading.Lock()


def worker(task_id: int) -> None:
    while stamp() < DURATION_S:
        try:
            outcome = call_api(task_id)
        except Exception:
            outcome = "error"
        with counts_lock:
            counts[outcome] += 1
        time.sleep(random.uniform(0.02, 0.08))


def director() -> None:
    time.sleep(3)
    ballast.chaos.inject_failure("mock_api", rate=0.85, duration_s=4.0)


print(f"Ballast demo: {WORKERS} agents on 'mock_api' for {DURATION_S}s "
      f"(chaos: 85% failure at t=3s for 4s)\n")

threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
threads.append(threading.Thread(target=director))
for t in threads:
    t.start()
for t in threads:
    t.join()

# --- 4. summary ---------------------------------------------------------------

by_type: dict = {}
for e in events:
    by_type.setdefault(e.event_type, []).append(e)

total = sum(counts.values())
print("\n--- summary " + "-" * 48)
print(f"calls: {total}  |  live: {counts['live']}  |  "
      f"fallback: {counts['cached']}  |  errors: {counts['error']}")

chaos_events = by_type.get(EventType.CHAOS_INJECTED, [])
chaos_at = chaos_events[0] if chaos_events else None
trip_at = next(
    (e for e in by_type.get(EventType.BREAKER_TRIP, [])
     if chaos_at and e.timestamp >= chaos_at.timestamp),
    None,
)
close_at = next(
    (e for e in by_type.get(EventType.BREAKER_CLOSE, [])
     if trip_at and e.timestamp >= trip_at.timestamp),
    None,
)

if chaos_at and trip_at:
    print(f"detection: breaker tripped {trip_at.timestamp - chaos_at.timestamp:.2f}s "
          f"after chaos began (target: < 2s)")
if trip_at and close_at:
    print(f"recovery:  breaker closed {close_at.timestamp - trip_at.timestamp:.2f}s after the trip")
print(f"trips: {len(by_type.get(EventType.BREAKER_TRIP, []))}  |  "
      f"shed: {len(by_type.get(EventType.REQUEST_SHED, []))}  |  "
      f"fallback events: {len(by_type.get(EventType.FALLBACK_USED, []))}")

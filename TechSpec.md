# Ballast — Technical Specification

**Status:** Draft v0.1
**Companion doc:** ballast-prd.md

---

## 1. Architecture overview

Ballast is an interceptor layer that sits between an agent orchestrator and its external dependencies (LLM APIs, tools, databases). It does not replace the orchestrator — it wraps individual calls.

```
Agent orchestrator (LangGraph / CrewAI / custom)
        │
        ▼
 ┌─────────────────────────────┐
 │   Ballast interceptor SDK    │   ← decorator / context manager
 └─────────────────────────────┘
        │
        ▼
 ┌─────────────────────────────┐
 │  Backpressure controller     │   ← global, tracks in-flight count
 └─────────────────────────────┘
        │
        ▼
 ┌─────────────────────────────┐
 │  Circuit breaker (per dep.)  │   ← tracks rolling health per dependency
 └─────────────────────────────┘
        │
   ┌────┴────┐
   ▼         ▼
Healthy    Fallback router
path       (cheaper model / cache)
   │         │
   └────┬────┘
        ▼
 Real dependency call
        │
        ▼
 Event bus → Dashboard + audit log
```

---

## 2. Components

### 2.1 Interceptor SDK (Python)

The primary integration point. Wraps any callable — a tool function, an LLM API call — with breaker and backpressure logic.

```python
from ballast import guarded

@guarded(dependency="openai_api", timeout=10, fallback="cached_response")
def call_llm(prompt: str) -> str:
    return openai_client.chat.completions.create(...)
```

Internally, `guarded` is a decorator that:
1. Checks backpressure controller — if over capacity, queues or rejects based on priority.
2. Checks circuit breaker state for `dependency` — if open, skips the real call and invokes the fallback.
3. If closed/half-open, calls the wrapped function, records success/failure/latency.
4. Emits an event to the event bus regardless of outcome.

A context-manager form is also supported for non-function-shaped call sites:

```python
with ballast.guard("postgres_db"):
    result = db.query(...)
```

### 2.2 Circuit breaker

State machine per named dependency:

- **Closed** — normal operation. Maintains a rolling window (default: last 20 calls or 30 seconds, whichever is smaller) of success/failure/latency.
- **Open** — trips when failure rate exceeds threshold (default 50%) or p95 latency exceeds N× baseline (default 3×) within the window. While open, all calls fail fast (or go to fallback) without touching the real dependency.
- **Half-open** — after a cooldown period (default 10s, exponential backoff on repeated trips), allows a small number of trial calls through. Success → close. Failure → reopen, cooldown increases.

Thresholds are configurable per dependency via a config object or YAML file (P3 stretch goal for the latter).

State storage:
- **MVP:** in-memory, single-process (fine for the demo scenario and most single-instance deployments).
- **P3 stretch:** Redis-backed for multi-process/distributed deployments — each breaker's state (window of recent calls, current state, cooldown timer) stored as a small hash, TTL-based expiry on the rolling window entries.

### 2.3 Backpressure controller

Global (not per-dependency) tracker of total in-flight requests and queue depth.

- Configurable max concurrency (default: 100 in-flight).
- When at capacity, new requests are queued (FIFO by default; priority queue as a later enhancement) rather than rejected outright.
- Queue depth itself has a ceiling — beyond that, lowest-priority requests are shed (rejected) to protect the system.
- Emits queue depth and shed-request events to the event bus for dashboard visibility.

### 2.4 Fallback router

Triggered when:
- The circuit breaker for a dependency is open, **or**
- The current run/session is within a configurable margin of its cost budget.

Fallback strategies (configurable per dependency, checked in order):
1. Return a cached response if one exists and is fresh enough (TTL-based cache, keyed on request hash).
2. Reroute to a cheaper/smaller model (for LLM calls specifically) — requires a difficulty classifier (2.5) to decide if this is safe.
3. Return a degraded static response (a `fallback` value passed at decoration time) if no smarter fallback is possible.
4. Fail explicitly with a clear error, only if none of the above apply.

### 2.5 Task-difficulty classifier (P1)

For MVP, this is **rules-based**, not a trained model — faster to ship, and more explainable in a demo/README than a black box:

- Prompt length under N tokens → eligible for smaller model.
- No structured-output requirement / no multi-step reasoning keywords detected → eligible for smaller model.
- Otherwise → stays on the original (larger) model, cost ceiling permitting.

A pluggable interface allows swapping in an actual classifier later without changing the router's calling convention.

### 2.6 Chaos injector

A separate, explicitly-enabled mode (`BALLAST_CHAOS=1` or a config flag) that wraps the *real* dependency call with deliberate fault injection, so the breaker and backpressure logic being tested is the real production code path, not a mock:

- `inject_latency(dependency, multiplier)` — artificially slows responses.
- `inject_failure(dependency, rate)` — randomly raises exceptions at the given rate.
- `inject_corruption(dependency, rate)` — returns malformed/truncated responses.
- Presets bundle these into named scenarios (e.g. `chaos.scenario("api_outage")` = 100% failure for 30 seconds then recovery).

### 2.7 Event bus + dashboard

- **Event bus (MVP):** a simple in-process pub/sub (Python's own event queue is enough for a single instance); P3 stretch is Redis pub/sub for multi-instance setups.
- **Dashboard (P2):** a small web app (FastAPI backend + a lightweight frontend — React or plain HTML/JS, matching your existing dark/orange design system) subscribing to the event bus and rendering:
  - Live breaker state per dependency (closed/open/half-open, with color coding).
  - In-flight request count and queue depth over time.
  - Running cost counter with a budget line.
  - Scrolling event log (trips, reroutes, shed requests).

---

## 3. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Interceptor SDK | Python 3.11+ | Matches the primary agent-framework ecosystem (LangGraph, CrewAI, AutoGen are all Python-first) |
| State storage (MVP) | In-memory | Zero setup for the demo and single-instance use |
| State storage (stretch) | Redis | Fast, TTL support, natural fit for rolling windows and pub/sub |
| Event log (MVP) | SQLite | Zero-config, file-based, good enough for audit trail at MVP scale |
| Dashboard backend | FastAPI | Lightweight, async-friendly, easy WebSocket support for live updates |
| Dashboard frontend | React + minimal CSS (or plain HTML/JS for speed) | Matches your existing dark/`#E8602C` design language |
| Packaging | Docker + docker-compose | Matches the "4-minute setup" bar set by comparable projects |

---

## 4. Data model (event log)

Minimal schema, SQLite for MVP:

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,      -- breaker_trip | breaker_close | fallback_used | request_shed | chaos_injected
    dependency TEXT,
    detail TEXT,                    -- JSON blob: latency, error message, cost delta, etc.
    session_id TEXT
);
```

---

## 5. API surface (SDK)

```python
# Configuration
ballast.configure(
    dependencies={
        "openai_api": {"failure_threshold": 0.5, "latency_multiplier": 3, "cooldown_s": 10},
        "postgres_db": {"failure_threshold": 0.3, "latency_multiplier": 2, "cooldown_s": 5},
    },
    max_concurrency=100,
    max_queue_depth=500,
    budget_usd_per_hour=10.0,
)

# Decorator usage
@guarded(dependency="openai_api", fallback=cached_or_cheaper)
def call_llm(prompt): ...

# Context manager usage
with ballast.guard("postgres_db"):
    ...

# Chaos control (test/dev only)
ballast.chaos.inject_failure("openai_api", rate=0.8)
ballast.chaos.scenario("api_outage").run(duration_s=30)

# Introspection
ballast.status()  # returns current breaker states, queue depth, cost burn
```

---

## 6. Testing strategy

- **Unit tests** for the breaker state machine — verify transitions (closed→open, open→half-open, half-open→closed/open) under synthetic call sequences.
- **Load tests** for the backpressure controller — verify queueing and shedding behavior under simulated concurrency spikes.
- **Chaos-driven integration tests** — the same chaos scenarios used for demos double as CI tests: assert the breaker trips within N calls of a chaos scenario starting, and recovers within one cooldown cycle of it ending.

---

## 7. Deployment

```bash
git clone <repo>
cd ballast
docker compose up -d --build
```

Defaults on first boot:
- Dashboard on `localhost:8080`.
- SQLite event log in a mounted volume.
- No external dependencies required to see the demo scenario — a mock dependency and chaos scenario ship built-in so a new user can see the full trip → fallback → recovery loop within minutes of cloning, with zero external API keys required.

---

## 8. Security considerations (v1 scope)

- Ballast does not proxy or inspect the *content* of requests/responses — it only tracks metadata (success/failure/latency) for breaker logic. This keeps the security surface small and avoids overlapping with the (separate, already-crowded) MCP-security tooling space.
- Dashboard should bind to localhost by default, same posture as comparable self-hosted tools — no auth in MVP, but the README should say so explicitly rather than implying more security than exists.

---

## 9. Build order (maps to PRD milestones)

1. Circuit breaker state machine + unit tests (no I/O, pure logic — fastest to get right).
2. Backpressure controller + unit tests.
3. Interceptor SDK wrapping both, tested against a mock dependency.
4. Chaos injector + first working demo scenario in the terminal (no dashboard yet — just printed state transitions).
5. Fallback router + budget tracking.
6. Event bus + SQLite logging.
7. Dashboard (FastAPI + frontend) subscribing to the event bus.
8. Docker packaging + README + demo video.
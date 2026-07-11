# Ballast

**A resilience and cost-control plane for multi-agent AI systems.**

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-E8602C)
![Tests](https://img.shields.io/badge/tests-81%20passing-53a868)
![License: MIT](https://img.shields.io/badge/license-MIT-9c9187)
![Status](https://img.shields.io/badge/status-M4%20complete-E8602C)

Ballast sits between your agent orchestrator (LangGraph, CrewAI, AutoGen, custom loops) and everything it depends on — LLM APIs, tools, databases. It detects overload and failure in real time, absorbs traffic spikes with backpressure, and falls back to cheaper alternatives instead of hard-failing. A chaos-injection mode lets you **prove** your pipeline survives an outage instead of hoping it does — and a live dashboard shows the whole story as it happens.

```
Agent orchestrator (LangGraph / CrewAI / custom)
        │
        ▼
 ┌─────────────────────────────┐
 │   Ballast interceptor SDK   │   ← @guarded decorator / guard() context manager
 └─────────────────────────────┘
        │
        ▼
 ┌─────────────────────────────┐
 │  Backpressure controller    │   ← global; FIFO queue + shedding
 └─────────────────────────────┘
        │
        ▼
 ┌─────────────────────────────┐
 │  Circuit breaker (per dep)  │   ← rolling health window per dependency
 └─────────────────────────────┘
        │
   ┌────┴─────┐
   ▼          ▼
Healthy    Fallback router
path       cache → cheaper model → static
   │          │
   └────┬─────┘
        ▼
 Real dependency call  ←──  chaos injector (opt-in fault injection)
        │
        ▼
 Event bus → dashboard · SQLite audit log
```

---

## Why Ballast exists

Multi-agent systems ship to production with almost none of the hardening that microservices learned over 15 years:

- **Cascading failure** — one slow or failing tool piles up retries until the whole pipeline dies.
- **No backpressure** — a fan-out of 50 agent spawns hammers a downstream API with nothing throttling it.
- **No graceful degradation** — a dependency outage either hard-fails the run or silently corrupts it.
- **Uncontrolled cost** — agent swarms burn token budgets with no cost-aware routing.
- **Unproven resilience** — nobody can test whether the pipeline actually survives an outage before one happens.

Ballast ports the proven answers — circuit breakers, backpressure, graceful degradation, chaos engineering — to the agent world, as a drop-in layer that wraps your existing calls. No orchestrator rewrite.

## Features

| | |
|---|---|
| 🔌 **Drop-in interceptor** | One decorator (`@guarded`) or context manager (`guard()`) around any call — tool, LLM API, database |
| ⚡ **Per-dependency circuit breakers** | Rolling window of success/failure/latency; trips on failure rate *or* p95 latency vs. a learned healthy baseline; half-open probing with exponential-backoff cooldowns |
| 🚦 **Global backpressure** | Concurrency ceiling with strict-FIFO queueing; sheds excess load instead of collapsing |
| 🪜 **Fallback chain** | cache → cheaper model → static value → explicit error, in that order, per dependency |
| 🧮 **Cost-aware routing** | Rolling-hour budget with soft/hard ceilings; past the soft ceiling, simple requests route to a cheaper model automatically (rules-based difficulty classifier, pluggable) |
| 💥 **Chaos injection** | Time-boxed failure/latency/corruption rules against the *real* code path — demos double as CI tests |
| 📊 **Live dashboard** | Breaker states, in-flight/queue charts, cost burn vs. budget, coalescing event feed, one-click demo — over WebSocket |
| 🗃️ **Audit trail** | Every trip, reroute, shed, and chaos event logged to SQLite |

## Quickstart

```bash
pip install -e .            # core: zero runtime dependencies
pip install -e .[dashboard] # + FastAPI dashboard
```

Wrap a call and configure your thresholds:

```python
import ballast
from ballast import guarded

ballast.configure(
    dependencies={
        "openai_api":  {"failure_threshold": 0.5, "latency_multiplier": 3, "cooldown_s": 10},
        "postgres_db": {"failure_threshold": 0.3, "latency_multiplier": 2, "cooldown_s": 5},
    },
    max_concurrency=100,        # global in-flight ceiling
    max_queue_depth=500,        # waiting requests beyond it queue up to here
    budget_usd_per_hour=10.0,   # soft ceiling at 80%, hard at 100%
)

@guarded(
    dependency="openai_api",
    timeout=10,
    cache_ttl_s=300,                       # rung 1: serve recent identical answers
    cheaper=call_small_model,              # rung 2: downgrade simple prompts
    fallback="Service degraded — cached summary unavailable.",  # rung 3: static
    cost_fn=lambda r: r.usage_cost_usd,    # meter spend against the budget
)
def call_llm(prompt: str) -> str:
    return client.chat.completions.create(...)

with ballast.guard("postgres_db"):          # context-manager form
    rows = db.query(...)
```

When `openai_api` degrades, its breaker trips within a handful of calls; while it's open, requests are served by the fallback chain instead of erroring, and the breaker probes its way back to closed once the API recovers. Nothing else in your pipeline changes.

### See it break (on purpose)

```bash
python examples/demo.py
```

50 concurrent workers, an 85%-failure chaos injection at t+3s, and a printed play-by-play:

```
[  3.03s] >> CHAOS INJECTED   mock_api {'kind': 'failure', 'value': 0.85, 'duration_s': 4.0}
[  3.06s] !! BREAKER TRIP     mock_api {'reason': 'failure_rate', 'failure_rate': 0.55}
[  3.06s] -> FALLBACK ACTIVE  mock_api (serving cached responses)
[  5.06s] ?? HALF-OPEN        mock_api
[  5.06s] !! BREAKER TRIP     mock_api {'reason': 'probe_failure', 'cooldown_s': 4.0}
[  9.06s] ?? HALF-OPEN        mock_api
[  9.06s] << CHAOS CLEARED    mock_api {'reason': 'expired'}
[  9.11s] OK BREAKER CLOSE    mock_api {'reason': 'probes_succeeded'}

calls: 10,466 | live: 4,553 | fallback: 5,901 | errors: 12
detection: breaker tripped 0.02s after chaos began (target: < 2s)
```

## The dashboard

```bash
pip install -e .[dashboard]
python -m ballast.dashboard        # → http://127.0.0.1:8080
```

Click **Run demo scenario** and watch a live incident: breaker cards flip CLOSED → OPEN → HALF-OPEN with cooldown countdowns, the fallback badge lights up, traffic and cost charts stream, and the chaos banner counts down with a one-click **Stop now**. The demo ships in-process — no API keys, no external services.

| Surface | What it does |
|---|---|
| `GET /` | The live Overview UI |
| `WS /ws` | 500 ms ticks: full status snapshot + events since last tick |
| `GET /api/status` | Breaker states, backpressure, budget, cache, active chaos |
| `GET /api/events` | SQLite event log with `event_type` / `dependency` / `limit` filters |
| `POST /api/demo/start` | Launch the built-in 30-worker demo swarm |
| `POST /api/chaos/run` | Run a preset scenario `{preset, dependency, duration_s}` |
| `POST /api/chaos/clear` | Stop all chaos immediately |

The dashboard binds to localhost and has **no authentication** — it is a local operations view, not an internet-facing service.

## Chaos engineering

Chaos is **opt-in twice**: it only functions when `BALLAST_CHAOS=1` is set or `configure(chaos_enabled=True)` is passed, and every injection is time-boxed (hard cap: 600 s — no indefinite faults). Faults wrap the *real* dependency call, so the breaker logic being exercised is the production code path, not a mock.

```python
ballast.chaos.inject_failure("openai_api", rate=0.8, duration_s=30)   # random ChaosError
ballast.chaos.inject_latency("openai_api", multiplier=3.0)            # 3× slowdown
ballast.chaos.inject_corruption("openai_api", rate=0.5)               # mangled responses
ballast.chaos.scenario("api_outage").run("openai_api", duration_s=30) # preset bundle
ballast.chaos.clear()                                                 # stop everything
```

Presets: `api_outage` (100% failure), `slow_api` (3× latency), `flaky_api` (50% failure). Injected failures raise a distinct `ChaosError`, so logs and tests can never confuse injected faults with real ones.

## Configuration reference

Per-dependency breaker settings (all optional — shown with defaults):

| Setting | Default | Meaning |
|---|---|---|
| `failure_threshold` | `0.5` | Trip when the window's failure rate exceeds this |
| `latency_multiplier` | `3.0` | Trip when window p95 latency exceeds this × the healthy baseline |
| `cooldown_s` | `10.0` | Open → half-open wait; doubles on each consecutive trip |
| `max_cooldown_s` | `300.0` | Backoff ceiling |
| `window_max_calls` | `20` | Rolling window size (calls) |
| `window_max_age_s` | `30.0` | Rolling window size (seconds) — whichever trims first |
| `min_calls` | `5` | Calls required in the window before trip rules evaluate |
| `half_open_probes` | `3` | Trial calls that must all succeed to close |

Global settings: `max_concurrency` (100), `max_queue_depth` (500), `budget_usd_per_hour` (None = off), `budget_soft_margin` (0.8), `budget_hard_behavior` (`"downgrade"` or `"refuse"`), `chaos_enabled` (False).

## Event vocabulary

Everything observable flows through one in-process event bus (SQLite log and dashboard are subscribers):

`breaker_trip` · `breaker_half_open` · `breaker_close` · `fallback_used` (with the rung that served: `cache` / `cheaper_model` / `static`) · `request_queued` · `request_shed` · `chaos_injected` · `chaos_cleared` · `budget_soft_ceiling` · `budget_hard_ceiling` · `manual_override`

Subscribe from your own code: `unsubscribe = ballast.subscribe(lambda event: ...)`.

## Project layout

```
src/ballast/
  breaker.py       circuit breaker: closed/open/half-open, learned latency baseline
  backpressure.py  global concurrency gate: strict-FIFO queue + shedding
  interceptor.py   @guarded / guard() — the integration point
  fallback.py      TTL response cache + rules-based difficulty classifier
  budget.py        rolling-hour spend tracker, soft/hard ceilings
  chaos.py         time-boxed fault rules + scenario presets
  events.py        Event model + in-process pub/sub bus
  eventlog.py      SQLite audit log (attach to any runtime)
  runtime.py       configure() / status() / the global runtime
  dashboard/       FastAPI backend + live web UI + built-in demo swarm
tests/             81 tests; breaker logic is FakeClock-driven (no sleeps)
examples/demo.py   terminal demo: swarm → chaos → trip → fallback → recovery
```

Design docs in the repo root: [`PRD.md`](PRD.md) (product requirements), [`TechSpec.md`](TechSpec.md) (architecture), [`UISpec.md`](UISpec.md) (dashboard screens & components).

## Development

```bash
pip install -e .[dev,dashboard]
pytest                      # 81 tests, ~2s
python examples/demo.py     # terminal demo
python -m ballast.dashboard # live dashboard
```

Design principles worth knowing before contributing:

- **All breaker timing is injectable** — logic takes a `Clock` callable; tests drive a `FakeClock` and never sleep.
- **Events are published outside locks** — a slow or reentrant subscriber can't deadlock the call path.
- **The bus swallows subscriber exceptions** — observability failures must never take down a guarded call.
- **Chaos rules expire on their own** — an abandoned experiment can't fault a dependency forever.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M1 | Circuit breaker + backpressure, unit tested | ✅ |
| M2 | Chaos injection + terminal demo | ✅ |
| M3 | Fallback routing + budget tracking | ✅ |
| M4 | Live dashboard + SQLite event log | ✅ |
| M5 | Docker packaging, demo video, launch | 🔜 |
| Later | Redis-backed shared state · async/`await` interceptor · Node/TS SDK · YAML policy config | — |

## Security posture (v1)

Ballast tracks call *metadata* (success/failure/latency/cost) — it never inspects or proxies request/response content. The dashboard binds to localhost with no auth; put it behind your own proxy if you expose it. Chaos injection cannot be enabled from the dashboard UI — only via environment/config.

## License

MIT — see [LICENSE](LICENSE).

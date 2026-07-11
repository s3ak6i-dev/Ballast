# Ballast

*A resilience and cost-control plane for multi-agent AI systems.*

Ballast sits between your agent orchestrator (LangGraph, CrewAI, AutoGen, custom loops) and everything it depends on — LLM APIs, tools, databases — detecting overload and failure in real time, absorbing traffic spikes via backpressure, and falling back to cheaper alternatives instead of hard-failing. A chaos-injection mode lets you *prove* resilience instead of claiming it.

**Status:** M1–M2 complete. Circuit breaker, backpressure controller, interceptor SDK, and chaos injector are implemented and tested (49 tests); the terminal demo scenario runs end-to-end (`python examples/demo.py`). Next: fallback router + budget tracking (M3), then the dashboard (M4). See `PRD.md`, `TechSpec.md`, and `UISpec.md` for the full design.

## Usage

```python
import ballast
from ballast import guarded

ballast.configure(
    dependencies={
        "openai_api": {"failure_threshold": 0.5, "latency_multiplier": 3, "cooldown_s": 10},
    },
    max_concurrency=100,
    max_queue_depth=500,
)

@guarded(dependency="openai_api", timeout=10, fallback="cached_response")
def call_llm(prompt: str) -> str:
    ...

with ballast.guard("postgres_db"):
    result = db.query(...)
```

## Layout

```
src/ballast/
  config.py        # dataclasses + defaults (from TechSpec §2.2/§5)
  events.py        # Event + in-process EventBus (pub/sub)
  exceptions.py    # CircuitOpenError, RequestShedError, ...
  breaker.py       # per-dependency circuit breaker (closed/open/half-open)
  backpressure.py  # global backpressure controller (FIFO queue + shedding)
  chaos.py         # chaos injector + scenario presets (time-boxed faults)
  interceptor.py   # @guarded decorator / guard() ctx mgr
  runtime.py       # global runtime: configure(), status()
tests/             # 49 unit tests (FakeClock-driven; no sleeps in breaker tests)
examples/demo.py   # M2 terminal demo: swarm -> chaos -> trip -> fallback -> recovery
```

## Development

```bash
pip install -e .[dev]
pytest
```

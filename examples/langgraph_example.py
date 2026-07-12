"""Ballast + LangGraph: the reference integration (PRD §10).

A two-node LangGraph pipeline (retrieve -> generate) whose dependency calls
are wrapped with @guarded. Mid-run, chaos kills the LLM API completely — and
the graph keeps completing every single invocation, served by the fallback
chain (cache -> cheaper model -> static), then recovers on its own.

The integration is exactly two decorators. The graph itself is untouched —
that's the point: Ballast wraps *calls*, not orchestrators.

Run:
    pip install ballast-agents langgraph
    python examples/langgraph_example.py

No API keys required — the LLM and vector DB are mocks with realistic
latency. To use real providers, replace the mock bodies and keep the
decorators; nothing else changes.
"""

import random
import threading
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import ballast
from ballast import EventType, guarded

# --------------------------------------------------------------------------
# 1. Configure Ballast: thresholds per dependency, plus a cost budget.
# --------------------------------------------------------------------------

ballast.configure(
    dependencies={
        "llm_api":   {"failure_threshold": 0.5, "latency_multiplier": 10, "cooldown_s": 2.0},
        "vector_db": {"failure_threshold": 0.3, "latency_multiplier": 10, "cooldown_s": 2.0},
    },
    max_concurrency=25,
    budget_usd_per_hour=5.0,
    chaos_enabled=True,  # dev only — lets this script run its own chaos drill
)

# --------------------------------------------------------------------------
# 2. The dependencies, guarded. Swap the bodies for real clients; the
#    decorators (the entire integration) stay the same.
# --------------------------------------------------------------------------


def _small_llm(prompt: str, docs: list[str]) -> str:
    time.sleep(random.uniform(0.005, 0.015))
    return f"[cheap-model] short answer about: {prompt[:40]}"


@guarded(
    dependency="llm_api",
    timeout=5.0,
    cache_ttl_s=120.0,                       # rung 1: recent identical answers
    cheaper=_small_llm,                      # rung 2: simple prompts downgrade
    fallback=lambda prompt, docs: "[degraded] Sorry — answers are briefly limited.",
    cost_fn=lambda result: 0.002,            # meter spend against the budget
    fallback_on_error=True,                  # serve the chain even in the
)                                            # breaker's detection window
def call_llm(prompt: str, docs: list[str]) -> str:
    # Real life: return openai_client.chat.completions.create(...)
    time.sleep(random.uniform(0.02, 0.05))
    return f"[live-model] grounded answer about: {prompt[:40]} ({len(docs)} docs)"


@guarded(dependency="vector_db", timeout=2.0, fallback=lambda query: [])
def search_vectors(query: str) -> list[str]:
    # Real life: return vector_store.similarity_search(query)
    time.sleep(random.uniform(0.005, 0.02))
    return [f"doc about {query[:24]}", "related runbook", "old incident report"]


# --------------------------------------------------------------------------
# 3. A completely ordinary LangGraph — no Ballast imports in the graph.
# --------------------------------------------------------------------------


class State(TypedDict):
    question: str
    docs: list[str]
    answer: str


def retrieve(state: State) -> dict:
    return {"docs": search_vectors(state["question"])}


def generate(state: State) -> dict:
    return {"answer": call_llm(state["question"], state["docs"])}


graph = StateGraph(State)
graph.add_node("retrieve", retrieve)
graph.add_node("generate", generate)
graph.add_edge(START, "retrieve")
graph.add_edge("retrieve", "generate")
graph.add_edge("generate", END)
pipeline = graph.compile()

# --------------------------------------------------------------------------
# 4. The chaos drill: run the graph continuously, kill the LLM mid-run,
#    and watch every invocation still complete.
# --------------------------------------------------------------------------

QUESTIONS = (
    "What is our refund policy?",
    "Summarize yesterday's incident",
    "Which region has the most signups?",
    "How do I rotate the API keys?",
)

START_T = time.monotonic()
stamp = lambda: time.monotonic() - START_T  # noqa: E731

interesting = {
    EventType.BREAKER_TRIP: "!! BREAKER TRIP",
    EventType.BREAKER_HALF_OPEN: "?? HALF-OPEN",
    EventType.BREAKER_CLOSE: "OK BREAKER CLOSE",
    EventType.CHAOS_INJECTED: ">> CHAOS INJECTED",
    EventType.CHAOS_CLEARED: "<< CHAOS CLEARED",
}
rung_counts: dict[str, int] = {}


def printer(event) -> None:
    if event.event_type is EventType.FALLBACK_USED:
        rung = event.detail.get("rung", "?")
        rung_counts[rung] = rung_counts.get(rung, 0) + 1
        return
    label = interesting.get(event.event_type)
    if label:
        print(f"[{stamp():5.2f}s] {label:<18} {event.dependency}  {event.detail}")


ballast.subscribe(printer)


def director() -> None:
    time.sleep(4.0)
    ballast.chaos.inject_failure("llm_api", rate=1.0, duration_s=4.0)  # total outage


threading.Thread(target=director, daemon=True).start()

print("LangGraph pipeline (retrieve -> generate) under Ballast.")
print("Total LLM outage injected at t=4s for 4s. Watch the graph keep answering.\n")

completed = 0
failed = 0
while stamp() < 14.0:
    question = random.choice(QUESTIONS)
    try:
        pipeline.invoke({"question": question, "docs": [], "answer": ""})
        completed += 1
    except Exception as exc:  # would mean the outage reached the graph
        failed += 1
        print(f"[{stamp():5.2f}s] GRAPH FAILURE: {exc!r}")
    time.sleep(0.05)

fallbacks_served = sum(rung_counts.values())
print("\n--- summary " + "-" * 48)
print(f"graph invocations: {completed + failed}  |  completed: {completed}  |  failed: {failed}")
print(f"answers served by the fallback chain: {fallbacks_served} "
      f"({', '.join(f'{k} x{v}' for k, v in sorted(rung_counts.items())) or 'none'})")
print(f"llm_api breaker now: {ballast.status()['dependencies']['llm_api']['state']}")
if failed == 0:
    print("\nEvery invocation completed through a total LLM outage. That's the pitch.")

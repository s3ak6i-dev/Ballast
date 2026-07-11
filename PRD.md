# Ballast — Product Requirements Document

*A resilience and cost-control plane for multi-agent AI systems*

**Status:** Draft v0.1
**Owner:** Surya
**Working name:** Ballast (placeholder — open to change)

---

## 1. One-liner

Ballast sits between your agent orchestrator and everything it depends on (LLM APIs, tools, databases), detecting overload and failure in real time, absorbing traffic spikes gracefully, and falling back to cheaper alternatives instead of hard-failing — with a chaos-testing mode to prove it actually works.

---

## 2. Problem statement

Multi-agent AI systems are being deployed everywhere right now with almost no production-hardening. Teams building on LangGraph, CrewAI, AutoGen, or raw agent loops routinely hit failure modes that have well-understood solutions in traditional distributed systems, but almost nobody has ported those solutions to the agent world:

- **Cascading failure:** one slow or failing tool/API causes agents to pile up retries, exhausting resources and taking down the whole pipeline.
- **No backpressure:** a burst of agent spawns (e.g. a fan-out task) can overwhelm downstream APIs with no throttling mechanism.
- **No graceful degradation:** when a dependency fails, systems either hard-fail the whole run or silently produce degraded output with no visibility.
- **Uncontrolled cost:** agent swarms can burn through token budgets fast with no cost-aware fallback logic.
- **No way to test resilience:** there's no standard way to verify an agent pipeline actually survives a dependency outage before it happens in production.

These are the same problems distributed systems engineering solved for microservices over the last 15 years (circuit breakers, backpressure, chaos engineering) — but the agent ecosystem hasn't caught up.

---

## 3. Target users

- **Primary:** Developers building production multi-agent systems (internal tools, agent-based SaaS products) who are past the prototype stage and need reliability guarantees.
- **Secondary:** Teams evaluating agent frameworks who want observability into swarm health.
- **Tertiary (for visibility):** Open-source contributors and the broader agent-tooling community — this is also a portfolio/credibility project.

---

## 4. Goals

- Provide a drop-in interceptor layer that works with any agent orchestrator without requiring a rewrite.
- Detect dependency failure and overload automatically, using circuit-breaker semantics.
- Absorb load spikes via backpressure instead of crashing or timing out.
- Offer cost-aware fallback routing so degraded operation is cheap, not just possible.
- Ship a chaos-injection mode so users can *prove* resilience, not just claim it.
- Provide a live dashboard showing swarm health, breaker states, and cost burn.

### Non-goals (v1)

- Not a full agent orchestration framework — Ballast wraps existing orchestrators, it doesn't replace them.
- Not a security/permissions layer (that's a separate concern — no tool-call authorization or sandboxing in v1).
- Not building a custom LLM — routing decisions use existing APIs/models, no model training.
- Not targeting non-Python ecosystems in v1 (Node/TS support is a later milestone).

---

## 5. Core features

### P0 — MVP (needed for first working demo)

| Feature | Description |
|---|---|
| Per-dependency circuit breaker | Tracks rolling success/failure/latency per named dependency; trips to open state past threshold; half-open probing on cooldown |
| Backpressure controller | Tracks in-flight request count and queue depth; throttles new agent spawns past a configurable ceiling |
| Python interceptor SDK | A decorator/context-manager wrapping any function call (tool call, API call) with breaker + backpressure logic |
| Chaos injection mode | CLI flag to deliberately inject latency, drop responses, or return errors for a named dependency |
| Basic event log | Every breaker trip, reroute, and throttle event logged to a local store (SQLite for MVP) |

### P1 — Cost-aware routing

| Feature | Description |
|---|---|
| Fallback router | When a breaker trips or budget nears its ceiling, reroute to a cheaper model or cached response instead of failing |
| Task-difficulty classifier | Lightweight heuristic or small model to decide if a request can be handled by a cheaper model |
| Budget tracking | Per-run and per-timeframe token/cost budgets with soft and hard ceilings |

### P2 — Observability & polish

| Feature | Description |
|---|---|
| Live dashboard | Web UI showing real-time breaker states, in-flight requests, cost burn, and event stream |
| Docker quick-start | `docker compose up` gets a working instance running in under 5 minutes |
| Chaos scenario presets | Pre-built chaos scripts ("kill the DB for 30s", "slow down the LLM API by 3x") for demoing |

### P3 — Stretch goals

| Feature | Description |
|---|---|
| Redis-backed shared state | For multi-process/multi-instance deployments |
| Node.js/TypeScript SDK | Port of the interceptor for the JS agent ecosystem |
| Policy-as-config | YAML-defined breaker thresholds, budgets, and fallback rules instead of code |

---

## 6. Key user stories

1. *As a developer*, I want to wrap my existing tool calls with a single decorator so I don't have to rewrite my orchestrator to get resilience.
2. *As a developer*, I want to see my dependency breaker trip and recover automatically when an API degrades, without my whole pipeline crashing.
3. *As a developer*, I want to deliberately kill a dependency in a test environment and watch my system detect and route around it.
4. *As a developer*, I want to set a cost ceiling and have the system automatically downgrade to cheaper models rather than blowing my budget.
5. *As a team lead*, I want a live dashboard so I can see the health of my agent swarm at a glance.

---

## 7. Success metrics

- **Technical:** breaker correctly trips within N failed calls (configurable, default 5) and recovers within one cooldown cycle after the dependency heals.
- **Demo:** a chaos scenario (kill dependency mid-run) is detected and routed around within under 2 seconds, visible live on the dashboard.
- **Adoption (stretch, if open-sourced):** GitHub stars, issues/PRs, and any real integrations reported by users.
- **Portfolio:** a working, demoable video and README that clearly show cause → detection → recovery.

---

## 8. Demo scenario (the thing to build toward first)

1. Spin up a simulated swarm of ~50 concurrent agent tasks calling a mock downstream API.
2. Trigger a chaos event: the mock API starts failing 80% of calls.
3. Dashboard shows the circuit breaker trip in real time.
4. Show the system automatically rerouting to a fallback (cached response / cheaper model).
5. Restore the mock API to healthy; show the breaker half-open, then close automatically.
6. Overlay a running cost counter throughout to show the fallback saved money during the incident.

This single scenario should be buildable as a ~90-second demo video and is the anchor for the whole v1.

---

## 9. Rough milestones

| Milestone | Scope |
|---|---|
| M1 | Circuit breaker + backpressure controller working locally, unit tested |
| M2 | Chaos injection mode + demo scenario running end-to-end in terminal |
| M3 | Fallback routing + budget tracking |
| M4 | Dashboard (live view of M1–M3 in the browser) |
| M5 | Docker packaging, README, demo video, launch |

---

## 10. Open questions

- Final project name (Ballast is a placeholder).
- Should the classifier for task difficulty be rules-based or a small trained model for v1? (Recommend rules-based first — faster to ship, easier to explain/trust.)
- Which agent framework to target first for the reference integration — LangGraph is the most likely candidate given current popularity.
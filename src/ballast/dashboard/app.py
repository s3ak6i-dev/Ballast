"""FastAPI backend for the Ballast dashboard (TechSpec §2.7, UISpec).

Surface:
    GET  /               the single-page UI
    GET  /api/status     runtime snapshot (breakers, backpressure, budget, chaos)
    GET  /api/events     event-log query (filters: event_type, dependency, limit)
    POST /api/demo/start built-in demo swarm (idempotent while running)
    POST /api/chaos/run  {preset, dependency, duration_s} — preset scenario
    POST /api/chaos/clear
    WS   /ws             500ms ticks: {status, events since last tick}

Binds to localhost by default; no auth (TechSpec §8 posture — say so, don't
imply more security than exists).

Note: no ``from __future__ import annotations`` here — FastAPI must resolve
the locally-defined request models at runtime, and stringified annotations
of local classes are invisible to it (they'd silently become query params).
"""

import asyncio
import queue
from importlib import resources

import ballast
from ..eventlog import SQLiteEventLog
from .demo_swarm import DemoSwarm


def build_runtime() -> "ballast.Runtime":
    """Dashboard-hosted runtime: mock dependencies + chaos, per TechSpec §7."""
    return ballast.configure(
        dependencies={
            # Generous latency multipliers: mock latencies are thread-jittery.
            "mock_llm": {"cooldown_s": 3.0, "latency_multiplier": 10.0},
            "vector_db": {"cooldown_s": 3.0, "latency_multiplier": 10.0},
        },
        max_concurrency=40,
        max_queue_depth=200,
        budget_usd_per_hour=2.0,
        chaos_enabled=True,
    )


def create_app(db_path: str = "ballast_events.db"):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    from fastapi.websockets import WebSocket, WebSocketDisconnect
    from pydantic import BaseModel

    runtime = build_runtime()
    event_log = SQLiteEventLog(db_path)
    event_log.attach(runtime)
    swarm = DemoSwarm()

    app = FastAPI(title="Ballast", version=ballast.__version__)
    index_html = (
        resources.files(__package__).joinpath("static/index.html").read_text(encoding="utf-8")
    )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return index_html

    @app.get("/api/status")
    def status() -> dict:
        return runtime.status()

    @app.get("/api/events")
    def events(
        limit: int = 200,
        event_type: str | None = None,
        dependency: str | None = None,
    ) -> list[dict]:
        return event_log.query(limit=limit, event_type=event_type, dependency=dependency)

    @app.post("/api/demo/start")
    def demo_start() -> dict:
        started = swarm.start()
        return {"started": started, "running": swarm.running}

    class ChaosRun(BaseModel):
        preset: str
        dependency: str
        duration_s: float = 30.0

    @app.post("/api/chaos/run")
    def chaos_run(body: ChaosRun) -> dict:
        try:
            runtime.chaos.scenario(body.preset).run(body.dependency, body.duration_s)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "active": runtime.chaos.active()}

    @app.post("/api/chaos/clear")
    def chaos_clear() -> dict:
        runtime.chaos.clear()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        # Bus events arrive on worker threads; a thread-safe queue bridges them
        # into this coroutine, drained once per tick.
        pending: queue.Queue = queue.Queue()
        unsubscribe = runtime.bus.subscribe(pending.put)
        try:
            while True:
                drained = []
                while True:
                    try:
                        drained.append(pending.get_nowait().to_dict())
                    except queue.Empty:
                        break
                await sock.send_json({
                    "type": "tick",
                    "status": runtime.status(),
                    "events": drained,
                    "demo_running": swarm.running,
                })
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            pass
        finally:
            unsubscribe()

    return app

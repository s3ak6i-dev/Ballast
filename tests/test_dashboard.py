"""Dashboard API contract (M4). Skipped when the dashboard extra isn't installed."""

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ballast.dashboard.app import create_app  # noqa: E402


@pytest.fixture
def client():
    app = create_app(db_path=":memory:")
    with TestClient(app) as test_client:
        yield test_client


def test_index_serves_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Ballast" in response.text


def test_status_shape(client):
    status = client.get("/api/status").json()
    assert {"session_id", "dependencies", "backpressure", "budget", "cache", "chaos"} <= set(status)
    assert "mock_llm" in status["dependencies"]
    assert status["chaos"]["enabled"] is True


def test_chaos_run_and_clear(client):
    response = client.post("/api/chaos/run", json={
        "preset": "api_outage", "dependency": "mock_llm", "duration_s": 5,
    })
    assert response.status_code == 200
    assert "mock_llm" in response.json()["active"]

    assert client.post("/api/chaos/clear").json() == {"ok": True}
    assert client.get("/api/status").json()["chaos"]["active"] == {}


def test_chaos_unknown_preset_rejected(client):
    response = client.post("/api/chaos/run", json={
        "preset": "nope", "dependency": "mock_llm",
    })
    assert response.status_code == 400


def test_events_endpoint_returns_log(client):
    client.post("/api/chaos/run", json={
        "preset": "api_outage", "dependency": "mock_llm", "duration_s": 5,
    })
    events = client.get("/api/events", params={"event_type": "chaos_injected"}).json()
    assert events and events[0]["dependency"] == "mock_llm"

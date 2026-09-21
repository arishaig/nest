"""Routes and the safeguards on the stop/start/restart buttons."""

import httpx
import pytest
from fastapi.testclient import TestClient

from subgen_ui import kube, loki
from subgen_ui.main import app

CSRF = {"X-Requested-With": "subgen-ui"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def deployment(_client):
        return {"desired": 1, "ready": 1}

    async def scale(_client, n):
        seen.append(("scale", n))

    async def restart(_client):
        seen.append(("restart",))

    monkeypatch.setattr(kube, "deployment", deployment)
    monkeypatch.setattr(kube, "scale", scale)
    monkeypatch.setattr(kube, "restart", restart)
    return seen


def test_index_and_healthz(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert client.get("/healthz").json() == {"ok": True}


def test_status_degrades_instead_of_failing_when_backends_are_down(client):
    # No service account token and no Loki in CI: both calls fail, the page
    # must still get a 200 with the problems listed.
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "unknown"
    assert len(body["problems"]) >= 2


def test_status_running(client, monkeypatch):
    async def deployment(_c):
        return {"desired": 1, "ready": 1}

    async def pod(_c):
        return {"name": "subgen-x", "phase": "Running", "restarts": 2, "age_s": 60, "last_exit": None}

    async def events(_c):
        return []

    monkeypatch.setattr(kube, "deployment", deployment)
    monkeypatch.setattr(kube, "newest_pod", pod)
    monkeypatch.setattr(loki, "fetch_events", events)
    body = client.get("/api/status").json()
    assert body["state"] == "running" and body["pod"]["restarts"] == 2 and body["problems"] == []


def test_actions_require_csrf_header(client, calls):
    assert client.post("/api/stop").status_code == 400
    assert calls == []


def test_unknown_action_is_404(client, calls):
    assert client.post("/api/explode", headers=CSRF).status_code == 404


def test_stop_scales_to_zero_and_start_to_one(client, calls):
    assert client.post("/api/stop", headers=CSRF).status_code == 200
    assert client.post("/api/start", headers=CSRF).status_code == 200
    assert calls == [("scale", 0), ("scale", 1)]


def test_restart_refused_while_stopped(client, monkeypatch, calls):
    async def stopped(_c):
        return {"desired": 0, "ready": 0}

    monkeypatch.setattr(kube, "deployment", stopped)
    assert client.post("/api/restart", headers=CSRF).status_code == 409
    assert calls == []


def test_kube_api_failure_is_a_502(client, monkeypatch):
    async def boom(_c, _n):
        raise httpx.ConnectError("nope")

    async def deployment(_c):
        return {"desired": 1, "ready": 1}

    monkeypatch.setattr(kube, "deployment", deployment)
    monkeypatch.setattr(kube, "scale", boom)
    assert client.post("/api/stop", headers=CSRF).status_code == 502

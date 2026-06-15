import asyncio

import httpx
import pytest

from nest_mcp import http_client, ssh_client, config


# ---- http_client.make_client -------------------------------------------

async def test_make_client_applies_options():
    c = http_client.make_client("http://x.test", headers={"a": "b"}, timeout=5)
    assert str(c.base_url).rstrip("/") == "http://x.test"
    assert c.headers["a"] == "b"
    await c.aclose()


# ---- http_client.UniFiSession ------------------------------------------

async def test_unifi_session_login_csrf_and_401_retry(monkeypatch):
    calls = {"login": 0, "data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            calls["login"] += 1
            return httpx.Response(200, headers={"x-csrf-token": "tok"}, json={})
        calls["data"] += 1
        if calls["data"] == 1:
            return httpx.Response(401, json={})  # forces re-login + retry
        return httpx.Response(200, json={"data": [1]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        http_client.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), base_url=kw.get("base_url", "http://t")),
    )

    session = http_client.UniFiSession("http://unifi.test", "u", "p")
    resp = await session.get("/data")
    assert resp.json() == {"data": [1]}
    assert calls["login"] == 2  # initial login + re-login after 401
    await session.aclose()


# ---- ssh_client.ssh_run -------------------------------------------------

class _FakeProc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode = rc
        self._out, self._err = out, err

    async def communicate(self):
        return self._out, self._err


async def test_ssh_run_success(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(0, out=b"hello\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await ssh_client.ssh_run("host", "echo hi") == "hello"


async def test_ssh_run_raises_on_nonzero(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(1, err=b"permission denied\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="permission denied"):
        await ssh_client.ssh_run("host", "bad")


# ---- config -------------------------------------------------------------

def test_config_defaults():
    assert config.proxmox.node == "proxmox"
    assert config.kubernetes.api_url.endswith(":6443")


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("NEST_PROXMOX_NODE", "other-node")
    assert config.ProxmoxSettings().node == "other-node"

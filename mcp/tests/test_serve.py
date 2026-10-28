import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nest_mcp import auth, serve


@pytest.fixture
def client():
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok, methods=["GET", "POST"])])
    app.add_middleware(serve.JWTMiddleware)
    return TestClient(app)


def _verify(monkeypatch, result):
    async def fake(token):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(serve, "verify_token", fake)


def test_discovery_advertises_offline_access(client):
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource"):
        assert "offline_access" in client.get(path).json()["scopes_supported"]


def test_missing_bearer_is_challenged_with_scope(client):
    r = client.get("/")
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["WWW-Authenticate"]
    assert "offline_access" in r.headers["WWW-Authenticate"]
    assert "invalid_token" not in r.headers["WWW-Authenticate"]


def test_dead_token_is_invalid_token_with_metadata(client, monkeypatch):
    _verify(monkeypatch, ValueError("Token inactive"))
    r = client.get("/", headers={"Authorization": "Bearer x"})
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["WWW-Authenticate"]
    assert "resource_metadata=" in r.headers["WWW-Authenticate"]


def test_authelia_outage_is_503_not_401(client, monkeypatch):
    _verify(monkeypatch, auth.AuthUnavailable("down"))
    r = client.get("/", headers={"Authorization": "Bearer x"})
    assert r.status_code == 503
    assert "WWW-Authenticate" not in r.headers


def test_valid_token_passes(client, monkeypatch):
    _verify(monkeypatch, {"active": True})
    assert client.get("/", headers={"Authorization": "Bearer x"}).text == "ok"


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("e", request=None, response=None)

    def json(self):
        return self._d


class _Client:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


@pytest.fixture
def introspect(monkeypatch):
    async def uri():
        return "http://authelia/introspection"

    monkeypatch.setattr(auth, "_get_introspection_uri", uri)
    auth._seen.clear()

    def setup(resp):
        monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **k: _Client(resp))

    return setup


async def test_verify_token_logs_grant_once(introspect, caplog):
    introspect(_Resp({"active": True, "client_id": "nest-mcp", "scope": "openid offline_access", "exp": 9999999999}))
    with caplog.at_level("INFO", logger="nest_mcp.auth"):
        await auth.verify_token("tok")
        await auth.verify_token("tok")
    lines = [r.message for r in caplog.records if "new token" in r.message]
    assert len(lines) == 1 and "offline_access" in lines[0]


async def test_verify_token_inactive_raises_value_error(introspect):
    introspect(_Resp({"active": False}))
    with pytest.raises(ValueError):
        await auth.verify_token("tok")


@pytest.mark.parametrize("resp", [httpx.ConnectError("boom"), _Resp({}, 500), _Resp({}, 401)])
async def test_verify_token_upstream_failure_is_unavailable(introspect, resp):
    introspect(resp)
    with pytest.raises(auth.AuthUnavailable):
        await auth.verify_token("tok")


async def test_verify_token_rejects_other_clients(introspect):
    introspect(_Resp({"active": True, "client_id": "some-other-app", "scope": "openid"}))
    with pytest.raises(ValueError, match="client not allowed"):
        await auth.verify_token("tok")

"""Shared test helpers for the nest_mcp tool suite.

The tools are async closures registered on a FastMCP instance inside each
module's ``register(mcp)``. We register them on a throwaway FastMCP, pull the
underlying functions back out, and call them directly with the network layer
stubbed — so the tests exercise the real formatting/transform logic without
touching the live homelab.
"""
import httpx
from mcp.server.fastmcp import FastMCP


def load_tools(module) -> dict:
    """Register a tool module on a fresh FastMCP; return {tool_name: fn}."""
    mcp = FastMCP("test")
    module.register(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


def patch_http(monkeypatch, module, routes: dict) -> None:
    """Stub ``module.make_client`` so HTTP calls return canned JSON.

    ``routes`` maps a request path (exact, or prefix when the key ends in ``*``)
    to either a JSON payload (implies 200) or a ``(status_code, payload)`` tuple.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for key, value in routes.items():
            if (key.endswith("*") and path.startswith(key[:-1])) or path == key:
                if callable(value):
                    value = value(request)
                status, payload = value if isinstance(value, tuple) else (200, value)
                if isinstance(payload, str):
                    return httpx.Response(status, text=payload)
                return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": f"no stub for {path}"})

    def fake_make_client(base_url: str, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=base_url,
            follow_redirects=True,
        )

    monkeypatch.setattr(module, "make_client", fake_make_client)


def patch_ssh(monkeypatch, module, responses) -> None:
    """Stub ``module.ssh_run``.

    ``responses`` is either a single string (returned for any command) or a
    callable ``(host, cmd) -> str``.
    """
    async def fake_ssh_run(host, cmd, *args, **kwargs):
        return responses(host, cmd) if callable(responses) else responses

    monkeypatch.setattr(module, "ssh_run", fake_ssh_run)

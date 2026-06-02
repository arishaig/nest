import os

import httpx

ISSUER = os.getenv("MCP_AUTHELIA_ISSUER", "https://auth.arishaig.site")
CLIENT_ID = os.getenv("MCP_CLIENT_ID", "nest-mcp")
CLIENT_SECRET = os.getenv("MCP_CLIENT_SECRET", "")

_introspection_uri: str | None = None


async def _get_introspection_uri() -> str:
    global _introspection_uri
    if _introspection_uri is None:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{ISSUER}/.well-known/openid-configuration")
            r.raise_for_status()
            _introspection_uri = r.json()["introspection_endpoint"]
    return _introspection_uri


async def verify_token(token: str) -> dict:
    uri = await _get_introspection_uri()
    async with httpx.AsyncClient() as c:
        r = await c.post(uri, data={"token": token}, auth=(CLIENT_ID, CLIENT_SECRET))
        r.raise_for_status()
    data = r.json()
    if not data.get("active"):
        raise ValueError("Token inactive")
    return data

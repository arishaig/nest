import hashlib
import logging
import os
import time

import httpx

log = logging.getLogger("nest_mcp.auth")


class AuthUnavailable(Exception):
    """Authelia could not answer; says nothing about the token itself."""

ISSUER = os.getenv("MCP_AUTHELIA_ISSUER", "https://auth.arishaig.site")
CLIENT_ID = os.getenv("MCP_CLIENT_ID", "nest-mcp")
CLIENT_SECRET = os.getenv("MCP_CLIENT_SECRET", "")
# Introspection only says a token is active, for any client of this Authelia.
# Accept tokens issued to these clients only (default: this server's own).
ALLOWED_CLIENT_IDS = {
    c.strip() for c in os.getenv("MCP_ALLOWED_CLIENT_IDS", CLIENT_ID).split(",") if c.strip()
}

_introspection_uri: str | None = None


# Token fingerprints already logged, so each grant's scope/expiry is recorded
# once (not on every request). Bounded; the oldest entry is dropped when full.
_seen: dict[str, None] = {}
_SEEN_MAX = 256


async def _get_introspection_uri() -> str:
    global _introspection_uri
    if _introspection_uri is None:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{ISSUER}/.well-known/openid-configuration")
            r.raise_for_status()
            _introspection_uri = r.json()["introspection_endpoint"]
    return _introspection_uri


def _log_grant(token: str, data: dict) -> None:
    key = hashlib.sha256(token.encode()).hexdigest()[:12]
    if key in _seen:
        return
    if len(_seen) >= _SEEN_MAX:
        _seen.pop(next(iter(_seen)))
    _seen[key] = None
    exp = data.get("exp")
    log.info(
        "new token %s client=%s scope=%r expires_in=%ss",
        key, data.get("client_id"), data.get("scope"),
        int(exp - time.time()) if exp else "?",
    )


async def verify_token(token: str) -> dict:
    """Introspect a bearer token.

    Raises ValueError when Authelia says the token is not active, and
    AuthUnavailable when Authelia could not give an answer at all.
    """
    try:
        uri = await _get_introspection_uri()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(uri, data={"token": token}, auth=(CLIENT_ID, CLIENT_SECRET))
            r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError, KeyError) as e:
        raise AuthUnavailable(f"introspection failed: {e!r}") from e
    if not data.get("active"):
        log.warning("token rejected: introspection reports inactive")
        raise ValueError("Token inactive")
    if data.get("client_id") not in ALLOWED_CLIENT_IDS:
        log.warning("token rejected: client_id=%r not allowed", data.get("client_id"))
        raise ValueError("Token client not allowed")
    _log_grant(token, data)
    return data

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from nest_mcp.auth import ISSUER, verify_token
from nest_mcp.server import mcp

_LOGO = Path(__file__).parent / "static" / "nest_logo.webp"


async def _serve_logo(request):
    return FileResponse(_LOGO, media_type="image/webp")

PORT = int(os.getenv("MCP_PORT", "8765"))

MCP_URL = "https://mcp.arishaig.site"

DISCOVERY = {
    "issuer": MCP_URL,
    "authorization_endpoint": f"{ISSUER}/api/oidc/authorization",
    "token_endpoint": f"{ISSUER}/api/oidc/token",
    "jwks_uri": f"{ISSUER}/jwks.json",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
}

PROTECTED_RESOURCE = {
    "resource": MCP_URL,
    "authorization_servers": [ISSUER],
    "bearer_methods_supported": ["header"],
    "resource_name": "Nest MCP",
}

class JWTMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/.well-known/oauth-authorization-server":
            return JSONResponse(DISCOVERY)
        if request.url.path == "/.well-known/oauth-protected-resource":
            return JSONResponse(PROTECTED_RESOURCE)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return Response(
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer realm="{MCP_URL}", resource_metadata="{MCP_URL}/.well-known/oauth-protected-resource"',
                },
            )
        try:
            await verify_token(auth[7:])
        except Exception:
            return Response(status_code=401)
        return await call_next(request)


def main():
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.streamable_http_path = "/"
    mcp.settings.stateless_http = True
    mcp.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=["mcp.arishaig.site"],
        allowed_origins=["https://claude.ai"],
    )
    mcp_app = mcp.streamable_http_app()
    mcp_app.add_middleware(JWTMiddleware)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/nest_logo.webp", _serve_logo),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

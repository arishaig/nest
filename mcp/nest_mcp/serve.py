import os

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from nest_mcp.auth import ISSUER, verify_token
from nest_mcp.server import mcp

PORT = int(os.getenv("MCP_PORT", "8765"))

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/api/oidc/authorization",
    "token_endpoint": f"{ISSUER}/api/oidc/token",
    "jwks_uri": f"{ISSUER}/jwks.json",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
}


class JWTMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/.well-known/oauth-authorization-server":
            return JSONResponse(DISCOVERY)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer realm="{ISSUER}"'},
            )
        try:
            await verify_token(auth[7:])
        except Exception:
            return Response(status_code=401)
        return await call_next(request)


def main():
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.streamable_http_path = "/"
    mcp.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=["mcp.arishaig.site"],
        allowed_origins=["https://claude.ai"],
    )
    app = mcp.streamable_http_app()
    app.add_middleware(JWTMiddleware)
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

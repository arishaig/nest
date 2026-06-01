from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from nest_mcp.tools.unifi import get_session


@asynccontextmanager
async def lifespan(server):
    # Pre-authenticate UniFi so the first tool call doesn't bear the login latency
    session = get_session()
    try:
        yield
    finally:
        await session.aclose()


mcp = FastMCP("nest-mcp", lifespan=lifespan)

from nest_mcp.tools import proxmox, adguard, homeassistant, arr, prometheus, scrutiny, unifi, jellyfin, jellyseerr  # noqa: E402

proxmox.register(mcp)
adguard.register(mcp)
homeassistant.register(mcp)
arr.register(mcp)
prometheus.register(mcp)
scrutiny.register(mcp)
unifi.register(mcp)
jellyfin.register(mcp)
jellyseerr.register(mcp)

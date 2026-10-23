def test_server_registers_all_tools():
    # Importing the server module wires every tool onto the MCPServer instance.
    from nest_mcp import server
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert {"prometheus_targets", "k8s_pods", "proxmox_list_vms", "lab_health_summary"} <= names
    assert len(names) > 30


async def test_backend_http_failures_reach_the_client_readably():
    import httpx
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError
    from nest_mcp.server import NestMCPServer

    srv = NestMCPServer("t")

    @srv.tool()
    async def dead() -> str:
        raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution",
                                 request=httpx.Request("GET", "http://jellyfin.media.svc:8096/Sessions?api_key=SECRET"))

    @srv.tool()
    async def bad() -> str:
        req = httpx.Request("POST", "http://tunarr:8000/api/channels?token=SECRET")
        raise httpx.HTTPStatusError("boom", request=req, response=httpx.Response(500, request=req, text="SECRET body"))

    @srv.tool()
    async def crash() -> str:
        raise KeyError("SECRET")

    with pytest.raises(ToolError) as e:
        await srv.call_tool("dead", {})
    assert "name resolution" in str(e.value) and "jellyfin.media.svc" in str(e.value)
    with pytest.raises(ToolError) as e:
        await srv.call_tool("bad", {})
    assert "HTTP 500 from POST tunarr/api/channels" in str(e.value)
    with pytest.raises(ToolError) as e:
        await srv.call_tool("crash", {})
    assert str(e.value) == "Error executing tool crash"
    assert "SECRET" not in str(e.value)


def test_describe_failure_without_request_and_unknown():
    import httpx
    from nest_mcp.server import describe_failure

    assert "ReadTimeout" in describe_failure(httpx.ReadTimeout("slow"))
    assert describe_failure(ValueError("x")) is None
    assert describe_failure(None) is None

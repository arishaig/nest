def test_server_registers_all_tools():
    # Importing the server module wires every tool onto the FastMCP instance.
    from nest_mcp import server
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert {"prometheus_targets", "k8s_pods", "proxmox_list_vms", "lab_health_summary"} <= names
    assert len(names) > 30

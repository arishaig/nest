from nest_mcp.tools import proxmox
from helpers import load_tools, patch_http

NODE = "/api2/json/nodes/proxmox"


async def test_list_containers_sorted_and_scaled(monkeypatch):
    patch_http(monkeypatch, proxmox, {f"{NODE}/lxc": {"data": [
        {"vmid": 105, "name": "mon", "status": "running", "cpu": 0.5,
         "mem": 1024**3, "maxmem": 2 * 1024**3},
        {"vmid": 100, "name": "docker", "status": "stopped", "cpu": 0, "mem": 0, "maxmem": 1024**3},
    ]}})
    out = await load_tools(proxmox)["proxmox_list_containers"]()
    assert [c["vmid"] for c in out] == [100, 105]
    assert out[1]["cpu"] == 50.0 and out[1]["mem_mb"] == 1024


async def test_list_vms(monkeypatch):
    patch_http(monkeypatch, proxmox, {f"{NODE}/qemu": {"data": [
        {"vmid": 110, "name": "alpha", "status": "running", "cpu": 0.1, "mem": 0, "maxmem": 0},
    ]}})
    out = await load_tools(proxmox)["proxmox_list_vms"]()
    assert out[0]["name"] == "alpha" and out[0]["cpu"] == 10.0


async def test_container_status(monkeypatch):
    patch_http(monkeypatch, proxmox, {f"{NODE}/lxc/*": {"data": {
        "name": "docker", "status": "running", "cpu": 0.25, "mem": 1024**2, "maxmem": 2 * 1024**2,
    }}})
    out = await load_tools(proxmox)["proxmox_container_status"](vmid=100)
    assert out["vmid"] == 100 and out["cpu_pct"] == 25.0 and out["mem_mb"] == 1


async def test_node_stats(monkeypatch):
    patch_http(monkeypatch, proxmox, {f"{NODE}/status": {"data": {
        "cpu": 0.3, "cpuinfo": {"cpus": 8}, "memory": {"used": 4 * 1024**3, "total": 16 * 1024**3},
        "loadavg": ["0.5"], "kversion": "6.x", "pveversion": "8.x",
    }}})
    out = await load_tools(proxmox)["proxmox_node_stats"]()
    assert out["cpus"] == 8 and out["mem_used_gb"] == 4.0 and out["mem_total_gb"] == 16.0


async def test_storage_status(monkeypatch):
    patch_http(monkeypatch, proxmox, {f"{NODE}/storage": {"data": [
        {"storage": "local", "type": "dir", "active": 1, "used": 1024**3,
         "total": 2 * 1024**3, "avail": 1024**3, "used_fraction": 0.5},
    ]}})
    out = await load_tools(proxmox)["proxmox_storage_status"]()
    assert out[0]["active"] is True and out[0]["pct_used"] == 50.0


async def test_recent_tasks(monkeypatch):
    patch_http(monkeypatch, proxmox, {f"{NODE}/tasks": {"data": [
        {"upid": "UPID:x", "type": "vzdump", "status": "OK", "user": "root@pam"},
    ]}})
    out = await load_tools(proxmox)["proxmox_recent_tasks"]()
    assert out[0]["type"] == "vzdump" and out[0]["status"] == "OK"


async def test_snapshots_skips_current(monkeypatch):
    patch_http(monkeypatch, proxmox, {
        f"{NODE}/lxc": {"data": [{"vmid": 100, "name": "docker"}]},
        f"{NODE}/qemu": {"data": [{"vmid": 110, "name": "alpha"}]},
        f"{NODE}/lxc/*": {"data": [{"name": "snap1", "snaptime": 5}, {"name": "current"}]},
        f"{NODE}/qemu/*": {"data": [{"name": "vmsnap", "snaptime": 3}]},
    })
    out = await load_tools(proxmox)["proxmox_snapshots"]()
    names = {s["snapshot"] for s in out}
    assert names == {"snap1", "vmsnap"}  # "current" filtered out


async def test_start_stop_container(monkeypatch):
    patch_http(monkeypatch, proxmox, {f"{NODE}/lxc/*": {"data": "UPID:task"}})
    tools = load_tools(proxmox)
    assert (await tools["proxmox_start_container"](vmid=100))["task"] == "UPID:task"
    assert (await tools["proxmox_stop_container"](vmid=100))["task"] == "UPID:task"

from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client


def _auth_header() -> dict:
    return {"Authorization": f"PVEAPIToken={config.proxmox.token}"}


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def proxmox_list_containers() -> list[dict]:
        """List all LXC containers on the Proxmox node with status, uptime, and IP."""
        async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=_auth_header()) as client:
            resp = await client.get(f"/api2/json/nodes/{config.proxmox.node}/lxc")
            resp.raise_for_status()
            containers = resp.json()["data"]
            result = []
            for c in sorted(containers, key=lambda x: x["vmid"]):
                result.append({
                    "vmid": c["vmid"],
                    "name": c.get("name", ""),
                    "status": c["status"],
                    "uptime": c.get("uptime", 0),
                    "cpu": round(c.get("cpu", 0) * 100, 1),
                    "mem_mb": round(c.get("mem", 0) / 1024 / 1024),
                    "maxmem_mb": round(c.get("maxmem", 0) / 1024 / 1024),
                    "tags": c.get("tags", ""),
                })
            return result

    @mcp.tool()
    async def proxmox_list_vms() -> list[dict]:
        """List all VMs on the Proxmox node with status."""
        async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=_auth_header()) as client:
            resp = await client.get(f"/api2/json/nodes/{config.proxmox.node}/qemu")
            resp.raise_for_status()
            vms = resp.json()["data"]
            return [
                {
                    "vmid": v["vmid"],
                    "name": v.get("name", ""),
                    "status": v["status"],
                    "uptime": v.get("uptime", 0),
                    "cpu": round(v.get("cpu", 0) * 100, 1),
                    "mem_mb": round(v.get("mem", 0) / 1024 / 1024),
                    "maxmem_mb": round(v.get("maxmem", 0) / 1024 / 1024),
                }
                for v in sorted(vms, key=lambda x: x["vmid"])
            ]

    @mcp.tool()
    async def proxmox_container_status(vmid: int) -> dict:
        """Get detailed current status for a specific LXC container by VMID."""
        async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=_auth_header()) as client:
            resp = await client.get(f"/api2/json/nodes/{config.proxmox.node}/lxc/{vmid}/status/current")
            resp.raise_for_status()
            d = resp.json()["data"]
            return {
                "vmid": vmid,
                "name": d.get("name", ""),
                "status": d["status"],
                "uptime": d.get("uptime", 0),
                "cpu_pct": round(d.get("cpu", 0) * 100, 2),
                "mem_mb": round(d.get("mem", 0) / 1024 / 1024),
                "maxmem_mb": round(d.get("maxmem", 0) / 1024 / 1024),
                "swap_mb": round(d.get("swap", 0) / 1024 / 1024),
                "maxswap_mb": round(d.get("maxswap", 0) / 1024 / 1024),
                "disk_mb": round(d.get("disk", 0) / 1024 / 1024),
                "maxdisk_mb": round(d.get("maxdisk", 0) / 1024 / 1024),
                "netin_mb": round(d.get("netin", 0) / 1024 / 1024, 1),
                "netout_mb": round(d.get("netout", 0) / 1024 / 1024, 1),
            }

    @mcp.tool()
    async def proxmox_node_stats() -> dict:
        """Get current CPU, memory, and load stats for the Proxmox node."""
        async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=_auth_header()) as client:
            resp = await client.get(f"/api2/json/nodes/{config.proxmox.node}/status")
            resp.raise_for_status()
            d = resp.json()["data"]
            return {
                "node": config.proxmox.node,
                "cpu_pct": round(d.get("cpu", 0) * 100, 1),
                "cpus": d.get("cpuinfo", {}).get("cpus", 0),
                "mem_used_gb": round(d.get("memory", {}).get("used", 0) / 1024**3, 2),
                "mem_total_gb": round(d.get("memory", {}).get("total", 0) / 1024**3, 2),
                "load_avg": d.get("loadavg", []),
                "uptime": d.get("uptime", 0),
                "kernel_version": d.get("kversion", ""),
                "pve_version": d.get("pveversion", ""),
            }

    @mcp.tool()
    async def proxmox_storage_status() -> list[dict]:
        """List all Proxmox storage pools with used/total/available space."""
        async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=_auth_header()) as client:
            resp = await client.get(f"/api2/json/nodes/{config.proxmox.node}/storage")
            resp.raise_for_status()
            stores = resp.json()["data"]
            return [
                {
                    "storage": s["storage"],
                    "type": s.get("type", ""),
                    "content": s.get("content", ""),
                    "active": s.get("active", 0) == 1,
                    "used_gb": round(s.get("used", 0) / 1024**3, 2),
                    "total_gb": round(s.get("total", 0) / 1024**3, 2),
                    "avail_gb": round(s.get("avail", 0) / 1024**3, 2),
                    "pct_used": round(s.get("used_fraction", 0) * 100, 1),
                }
                for s in sorted(stores, key=lambda x: x["storage"])
            ]

    @mcp.tool()
    async def proxmox_recent_tasks(limit: int = 20) -> list[dict]:
        """List recent Proxmox tasks (backups, migrations, etc.) with status and time."""
        async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=_auth_header()) as client:
            resp = await client.get(f"/api2/json/nodes/{config.proxmox.node}/tasks", params={"limit": limit})
            resp.raise_for_status()
            tasks = resp.json()["data"]
            return [
                {
                    "upid": t.get("upid", ""),
                    "type": t.get("type", ""),
                    "status": t.get("status", ""),
                    "user": t.get("user", ""),
                    "starttime": t.get("starttime", 0),
                    "endtime": t.get("endtime", 0),
                    "id": t.get("id", ""),
                }
                for t in tasks
            ]

    @mcp.tool()
    async def proxmox_start_container(vmid: int) -> dict:
        """[MUTATING] Start a stopped LXC container by VMID."""
        async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=_auth_header()) as client:
            resp = await client.post(f"/api2/json/nodes/{config.proxmox.node}/lxc/{vmid}/status/start")
            resp.raise_for_status()
            return {"vmid": vmid, "task": resp.json()["data"]}

    @mcp.tool()
    async def proxmox_stop_container(vmid: int) -> dict:
        """[MUTATING] Stop a running LXC container by VMID."""
        async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=_auth_header()) as client:
            resp = await client.post(f"/api2/json/nodes/{config.proxmox.node}/lxc/{vmid}/status/stop")
            resp.raise_for_status()
            return {"vmid": vmid, "task": resp.json()["data"]}

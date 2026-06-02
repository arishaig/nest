import httpx
from mcp.server.fastmcp import FastMCP

from nest_mcp import config


async def _get_ticket(client: httpx.AsyncClient) -> tuple[str, str]:
    resp = await client.post(
        f"{config.pbs.url}/api2/json/access/ticket",
        data={"username": config.pbs.username, "password": config.pbs.password},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data["ticket"], data["CSRFPreventionToken"]


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def proxmox_backup_status() -> dict:
        """Get recent backup job results and datastore usage from Proxmox Backup Server."""
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            ticket, _ = await _get_ticket(client)
            headers = {"Cookie": f"PBSAuthCookie={ticket}"}

            tasks_resp = await client.get(
                f"{config.pbs.url}/api2/json/nodes/{config.pbs.node}/tasks",
                headers=headers,
                params={"limit": 20, "typefilter": "backup"},
            )
            tasks_resp.raise_for_status()
            tasks = tasks_resp.json().get("data", [])

            ds_resp = await client.get(
                f"{config.pbs.url}/api2/json/admin/datastore",
                headers=headers,
            )
            ds_resp.raise_for_status()
            datastores = ds_resp.json().get("data", [])

        recent_jobs = [
            {
                "id": t.get("id"),
                "upid": t.get("upid", "")[:60],
                "status": t.get("status"),
                "starttime": t.get("starttime"),
                "endtime": t.get("endtime"),
                "worker_id": t.get("worker_id"),
            }
            for t in tasks
        ]

        datastore_usage = [
            {
                "name": d.get("store"),
                "total_gb": round(d.get("total", 0) / 1024**3, 1),
                "used_gb": round(d.get("used", 0) / 1024**3, 1),
                "avail_gb": round(d.get("avail", 0) / 1024**3, 1),
                "gc_status": d.get("gc-status", {}).get("upid", "unknown"),
            }
            for d in datastores
        ]

        return {"recent_backup_tasks": recent_jobs, "datastores": datastore_usage}

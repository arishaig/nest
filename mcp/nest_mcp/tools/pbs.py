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

            datastore_usage = []
            for d in datastores:
                name = d.get("store", "")
                status_resp = await client.get(
                    f"{config.pbs.url}/api2/json/admin/datastore/{name}/status",
                    headers=headers,
                )
                gc_resp = await client.get(
                    f"{config.pbs.url}/api2/json/admin/datastore/{name}/gc",
                    headers=headers,
                )
                s = status_resp.json().get("data", {}) if status_resp.is_success else {}
                gc = gc_resp.json().get("data", {}) if gc_resp.is_success else {}
                datastore_usage.append({
                    "name": name,
                    "total_gb": round(s.get("total", 0) / 1024**3, 1),
                    "used_gb": round(s.get("used", 0) / 1024**3, 1),
                    "avail_gb": round(s.get("avail", 0) / 1024**3, 1),
                    "gc_status": gc.get("last-run-state", "unknown"),
                })

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

        return {"recent_backup_tasks": recent_jobs, "datastores": datastore_usage}


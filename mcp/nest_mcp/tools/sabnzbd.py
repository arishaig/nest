"""SABnzbd tools: queue, history, status. Read-only.

SABnzbd takes its API key as a query parameter, so every error raised from here
is rewritten without the URL (httpx's own messages include it), and server.py
silences httpx's INFO request logging.
"""
import re
import time
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer

from nest_mcp import config
from nest_mcp.http_client import make_client


async def sab_call(mode: str, **params) -> dict:
    """GET /api?mode=<mode>&output=json and return the parsed body.

    Raises RuntimeError with one clear message (service, status, hint) and never
    the request URL, which carries the API key.
    """
    if not config.sabnzbd.key:
        raise RuntimeError("SABnzbd: NEST_SABNZBD_KEY is not set (vault var sabnzbd_api_key); nest-mcp cannot call SABnzbd yet.")
    query = {"mode": mode, "output": "json", "apikey": config.sabnzbd.key, **{k: v for k, v in params.items() if v is not None}}
    try:
        async with make_client(config.sabnzbd.url, timeout=30.0) as client:
            resp = await client.get("/api", params=query)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"SABnzbd returned HTTP {e.response.status_code} for mode={mode}; check the sabnzbd pod (k8s_pods namespace=media) and the API key.") from None
    except httpx.RequestError as e:
        raise RuntimeError(f"SABnzbd unreachable for mode={mode} ({type(e).__name__}); is the sabnzbd pod running?") from None
    except ValueError:
        raise RuntimeError(f"SABnzbd returned non-JSON for mode={mode}; the API key may be wrong.") from None
    if isinstance(body, dict) and body.get("status") is False:
        raise RuntimeError(f"SABnzbd rejected mode={mode}: {body.get('error', 'unknown error')}")
    return body


def _fmt_ts(epoch) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    except (TypeError, ValueError):
        return ""


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def queue_rows(queue: dict) -> list[dict]:
    rows = []
    for s in queue.get("slots", []):
        mb, left = _f(s.get("mb")), _f(s.get("mbleft"))
        rows.append({
            "nzo_id": s.get("nzo_id", ""),
            "name": s.get("filename", ""),
            "category": s.get("cat", ""),
            "status": s.get("status", ""),
            "mb": round(mb),
            "mb_left": round(left),
            "percentage": s.get("percentage", ""),
            "eta": s.get("timeleft", ""),
            "priority": s.get("priority", ""),
        })
    return rows


def register(mcp: MCPServer) -> None:

    @mcp.tool()
    async def sabnzbd_status() -> dict:
        """SABnzbd health at a glance: paused state, speed and speed limit, free disk on the incomplete and complete dirs, queue size, and recent warnings. Explains "0 MB left but still downloading" queues (paused, disk full, or a post-processing/repair backlog). Read-only."""
        q = (await sab_call("queue", limit=0)).get("queue", {})
        warnings = (await sab_call("warnings")).get("warnings", [])
        cfg = (await sab_call("get_config", section="misc")).get("config", {}).get("misc", {})
        return {
            "state": q.get("status", ""),
            "paused": bool(q.get("paused")),
            "pause_int": q.get("pause_int", ""),
            "speed": q.get("speed", ""),
            "speed_limit": q.get("speedlimit", ""),
            "jobs_in_queue": q.get("noofslots_total", q.get("noofslots", 0)),
            "mb_left": round(_f(q.get("mbleft"))),
            "eta": q.get("timeleft", ""),
            "free_gb_incomplete": _f(q.get("diskspace1")),
            "free_gb_complete": _f(q.get("diskspace2")),
            "incomplete_dir": cfg.get("download_dir", ""),
            "complete_dir": cfg.get("complete_dir", ""),
            "warnings": [str(w)[:300] for w in warnings[-10:]],
        }

    @mcp.tool()
    async def sabnzbd_queue(limit: int = 50) -> dict:
        """SABnzbd's own download queue (not just the jobs Radarr/Sonarr track): per job the nzo_id, name, category, status, MB left, ETA and priority, plus whether the queue is paused. Read-only."""
        q = (await sab_call("queue", limit=max(1, min(limit, 500)))).get("queue", {})
        return {
            "paused": bool(q.get("paused")),
            "speed": q.get("speed", ""),
            "total": q.get("noofslots_total", q.get("noofslots", 0)),
            "jobs": queue_rows(q),
        }

    @mcp.tool()
    async def sabnzbd_history(limit: int = 20, category: str = "") -> list[dict]:
        """Recent finished SABnzbd jobs: status (Completed / Failed / Extracting...), fail message, `storage` (the final path inside the container, /data/... as Radarr and Jellyfin see it), category and completion time. Optional category filter. Read-only."""
        body = await sab_call("history", limit=max(1, min(limit, 200)), category=category or None)
        return [
            {
                "nzo_id": s.get("nzo_id", ""),
                "name": s.get("name", ""),
                "category": s.get("category", ""),
                "status": s.get("status", ""),
                "fail_message": s.get("fail_message", ""),
                "storage": s.get("storage", ""),
                "size": s.get("size", ""),
                "completed": _fmt_ts(s.get("completed")),
            }
            for s in body.get("history", {}).get("slots", [])
        ]

    @mcp.tool()
    async def sabnzbd_job_action(
        nzo_ids: list[str], action: Literal["delete", "pause", "resume", "priority"],
        priority: Literal["Low", "Normal", "High", "Force"] = "Normal", del_files: bool = False,
    ) -> dict:
        """[DESTRUCTIVE] Act on SABnzbd queue jobs by nzo_id (from sabnzbd_queue): delete (del_files=True also removes the partial download), pause, resume, or priority (Low/Normal/High/Force). At most 20 ids per call. SABnzbd also accepts ids that are already gone, so check sabnzbd_queue afterwards. Does not touch history. Confirm with the user first."""
        if not nzo_ids or len(nzo_ids) > 20:
            raise ValueError("Pass 1-20 nzo_ids")
        bad = [i for i in nzo_ids if not re.fullmatch(r"SABnzbd_nzo_[A-Za-z0-9_]+", i)]
        if bad:
            raise ValueError(f"Not SABnzbd job ids: {bad}")
        if action == "delete":
            await sab_call("queue", name="delete", value=",".join(nzo_ids), del_files=1 if del_files else 0)
        else:
            for nzo in nzo_ids:
                if action == "priority":
                    await sab_call("queue", name="priority", value=nzo, value2={"Low": -1, "Normal": 0, "High": 1, "Force": 2}[priority])
                else:
                    await sab_call("queue", name=action, value=nzo)
        return {"action": action, "nzo_ids": nzo_ids, "deleted_files": bool(del_files and action == "delete")}

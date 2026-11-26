"""SABnzbd tools: status, queue, history, plus job/history actions and a wait helper.

SABnzbd takes its API key as a query parameter, so every error raised from here
is rewritten without the URL (httpx's own messages include it), and server.py
silences httpx's INFO request logging.
"""
import asyncio
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


# Both id shapes exist: old installs use SABnzbd_nzo_<random>, current ones a UUID.
NZO_ID_RE = re.compile(r"(SABnzbd_nzo_[A-Za-z0-9_]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_TERMINAL = {"Completed", "Failed"}
_WAIT_POLL_S = 5.0
_WAIT_MAX_S = 240  # stay well under the connector's request timeout


def check_nzo_ids(nzo_ids: list[str]) -> None:
    if not nzo_ids or len(nzo_ids) > 20:
        raise ValueError("Pass 1-20 nzo_ids")
    bad = [i for i in nzo_ids if not NZO_ID_RE.fullmatch(i)]
    if bad:
        raise ValueError(f"Not SABnzbd job ids: {bad}")


def stuck_suspects(queue: dict) -> list[dict]:
    """Jobs SABnzbd thinks are fully downloaded (0 MB left) but still lists as Downloading.

    That is the "assembler hung on NFS" signature: they never enter post-processing, and
    the log fills with `Found idle job`. A job can legitimately show this for a few seconds
    right before post-processing, so callers say to re-check rather than conclude.
    Stateless on purpose: nest-mcp runs several replicas, so there is no per-process history.
    """
    out = []
    for s in queue.get("slots", []):
        if s.get("status") == "Downloading" and _f(s.get("mb")) > 0 and _f(s.get("mbleft")) == 0:
            out.append({"nzo_id": s.get("nzo_id", ""), "name": s.get("filename", ""), "category": s.get("cat", "")})
    return out


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
        suspects = stuck_suspects(q)
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
            "suspected_stuck": suspects,
            "stuck_note": (
                f"{len(suspects)} job(s) show 0 MB left but are still Downloading. If that persists for a couple of "
                "minutes it is the NFS assembler hang (log: `Found idle job` / `Resetting bad trylist`): the pod needs a "
                "restart, and in-flight jobs should be deleted first, because a restart turns cached "
                "articles into silent holes."
            ) if suspects else "",
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
        check_nzo_ids(nzo_ids)
        if action == "delete":
            await sab_call("queue", name="delete", value=",".join(nzo_ids), del_files=1 if del_files else 0)
        else:
            for nzo in nzo_ids:
                if action == "priority":
                    await sab_call("queue", name="priority", value=nzo, value2={"Low": -1, "Normal": 0, "High": 1, "Force": 2}[priority])
                else:
                    await sab_call("queue", name=action, value=nzo)
        return {"action": action, "nzo_ids": nzo_ids, "deleted_files": bool(del_files and action == "delete")}

    @mcp.tool()
    async def sabnzbd_history_action(
        nzo_ids: list[str], action: Literal["delete", "retry"], del_files: bool = False,
    ) -> dict:
        """[DESTRUCTIVE] Act on finished SABnzbd history entries by nzo_id (from sabnzbd_history): delete the entry (del_files=True also removes its files from the incomplete/complete dirs: use it for a Failed job's leftovers), or retry a Failed job (re-queues it and repairs from what is already downloaded; it does not fetch from other indexers). At most 20 ids per call. Retry fails again if the articles are genuinely missing: re-grab a different release instead. Confirm with the user first."""
        check_nzo_ids(nzo_ids)
        if action == "delete":
            await sab_call("history", name="delete", value=",".join(nzo_ids), del_files=1 if del_files else 0)
        else:
            for nzo in nzo_ids:
                await sab_call("retry", value=nzo)
        return {"action": action, "nzo_ids": nzo_ids, "deleted_files": bool(del_files and action == "delete")}

    @mcp.tool()
    async def sabnzbd_wait(nzo_id: str, timeout_s: int = 120) -> dict:
        """Wait (up to `timeout_s`, max 240) for one SABnzbd job to reach Completed or Failed, polling every few seconds, and return its state: `state` (queue status, or the history status such as Verifying / Extracting / Completed / Failed), `percentage`, `fail_message`, and `storage` (the final path inside the container, ready for media_place_version). Returns early on a terminal state. If the job sits at 0 MB left as Downloading the whole time it says so (`stuck_suspected`, see sabnzbd_status). Read-only; call again to keep waiting."""
        check_nzo_ids([nzo_id])
        deadline = time.monotonic() + max(1, min(timeout_s, _WAIT_MAX_S))
        last: dict = {}
        stuck = True
        while True:
            q = (await sab_call("queue", limit=500)).get("queue", {})
            slot = next((s for s in q.get("slots", []) if s.get("nzo_id") == nzo_id), None)
            if slot:
                last = {"where": "queue", "state": slot.get("status", ""), "name": slot.get("filename", ""),
                        "percentage": slot.get("percentage", ""), "mb_left": round(_f(slot.get("mbleft"))), "eta": slot.get("timeleft", "")}
                stuck = stuck and bool(stuck_suspects({"slots": [slot]}))
            else:
                stuck = False
                hist = (await sab_call("history", limit=200)).get("history", {}).get("slots", [])
                h = next((s for s in hist if s.get("nzo_id") == nzo_id), None)
                if not h:
                    return {"where": "none", "state": "not_found", "note": "Not in the queue or the last 200 history entries."}
                last = {"where": "history", "state": h.get("status", ""), "name": h.get("name", ""),
                        "fail_message": h.get("fail_message", ""), "storage": h.get("storage", ""), "size": h.get("size", "")}
                if h.get("status") in _TERMINAL:
                    return {**last, "done": True}
            if time.monotonic() >= deadline:
                return {**last, "done": False, "stuck_suspected": stuck and last.get("where") == "queue"}
            await asyncio.sleep(_WAIT_POLL_S)

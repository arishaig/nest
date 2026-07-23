import asyncio
import os
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP

from nest_mcp import config


def _client() -> httpx.AsyncClient:
    token = os.environ.get("NEST_K8S_TOKEN") or config.kubernetes.token
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        base_url=config.kubernetes.api_url,
        headers=headers,
        # Talos API VIP is a raw internal IP signed by the cluster's own CA,
        # not a publicly trusted one — same situation as Proxmox/AdGuard/UniFi
        # below, all of which also run with verify_tls off.
        verify=False,
        timeout=15,
    )


def _age(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.rstrip("Z")).replace(tzinfo=timezone.utc)
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 3600:
            return f"{s // 60}m"
        if s < 86400:
            return f"{s // 3600}h"
        return f"{s // 86400}d"
    except ValueError:
        return ""


async def _fetch_pods(namespace: str = "") -> list[dict]:
    path = f"/api/v1/namespaces/{namespace}/pods" if namespace else "/api/v1/pods"
    async with _client() as c:
        resp = await c.get(path)
        resp.raise_for_status()
    return resp.json().get("items", [])


async def _fetch_nodes() -> list[dict]:
    async with _client() as c:
        resp = await c.get("/api/v1/nodes")
        resp.raise_for_status()
    return resp.json().get("items", [])


def _classify_pod(item: dict) -> dict:
    meta = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    cs_list = status.get("containerStatuses", [])

    restarts = sum(cs.get("restartCount", 0) for cs in cs_list)
    ready_count = sum(1 for cs in cs_list if cs.get("ready", False))
    total_count = len(cs_list) or len(spec.get("containers", []))
    phase = status.get("phase", "")

    if phase == "Running" and ready_count == total_count:
        state = "Running"
    elif phase in ("Pending", "Succeeded", "Failed"):
        state = phase
    else:
        for cs in cs_list:
            waiting = cs.get("state", {}).get("waiting", {})
            if waiting:
                state = waiting.get("reason", "Unknown")
                break
        else:
            state = f"{phase} ({ready_count}/{total_count})"

    return {
        "namespace": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "state": state,
        "ready": f"{ready_count}/{total_count}",
        "restarts": restarts,
        "age": _age(meta.get("creationTimestamp")),
        "node": spec.get("nodeName", ""),
    }


def _node_pod_stats(nodes_items: list[dict], pods_items: list[dict]) -> list[dict]:
    stats: dict[str, dict] = {}
    for item in nodes_items:
        meta = item.get("metadata", {})
        name = meta.get("name", "")
        conditions = item.get("status", {}).get("conditions", [])
        ready_cond = next((cond for cond in conditions if cond["type"] == "Ready"), {})
        stats[name] = {
            "name": name,
            "ready": ready_cond.get("status") == "True",
            "pods_total": 0,
            "pods_running": 0,
            "pods_completed": 0,
            "pods_not_ready": 0,
            "problem_pods": [],
        }

    for item in pods_items:
        pod = _classify_pod(item)
        node = pod["node"]
        if not node:
            continue
        entry = stats.setdefault(node, {
            "name": node,
            "ready": None,
            "pods_total": 0,
            "pods_running": 0,
            "pods_completed": 0,
            "pods_not_ready": 0,
            "problem_pods": [],
        })
        entry["pods_total"] += 1
        if pod["state"] == "Running":
            entry["pods_running"] += 1
        elif pod["state"] == "Succeeded":
            entry["pods_completed"] += 1
        else:
            entry["pods_not_ready"] += 1
            entry["problem_pods"].append(f"{pod['namespace']}/{pod['name']} ({pod['state']})")

    return sorted(stats.values(), key=lambda n: n["name"])


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def k8s_pods(namespace: str = "") -> list[dict]:
        """List Kubernetes pods across the cluster with status, readiness, and restart counts.

        Args:
            namespace: Filter to a single namespace (e.g. "media", "authelia"). Empty = all.
        """
        items = await _fetch_pods(namespace)
        pods = [_classify_pod(item) for item in items]
        return sorted(pods, key=lambda p: (p["namespace"], p["name"]))

    @mcp.tool()
    async def k8s_node_pod_stats() -> list[dict]:
        """Per-node pod distribution and health: pod counts (total/running/completed/not-ready)
        and readiness per node, plus the specific pods causing trouble on each node. Completed
        Job/CronJob pods count separately and are not treated as problems. Use this to see how
        many pods are running on which nodes and spot scheduling imbalance or a node
        accumulating unhealthy pods.
        """
        nodes_items, pods_items = await asyncio.gather(_fetch_nodes(), _fetch_pods())
        return _node_pod_stats(nodes_items, pods_items)

    @mcp.tool()
    async def k8s_events(
        namespace: str = "",
        warning_only: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        """List Kubernetes events, Warning events by default — best first stop for pod crash diagnosis.

        Args:
            namespace: Filter to a namespace. Empty = all namespaces.
            warning_only: Return only Warning events (default True). False returns all events.
            limit: Max events to return (default 20, newest first).
        """
        path = f"/api/v1/namespaces/{namespace}/events" if namespace else "/api/v1/events"
        params: dict = {}
        if warning_only:
            params["fieldSelector"] = "type=Warning"

        async with _client() as c:
            resp = await c.get(path, params=params)
            resp.raise_for_status()

        items = resp.json().get("items", [])

        def _ts(item: dict) -> str:
            return item.get("lastTimestamp") or item.get("eventTime") or ""

        items = sorted(items, key=_ts, reverse=True)[:limit]

        return [
            {
                "namespace": item.get("metadata", {}).get("namespace", ""),
                "type": item.get("type", ""),
                "reason": item.get("reason", ""),
                "object": "{}/{}".format(
                    item.get("involvedObject", {}).get("kind", ""),
                    item.get("involvedObject", {}).get("name", ""),
                ),
                "message": item.get("message", "")[:200],
                "count": item.get("count", 1),
                "last_seen": _age(_ts(item)),
            }
            for item in items
        ]

    @mcp.tool()
    async def k8s_nodes() -> list[dict]:
        """List Kubernetes nodes: readiness, capacity, allocatable resources, and taints."""
        items = await _fetch_nodes()

        nodes = []
        for item in items:
            meta = item.get("metadata", {})
            status = item.get("status", {})
            spec = item.get("spec", {})

            conditions = status.get("conditions", [])
            ready_cond = next((cond for cond in conditions if cond["type"] == "Ready"), {})
            problems = [
                cond["type"] for cond in conditions
                if cond["type"] != "Ready" and cond.get("status") == "True"
            ]

            cap = status.get("capacity", {})
            alloc = status.get("allocatable", {})
            labels = meta.get("labels", {})

            nodes.append({
                "name": meta.get("name", ""),
                "ready": ready_cond.get("status") == "True",
                "age": _age(meta.get("creationTimestamp")),
                "arch": labels.get("kubernetes.io/arch", ""),
                "cpu_capacity": cap.get("cpu", ""),
                "cpu_allocatable": alloc.get("cpu", ""),
                "memory_capacity": cap.get("memory", ""),
                "memory_allocatable": alloc.get("memory", ""),
                "taints": [
                    "{}:{}".format(t["key"], t.get("effect", ""))
                    for t in spec.get("taints", [])
                ],
                "problems": problems,
            })

        return sorted(nodes, key=lambda n: n["name"])

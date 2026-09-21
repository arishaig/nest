"""Minimal Kubernetes API client: just what the status page and buttons need.

Talks REST with the pod's ServiceAccount token instead of pulling in the
kubernetes client library. The Role behind that ServiceAccount only allows
get/patch on the one deployment (and its scale subresource) plus listing pods.
"""

from datetime import datetime, timezone
from pathlib import Path

import httpx

from subgen_ui.config import settings

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MERGE_PATCH = {"Content-Type": "application/merge-patch+json"}


def make_client() -> httpx.AsyncClient:
    ca = SA_DIR / "ca.crt"
    return httpx.AsyncClient(verify=str(ca) if ca.exists() else True, timeout=10)


def _headers(extra: dict | None = None) -> dict:
    # Re-read every call: projected tokens rotate.
    headers = {"Authorization": f"Bearer {(SA_DIR / 'token').read_text().strip()}"}
    return {**headers, **(extra or {})}


def _deployment_url() -> str:
    return (
        f"{settings.kube_url}/apis/apps/v1/namespaces/{settings.namespace}"
        f"/deployments/{settings.deployment}"
    )


async def deployment(client: httpx.AsyncClient) -> dict:
    resp = await client.get(_deployment_url(), headers=_headers())
    resp.raise_for_status()
    status = resp.json().get("status", {})
    spec = resp.json().get("spec", {})
    return {
        "desired": spec.get("replicas", 0),
        "ready": status.get("readyReplicas", 0),
    }


async def newest_pod(client: httpx.AsyncClient) -> dict | None:
    resp = await client.get(
        f"{settings.kube_url}/api/v1/namespaces/{settings.namespace}/pods",
        params={"labelSelector": settings.pod_selector},
        headers=_headers(),
    )
    resp.raise_for_status()
    pods = [p for p in resp.json()["items"] if not p["metadata"].get("deletionTimestamp")]
    if not pods:
        return None
    pod = max(pods, key=lambda p: p["metadata"]["creationTimestamp"])
    statuses = pod.get("status", {}).get("containerStatuses", [])
    last = next(
        (s["lastState"]["terminated"] for s in statuses if s.get("lastState", {}).get("terminated")),
        None,
    )
    created = datetime.fromisoformat(pod["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
    return {
        "name": pod["metadata"]["name"],
        "phase": pod.get("status", {}).get("phase"),
        "restarts": sum(s.get("restartCount", 0) for s in statuses),
        "age_s": int((datetime.now(timezone.utc) - created).total_seconds()),
        "last_exit": (
            {"reason": last.get("reason"), "exit_code": last.get("exitCode"), "at": last.get("finishedAt")}
            if last
            else None
        ),
    }


async def scale(client: httpx.AsyncClient, replicas: int) -> None:
    resp = await client.patch(
        f"{_deployment_url()}/scale",
        json={"spec": {"replicas": replicas}},
        headers=_headers(MERGE_PATCH),
    )
    resp.raise_for_status()


async def restart(client: httpx.AsyncClient) -> None:
    """Same mechanism as `kubectl rollout restart`: bump a pod-template annotation."""
    now = datetime.now(timezone.utc).isoformat()
    resp = await client.patch(
        _deployment_url(),
        json={"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}},
        headers=_headers(MERGE_PATCH),
    )
    resp.raise_for_status()

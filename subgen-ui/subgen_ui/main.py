import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from subgen_ui import kube, loki
from subgen_ui.config import settings
from subgen_ui.parse import summarize

log = logging.getLogger("subgen_ui")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Mutating requests must carry this header. Browsers won't add a custom header
# to a cross-site form post or <img> request, so it stops drive-by CSRF. The
# real access control is Authelia + local-only at the ingress.
CSRF_HEADER = ("x-requested-with", "subgen-ui")
ACTIONS = {"stop", "start", "restart"}

_action_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.kube = kube.make_client()
    app.state.http = httpx.AsyncClient()
    yield
    await app.state.kube.aclose()
    await app.state.http.aclose()


app = FastAPI(title="subgen-ui", lifespan=lifespan)


def _state(dep: dict | None, pod: dict | None) -> str:
    if dep is None:
        return "unknown"
    if dep["desired"] == 0:
        return "stopping" if pod else "stopped"
    return "running" if dep["ready"] >= 1 else "starting"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/status")
async def status(request: Request):
    dep, pod, events = await asyncio.gather(
        kube.deployment(request.app.state.kube),
        kube.newest_pod(request.app.state.kube),
        loki.fetch_events(request.app.state.http),
        return_exceptions=True,
    )
    problems = []
    for label, value in (("kubernetes deployment", dep), ("kubernetes pods", pod), ("loki", events)):
        if isinstance(value, Exception):
            problems.append(f"{label}: {type(value).__name__}: {value}")
    dep = None if isinstance(dep, Exception) else dep
    pod = None if isinstance(pod, Exception) else pod
    queue = (
        None
        if isinstance(events, Exception)
        else summarize(events, time.time_ns(), pod["name"] if pod else None)
    )
    return {
        "state": _state(dep, pod),
        "replicas": dep,
        "pod": pod,
        "queue": queue,
        "problems": problems,
        "generated_at": int(time.time()),
    }


@app.post("/api/{action}")
async def act(action: str, request: Request):
    if action not in ACTIONS:
        raise HTTPException(404, "unknown action")
    if request.headers.get(CSRF_HEADER[0]) != CSRF_HEADER[1]:
        raise HTTPException(400, f"missing {CSRF_HEADER[0]} header")
    # Authelia forwards the authenticated username; log it for the audit trail.
    user = request.headers.get("remote-user", "unknown")
    safe_action = action.replace("\r", "").replace("\n", "")
    safe_user = user.replace("\r", "").replace("\n", "")
    client = request.app.state.kube
    async with _action_lock:
        try:
            desired = (await kube.deployment(client))["desired"]
            if action == "stop":
                await kube.scale(client, 0)
            elif action == "start":
                await kube.scale(client, 1)
            elif desired == 0:
                raise HTTPException(409, "subgen is stopped; use start")
            else:
                await kube.restart(client)
        except (httpx.HTTPError, OSError) as exc:
            log.error("action %s by %s failed: %s", safe_action, safe_user, exc)
            raise HTTPException(502, f"kubernetes API error: {exc}") from exc
    log.info("action %s by %s (was desired=%s)", safe_action, safe_user, desired)
    return {"ok": True, "action": action}


def serve() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=settings.listen_port)

import time

import httpx

from subgen_ui.config import settings
from subgen_ui.parse import Event

QUERY = (
    '{{namespace="{ns}", container="app", pod=~"{dep}-.*"}} '
    '|~ "WORKER (START|FINISH)|Error processing or transcribing"'
)
MAX_ENTRIES = 5000  # Loki's default max_entries_limit


async def fetch_events(client: httpx.AsyncClient) -> list[Event]:
    """Subgen's job/error log lines for the last window, ascending by time."""
    end = time.time_ns()
    resp = await client.get(
        f"{settings.loki_url}/loki/api/v1/query_range",
        params={
            "query": QUERY.format(ns=settings.namespace, dep=settings.deployment),
            "start": end - settings.window_hours * 3600 * 10**9,
            "end": end,
            "limit": MAX_ENTRIES,
            # backward so a full page keeps the newest lines, not the oldest
            "direction": "backward",
        },
        timeout=10,
    )
    resp.raise_for_status()
    events = [
        Event(int(ts), stream["stream"].get("pod", ""), line)
        for stream in resp.json()["data"]["result"]
        for ts, line in stream["values"]
    ]
    events.sort(key=lambda e: e.ts_ns)
    return events

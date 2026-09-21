"""Turn subgen's log lines into a status summary.

subgen exposes no status API, so queue depth, the current job and failures
only exist as log lines, e.g.:

  WORKER START :[TRANSCRIBE] Show.S01E01.mkv | Jobs: 1 processing, 63 queued
  WORKER FINISH: [TRANSCRIBE] Show.S01E01.mkv in 4m 58s | Remaining: 64 queued
  Error processing or transcribing /data/media/tv/Show/Show.S01E01.mkv in English: <reason>

A failed job logs the error line and then a normal FINISH line, so a FINISH
counts as failed when an error for the same file preceded it.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

START_RE = re.compile(
    r"WORKER START\s*:\s*\[(?P<kind>[A-Z_]+)\]\s*(?P<name>.+?)\s*\|\s*"
    r"Jobs:\s*(?P<proc>\d+) processing,\s*(?P<queued>\d+) queued"
)
FINISH_RE = re.compile(
    r"WORKER FINISH:\s*\[(?P<kind>[A-Z_]+)\]\s*(?P<name>.+?)\s+in\s+"
    r"(?P<dur>\d+m \d+s)\s*\|\s*Remaining:\s*(?P<queued>\d+) queued"
)
ERROR_RE = re.compile(
    r"Error processing or transcribing (?P<path>.+?) in (?P<lang>[^:]+?): (?P<reason>.*)$"
)

RATE_WINDOW_NS = 3600 * 10**9
MAX_RECENT_ERRORS = 8
MAX_REASONS = 5


@dataclass(frozen=True)
class Event:
    ts_ns: int
    pod: str
    line: str


def summarize(events: list[Event], now_ns: int, current_pod: str | None = None) -> dict:
    """Summarize events (ascending by time).

    current_pod is the pod that is running now (from the k8s API). Everything
    is computed from that pod's own log lines: an older pod's last line
    describes a queue that no longer exists, and its failures say nothing
    about whether the current pod (possibly running a fix) is healthy. With no
    current pod, the pod that logged last is used.

    Throughput and ETA are based on TRANSCRIBE jobs only. The queue also holds
    DETECT_LANGUAGE jobs that finish in about a second and would inflate the
    rate, so the ETA is an upper bound while detect jobs are still queued.
    """
    if current_pod is None and events:
        current_pod = events[-1].pod
    if current_pod is not None:
        events = [e for e in events if e.pod == current_pod]

    running: dict | None = None
    queued_by_pod: dict[str, int] = {}
    pending_errors: set[str] = set()
    finishes: list[tuple[int, str, bool]] = []
    errors: list[dict] = []

    for ev in events:
        if m := START_RE.search(ev.line):
            running = {"kind": m["kind"], "name": m["name"], "since_ns": ev.ts_ns, "pod": ev.pod}
            queued_by_pod[ev.pod] = int(m["queued"])
        elif m := FINISH_RE.search(ev.line):
            if running and running["name"] == m["name"] and running["pod"] == ev.pod:
                running = None
            queued_by_pod[ev.pod] = int(m["queued"])
            failed = m["name"] in pending_errors
            pending_errors.discard(m["name"])
            finishes.append((ev.ts_ns, m["kind"], failed))
        elif m := ERROR_RE.search(ev.line):
            name = m["path"].rsplit("/", 1)[-1]
            pending_errors.add(name)
            errors.append({"ts_ns": ev.ts_ns, "name": name, "reason": m["reason"].strip()})

    current = running

    recent = [f for f in finishes if f[0] >= now_ns - RATE_WINDOW_NS and f[1] == "TRANSCRIBE"]
    rate = None
    if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
        rate = (len(recent) - 1) / ((recent[-1][0] - recent[0][0]) / 3600e9)

    queued = queued_by_pod.get(current_pod) if current_pod else None
    eta_hours = queued / rate if queued and rate else None

    return {
        "queued": queued,
        "current": (
            {
                "kind": current["kind"],
                "name": current["name"],
                "elapsed_s": max(0, (now_ns - current["since_ns"]) // 10**9),
            }
            if current
            else None
        ),
        "last_hour": {
            "finished": len(recent),
            "failed": sum(1 for *_, failed in recent if failed),
            "per_hour": rate,
            "eta_hours": eta_hours,
        },
        "reasons": Counter(e["reason"] for e in errors).most_common(MAX_REASONS),
        "recent_errors": [
            {"age_s": max(0, (now_ns - e["ts_ns"]) // 10**9), "name": e["name"], "reason": e["reason"]}
            for e in errors[-MAX_RECENT_ERRORS:][::-1]
        ],
    }

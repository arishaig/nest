"""Zigbee2MQTT diagnostics, reached through Home Assistant's WebSocket API.

nest-mcp already holds an admin HA token, and HA's MQTT integration can both
subscribe (``mqtt/subscribe``) and publish (``mqtt.publish``). Going through HA
means no broker credentials and no firewall hole from the cluster to
mosquitto on the IoT VLAN.

Z2M retains ``bridge/info``, ``bridge/devices``, ``bridge/groups`` and
``bridge/state``, so a short subscription returns the whole picture. Loki only
has what the add-on's console flushed, which can go silent for hours while Z2M
is healthy; ``z2m_probe`` listens live instead.
"""
import asyncio
import contextlib
import json
import re
import time
import uuid

from mcp.server.mcpserver import MCPServer
from websockets.asyncio.client import connect

from nest_mcp import config
from nest_mcp.http_client import make_client
from nest_mcp.tools.homeassistant import HARefusal, _WS_MAX_SIZE, _ws_session

# Bridge requests that are safe to expose. Anything that edits the Zigbee
# network (device/remove, group/*, options, permit_join) is deliberately absent.
_REQUESTS = {"health_check", "coordinator_check", "restart"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# A friendly name goes straight into a topic: no wildcards, no empty segments.
_BAD_TARGET = re.compile(r"[#+]|^/|/$|//|^$")
_LEVEL = re.compile(r"\x1b\[\d+m(error|warning|info|debug)\x1b\[\d+m:")
_ADDON_LABEL = "app_45df7312_zigbee2mqtt"


def _parse(payload):
    """MQTT payloads arrive as strings; decode JSON where possible."""
    if not isinstance(payload, str):
        return payload
    try:
        return json.loads(payload)
    except ValueError:
        return payload


class _Mqtt:
    """One HA WebSocket used as an MQTT client: subscribe, publish, collect."""

    def __init__(self, ws):
        self._ws = ws
        self._id = 0
        self.events: list[dict] = []

    async def _recv(self, timeout: float) -> dict:
        msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=timeout))
        ev = msg.get("event")
        if msg.get("type") == "event" and isinstance(ev, dict) and "topic" in ev:
            self.events.append({
                "topic": ev["topic"],
                "payload": _parse(ev.get("payload")),
                "retain": bool(ev.get("retain")),
                "at": time.monotonic(),
            })
        return msg

    async def request(self, payload: dict):
        self._id += 1
        mid = self._id
        await self._ws.send(json.dumps({"id": mid, **payload}))
        while True:
            msg = await self._recv(10)
            if msg.get("id") == mid and msg.get("type") == "result":
                if not msg.get("success"):
                    err = msg.get("error", {})
                    raise HARefusal(f"Home Assistant rejected {payload['type']}: {err.get('message', msg)}")
                return msg.get("result")

    async def subscribe(self, topic: str) -> None:
        await self.request({"type": "mqtt/subscribe", "topic": topic})

    async def publish(self, topic: str, payload) -> None:
        await self.request({
            "type": "call_service", "domain": "mqtt", "service": "publish",
            "service_data": {"topic": topic,
                             "payload": payload if isinstance(payload, str) else json.dumps(payload)},
        })

    async def collect(self, seconds: float, until=None) -> None:
        """Read events for ``seconds``; stop early once ``until(events)`` is true."""
        deadline = time.monotonic() + seconds
        while until is None or not until(self.events):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await self._recv(remaining)
            except asyncio.TimeoutError:
                return


@contextlib.asynccontextmanager
async def _mqtt_session():
    """Open an authenticated HA WebSocket; yield an ``_Mqtt``."""
    url = config.homeassistant.url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    async with connect(url, open_timeout=10, max_size=_WS_MAX_SIZE) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise ValueError(f"unexpected greeting from Home Assistant: {hello}")
        await ws.send(json.dumps({"type": "auth", "access_token": config.homeassistant.token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            raise ValueError(f"Home Assistant rejected the token: {reply.get('message', reply)}")
        yield _Mqtt(ws)


def _latest(events: list[dict], topic: str):
    """Payload of the newest event on ``topic``, or None."""
    for ev in reversed(events):
        if ev["topic"] == topic:
            return ev["payload"]
    return None


def _check_target(target: str) -> None:
    if _BAD_TARGET.search(target):
        raise HARefusal(f"invalid Zigbee2MQTT name {target!r}: no wildcards or empty path segments")


def _summarise(events: list[dict], base: str, ha_entities: dict[str, list[str]]) -> dict:
    info = _latest(events, f"{base}/bridge/info") or {}
    devices = _latest(events, f"{base}/bridge/devices") or []
    groups = _latest(events, f"{base}/bridge/groups") or []
    state = _latest(events, f"{base}/bridge/state")
    avail: dict[str, str] = {}
    for ev in events:
        m = re.fullmatch(re.escape(base) + r"/(.+)/availability", ev["topic"])
        if m and m.group(1) != "bridge":
            p = ev["payload"]
            avail[m.group(1)] = p.get("state") if isinstance(p, dict) else str(p)

    by_ieee = {d.get("ieee_address"): d for d in devices if isinstance(d, dict)}
    out_devices = []
    for d in by_ieee.values():
        if d.get("type") == "Coordinator":
            continue
        name = d.get("friendly_name")
        defn = d.get("definition") or {}
        out_devices.append({
            "friendly_name": name,
            "ieee_address": d.get("ieee_address"),
            "type": d.get("type"),
            "power_source": d.get("power_source"),
            "vendor": defn.get("vendor"),
            "model": defn.get("model"),
            "interview_completed": d.get("interview_completed"),
            "disabled": d.get("disabled", False),
            "availability": avail.get(name, "unknown"),
            "ha_entities": ha_entities.get(d.get("ieee_address"), []),
        })
    out_devices.sort(key=lambda d: (d["availability"] == "online", d["friendly_name"] or ""))

    out_groups = []
    for g in groups:
        members = [by_ieee.get(m.get("ieee_address"), {}).get("friendly_name", m.get("ieee_address"))
                   for m in g.get("members", [])]
        out_groups.append({
            "id": g.get("id"),
            "friendly_name": g.get("friendly_name"),
            "member_count": len(members),
            "members": members,
            "scene_count": len(g.get("scenes", [])),
            "ha_entities": ha_entities.get(f"group:{g.get('id')}", []),
        })
    out_groups.sort(key=lambda g: g["id"] if isinstance(g["id"], int) else 0)

    coord = info.get("coordinator") or {}
    cfg = info.get("config") or {}
    return {
        "bridge_state": state.get("state") if isinstance(state, dict) else state,
        "version": info.get("version"),
        "base_topic": base,
        "coordinator": {"type": coord.get("type"), "meta": coord.get("meta")},
        "adapter": (cfg.get("serial") or {}).get("adapter"),
        "network": {k: (info.get("network") or {}).get(k) for k in ("channel", "pan_id")},
        "availability_enabled": bool(cfg.get("availability")),
        "device_count": len(out_devices),
        "devices_not_online": [d["friendly_name"] for d in out_devices if d["availability"] != "online"],
        "group_count": len(out_groups),
        "groups": out_groups,
        "devices": out_devices,
    }


async def _ha_entities_by_z2m(base: str) -> tuple[dict[str, list[str]], dict]:
    """Map Z2M devices (by ieee) and groups (as ``group:<id>``) to their HA entity ids.

    Discovery unique_ids are expected to look like ``<ieee|group id>_<component>_<base topic>``.
    Entities that don't match are counted and sampled in the returned stats instead of
    guessed at, so a wrong assumption shows up in the output. ``ha_entity_registry`` shows
    the real unique_ids.
    """
    pat = re.compile(r"^(?P<id>[^_]+)_(?P<comp>[a-z_]+?)_" + re.escape(base) + r"$")
    out: dict[str, list[str]] = {}
    mqtt_total = 0
    unmatched: list[dict] = []
    async with _ws_session() as call:
        for ent in await call({"type": "config/entity_registry/list"}):
            if ent.get("platform") != "mqtt":
                continue
            mqtt_total += 1
            m = pat.match(ent.get("unique_id") or "")
            if not m:
                unmatched.append({"entity_id": ent["entity_id"], "unique_id": ent.get("unique_id")})
                continue
            ident = m["id"]
            key = ident if ident.startswith("0x") else f"group:{ident}"
            out.setdefault(key, []).append(ent["entity_id"])
    stats = {"mqtt_entities": mqtt_total, "matched": mqtt_total - len(unmatched),
             "unmatched": len(unmatched), "unmatched_examples": unmatched[:5]}
    return out, stats


def register(mcp: MCPServer) -> None:

    @mcp.tool()
    async def z2m_bridge_info(base_topic: str = "zigbee2mqtt", settle_seconds: float = 3.0) -> dict:
        """Zigbee2MQTT overview, read live from the broker through Home Assistant. Read-only. Returns the bridge state and version, coordinator type and adapter driver (`adapter`: ezsp, zstack, ember...), network channel, every device (type, power source, availability, model) with unavailable ones listed first, and every Zigbee group with its members. `ha_entities` on each device/group says which HA entities (e.g. light.main_bathroom_vanity) it drives, so you can tell whether an HA light is a group or a single bulb. That mapping relies on HA's MQTT unique_id format; `ha_mapping` reports how many MQTT entities matched and examples that didn't (or an error), so check it before trusting an empty `ha_entities`. Use `group_count` and the `groups` list when Z2M logs 'No more available slots MulticastId subscription'. `settle_seconds` is how long to wait for retained messages and per-device availability."""
        base = base_topic.strip("/")
        async with _mqtt_session() as mqtt:
            await mqtt.subscribe(f"{base}/bridge/#")
            await mqtt.subscribe(f"{base}/+/availability")
            await mqtt.collect(min(max(settle_seconds, 0.5), 15))
            events = mqtt.events
        if _latest(events, f"{base}/bridge/info") is None:
            raise HARefusal(
                f"no {base}/bridge/info received: Zigbee2MQTT isn't publishing, or base_topic isn't {base!r}")
        try:
            ha_entities, mapping = await _ha_entities_by_z2m(base)
        except Exception as err:  # the mapping is a convenience; the bridge data still stands
            ha_entities, mapping = {}, {"error": f"{type(err).__name__}: {err}"}
        return {**_summarise(events, base, ha_entities), "ha_mapping": mapping}

    @mcp.tool()
    async def z2m_probe(target: str, mode: str = "get", payload: dict | None = None,
                        seconds: float = 8.0, base_topic: str = "zigbee2mqtt") -> dict:
        """Send one command to a Zigbee2MQTT device or group and watch what happens, live. `target` is the Z2M friendly name (see z2m_bridge_info). mode='get' (default, non-invasive) asks the device to report its state; mode='set' sends `payload` as a real command and CHANGES the device (e.g. {"state": "TOGGLE"}). Returns whether a fresh (non-retained) state message came back and how long it took, plus every Z2M log line emitted during the window (errors and warnings first). This is what tells 'the coordinator rejects or times out the command' (errors in `log`, e.g. multicast slots, EZSP/UART timeouts) from 'the device never answers' (no errors, no reply). Probe a bulb and the group it belongs to and compare."""
        _check_target(target)
        if mode not in ("get", "set"):
            raise HARefusal("mode must be 'get' or 'set'")
        if mode == "set" and not payload:
            raise HARefusal("mode='set' needs a payload, e.g. {\"state\": \"TOGGLE\"}")
        base = base_topic.strip("/")
        sent = payload or {"state": ""}
        async with _mqtt_session() as mqtt:
            await mqtt.subscribe(f"{base}/bridge/logging")
            await mqtt.subscribe(f"{base}/{target}")
            await mqtt.collect(1.0)  # drain the retained state so it isn't mistaken for a reply
            baseline = len(mqtt.events)
            sent_at = time.monotonic()
            await mqtt.publish(f"{base}/{target}/{mode}", sent)
            await mqtt.collect(
                min(max(seconds, 1), 30),
                until=lambda evs: any(e["topic"] == f"{base}/{target}" and not e["retain"]
                                      for e in evs[baseline:]))
            fresh = mqtt.events[baseline:]
        replies = [e for e in fresh if e["topic"] == f"{base}/{target}" and not e["retain"]]
        log = []
        for e in fresh:
            if e["topic"] != f"{base}/bridge/logging" or not isinstance(e["payload"], dict):
                continue
            log.append({"level": e["payload"].get("level"),
                        "message": _ANSI.sub("", str(e["payload"].get("message", ""))),
                        "seconds_after_send": round(e["at"] - sent_at, 2)})
        log.sort(key=lambda r: r["level"] not in ("error", "warning"))
        errors = [r for r in log if r["level"] in ("error", "warning")]
        if replies:
            verdict = "replied"
        elif errors:
            verdict = "no_reply_and_errors_logged"
        else:
            verdict = "no_reply_no_errors"
        return {
            "target": target, "sent": {"topic": f"{base}/{target}/{mode}", "payload": sent},
            "verdict": verdict,
            "reply_seconds": round(replies[0]["at"] - sent_at, 2) if replies else None,
            "reply": replies[0]["payload"] if replies else None,
            "log": log[:40],
        }

    @mcp.tool()
    async def z2m_bridge_request(command: str, payload: dict | None = None, seconds: float = 12.0,
                                 base_topic: str = "zigbee2mqtt") -> dict:
        """Send a Zigbee2MQTT bridge request and return its response. Allowed commands: `health_check` (read-only), `coordinator_check` (read-only; not every adapter supports it, an error response is informative), and `restart` ([DESTRUCTIVE]: restarts Z2M, which re-initialises the coordinator and its multicast table; every Zigbee device is unreachable for about a minute, so confirm with the user first). Network edits (device removal, groups, options) are not available here."""
        if command not in _REQUESTS:
            raise HARefusal(f"command must be one of {sorted(_REQUESTS)}")
        base = base_topic.strip("/")
        txn = uuid.uuid4().hex
        topic = f"{base}/bridge/response/{command}"
        body = {**(payload or {}), "transaction": txn}
        async with _mqtt_session() as mqtt:
            await mqtt.subscribe(topic)
            await mqtt.collect(0.5)  # nothing is retained on response topics, but settle anyway
            await mqtt.publish(f"{base}/bridge/request/{command}", body)
            await mqtt.collect(
                min(max(seconds, 1), 60),
                until=lambda evs: any(e["topic"] == topic and isinstance(e["payload"], dict)
                                      and e["payload"].get("transaction") == txn for e in evs))
            events = mqtt.events
        resp = next((e["payload"] for e in events if e["topic"] == topic and isinstance(e["payload"], dict)
                     and e["payload"].get("transaction") == txn), None)
        if resp is None:
            return {"command": command, "response": None,
                    "note": "no response before the timeout: Z2M may be hung or restarting"}
        return {"command": command, "status": resp.get("status"), "error": resp.get("error"),
                "data": resp.get("data")}

    @mcp.tool()
    async def z2m_log_summary(since_minutes: int = 360, top: int = 15) -> dict:
        """Zigbee2MQTT add-on log lines from Loki, de-noised: stack frames dropped, ANSI colours stripped, timestamps and numbers normalised so repeats collapse into one row with a count and first/last time. Read-only. `levels_seen` shows which log levels the add-on emits: if there is no info/debug at all it runs at warning level, so a quiet stream means no problems, not a shipping gap. Loki only holds what the console flushed, so use z2m_probe for a live check."""
        since_minutes = min(max(1, since_minutes), 10080)
        now = int(time.time())
        async with make_client(config.loki.url) as client:
            resp = await client.get("/loki/api/v1/query_range", params={
                "query": f'{{syslog_identifier="{_ADDON_LABEL}"}}',
                "start": now - since_minutes * 60, "end": now, "limit": 5000, "direction": "forward"})
            resp.raise_for_status()
            streams = resp.json().get("data", {}).get("result", [])
        rows: dict[str, dict] = {}
        levels: dict[str, int] = {}
        total = 0
        for stream in streams:
            for ts_ns, line in stream.get("values", []):
                lvl = _LEVEL.search(line)
                if lvl:
                    levels[lvl[1]] = levels.get(lvl[1], 0) + 1
                line = _ANSI.sub("", line)
                total += 1
                if line.lstrip().startswith("at ") or not line.strip():
                    continue
                key = re.sub(r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\]\s*", "", line).strip()
                key = re.sub(r'\{"type":"Buffer".*?\}\}?', "{buffer}", key)
                key = re.sub(r"\b0x[0-9a-fA-F]+\b|\b\d+\b", "#", key)[:300]
                ts = int(ts_ns) / 1e9
                row = rows.setdefault(key, {"count": 0, "first": ts, "last": ts, "example": line[:300]})
                row["count"] += 1
                row["first"], row["last"] = min(row["first"], ts), max(row["last"], ts)
        fmt = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
        ranked = sorted(rows.values(), key=lambda r: ("error" not in r["example"], -r["count"]))[:top]
        newest = max((float(v[0]) / 1e9 for s in streams for v in s.get("values", [])), default=None)
        return {
            "lines_scanned": total, "distinct_messages": len(rows), "levels_seen": levels,
            "newest_line": fmt(newest) if newest else None,
            "messages": [{"count": r["count"], "first": fmt(r["first"]), "last": fmt(r["last"]),
                          "example": r["example"]} for r in ranked],
        }

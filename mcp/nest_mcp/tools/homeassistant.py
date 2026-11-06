import asyncio
import contextlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from websockets.asyncio.client import connect
from nest_mcp import config
from nest_mcp.http_client import make_client


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.homeassistant.token}"}


# --- Automation audit -------------------------------------------------------
# Each rule below is a pattern that has actually broken an automation here
# without any error. Add new ones as they turn up.

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
_SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_OPENING_CLASSES = {"door", "window", "opening", "garage_door"}
# A script's key in scripts.yaml; it's also used as a URL path segment.
_SCRIPT_ID_RE = re.compile(r"^[a-z0-9_]+$")
# Renders the area list, the device ids that no longer resolve, and each
# live device's entities (to suggest a state-trigger replacement) in one call.
_REGISTRY_TEMPLATE = (
    "{%- set ns = namespace(m=[], e=[]) -%}"
    "{%- for d in dev_ids -%}{%- if device_attr(d, 'name') is none -%}"
    "{%- set ns.m = ns.m + [d] -%}"
    "{%- else -%}{%- set ns.e = ns.e + [[d, device_entities(d)]] -%}{%- endif -%}{%- endfor -%}"
    "{{ {'areas': areas(), 'missing_devices': ns.m, 'device_entities': ns.e} | tojson }}"
)


async def _post_config(client, path: str, config: dict) -> None:
    resp = await client.post(path, json=config)
    if resp.status_code == 400:
        # HA's validation message says what's wrong with the config.
        raise ValueError(f"Home Assistant rejected the config: {resp.json().get('message', resp.text)}")
    resp.raise_for_status()


# --- WebSocket API ----------------------------------------------------------
# Helpers (input_boolean etc.) can only be created over HA's WebSocket API;
# there is no REST endpoint for them. It needs an admin token.

class HAWebSocketError(ValueError):
    """A failed WebSocket command; ``code`` is HA's error code (e.g. ``config_not_found``)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


_WS_MAX_SIZE = 64 * 1024 * 1024


@contextlib.asynccontextmanager
async def _ws_session():
    """Open an authenticated HA WebSocket; yield ``call(payload) -> result``."""
    url = config.homeassistant.url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    # Registry and state dumps (get_states, entity_registry/list) exceed the
    # websockets library's 1 MiB default frame limit on a real install.
    async with connect(url, open_timeout=10, max_size=_WS_MAX_SIZE) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise ValueError(f"unexpected greeting from Home Assistant: {hello}")
        await ws.send(json.dumps({"type": "auth", "access_token": config.homeassistant.token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            raise ValueError(f"Home Assistant rejected the token: {reply.get('message', reply)}")
        next_id = 0

        async def call(payload: dict):
            nonlocal next_id
            next_id += 1
            await ws.send(json.dumps({"id": next_id, **payload}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if msg.get("id") != next_id:
                    continue
                if not msg.get("success"):
                    err = msg.get("error", {})
                    raise HAWebSocketError(
                        f"Home Assistant rejected {payload['type']}: {err.get('message', msg)}",
                        code=err.get("code", ""),
                    )
                return msg.get("result")

        yield call


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _is_template(value) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _collect_refs(node, refs: dict) -> None:
    """Walk an automation config collecting literal entity/device/area/service references.

    Templated values are skipped: they only resolve at runtime.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                refs["entities"].update(
                    e for e in _as_list(value)
                    if isinstance(e, str) and "." in e and not _is_template(e)
                )
            elif key == "device_id":
                refs["devices"].update(d for d in _as_list(value) if isinstance(d, str) and not _is_template(d))
            elif key == "area_id":
                refs["areas"].update(a for a in _as_list(value) if isinstance(a, str) and not _is_template(a))
            elif key in ("action", "service") and isinstance(value, str) and _SERVICE_RE.match(value):
                # Notification button action ids share the
                # `action` key but never look like domain.service.
                refs["services"].add(value)
            _collect_refs(value, refs)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, refs)


def _triggers(cfg: dict) -> list:
    # HA accepts both the current `triggers` key and the legacy `trigger`.
    return _as_list(cfg.get("triggers", cfg.get("trigger")))


def _lint_automation(cfg: dict, states: dict[str, dict]) -> list[dict]:
    """Pattern checks on one automation config (states only supply device_class)."""
    findings = []
    for t in _triggers(cfg):
        if not isinstance(t, dict):
            continue
        kind = t.get("trigger", t.get("platform"))
        if kind == "device":
            # Re-pairing a device gives it a new device_id and the trigger
            # silently never fires again; this has broken lights here before.
            findings.append({
                "severity": "warning", "rule": "device_trigger",
                "detail": "Device trigger pins a device_id that changes on re-pair; use a state trigger on the entity.",
                "device_id": t.get("device_id"), "domain": t.get("domain"),
            })
        # A door sensor that drops off Zigbee and comes back reporting open
        # never goes off -> on, so a from: off trigger misses it. Only for
        # door/window-type sensors, where a missed open matters; on presence
        # sensors a from: guard is often deliberate (a reconnect shouldn't
        # count as presence). from: null means "any state" in HA, so it's fine.
        openings = [
            e for e in _as_list(t.get("entity_id"))
            if isinstance(e, str)
            and states.get(e, {}).get("attributes", {}).get("device_class") in _OPENING_CLASSES
        ]
        from_states = [str(f) for f in _as_list(t.get("from"))]
        if (
            kind == "state"
            and openings
            and from_states
            and "to" in t
            and not {"unavailable", "unknown"} & set(from_states)
        ):
            findings.append({
                "severity": "warning", "rule": "from_state_misses_recovery",
                "detail": f"State trigger on {', '.join(openings)} requires from: {t['from']}; if the sensor drops off "
                          "and comes back already in the new state, this never fires. Drop `from`.",
            })
    return findings


def _audit(
    automations: list[dict],
    configs: dict[str, dict | None],
    states: dict[str, dict],
    services: set[str],
    areas: set[str],
    missing_devices: set[str],
    device_entities: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Check every automation's pattern lint plus its references against live HA."""
    findings = []
    device_entities = device_entities or {}

    def add(auto, severity, rule, detail, **extra):
        findings.append({"automation_id": auto["automation_id"], "alias": auto["alias"],
                         "severity": severity, "rule": rule, "detail": detail, **extra})

    for auto in automations:
        cfg = configs.get(auto["automation_id"])
        if auto["state"] == "unavailable" or cfg is None:
            add(auto, "error", "config_invalid",
                "HA couldn't load this automation (entity unavailable or config not found); it never runs.")
            continue
        if auto["state"] == "off":
            # Turned off on purpose; its references don't matter until it's back on.
            add(auto, "info", "disabled", "Automation is turned off.")
            continue
        for f in _lint_automation(cfg, states):
            extra = {}
            if f["rule"] == "device_trigger":
                # The device's entities in the trigger's domain are the
                # candidates for the replacement state trigger.
                extra["suggested_entities"] = [
                    e for e in device_entities.get(f["device_id"], [])
                    if not f["domain"] or e.startswith(f"{f['domain']}.")
                ]
            add(auto, f["severity"], f["rule"], f["detail"], **extra)

        refs = {"entities": set(), "devices": set(), "areas": set(), "services": set()}
        _collect_refs(cfg, refs)
        for e in sorted(refs["entities"]):
            s = states.get(e)
            if s is None:
                add(auto, "error", "missing_entity", f"{e} doesn't exist.")
            elif s["state"] in ("unavailable", "unknown"):
                add(auto, "warning", "unavailable_entity",
                    f"{e} is {s['state']} (since {s.get('last_changed', '?')}); this automation can't see it change.")
        for d in sorted(refs["devices"] & missing_devices):
            add(auto, "error", "missing_device", f"device_id {d} no longer exists (device re-paired or removed).")
        for a in sorted(refs["areas"] - areas):
            add(auto, "error", "missing_area", f"Area {a} doesn't exist.")
        for svc in sorted(refs["services"] - services):
            add(auto, "error", "missing_service", f"Action {svc} doesn't exist (renamed notify target or removed integration?).")

        if not auto.get("last_triggered"):
            add(auto, "info", "never_triggered", "Enabled but has never fired.")

    return sorted(findings, key=lambda f: (_SEVERITY_ORDER[f["severity"]], f["alias"], f["rule"]))


# --- State history ------------------------------------------------------------

_HISTORY_MAX_HOURS = 24 * 10  # HA's recorder keeps 10 days by default.


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _history_runs(rows: list[dict], end: datetime) -> list[dict]:
    """Turn HA's minimal history rows into runs: state, start, and how long it held."""
    runs = []
    for i, row in enumerate(rows):
        start = _parse_ts(row["last_changed"])
        until = _parse_ts(rows[i + 1]["last_changed"]) if i + 1 < len(rows) else end
        runs.append({
            "state": row["state"],
            "at": start.isoformat(timespec="seconds"),
            "seconds": round((until - start).total_seconds(), 1),
        })
    # HA reports the state at the window start as the first row, so the first run's
    # real start and the last run's real end lie outside the window: both are clipped.
    if runs:
        runs[0]["clipped"] = True
        runs[-1]["clipped"] = True
    return runs


def _as_number(state: str) -> float | None:
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def _summarise_runs(runs: list[dict]) -> dict:
    """Per-state duration stats; numeric sensors get one value summary instead.

    A numeric sensor (temperature, pressure, lux) has a new state on almost every
    reading, so a bucket per state would be as long as the history itself.
    """
    complete = [r for r in runs if not r.get("clipped")]
    numeric = [(v, r["seconds"]) for r in complete if (v := _as_number(r["state"])) is not None]
    other = [r for r in complete if _as_number(r["state"]) is None]
    if numeric and len(numeric) >= len(other):
        values = sorted(v for v, _ in numeric)
        total = sum(secs for _, secs in numeric)
        summary: dict = {"numeric": {
            "readings": len(values),
            "min": round(values[0], 3),
            "max": round(values[-1], 3),
            "median": round(values[len(values) // 2], 3),
            # Weighted by how long each reading held, so a burst of quick updates doesn't skew it.
            "time_weighted_mean": round(sum(v * secs for v, secs in numeric) / total, 3) if total else None,
        }}
    else:
        summary, other = {}, complete
    by_state: dict[str, list[float]] = {}
    for r in other:
        by_state.setdefault(r["state"], []).append(r["seconds"])
    summary.update({
        state: {"count": len(secs), "min_s": min(secs), "max_s": max(secs),
                "median_s": sorted(secs)[len(secs) // 2]}
        for state, secs in by_state.items()
    })
    return summary


def register(mcp: MCPServer) -> None:

    @mcp.tool()
    async def ha_list_entities(domain: str = "") -> list[dict]:
        """List Home Assistant entities. Optionally filter by domain (e.g. 'sensor', 'switch', 'light', 'climate')."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.get("/api/states")
            resp.raise_for_status()
            states = resp.json()
            if domain:
                states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]
            return [
                {
                    "entity_id": s["entity_id"],
                    "state": s["state"],
                    "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
                    "last_changed": s.get("last_changed", ""),
                }
                for s in sorted(states, key=lambda x: x["entity_id"])
            ]

    @mcp.tool()
    async def ha_get_state(entity_id: str) -> dict:
        """Get the current state and all attributes of a specific Home Assistant entity."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.get(f"/api/states/{entity_id}")
            resp.raise_for_status()
            s = resp.json()
            return {
                "entity_id": s["entity_id"],
                "state": s["state"],
                "attributes": s.get("attributes", {}),
                "last_changed": s.get("last_changed", ""),
                "last_updated": s.get("last_updated", ""),
            }

    @mcp.tool()
    async def ha_history(
        entity_ids: list[str],
        hours: float = 24,
        start: str = "",
        end: str = "",
        state: str = "",
        min_seconds: float = 0,
        max_seconds: float = 0,
        below: float | None = None,
        above: float | None = None,
        limit: int = 300,
    ) -> dict:
        """State history of Home Assistant entities from the recorder (default retention 10 days).

        Each entity gets a list of runs: the state, when it started (UTC) and how many seconds it held
        until the next change. The first and last runs are cut off by the window edges and marked
        clipped. A per-state summary (count, min, median, max seconds) covers the unclipped runs in
        the window, before filtering; numeric sensors (temperature, pressure, lux) instead get one
        `numeric` summary (readings, min, max, median, time-weighted mean) plus any non-numeric
        states such as unavailable.
        Window: the last `hours`, or explicit ISO-8601 `start`/`end` (UTC unless an offset is given).
        Filter runs with `state` (e.g. "off") and `min_seconds`/`max_seconds` (0 = no bound), e.g.
        to find short sensor dropouts. For numeric sensors, `below`/`above` keep only runs whose value
        is under/over a bound (e.g. below=35). `limit` caps runs returned per entity (newest kept).
        """
        if not entity_ids:
            raise ValueError("entity_ids must name at least one entity")
        now = datetime.now(timezone.utc)
        t_end = _parse_ts(end) if end else now
        t_start = _parse_ts(start) if start else t_end - timedelta(hours=hours)
        if t_start >= t_end:
            raise ValueError("start must be before end")
        if t_end - t_start > timedelta(hours=_HISTORY_MAX_HOURS):
            raise ValueError(f"window is longer than {_HISTORY_MAX_HOURS} h; HA keeps 10 days by default")
        params = {
            "filter_entity_id": ",".join(entity_ids),
            "end_time": t_end.isoformat(),
            "minimal_response": "",
            "no_attributes": "",
        }
        async with make_client(config.homeassistant.url, headers=_headers(), timeout=30.0) as client:
            resp = await client.get(f"/api/history/period/{t_start.isoformat()}", params=params)
            resp.raise_for_status()
            series = resp.json()
        # HA returns one list per entity; with minimal_response only the first row carries entity_id.
        out = {}
        for rows in series:
            if not rows:
                continue
            entity_id = rows[0]["entity_id"]
            runs = _history_runs(rows, min(t_end, now))
            summary = _summarise_runs(runs)
            picked = [
                r for r in runs
                if (not state or r["state"] == state)
                and r["seconds"] >= min_seconds
                and (not max_seconds or r["seconds"] <= max_seconds)
                and (below is None or ((v := _as_number(r["state"])) is not None and v < below))
                and (above is None or ((v := _as_number(r["state"])) is not None and v > above))
            ]
            out[entity_id] = {
                "summary": summary,
                "matched": len(picked),
                "runs": picked[-limit:] if limit > 0 else picked,
            }
        missing = [e for e in entity_ids if e not in out]
        return {
            "start": t_start.isoformat(timespec="seconds"),
            "end": t_end.isoformat(timespec="seconds"),
            "entities": out,
            **({"no_history": missing} if missing else {}),
        }

    @mcp.tool()
    async def ha_list_areas() -> list[dict]:
        """List all areas (rooms) configured in Home Assistant."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.post("/api/template", json={"template": "{{ areas() | list }}"})
            resp.raise_for_status()
            import ast
            area_ids = ast.literal_eval(resp.text)
            areas = []
            for area_id in area_ids:
                resp2 = await client.post(
                    "/api/template",
                    json={"template": f"{{{{ area_name('{area_id}') }}}}"},
                )
                areas.append({"area_id": area_id, "name": resp2.text.strip()})
            return sorted(areas, key=lambda x: x["name"])

    @mcp.tool()
    async def ha_call_service(domain: str, service: str, entity_id: str, data: dict = {}) -> dict:
        """[DESTRUCTIVE] Call a Home Assistant service to control a physical device or automation (e.g. lights, switches, climate, locks). Confirm the domain, service, and entity_id with the user before calling."""
        payload = {"entity_id": entity_id, **data}
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.post(f"/api/services/{domain}/{service}", json=payload)
            resp.raise_for_status()
            changed = resp.json()
            return {
                "called": f"{domain}.{service}",
                "entity_id": entity_id,
                "changed_states": len(changed),
            }

    # Automations go through the config API the HA editor uses: it writes
    # automations.yaml and reloads, so they show up and stay editable in the UI.
    # It needs an admin token.

    @mcp.tool()
    async def ha_list_automations() -> list[dict]:
        """List Home Assistant automations with their config id (for ha_get_automation), enabled state and last trigger time."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.get("/api/states")
            resp.raise_for_status()
            return [
                {
                    "entity_id": s["entity_id"],
                    "automation_id": s["attributes"].get("id", ""),
                    "alias": s["attributes"].get("friendly_name", ""),
                    "state": s["state"],
                    "last_triggered": s["attributes"].get("last_triggered"),
                }
                for s in sorted(resp.json(), key=lambda x: x["entity_id"])
                if s["entity_id"].startswith("automation.")
            ]

    @mcp.tool()
    async def ha_get_automation(automation_id: str) -> dict:
        """Get a Home Assistant automation's full config (alias, triggers, conditions, actions) by its config id from ha_list_automations."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.get(f"/api/config/automation/config/{automation_id}")
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def ha_save_automation(automation: dict, automation_id: str = "") -> dict:
        """[DESTRUCTIVE] Create or replace a Home Assistant automation. It's active immediately and may control physical devices. `automation` is the full config as in automations.yaml: alias, description, triggers, conditions, actions, mode. Omit automation_id to create a new one; pass one to REPLACE that automation entirely (fetch it with ha_get_automation first and send the edited whole). Show the user the final config and confirm before calling."""
        if not automation.get("alias"):
            raise ValueError("automation needs an alias")
        created = not automation_id
        # Same id scheme as the HA editor (epoch millis).
        automation_id = automation_id or str(int(time.time() * 1000))
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            if created:
                # Never overwrite by accident on an id collision.
                existing = await client.get(f"/api/config/automation/config/{automation_id}")
                if existing.status_code != 404:
                    raise ValueError(f"automation id {automation_id} already exists")
            await _post_config(client, f"/api/config/automation/config/{automation_id}", automation)
            return {"automation_id": automation_id, "alias": automation["alias"], "created": created}

    @mcp.tool()
    async def ha_delete_automation(automation_id: str) -> dict:
        """[DESTRUCTIVE] Permanently delete a Home Assistant automation by its config id. Confirm the automation (alias and id) with the user before calling."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.delete(f"/api/config/automation/config/{automation_id}")
            resp.raise_for_status()
            return {"deleted": automation_id}

    # Scripts use the same config API, keyed by the script's key in scripts.yaml.
    # Script states don't carry that key, so the list derives it from the
    # entity_id, which matches unless the entity was renamed in the UI.

    @mcp.tool()
    async def ha_list_scripts() -> list[dict]:
        """List Home Assistant scripts with their script_id (for ha_get_script), state (on = running now) and last run time."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.get("/api/states")
            resp.raise_for_status()
            return [
                {
                    "entity_id": s["entity_id"],
                    "script_id": s["entity_id"].split(".", 1)[1],
                    "alias": s["attributes"].get("friendly_name", ""),
                    "state": s["state"],
                    "last_triggered": s["attributes"].get("last_triggered"),
                }
                for s in sorted(resp.json(), key=lambda x: x["entity_id"])
                if s["entity_id"].startswith("script.")
            ]

    async def _get_script(client, script_id: str) -> dict | None:
        if not _SCRIPT_ID_RE.match(script_id):
            raise ValueError(f"invalid script_id {script_id!r}: use lowercase letters, digits and underscores")
        resp = await client.get(f"/api/config/script/config/{script_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    @mcp.tool()
    async def ha_get_script(script_id: str) -> dict:
        """Get a Home Assistant script's full config (alias, fields, sequence, mode) by its script_id from ha_list_scripts."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            cfg = await _get_script(client, script_id)
        if cfg is None:
            raise ValueError(f"no script config for {script_id!r} (entity renamed, or script not defined in scripts.yaml?)")
        return cfg

    @mcp.tool()
    async def ha_save_script(script_id: str, script: dict, create: bool = False) -> dict:
        """[DESTRUCTIVE] Create or replace a Home Assistant script. The change is live immediately for every automation that calls it, and scripts may control physical devices. `script` is the full config as in scripts.yaml under its key: alias, description, fields, sequence, mode. With create=True, makes a new script with this script_id (lowercase letters, digits, underscores) and refuses if one exists. Otherwise the script must exist and is REPLACED entirely: fetch it with ha_get_script first and send the edited whole. Show the user the final config and confirm before calling."""
        if not script.get("sequence"):
            raise ValueError("script needs a sequence")
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            exists = await _get_script(client, script_id) is not None
            if create and exists:
                raise ValueError(f"script {script_id} already exists")
            if not create and not exists:
                raise ValueError(f"no script {script_id} to replace; pass create=True to make a new one")
            await _post_config(client, f"/api/config/script/config/{script_id}", script)
        return {"script_id": script_id, "alias": script.get("alias", ""), "created": create}

    # Helpers created here are stored in HA's .storage, so they show up and stay
    # editable in the UI (Settings -> Devices & services -> Helpers).

    @mcp.tool()
    async def ha_create_input_boolean(name: str, icon: str = "", initial: bool = False) -> dict:
        """[DESTRUCTIVE] Create a Home Assistant toggle helper (input_boolean), e.g. a manual-override flag for an automation. Its entity_id is derived from `name` (lowercased, spaces to underscores) and is returned. Refuses if a helper with that name already exists. `initial` sets the starting state; `icon` is an optional mdi: icon. Confirm the name with the user before calling."""
        if not name.strip():
            raise ValueError("helper needs a name")
        async with _ws_session() as call:
            existing = await call({"type": "input_boolean/list"})
            if any(h.get("name", "").casefold() == name.strip().casefold() for h in existing):
                raise ValueError(f"an input_boolean named {name!r} already exists")
            payload = {"type": "input_boolean/create", "name": name.strip(), "initial": initial}
            if icon:
                payload["icon"] = icon
            created = await call(payload)
        return {"entity_id": f"input_boolean.{created['id']}", "name": created["name"], "created": True}

    # --- Device cleanup -----------------------------------------------------
    # Devices, entity registry entries and config entries are only listed over
    # the WebSocket API. Removal goes through HA's own device-removal path, which
    # the owning integration can refuse (e.g. ESPHome, where each device is its
    # own config entry and the entry itself has to be deleted).

    _DEAD_STATES = {"unavailable", "unknown", None}

    async def _registry_snapshot(call) -> tuple[list, list, dict, dict]:
        devices = await call({"type": "config/device_registry/list"})
        entities = await call({"type": "config/entity_registry/list"})
        entries = {e["entry_id"]: e for e in await call({"type": "config_entries/get"})}
        states = {s["entity_id"]: s for s in await call({"type": "get_states"})}
        return devices, entities, entries, states

    def _state_of(states: dict, entity_id: str):
        st = states.get(entity_id)
        if st is None:
            return None
        if st.get("attributes", {}).get("restored"):
            return "restored"
        return st.get("state")

    @mcp.tool()
    async def ha_list_devices(dead_only: bool = True, area_id: str = "") -> dict:
        """List Home Assistant devices with their integration, area and entity states, to find dead devices to clean up. Read-only. With `dead_only` (default), only devices whose enabled entities are ALL unavailable/unknown/restored (or that have no entities) are returned, plus `orphan_entities`: registry entities with no device that are not being provided by any integration (restored/missing state). `removable` says whether HA's device-removal path is supported by the integration; when it isn't, `sole_device_of_entry` says whether deleting the integration entry would remove only this device."""
        async with _ws_session() as call:
            devices, entities, entries, states = await _registry_snapshot(call)
        by_device: dict[str, list] = {}
        for ent in entities:
            if ent.get("device_id"):
                by_device.setdefault(ent["device_id"], []).append(ent)
        devices_per_entry: dict[str, int] = {}
        for dev in devices:
            for eid in dev.get("config_entries") or []:
                devices_per_entry[eid] = devices_per_entry.get(eid, 0) + 1
        out = []
        for dev in devices:
            if area_id and dev.get("area_id") != area_id:
                continue
            ents = [e for e in by_device.get(dev["id"], []) if not e.get("disabled_by")]
            ent_states = [{"entity_id": e["entity_id"], "state": _state_of(states, e["entity_id"])} for e in ents]
            dead = all(es["state"] in _DEAD_STATES | {"restored"} for es in ent_states)
            if dead_only and not dead:
                continue
            entry_ids = dev.get("config_entries") or []
            entry = entries.get(dev.get("primary_config_entry") or (entry_ids[0] if entry_ids else ""), {})
            out.append({
                "device_id": dev["id"],
                "name": dev.get("name_by_user") or dev.get("name"),
                "area_id": dev.get("area_id"),
                "manufacturer": dev.get("manufacturer"),
                "model": dev.get("model"),
                "integration": entry.get("domain"),
                "config_entry": {"entry_id": entry.get("entry_id"), "title": entry.get("title"),
                                 "state": entry.get("state")} if entry else None,
                "removable": bool(entry.get("supports_remove_device")),
                "sole_device_of_entry": bool(entry) and devices_per_entry.get(entry.get("entry_id"), 0) == 1,
                "disabled": bool(dev.get("disabled_by")),
                "all_entities_dead": dead,
                "entities": ent_states,
            })
        out.sort(key=lambda d: (d["integration"] or "", d["name"] or ""))
        orphans = []
        if dead_only:
            for ent in entities:
                if ent.get("device_id") or ent.get("disabled_by"):
                    continue
                st = _state_of(states, ent["entity_id"])
                if st in (None, "restored"):
                    orphans.append({"entity_id": ent["entity_id"], "platform": ent.get("platform"), "state": st})
        return {"count": len(out), "devices": out, "orphan_entities": orphans}

    @mcp.tool()
    async def ha_remove_device(device_id: str, delete_config_entry: bool = False) -> dict:
        """[DESTRUCTIVE] Remove a dead Home Assistant device and its entities. Refuses unless every enabled entity of the device is unavailable/unknown/restored. Uses HA's device-removal path (the integration must support it). If it doesn't (e.g. ESPHome, one config entry per device) and `delete_config_entry=True`, deletes the device's integration entry instead, but only when that entry contains no other device. Zigbee2MQTT/MQTT devices still paired in Zigbee2MQTT will be rediscovered; remove them there first. Confirm each device with the user before calling."""
        async with _ws_session() as call:
            devices, entities, entries, states = await _registry_snapshot(call)
            dev = next((d for d in devices if d["id"] == device_id), None)
            if dev is None:
                raise ValueError(f"no device with id {device_id}")
            live = [e["entity_id"] for e in entities
                    if e.get("device_id") == device_id and not e.get("disabled_by")
                    and _state_of(states, e["entity_id"]) not in _DEAD_STATES | {"restored"}]
            if live:
                raise ValueError(f"refusing: device still has live entities {live}")
            name = dev.get("name_by_user") or dev.get("name")
            entry_ids = dev.get("config_entries") or []
            entry = entries.get(dev.get("primary_config_entry") or (entry_ids[0] if entry_ids else ""), {})
            if entry.get("supports_remove_device"):
                await call({"type": "config/device_registry/remove", "device_id": device_id})
                return {"device_id": device_id, "name": name, "removed": True, "via": "device_registry"}
        if not delete_config_entry:
            raise ValueError(
                f"{entry.get('domain') or 'this'} integration doesn't support removing devices; "
                "pass delete_config_entry=True to delete its integration entry instead")
        if not entry or sum(entry["entry_id"] in (d.get("config_entries") or []) for d in devices) != 1:
            raise ValueError("refusing: the integration entry holds other devices too; remove it in the UI")
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.delete(f"/api/config/config_entries/entry/{entry['entry_id']}")
            resp.raise_for_status()
        return {"device_id": device_id, "name": name, "removed": True, "via": "config_entry",
                "config_entry": {"entry_id": entry["entry_id"], "domain": entry.get("domain"), "title": entry.get("title")}}

    @mcp.tool()
    async def ha_remove_entity(entity_id: str) -> dict:
        """[DESTRUCTIVE] Remove a leftover entity from Home Assistant's entity registry (e.g. an orphan from ha_list_devices). Refuses if the entity is currently provided by an integration (has a live, non-restored state), since it would just come back. Confirm with the user before calling."""
        async with _ws_session() as call:
            states = {s["entity_id"]: s for s in await call({"type": "get_states"})}
            st = _state_of(states, entity_id)
            if st not in (None, "restored"):
                raise ValueError(f"refusing: {entity_id} is provided by a loaded integration (state {st!r})")
            await call({"type": "config/entity_registry/remove", "entity_id": entity_id})
        return {"entity_id": entity_id, "removed": True}

    # --- Dashboards (Lovelace) ---------------------------------------------
    # Dashboard configs live only in HA's .storage and are only reachable over
    # the WebSocket API. Storage-mode dashboards can be read and saved; YAML-mode
    # ones are read-only, and strategy (auto-generated) dashboards have no
    # stored config until someone takes control of them.

    async def _get_dashboard_config(call, url_path: str):
        """Stored config for a dashboard, or None when it has none (strategy/auto-generated)."""
        payload = {"type": "lovelace/config", "force": False}
        if url_path:
            payload["url_path"] = url_path
        try:
            return await call(payload)
        except HAWebSocketError as err:
            if err.code == "config_not_found" and "Unknown config" not in str(err):
                return None
            raise

    @mcp.tool()
    async def ha_list_dashboards() -> dict:
        """List Home Assistant dashboards and loaded frontend resources. Read-only. `dashboards` includes the default dashboard (url_path ""), each with its mode: "storage" (editable via ha_save_dashboard) or "yaml" (read-only). `resources` lists the custom-card JS/CSS the frontend loads, which shows which custom cards are still installed."""
        async with _ws_session() as call:
            dashboards = await call({"type": "lovelace/dashboards/list"})
            resources = await call({"type": "lovelace/resources"})
        default = {"url_path": "", "title": "Overview (default)", "mode": "storage", "default": True}
        listed = [
            {
                "url_path": d.get("url_path", ""),
                "title": d.get("title", ""),
                "mode": d.get("mode", "storage"),
                "icon": d.get("icon"),
                "show_in_sidebar": d.get("show_in_sidebar", True),
                "require_admin": d.get("require_admin", False),
            }
            for d in dashboards
        ]
        return {
            "dashboards": [default, *sorted(listed, key=lambda d: d["url_path"])],
            "resources": [{"url": r.get("url"), "type": r.get("type")} for r in resources],
        }

    @mcp.tool()
    async def ha_get_dashboard(url_path: str = "") -> dict:
        """Get a Home Assistant dashboard's config (views, sections, cards) exactly as stored, in the shape ha_save_dashboard accepts. Read-only. `url_path` "" is the default dashboard. A dashboard with no stored config (auto-generated/strategy) returns `config: null` with `generated: true`."""
        async with _ws_session() as call:
            cfg = await _get_dashboard_config(call, url_path)
        return {"url_path": url_path, "generated": cfg is None, "config": cfg}

    @mcp.tool()
    async def ha_save_dashboard(
        url_path: str,
        config: dict,
        create: bool = False,
        title: str = "",
        icon: str = "",
        show_in_sidebar: bool = True,
    ) -> dict:
        """[DESTRUCTIVE] Replace a storage-mode Home Assistant dashboard's whole config (`{"views": [...]}`), or create a new dashboard with `create=True` (needs `title`; `url_path` must contain a hyphen and must not exist yet). Before overwriting, the current config is read and returned as `previous`: save it somewhere durable so the change can be reverted by saving it back. YAML-mode dashboards can't be saved. Confirm with the user before overwriting an existing dashboard."""
        if not isinstance(config, dict) or not isinstance(config.get("views"), list):
            raise ValueError('config must be a dashboard config with a "views" list')
        async with _ws_session() as call:
            if create:
                if not title.strip():
                    raise ValueError("creating a dashboard needs a title")
                if "-" not in url_path:
                    raise ValueError("a new dashboard's url_path must contain a hyphen, e.g. 'home-v2'")
                existing = await call({"type": "lovelace/dashboards/list"})
                if any(d.get("url_path") == url_path for d in existing):
                    raise ValueError(f"a dashboard at {url_path!r} already exists; save without create=True")
                payload = {"type": "lovelace/dashboards/create", "url_path": url_path,
                           "title": title.strip(), "show_in_sidebar": show_in_sidebar}
                if icon:
                    payload["icon"] = icon
                await call(payload)
                previous = None
            else:
                previous = await _get_dashboard_config(call, url_path)
            save = {"type": "lovelace/config/save", "config": config}
            if url_path:
                save["url_path"] = url_path
            try:
                await call(save)
            except HAWebSocketError as err:
                if "Not supported" in str(err):
                    raise ValueError(f"{url_path or 'the default dashboard'} is a YAML-mode dashboard; it can't be saved from here") from err
                raise
        return {"url_path": url_path, "created": create, "saved": True, "previous": previous}

    async def _fetch_automations(client, automation_id: str = "") -> tuple[list[dict], dict, dict]:
        """(automation list, {id: config or None}, {entity_id: state}) in one pass."""
        resp = await client.get("/api/states")
        resp.raise_for_status()
        states = {s["entity_id"]: s for s in resp.json()}
        automations = [
            {
                "automation_id": s["attributes"].get("id", ""),
                "alias": s["attributes"].get("friendly_name", eid),
                "state": s["state"],
                "last_triggered": s["attributes"].get("last_triggered"),
            }
            for eid, s in sorted(states.items())
            if eid.startswith("automation.") and s["attributes"].get("id")
        ]
        if automation_id:
            automations = [a for a in automations if a["automation_id"] == automation_id]
            if not automations:
                raise ValueError(f"no automation with id {automation_id}")

        async def get_config(aid):
            r = await client.get(f"/api/config/automation/config/{aid}")
            return r.json() if r.status_code == 200 else None

        configs = await asyncio.gather(*(get_config(a["automation_id"]) for a in automations))
        return automations, dict(zip((a["automation_id"] for a in automations), configs)), states

    @mcp.tool()
    async def ha_export_automations(automation_id: str = "") -> dict:
        """Export Home Assistant automation configs (all, or one by config id) as returned by the HA editor's config API. Each config can be passed back to ha_save_automation unchanged. Automations whose config HA couldn't return are listed under `skipped`, so a snapshot is never silently incomplete. Read-only."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            automations, configs, _ = await _fetch_automations(client, automation_id)
        exported = [configs[a["automation_id"]] for a in automations if configs[a["automation_id"]]]
        return {
            "count": len(exported),
            "automations": exported,
            "skipped": [{"automation_id": a["automation_id"], "alias": a["alias"]}
                        for a in automations if not configs[a["automation_id"]]],
        }

    @mcp.tool()
    async def ha_audit_automations(automation_id: str = "", include_info: bool = True) -> dict:
        """Audit Home Assistant automations (all, or one by config id) for silent failures. Read-only.

        Reconciles every automation against live HA: references to entities that don't exist or are
        unavailable, device_ids that no longer resolve, missing areas/actions, configs HA can't load.
        Also lints for patterns that have failed here before: device triggers (break on re-pair) and
        state triggers with `from:` on binary sensors (miss recovery from unavailable). Info-level
        findings: disabled or never-fired automations. Fix via ha_get_automation + ha_save_automation.
        """
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            automations, configs, states = await _fetch_automations(client, automation_id)
            refs = {"entities": set(), "devices": set(), "areas": set(), "services": set()}
            for cfg in configs.values():
                if cfg:
                    _collect_refs(cfg, refs)
            svc_resp = await client.get("/api/services")
            svc_resp.raise_for_status()
            services = {f"{d['domain']}.{s}" for d in svc_resp.json() for s in d["services"]}
            reg_resp = await client.post(
                "/api/template",
                json={"template": _REGISTRY_TEMPLATE, "variables": {"dev_ids": sorted(refs["devices"])}},
            )
            reg_resp.raise_for_status()
            registry = json.loads(reg_resp.text)

        findings = _audit(automations, configs, states, services,
                          set(registry["areas"]), set(registry["missing_devices"]),
                          dict(registry.get("device_entities", [])))
        if not include_info:
            findings = [f for f in findings if f["severity"] != "info"]
        summary = {sev: sum(f["severity"] == sev for f in findings) for sev in _SEVERITY_ORDER}
        return {"automations_checked": len(automations), "summary": summary, "findings": findings}

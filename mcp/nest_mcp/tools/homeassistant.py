import asyncio
import json
import re
import time

from mcp.server.mcpserver import MCPServer
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
# Renders the area list, the device ids that no longer resolve, and each
# live device's entities (to suggest a state-trigger replacement) in one call.
_REGISTRY_TEMPLATE = (
    "{%- set ns = namespace(m=[], e=[]) -%}"
    "{%- for d in dev_ids -%}{%- if device_attr(d, 'name') is none -%}"
    "{%- set ns.m = ns.m + [d] -%}"
    "{%- else -%}{%- set ns.e = ns.e + [[d, device_entities(d)]] -%}{%- endif -%}{%- endfor -%}"
    "{{ {'areas': areas(), 'missing_devices': ns.m, 'device_entities': ns.e} | tojson }}"
)


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
            resp = await client.post(f"/api/config/automation/config/{automation_id}", json=automation)
            if resp.status_code == 400:
                # HA's validation message says what's wrong with the config.
                raise ValueError(f"Home Assistant rejected the config: {resp.json().get('message', resp.text)}")
            resp.raise_for_status()
            return {"automation_id": automation_id, "alias": automation["alias"], "created": created}

    @mcp.tool()
    async def ha_delete_automation(automation_id: str) -> dict:
        """[DESTRUCTIVE] Permanently delete a Home Assistant automation by its config id. Confirm the automation (alias and id) with the user before calling."""
        async with make_client(config.homeassistant.url, headers=_headers()) as client:
            resp = await client.delete(f"/api/config/automation/config/{automation_id}")
            resp.raise_for_status()
            return {"deleted": automation_id}

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

import time

from mcp.server.mcpserver import MCPServer
from nest_mcp import config
from nest_mcp.http_client import make_client


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.homeassistant.token}"}


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

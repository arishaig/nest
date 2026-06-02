from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.homeassistant.token}"}


def register(mcp: FastMCP) -> None:

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

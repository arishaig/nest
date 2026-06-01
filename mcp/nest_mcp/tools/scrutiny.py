from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def scrutiny_summary() -> list[dict]:
        """Get SMART health summary for all monitored drives: model, temperature, and pass/fail status."""
        async with make_client(config.scrutiny.url) as client:
            resp = await client.get("/api/summary")
            resp.raise_for_status()
            data = resp.json().get("data", {})
            devices = data.get("summary", {})
            result = []
            for wwn, info in devices.items():
                device = info.get("device", {})
                smart = info.get("smart", {})
                result.append({
                    "wwn": wwn,
                    "device_name": device.get("device_name", ""),
                    "model_name": device.get("model_name", ""),
                    "capacity_gb": round(device.get("capacity", 0) / 1024**3),
                    "smart_status": smart.get("Status", 0),
                    "temp_c": smart.get("temp", 0),
                    "power_on_hours": smart.get("power_on_hours", 0),
                })
            return sorted(result, key=lambda x: x["device_name"])

    @mcp.tool()
    async def scrutiny_disk_detail(wwn: str) -> dict:
        """Get full SMART attribute detail for a specific drive by WWN (from scrutiny_summary)."""
        async with make_client(config.scrutiny.url) as client:
            resp = await client.get(f"/api/device/{wwn}/details")
            resp.raise_for_status()
            data = resp.json().get("data", {})
            device = data.get("device", {})
            attrs = data.get("smart_results", [{}])[0].get("attrs", {}) if data.get("smart_results") else {}
            return {
                "wwn": wwn,
                "device_name": device.get("device_name", ""),
                "model_name": device.get("model_name", ""),
                "firmware": device.get("firmware", ""),
                "capacity_gb": round(device.get("capacity", 0) / 1024**3),
                "smart_attributes": {
                    k: {
                        "name": v.get("attribute_name", ""),
                        "value": v.get("value", 0),
                        "worst": v.get("worst", 0),
                        "threshold": v.get("thresh", 0),
                        "raw": v.get("raw_value", 0),
                        "status": v.get("status", 0),
                    }
                    for k, v in attrs.items()
                },
            }

"""Floor-plan dashboard tools: place lights and devices on a plan, glow that follows walls, calibrate.

The model lives in the dashboard config itself (key ``nest_floorplan``), so it
survives pod restarts and needs no storage here; the masks are embedded in the
card as small data-URI PNGs, so nothing has to be uploaded to /config/www.
The plan image itself (SVG or PNG) must already be served by Home Assistant,
e.g. /local/... from /config/www, or be any URL nest-mcp can fetch.
"""
import asyncio
import copy
import os
import io
import json
import re
import time
from datetime import datetime, timezone

import numpy as np
from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage

from nest_mcp import config
from nest_mcp.floorplan import engine
from nest_mcp.floorplan import model as M
from nest_mcp.floorplan import render as R
from nest_mcp.http_client import make_client
from nest_mcp.tools import homeassistant as ha

_CONFIG_WARN_BYTES = 1_500_000
# Mask builds fan out over processes; keep headroom for the server under the pod's memory limit.
_WORKERS = min(3, os.cpu_count() or 1)
_WARM_WHITE_K = 2700
_raster_cache: dict[tuple, np.ndarray] = {}


class FloorplanError(ha.HARefusal):
    """Refusal or bad input, shown to the caller verbatim."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Plan image ---------------------------------------------------------------

def _svg_size(svg: bytes) -> tuple[int, int] | None:
    head = svg[:4000].decode("utf-8", "replace")
    m = re.search(r'viewBox\s*=\s*"\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', head)
    if m:
        return round(float(m.group(1))), round(float(m.group(2)))
    w = re.search(r'\bwidth\s*=\s*"([\d.]+)', head)
    h = re.search(r'\bheight\s*=\s*"([\d.]+)', head)
    return (round(float(w.group(1))), round(float(h.group(1)))) if w and h else None


def _rasterize(data: bytes, size: tuple[int, int] | None) -> np.ndarray:
    """Greyscale raster of an SVG (at its viewBox size) or a bitmap plan."""
    if b"<svg" in data[:4000]:
        import cairosvg  # needs libcairo; only plans in SVG form use it

        size = size or _svg_size(data)
        if not size:
            raise FloorplanError("the SVG has no viewBox or width/height; pass size explicitly")
        png = cairosvg.svg2png(bytestring=data, output_width=size[0], output_height=size[1],
                               background_color="white")
        img = PILImage.open(io.BytesIO(png))
    else:
        img = PILImage.open(io.BytesIO(data))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = PILImage.new("RGBA", img.size, (255, 255, 255, 255))
            img = PILImage.alpha_composite(bg, img)
        if size and img.size != tuple(size):
            img = img.resize(tuple(size), PILImage.LANCZOS)
    return np.array(img.convert("L"))


async def _fetch(url: str) -> bytes:
    """A plan image: a path is fetched from Home Assistant, a full URL as-is (without the HA token)."""
    if url.startswith("/"):
        client = make_client(config.homeassistant.url, headers=ha._headers(), timeout=30.0)
    elif url.startswith(("http://", "https://")):
        client = make_client(url, timeout=30.0)
    else:
        raise FloorplanError(f"image {url!r} must be a path Home Assistant serves (/local/...) or an http(s) URL")
    async with client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def _plan_raster(model: dict) -> np.ndarray:
    key = (model["image"], tuple(model["size"]))
    if key not in _raster_cache:
        data = await _fetch(model["image"])
        _raster_cache.clear()           # one plan at a time is plenty
        _raster_cache[key] = await asyncio.to_thread(_rasterize, data, tuple(model["size"]))
    return _raster_cache[key]


async def _geometry(model: dict) -> M.Geometry:
    gray = await _plan_raster(model)
    return await asyncio.to_thread(M.Geometry, model, gray)


# --- Load / save --------------------------------------------------------------

async def _load(url_path: str) -> tuple[dict, dict]:
    async with ha._ws_session() as call:
        cfg = await _get_config(call, url_path)
    if not cfg or M.MODEL_KEY not in cfg:
        raise FloorplanError(f"dashboard {url_path!r} has no floor plan; start one with floorplan_create")
    return cfg, cfg[M.MODEL_KEY]


async def _get_config(call, url_path: str) -> dict | None:
    payload = {"type": "lovelace/config", "force": False}
    if url_path:
        payload["url_path"] = url_path
    try:
        return await call(payload)
    except ha.HAWebSocketError as err:
        if err.code == "config_not_found":
            return None
        raise


async def _save(url_path: str, cfg: dict) -> None:
    async with ha._ws_session() as call:
        save = {"type": "lovelace/config/save", "config": cfg}
        if url_path:
            save["url_path"] = url_path
        await call(save)


async def _commit(url_path: str, cfg: dict, model: dict, before: dict | None, note: str,
                  rebuild: set[str] | None = None, title: str = "", force_sun: bool = False) -> dict:
    """Record history, regenerate stale masks, write the card and save the dashboard."""
    if before is not None:
        M.push_history(model, before, note, _now())
    found = R.find_card(cfg)
    reuse = R.previous_masks(found[0][found[1]] if found else None)
    if force_sun:
        reuse = {k: v for k, v in reuse.items() if not k.startswith("sun:")}
    stale = set(R.stale_fixtures(model, reuse)) | (rebuild or set())
    geo = await _geometry(model) if stale or R.stale_sun(model, reuse) else None
    started = time.monotonic()
    elements, stats = await asyncio.to_thread(R.build_elements, model, geo, reuse, rebuild, _WORKERS)
    R.place_card(cfg, R.build_card(model, elements), title)
    cfg[M.MODEL_KEY] = model
    size = len(json.dumps(cfg))
    if size > _CONFIG_WARN_BYTES:
        stats["warnings"].append(f"dashboard config is {size // 1024} KiB; consider a larger light.mask_scale")
    await _save(url_path, cfg)
    return {"saved": True, "note": note, "masks_regenerated": stats["regenerated"],
            "masks_reused": len(stats["reused"]),
            **({"sun_masks_regenerated": stats["sun_regenerated"]} if stats["sun_regenerated"] else {}),
            "build_seconds": round(time.monotonic() - started, 1),
            "config_kib": size // 1024, **({"warnings": stats["warnings"]} if stats["warnings"] else {})}


# --- Summaries ----------------------------------------------------------------

def _fixture_summary(model: dict, fid: str) -> dict:
    fx = model["fixtures"][fid]
    out = {"id": fid, "entity": fx["entity"], "kind": fx["kind"], "x": fx["x"], "y": fx["y"]}
    if "icon_x" in fx or "icon_y" in fx:
        out["icon_at"] = [fx.get("icon_x", fx["x"]), fx.get("icon_y", fx["y"])]
    for k in ("icon", "icon_anchor", "icon_pad", "icon_scale", "icon_offset"):
        if k in fx:
            out[k] = fx[k]
    if fx["kind"] == "light":
        custom = {k: v for k, v in (fx.get("glow") or {}).items() if v != M.GLOW_DEFAULTS.get(k)}
        out["glow"] = custom or "defaults"
    return out


def _summary(model: dict) -> dict:
    return {
        "image": model["image"], "dark_image": model.get("dark_image") or None, "size": model["size"],
        "fixtures": [_fixture_summary(model, f) for f in sorted(model.get("fixtures") or {})],
        "openings": model.get("openings") or {},
        "blockers": model.get("blockers") or {},
        "outdoors": model.get("outdoors") or {},
        "settings": {s: M.settings(model, s) for s in ("walls", "light", "transmission", "sun")},
        "calibration_session": bool(model.get("session")),
        "history": [{"at": h["at"], "note": h["note"]} for h in model.get("history") or []],
    }


def _default_kind(entity: str) -> str:
    domain = entity.split(".", 1)[0]
    return {"light": "light", "switch": "light", "fan": "fan"}.get(domain, "device")


# --- Light control for calibration ---------------------------------------------

async def _states(client, entities: list[str]) -> dict[str, dict]:
    resp = await client.get("/api/states")
    resp.raise_for_status()
    wanted = set(entities)
    return {s["entity_id"]: s for s in resp.json() if s["entity_id"] in wanted}


async def _service(client, domain: str, service: str, data: dict) -> None:
    resp = await client.post(f"/api/services/{domain}/{service}", json=data)
    resp.raise_for_status()


def _restore_payload(state: dict) -> tuple[str, dict]:
    """(service, data) that puts an entity back into a snapshotted state."""
    entity = state["entity_id"]
    domain = entity.split(".", 1)[0]
    if state["state"] != "on":
        return f"{domain}.turn_off", {"entity_id": entity}
    data = {"entity_id": entity}
    attrs = state.get("attributes") or {}
    if domain == "light":
        if attrs.get("brightness") is not None:
            data["brightness"] = attrs["brightness"]
        mode = attrs.get("color_mode")
        if mode == "color_temp" and attrs.get("color_temp_kelvin"):
            data["color_temp_kelvin"] = attrs["color_temp_kelvin"]
        elif mode in ("xy", "hs", "rgb", "rgbw", "rgbww") and attrs.get("xy_color"):
            data["xy_color"] = attrs["xy_color"]
        if attrs.get("effect") and attrs["effect"] not in ("none", "None"):
            data["effect"] = attrs["effect"]
    return f"{domain}.turn_on", data


def register(mcp: MCPServer) -> None:

    @mcp.tool()
    async def floorplan_get(url_path: str, include_gaps: bool = False, raw: bool = False,
                            include_thin_walls: bool = False) -> dict:
        """Read a floor-plan dashboard's model: plan image and size, fixtures (lights, fans, devices) with positions and glow calibration, openings (doors/windows), blockers, settings and change history. Read-only. Coordinates are plan pixels. `include_gaps` also lists the wall gaps detected in the plan (candidate doors/windows, each with a guessed kind, and for a window on an outside wall a `suggested_facing`) that floorplan_set_opening can adopt via `from_gap`. `include_thin_walls` lists long hairline walls the detector skipped (light leaks through them): `touches_wall` ones that run between rooms are likely real walls; adopt one as a blocker with floorplan_set_blocker(from_thin_wall=...). Furniture outlines show up too. `raw` returns the stored model itself (without history), the shape floorplan_import accepts: save it to back a plan up."""
        _, model = await _load(url_path)
        out = {"model": M.snapshot(model)} if raw else _summary(model)
        if include_gaps:
            out["gaps"] = (await _geometry(model)).gaps()
        if include_thin_walls:
            out["thin_walls"] = (await _geometry(model)).thin_walls()
        return out

    @mcp.tool()
    async def floorplan_preview(url_path: str, fixture_id: str = "", brightness_pct: float = 100,
                                show_gaps: bool = False, show_thin_walls: bool = False,
                                sun_azimuth: float | None = None, sun_elevation: float | None = None,
                                cloud_pct: float = 0, open_sensors: list[str] | None = None):
        """Render the floor plan as an image: walls (dark), wall gaps the detector sealed (orange), openings (green open / red closed / blue door-sensor / cyan window / purple glass door), blockers (magenta), fixtures (labelled dots) and a 100 px coordinate grid for picking positions. With `fixture_id`, that light's glow is drawn as the dashboard shows it at `brightness_pct` with door sensors closed: use it to check reach and wall shadows. `show_gaps` outlines detected gaps with their ids (and a suggested facing for outside windows); `show_thin_walls` outlines hairline walls the detector skipped (blue touch a wall, grey are already blocked). With `sun_azimuth` and `sun_elevation` (compass degrees; use sun.sun's attributes, or any angle you want to check) the sunlight the dashboard would draw is simulated in amber, with `cloud_pct`, and `open_sensors` (door sensor entity ids to treat as open; default closed). The result lists each sun layer with its strength and the gates that scaled it (incidence, low sun, through, elevation fade, glass door): use it to see why a window is dark at an angle, or to check the plan at noon or midsummer without waiting. Read-only."""
        _, model = await _load(url_path)
        if fixture_id and fixture_id not in (model.get("fixtures") or {}):
            raise FloorplanError(f"no fixture {fixture_id!r}")
        geo = await _geometry(model)
        info: dict = {"size": model["size"], "fixture": fixture_id or None}
        alpha = None
        if (sun_azimuth is None) != (sun_elevation is None):
            raise FloorplanError("give both sun_azimuth and sun_elevation")
        if sun_azimuth is not None:
            alpha, layers = await asyncio.to_thread(
                R.sun_state, model, geo, sun_azimuth, sun_elevation, cloud_pct, frozenset(open_sensors or []))
            info["sun"] = {"azimuth": sun_azimuth, "elevation": sun_elevation, "cloud_pct": cloud_pct,
                           "layers": layers}
        png = await asyncio.to_thread(R.preview, model, geo, fixture_id, brightness_pct / 100, show_gaps,
                                      1100, alpha, show_thin_walls)
        return [info, Image(data=png, format="png")]

    @mcp.tool()
    async def floorplan_create(url_path: str, image: str, dark_image: str = "", title: str = "",
                               create_dashboard: bool = False, size: list[int] | None = None):
        """[DESTRUCTIVE] Start a floor-plan dashboard from a plan image Home Assistant already serves (e.g. /local/floorplan/plan.svg from /config/www). With `create_dashboard=True` a new dashboard is made at `url_path` (needs `title`; url_path must contain a hyphen); otherwise a "Floor Plan" view is added to the existing dashboard. Refuses if the dashboard already has a floor plan. SVG plans are measured at their viewBox size (or pass `size` [w, h]); walls are detected from dark thick lines. Returns the detected wall gaps (candidate doors/windows to adopt with floorplan_set_opening) and a preview image. Next: floorplan_set_fixture for each light/fan/device. Glow needs the card-mod frontend plugin."""
        data = await _fetch(image)
        plan_size = tuple(size) if size else (_svg_size(data) if b"<svg" in data[:4000] else None)
        gray = await asyncio.to_thread(_rasterize, data, plan_size)
        model = M.new_model(image, (gray.shape[1], gray.shape[0]), dark_image)
        _raster_cache.clear()
        _raster_cache[(image, tuple(model["size"]))] = gray
        async with ha._ws_session() as call:
            if create_dashboard:
                if not title.strip() or "-" not in url_path:
                    raise FloorplanError("a new dashboard needs a title and a url_path containing a hyphen")
                existing = await call({"type": "lovelace/dashboards/list"})
                if any(d.get("url_path") == url_path for d in existing):
                    raise FloorplanError(f"a dashboard at {url_path!r} already exists; omit create_dashboard")
                await call({"type": "lovelace/dashboards/create", "url_path": url_path,
                            "title": title.strip(), "icon": "mdi:floor-plan", "show_in_sidebar": True})
                cfg = {"views": []}
            else:
                cfg = await _get_config(call, url_path)
                if cfg is None:
                    raise FloorplanError(f"dashboard {url_path!r} has no stored config (auto-generated); "
                                         "take control of it in the UI first, or use create_dashboard")
                if M.MODEL_KEY in cfg:
                    raise FloorplanError(f"dashboard {url_path!r} already has a floor plan")
        result = await _commit(url_path, cfg, model, None, "created", title=title)
        geo = await asyncio.to_thread(M.Geometry, model, gray)
        png = await asyncio.to_thread(R.preview, model, geo, "", 1.0, True)
        return [{**result, "size": model["size"], "gaps": geo.gaps()}, Image(data=png, format="png")]

    @mcp.tool()
    async def floorplan_import(url_path: str, model: dict, adopt_card: bool = False) -> dict:
        """[DESTRUCTIVE] Load a whole floor-plan model into a dashboard and rebuild its card: for migrating a hand-built floor plan, or restoring a model saved from floorplan_get. `model` is what floorplan_get(raw=True) returns under "model" (image, size, fixtures/openings/blockers as {id: {...}}, settings); it is validated first. Replaces any existing model (the old one stays undoable). With `adopt_card`, a dashboard without a floor-plan card has its first picture-elements card taken over in place instead of getting a new view. Regenerates every mask (can take a minute)."""
        try:
            new = M.validate(model)
        except M.ModelError as err:
            raise FloorplanError(str(err)) from err
        async with ha._ws_session() as call:
            cfg = await _get_config(call, url_path)
        if cfg is None:
            raise FloorplanError(f"dashboard {url_path!r} has no stored config")
        old = cfg.get(M.MODEL_KEY)
        before = M.snapshot(old) if old else None
        new["history"] = list((old or {}).get("history") or [])
        new["session"] = (old or {}).get("session")
        adopted = False
        if R.find_card(cfg) is None and adopt_card:
            adopted = R.adopt_card(cfg)
            if not adopted:
                raise FloorplanError("adopt_card: the dashboard has no picture-elements card")
        _raster_cache.clear()
        result = await _commit(url_path, cfg, new, before, "imported model")
        return {**result, "adopted_card": adopted, "fixtures": len(new["fixtures"]),
                "openings": len(new["openings"]), "blockers": len(new["blockers"])}

    @mcp.tool()
    async def floorplan_set_fixture(
        url_path: str, fixture_id: str, entity: str = "", x: float | None = None, y: float | None = None,
        kind: str = "", icon: str | None = None, icon_x: float | None = None, icon_y: float | None = None,
        icon_anchor: str | None = None, icon_pad: float | None = None, icon_scale: float | None = None,
        icon_offset: list[float] | None = None,
    ) -> dict:
        """[DESTRUCTIVE] Add or move a fixture on the plan and update the dashboard live. A fixture is one physical light source (or a fan/device icon); one entity may have several fixtures, e.g. two ceiling lights on one switch.
        New fixture: needs `entity`, `x`, `y` (plan pixels: where the light actually is). `kind` defaults from the domain (light/switch -> light with glow, fan -> spinning fan icon, else plain device icon).
        Existing fixture: pass only what changes. Moving x/y regenerates its wall masks.
        Icons: by default centred on the fixture. `icon_x`/`icon_y` put the icon elsewhere (e.g. on the wall next to a lamp); `icon_anchor` (center, n, s, e, w, ne, nw, se, sw) says which side of that point the icon sits, `icon_pad` px clear of it (default 6); `icon_offset` [dx, dy] px nudges it; `icon_scale` (default 1.25). `icon` is an mdi: icon."""
        check = M.check_id(fixture_id, "fixture")
        cfg, model = await _load(url_path)
        before = M.snapshot(model)
        fixtures = model.setdefault("fixtures", {})
        fx = copy.deepcopy(fixtures.get(check) or {})
        new = not fx
        if new:
            if not entity or x is None or y is None:
                raise FloorplanError("a new fixture needs entity, x and y")
            fx = {"entity": M.check_entity(entity), "kind": kind or _default_kind(entity), "x": x, "y": y}
        else:
            if entity:
                fx["entity"] = M.check_entity(entity)
            if kind:
                fx["kind"] = kind
            if x is not None:
                fx["x"] = x
            if y is not None:
                fx["y"] = y
        if fx["kind"] not in M.FIXTURE_KINDS:
            raise FloorplanError(f"kind must be one of {sorted(M.FIXTURE_KINDS)}")
        M.check_point(model, fx["x"], fx["y"], "fixture position")
        for k, v in (("icon", icon), ("icon_x", icon_x), ("icon_y", icon_y), ("icon_anchor", icon_anchor),
                     ("icon_pad", icon_pad), ("icon_scale", icon_scale), ("icon_offset", icon_offset)):
            if v is not None:
                fx[k] = v
        if fx.get("icon_anchor", "center") not in M.ANCHORS:
            raise FloorplanError(f"icon_anchor must be one of {sorted(M.ANCHORS)}")
        if "icon_offset" in fx and len(fx["icon_offset"]) != 2:
            raise FloorplanError("icon_offset is [dx, dy]")
        if "icon_x" in fx or "icon_y" in fx:
            M.check_point(model, fx.get("icon_x", fx["x"]), fx.get("icon_y", fx["y"]), "icon position")
        if fx["kind"] == "light":
            geo = await _geometry(model)
            if geo.base[int(fx["y"]), int(fx["x"])]:
                raise FloorplanError(f"({fx['x']}, {fx['y']}) is inside a wall; move it into the room")
        fixtures[check] = fx
        note = f"{'added' if new else 'updated'} fixture {check}"
        return {**await _commit(url_path, cfg, model, before, note), "fixture": _fixture_summary(model, check)}

    @mcp.tool()
    async def floorplan_remove_fixture(url_path: str, fixture_id: str) -> dict:
        """[DESTRUCTIVE] Remove a fixture (its glow and icon) from the floor plan. Undo with floorplan_undo."""
        cfg, model = await _load(url_path)
        if fixture_id not in (model.get("fixtures") or {}):
            raise FloorplanError(f"no fixture {fixture_id!r}")
        before = M.snapshot(model)
        del model["fixtures"][fixture_id]
        for bl in (model.get("blockers") or {}).values():
            if fixture_id in (bl.get("fixtures") or []):
                bl["fixtures"].remove(fixture_id)
        return await _commit(url_path, cfg, model, before, f"removed fixture {fixture_id}")

    @mcp.tool()
    async def floorplan_set_opening(
        url_path: str, opening_id: str, kind: str = "", rect: list[int] | None = None,
        line: list[int] | None = None, from_gap: str = "", state: str = "", sensor: str = "",
        transmission: float | None = None, facing: str = "", sun: dict | None = None, remove: bool = False,
    ) -> dict:
        """[DESTRUCTIVE] Add, change or remove a door/window opening; regenerates the affected glow masks live.
        Wall gaps (doorways, windows) block light until made an opening. Place it with `from_gap` (an id from floorplan_get(include_gaps=True)), a `rect` [x0, y0, x1, y1] covering the gap, or a `line` [x0, y0, x1, y1] drawn across it (for diagonal doors or gaps wider than the detector seals; a line is a wall when closed).
        `kind`: door, window, glass_door, opening (archway), open_air (an open side of a covered balcony). `state`: open, closed, or sensor (glow follows `sensor`, a binary_sensor that is 'on' when open: two glow layers per affected light).
        When closed, light passes at `transmission` (defaults per kind from the transmission settings: door 0, window 0.7, glass_door 0.35).
        Sunlight: give a window, glass_door or open_air opening a `facing` (n, e, s or w, or ne/nw/se/sw for a diagonal door: the way it looks out, plan up = n; "none" clears it) and it throws a sun beam into the room whenever the real sun (sun.sun) is on that side, dimmed by cloud cover. `sun` overrides per opening: height_m (ceiling/head height, sets how far the beam reaches), spread (far-end width relative to the window), blur, strength, full_until (0-1: how far along the beam stays at full strength), fade_from_deg + fade_to_deg (together: the beam fades out between these sun elevations, for a window behind a covered balcony that only sees low sun, e.g. 15 and 40), through (the id of another opening, e.g. the balcony's open_air edge: this window is lit only while the sun can reach it through that opening)."""
        M.check_id(opening_id, "opening")
        cfg, model = await _load(url_path)
        before = M.snapshot(model)
        openings = model.setdefault("openings", {})
        if remove:
            if opening_id not in openings:
                raise FloorplanError(f"no opening {opening_id!r}")
            users = sorted(k for k, v in openings.items() if (v.get("sun") or {}).get("through") == opening_id)
            if users:
                raise FloorplanError(f"{users} get their sun through {opening_id!r}; clear their sun.through first")
            del openings[opening_id]
            return await _commit(url_path, cfg, model, before, f"removed opening {opening_id}")
        op = copy.deepcopy(openings.get(opening_id) or {})
        if from_gap:
            gap = next((g for g in (await _geometry(model)).gaps() if g["id"] == from_gap), None)
            if gap is None:
                raise FloorplanError(f"no gap {from_gap!r}; list them with floorplan_get(include_gaps=True)")
            op.pop("line", None)
            op["rect"] = gap["rect"]
            op.setdefault("kind", gap["guess"])
        elif rect is not None or line is not None:
            op.pop("rect", None)
            op.pop("line", None)
            op.update(M.check_shape(model, rect, line))
        if "rect" not in op and "line" not in op:
            raise FloorplanError("a new opening needs from_gap, rect or line")
        if kind:
            op["kind"] = kind
        op.setdefault("kind", "door")
        if op["kind"] not in M.OPENING_KINDS:
            raise FloorplanError(f"kind must be one of {sorted(M.OPENING_KINDS)}")
        if state:
            op["state"] = state
        op.setdefault("state", "open" if op["kind"] == "opening" else "closed")
        if op["state"] not in M.OPENING_STATES:
            raise FloorplanError(f"state must be one of {sorted(M.OPENING_STATES)}")
        if sensor:
            op["sensor"] = M.check_entity(sensor, "sensor")
        if op["state"] == "sensor" and not op.get("sensor"):
            raise FloorplanError("state 'sensor' needs a sensor (binary_sensor that is 'on' when open)")
        if transmission is not None:
            if not 0 <= transmission <= 1:
                raise FloorplanError("transmission is between 0 and 1")
            op["transmission"] = transmission
        if facing:
            if facing == "none":
                op.pop("facing", None)
            else:
                if op["kind"] not in M.SUN_KINDS:
                    raise FloorplanError(f"only {sorted(M.SUN_KINDS)} openings can have a facing")
                op["facing"] = facing
        if sun is not None:
            unknown = set(sun) - set(M.SUN_OVERRIDES)
            if unknown:
                raise FloorplanError(f"unknown sun overrides {sorted(unknown)}; valid: {sorted(M.SUN_OVERRIDES)}")
            if sun:
                op["sun"] = {**(op.get("sun") or {}), **sun}
                try:
                    M.check_fade(op["sun"], f"opening {opening_id}")
                except M.ModelError as err:
                    raise FloorplanError(str(err)) from err
            else:
                op.pop("sun", None)             # an empty dict clears the overrides
        if (op.get("sun") or {}).get("through"):
            try:
                M.check_through(openings, opening_id, op["sun"])
            except M.ModelError as err:
                raise FloorplanError(str(err)) from err
        if op.get("facing"):
            try:
                M.window_frame({**op})
            except M.ModelError as err:
                raise FloorplanError(f"opening {opening_id}: {err}") from err
        elif op.get("sun"):
            raise FloorplanError("sun overrides need a facing")
        openings[opening_id] = op
        return {**await _commit(url_path, cfg, model, before, f"set opening {opening_id}"),
                "opening": {opening_id: op}}

    @mcp.tool()
    async def floorplan_set_outdoor(url_path: str, area_id: str, x: int = -1, y: int = -1,
                                    remove: bool = False) -> dict:
        """[DESTRUCTIVE] Mark an enclosed area of the plan as open air (a balcony) so it gets a faint sky-light wash whenever the sun is up, dimmed by cloud cover.
        Give any point (x, y) inside the area; the area is everything enclosed by the walls around it. For direct sun on a covered balcony also add an `open_air` opening on its open side (floorplan_set_opening with facing)."""
        M.check_id(area_id, "outdoor area")
        cfg, model = await _load(url_path)
        before = M.snapshot(model)
        outdoors = model.setdefault("outdoors", {})
        if remove:
            if area_id not in outdoors:
                raise FloorplanError(f"no outdoor area {area_id!r}")
            del outdoors[area_id]
            return await _commit(url_path, cfg, model, before, f"removed outdoor area {area_id}")
        if x < 0 or y < 0:
            raise FloorplanError("give a point (x, y) inside the area")
        try:
            M.check_point(model, x, y, "point")
        except M.ModelError as err:
            raise FloorplanError(str(err)) from err
        outdoors[area_id] = {"x": x, "y": y}
        return {**await _commit(url_path, cfg, model, before, f"set outdoor area {area_id}"),
                "outdoors": {area_id: outdoors[area_id]}}

    @mcp.tool()
    async def floorplan_set_blocker(url_path: str, blocker_id: str, rect: list[int] | None = None,
                                    line: list[int] | None = None, fixtures: list[str] | None = None,
                                    remove: bool = False, from_thin_wall: str = "") -> dict:
        """[DESTRUCTIVE] Add, change or remove a light blocker; regenerates affected masks live. A blocker is a `rect` or `line` that stops light: a wall the plan doesn't draw, or tall furniture such as a kitchen island. With `fixtures` (ids) it only shades those lights (e.g. an island that hides one lamp but not the ceiling light above it); empty = all lights. `from_thin_wall` adopts a hairline wall found by floorplan_get(include_thin_walls=True) (e.g. "thin-1") as the blocker's rect."""
        M.check_id(blocker_id, "blocker")
        cfg, model = await _load(url_path)
        before = M.snapshot(model)
        blockers = model.setdefault("blockers", {})
        if remove:
            if blocker_id not in blockers:
                raise FloorplanError(f"no blocker {blocker_id!r}")
            del blockers[blocker_id]
            return await _commit(url_path, cfg, model, before, f"removed blocker {blocker_id}")
        bl = copy.deepcopy(blockers.get(blocker_id) or {})
        if from_thin_wall:
            cand = next((c for c in (await _geometry(model)).thin_walls() if c["id"] == from_thin_wall), None)
            if cand is None:
                raise FloorplanError(f"no thin wall {from_thin_wall!r}; list them with "
                                     "floorplan_get(include_thin_walls=True)")
            x0, y0, x1, y1 = cand["rect"]
            if x1 - x0 < 8:                      # a hairline is 1-2 px: block at least a wall's thickness
                x0, x1 = x0 - (8 - (x1 - x0)) // 2, x0 - (8 - (x1 - x0)) // 2 + 8
            if y1 - y0 < 8:
                y0, y1 = y0 - (8 - (y1 - y0)) // 2, y0 - (8 - (y1 - y0)) // 2 + 8
            w, h = model["size"]
            rect, line = [max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)], None
        if rect is not None or line is not None:
            bl = {k: v for k, v in bl.items() if k not in ("rect", "line")}
            bl.update(M.check_shape(model, rect, line))
        if "rect" not in bl and "line" not in bl:
            raise FloorplanError("a new blocker needs rect, line or from_thin_wall")
        if fixtures is not None:
            unknown = [f for f in fixtures if f not in (model.get("fixtures") or {})]
            if unknown:
                raise FloorplanError(f"unknown fixtures {unknown}")
            bl["fixtures"] = list(fixtures)
        bl.setdefault("fixtures", [])
        blockers[blocker_id] = bl
        return {**await _commit(url_path, cfg, model, before, f"set blocker {blocker_id}"),
                "blocker": {blocker_id: bl}}

    @mcp.tool()
    async def floorplan_calibrate(
        url_path: str, fixture_id: str = "", entity: str = "", size: float | None = None,
        strength: float | None = None, reach_curve: float | None = None, intensity_curve: float | None = None,
        whiten: float | None = None, default_rgb: list[int] | None = None, dim_reach: float | None = None,
        dim_boost: float | None = None, reset: bool = False,
    ) -> dict:
        """[DESTRUCTIVE] Tune how a light's glow looks, live on the dashboard (no mask regeneration, so it's quick). Target one `fixture_id`, or every fixture of an `entity`. Only the parameters you pass change; `reset` returns all to defaults first.
        Mapping plain feedback (compare the dashboard with the real room; "the real light reaches further" means the glow is too small):
        - size (reach; default 1.0): glow radius multiplier. "a little less far" ~ -10%, "quite a bit" ~ -25%.
        - strength (default 1.0): peak opacity. "a little brighter" ~ +8-15%.
        - reach_curve / intensity_curve (default 1.0): how reach / intensity scale with brightness; < 1 keeps dim levels larger/brighter (adjust when 100% is right but 40% is off).
        - dim_reach / dim_boost (default 1.0): extra reach / intensity factor that applies only below 40% brightness, fading in to full at ~0% (use when 100% and 40% are right but 10% is off; e.g. dim_reach 0.9 = 10% smaller at very low brightness).
        - whiten (0-1; default from settings): blend of colour-temperature colours toward white ("whiter in real life" -> raise).
        - default_rgb: colour for lights with no colour attributes (plain on/off switches); "yellower" -> lower blue.
        Returns the values before and after. Undo with floorplan_undo."""
        if bool(fixture_id) == bool(entity):
            raise FloorplanError("give exactly one of fixture_id or entity")
        cfg, model = await _load(url_path)
        fixtures = model.get("fixtures") or {}
        targets = [fixture_id] if fixture_id else [f for f, fx in fixtures.items() if fx["entity"] == entity]
        if not targets or any(t not in fixtures for t in targets):
            raise FloorplanError(f"no fixture for {fixture_id or entity!r}")
        if any(fixtures[t]["kind"] != "light" for t in targets):
            raise FloorplanError("only light fixtures have a glow to calibrate")
        changes = {"size": size, "strength": strength, "reach_curve": reach_curve,
                   "intensity_curve": intensity_curve, "whiten": whiten, "default_rgb": default_rgb,
                   "dim_reach": dim_reach, "dim_boost": dim_boost}
        changes = {k: v for k, v in changes.items() if v is not None}
        for k, v in changes.items():
            if k == "default_rgb":
                if len(v) != 3 or not all(0 <= c <= 255 for c in v):
                    raise FloorplanError("default_rgb is [r, g, b] with 0-255 values")
            elif k == "whiten" and not 0 <= v <= 1:
                raise FloorplanError("whiten is between 0 and 1")
            elif k != "whiten" and not 0 < v <= 5:
                raise FloorplanError(f"{k} must be > 0 and <= 5")
        if not changes and not reset:
            raise FloorplanError("nothing to change")
        before = M.snapshot(model)
        report = {}
        for t in targets:
            fx = fixtures[t]
            old = {k: v for k, v in (fx.get("glow") or {}).items()}
            new = {} if reset else dict(old)
            new.update(changes)
            fx["glow"] = {k: v for k, v in new.items() if v != M.GLOW_DEFAULTS.get(k)}
            report[t] = {"before": old or "defaults", "after": fx["glow"] or "defaults"}
        note = f"calibrated {fixture_id or entity}: " + ", ".join(f"{k}={v}" for k, v in changes.items())
        return {**await _commit(url_path, cfg, model, before, note + (" (reset)" if reset else "")),
                "fixtures": report}

    @mcp.tool()
    async def floorplan_settings(url_path: str, walls: dict | None = None, light: dict | None = None,
                                 transmission: dict | None = None, sun: dict | None = None,
                                 image: str = "", dark_image: str = "") -> dict:
        """[DESTRUCTIVE] Change model-wide settings; only the keys you pass change. Changing walls, the image or soft-shadow/mask settings regenerates every mask (slow).
        walls: threshold (darkness of wall pixels, 60), min_run (30), thickness (7), seal_gap (gaps shorter than this are sealed, 110), line_width (10).
        light: whiten (0.6), color_k (how much light colour dims the glow, 0.5), default_rgb ([255, 225, 185]), radius (soft-shadow source size, 36), samples (24), blur (6), mask_scale (mask downscale, 3).
        transmission: door, window, glass_door, opening, open_air (0-1; light passing a closed opening of that kind).
        sun: sun_entity (sun.sun), weather_entity (weather.forecast_home, its cloud_coverage dims the sun), north_deg (compass bearing of the plan's "up", 0), px_per_m (plan pixels per metre, 90), height_m (2.1), max_px (longest beam, 700), spread (2.9), strength (0.9), cloud_dim (share of strength lost under full cloud, 0.45), blur (18), outdoor_strength (sky-light wash over outdoor areas, 0.3), color ([255, 248, 232]).
        image / dark_image: replace the plan images (same plan, same coordinates)."""
        cfg, model = await _load(url_path)
        before = M.snapshot(model)
        for section, values in (("walls", walls), ("light", light), ("transmission", transmission),
                                ("sun", sun)):
            if not values:
                continue
            defaults = {"walls": M.WALL_DEFAULTS, "light": M.LIGHT_DEFAULTS,
                        "transmission": M.TRANSMISSION_DEFAULTS, "sun": M.SUN_DEFAULTS}[section]
            unknown = set(values) - set(defaults)
            if unknown:
                raise FloorplanError(f"unknown {section} settings {sorted(unknown)}; valid: {sorted(defaults)}")
            model[section] = {**M.settings(model, section), **values}
        if image:
            model["image"] = image
        if dark_image:
            model["dark_image"] = dark_image
        return {**await _commit(url_path, cfg, model, before, "settings changed"),
                "settings": {s: M.settings(model, s) for s in ("walls", "light", "transmission", "sun")}}

    @mcp.tool()
    async def floorplan_audit(url_path: str) -> dict:
        """Check a floor plan for setup mistakes that otherwise show up as nothing at all. Read-only.
        Reports warnings for: windows, glass doors and open_air edges with no `facing` (they throw no sun); sun beams that depend on an opening that was removed; entities that don't exist in Home Assistant (fixtures, door sensors, the sun and weather entities, a weather entity with no cloud_coverage); hairline walls the detector missed that run wall to wall and have no blocker (light and sun leak through them: see floorplan_get(include_thin_walls=True)). Also notes how big the dashboard config is. Run it after building or changing a plan, and before declaring it done."""
        cfg, model = await _load(url_path)
        warnings, notes = [], []
        openings = model.get("openings") or {}
        for oid, op in sorted(openings.items()):
            if op.get("kind") in M.SUN_KINDS and not op.get("facing"):
                hint = ""
                if "rect" in op:
                    geo = await _geometry(model)
                    side = engine.suggest_facing(op["rect"], geo.sealed | geo.global_blockers)
                    hint = f" (looks out {side})" if side else ""
                warnings.append(f"opening {oid} is a {op['kind']} with no facing, so it throws no sun{hint}")
            through = (op.get("sun") or {}).get("through")
            if through and through not in openings:
                warnings.append(f"opening {oid} gets its sun through {through!r}, which no longer exists (not gated)")
        if any(o.get("kind") == "open_air" for o in openings.values()) and not model.get("outdoors"):
            notes.append("an open_air edge but no outdoor area: add floorplan_set_outdoor for the sky-light wash")
        sun = M.settings(model, "sun")
        wanted = sorted({fx["entity"] for fx in (model.get("fixtures") or {}).values()}
                        | {op["sensor"] for op in openings.values() if op.get("sensor")}
                        | ({sun["sun_entity"], sun["weather_entity"]} if M.sun_windows(model) or model.get("outdoors") else set()))
        try:
            async with make_client(config.homeassistant.url, headers=ha._headers(), timeout=30.0) as client:
                states = await _states(client, wanted)
        except Exception as err:                      # noqa: BLE001 - report, don't fail the audit
            states = None
            notes.append(f"couldn't read Home Assistant states ({err}); entity checks skipped")
        if states is not None:
            for ent in wanted:
                if ent not in states:
                    warnings.append(f"entity {ent} does not exist in Home Assistant")
            weather = states.get(sun["weather_entity"])
            if weather and "cloud_coverage" not in (weather.get("attributes") or {}):
                warnings.append(f"{sun['weather_entity']} has no cloud_coverage attribute, so clouds won't dim the sun")
        open_lines = [c for c in (await _geometry(model)).thin_walls() if not c["blocked"]]
        # A partition drawn as a pair of lines is ~10 px thick; a single hairline is a table or tub edge, and
        # lines on the plan's edge are window panes.
        for c in [c for c in open_lines if c["ends_on_walls"] == 2 and c["length"] >= 150
                  and c["thickness"] >= 8 and not c["on_plan_edge"]]:
            warnings.append(f"{c['id']} {c['rect']} is a {c['length']} px double hairline running wall to wall that "
                            "nothing blocks, so light and sun pass through it: check "
                            "floorplan_preview(show_thin_walls=True); if it is a wall (not a tub or counter edge), "
                            f"floorplan_set_blocker(from_thin_wall={c['id']!r})")
        if open_lines:
            notes.append(f"{len(open_lines)} hairline candidates are unblocked (furniture outlines count); "
                         "only double-line walls running wall to wall are warned about")
        notes.append(f"dashboard config is {len(json.dumps(cfg)) // 1024} KiB; "
                     f"{len(model.get('fixtures') or {})} fixtures, {len(openings)} openings")
        return {"ok": not warnings, "warnings": warnings, "notes": notes}

    @mcp.tool()
    async def floorplan_undo(url_path: str) -> dict:
        """[DESTRUCTIVE] Revert the floor plan to before its last change (fixtures, openings, blockers, calibration, settings) and update the dashboard. Up to 10 steps are kept; each undo uses one."""
        cfg, model = await _load(url_path)
        hist = model.get("history") or []
        if not hist:
            raise FloorplanError("nothing to undo")
        last = hist.pop()
        restored = {**copy.deepcopy(last["model"]), "history": hist, "session": model.get("session")}
        result = await _commit(url_path, cfg, restored, None, f"undid: {last['note']}")
        return {**result, "undone": last["note"], "steps_left": len(hist)}

    @mcp.tool()
    async def floorplan_rebuild(url_path: str, force: bool = False) -> dict:
        """[DESTRUCTIVE] Regenerate the floor-plan card from its model: masks whose inputs changed (or all with `force`), glows and icons. Use after the plan image file itself was edited."""
        cfg, model = await _load(url_path)
        if force:
            _raster_cache.clear()
        rebuild = {f for f, fx in (model.get("fixtures") or {}).items() if fx["kind"] == "light"} if force else None
        return await _commit(url_path, cfg, model, None, "rebuilt", rebuild=rebuild, force_sun=force)

    @mcp.tool()
    async def floorplan_calibration(url_path: str, action: str, entity: str = "", brightness_pct: float = 100,
                                    color_temp_kelvin: int = 0, rgb_color: list[int] | None = None,
                                    pause_automations: list[str] | None = None) -> dict:
        """[DESTRUCTIVE] Drive the real lights for a calibration session (controls physical devices; confirm with the user first).
        action "start": snapshots every floor-plan light's state, turns off the given `pause_automations` (motion lights, circadian colour etc. that would change lights mid-test) and turns all floor-plan lights off. Only one session at a time.
        action "show": turns `entity` on alone at `brightness_pct` (with `rgb_color`, or `color_temp_kelvin`, default 2700 K warm white on lights that support it; any running effect is cleared), every other floor-plan light off, so the user can compare the room with the dashboard. Then adjust with floorplan_calibrate in plain-word steps and repeat; a typical pass is 100/40/10 % warm white per light, then cool white and a saturated colour on colour lights.
        action "end": restores every snapshotted light (on/off, brightness, colour, effect) and turns the paused automations back on.
        The snapshot is stored in the dashboard, so a session survives restarts; always end it."""
        if action not in ("start", "show", "end"):
            raise FloorplanError("action is start, show or end")
        cfg, model = await _load(url_path)
        entities = sorted({fx["entity"] for fx in (model.get("fixtures") or {}).values()
                           if fx["kind"] == "light"})
        session = model.get("session")
        async with make_client(config.homeassistant.url, headers=ha._headers()) as client:
            if action == "start":
                if session:
                    raise FloorplanError(f"a calibration session is already running (since {session['started']}); "
                                         "end it first")
                paused = [M.check_entity(a, "automation") for a in pause_automations or []]
                states = await _states(client, entities)
                session = {"started": _now(), "states": states, "paused": paused}
                model["session"] = session
                cfg[M.MODEL_KEY] = model
                await _save(url_path, cfg)       # persist the snapshot before touching anything
                if paused:
                    await _service(client, "automation", "turn_off", {"entity_id": paused})
                for e in entities:
                    await _service(client, e.split(".", 1)[0], "turn_off", {"entity_id": e})
                return {"started": session["started"], "lights": entities, "paused": paused,
                        "snapshot": {e: s["state"] for e, s in states.items()}}
            if not session:
                raise FloorplanError("no calibration session; start one first")
            if action == "show":
                if entity not in entities:
                    raise FloorplanError(f"{entity!r} is not a light on this floor plan")
                for e in entities:
                    if e != entity:
                        await _service(client, e.split(".", 1)[0], "turn_off", {"entity_id": e})
                domain = entity.split(".", 1)[0]
                data = {"entity_id": entity}
                if domain == "light":
                    attrs = (await _states(client, [entity])).get(entity, {}).get("attributes", {})
                    data["brightness_pct"] = brightness_pct
                    if rgb_color:
                        data["rgb_color"] = rgb_color
                    elif color_temp_kelvin or "color_temp" in (attrs.get("supported_color_modes") or []):
                        # Same warm white every step keeps 100/40/10 % comparable.
                        data["color_temp_kelvin"] = color_temp_kelvin or _WARM_WHITE_K
                    # A Hue effect (e.g. "fireplace") survives turn_off and would override the test colour.
                    if "none" in (attrs.get("effect_list") or []):
                        data["effect"] = "none"
                await _service(client, domain, "turn_on", data)
                return {"showing": entity, **{k: v for k, v in data.items() if k != "entity_id"}}
            # end
            restored = []
            for e, st in session["states"].items():
                service, data = _restore_payload(st)
                domain, name = service.split(".")
                await _service(client, domain, name, data)
                restored.append({"entity": e, "to": st["state"]})
            if session.get("paused"):
                await _service(client, "automation", "turn_on", {"entity_id": session["paused"]})
        model["session"] = None
        cfg[M.MODEL_KEY] = model
        await _save(url_path, cfg)
        return {"ended": True, "restored": restored, "automations_resumed": session.get("paused", [])}

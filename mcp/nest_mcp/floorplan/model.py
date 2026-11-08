"""The floor-plan model: plain JSON kept in the dashboard config under ``nest_floorplan``.

Shape (every key optional except image/size; defaults below fill the rest)::

    {
      "version": 1,
      "image": "/local/plan.svg", "dark_image": "/local/plan-dark.svg", "size": [W, H],
      "walls": {...WALL_DEFAULTS},
      "light": {...LIGHT_DEFAULTS},
      "transmission": {...TRANSMISSION_DEFAULTS},
      "sun": {...SUN_DEFAULTS},     # sunlight through windows (and over outdoor areas)
      "outdoors": {id: {"x", "y"}}, # open-air areas (a balcony): lit whenever the sun is up
      "openings": {id: {"kind", "rect" | "line", "state", "sensor", "transmission",
                        "facing"}},   # facing: n/e/s/w, the way a window looks out (plan up = n)
      "blockers": {id: {"rect" | "line", "fixtures": [fixture ids, empty = all]}},
      "fixtures": {id: {"entity", "kind", "x", "y", "icon", "icon_x", "icon_y",
                        "icon_anchor", "icon_pad", "icon_scale", "icon_offset", "glow": {...}}},
      "session": {...} | null,      # live calibration session (light snapshot)
      "history": [...]              # previous models, newest last (for undo)
    }

Coordinates are plan pixels (the SVG viewBox / raster size). rects are
[x0, y0, x1, y1) and lines [x0, y0, x1, y1].
"""
import copy
import hashlib
import json
import re

import numpy as np

from nest_mcp.floorplan import engine

MODEL_KEY = "nest_floorplan"
CARD_MARKER = "nest_floorplan_card"
HISTORY_LIMIT = 10

WALL_DEFAULTS = {"threshold": 60, "min_run": 30, "thickness": 7, "seal_gap": 110, "line_width": 10}
LIGHT_DEFAULTS = {"whiten": 0.6, "color_k": 0.5, "default_rgb": [255, 225, 185],
                  "radius": 36, "samples": 24, "blur": 6, "mask_scale": 3}
TRANSMISSION_DEFAULTS = {"door": 0.0, "window": 0.7, "glass_door": 0.35, "opening": 1.0, "open_air": 1.0}
SUN_DEFAULTS = {
    "sun_entity": "sun.sun", "weather_entity": "weather.forecast_home",
    "north_deg": 0,        # compass bearing of the plan's "up" (0 = north is up)
    "px_per_m": 90,        # plan pixels per metre (interior doors are ~80-90 px)
    "height_m": 2.1,       # how far up the window the beam starts to fall (sets its length)
    "max_px": 700,         # longest beam
    "spread": 2.9,         # width at the far end relative to the window
    "strength": 0.9,       # opacity of a beam at the window in full sun
    "cloud_dim": 0.45,     # share of the strength lost under complete cloud cover
    "blur": 18,            # softness of beam edges (plan px)
    "outdoor_strength": 0.3,  # opacity of the wash over open-air areas
    "color": [255, 248, 232],
}
GLOW_DEFAULTS = {"size": 1.0, "strength": 1.0, "reach_curve": 1.0, "intensity_curve": 1.0,
                 "whiten": None, "default_rgb": None, "dim_reach": 1.0, "dim_boost": 1.0}

FIXTURE_KINDS = {"light", "fan", "device"}
OPENING_KINDS = set(TRANSMISSION_DEFAULTS)
SUN_KINDS = {"window", "glass_door", "open_air"}   # openings that let sunlight in when they have a facing
# Per-opening overrides of the sun settings: an open-air edge under a ceiling has its own height.
SUN_OVERRIDES = {"height_m": float, "spread": float, "blur": float, "strength": float, "full_until": float}
FACINGS = {"n": (0, -1), "e": (1, 0), "s": (0, 1), "w": (-1, 0)}   # outward normals, plan coords
OPENING_STATES = {"open", "closed", "sensor"}
ANCHORS = {"center", "n", "s", "e", "w", "ne", "nw", "se", "sw"}
MAX_SENSORS_PER_FIXTURE = 2
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class ModelError(ValueError):
    """Bad input for the model; the message is meant for the caller."""


def new_model(image: str, size: tuple[int, int], dark_image: str = "") -> dict:
    return {"version": 1, "image": image, "dark_image": dark_image, "size": [int(size[0]), int(size[1])],
            "walls": dict(WALL_DEFAULTS), "light": dict(LIGHT_DEFAULTS),
            "transmission": dict(TRANSMISSION_DEFAULTS), "sun": dict(SUN_DEFAULTS), "outdoors": {},
            "openings": {}, "blockers": {}, "fixtures": {}, "session": None, "history": []}


def settings(model: dict, section: str) -> dict:
    defaults = {"walls": WALL_DEFAULTS, "light": LIGHT_DEFAULTS, "transmission": TRANSMISSION_DEFAULTS,
                            "sun": SUN_DEFAULTS}[section]
    return {**defaults, **(model.get(section) or {})}


def glow(model: dict, fixture: dict) -> dict:
    """A fixture's glow parameters with model-wide defaults resolved."""
    light = settings(model, "light")
    g = {**GLOW_DEFAULTS, **(fixture.get("glow") or {})}
    if g["whiten"] is None:
        g["whiten"] = light["whiten"]
    if g["default_rgb"] is None:
        g["default_rgb"] = list(light["default_rgb"])
    g["color_k"] = light["color_k"]
    return g


# --- Validation -------------------------------------------------------------

def check_id(value: str, what: str) -> str:
    if not _ID_RE.match(value or ""):
        raise ModelError(f"{what} id {value!r}: use lowercase letters, digits, '-' or '_' (max 41 chars)")
    return value


def check_entity(value: str, what: str = "entity") -> str:
    if not _ENTITY_RE.match(value or ""):
        raise ModelError(f"{what} {value!r} is not an entity id like light.kitchen")
    return value


def check_point(model: dict, x: float, y: float, what: str) -> None:
    w, h = model["size"]
    if not (0 <= x < w and 0 <= y < h):
        raise ModelError(f"{what} ({x}, {y}) is outside the plan (0..{w - 1}, 0..{h - 1})")


def check_shape(model: dict, rect, line) -> dict:
    if (rect is None) == (line is None):
        raise ModelError("give exactly one of rect [x0, y0, x1, y1] or line [x0, y0, x1, y1]")
    coords = rect if rect is not None else line
    if len(coords) != 4:
        raise ModelError("rect/line needs four numbers: x0, y0, x1, y1")
    check_point(model, coords[0], coords[1], "start")
    w, h = model["size"]
    if not (0 <= coords[2] <= w and 0 <= coords[3] <= h):
        raise ModelError(f"end ({coords[2]}, {coords[3]}) is outside the plan ({w} x {h})")
    if rect is not None and (rect[2] <= rect[0] or rect[3] <= rect[1]):
        raise ModelError("rect needs x1 > x0 and y1 > y0")
    return {"rect": [int(v) for v in rect]} if rect is not None else {"line": [int(v) for v in line]}


def validate(model: dict) -> dict:
    """Check a whole model (e.g. an import) and return it normalised; raises ModelError."""
    if not isinstance(model, dict):
        raise ModelError("model must be an object")
    if not model.get("image") or not isinstance(model.get("size"), list) or len(model["size"]) != 2:
        raise ModelError("model needs image and size [w, h]")
    out = new_model(model["image"], tuple(model["size"]), model.get("dark_image") or "")
    for section, defaults in (("walls", WALL_DEFAULTS), ("light", LIGHT_DEFAULTS),
                              ("transmission", TRANSMISSION_DEFAULTS), ("sun", SUN_DEFAULTS)):
        values = model.get(section) or {}
        unknown = set(values) - set(defaults)
        if unknown:
            raise ModelError(f"unknown {section} settings {sorted(unknown)}")
        out[section] = {**defaults, **values}
    for fid, fx in (model.get("fixtures") or {}).items():
        check_id(fid, "fixture")
        check_entity(fx.get("entity", ""))
        if fx.get("kind") not in FIXTURE_KINDS:
            raise ModelError(f"fixture {fid}: kind must be one of {sorted(FIXTURE_KINDS)}")
        check_point(out, fx.get("x", -1), fx.get("y", -1), f"fixture {fid}")
        if fx.get("icon_anchor", "center") not in ANCHORS:
            raise ModelError(f"fixture {fid}: icon_anchor must be one of {sorted(ANCHORS)}")
        unknown = set(fx.get("glow") or {}) - set(GLOW_DEFAULTS)
        if unknown:
            raise ModelError(f"fixture {fid}: unknown glow parameters {sorted(unknown)}")
        out["fixtures"][fid] = copy.deepcopy(fx)
    for oid, op in (model.get("openings") or {}).items():
        check_id(oid, "opening")
        shape = check_shape(out, op.get("rect"), op.get("line"))
        if op.get("kind") not in OPENING_KINDS or op.get("state", "closed") not in OPENING_STATES:
            raise ModelError(f"opening {oid}: bad kind or state")
        if op.get("state") == "sensor":
            check_entity(op.get("sensor", ""), f"opening {oid} sensor")
        if op.get("facing") is not None:
            if op.get("kind") not in SUN_KINDS:
                raise ModelError(f"opening {oid}: only windows and glass doors can have a facing")
            unknown = set(op.get("sun") or {}) - set(SUN_OVERRIDES)
            if unknown:
                raise ModelError(f"opening {oid}: unknown sun overrides {sorted(unknown)}")
            window_frame({**op, **shape})
        out["openings"][oid] = {**{k: v for k, v in op.items() if k not in ("rect", "line")}, **shape}
    for bid, bl in (model.get("blockers") or {}).items():
        check_id(bid, "blocker")
        shape = check_shape(out, bl.get("rect"), bl.get("line"))
        unknown = [f for f in bl.get("fixtures") or [] if f not in out["fixtures"]]
        if unknown:
            raise ModelError(f"blocker {bid}: unknown fixtures {unknown}")
        out["blockers"][bid] = {**shape, "fixtures": list(bl.get("fixtures") or [])}
    for aid, area in (model.get("outdoors") or {}).items():
        check_id(aid, "outdoor area")
        check_point(out, area.get("x", -1), area.get("y", -1), f"outdoor area {aid}")
        out["outdoors"][aid] = {"x": area["x"], "y": area["y"]}
    return out


def window_frame(op: dict) -> dict:
    """Geometry of a window opening for sunlight: centre, wall direction, half length, normals.

    `normal_out` points outside (the way the window looks), `normal_in` into the room.
    Raises ModelError when the facing doesn't fit the wall the window sits in.
    """
    if "rect" in op:
        x0, y0, x1, y1 = op["rect"]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        horizontal = (x1 - x0) >= (y1 - y0)
        tx, ty = (1.0, 0.0) if horizontal else (0.0, 1.0)
        half = ((x1 - x0) if horizontal else (y1 - y0)) / 2
    else:
        x0, y0, x1, y1 = op["line"]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        length = max(float(np.hypot(x1 - x0, y1 - y0)), 1.0)
        tx, ty, half = (x1 - x0) / length, (y1 - y0) / length, length / 2
    facing = op.get("facing")
    if facing not in FACINGS:
        raise ModelError(f"facing must be one of {sorted(FACINGS)}")
    nx, ny = FACINGS[facing]
    if abs(nx * tx + ny * ty) > 0.5:
        raise ModelError(f"facing {facing!r} runs along this window's wall; use the direction it looks out")
    return {"mx": mx, "my": my, "tx": tx, "ty": ty, "half": half,
            "normal_out": (nx, ny), "normal_in": (-nx, -ny), "sun": dict(op.get("sun") or {})}


# --- History ----------------------------------------------------------------

def snapshot(model: dict) -> dict:
    return {k: copy.deepcopy(v) for k, v in model.items() if k not in ("history", "session")}


def push_history(model: dict, before: dict, note: str, at: str) -> None:
    hist = model.setdefault("history", [])
    hist.append({"at": at, "note": note, "model": before})
    del hist[:-HISTORY_LIMIT]


# --- Geometry from the model -------------------------------------------------

def _shape_mask(shape, item: dict, line_width: int) -> np.ndarray:
    if "rect" in item:
        return engine.draw_mask(shape, rects=[item["rect"]])
    return engine.draw_mask(shape, lines=[item["line"]], width=line_width)


class Geometry:
    """Barrier rasters for one model on one plan raster."""

    def __init__(self, model: dict, gray: np.ndarray):
        self.model = model
        self.gray = gray
        walls = settings(model, "walls")
        self.raw, self.sealed = engine.detect_walls(
            gray, walls["threshold"], walls["min_run"], walls["thickness"], walls["seal_gap"])
        self.line_width = walls["line_width"]
        shape = gray.shape
        trans = settings(model, "transmission")
        self.global_blockers = np.zeros(shape, bool)
        self.fixture_blockers: dict[str, np.ndarray] = {}
        for bl in (model.get("blockers") or {}).values():
            px = _shape_mask(shape, bl, self.line_width)
            targets = bl.get("fixtures") or []
            if not targets:
                self.global_blockers |= px
            for fid in targets:
                self.fixture_blockers[fid] = self.fixture_blockers.get(fid, np.zeros(shape, bool)) | px
        # Each opening's pixels. A line is drawn across a gap (diagonal doors, gaps
        # too wide to seal) and is a wall when closed. A rect covers what blocks the
        # gap inside it: the sealing, plus any wall drawn there by hand, but never
        # the plan's own walls.
        line_walls = np.zeros(shape, bool)
        for op in (model.get("openings") or {}).values():
            if "line" in op:
                line_walls |= _shape_mask(shape, op, self.line_width)
        self.openings = {}
        for oid, op in (model.get("openings") or {}).items():
            if "rect" in op:
                blocked = self.sealed | line_walls | self.global_blockers
                px = _shape_mask(shape, op, self.line_width) & blocked & ~self.raw
            else:
                px = _shape_mask(shape, op, self.line_width)
            t = op.get("transmission")
            self.openings[oid] = {**op, "pixels": px,
                                  "closed_t": trans[op["kind"]] if t is None else float(t)}
        self.base = self.sealed | line_walls

    def gaps(self) -> list[dict]:
        return engine.find_gaps(self.gray, self.raw, self.sealed)

    def barrier_for(self, fixture_id: str, sensors_open: dict[str, bool]) -> tuple[np.ndarray, list]:
        """(barrier, partial-transmission groups) for a fixture and a sensor assignment."""
        b = self.base | self.global_blockers
        if fixture_id in self.fixture_blockers:
            b = b | self.fixture_blockers[fixture_id]
        groups: dict[float, np.ndarray] = {}
        for op in self.openings.values():
            state = op.get("state", "closed")
            if state == "open" or (state == "sensor" and sensors_open.get(op.get("sensor"), False)):
                t = 1.0
            else:
                t = op["closed_t"]
            if t >= 1.0:
                b = b & ~op["pixels"]
            elif t > 0:
                groups[t] = groups.get(t, np.zeros_like(b)) | op["pixels"]
        return b, [(t, px & b) for t, px in sorted(groups.items())]

    def sensors(self) -> list[str]:
        return sorted({op["sensor"] for op in self.openings.values()
                       if op.get("state") == "sensor" and op.get("sensor")})

    def lit(self, fixture_id: str, sensors_open: dict[str, bool]) -> np.ndarray:
        fx = self.model["fixtures"][fixture_id]
        light = settings(self.model, "light")
        b, partial = self.barrier_for(fixture_id, sensors_open)
        return engine.lit(b, fx["x"], fx["y"], partial,
                          radius=light["radius"], samples=light["samples"], blur=light["blur"])


def fixture_key(model: dict, fixture_id: str) -> str:
    """Hash of every input that shapes a fixture's masks; masks with this key can be reused."""
    fx = model["fixtures"][fixture_id]
    relevant = {
        "image": model["image"], "size": model["size"],
        "walls": settings(model, "walls"), "light": {k: settings(model, "light")[k]
                                                    for k in ("radius", "samples", "blur", "mask_scale")},
        "transmission": settings(model, "transmission"),
        "openings": model.get("openings") or {},
        "blockers": {k: v for k, v in (model.get("blockers") or {}).items()
                     if not v.get("fixtures") or fixture_id in v["fixtures"]},
        "pos": [fx["x"], fx["y"]],
    }
    return hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:12]


def sun_windows(model: dict) -> dict[str, dict]:
    """{opening id: window frame} for every window or glass door that has a facing."""
    return {oid: window_frame(op) for oid, op in sorted((model.get("openings") or {}).items())
            if op.get("kind") in SUN_KINDS and op.get("facing")}


def sun_key(model: dict, what: str, item: dict) -> str:
    """Hash of every input that shapes a sun mask (a window's reach or an outdoor area)."""
    relevant = {
        "image": model["image"], "size": model["size"],
        "walls": settings(model, "walls"), "transmission": settings(model, "transmission"),
        # The per-opening sun overrides only shape the beam template, never a mask.
        "openings": {k: {a: b for a, b in v.items() if a != "sun"} for k, v in (model.get("openings") or {}).items()},
        "blockers": {k: v for k, v in (model.get("blockers") or {}).items() if not v.get("fixtures")},
        "what": what, "item": {a: b for a, b in item.items() if a != "sun"},
        "scale": settings(model, "light")["mask_scale"],
    }
    return hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:12]


def sun_masks(geo: Geometry, only: set[str] | None = None) -> dict[str, np.ndarray]:
    """{"window:<id>" | "outdoor:<id>": mask} for each window's reach and outdoor area.

    Doors that follow a sensor are taken as closed, so sunlight never leaks into
    a room behind one. `only` limits the work to some of the names.
    """
    barrier, _ = geo.barrier_for("", {})
    out = {}
    for oid, fr in sun_windows(geo.model).items():
        if only is not None and f"window:{oid}" not in only:
            continue
        out[f"window:{oid}"] = engine.window_reach(
            barrier, fr["mx"], fr["my"], fr["tx"], fr["ty"], fr["half"], *fr["normal_in"],
            samples=max(5, int(fr["half"] / 25)))
    for aid, area in (geo.model.get("outdoors") or {}).items():
        if only is not None and f"outdoor:{aid}" not in only:
            continue
        out[f"outdoor:{aid}"] = engine.fill_region(barrier, area["x"], area["y"])
    return out


def mask_variants(geo: Geometry, fixture_id: str) -> tuple[list[tuple[dict, np.ndarray]], list[str]]:
    """[(sensor assignment, mask)] for a fixture, plus warnings.

    Only sensors whose door actually changes what this fixture lights get a
    variant; the most influential MAX_SENSORS_PER_FIXTURE are kept.
    """
    closed_all = {s: False for s in geo.sensors()}
    base = geo.lit(fixture_id, closed_all)
    influence = []
    single = {}
    for s in closed_all:
        m = geo.lit(fixture_id, {**closed_all, s: True})
        n = engine.differs(base, m)
        if n:
            influence.append((n, s))
            single[s] = m
    influence.sort(reverse=True)
    warnings = []
    if len(influence) > MAX_SENSORS_PER_FIXTURE:
        dropped = [s for _, s in influence[MAX_SENSORS_PER_FIXTURE:]]
        warnings.append(f"{fixture_id}: only the {MAX_SENSORS_PER_FIXTURE} most influential door sensors "
                        f"gate its glow; ignoring {', '.join(dropped)}")
    used = sorted(s for _, s in influence[:MAX_SENSORS_PER_FIXTURE])
    variants = []
    for bits in range(2 ** len(used)):
        assign = {s: bool(bits >> i & 1) for i, s in enumerate(used)}
        opened = [s for s, v in assign.items() if v]
        if not opened:
            mask = base
        elif len(opened) == 1:
            mask = single[opened[0]]
        else:
            mask = geo.lit(fixture_id, {**closed_all, **assign})
        variants.append((assign, mask))
    return variants, warnings


def encoded_variants(geo: Geometry, fixture_id: str) -> tuple[list[tuple[dict, str]], list[str]]:
    """mask_variants with each mask already encoded as a data URI (small to pass between processes)."""
    scale = settings(geo.model, "light")["mask_scale"]
    variants, warnings = mask_variants(geo, fixture_id)
    return [(assign, engine.mask_data_uri(mask, scale)) for assign, mask in variants], warnings


_WORKER_GEO: "Geometry | None" = None


def _worker_init(geo: Geometry) -> None:
    global _WORKER_GEO
    _WORKER_GEO = geo


def _worker_run(fixture_id: str):
    return fixture_id, *encoded_variants(_WORKER_GEO, fixture_id)


def compute_parallel(geo: Geometry, fixture_ids: list[str], workers: int) -> dict[str, tuple]:
    """{fixture id: (encoded variants, warnings)}, spread over `workers` processes.

    Ray casting is pure-Python-loop heavy (GIL-bound), so threads don't help; a
    full rebuild of a dozen lights drops from ~25 s to under 10 s on 4 cores.
    """
    if workers <= 1 or len(fixture_ids) <= 1:
        return {f: encoded_variants(geo, f) for f in fixture_ids}
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    # forkserver, not fork: the server process runs threads (asyncio.to_thread).
    ctx = multiprocessing.get_context("forkserver")
    with ProcessPoolExecutor(max_workers=min(workers, len(fixture_ids)), mp_context=ctx,
                             initializer=_worker_init, initargs=(geo,)) as pool:
        return {fid: (variants, warnings) for fid, variants, warnings in pool.map(_worker_run, fixture_ids)}


# --- Glow maths (mirror of the card_mod template, for previews and tests) -----

def glow_shape(g: dict, plan_width: int, brightness: float = 1.0,
               rgb: tuple[int, int, int] | None = None, color_temp: bool = True) -> dict:
    """Pixel radius, peak alpha and colour the template produces for a light state."""
    c = list(rgb) if rgb else list(g["default_rgb"])
    w = g["whiten"] if color_temp else 0
    c = [int(v + (255 - v) * w) for v in c]
    lum = (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255
    b = brightness
    dimf = g["dim_reach"] + (1 - g["dim_reach"]) * min(b / 0.4, 1)
    dimg = 1 + (g["dim_boost"] - 1) * (1 - min(b / 0.4, 1))
    rx = (14 + 20 * b ** g["reach_curve"]) * g["size"] * dimf
    a = min((0.25 + 0.35 * b ** g["intensity_curve"]) * g["strength"] * dimg * lum ** g["color_k"], 0.95)
    return {"radius_px": rx / 100 * plan_width, "alpha": a, "rgb": c}

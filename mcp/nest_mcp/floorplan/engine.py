"""Raster geometry for floor plans: walls, door/window gaps, soft line-of-sight masks.

Everything works on a boolean "barrier" raster at the plan's own pixel size
(the SVG viewBox), so coordinates in the model are plan pixels.
"""
import base64
import io
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


# --- Morphology (numpy only) ------------------------------------------------

def _window(a: np.ndarray, kh: int, kw: int, need_all: bool) -> np.ndarray:
    """True where all (erode) or any (dilate) pixels of a kh x kw window are set.

    The window is anchored at its centre, like OpenCV's default, and the border
    never affects the result (padded with 1 for erode, 0 for dilate).
    """
    ah, aw = kh // 2, kw // 2
    p = np.pad(a.astype(np.int32), ((ah, kh - 1 - ah), (aw, kw - 1 - aw)),
               constant_values=1 if need_all else 0)
    s = np.pad(p.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    total = s[kh:, kw:] - s[:-kh, kw:] - s[kh:, :-kw] + s[:-kh, :-kw]
    return total == kh * kw if need_all else total > 0


def _open(a, kh, kw):
    return _window(_window(a, kh, kw, True), kh, kw, False)


def _close(a, kh, kw):
    return _window(_window(a, kh, kw, False), kh, kw, True)


def detect_walls(gray: np.ndarray, threshold: int = 60, min_run: int = 30,
                 thickness: int = 7, seal_gap: int = 110) -> tuple[np.ndarray, np.ndarray]:
    """(raw, sealed) wall rasters from a greyscale plan.

    raw: dark horizontal/vertical runs at least `thickness` thick and `min_run`
    long, so furniture outlines, door leaves and window panes drop out.
    sealed: raw plus every gap along a wall shorter than `seal_gap` closed, so
    doorways and windows block light until the model opens them.
    """
    dark = gray < threshold
    hw = _open(dark, thickness, min_run)
    vw = _open(dark, min_run, thickness)
    raw = hw | vw
    sealed = raw | _close(hw, 1, seal_gap) | _close(vw, seal_gap, 1)
    return raw, sealed


def draw_mask(shape: tuple[int, int], rects=(), lines=(), width: int = 10) -> np.ndarray:
    """Boolean raster of filled rects [x0, y0, x1, y1) and lines of `width` px."""
    img = Image.new("1", (shape[1], shape[0]), 0)
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in rects:
        d.rectangle([min(x0, x1), min(y0, y1), max(x0, x1) - 1, max(y0, y1) - 1], fill=1)
    for x0, y0, x1, y1 in lines:
        d.line([x0, y0, x1, y1], fill=1, width=width)
    return np.array(img, dtype=bool)


def find_gaps(gray: np.ndarray, raw: np.ndarray, sealed: np.ndarray,
              line_threshold: int = 200, min_pixels: int = 40) -> list[dict]:
    """Wall gaps the sealing closed: each is a candidate door or window.

    A gap with thin dark lines inside it (a drawn window pane) is guessed to be a
    window; an empty one a door. Ids are stable for a given plan and wall settings.
    """
    seal = sealed & ~raw
    labels = _label(seal)
    gaps = []
    for lab in range(1, labels.max() + 1):
        ys, xs = np.nonzero(labels == lab)
        if xs.size < min_pixels:
            continue
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        # Door swings are thin anti-aliased strokes, much lighter than walls.
        thin = (gray < line_threshold) & ~raw
        gaps.append({"rect": [x0, y0, x1, y1], "pixels": int(xs.size),
                     "guess": _guess_kind(thin, (x0, y0, x1, y1))})
    gaps.sort(key=lambda g: (g["rect"][1], g["rect"][0]))
    for i, g in enumerate(gaps, 1):
        g["id"] = f"gap-{i}"
    return gaps


def _guess_kind(thin: np.ndarray, rect: tuple[int, int, int, int], margin: int = 4) -> str:
    """Door if a swing (leaf + arc) is drawn next to the gap, otherwise window.

    Plans draw door frames and window panes alike (lines across the gap), so the
    gap itself doesn't tell them apart; the quarter-circle swing beside it does.
    A leaf plus arc of radius L is about 2.5 L of line; ask for 1.2 L. The search
    square reaches `margin` px past the jambs, where the leaf usually starts.
    """
    x0, y0, x1, y1 = rect
    h, w = thin.shape
    if x1 - x0 >= y1 - y0:          # horizontal wall: swing is above or below
        length = x1 - x0
        a, b = max(0, x0 - margin), min(w, x1 + margin)
        sides = [thin[max(0, y0 - length):y0, a:b], thin[y1:min(h, y1 + length), a:b]]
    else:                            # vertical wall: swing is left or right
        length = y1 - y0
        a, b = max(0, y0 - margin), min(h, y1 + margin)
        sides = [thin[a:b, max(0, x0 - length):x0], thin[a:b, x1:min(w, x1 + length)]]
    return "door" if max(int(s.sum()) for s in sides) >= 1.2 * length else "window"


def _label(mask: np.ndarray) -> np.ndarray:
    """8-connected component labels (0 = background). Fine for sparse masks."""
    labels = np.zeros(mask.shape, np.int32)
    h, w = mask.shape
    current = 0
    for y, x in zip(*np.nonzero(mask)):
        if labels[y, x]:
            continue
        current += 1
        labels[y, x] = current
        stack = [(y, x)]
        while stack:
            cy, cx = stack.pop()
            for ny in (cy - 1, cy, cy + 1):
                for nx in (cx - 1, cx, cx + 1):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not labels[ny, nx]:
                        labels[ny, nx] = current
                        stack.append((ny, nx))
    return labels


# --- Line of sight ----------------------------------------------------------

# 900 rays at 1.5 px steps match 1440 at 1 px once the soft shadow is blurred,
# at ~60 % of the cost. Steps must stay well under the 7 px minimum wall thickness.
_N_RAYS = 900
_ANGLES = np.linspace(0, 2 * np.pi, _N_RAYS, endpoint=False)
_DX, _DY = np.cos(_ANGLES), np.sin(_ANGLES)


def point_cast(barrier: np.ndarray, sx: float, sy: float, step: float = 1.5) -> np.ndarray:
    """Area visible from a point (uint8 0/255): rays march until they hit a barrier.

    `step` must stay below the thinnest barrier (walls are >= 7 px) or rays tunnel.
    """
    h, w = barrier.shape
    x = np.full(_N_RAYS, float(sx))
    y = np.full(_N_RAYS, float(sy))
    live = np.arange(_N_RAYS)
    while live.size:
        nx = x[live] + _DX[live] * step
        ny = y[live] + _DY[live] * step
        ix, iy = np.rint(nx).astype(np.int64), np.rint(ny).astype(np.int64)
        ok = (ix >= 0) & (iy >= 0) & (ix < w) & (iy < h)
        ok[ok] = ~barrier[iy[ok], ix[ok]]
        x[live[ok]] = nx[ok]
        y[live[ok]] = ny[ok]
        live = live[ok]
    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).polygon(list(zip(x.tolist(), y.tolist())), fill=255)
    return np.array(img)


def _clear_line(barrier: np.ndarray, x0, y0, x1, y1) -> bool:
    n = int(max(abs(x1 - x0), abs(y1 - y0))) + 2
    xs = np.rint(np.linspace(x0, x1, n)).astype(np.int64)
    ys = np.rint(np.linspace(y0, y1, n)).astype(np.int64)
    return not barrier[ys, xs].any()


def soft_cast(barrier: np.ndarray, sx: float, sy: float, radius: float = 36,
              samples: int = 24, blur: float = 6) -> np.ndarray:
    """Soft-shadow visibility (uint8): an area light of `radius` px sampled on a disk.

    Samples inside a barrier or cut off from the centre by one are skipped, so a
    lamp next to a wall doesn't shine through it. Never paints on barriers.
    """
    h, w = barrier.shape
    if not (0 <= sx < w and 0 <= sy < h) or barrier[int(sy), int(sx)]:
        raise ValueError(f"light position ({sx:.0f}, {sy:.0f}) is outside the plan or inside a wall")
    golden = math.pi * (3 - math.sqrt(5))
    points = [(sx, sy)] + [
        (sx + radius * math.sqrt((k + 0.5) / samples) * math.cos(k * golden),
         sy + radius * math.sqrt((k + 0.5) / samples) * math.sin(k * golden))
        for k in range(samples)
    ]
    acc = np.zeros((h, w), np.float32)
    used = 0
    for px, py in points:
        if not (0 <= px < w and 0 <= py < h) or barrier[int(py), int(px)]:
            continue
        if not _clear_line(barrier, sx, sy, px, py):
            continue
        acc += point_cast(barrier, px, py)
        used += 1
    m = (acc / used).astype(np.uint8)
    m[barrier] = 0
    if blur > 0:
        m = np.array(Image.fromarray(m).filter(ImageFilter.GaussianBlur(blur)))
        m[barrier] = 0
    return m


def lit(barrier: np.ndarray, sx: float, sy: float, partial: list[tuple[float, np.ndarray]] = (),
        **soft) -> np.ndarray:
    """Visibility through walls with partially transmissive openings (uint8).

    `partial` is [(transmission, opening pixels)]: each group adds `transmission`
    times the extra area it would light if it were fully open.
    """
    base = soft_cast(barrier, sx, sy, **soft)
    if not partial:
        return base
    out = base.astype(np.float32)
    carved = np.zeros_like(barrier)
    for t, pixels in partial:
        opened = soft_cast(barrier & ~pixels, sx, sy, **soft).astype(np.float32)
        out += t * np.clip(opened - base, 0, None)
        carved |= pixels
    out[barrier & ~carved] = 0
    return np.clip(out, 0, 255).astype(np.uint8)


def window_reach(barrier: np.ndarray, mx: float, my: float, tx: float, ty: float, half: float,
                 nx: float, ny: float, blur: float = 4, samples: int = 5) -> np.ndarray:
    """Area a window can see (uint8): what its sunlight could ever reach.

    (mx, my) is the window's centre, (tx, ty) its direction along the wall, `half`
    its half length and (nx, ny) the unit normal pointing into the room. Starts a
    few pixels inside the room, past the wall that seals the window, from points
    spread along the window, so the mask stops at walls and closed doors.
    """
    h, w = barrier.shape
    acc = np.zeros((h, w), np.uint8)
    used = 0
    for i in range(samples):
        f = 0.0 if samples == 1 else (i / (samples - 1) * 2 - 1) * 0.85
        px, py = mx + tx * half * f, my + ty * half * f
        # Step inward until clear of the wall, at most 60 px.
        for step in range(0, 60):
            qx, qy = px + nx * step, py + ny * step
            if not (0 <= qx < w and 0 <= qy < h):
                break
            if not barrier[int(qy), int(qx)]:
                qx, qy = qx + nx * 2, qy + ny * 2
                if 0 <= qx < w and 0 <= qy < h and not barrier[int(qy), int(qx)]:
                    acc = np.maximum(acc, point_cast(barrier, qx, qy))
                    used += 1
                break
    if not used:
        raise ValueError(f"window at ({mx:.0f}, {my:.0f}) has no open floor on its inner side")
    acc[barrier] = 0
    if blur > 0:
        acc = np.array(Image.fromarray(acc).filter(ImageFilter.GaussianBlur(blur)))
        acc[barrier] = 0
    return acc


def fill_region(barrier: np.ndarray, x: float, y: float, blur: float = 2) -> np.ndarray:
    """The enclosed area around (x, y) (uint8 0/255), bounded by the barrier raster."""
    h, w = barrier.shape
    if not (0 <= x < w and 0 <= y < h) or barrier[int(y), int(x)]:
        raise ValueError(f"point ({x:.0f}, {y:.0f}) is outside the plan or inside a wall")
    # .copy(): an image made from an array is read-only, and floodfill would silently do nothing.
    img = Image.fromarray(np.where(barrier, 1, 0).astype(np.uint8)).copy()
    ImageDraw.floodfill(img, (int(x), int(y)), 2)
    region = np.array(img) == 2
    out = np.where(region, 255, 0).astype(np.uint8)
    if blur > 0:
        out = np.array(Image.fromarray(out).filter(ImageFilter.GaussianBlur(blur)))
        out[barrier] = 0
    return out


def mask_data_uri(mask: np.ndarray, scale: int = 3) -> str:
    """A mask as a small white+alpha PNG data URI (the alpha channel is what CSS masks use)."""
    a = Image.fromarray(mask)
    if scale > 1:
        a = a.resize((max(1, a.width // scale), max(1, a.height // scale)), Image.LANCZOS)
    a = a.point(lambda v: (v // 8) * 8)
    la = Image.merge("LA", (Image.new("L", a.size, 255), a))
    buf = io.BytesIO()
    la.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def differs(a: np.ndarray, b: np.ndarray, tolerance: int = 20, min_pixels: int = 200) -> int:
    """How many pixels two masks differ on (0 when below `min_pixels`)."""
    n = int((np.abs(a.astype(np.int16) - b.astype(np.int16)) > tolerance).sum())
    return n if n >= min_pixels else 0

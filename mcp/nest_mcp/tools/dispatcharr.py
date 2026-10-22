import asyncio
import re
import shutil
import time
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver import MCPServer
from nest_mcp import config
from nest_mcp.http_client import make_client

# Collections that share one list shape. Secrets are stripped from the rows.
_LISTS = {
    "m3u-accounts": "/api/m3u/accounts/",
    "epg-sources": "/api/epg/sources/",
    "stream-profiles": "/api/core/streamprofiles/",
    "channel-profiles": "/api/channels/profiles/",
    "output-profiles": "/api/core/outputprofiles/",
}
_SECRET_FIELDS = ("password", "username", "api_key", "token")

# Perceptual hashes (see _fingerprint) of known error slates, e.g. the yellow-on-black
# Portuguese message some Pluto streams show. Filled in from a real capture:
# probe the affected channel, then add its `fingerprint` here or pass slate_hashes.
KNOWN_SLATES: dict[str, str] = {}
_SLATE_MAX_DISTANCE = 12  # of 144 bits

_W, _H = 96, 54
_FRAME = _W * _H * 3
_PROBE_MAX = 25


def _client() -> httpx.AsyncClient:
    headers = {"X-API-Key": config.dispatcharr.key} if config.dispatcharr.key else {}
    return make_client(config.dispatcharr.url, headers=headers, timeout=60.0)


async def _call(client: httpx.AsyncClient, method: str, path: str, **kwargs):
    """Request Dispatcharr; surface its error body and say when the key is the problem."""
    resp = await client.request(method, path, **kwargs)
    if resp.status_code in (401, 403) and not config.dispatcharr.key:
        raise RuntimeError(
            f"Dispatcharr {method} {path} -> {resp.status_code}: NEST_DISPATCHARR_KEY is not set "
            "(generate an API key in the Dispatcharr UI and add it to the nest-mcp secret)")
    if resp.is_error:
        raise RuntimeError(f"Dispatcharr {method} {path} -> {resp.status_code}: {resp.text[:500]}")
    if not resp.content:
        return {"status": resp.status_code}
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "body": resp.text[:2000]}


def _scrub(row):
    if isinstance(row, dict):
        return {k: _scrub(v) for k, v in row.items() if k not in _SECRET_FIELDS}
    if isinstance(row, list):
        return [_scrub(v) for v in row]
    return row


def _page(data: dict, page: int, rows: list) -> dict:
    return {"count": data.get("count", len(rows)), "page": page, "has_next": bool(data.get("next")), "items": rows}


def _channel_row(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "uuid": c.get("uuid"),
        "number": c.get("effective_channel_number") or c.get("channel_number"),
        "name": c.get("effective_name") or c.get("name"),
        "group_id": c.get("effective_channel_group_id") or c.get("channel_group_id"),
        "tvg_id": c.get("effective_tvg_id") or c.get("tvg_id"),
        "epg_data_id": c.get("effective_epg_data_id") or c.get("epg_data_id"),
        "streams": c.get("streams"),
        "hidden": c.get("hidden_from_output"),
    }


def _height(stats: dict) -> int | None:
    res = stats.get("resolution")
    if isinstance(res, str) and "x" in res:
        try:
            return int(res.lower().split("x")[1])
        except ValueError:
            return None
    return stats.get("height") if isinstance(stats.get("height"), int) else None


def _stream_row(s: dict) -> dict:
    stats = s.get("stream_stats") or {}
    return {
        "id": s.get("id"),
        "name": s.get("name"),
        "group": s.get("channel_group"),
        "account": s.get("m3u_account"),
        "tvg_id": s.get("tvg_id"),
        "stale": s.get("is_stale"),
        "viewers": s.get("current_viewers"),
        "resolution": stats.get("resolution"),
        "video_codec": stats.get("video_codec"),
        "fps": stats.get("source_fps"),
        "bitrate_kbps": stats.get("ffmpeg_output_bitrate"),
        "stats_updated": s.get("stream_stats_updated_at"),
        "has_stats": bool(stats),
    }


# ── Open endpoints: the published lineup (no API key) ─────────────────────────

def _parse_m3u(text: str) -> list[dict]:
    """Channels from Dispatcharr's /output/m3u: number, name, group, tvg id and stream path."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
        url = next((ln for ln in lines[i + 1:i + 4] if ln and not ln.startswith("#")), "")
        out.append({
            "number": attrs.get("tvg-chno") or attrs.get("tvg-id", ""),
            "tvg_id": attrs.get("tvg-id", ""),
            "name": line.rsplit(",", 1)[-1].strip() or attrs.get("tvg-name", ""),
            "group": attrs.get("group-title", ""),
            "uuid": urlparse(url).path.rstrip("/").rsplit("/", 1)[-1],
            "path": urlparse(url).path,
        })
    return out


async def _lineup() -> list[dict]:
    async with make_client(config.dispatcharr.url, timeout=60.0) as client:
        resp = await client.get("/output/m3u")
        if resp.is_error:
            raise RuntimeError(f"Dispatcharr /output/m3u -> {resp.status_code}: {resp.text[:300]}")
        return _parse_m3u(resp.text)


def _select(lineup: list[dict], channels: list[str] | None, group: str, search: str) -> list[dict]:
    rows = lineup
    if channels:
        wanted = {c.strip().lower() for c in channels}
        rows = [r for r in rows if r["number"].lower() in wanted or r["uuid"].lower() in wanted
                or r["name"].lower() in wanted or r["tvg_id"].lower() in wanted]
    if group:
        rows = [r for r in rows if r["group"].lower() == group.lower()]
    if search:
        rows = [r for r in rows if search.lower() in r["name"].lower()]
    return rows


# ── Frame analysis (pure Python; frames are 96x54 RGB) ────────────────────────

def _luma(frame: bytes) -> list[int]:
    return [(frame[i] * 299 + frame[i + 1] * 587 + frame[i + 2] * 114) // 1000 for i in range(0, len(frame), 3)]


def _color_stats(frame: bytes) -> tuple[float, float]:
    """(fraction near-black, fraction yellow) of a frame."""
    black = yellow = 0
    for i in range(0, len(frame), 3):
        r, g, b = frame[i], frame[i + 1], frame[i + 2]
        if max(r, g, b) < 50:
            black += 1
        elif r > 170 and g > 140 and b < 110:
            yellow += 1
    n = len(frame) // 3
    return black / n, yellow / n


def _fingerprint(frame: bytes) -> str:
    """Average hash: mean luma of each cell of a 16x9 grid, one bit per cell above the frame's mean (hex).

    Area averaging, not point sampling: a mostly black slate with a line of small
    text still lights up distinct cells, where a 9x8 difference hash would see a
    flat frame."""
    luma = _luma(frame)
    cw, ch = _W // 16, _H // 9
    cells = []
    for gy in range(9):
        for gx in range(16):
            total = sum(luma[(gy * ch + y) * _W + gx * cw + x] for y in range(ch) for x in range(cw))
            cells.append(total / (cw * ch))
    mean = sum(cells) / len(cells)
    bits = 0
    for c in cells:
        bits = (bits << 1) | (1 if c > mean else 0)
    return f"{bits:036x}"


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _mean_diff(a: bytes, b: bytes) -> float:
    la, lb = _luma(a), _luma(b)
    return sum(abs(x - y) for x, y in zip(la[::5], lb[::5])) / len(la[::5])


def _analyse(frames: list[bytes], slates: dict[str, str]) -> dict:
    stats = [_color_stats(f) for f in frames]
    diffs = [_mean_diff(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
    text_on_black = sum(1 for b, y in stats if b > 0.75 and y > 0.008)
    out = {
        "frames": len(frames),
        "blank_frames": sum(1 for b, _ in stats if b > 0.97),
        "frozen": bool(diffs) and max(diffs) < 2.0,
        "fingerprint": _fingerprint(frames[-1]),
    }
    # Yellow text on black that does not move for several seconds. Title cards
    # also match the colours but they animate, so require a static picture.
    out["possible_slate"] = text_on_black >= 2 and out["frozen"]
    for name, h in slates.items():
        dist = min(_hamming(h, _fingerprint(f)) for f in frames)
        if dist <= _SLATE_MAX_DISTANCE:
            out["known_slate"] = name
            out["slate_distance"] = dist
            break
    return out


async def _capture(url: str, seconds: int) -> dict:
    """One ffmpeg connection: decode a frame every 2 s for `seconds`, scaled down, and keep its log."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-t", str(seconds), "-i", url,
        "-map", "0:v:0", "-vf", f"fps=1/2,scale={_W}:{_H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    err_task = asyncio.ensure_future(proc.stderr.read())
    frames: list[bytes] = []
    first = None
    try:
        while True:
            frames.append(await asyncio.wait_for(proc.stdout.readexactly(_FRAME), seconds + 40))
            first = first if first is not None else time.monotonic() - started
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        pass
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    stderr = (await err_task).decode(errors="replace")
    return {"frames": frames, "stderr": stderr, "startup_s": round(first, 1) if first is not None else None}


def _stream_info(stderr: str) -> dict:
    m = re.search(r"Video: (\w+).*?, (\d{2,5})x(\d{2,5})", stderr)
    fps = re.search(r"([\d.]+) fps", stderr)
    info = {}
    if m:
        info.update(video_codec=m.group(1), resolution=f"{m.group(2)}x{m.group(3)}")
    if fps:
        info["fps"] = float(fps.group(1))
    return info


def register(mcp: MCPServer) -> None:

    # ── Read: published lineup and probing (no API key needed) ────────────────

    @mcp.tool()
    async def dispatcharr_lineup(group: str = "", search: str = "", limit: int = 100) -> dict:
        """Channels Dispatcharr publishes in its M3U (what Jellyfin sees): number, name, group, tvg id and stream UUID. Needs no API key. Filter by exact `group` and/or a name `search`."""
        rows = _select(await _lineup(), None, group, search)
        groups: dict[str, int] = {}
        for r in rows:
            groups[r["group"]] = groups.get(r["group"], 0) + 1
        return {"total": len(rows), "groups": dict(sorted(groups.items(), key=lambda kv: -kv[1])),
                "items": [{k: r[k] for k in ("number", "name", "group", "tvg_id", "uuid")} for r in rows[:limit]]}

    @mcp.tool()
    async def dispatcharr_probe_channels(
        channels: list[str] | None = None, group: str = "", search: str = "", limit: int = 10,
        seconds: int = 6, pause: float = 3.0, slate_hashes: dict[str, str] | None = None,
    ) -> list[dict]:
        """Actively probe live channels through Dispatcharr's proxy (needs ffmpeg in this environment; no API key). For each: seconds to first frame, codec/resolution/fps, blank or frozen picture, and whether it shows an error slate (a known one by image fingerprint, or a static yellow-on-black text screen as a lower-confidence guess). Select by `channels` (numbers, names or UUIDs), `group` or name `search`; capped at 25 per call and run one at a time with `pause` seconds between, so it does not overload the proxy or Pluto. Pass `slate_hashes` ({name: fingerprint}) to detect a slate seen on a channel before (every probe result carries the `fingerprint` of its last frame)."""
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is not installed where nest-mcp runs; probing needs it "
                               "(add it to the image, or run the probe somewhere that has it)")
        picked = _select(await _lineup(), channels, group, search)[:min(limit, _PROBE_MAX)]
        slates = {**KNOWN_SLATES, **(slate_hashes or {})}
        results = []
        for n, ch in enumerate(picked):
            if n:
                await asyncio.sleep(pause)
            row = {"number": ch["number"], "name": ch["name"], "group": ch["group"]}
            # Go straight to the service, not back out through the public hostname.
            cap = await _capture(config.dispatcharr.url.rstrip("/") + ch["path"], seconds)
            row["startup_s"] = cap["startup_s"]
            row.update(_stream_info(cap["stderr"]))
            if cap["frames"]:
                row.update(_analyse(cap["frames"], slates))
            else:
                tail = [ln for ln in cap["stderr"].strip().splitlines() if ln.strip()][-1:]
                row["error"] = tail[0][-160:] if tail else "no frames decoded"
            results.append(row)
        return results

    # ── Read: management API (needs the API key) ──────────────────────────────

    @mcp.tool()
    async def dispatcharr_status() -> dict:
        """Dispatcharr overview: version, M3U accounts and EPG sources with their refresh status, and live proxy stats (active channels and viewers). Version needs no key; the rest does."""
        async with _client() as client:
            out: dict = {"version": await _call(client, "GET", "/api/core/version/")}
            for key, path in (("m3u_accounts", "/api/m3u/accounts/"), ("epg_sources", "/api/epg/sources/")):
                try:
                    rows = _scrub(await _call(client, "GET", path))
                    out[key] = [{k: r.get(k) for k in ("id", "name", "is_active", "status", "last_message",
                                                       "updated_at", "max_streams")} for r in rows]
                except RuntimeError as exc:
                    out[key] = {"error": str(exc)}
            try:
                out["live"] = await _call(client, "GET", "/proxy/stats/")
            except RuntimeError as exc:
                out["live"] = {"error": str(exc)}
            return out

    @mcp.tool()
    async def dispatcharr_channels(search: str = "", group_id: int = 0, page: int = 1, page_size: int = 50) -> dict:
        """List Dispatcharr channels (the managed ones, with ids): number, name, group, tvg id, EPG link, attached stream ids. Needs the API key."""
        params: dict = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if group_id:
            params["channel_group"] = group_id
        async with _client() as client:
            data = await _call(client, "GET", "/api/channels/channels/", params=params)
        rows = data.get("results", data) if isinstance(data, dict) else data
        return _page(data if isinstance(data, dict) else {}, page, [_channel_row(c) for c in rows])

    @mcp.tool()
    async def dispatcharr_channel(channel_id: int) -> dict:
        """Full record of one Dispatcharr channel by id, including its attached streams' stats."""
        async with _client() as client:
            chan = await _call(client, "GET", f"/api/channels/channels/{channel_id}/")
            try:
                chan["stream_stats"] = await _call(client, "GET", f"/api/channels/channels/{channel_id}/streams/stats/")
            except RuntimeError as exc:
                chan["stream_stats"] = {"error": str(exc)}
            return chan

    @mcp.tool()
    async def dispatcharr_streams(
        search: str = "", account: str = "", group: str = "", page: int = 1, page_size: int = 50,
    ) -> dict:
        """List ingested streams (before they become channels) with Dispatcharr's own quality stats: resolution, codec, fps, bitrate, stale flag, viewers. Filter by name `search`, M3U `account` name, or `group` name. Needs the API key."""
        params: dict = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if account:
            params["m3u_account_name"] = account
        if group:
            params["channel_group_name"] = group
        async with _client() as client:
            data = await _call(client, "GET", "/api/channels/streams/", params=params)
        rows = data.get("results", data) if isinstance(data, dict) else data
        return _page(data if isinstance(data, dict) else {}, page, [_stream_row(s) for s in rows])

    @mcp.tool()
    async def dispatcharr_quality_report(
        account: str = "", group: str = "", min_height: int = 480, max_streams: int = 600,
    ) -> dict:
        """Quality overview from the stats Dispatcharr stores per stream: resolution histogram, and the streams that have no stats, are stale, or are below `min_height` (default 480; Pluto's normal tier is 1216x684). Dispatcharr only records stats once a stream has been played, so many streams show no_stats until they are watched or probed with dispatcharr_probe_channels. Scope with M3U `account` name or `group` name. Needs the API key. Probing channels also fills in their stats."""
        params: dict = {"page_size": 200}
        if account:
            params["m3u_account_name"] = account
        if group:
            params["channel_group_name"] = group
        rows: list[dict] = []
        async with _client() as client:
            page = 1
            while len(rows) < max_streams:
                data = await _call(client, "GET", "/api/channels/streams/", params={**params, "page": page})
                rows.extend(data.get("results", []))
                if not data.get("next"):
                    break
                page += 1
        rows = rows[:max_streams]
        hist: dict[str, int] = {}
        problems: dict[str, list] = {"no_stats": [], "stale": [], "low_res": []}
        for s in rows:
            stats = s.get("stream_stats") or {}
            hist[stats.get("resolution") or "unknown"] = hist.get(stats.get("resolution") or "unknown", 0) + 1
            label = f"{s.get('id')} {s.get('name')}"
            if not stats:
                problems["no_stats"].append(label)
            if s.get("is_stale"):
                problems["stale"].append(label)
            h = _height(stats)
            if h is not None and h < min_height:
                problems["low_res"].append(f"{label} ({stats.get('resolution')})")
        return {
            "streams": len(rows),
            "resolutions": dict(sorted(hist.items(), key=lambda kv: -kv[1])),
            "problem_counts": {k: len(v) for k, v in problems.items()},
            "problems": {k: v[:50] for k, v in problems.items()},
        }

    @mcp.tool()
    async def dispatcharr_list_resources(kind: str) -> list | dict:
        """List a Dispatcharr collection (credentials stripped). kind: m3u-accounts, epg-sources, stream-profiles, channel-profiles, output-profiles. Needs the API key."""
        if kind not in _LISTS:
            raise ValueError(f"Unknown kind {kind!r}; choose one of: {', '.join(_LISTS)}")
        async with _client() as client:
            return _scrub(await _call(client, "GET", _LISTS[kind]))

    # ── Write ([DESTRUCTIVE]; all need the API key) ───────────────────────────

    @mcp.tool()
    async def dispatcharr_create_channel_from_stream(
        stream_id: int, channel_number: float | None = None, name: str = "", profile_ids: list[int] | None = None,
    ) -> dict:
        """[DESTRUCTIVE] Create a channel from an ingested stream (ids from dispatcharr_streams). It appears in the published M3U/XMLTV, so Jellyfin's Live TV guide changes. Omit channel_number to take the next free one. Confirm with the user first."""
        body: dict = {"stream_id": stream_id}
        if channel_number is not None:
            body["channel_number"] = channel_number
        if name:
            body["name"] = name
        if profile_ids:
            body["channel_profile_ids"] = profile_ids
        async with _client() as client:
            return await _call(client, "POST", "/api/channels/channels/from-stream/", json=body)

    @mcp.tool()
    async def dispatcharr_update_channel(channel_id: int, changes: dict) -> dict:
        """[DESTRUCTIVE] Change fields of a channel (name, channel_number, channel_group_id, tvg_id, epg_data_id, streams [ordered stream ids, first is preferred], stream_profile_id, hidden_from_output, ...). Only the given fields change (PATCH). Confirm with the user first."""
        async with _client() as client:
            return await _call(client, "PATCH", f"/api/channels/channels/{channel_id}/", json=changes)

    @mcp.tool()
    async def dispatcharr_delete_channels(channel_ids: list[int]) -> dict:
        """[DESTRUCTIVE] Delete channels by id, one request each. Jellyfin loses them on its next guide refresh; recordings scheduled on them break. Confirm with the user first."""
        deleted, failed = [], {}
        async with _client() as client:
            for cid in channel_ids:
                try:
                    await _call(client, "DELETE", f"/api/channels/channels/{cid}/")
                    deleted.append(cid)
                except RuntimeError as exc:
                    failed[cid] = str(exc)
        return {"deleted": deleted, "failed": failed}

    @mcp.tool()
    async def dispatcharr_assign_numbers(channel_ids: list[int], starting_number: float = 1) -> dict:
        """[DESTRUCTIVE] Renumber channels in the order given, counting up from `starting_number`. Changes the numbers Jellyfin shows. Confirm with the user first."""
        async with _client() as client:
            return await _call(client, "POST", "/api/channels/channels/assign/",
                               json={"channel_ids": channel_ids, "starting_number": starting_number})

    @mcp.tool()
    async def dispatcharr_set_epg(channel_id: int, epg_data_id: int) -> dict:
        """[DESTRUCTIVE] Link a channel to an EPG entry (guide data) and refresh its programme data."""
        async with _client() as client:
            return await _call(client, "POST", f"/api/channels/channels/{channel_id}/set-epg/",
                               json={"epg_data_id": epg_data_id})

    @mcp.tool()
    async def dispatcharr_refresh(kind: str, source_id: int = 0) -> dict:
        """Refresh a source now. kind 'm3u' (re-reads playlists; source_id 0 = all active accounts) or 'epg' (guide data; source_id required). IDs from dispatcharr_list_resources."""
        async with _client() as client:
            if kind == "m3u":
                path = f"/api/m3u/refresh/{source_id}/" if source_id else "/api/m3u/refresh/"
                return await _call(client, "POST", path)
            if kind == "epg":
                if not source_id:
                    raise ValueError("source_id is required for epg")
                return await _call(client, "POST", "/api/epg/import/", json={"id": source_id})
        raise ValueError("kind must be 'm3u' or 'epg'")

    @mcp.tool()
    async def dispatcharr_stop_channel(channel_uuid: str) -> dict:
        """[DESTRUCTIVE] Stop a live channel's proxy session (viewers are disconnected and must retune). Use the UUID from dispatcharr_lineup or dispatcharr_channel."""
        async with _client() as client:
            return await _call(client, "POST", f"/proxy/ts/stop/{channel_uuid}")

    @mcp.tool()
    async def dispatcharr_api(method: str, path: str, body: dict | list | None = None, query: dict | None = None) -> dict | list:
        """[DESTRUCTIVE unless GET] Call any Dispatcharr API endpoint directly. `path` starts with /api/ or /proxy/ (the OpenAPI spec is at /api/schema/). Use for anything the dedicated dispatcharr_* tools do not cover. Non-GET calls change live state: confirm with the user first."""
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("method must be GET, POST, PUT, PATCH or DELETE")
        if not path.startswith(("/api/", "/proxy/")):
            raise ValueError("path must start with /api/ or /proxy/")
        async with _client() as client:
            return await _call(client, method, path, json=body, params=query)

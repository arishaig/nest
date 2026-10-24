import asyncio
import math
import random

import httpx
from mcp.server.mcpserver import MCPServer
from nest_mcp import config
from nest_mcp.http_client import make_client

# Resource collections that share one CRUD shape (list / get / create / replace / delete).
_RESOURCES = {
    "custom-shows": "/api/custom-shows",
    "filler-lists": "/api/filler-lists",
    "smart-collections": "/api/smart_collections",
    "transcode-configs": "/api/transcode_configs",
    "stream-selection-profiles": "/api/stream-selection-profiles",
}

# Singleton settings documents (GET to read, PUT to replace).
_SETTINGS = {
    "ffmpeg": "/api/ffmpeg-settings",
    "hdhr": "/api/hdhr-settings",
    "xmltv": "/api/xmltv-settings",
    "plex": "/api/plex-settings",
    "system": "/api/system/settings",
    "media-source": "/api/settings/media-source",
    "feature-flags": "/api/system/feature-flags",
}

_PAGE = 200
_SEARCH_PAGES = 15  # search pages are ~20 hits; cap runaway common titles
_SEARCH_CONCURRENCY = 8
_DURATION_SLOP_MS = 1000


def _client() -> httpx.AsyncClient:
    # Scans, big lineups and programming rewrites are slow; Tunarr's API has no auth.
    return make_client(config.tunarr.url, timeout=120.0)


async def _call(client: httpx.AsyncClient, method: str, path: str, **kwargs):
    """Request Tunarr; surface its error body (raise_for_status alone drops it)."""
    resp = await client.request(method, path, **kwargs)
    if resp.is_error:
        raise RuntimeError(f"Tunarr {method} {path} -> {resp.status_code}: {resp.text[:500]}")
    if not resp.content:
        return {"status": resp.status_code}
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "body": resp.text[:2000]}


async def _channel(client: httpx.AsyncClient, ref: str) -> dict:
    """Resolve a channel by UUID, channel number or (case-insensitive) name."""
    ref = str(ref).strip()
    for ch in await _call(client, "GET", "/api/channels"):
        if ch["id"] == ref or str(ch.get("number")) == ref or ch.get("name", "").lower() == ref.lower():
            return ch
    raise ValueError(f"No Tunarr channel matches {ref!r}; call tunarr_channels to list them")


async def _all_programs(client: httpx.AsyncClient, channel_id: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        page = await _call(
            client, "GET", f"/api/channels/{channel_id}/programs",
            params={"limit": _PAGE, "offset": offset},
        )
        out.extend(page["result"])
        offset += _PAGE
        if offset >= page["total"]:
            return out


def _ident(program: dict, kind: str) -> str:
    for i in program.get("identifiers") or []:
        if i.get("type") == kind:
            return i.get("id", "")
    return ""


def _program_row(p: dict) -> dict:
    show = p.get("show") or {}
    season = p.get("season") or {}
    return {
        "id": p.get("uuid"),
        "title": p.get("title"),
        "show": show.get("title"),
        "season": season.get("index"),
        "episode": p.get("episodeNumber"),
        "type": p.get("type"),
        "year": p.get("year"),
        "minutes": round((p.get("duration") or 0) / 60000, 1),
        "state": p.get("state"),
        "jellyfin_id": _ident(p, "jellyfin"),
        "imdb": _ident(p, "imdb"),
        "tvdb": _ident(p, "tvdb"),
    }


def _same_content(old: dict, new: dict) -> bool:
    """True if `new` is the freshly scanned twin of the dead program `old`.

    After Jellyfin re-IDs its library, Tunarr keeps the old rows (state 'missing')
    and adds new ones (state 'ok'). The twin has the same title, year and episode
    number. Where both records carry an IMDb/TVDB id those must agree and are
    trusted over duration (files get re-encoded or re-cut); with no shared
    external id, the duration must match too.
    """
    if new.get("state") != "ok" or new.get("uuid") == old.get("uuid") or new.get("type") != old.get("type"):
        return False
    if (new.get("title") or "") != (old.get("title") or ""):
        return False
    if new.get("episodeNumber") != old.get("episodeNumber") or new.get("year") != old.get("year"):
        return False
    shared_id = False
    for kind in ("tvdb", "imdb"):
        a, b = _ident(old, kind), _ident(new, kind)
        if a and b:
            if a != b:
                return False
            shared_id = True
    if shared_id:
        return True
    return abs((new.get("duration") or 0) - (old.get("duration") or 0)) <= _DURATION_SLOP_MS


def _best_twin(old: dict, hits: list[dict]) -> dict | None:
    """The matching candidate closest in duration to `old` (prefers the same cut of the episode)."""
    twins = [h for h in hits if _same_content(old, h)]
    if not twins:
        return None
    return min(twins, key=lambda h: abs((h.get("duration") or 0) - (old.get("duration") or 0)))


async def _search_title(client: httpx.AsyncClient, title: str, media_source: str, library: str) -> list[dict]:
    hits: list[dict] = []
    for page in range(1, _SEARCH_PAGES + 1):
        body: dict = {"query": {"query": title, "restrictSearchTo": ["title"]}, "page": page}
        if media_source:
            body["mediaSourceId"] = media_source
        if library:
            body["libraryId"] = library
        res = await _call(client, "POST", "/api/programs/search", json=body)
        hits.extend(res.get("results", []))
        if page >= (res.get("totalPages") or 1):
            break
    return hits


def _season_episode(item: dict) -> tuple:
    prog = item.get("program") or {}
    return ((prog.get("season") or {}).get("index") or 0, prog.get("episodeNumber") or 0)


def _units(lineup: list[dict], shows_by_id: dict[str, str]) -> list[tuple[str, list[dict]]]:
    """Split a lineup into (show, entries) units: a content entry travels with the flex that pads it."""
    units: list[tuple[str, list[dict]]] = []
    for entry in lineup:
        entry = _lineup_entry(entry)
        if entry.get("type") == "flex" and units and units[-1][1][-1].get("type") == "content" and len(units[-1][1]) == 1:
            units[-1][1].append(entry)
        elif entry.get("type") == "content":
            units.append((shows_by_id.get(entry.get("id"), "?"), [entry]))
        else:
            units.append(("", [entry]))
    return units


def _place(existing: list, new: list, placement: str, seed: int) -> list:
    """Merge `new` units into `existing` units according to `placement`."""
    if placement == "append":
        return existing + new
    if placement == "shuffle_in":
        rng = random.Random(seed)
        merged = list(existing)
        for unit in new:
            merged.insert(rng.randint(0, len(merged)), unit)
        return merged
    if placement == "round_robin":
        groups: dict[str, list] = {}
        for unit in existing + new:
            groups.setdefault(unit[0], []).append(unit)
        # Units with no show (flex, fillers, redirects) keep their place at the tail.
        tail = groups.pop("", [])
        queues = list(groups.values())
        merged = []
        while any(queues):
            for q in queues:
                if q:
                    merged.append(q.pop(0))
        return merged + tail
    raise ValueError("placement must be 'append', 'round_robin' or 'shuffle_in'")


def _slice(rows: list, limit: int, offset: int) -> dict:
    return {"total": len(rows), "offset": offset, "items": rows[offset:offset + limit]}


def _need(table: dict, name: str, what: str) -> str:
    if name not in table:
        raise ValueError(f"Unknown {what} {name!r}; choose one of: {', '.join(table)}")
    return table[name]


def register(mcp: MCPServer) -> None:

    # ── Read: server, channels, programs ──────────────────────────────────────

    @mcp.tool()
    async def tunarr_status() -> dict:
        """Tunarr server overview: version, health checks, ffmpeg, sessions, XMLTV last refresh, scheduled tasks."""
        async with _client() as client:
            out: dict = {}
            for key, path in (
                ("version", "/api/version"),
                ("health", "/api/system/health"),
                ("sessions", "/api/sessions"),
                ("xmltv_last_refresh", "/api/xmltv-last-refresh"),
                ("guide", "/api/guide/status"),
            ):
                try:
                    out[key] = await _call(client, "GET", path)
                except RuntimeError as exc:
                    out[key] = {"error": str(exc)}
            tasks = await _call(client, "GET", "/api/tasks")
            out["tasks"] = [
                {"id": t["id"], "scheduled": [
                    {"next": s.get("nextExecution"), "last": s.get("lastExecution"), "running": s.get("running")}
                    for s in t.get("scheduledTasks", [])]}
                for t in tasks
            ]
            return out

    @mcp.tool()
    async def tunarr_channels() -> list[dict]:
        """List Tunarr channels: number, name, id, lineup length (entries, repeats counted), total hours, stream mode, offline/stealth/on-demand flags."""
        async with _client() as client:
            return [
                {
                    "number": c.get("number"),
                    "name": c.get("name"),
                    "id": c.get("id"),
                    "lineup_entries": c.get("programCount"),
                    "hours": round((c.get("duration") or 0) / 3_600_000, 1),
                    "stream_mode": c.get("streamMode"),
                    "offline": c.get("offline"),
                    "stealth": c.get("stealth"),
                    "on_demand": (c.get("onDemand") or {}).get("enabled"),
                    "transcode_config_id": c.get("transcodeConfigId"),
                    "sessions": c.get("sessions"),
                }
                for c in await _call(client, "GET", "/api/channels")
            ]

    @mcp.tool()
    async def tunarr_channel(channel: str) -> dict:
        """Full configuration of one channel (number, UUID or name): icon, watermark, filler, transcode config, subtitles, on-demand, etc."""
        async with _client() as client:
            ch = await _channel(client, channel)
            return await _call(client, "GET", f"/api/channels/{ch['id']}")

    @mcp.tool()
    async def tunarr_channel_programs(
        channel: str, state: str = "", search: str = "", limit: int = 100, offset: int = 0,
    ) -> dict:
        """List the distinct programs on a channel (number, UUID or name) as compact rows: title, show, season/episode, minutes, Jellyfin ID, and Tunarr `state` ('ok' or 'missing'). Filter by `state` and/or a case-insensitive title/show `search`; page with limit/offset."""
        async with _client() as client:
            ch = await _channel(client, channel)
            rows = [_program_row(p) for p in await _all_programs(client, ch["id"])]
        if state:
            rows = [r for r in rows if r["state"] == state]
        if search:
            needle = search.lower()
            rows = [r for r in rows if needle in (r["title"] or "").lower() or needle in (r["show"] or "").lower()]
        return _slice(rows, limit, offset)

    @mcp.tool()
    async def tunarr_channel_lineup(channel: str, limit: int = 100, offset: int = 0) -> dict:
        """The ordered lineup of a channel (what plays, in order): each entry's type (content/flex/redirect/custom/filler), program UUID, minutes and title. Use this to see exact ordering before editing with tunarr_set_programming."""
        async with _client() as client:
            ch = await _channel(client, channel)
            prog = await _call(client, "GET", f"/api/channels/{ch['id']}/programming")
        programs = prog.get("programs", {})
        rows = []
        for idx, entry in enumerate(prog.get("lineup", [])):
            detail = programs.get(entry.get("id"), {}).get("program") or {}
            rows.append({
                "index": idx,
                "type": entry.get("type"),
                "id": entry.get("id"),
                "minutes": round((entry.get("duration") or 0) / 60000, 1),
                "title": detail.get("title"),
                "show": (detail.get("show") or {}).get("title"),
                "state": detail.get("state"),
            })
        return {"channel": ch["name"], **_slice(rows, limit, offset)}

    @mcp.tool()
    async def tunarr_now_playing(channel: str) -> dict:
        """What a channel is playing right now (and its position), as Tunarr's schedule computes it."""
        async with _client() as client:
            ch = await _channel(client, channel)
            return await _call(client, "GET", f"/api/channels/{ch['id']}/now_playing")

    @mcp.tool()
    async def tunarr_guide(date_from: str, date_to: str, channel: str = "") -> dict | list:
        """TV guide (EPG) between two ISO-8601 timestamps, for one channel or all. Example: date_from='2026-10-22T18:00:00Z'."""
        params = {"dateFrom": date_from, "dateTo": date_to}
        async with _client() as client:
            if channel:
                ch = await _channel(client, channel)
                return await _call(client, "GET", f"/api/guide/channels/{ch['id']}", params=params)
            return await _call(client, "GET", "/api/guide/channels", params=params)

    @mcp.tool()
    async def tunarr_sessions() -> dict | list:
        """Active Tunarr stream sessions (which channels are being watched, by whom)."""
        async with _client() as client:
            return await _call(client, "GET", "/api/sessions")

    # ── Read: media sources, libraries, programs search ───────────────────────

    @mcp.tool()
    async def tunarr_media_sources() -> list[dict]:
        """Media sources (Jellyfin/Plex/Emby) configured in Tunarr with their libraries, enabled flags and last scan times."""
        async with _client() as client:
            sources = await _call(client, "GET", "/api/media-sources")
            for src in sources:
                src.pop("accessToken", None)
            return sources

    @mcp.tool()
    async def tunarr_scan_status(media_source: str, library: str = "") -> dict | list:
        """Scan state for a media source, or one library of it ('idle'/'queued'/'in_progress' with percentComplete). Pass IDs from tunarr_media_sources."""
        async with _client() as client:
            if library:
                return await _call(client, "GET", f"/api/media-sources/{media_source}/{library}/status")
            return await _call(client, "GET", f"/api/media-sources/{media_source}/status")

    @mcp.tool()
    async def tunarr_search_programs(
        query: str = "", media_source: str = "", library: str = "", filter: dict | None = None,
        restrict_to: list[str] | None = None, sort_by: str = "", descending: bool = False,
        limit: int = 25, page: int = 1,
    ) -> dict:
        """Search Tunarr's synced program database (what can be put on a channel). `query` is free text matched against titles (restrict_to names the fields to search, e.g. ["title"]). `filter` is Tunarr's structured filter tree ({"type":"value","fieldSpec":{...}} / {"type":"op","op":"and"|"or","children":[...]}), the same shape smart collections use. sort_by: title, sortTitle, duration, originalReleaseDate, originalReleaseYear, addedAt, index. Restrict to a media source and library by ID (tunarr_media_sources). Results include each program's Tunarr UUID for tunarr_set_programming."""
        search: dict = {"query": query or None}
        if restrict_to:
            search["restrictSearchTo"] = restrict_to
        if filter:
            search["filter"] = filter
        if sort_by:
            search["sort"] = [{"field": sort_by, "direction": "desc" if descending else "asc"}]
        body: dict = {"query": search, "limit": limit, "page": page}
        if media_source:
            body["mediaSourceId"] = media_source
        if library:
            body["libraryId"] = library
        async with _client() as client:
            return await _call(client, "POST", "/api/programs/search", json=body)

    @mcp.tool()
    async def tunarr_program(program_id: str) -> dict:
        """One program by Tunarr UUID, plus its stream details (codecs, streams, subtitles)."""
        async with _client() as client:
            prog = await _call(client, "GET", f"/api/programs/{program_id}")
            try:
                prog["stream_details"] = await _call(client, "GET", f"/api/programs/{program_id}/stream_details")
            except RuntimeError as exc:
                prog["stream_details"] = {"error": str(exc)}
            return prog

    # ── Read: other resources and settings ────────────────────────────────────

    @mcp.tool()
    async def tunarr_list_resources(kind: str) -> list | dict:
        """List a resource collection. kind: custom-shows, filler-lists, smart-collections, transcode-configs, stream-selection-profiles."""
        async with _client() as client:
            return await _call(client, "GET", _need(_RESOURCES, kind, "kind"))

    @mcp.tool()
    async def tunarr_get_resource(kind: str, resource_id: str, include_programs: bool = False) -> dict:
        """One resource by ID (see tunarr_list_resources for kinds). For custom-shows and filler-lists, include_programs=True also returns their programs."""
        base = _need(_RESOURCES, kind, "kind")
        async with _client() as client:
            out = await _call(client, "GET", f"{base}/{resource_id}")
            if include_programs and kind in ("custom-shows", "filler-lists"):
                out = {"resource": out, "programs": await _call(client, "GET", f"{base}/{resource_id}/programs")}
            return out

    @mcp.tool()
    async def tunarr_get_settings(kind: str) -> dict:
        """Read a settings document. kind: ffmpeg, hdhr, xmltv, plex, system, media-source, feature-flags."""
        async with _client() as client:
            return await _call(client, "GET", _need(_SETTINGS, kind, "kind"))

    @mcp.tool()
    async def tunarr_trash() -> dict:
        """Programs Tunarr has flagged as no longer present in their source (the trash), which can be emptied with tunarr_empty_trash."""
        async with _client() as client:
            return await _call(client, "GET", "/api/trash")

    # ── Audit ─────────────────────────────────────────────────────────────────

    @mcp.tool()
    async def tunarr_audit_programs(channel: str = "", check_jellyfin: bool = False) -> list[dict]:
        """Health-check channel lineups. Per channel: program counts by Tunarr `state` ('ok'/'missing') and the missing programs grouped by show. With check_jellyfin=True, also verifies every program's Jellyfin ID against Jellyfin itself (catches stale IDs before Tunarr's next library scan flags them). Omit `channel` for all."""
        async with _client() as client:
            if channel:
                channels = [await _channel(client, channel)]
            else:
                channels = await _call(client, "GET", "/api/channels")
            results = []
            for ch in channels:
                programs = await _all_programs(client, ch["id"])
                states: dict[str, int] = {}
                missing_by_show: dict[str, int] = {}
                jf_ids = {}
                for p in programs:
                    states[p.get("state") or "unknown"] = states.get(p.get("state") or "unknown", 0) + 1
                    if p.get("state") == "missing":
                        name = (p.get("show") or {}).get("title") or p.get("title") or "?"
                        missing_by_show[name] = missing_by_show.get(name, 0) + 1
                    jid = _ident(p, "jellyfin")
                    if jid:
                        jf_ids[jid.replace("-", "").lower()] = p
                row = {
                    "channel": f"{ch.get('number')} {ch.get('name')}",
                    "programs": len(programs),
                    "by_state": states,
                    "missing_by_show": dict(sorted(missing_by_show.items(), key=lambda kv: -kv[1])),
                }
                if check_jellyfin:
                    absent = await _absent_in_jellyfin(list(jf_ids))
                    row["jellyfin_absent"] = len(absent)
                    shows: dict[str, int] = {}
                    for jid in absent:
                        name = (jf_ids[jid].get("show") or {}).get("title") or jf_ids[jid].get("title") or "?"
                        shows[name] = shows.get(name, 0) + 1
                    row["jellyfin_absent_by_show"] = dict(sorted(shows.items(), key=lambda kv: -kv[1]))
                results.append(row)
            return results

    async def _absent_in_jellyfin(ids: list[str]) -> list[str]:
        """IDs (dashless lowercase) that Jellyfin does not know about."""
        headers = {"Authorization": f'MediaBrowser Token="{config.jellyfin.key}"'}
        present: set[str] = set()
        async with make_client(config.jellyfin.url, headers=headers, timeout=60.0) as jf:
            for i in range(0, len(ids), 100):
                batch = ids[i:i + 100]
                resp = await jf.get("/Items", params={"Ids": ",".join(batch), "Recursive": "true"})
                resp.raise_for_status()
                present.update(it["Id"].replace("-", "").lower() for it in resp.json().get("Items", []))
        return [i for i in ids if i not in present]

    # ── Write: channels and programming ───────────────────────────────────────

    @mcp.tool()
    async def tunarr_create_channel(channel: dict) -> dict:
        """[DESTRUCTIVE] Create a Tunarr channel. `channel` is Tunarr's create body: at minimum name, number, icon, and the other fields shown by tunarr_channel on an existing channel (take one as a template). Show the user the final body and confirm before calling."""
        async with _client() as client:
            return await _call(client, "POST", "/api/channels", json=channel)

    @mcp.tool()
    async def tunarr_update_channel(channel: str, changes: dict) -> dict:
        """[DESTRUCTIVE] Update channel settings (name, number, icon, watermark, stream mode, offline, on-demand, transcode config, ...). Fetches the current channel, merges `changes` over it, and PUTs the whole thing back, so only send the fields you want to change. Programming is not touched; use tunarr_set_programming for that. Confirm with the user first."""
        async with _client() as client:
            ch = await _channel(client, channel)
            current = await _call(client, "GET", f"/api/channels/{ch['id']}")
            return await _call(client, "PUT", f"/api/channels/{ch['id']}", json={**current, **changes})

    @mcp.tool()
    async def tunarr_delete_channel(channel: str) -> dict:
        """[DESTRUCTIVE] Permanently delete a channel and its lineup. Cannot be undone. Confirm with the user first."""
        async with _client() as client:
            ch = await _channel(client, channel)
            await _call(client, "DELETE", f"/api/channels/{ch['id']}")
            return {"deleted": f"{ch.get('number')} {ch.get('name')}", "id": ch["id"]}

    @mcp.tool()
    async def tunarr_set_programming(channel: str, programming: dict) -> dict:
        """[DESTRUCTIVE] Replace (or append to) a channel's programming. `programming` is Tunarr's body, one of: {"type":"manual","lineup":[{"type":"content","id":<program UUID>,"duration":<ms>}, {"type":"flex"|"custom"|"filler"|"redirect",...}],"append":false}; {"type":"time","programs":[...],"schedule":{...}}; {"type":"random","programs":[...],"schedule":{...}}. Program UUIDs come from tunarr_search_programs. Unless append=true this REPLACES the whole lineup. Snapshot first with tunarr_channel_lineup and confirm with the user."""
        async with _client() as client:
            ch = await _channel(client, channel)
            return await _call(client, "POST", f"/api/channels/{ch['id']}/programming", json=programming)

    @mcp.tool()
    async def tunarr_remove_programs(
        channel: str, program_ids: list[str] | None = None, only_missing: bool = False, dry_run: bool = True,
    ) -> dict:
        """[DESTRUCTIVE] Remove programs from a channel's lineup, keeping everything else (order, flex, fillers) intact. Select by `program_ids` (Tunarr UUIDs) and/or `only_missing=True` (every program Tunarr flags 'missing'). Defaults to dry_run=True, which only reports what would be removed; set dry_run=False to apply. The response lists the removed entries so they can be restored. Confirm with the user before applying."""
        if not program_ids and not only_missing:
            raise ValueError("Pass program_ids and/or only_missing=True")
        async with _client() as client:
            ch = await _channel(client, channel)
            prog = await _call(client, "GET", f"/api/channels/{ch['id']}/programming")
            programs = prog.get("programs", {})
            wanted = set(program_ids or [])

            def doomed(entry: dict) -> bool:
                if entry.get("type") != "content":
                    return False
                detail = programs.get(entry.get("id"), {}).get("program") or {}
                return entry.get("id") in wanted or (only_missing and detail.get("state") == "missing")

            keep = [e for e in prog.get("lineup", []) if not doomed(e)]
            removed = [e for e in prog.get("lineup", []) if doomed(e)]
            result = {
                "channel": f"{ch.get('number')} {ch.get('name')}",
                "before": len(prog.get("lineup", [])), "after": len(keep), "removed": len(removed),
                "applied": False,
            }
            if dry_run or not removed:
                return result
            await _call(client, "POST", f"/api/channels/{ch['id']}/programming",
                        json={"type": "manual", "lineup": [_lineup_entry(e) for e in keep], "append": False})
            result["applied"] = True
            result["removed_entries"] = [_lineup_entry(e) for e in removed]
            return result

    @mcp.tool()
    async def tunarr_add_shows(
        channel: str, shows: list[str], placement: str = "append", pad_to_minutes: int = 0,
        seed: int = 0, dry_run: bool = True, max_per_show: int = 0,
    ) -> dict:
        """[DESTRUCTIVE] Add whole TV shows to a channel by title. For each title (exact, case-insensitive) every playable episode ('ok' state) is gathered across duplicate library rows, de-duplicated by season/episode, put in season/episode order, and skipped if already on the channel. placement: 'append' (after the existing lineup), 'round_robin' (rebuild the lineup so shows alternate episode by episode; each show keeps its own order) or 'shuffle_in' (scatter the new episodes at random positions, `seed` makes it repeatable). pad_to_minutes (e.g. 30) follows each new episode with flex up to the next slot boundary, to match channels built that way; leave 0 for back-to-back channels. max_per_show (>0) adds only that many randomly chosen episodes of each show (`seed` makes the pick repeatable), to keep one show from dominating a channel. Existing flex, fillers and redirects are kept. Defaults to dry_run=True, which only reports counts per show; set dry_run=False to rewrite the lineup. Snapshot with tunarr_channel_lineup and confirm with the user before applying."""
        if placement not in ("append", "round_robin", "shuffle_in"):
            raise ValueError("placement must be 'append', 'round_robin' or 'shuffle_in'")
        if not shows:
            raise ValueError("Pass at least one show title")
        async with _client() as client:
            ch = await _channel(client, channel)
            prog = await _call(client, "GET", f"/api/channels/{ch['id']}/programming")
            lineup = prog.get("lineup", [])
            known = prog.get("programs", {})
            shows_by_id = {uid: ((v.get("program") or {}).get("show") or {}).get("title") or "?" for uid, v in known.items()}
            on_channel = {e.get("id") for e in lineup if e.get("type") == "content"}
            slot_ms = pad_to_minutes * 60000

            report, new_units = [], []
            for title in shows:
                rows = []
                for page in range(1, _SEARCH_PAGES + 1):
                    res = await _call(client, "POST", "/api/programs/search", json={
                        "query": {"query": title, "restrictSearchTo": ["title"], "filter": {
                            "type": "value", "fieldSpec": {"key": "type", "name": "type", "type": "string", "op": "=", "value": ["show"]}}},
                        "page": page, "limit": 50,
                    })
                    rows.extend(h for h in res.get("results", []) if (h.get("title") or "").lower() == title.lower())
                    if page >= (res.get("totalPages") or 1):
                        break
                if not rows:
                    report.append({"show": title, "error": "no show with that title in Tunarr's library; check the spelling or scan the library"})
                    continue
                episodes: dict[tuple, dict] = {}
                skipped_missing = skipped_present = 0
                for row in rows:
                    for item in await _call(client, "GET", f"/api/programs/{row['uuid']}/descendants"):
                        p = item.get("program") or {}
                        if p.get("type") != "episode":
                            continue
                        if p.get("state") != "ok":
                            skipped_missing += 1
                            continue
                        key = _season_episode(item)
                        if item["id"] in on_channel:
                            skipped_present += 1
                            episodes.setdefault(key, None)  # the slot is taken; a twin must not fill it
                        elif key not in episodes:
                            episodes[key] = item
                added = [v for _, v in sorted(episodes.items()) if v is not None]
                if max_per_show > 0 and len(added) > max_per_show:
                    picked = set(random.Random(seed).sample(range(len(added)), max_per_show))
                    added = [v for i, v in enumerate(added) if i in picked]
                for item in added:
                    dur = item.get("duration") or (item.get("program") or {}).get("duration") or 0
                    entries = [{"type": "content", "id": item["id"], "duration": dur}]
                    if slot_ms and dur:
                        pad = math.ceil(dur / slot_ms) * slot_ms - dur
                        if pad > 0:
                            entries.append({"type": "flex", "duration": pad})
                    new_units.append((title, entries))
                report.append({
                    "show": title, "library_rows": len(rows), "to_add": len(added),
                    "already_on_channel": skipped_present, "skipped_not_playable": skipped_missing,
                    "hours": round(sum((i.get("duration") or 0) for i in added) / 3600000, 1),
                })

            existing = _units(lineup, shows_by_id)
            merged = _place(existing, [(t, [dict(e) for e in es]) for t, es in new_units], placement, seed)
            new_lineup = [e for _, es in merged for e in es]
            result = {
                "channel": f"{ch.get('number')} {ch.get('name')}", "placement": placement,
                "shows": report, "entries_before": len(lineup), "entries_after": len(new_lineup),
                "applied": False,
            }
            if dry_run or not new_units:
                return result
            await _call(client, "POST", f"/api/channels/{ch['id']}/programming",
                        json={"type": "manual", "lineup": new_lineup, "append": False})
            result["applied"] = True
            return result

    @mcp.tool()
    async def tunarr_rematch_missing(
        channel: str = "", dry_run: bool = True, drop_unmatched: bool = False,
    ) -> list[dict]:
        """[DESTRUCTIVE] Repair lineups after Jellyfin re-IDed its library: for every lineup entry whose program Tunarr flags 'missing', find its freshly scanned twin (state 'ok': same title, year and episode number; IMDb/TVDB ids must agree where present, otherwise duration must match; the lineup slot takes the new file's duration) and swap the entry over, keeping order, flex and fillers. Run a library scan first (tunarr_scan_library) so the twins exist. Omit `channel` for all channels. Defaults to dry_run=True, which only reports matched/unmatched counts and examples; set dry_run=False to rewrite the lineups. Unmatched entries are kept (still broken) unless drop_unmatched=True removes them. Confirm with the user before applying."""
        async with _client() as client:
            if channel:
                channels = [await _channel(client, channel)]
            else:
                channels = await _call(client, "GET", "/api/channels")
            plans = []
            for ch in channels:
                programs = {p["uuid"]: p for p in await _all_programs(client, ch["id"])}
                prog = await _call(client, "GET", f"/api/channels/{ch['id']}/programming")
                lineup = prog.get("lineup", [])
                dead = {e["id"] for e in lineup if e.get("type") == "content" and (programs.get(e.get("id")) or {}).get("state") == "missing"}
                plans.append((ch, programs, lineup, dead))

            # One search per distinct (title, library), run concurrently and shared across channels.
            wanted: dict[tuple, dict] = {}
            for _, programs, _, dead in plans:
                for uid in dead:
                    old = programs[uid]
                    wanted.setdefault((old.get("title") or "", old.get("mediaSourceId") or "", old.get("libraryId") or ""), old)
            sem = asyncio.Semaphore(_SEARCH_CONCURRENCY)

            async def run(key):
                async with sem:
                    return key, await _search_title(client, *key)

            hits = dict(await asyncio.gather(*(run(k) for k in wanted)))

            results = []
            for ch, programs, lineup, dead in plans:
                replace: dict[str, dict] = {}
                unmatched = []
                for uid in dead:
                    old = programs[uid]
                    key = (old.get("title") or "", old.get("mediaSourceId") or "", old.get("libraryId") or "")
                    twin = _best_twin(old, hits[key])
                    if twin:
                        replace[uid] = twin
                    else:
                        unmatched.append(old)
                new_lineup = []
                for entry in lineup:
                    if entry.get("id") in replace:
                        twin = replace[entry["id"]]
                        # The new file may be a different length; the slot must follow it.
                        new_lineup.append({**_lineup_entry(entry), "id": twin["uuid"],
                                           "duration": twin.get("duration") or entry.get("duration")})
                    elif entry.get("id") in dead and drop_unmatched:
                        continue
                    else:
                        new_lineup.append(_lineup_entry(entry))
                row = {
                    "channel": f"{ch.get('number')} {ch.get('name')}",
                    "lineup_entries": len(lineup),
                    "missing_programs": len(dead),
                    "matched": len(replace),
                    "duration_changed": sum(
                        1 for uid, tw in replace.items()
                        if abs((tw.get("duration") or 0) - (programs[uid].get("duration") or 0)) > _DURATION_SLOP_MS),
                    "unmatched": len(unmatched),
                    "unmatched_examples": [
                        f"{(u.get('show') or {}).get('title') or '?'} - {u.get('title')} (e{u.get('episodeNumber')}, {u.get('year')})"
                        for u in unmatched[:10]
                    ],
                    "applied": False,
                }
                if not dry_run and (replace or (drop_unmatched and unmatched)):
                    await _call(client, "POST", f"/api/channels/{ch['id']}/programming",
                                json={"type": "manual", "lineup": new_lineup, "append": False})
                    row["applied"] = True
                    row["lineup_entries_after"] = len(new_lineup)
                results.append(row)
            return results

    @mcp.tool()
    async def tunarr_generate_schedule(channel: str, mode: str, schedule: dict) -> dict:
        """[DESTRUCTIVE] Generate a channel schedule from slots and apply it to the channel's existing programming. mode 'slots' (Tunarr schedule-slots: ordered slots with a duration/order each) or 'time-slots' (schedule-time-slots: slots pinned to times of day). `schedule` is Tunarr's schedule object. Replaces the channel's lineup; confirm with the user first."""
        path = {"slots": "schedule-slots", "time-slots": "schedule-time-slots"}.get(mode)
        if path is None:
            raise ValueError("mode must be 'slots' or 'time-slots'")
        async with _client() as client:
            ch = await _channel(client, channel)
            return await _call(client, "POST", f"/api/channels/{ch['id']}/{path}", json={"schedule": schedule})

    @mcp.tool()
    async def tunarr_stop_sessions(channel: str) -> dict:
        """[DESTRUCTIVE] Stop all active stream sessions on a channel (viewers are disconnected and will have to retune). Use after fixing a channel to clear a stuck stream."""
        async with _client() as client:
            ch = await _channel(client, channel)
            return await _call(client, "DELETE", f"/api/channels/{ch['id']}/sessions")

    # ── Write: custom shows, fillers, collections, transcode configs, profiles ─

    @mcp.tool()
    async def tunarr_save_resource(kind: str, body: dict, resource_id: str = "") -> dict:
        """[DESTRUCTIVE] Create or replace a resource. kind: custom-shows, filler-lists, smart-collections, transcode-configs, stream-selection-profiles. Omit resource_id to create; pass it to REPLACE that resource entirely (fetch it with tunarr_get_resource, edit, send the whole). Show the user the final body and confirm first."""
        base = _need(_RESOURCES, kind, "kind")
        async with _client() as client:
            if resource_id:
                return await _call(client, "PUT", f"{base}/{resource_id}", json=body)
            return await _call(client, "POST", base, json=body)

    @mcp.tool()
    async def tunarr_delete_resource(kind: str, resource_id: str) -> dict:
        """[DESTRUCTIVE] Delete a custom show, filler list, smart collection, transcode config or stream-selection profile. Channels referencing it may break. Confirm with the user first."""
        base = _need(_RESOURCES, kind, "kind")
        async with _client() as client:
            return await _call(client, "DELETE", f"{base}/{resource_id}")

    @mcp.tool()
    async def tunarr_copy_transcode_config(config_id: str) -> dict:
        """Duplicate a transcode config (a safe starting point for edits instead of changing one in use)."""
        async with _client() as client:
            return await _call(client, "POST", f"/api/transcode_configs/{config_id}/copy")

    @mcp.tool()
    async def tunarr_sync_custom_show(custom_show_id: str) -> dict:
        """Re-sync a custom show from its external playlist source (only for custom shows created with a sync source)."""
        async with _client() as client:
            return await _call(client, "POST", f"/api/custom-shows/{custom_show_id}/sync")

    @mcp.tool()
    async def tunarr_set_settings(kind: str, settings: dict) -> dict:
        """[DESTRUCTIVE] Replace a settings document. kind: ffmpeg, hdhr, xmltv, plex, system, media-source, feature-flags. Read it first with tunarr_get_settings, edit, send the whole document back. Some settings need a restart to take effect. Confirm with the user first."""
        async with _client() as client:
            return await _call(client, "PUT", _need(_SETTINGS, kind, "kind"), json=settings)

    # ── Write: media sources, scans, tasks, maintenance ───────────────────────

    @mcp.tool()
    async def tunarr_scan_library(media_source: str, library: str, force: bool = True) -> dict:
        """Start a scan of one library so Tunarr re-syncs its program database from the media source (this is what refreshes IDs and flips programs between 'ok' and 'missing'). Returns immediately; a large library takes many minutes, so poll tunarr_scan_status. IDs come from tunarr_media_sources."""
        async with _client() as client:
            return await _call(
                client, "POST", f"/api/media-sources/{media_source}/libraries/{library}/scan",
                params={"forceScan": str(force).lower()},
            )

    @mcp.tool()
    async def tunarr_refresh_libraries(media_source: str) -> dict:
        """Re-fetch the list of libraries a media source offers (new/removed libraries). Does not scan their contents."""
        async with _client() as client:
            return await _call(client, "POST", f"/api/media-sources/{media_source}/libraries/refresh")

    @mcp.tool()
    async def tunarr_set_library_enabled(media_source: str, library: str, enabled: bool) -> dict:
        """[DESTRUCTIVE] Enable or disable a library for Tunarr. Disabled libraries are not scanned and their programs cannot be added to channels."""
        async with _client() as client:
            return await _call(
                client, "PUT", f"/api/media-sources/{media_source}/libraries/{library}", json={"enabled": enabled},
            )

    @mcp.tool()
    async def tunarr_run_task(task_id: str) -> dict:
        """Run a scheduled Tunarr task now. IDs are listed by tunarr_status (e.g. ScanLibrariesTask, RefreshMediaSourceLibraryTask, BackupTask)."""
        async with _client() as client:
            return await _call(client, "POST", f"/api/tasks/{task_id}/run")

    @mcp.tool()
    async def tunarr_run_fixer(fixer_id: str) -> dict:
        """Run one of Tunarr's built-in data fixers by ID (repairs for known DB inconsistencies). Valid IDs are in Tunarr's docs and server logs (use k8s_pod_logs)."""
        async with _client() as client:
            return await _call(client, "POST", f"/api/system/fixers/{fixer_id}/run")

    @mcp.tool()
    async def tunarr_refresh_xmltv() -> dict:
        """Regenerate the XMLTV guide file that Jellyfin (and other clients) read."""
        async with _client() as client:
            return await _call(client, "POST", "/api/xmltv/refresh")

    @mcp.tool()
    async def tunarr_empty_trash() -> dict:
        """[DESTRUCTIVE] Permanently delete the programs Tunarr has in its trash (items missing from their source). Confirm with the user first."""
        async with _client() as client:
            return await _call(client, "DELETE", "/api/trash")

    # ── Escape hatch ──────────────────────────────────────────────────────────

    @mcp.tool()
    async def tunarr_api(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict | list:
        """[DESTRUCTIVE unless GET] Call any Tunarr API endpoint directly. `path` starts with /api/ (the OpenAPI spec is at /openapi.json). Use for anything the dedicated tunarr_* tools do not cover. Non-GET calls change live state: confirm with the user first."""
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("method must be GET, POST, PUT, PATCH or DELETE")
        if not path.startswith("/api/"):
            raise ValueError("path must start with /api/")
        async with _client() as client:
            return await _call(client, method, path, json=body, params=query)


def _lineup_entry(entry: dict) -> dict:
    """Reduce a lineup entry from GET /programming to the shape POST /programming accepts."""
    keep = ("type", "id", "duration", "icon", "customShowId", "index", "fillerListId", "fillerType",
            "channel", "channelNumber", "channelName", "startOffsetMs")
    return {k: entry[k] for k in keep if k in entry}


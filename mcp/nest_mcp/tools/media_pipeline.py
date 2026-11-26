"""One-call view of the "add a second version of a movie" pipeline: SABnzbd job -> files on disk -> Jellyfin versions."""
import posixpath
import re
import shlex

from mcp.server.mcpserver import MCPServer

from nest_mcp import config
from nest_mcp.http_client import make_client
from nest_mcp.ssh_client import ssh_run
from nest_mcp.tools.jellyfin import _headers, _source_view
from nest_mcp.tools.media_files import _host_path
from nest_mcp.tools.sabnzbd import _f, sab_call

_VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts")


def _words(s: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]


def _matches(job_name: str, title_words: list[str], year: int) -> bool:
    have = set(_words(job_name))
    return all(w in have for w in title_words) and (not year or str(year) in have)


def _brief(src: dict) -> dict:
    v = (src.get("video") or [{}])[0]
    a = (src.get("audio") or [{}])[0]
    probed = bool(src.get("video"))
    return {
        "name": src.get("name", ""), "file": posixpath.basename(src.get("path", "")), "container": src.get("container", ""),
        "size_gb": src.get("size_gb"), "bitrate_mbps": src.get("bitrate_mbps"), "probed": probed,
        "video": f'{v.get("codec", "")} {v.get("resolution", "")} {v.get("range", "")}'.strip() if probed else "not probed yet",
        "audio": f'{a.get("codec", "")} {a.get("channels", "")}ch'.strip() if src.get("audio") else "",
        "audio_note": a.get("note", ""),
    }


def register(mcp: MCPServer) -> None:

    @mcp.tool()
    async def movie_version_status(title: str, year: int = 0) -> dict:
        """The whole "second version of a movie" pipeline in one call, so progress is one question instead of several: (1) SABnzbd jobs whose name matches the title (+year), in the queue or recent history, with state / percentage / fail message / `storage` path; (2) Jellyfin's versions of the movie (file, container, size, bitrate, video, audio, and whether Jellyfin has probed the file yet); (3) video files sitting in the movie's folder that Jellyfin does not list yet (placed but not scanned). `summary` says what to do next. Read-only."""
        words = _words(title)
        if not words:
            raise ValueError("Pass a title")

        jobs: list[dict] = []
        try:
            q = (await sab_call("queue", limit=500)).get("queue", {})
            for s in q.get("slots", []):
                if _matches(s.get("filename", ""), words, year):
                    jobs.append({"where": "queue", "nzo_id": s.get("nzo_id", ""), "name": s.get("filename", ""), "category": s.get("cat", ""),
                                 "state": s.get("status", ""), "percentage": s.get("percentage", ""), "mb_left": round(_f(s.get("mbleft"))),
                                 "eta": s.get("timeleft", "")})
            hist = (await sab_call("history", limit=200)).get("history", {}).get("slots", [])
            for s in hist:
                if _matches(s.get("name", ""), words, year):
                    jobs.append({"where": "history", "nzo_id": s.get("nzo_id", ""), "name": s.get("name", ""), "category": s.get("category", ""),
                                 "state": s.get("status", ""), "fail_message": s.get("fail_message", ""), "storage": s.get("storage", "")})
        except Exception as e:
            jobs = [{"error": str(e)[:200]}]

        movies: list[dict] = []
        async with make_client(config.jellyfin.url, headers=_headers()) as client:
            resp = await client.get("/Items", params={
                "IncludeItemTypes": "Movie", "Recursive": "true", "searchTerm": title,
                "Fields": "MediaSources,MediaStreams,Path,ProductionYear", "Limit": 10,
            })
            resp.raise_for_status()
            items = resp.json().get("Items", [])
        if year:
            items = [i for i in items if i.get("ProductionYear") == year]

        for i in items:
            versions = [_brief(_source_view(src)) for src in i.get("MediaSources", [])]
            folder = posixpath.dirname(i["MediaSources"][0].get("Path", "")) if i.get("MediaSources") else ""
            listed = {v["file"] for v in versions}
            unlisted: list[str] | str = []
            if folder:
                try:
                    raw = await ssh_run(config.fileserver.host, f"ls -1 -- {shlex.quote(_host_path(folder))}", key=config.fileserver.ssh_key, timeout=30)
                    unlisted = [f for f in raw.splitlines() if f.lower().endswith(_VIDEO_EXT) and f not in listed]
                except Exception as e:
                    unlisted = f"could not list folder: {(str(e) or type(e).__name__)[:120]}"
            movies.append({"id": i.get("Id"), "name": i.get("Name"), "year": i.get("ProductionYear"), "folder": folder,
                           "versions": versions, "on_disk_not_in_jellyfin": unlisted})

        notes: list[str] = []
        active = [j for j in jobs if j.get("where") == "queue"]
        if active:
            notes.append(f"{len(active)} SABnzbd job(s) still downloading; sabnzbd_wait(nzo_id) to wait.")
        for j in jobs:
            if j.get("where") == "history":
                if j["state"] == "Failed":
                    notes.append(f"{j['name']} FAILED: {j.get('fail_message') or 'no message'}; sabnzbd_history_action retry, or grab another release.")
                elif j["state"] == "Completed":
                    notes.append(f"{j['name']} is Completed and not yet placed? media_verify_file then media_place_version(source_path={j.get('storage')!r}) if it is not in the movie folder.")
                else:
                    notes.append(f"{j['name']} is {j['state']} (post-processing); check again shortly.")
        for m in movies:
            if isinstance(m["on_disk_not_in_jellyfin"], list) and m["on_disk_not_in_jellyfin"]:
                notes.append(f"{m['name']}: on disk but not in Jellyfin: {', '.join(m['on_disk_not_in_jellyfin'])}; jellyfin_refresh(created_paths=[folder]) and re-check.")
            unprobed = [v["file"] for v in m["versions"] if not v["probed"]]
            if unprobed:
                notes.append(f"{m['name']}: Jellyfin has not probed {', '.join(unprobed)} yet (streams empty); give it a minute.")
            notes.append(f"{m['name']}: {len(m['versions'])} version(s) in Jellyfin.")
        if not movies:
            notes.append("No matching movie in Jellyfin.")
        return {"title": title, "year": year or None, "downloads": jobs, "jellyfin": movies, "summary": notes}

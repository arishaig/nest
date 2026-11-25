import json
import re
import shlex

from mcp.server.mcpserver import MCPServer

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run
from nest_mcp.tools.sabnzbd import sab_call

_MAX_ENTRIES = 500
_MAX_DEPTH = 3
_SURVEY_DEFAULT_TIMEOUT = 120
_SURVEY_MAX_TIMEOUT = 1800


_PLACE_TIMEOUT = 3600
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,59}$")
_CONTAINER_DATA = "/data/"  # where pods mount the media NFS export (PV media-nfs = <media_root> on PVE)

# Runs on PVE as root (python3 -c SCRIPT JSON_ARGS). Self-contained so it can be tested locally.
# Re-validates every path with realpath: the caller's checks are not trusted.
_PLACE_SCRIPT = r"""
import json, os, re, shutil, stat, sys
a = json.loads(sys.argv[1])
VID = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts")
def out(d):
    print(json.dumps(d)); sys.exit(0)
def real(p): return os.path.realpath(p)
def under(p, root):
    p, root = real(p), real(root)
    return p != root and p.startswith(root + os.sep)
src, folder = a["src"], a["folder"]
if not os.path.exists(src): out({"error": "source does not exist: " + src})
if not under(src, a["src_root"]): out({"error": "source is not inside the SABnzbd complete dir"})
if not os.path.isdir(folder): out({"error": "movie folder does not exist: " + folder})
if real(os.path.dirname(real(folder))) != real(a["movies_root"]) or not under(folder, a["movies_root"]):
    out({"error": "movie folder must be a direct child of the movies directory"})
def is_sample(path):
    parts = path.split(os.sep)
    stem = re.split(r"[^a-z0-9]+", os.path.splitext(parts[-1])[0].lower())
    return "sample" in stem or any(x.lower() in ("sample", "samples") for x in parts[-3:-1])
cands = []
if os.path.isfile(src): walk = [(os.path.dirname(src), [], [os.path.basename(src)])]
else: walk = os.walk(src)
for d, _, files in walk:
    for f in files:
        p = os.path.join(d, f)
        if f.lower().endswith(VID) and not os.path.islink(p) and not is_sample(p) and under(p, a["src_root"]):
            cands.append((os.path.getsize(p), p))
if not cands: out({"error": "no video file (non-sample) found in the source"})
cands.sort(reverse=True)
size, video = cands[0]
name = os.path.basename(folder)
existing = sorted(f for f in os.listdir(folder) if f.lower().endswith(VID))
def groups(fname):
    # Mirrors Jellyfin's VideoListResolver.IsEligibleForMultiVersion (minus its keyword cleaning, so
    # stricter): starts with the folder name (any case), then nothing, or "-", "_", "." or "[".
    stem = os.path.splitext(fname)[0]
    if not stem.lower().startswith(name.lower()): return False
    rest = stem[len(name):].strip()
    return rest == "" or rest[0] in "-_.["
violators = [f for f in existing if not groups(f)]
base = {"source_file": video, "size_gb": round(size / 1024**3, 2), "existing_videos": existing, "ignored_other_videos": [os.path.basename(p) for _, p in cands[1:]][:10]}
if violators:
    out({**base, "status": "blocked", "not_groupable": violators,
         "reason": "Jellyfin only groups versions when every video file in the folder is named like the folder %r, optionally followed by ' - label', '[label]', '_' or '.'" % name})
ext = os.path.splitext(video)[1]
dest = os.path.join(folder, "%s - %s%s" % (name, a["label"], ext))
moves = [(video, dest)]
if a["subs"]:
    stem = os.path.splitext(os.path.basename(video))[0]
    for f in sorted(os.listdir(os.path.dirname(video))):
        if f.lower().endswith(".srt") and f.startswith(stem):
            moves.append((os.path.join(os.path.dirname(video), f), os.path.join(folder, "%s - %s%s" % (name, a["label"], f[len(stem):]))))
for _, d in moves:
    if os.path.lexists(d) or os.path.lexists(d + ".partial"):
        out({**base, "status": "blocked", "reason": "destination already exists (never overwritten): " + d})
ref = os.path.join(folder, existing[0]) if existing else folder
st = os.stat(ref)
uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode) & 0o666 if existing else 0o644
same_fs = os.stat(video).st_dev == os.stat(folder).st_dev
plan = {**base, "destination": dest, "method": "rename" if same_fs else "copy+verify+delete",
        "owner": "%d:%d" % (uid, gid), "mode": oct(mode), "files": [{"from": s, "to": d} for s, d in moves]}
if not a["apply"]: out({**plan, "status": "dry_run"})
done = []
try:
    for s, d in moves:
        if os.stat(s).st_dev == os.stat(folder).st_dev:
            os.rename(s, d)
        else:
            tmp = d + ".partial"
            shutil.copyfile(s, tmp)
            if os.path.getsize(tmp) != os.path.getsize(s): raise IOError("size mismatch after copy: " + s)
            os.rename(tmp, d)
            os.remove(s)
        os.chown(d, uid, gid); os.chmod(d, mode)
        done.append(d)
except Exception as e:
    for s, d in moves:
        if os.path.exists(d + ".partial"): os.remove(d + ".partial")
    out({**plan, "status": "failed", "error": str(e), "completed": done})
out({**plan, "status": "placed", "completed": done})
"""


def _host_path(p: str) -> str:
    """Container path (/data/...) or host path -> host path under media_root; reject anything else."""
    if "\x00" in p or ".." in p.split("/"):
        raise ValueError(f"Invalid path {p!r}")
    root = config.fileserver.media_root.rstrip("/")
    if p.startswith(_CONTAINER_DATA):
        return f"{root}/{p[len(_CONTAINER_DATA):]}"
    if p.startswith(root + "/"):
        return p
    raise ValueError(f"{p!r} is not under {_CONTAINER_DATA} (the media share) or {root}/")


def _media_subtree_root() -> str:
    """tv/movies live under a `media/` subdir of the exported ZFS dataset."""
    return f"{config.fileserver.media_root.rstrip('/')}/media"


def _parse_ncdu(node: list, depth: int, current: int = 0, prefix: str = "") -> list[dict]:
    """Flatten an ncdu directory array into a list of entries up to `depth` levels.

    ncdu array format: [dir_meta_dict, child, child, ...]
    Each child is either a dict (file) or a list (subdirectory, same format).
    dir_meta_dict keys: name, asize (apparent), dsize (disk, ZFS-aware).
    """
    results = []
    for item in node[1:]:  # index 0 is the directory's own metadata
        if isinstance(item, dict):
            name = item.get("name", "")
            results.append({
                "type": "file",
                "name": name,
                "path": f"{prefix}/{name}" if prefix else name,
                "size_mb": round(item.get("dsize", item.get("asize", 0)) / 1_048_576, 1),
            })
        elif isinstance(item, list) and item:
            meta = item[0] if isinstance(item[0], dict) else {}
            name = meta.get("name", "")
            full_path = f"{prefix}/{name}" if prefix else name
            results.append({
                "type": "dir",
                "name": name,
                "path": full_path,
                "size_mb": round(meta.get("dsize", meta.get("asize", 0)) / 1_048_576, 1),
            })
            if current < depth - 1:
                results.extend(_parse_ncdu(item, depth, current + 1, full_path))
    return results


def register(mcp: MCPServer) -> None:

    @mcp.tool()
    async def media_ls(path: str = "", depth: int = 1) -> dict:
        """List files and directories on the media NFS share with cumulative directory sizes.

        Runs ncdu on PVE (the ZFS/NFS source) and returns a structured view of the
        media filesystem. Directory sizes are cumulative and ZFS-compression-aware,
        so you can directly compare what's on disk against what Sonarr/Radarr/Jellyfin report.

        Args:
            path:  Relative path under the media root (e.g. "tv", "tv/Star Trek",
                   "movies/Dune (2021)"). Defaults to the media root itself.
            depth: Levels to descend (1 = immediate children only, max 3).
                   Scanning a broad path at depth > 1 (e.g. all of tv/) can take 10-30s
                   while ncdu traverses every file; narrow the path first.
        """
        if path:
            parts = path.split("/")
            if ".." in parts or path.startswith("/") or "\x00" in path:
                return {"error": "Invalid path — no '..' segments or absolute paths allowed."}

        depth = min(max(1, depth), _MAX_DEPTH)

        root = _media_subtree_root()
        target = f"{root}/{path}".rstrip("/") if path else root

        # stderr stays attached (2>&1) so a bad path shows ncdu's own message instead of empty output.
        cmd = f"/usr/bin/ncdu -0 -x -o - {shlex.quote(target)} 2>&1"
        try:
            raw = await ssh_run(
                config.fileserver.host, cmd,
                key=config.fileserver.ssh_key,
                timeout=60,
            )
        except Exception as e:
            return {"error": str(e)}

        if not raw.lstrip().startswith("["):
            return {"error": f"ncdu failed: {raw[:300] or 'no output'}"}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse ncdu output: {e}", "raw_preview": raw[:300]}

        # ncdu format: [version, reserved, {metadata}, root_array]
        if not isinstance(data, list) or len(data) < 4:
            return {"error": "Unexpected ncdu output format", "raw_preview": str(data)[:300]}

        version = data[0]
        if version not in (1, 2):
            return {"error": f"Unsupported ncdu format version {version}"}

        root_array = data[3]
        entries = _parse_ncdu(root_array, depth=depth)

        truncated = len(entries) > _MAX_ENTRIES
        return {
            "root": target,
            "depth": depth,
            "total": len(entries),
            "truncated": truncated,
            "entries": entries[:_MAX_ENTRIES],
        }

    @mcp.tool()
    async def media_audio_lang_survey(path: str = "", timeout: int = _SURVEY_DEFAULT_TIMEOUT) -> dict:
        """Survey video files for English audio tracks that aren't the default audio stream.

        Read-only. Runs ffprobe (via survey_audio_lang.py, deployed by
        playbooks/provision/pve.yml) on PVE — the ZFS pool owner — and buckets
        every video file under the scanned path:
          - ok:          an eng-tagged audio stream is already the default
          - needs_fix:   an eng-tagged stream exists but isn't default (fixable
                         losslessly with mkvpropedit, no re-encode)
          - no_eng_tag:  no audio stream is tagged eng — needs manual inspection
          - no_audio / error

        Args:
            path:    Relative path under the media root (e.g. "tv", "tv/Star Trek",
                     "movies/Dune (2021)"). Defaults to scanning "tv" and "movies"
                     together — on a large library that can take several minutes;
                     narrow to a subfolder for a faster, targeted check.
            timeout: Seconds to allow the scan (default 120, max 1800). Raise this
                     for a broad, unscoped survey rather than re-running on timeout —
                     a full tv+movies library can take many minutes.
        """
        if path:
            parts = path.split("/")
            if ".." in parts or path.startswith("/") or "\x00" in path:
                return {"error": "Invalid path — no '..' segments or absolute paths allowed."}

        timeout = min(max(10, timeout), _SURVEY_MAX_TIMEOUT)

        root = _media_subtree_root()
        targets = [f"{root}/{path}".rstrip("/")] if path else [f"{root}/tv", f"{root}/movies"]

        cmd = "python3 /usr/local/bin/survey_audio_lang.py --json " + " ".join(shlex.quote(t) for t in targets)
        try:
            raw = await ssh_run(
                config.fileserver.host, cmd,
                key=config.fileserver.ssh_key,
                timeout=timeout,
            )
        except TimeoutError:
            return {"error": f"Scan exceeded {timeout}s — narrow `path` to a subfolder or raise `timeout`."}
        except Exception as e:
            return {"error": str(e)}

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse survey output: {e}", "raw_preview": raw[:300]}

    @mcp.tool()
    async def media_audio_lang_fix(path: str = "", apply: bool = False, timeout: int = _SURVEY_DEFAULT_TIMEOUT) -> dict:
        """Set English as the default audio track wherever it exists but isn't default.

        Lossless: runs mkvpropedit (via fix_audio_lang.py, deployed by
        playbooks/provision/pve.yml) on PVE — the ZFS pool owner — to flip
        container-level disposition flags only. No re-encode, no re-mux,
        stream data untouched. Runs on PVE rather than the fileserver LXC
        because that LXC is unprivileged and can't write files owned by
        UIDs outside its idmap range (e.g. root, from k8s pods writing over
        NFS) — PVE has real root access to every file regardless of owner.
        Only .mkv files are eligible for the write path; other containers
        are reported under skipped_unsupported_container.

        Defaults to `apply=False` (dry run) — reports what it *would* change
        without touching any file. Always dry-run a path first and review the
        counts/examples before passing apply=True. Buckets:
          - fixed / would_fix:              default flag changed (or would be)
          - already_ok:                     English is already default
          - skipped_unsupported_container:  needs a fix but isn't .mkv
          - skipped_no_eng:                 no eng-tagged audio stream at all
          - error:                          ffprobe/mkvpropedit failed on the file

        Args:
            path:    Relative path under the media root (e.g. "tv", "tv/Star Trek",
                     "movies/Dune (2021)"). Defaults to scanning "tv" and "movies"
                     together. Narrow this to review/apply incrementally rather
                     than fixing the whole library in one call.
            apply:   False (default) = dry run, changes nothing. True = actually
                     run mkvpropedit on every needs_fix .mkv found.
            timeout: Seconds to allow the run (default 120, max 1800). A full
                     tv+movies pass can take many minutes; narrow `path` or
                     raise this rather than re-running on timeout.
        """
        if path:
            parts = path.split("/")
            if ".." in parts or path.startswith("/") or "\x00" in path:
                return {"error": "Invalid path — no '..' segments or absolute paths allowed."}

        timeout = min(max(10, timeout), _SURVEY_MAX_TIMEOUT)

        root = _media_subtree_root()
        targets = [f"{root}/{path}".rstrip("/")] if path else [f"{root}/tv", f"{root}/movies"]

        cmd = "python3 /usr/local/bin/fix_audio_lang.py --json "
        if apply:
            cmd += "--apply "
        cmd += " ".join(shlex.quote(t) for t in targets)

        try:
            raw = await ssh_run(
                config.fileserver.host, cmd,
                key=config.fileserver.ssh_key,
                timeout=timeout,
            )
        except TimeoutError:
            return {"error": f"Run exceeded {timeout}s — narrow `path` to a subfolder or raise `timeout`."}
        except Exception as e:
            return {"error": str(e)}

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse fix output: {e}", "raw_preview": raw[:300]}

    @mcp.tool()
    async def media_place_version(
        source_path: str, movie_folder: str, version_label: str, dry_run: bool = True, include_subtitles: bool = False,
    ) -> dict:
        """[DESTRUCTIVE when dry_run=False] Move a finished download into a movie's folder as an extra Jellyfin version, e.g. `The Blues Brothers (1980) - 2160p x265.mkv` next to the existing file. `source_path`: the completed download (file or folder) under SABnzbd's complete dir, as containers see it (/data/usenet/complete/..., see sabnzbd_history `storage`). `movie_folder`: the movie's folder (/data/media/movies/Name (Year), or just `Name (Year)`). `version_label`: letters/digits/space/._+- (e.g. `1080p x265 20Mbps`).

        Jellyfin only groups versions when EVERY video file in the folder is named like the folder (`Name (Year)`), optionally followed by ` - label`, `[label]`, `_` or `.` (so Radarr's default `Name (Year) Remux-2160p` does NOT group). If an existing file does not qualify (e.g. `The.Blues.Brothers.1980...mkv`), this reports status `blocked` and changes nothing: fix the existing name via radarr_rename_files (never renamed behind Radarr's back here). Moves only the largest non-sample video (plus same-name .srt files with include_subtitles=True); never overwrites; other files in the download are left where they are. Same filesystem = atomic rename, otherwise copy, size-verify, then delete the source. Owner and mode copy the existing file's. dry_run=True (default) returns the exact plan first. Afterwards run jellyfin_refresh(created_paths=[the movie folder]) and jellyfin_media_info to see both versions.

        Radarr caveat (unverified): its folder rescan may import an untracked video that beats its tracked file and recycle the old one (see `rescanAfterRefresh` in Radarr's /api/v3/config/mediamanagement). Check after the first real use. Run dry_run=True first and read `method`: `rename` means same filesystem; `copy+verify+delete` means a slow copy. Confirm with the user first."""
        if not _LABEL_RE.match(version_label):
            raise ValueError("version_label may only contain letters, digits, space and . _ + - (max 60, must start with a letter or digit)")
        movies = f"{_media_subtree_root()}/movies"
        if "/" not in movie_folder:
            movie_folder = f"{movies}/{movie_folder}"
        complete = (await sab_call("get_config", section="misc")).get("config", {}).get("misc", {}).get("complete_dir", "")
        if not complete:
            raise RuntimeError("SABnzbd did not report its complete_dir; run sabnzbd_status.")
        args = {
            "src": _host_path(source_path), "folder": _host_path(movie_folder), "label": version_label,
            "subs": include_subtitles, "apply": not dry_run,
            "src_root": _host_path(complete), "movies_root": movies,
        }
        cmd = f"python3 -c {shlex.quote(_PLACE_SCRIPT)} {shlex.quote(json.dumps(args))}"
        try:
            raw = await ssh_run(config.fileserver.host, cmd, key=config.fileserver.ssh_key, timeout=_PLACE_TIMEOUT)
        except TimeoutError:
            return {"error": f"Placement exceeded {_PLACE_TIMEOUT}s; check media_ls: a .partial file may remain next to the destination."}
        except Exception as e:
            return {"error": str(e)[:300]}
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "Unexpected output from PVE", "raw_preview": raw[:300]}
        # Jellyfin and Radarr see /data/..., not the PVE path: hand back the path jellyfin_refresh needs.
        root = config.fileserver.media_root.rstrip("/")
        folder = args["folder"]
        out["movie_folder_container_path"] = _CONTAINER_DATA + folder[len(root) + 1:]
        if out.get("status") == "placed":
            out["next"] = f'jellyfin_refresh(created_paths=["{out["movie_folder_container_path"]}"]), then jellyfin_media_info to confirm two sources'
        return out

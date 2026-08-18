import json
import shlex

from mcp.server.mcpserver import MCPServer

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run

_MAX_ENTRIES = 500
_MAX_DEPTH = 3
_SURVEY_DEFAULT_TIMEOUT = 120
_SURVEY_MAX_TIMEOUT = 600


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

        root = config.fileserver.media_root.rstrip("/")
        target = f"{root}/{path}".rstrip("/") if path else root

        cmd = f"/usr/bin/ncdu -0 -x -o - {shlex.quote(target)} 2>/dev/null"
        try:
            raw = await ssh_run(
                config.fileserver.host, cmd,
                key=config.fileserver.ssh_key,
                timeout=60,
            )
        except Exception as e:
            return {"error": str(e)}

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
        playbooks/provision/fileserver.yml) on the fileserver LXC and buckets
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
            timeout: Seconds to allow the scan (default 120, max 600). Raise this
                     for a broad, unscoped survey rather than re-running on timeout.
        """
        if path:
            parts = path.split("/")
            if ".." in parts or path.startswith("/") or "\x00" in path:
                return {"error": "Invalid path — no '..' segments or absolute paths allowed."}

        timeout = min(max(10, timeout), _SURVEY_MAX_TIMEOUT)

        root = config.fileshare.media_root.rstrip("/")
        targets = [f"{root}/{path}".rstrip("/")] if path else [f"{root}/tv", f"{root}/movies"]

        cmd = "python3 /usr/local/bin/survey_audio_lang.py --json " + " ".join(shlex.quote(t) for t in targets)
        try:
            raw = await ssh_run(
                config.fileshare.host, cmd,
                key=config.fileshare.ssh_key,
                timeout=timeout,
            )
        except Exception as e:
            return {"error": str(e)}

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse survey output: {e}", "raw_preview": raw[:300]}

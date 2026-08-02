import re

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run

# The full set of Solr collections musicbrainz-docker indexes. Kept as a
# constant rather than discovered at runtime so a single SSH round-trip can
# check them all (see architecture-review.md-style ===SECTION=== batching
# used elsewhere in this package).
_COLLECTIONS = [
    "annotation", "area", "artist", "cdstub", "event", "instrument", "label",
    "place", "recording", "release", "release-group", "series", "tag", "url", "work",
]

_SEARCH_CONTAINER = "musicbrainz-docker-search-1"

# The [s] regex trick avoids pgrep matching its own invoking shell: ssh always
# runs the remote command via a shell wrapper, and that wrapper's own cmdline
# contains this pattern's literal text — pgrep -f would otherwise "detect" a
# reindex running when there is none, permanently poisoning both the status
# check and the already-running guard in reindex_start.
_PGREP_PATTERN = "[s]ir reindex"


async def _ssh(cmd: str, timeout: int = 15) -> str:
    return await ssh_run(config.musicbrainz.host, cmd, key=config.musicbrainz.ssh_key, timeout=timeout)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def musicbrainz_reindex_status() -> dict:
        """Status of the MusicBrainz Solr search index rebuild (the 'sir reindex' job).

        Reports whether a rebuild is currently running, what it's importing
        right now, the tail of its log, and a document count per Solr
        collection so an apparently-successful run can be told apart from
        one that silently left collections empty (see the 2026-08-01
        incident: a backup-restore path reported success while restoring
        nothing, discovered only by checking numFound directly).
        """
        try:
            raw = await _ssh(
                "echo '===RUNNING==='; "
                f"pgrep -af '{_PGREP_PATTERN}' || true; "
                f"echo '===LOG==='; tail -n 20 {config.musicbrainz.reindex_log} 2>&1; "
                "echo '===COUNTS==='; "
                + "; ".join(
                    f"echo -n '{c}:'; "
                    f"docker exec {_SEARCH_CONTAINER} curl -s "
                    # q=*:* reads 0 on this core even with defType=lucene forced — this
                    # schema/handler doesn't special-case it into MatchAllDocsQuery the
                    # normal way. id:* is a real field present on every doc and gives an
                    # accurate count (found 2026-08-02 chasing a false "index is empty" alarm).
                    f"'http://localhost:8983/solr/{c}/select?q=id:*&rows=0' "
                    "| grep -o 'numFound\":[0-9]*' | cut -d: -f2"
                    for c in _COLLECTIONS
                )
            )
        except Exception as e:
            return {"error": str(e)}

        sections: dict[str, list[str]] = {}
        current = None
        for line in raw.splitlines():
            if line.startswith("===") and line.endswith("==="):
                current = line.strip("=")
                sections[current] = []
            elif current:
                sections[current].append(line)

        running = bool("\n".join(sections.get("RUNNING", [])).strip())

        log_lines = sections.get("LOG", [])
        current_step = None
        for line in reversed(log_lines):
            m = re.search(r"(Importing \S+|Checking whether.*supported)", line)
            if m:
                current_step = m.group(1)
                break

        counts: dict[str, int | None] = {}
        for line in sections.get("COUNTS", []):
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            name = name.strip()
            value = value.strip()
            counts[name] = int(value) if value.isdigit() else None

        empty = [c for c in _COLLECTIONS if counts.get(c) == 0]

        return {
            "running": running,
            "current_step": current_step,
            "log_tail": "\n".join(log_lines[-10:]),
            "collection_counts": counts,
            "empty_collections": empty,
            "all_populated": bool(counts) and not empty and all(c in counts for c in _COLLECTIONS),
        }

    @mcp.tool()
    async def musicbrainz_reindex_start() -> dict:
        """Start a full MusicBrainz Solr search index rebuild ('sir reindex') in the background.

        Rebuilds every collection from the local Postgres database (not the
        MetaBrainz-hosted backup archives — those silently restore empty in
        this environment as of 2026-08-01, see musicbrainz_reindex_status's
        docstring). This can take hours; the job is launched detached
        (setsid+nohup+disown) so it keeps running after this call returns
        and independent of any client connection. Poll musicbrainz_reindex_status
        for progress. Refuses to start a second run if one is already active.
        """
        try:
            status_raw = await _ssh(f"pgrep -af '{_PGREP_PATTERN}' || true")
        except Exception as e:
            return {"error": str(e)}
        if status_raw.strip():
            return {"error": "a reindex is already running", "started": False}

        launch_cmd = (
            f"cd {config.musicbrainz.compose_dir} && "
            f"echo \"=== reindex started via nest-mcp $(date) ===\" >> {config.musicbrainz.reindex_log} && "
            f"setsid nohup sudo -u {config.musicbrainz.compose_user} "
            f"/usr/bin/docker compose exec -T indexer python -m sir reindex "
            f">> {config.musicbrainz.reindex_log} 2>&1 < /dev/null & disown"
        )
        try:
            await _ssh(launch_cmd, timeout=10)
        except Exception as e:
            return {"error": str(e), "started": False}

        return {"started": True, "note": "poll musicbrainz_reindex_status for progress"}

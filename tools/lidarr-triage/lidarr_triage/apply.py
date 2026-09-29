"""Act on approved PRUNE rows. Dry-run unless `execute=True`.

Per album, strictly in this order — any failure aborts that album:
  1. re-fetch; refuse if its track files changed since the report
  2. unmonitor the album
  3. re-read and verify it is unmonitored
  4. delete only the non-kept track files, one by one, via the Lidarr API
     (so the Recycling Bin catches them)

Deleting before unmonitoring would make Lidarr see a monitored album with
missing tracks and re-download it.

Preconditions for execute: Recycling Bin configured, a Lidarr backup newer
than `max_backup_age`, and no enabled import list with "monitor existing"
set (it would re-monitor albums we just unmonitored).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .lidarr import Lidarr, LidarrError, quality_name

APPROVED = {"y", "yes", "x", "true", "1", "approved"}


class PreconditionError(RuntimeError):
    pass


@dataclass
class Row:
    album_id: int
    artist: str
    album: str
    keep_track_ids: set[int]
    snapshot: set[int]


@dataclass
class Outcome:
    done: list[int] = field(default_factory=list)
    aborted: dict[int, str] = field(default_factory=dict)
    planned: dict[int, list[int]] = field(default_factory=dict)  # dry run: album -> trackfile ids
    remonitored: list[int] = field(default_factory=list)
    import_lists: list[dict] = field(default_factory=list)


def _int_set(value: str) -> set[int]:
    return {int(v) for v in (value or "").replace(",", ";").split(";") if v.strip()}


def read_approved(path: Path) -> list[Row]:
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if (r.get("approved") or "").strip().lower() not in APPROVED:
                continue
            keep = _int_set(r.get("keep_track_ids", ""))
            if not keep:
                raise ValueError(f"album {r.get('album_id')}: approved with empty keep_track_ids")
            rows.append(Row(int(r["album_id"]), r.get("artist", ""), r.get("album", ""),
                            keep, _int_set(r.get("trackfile_snapshot", ""))))
    return rows


class RunLog:
    """Append-only JSONL: enough to reconstruct and undo every change."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def write(self, event: str, **data) -> None:
        rec = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _list_summary(lists: list[dict]) -> list[dict]:
    return [{"name": i.get("name"), "enableAutomaticAdd": i.get("enableAutomaticAdd"),
             "shouldMonitor": i.get("shouldMonitor"),
             "shouldMonitorExisting": i.get("shouldMonitorExisting")} for i in lists]


def check_preconditions(lidarr: Lidarr, *, max_backup_age: timedelta | None,
                        allow_monitor_existing: bool = False,
                        now: datetime | None = None) -> dict:
    mm = lidarr.media_management()
    if not (mm.get("recycleBin") or "").strip():
        raise PreconditionError(
            "Lidarr Recycling Bin is not set: deletes would be permanent and "
            "Tank/media_root has no backups. Set it first (docs/music-lidarr-safety.md).")
    info = {"recycleBin": mm["recycleBin"], "recycleBinCleanupDays": mm.get("recycleBinCleanupDays")}
    if max_backup_age is not None:
        newest = lidarr.newest_backup_time()
        now = now or datetime.now(timezone.utc)
        if newest is None or now - newest > max_backup_age:
            raise PreconditionError(
                f"newest Lidarr backup is {newest or 'missing'}; take one first "
                "(POST /api/v1/command {\"name\":\"Backup\"}, docs/music-lidarr-safety.md)")
        info["newestBackup"] = newest.isoformat()
    lists = _list_summary(lidarr.import_lists())
    risky = [i["name"] for i in lists if i["enableAutomaticAdd"] and i["shouldMonitorExisting"]]
    if risky and not allow_monitor_existing:
        raise PreconditionError(
            f"import list(s) {risky} have 'Monitor Existing' enabled and would re-monitor "
            "albums after pruning. Turn that off, or pass --allow-monitor-existing-lists.")
    info["importLists"] = lists
    return info


def _plan(lidarr: Lidarr, row: Row) -> tuple[list[dict], dict[int, dict]]:
    """Return (trackfiles to delete, all current trackfiles by id). Raises on any mismatch."""
    files = {f["id"]: f for f in lidarr.trackfiles(album_id=row.album_id)}
    if row.snapshot and set(files) != row.snapshot:
        raise LidarrError(f"track files changed since report (was {sorted(row.snapshot)}, now {sorted(files)})")
    tracks = {t["id"]: t for t in lidarr.tracks(album_id=row.album_id)}
    missing = row.keep_track_ids - set(tracks)
    if missing:
        raise LidarrError(f"keep_track_ids {sorted(missing)} are not tracks of this album")
    no_file = [i for i in row.keep_track_ids if not tracks[i].get("trackFileId")]
    if no_file:
        raise LidarrError(f"kept track(s) {no_file} have no file; refusing to delete the rest")
    keep_files = {tracks[i]["trackFileId"] for i in row.keep_track_ids}
    delete = [f for fid, f in sorted(files.items()) if fid not in keep_files]
    if len(delete) >= len(files):
        raise LidarrError("plan would delete every track file")
    return delete, files


def run(lidarr: Lidarr, rows: list[Row], *, execute: bool, log: RunLog | None,
        max_backup_age: timedelta | None = timedelta(hours=1),
        allow_monitor_existing: bool = False) -> Outcome:
    out = Outcome()
    if execute:
        if log is None:
            raise ValueError("execute requires a run log")
        info = check_preconditions(lidarr, max_backup_age=max_backup_age,
                                   allow_monitor_existing=allow_monitor_existing)
        status = lidarr.status()
        log.write("start", lidarr_version=status.get("version"), albums=[r.album_id for r in rows], **info)

    for row in rows:
        try:
            delete, files = _plan(lidarr, row)
        except LidarrError as e:
            out.aborted[row.album_id] = f"plan: {e}"
            if log:
                log.write("abort", album_id=row.album_id, stage="plan", error=str(e))
            continue
        if not execute:
            out.planned[row.album_id] = [f["id"] for f in delete]
            continue

        album = lidarr.album(row.album_id)
        log.write("album", album_id=row.album_id, album_mbid=album.get("foreignAlbumId"),
                  artist_id=album.get("artistId"),
                  artist_mbid=(album.get("artist") or {}).get("foreignArtistId"),
                  artist=row.artist, title=album.get("title"), was_monitored=album.get("monitored"),
                  keep_track_ids=sorted(row.keep_track_ids),
                  trackfiles=[_file_record(f) for f in files.values()])
        try:
            lidarr.set_album_monitored(row.album_id, False)
            log.write("unmonitor", album_id=row.album_id)
            if lidarr.album(row.album_id).get("monitored") is not False:
                raise LidarrError("album still monitored after PUT /album/monitor")
            log.write("verified-unmonitored", album_id=row.album_id)
        except LidarrError as e:
            out.aborted[row.album_id] = f"unmonitor: {e}"
            log.write("abort", album_id=row.album_id, stage="unmonitor", error=str(e))
            continue

        try:
            for f in delete:
                lidarr.delete_trackfile(f["id"])
                log.write("delete", album_id=row.album_id, **_file_record(f))
        except LidarrError as e:
            out.aborted[row.album_id] = f"delete: {e}"
            log.write("abort", album_id=row.album_id, stage="delete", error=str(e))
            continue
        out.done.append(row.album_id)

    if execute:
        _post_checks(lidarr, out, log)
    return out


def _file_record(f: dict) -> dict:
    return {"trackfile_id": f["id"], "path": f.get("path"), "size": f.get("size"),
            "quality": quality_name(f), "date_added": f.get("dateAdded")}


def _post_checks(lidarr: Lidarr, out: Outcome, log: RunLog) -> None:
    out.remonitored = [a for a in out.done if lidarr.album(a).get("monitored") is not False]
    out.import_lists = _list_summary(lidarr.import_lists())
    log.write("finish", done=out.done, aborted=out.aborted, remonitored=out.remonitored,
              import_lists=out.import_lists)

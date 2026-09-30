"""lidarr-triage command line.

  lidarr-triage report --history DIR [--out reports/]      read-only
  lidarr-triage quality-report [--from prune.csv]          read-only
  lidarr-triage apply prune.csv                            dry run (read-only)
  lidarr-triage apply prune.csv --apply                    unmonitor + delete

Needs LIDARR_API_KEY in the environment (and LIDARR_URL unless the LAN LB
default is right). See README.md.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import apply as apply_mod
from . import quality
from .buckets import decide
from .history import HistoryFormatError, load_history
from .library import load_library
from .lidarr import VERIFIED_AGAINST, Lidarr, LidarrError
from .match import match_plays
from .report import write_reports
from .score import ScoreConfig

HERE = Path(__file__).resolve().parent.parent


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _check_version(lidarr: Lidarr) -> str:
    version = lidarr.status().get("version", "?")
    if version.split(".")[:2] != VERIFIED_AGAINST.split(".")[:2]:
        print(f"warning: Lidarr {version}; endpoints were verified against {VERIFIED_AGAINST}. "
              "Re-check lidarr.py against the live API spec.", file=sys.stderr)
    return version


def cmd_report(args) -> int:
    cfg = ScoreConfig(min_ms=args.min_ms, half_life_years=args.half_life,
                      passive_weight=args.passive_weight, min_track_score=args.min_track_score,
                      keep_min_tracks=args.keep_min_tracks)
    plays, stats = load_history(args.history)
    real = [p for p in plays if p.is_real(cfg.min_ms)]
    print(f"history: {stats.files} files, {stats.records} records, {stats.plays} track plays "
          f"({stats.non_track} non-track dropped), {len(real)} real (not skipped, >= {cfg.min_ms // 1000}s)")
    print(f"         {stats.first:%Y-%m-%d} .. {stats.last:%Y-%m-%d}; skipped=null in {stats.skipped_null} records")

    lidarr = Lidarr(args.url)
    version = _check_version(lidarr)
    library = load_library(lidarr, progress=lambda n, t: print(f"\rlidarr: artist {n}/{t}", end="", file=sys.stderr))
    print(file=sys.stderr)
    matches = match_plays(real, library, threshold=args.threshold, review_floor=args.review_floor)
    decisions = decide(library, matches, cfg)

    header = [
        f"- Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC against Lidarr {version}",
        f"- History {stats.first:%Y-%m-%d} .. {stats.last:%Y-%m-%d}: {len(real)} real plays of {stats.plays}",
        f"- Match threshold {args.threshold}, review floor {args.review_floor}; half-life {cfg.half_life_years}y, "
        f"passive weight {cfg.passive_weight}, track counts at score >= {cfg.min_track_score}, "
        f"KEEP at >= {cfg.keep_min_tracks} tracks",
    ]
    paths = write_reports(args.out, decisions, matches, header)
    counts = Counter(d.bucket for d in decisions)
    print("buckets: " + ", ".join(f"{b} {counts.get(b, 0)}" for b in ("KEEP", "PRUNE", "REVIEW", "HOLD")))
    print(f"report:  {paths['summary']}")
    return 0


def cmd_quality(args) -> int:
    lidarr = Lidarr(args.url)
    _check_version(lidarr)
    rows = quality.from_report(lidarr, args.from_csv) if args.from_csv else quality.from_unmonitored(lidarr)
    out = args.out / "non-flac-kept.csv"
    quality.write(rows, out)
    print(f"{len(rows)} non-FLAC kept track(s) in {len({r['album_id'] for r in rows})} album(s) -> {out}")
    return 0


def cmd_apply(args) -> int:
    rows = apply_mod.read_approved(args.reviewed)
    if not rows:
        _err(f"no rows marked approved in {args.reviewed}")
        return 2
    lidarr = Lidarr(args.url)
    _check_version(lidarr)
    log = None
    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log = apply_mod.RunLog(args.runs / f"apply-{stamp}.jsonl")
        print(f"logging to {log.path}")
    try:
        out = apply_mod.run(lidarr, rows, execute=args.apply, log=log,
                            max_backup_age=None if args.skip_backup_check else timedelta(minutes=args.max_backup_age),
                            allow_monitor_existing=args.allow_monitor_existing_lists)
    except apply_mod.PreconditionError as e:
        _err(str(e))
        return 3
    finally:
        if log:
            log.close()

    if not args.apply:
        for r in rows:
            if r.album_id in out.planned:
                print(f"would unmonitor + delete {len(out.planned[r.album_id])} file(s): {r.artist} — {r.album}")
        print("dry run: nothing changed. Re-run with --apply to act.")
    else:
        print(f"done: {len(out.done)} album(s)")
        if out.remonitored:
            print(f"WARNING: re-monitored after processing: {out.remonitored}")
        enabled = [i["name"] for i in out.import_lists if i["enableAutomaticAdd"]]
        if enabled:
            print(f"note: enabled import lists that can re-add/monitor albums: {enabled}")
    for album_id, why in out.aborted.items():
        print(f"ABORTED album {album_id}: {why}")
    return 1 if out.aborted or out.remonitored else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lidarr-triage", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="Lidarr base URL (default: $LIDARR_URL or the LAN LB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="score the library against Spotify history (read-only)")
    r.add_argument("--history", type=Path, required=True, help="directory with Streaming_History_Audio_*.json")
    r.add_argument("--out", type=Path, default=HERE / "reports")
    r.add_argument("--threshold", type=float, default=90.0, help="match confidence to count a play (0-100)")
    r.add_argument("--review-floor", type=float, default=70.0, help="below this, a match is ignored entirely")
    r.add_argument("--min-ms", type=int, default=30_000, help="shortest real play, ms")
    r.add_argument("--half-life", type=float, default=3.0, help="years for a play's weight to halve")
    r.add_argument("--passive-weight", type=float, default=0.5, help="weight of non-intentional starts")
    r.add_argument("--min-track-score", type=float, default=0.5, help="score for a track to count as played")
    r.add_argument("--keep-min-tracks", type=int, default=3, help="counting tracks needed for KEEP")
    r.set_defaults(func=cmd_report)

    q = sub.add_parser("quality-report", help="kept tracks that aren't FLAC (read-only)")
    q.add_argument("--from", dest="from_csv", type=Path, help="prune.csv to preview instead of unmonitored albums")
    q.add_argument("--out", type=Path, default=HERE / "reports")
    q.set_defaults(func=cmd_quality)

    a = sub.add_parser("apply", help="act on approved rows of a reviewed prune.csv (dry run unless --apply)")
    a.add_argument("reviewed", type=Path)
    a.add_argument("--apply", action="store_true", help="actually unmonitor and delete")
    a.add_argument("--runs", type=Path, default=HERE / "runs", help="where the JSONL action log goes")
    a.add_argument("--max-backup-age", type=int, default=60, help="minutes; newest Lidarr backup must be newer")
    a.add_argument("--skip-backup-check", action="store_true")
    a.add_argument("--allow-monitor-existing-lists", action="store_true",
                   help="proceed although an import list has 'Monitor Existing' enabled")
    a.set_defaults(func=cmd_apply)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (HistoryFormatError, LidarrError, ValueError) as e:
        _err(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())

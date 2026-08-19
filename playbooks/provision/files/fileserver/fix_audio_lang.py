#!/usr/bin/env python3
"""Set English as the default audio track wherever it exists but isn't default.

Lossless: uses mkvpropedit to flip container-level disposition flags only —
no re-encode, no re-mux, streams untouched. Only Matroska (.mkv) files are
eligible; other containers are reported as skipped_unsupported_container.

Defaults to a dry run (report what would change, touch nothing). Pass --apply
to actually run mkvpropedit.

Deployed to the fileserver LXC via playbooks/provision/fileserver.yml; invoked
either by hand or by nest-mcp's media_audio_lang_fix tool (--json mode).

Usage:
  fix_audio_lang.py /mnt/media_root/media/tv                  # dry run
  fix_audio_lang.py --apply /mnt/media_root/media/tv/Atlanta  # actually fix
  fix_audio_lang.py --json --apply /mnt/media_root/media/tv
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from audio_lang_common import MKVPROPEDIT_EXTS, VIDEO_EXTS, audio_track_edits, classify, ffprobe_streams

_EXAMPLES_PER_BUCKET = 20


def mkvpropedit_args(edits: list[tuple[int, int]]) -> list[str]:
    args = []
    for track_num, want_default in edits:
        args += ["--edit", f"track:a{track_num}", "--set", f"flag-default={want_default}"]
    return args


def run_fix(roots: list[Path], apply: bool, progress=lambda done, total: None):
    files = []
    for root in roots:
        if root.is_dir():
            files.extend(p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS)

    buckets: dict[str, list[dict]] = {
        k: [] for k in ("fixed", "would_fix", "already_ok", "skipped_unsupported_container", "skipped_no_eng", "error")
    }
    for i, f in enumerate(files, 1):
        try:
            streams = ffprobe_streams(f)
            status, detail = classify(streams)

            if status != "needs_fix":
                bucket = "already_ok" if status == "ok" else "skipped_no_eng"
                buckets[bucket].append({"path": str(f), "detail": detail})
                progress(i, len(files))
                continue

            if f.suffix.lower() not in MKVPROPEDIT_EXTS:
                buckets["skipped_unsupported_container"].append({"path": str(f), "detail": detail})
                progress(i, len(files))
                continue

            edits = audio_track_edits(streams)
            if not edits:
                buckets["already_ok"].append({"path": str(f), "detail": detail})
                progress(i, len(files))
                continue

            if not apply:
                buckets["would_fix"].append({"path": str(f), "detail": f"default was {detail}"})
                progress(i, len(files))
                continue

            proc = subprocess.run(
                ["mkvpropedit", str(f), *mkvpropedit_args(edits)],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                buckets["error"].append({"path": str(f), "detail": proc.stderr.strip()[:200]})
            else:
                buckets["fixed"].append({"path": str(f), "detail": f"default was {detail}"})
        except Exception as e:
            buckets["error"].append({"path": str(f), "detail": str(e)})
        progress(i, len(files))

    return len(files), buckets


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("roots", nargs="+", type=Path, help="Directories to scan recursively")
    parser.add_argument("--apply", action="store_true", help="Actually run mkvpropedit (default: dry run)")
    parser.add_argument("--json", action="store_true", help="Emit a single JSON summary to stdout instead of a human report")
    args = parser.parse_args()

    def progress(done, total):
        if not args.json and (done % 100 == 0 or done == total):
            print(f"  {done}/{total}", file=sys.stderr)

    if not args.json:
        mode = "APPLYING FIXES" if args.apply else "DRY RUN"
        print(f"[{mode}] Scanning {', '.join(str(r) for r in args.roots)}...", file=sys.stderr)

    total, buckets = run_fix(args.roots, args.apply, progress)

    if args.json:
        summary = {
            "total_files": total,
            "dry_run": not args.apply,
            "counts": {k: len(v) for k, v in buckets.items()},
            "examples": {k: v[:_EXAMPLES_PER_BUCKET] for k, v in buckets.items() if v},
        }
        print(json.dumps(summary))
        return

    print(f"\n--- Summary ({'dry run' if not args.apply else 'applied'}) ---")
    for status in ("fixed", "would_fix", "already_ok", "skipped_unsupported_container", "skipped_no_eng", "error"):
        print(f"{status:30} {len(buckets[status])}")

    changed = buckets["fixed"] or buckets["would_fix"]
    if changed:
        label = "fixed" if args.apply else "would fix"
        print(f"\nFirst 20 {label}:")
        for entry in changed[:_EXAMPLES_PER_BUCKET]:
            print(f"  [{entry['detail']}] {entry['path']}")


if __name__ == "__main__":
    main()

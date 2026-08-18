#!/usr/bin/env python3
"""Survey video files for English audio tracks that aren't the default stream.

Read-only. Walks a media tree, runs ffprobe on each video file, and buckets it into:
  - ok            a stream tagged language=eng is already the default audio stream
  - needs_fix     an eng-tagged stream exists but a different stream is default
                  (fixable losslessly with mkvpropedit, no re-encode)
  - no_eng_tag    no audio stream is tagged eng at all (needs manual inspection —
                  English audio may exist but be untagged, or may genuinely be missing)
  - no_audio      ffprobe found no audio streams
  - error         ffprobe failed to read the file

Deployed to the fileserver LXC via playbooks/provision/fileserver.yml; invoked
either by hand or by nest-mcp's media_audio_lang_survey tool (--json mode).

Usage:
  survey_audio_lang.py /mnt/media_root/media/tv
  survey_audio_lang.py --json /mnt/media_root/media/tv /mnt/media_root/media/movies
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi"}
_EXAMPLES_PER_BUCKET = 20


def ffprobe_streams(path: Path):
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries", "stream=index,codec_type:stream_tags=language:disposition=default",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200])
    return json.loads(proc.stdout).get("streams", [])


def classify(streams):
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        return "no_audio", ""

    eng_streams = [s for s in audio if s.get("tags", {}).get("language", "").lower() in ("eng", "en")]
    if not eng_streams:
        langs = sorted({s.get("tags", {}).get("language", "und") for s in audio})
        return "no_eng_tag", ",".join(langs)

    default_stream = next((s for s in audio if s.get("disposition", {}).get("default") == 1), audio[0])
    default_lang = default_stream.get("tags", {}).get("language", "").lower()
    if default_lang in ("eng", "en"):
        return "ok", ""
    return "needs_fix", default_lang or "und"


def run_survey(roots: list[Path], progress=lambda done, total: None):
    files = []
    for root in roots:
        if root.is_dir():
            files.extend(p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS)

    buckets: dict[str, list[dict]] = {k: [] for k in ("ok", "needs_fix", "no_eng_tag", "no_audio", "error")}
    for i, f in enumerate(files, 1):
        try:
            status, detail = classify(ffprobe_streams(f))
        except Exception as e:
            status, detail = "error", str(e)
        buckets[status].append({"path": str(f), "detail": detail})
        progress(i, len(files))

    return len(files), buckets


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("roots", nargs="+", type=Path, help="Directories to scan recursively")
    parser.add_argument("--json", action="store_true", help="Emit a single JSON summary to stdout instead of a human report")
    args = parser.parse_args()

    def progress(done, total):
        if not args.json and (done % 100 == 0 or done == total):
            print(f"  {done}/{total}", file=sys.stderr)

    if not args.json:
        print(f"Scanning {', '.join(str(r) for r in args.roots)}...", file=sys.stderr)

    total, buckets = run_survey(args.roots, progress)

    if args.json:
        summary = {
            "total_files": total,
            "counts": {k: len(v) for k, v in buckets.items()},
            "examples": {k: v[:_EXAMPLES_PER_BUCKET] for k, v in buckets.items() if v},
        }
        print(json.dumps(summary))
        return

    print("\n--- Summary ---")
    for status in ("ok", "needs_fix", "no_eng_tag", "no_audio", "error"):
        print(f"{status:12} {len(buckets[status])}")

    if buckets["needs_fix"]:
        print("\nFirst 20 needs_fix examples:")
        for entry in buckets["needs_fix"][:_EXAMPLES_PER_BUCKET]:
            print(f"  [default={entry['detail']}] {entry['path']}")


if __name__ == "__main__":
    main()

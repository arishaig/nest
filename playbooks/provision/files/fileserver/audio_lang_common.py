"""Shared ffprobe/classification logic for survey_audio_lang.py and fix_audio_lang.py.

Deployed alongside both scripts to the fileserver LXC by
playbooks/provision/fileserver.yml.
"""
import json
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi"}
MKVPROPEDIT_EXTS = {".mkv"}  # mkvpropedit only supports Matroska containers


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
    """Bucket a file's audio streams: ok / needs_fix / no_eng_tag / no_audio."""
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


def audio_track_edits(streams):
    """For a needs_fix file, compute the mkvpropedit track edits to make English default.

    Returns a list of (track_number, want_default) pairs, 1-based within the
    audio tracks only (mkvpropedit's `track:aN` addressing) — or None if the
    file isn't eligible (no eng-tagged audio stream at all).
    """
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    eng_idx = next(
        (i for i, s in enumerate(audio) if s.get("tags", {}).get("language", "").lower() in ("eng", "en")),
        None,
    )
    if eng_idx is None:
        return None

    currently_default = [i for i, s in enumerate(audio) if s.get("disposition", {}).get("default") == 1]
    if currently_default == [eng_idx]:
        return []  # already correct

    return [(i + 1, 1 if i == eng_idx else 0) for i in range(len(audio))]

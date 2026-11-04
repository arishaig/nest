#!/usr/bin/env python3
"""pluto-proxy: M3U playlist and normalising HLS proxy for Pluto TV's free channels.

Pluto's stream URLs need a short-lived session token from its own (unofficial)
boot API, so there is no static playlist. This serves:

  /playlist.m3u            channels as an M3U; tvg-id is Pluto's channel id,
                           which is also the id in i.mjh.nz's PlutoTV/us.xml guide
  /ch/<id>.m3u8            a one-variant master (best quality) whose media
                           playlist is served from here
  /s/<key>/v.m3u8          Pluto's media playlist with every segment pointed
                           back here
  /s/<key>/v/<seq>.ts      one segment, normalised (below); where Pluto's
                           original audio is a separate rendition (about half
                           the channels) it is merged into each segment
  /healthz                 200 once a session and channel list are loaded

Why every segment goes through here: Pluto's segments do not share a stream
layout. Shows, ads and the repackaged ads all use different PIDs, and some carry
their own audio or an ID3 track. An ffmpeg remuxer (Dispatcharr's stream
profile) fixes its output streams at startup, so when the layout changes the
streams it mapped starve and the channel stalls at every ad break. Each segment
is therefore decrypted and copy-remuxed into one fixed layout: video on PID
0x100, audio on 0x101, nothing else, timestamps unchanged. Ad segments
(AD_RE: ad creatives and Pluto's ad-break bumpers) are replaced with a black, silent segment of the
same length, at the same timestamps, or with a slice of a clip from
BREAKS_DIR (one picked per break, looped). Channels in CC_CHANNELS also get
Pluto's subtitles as closed captions in the video (add_captions; needs
GStreamer's gst-launch-1.0, skipped without it).

Needs ffmpeg and ffprobe on PATH; without them /ch falls back to pointing the
client straight at Pluto (no normalising). Usage and caveats: docs/live-tv.md.
"""
import collections
import datetime
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "Mozilla/5.0"
BOOT_URL = "https://boot.pluto.tv/v4/start"
REFRESH = int(os.environ.get("REFRESH_SECONDS", "21600"))  # tokens last 24h
PORT = int(os.environ.get("PORT", "8181"))
MAX_BANDWIDTH = int(os.environ.get("MAX_BANDWIDTH", "0"))  # bits/s; 0 = best available
STREAM_TTL = int(os.environ.get("STREAM_TTL_SECONDS", "300"))  # idle streams are forgotten
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
CLIENT_ID = str(uuid.uuid4())
ID_RE = re.compile(r"^[0-9a-f]{24}$")
# Ads: raw creatives (`.../N_ad/creative/<id>_ad/...` or
# `.../creative/N_ad/<id>_ad/...`), creatives Pluto repackages as TS on
# the fly (`/mp4/v1/...id3(p=clik,...)`: about 3 in 10 ad segments, and the
# path then has no `_ad/creative/`), and Pluto's "ad break" bumper animations.
# Surveyed 2026-11-04 over all 456 channels: every repackaged segment carried
# p=clik and was an ad; shows come from /live/ or plain .../clip/ HLS.
AD_RE = re.compile(r"/creative/|[0-9a-f]{24}_ad/|id3\(p=clik[,)]|ad_?bumper", re.I)
VIDEO_PID, AUDIO_PID = 0x100, 0x101
# Used for a slate when neither the show nor the ad says otherwise.
DEFAULT_VIDEO = {"width": 1280, "height": 720, "fps": "30/1"}
DEFAULT_AUDIO = {"sample_rate": 48000, "channels": 2}
TIMELINE_START = 10.0  # output seconds at the first segment's date
RUN_MATCH = 2.0        # s: video and audio runs this close in date are the same run
DEBUG = os.environ.get("PLUTO_DEBUG") == "1"  # log every segment's placement
SEGMENTS_KEPT = 60     # per track: a few playlist windows
BUILT_KEPT = 6         # per track: finished segments kept for retried and built-ahead requests (~2 MB each)
# Ad breaks are filled from video clips in this folder (read-only), one
# picked at random per break, played from its start and looped; empty or
# unset means black. See docs/live-tv.md for how to add clips.
# Closed captions (see add_captions): channels listed in CC_CHANNELS (Pluto
# channel ids, comma-separated, or "*" for all) get Pluto's subtitle track
# as CEA-608 captions in the video, which players show as a CC track.
CC_CHANNELS = {c.strip() for c in os.environ.get("CC_CHANNELS", "").split(",") if c.strip()}
GST_LAUNCH = os.environ.get("GST_LAUNCH", "gst-launch-1.0")
# Channels (same form) that also get a "(CC)" twin in the playlist, with
# the captions drawn into the picture (burn_captions): for players that
# can't show CEA-608 or (Jellyfin live TV) are never offered a caption track.
CC_BURN_CHANNELS = {c.strip() for c in os.environ.get("CC_BURN_CHANNELS", "").split(",") if c.strip()}
BURN_TIMEOUT = 15      # s: a show segment taking longer goes out without captions drawn
BURN_DIR = os.environ.get("BURN_DIR", "/dev/shm")  # caption files for ffmpeg (root fs is read-only)
CC_TIMEOUT = 10        # s: a segment whose captioning takes longer goes out without
CC_GROUP = 0.5         # s: words this close together are sent as one caption
CC_TAIL = 0.5          # s: words due this close to a segment's end go in the next one
CUES_KEPT = 3000       # per stream: Pluto sends a cue per word, ~3/s
BREAKS_DIR = os.environ.get("BREAKS_DIR", "")
BREAKS_RESCAN = int(os.environ.get("BREAKS_RESCAN_SECONDS", "300"))
BREAK_EXTS = (".mp4", ".m4v", ".mkv", ".webm", ".mov", ".ts")
BREAK_SETTLE = 120     # s: files modified more recently may still be being written
CLIP_TIMEOUT = 12      # s: a clip segment taking longer falls back to black

_lock = threading.Lock()
_state = {"session": None, "channels": None, "at": 0.0}


def fetch_json(url, token=None):
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return json.load(r)


def fetch_bytes(url, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
        return r.read(), r.geturl()


def boot(client_id=None):
    """A Pluto session. Pluto plays one stream per session: opening a second
    one ends the first (its playlist turns empty with EXT-X-ENDLIST), so every
    stream gets its own session and client id (open_channel). CLIENT_ID's
    session is only used for the channel list."""
    q = urllib.parse.urlencode({
        "appName": "web", "appVersion": "9.0.0", "deviceVersion": "120.0",
        "deviceModel": "web", "deviceMake": "chrome", "deviceType": "web",
        "clientID": client_id or CLIENT_ID, "clientModelNumber": "1.0.0",
        "serverSideAds": "false",
    })
    b = fetch_json(BOOT_URL + "?" + q)
    return {
        "token": b["sessionToken"],
        "params": b["stitcherParams"],
        "stitcher": b["servers"]["stitcher"],
        "channels_api": b["servers"]["channels"],
    }


def load_channels(session):
    api, tok = session["channels_api"], session["token"]
    cats = {c["id"]: c["name"] for c in fetch_json(api + "/v2/guide/categories", tok)["data"]}
    out = {}
    for c in fetch_json(api + "/v2/guide/channels?limit=1000", tok)["data"]:
        path = (c.get("stitched") or {}).get("path")
        if not path or c.get("plutoOfficeOnly") or not ID_RE.match(c["id"]):
            continue
        logos = {i.get("type"): i.get("url") for i in c.get("images", [])}
        out[c["id"]] = {
            "name": c["name"],
            "number": c.get("number") or 0,
            "path": path,
            "logo": logos.get("colorLogoPNG") or next(iter(logos.values()), ""),
            "group": next((cats[i] for i in c.get("categoryIDs", []) if i in cats), "Pluto TV"),
        }
    return out


def refresh(force=False):
    """Return (session, channels), refreshing both when stale. Keeps the last
    good copy if Pluto is unreachable."""
    with _lock:
        if force or _state["session"] is None or time.time() - _state["at"] > REFRESH:
            try:
                session = boot()
                _state["channels"] = load_channels(session)
                _state["session"], _state["at"] = session, time.time()
            except Exception as e:  # noqa: BLE001 - serve stale rather than die
                print("refresh failed: %s" % type(e).__name__, flush=True)
                if _state["session"] is None:
                    raise
        return _state["session"], _state["channels"]


def attr(s):
    return str(s).replace('"', "'").replace("\n", " ")


def playlist(base, channels):
    lines = ["#EXTM3U"]
    for cid, c in sorted(channels.items(), key=lambda kv: (kv[1]["number"], kv[1]["name"])):
        lines.append(
            '#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-chno="%s" tvg-logo="%s" group-title="%s",%s'
            % (cid, attr(c["name"]), c["number"], attr(c["logo"]), attr(c["group"]), attr(c["name"]))
        )
        lines.append("%s/ch/%s.m3u8" % (base, cid))
        if "*" in CC_BURN_CHANNELS or cid in CC_BURN_CHANNELS:
            lines.append(
                '#EXTINF:-1 tvg-id="%s" tvg-name="%s (CC)" tvg-chno="%s" tvg-logo="%s" group-title="%s",%s (CC)'
                % (cid, attr(c["name"]), c["number"], attr(c["logo"]), attr(c["group"]), attr(c["name"])))
            lines.append("%s/cc/%s.m3u8" % (base, cid))
    return "\n".join(lines) + "\n"


def stream_url(session, channel):
    return "%s/v2%s?%s&jwt=%s&masterJWTPassthrough=true" % (
        session["stitcher"], channel["path"], session["params"], session["token"])


# --- master playlist ---------------------------------------------------------

def _best_variant(master, max_bw=0):
    """(STREAM-INF line, URI) of the highest-bandwidth variant (within max_bw if
    set), or None if the playlist lists no variants."""
    best = None
    lines = master.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"#EXT-X-STREAM-INF:.*?BANDWIDTH=(\d+)", line)
        if not m or i + 1 >= len(lines):
            continue
        bw, uri = int(m.group(1)), lines[i + 1].strip()
        if not uri or uri.startswith("#") or (max_bw and bw > max_bw):
            continue
        if best is None or bw > best[0]:
            best = (bw, line, uri)
    return best[1:] if best else None


def pick_renditions(master, base_url, max_bw=0):
    """(bandwidth, video playlist URL, audio playlist URL or None) for the best
    variant, or None if the master lists no variants. The audio URL is set when
    the original audio is a separate EXT-X-MEDIA rendition (about half of
    Pluto's channels): the variant alone is then silent. The audio-description
    track is never picked."""
    best = _best_variant(master, max_bw)
    if not best:
        return None
    bw = int(re.search(r"BANDWIDTH=(\d+)", best[0]).group(1))
    video = urllib.parse.urljoin(base_url, best[1])
    group = re.search(r'AUDIO="([^"]+)"', best[0])
    audio = None
    if group:
        media = [l for l in master.splitlines()
                 if l.startswith("#EXT-X-MEDIA:TYPE=AUDIO") and 'GROUP-ID="%s"' % group.group(1) in l
                 and "URI=" in l and "describes-video" not in l]
        if media:
            line = next((l for l in media if "DEFAULT=YES" in l), media[0])
            audio = urllib.parse.urljoin(base_url, re.search(r'URI="([^"]*)"', line).group(1))
    return bw, video, audio


def pick_subtitles(master, base_url):
    """URL of the master's English subtitle rendition (else its first), or
    None if it lists none."""
    media = [l for l in master.splitlines()
             if l.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" in l and 'URI="' in l]
    if not media:
        return None
    line = next((l for l in media if 'LANGUAGE="en' in l or 'NAME="English' in l), media[0])
    return urllib.parse.urljoin(base_url, re.search(r'URI="([^"]*)"', line).group(1))


def master_playlist(key, bandwidth):
    """Always one variant with muxed audio, even where Pluto's audio is a
    separate rendition: the proxy merges it into each segment. With two
    playlists, ffmpeg's HLS reader can starve one of them (it stopped reading
    audio for 90 s through an ad break and then skipped ahead), which showed as
    a black picture over the show's sound and then a freeze."""
    return "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=%d\n/s/%s/v.m3u8\n" % (bandwidth, key)


def fetch_master(url):
    data, final = fetch_bytes(url, timeout=10)
    return data.decode("utf-8", "replace"), final


# --- media playlists ---------------------------------------------------------

HEADER_TAGS = ("#EXT-X-VERSION", "#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE",
               "#EXT-X-DISCONTINUITY-SEQUENCE", "#EXT-X-PLAYLIST-TYPE", "#EXT-X-INDEPENDENT-SEGMENTS")


def parse_pdt(line):
    """'#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:45.299Z' -> epoch seconds."""
    try:
        v = line.split(":", 1)[1].strip().replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(v).timestamp()
    except (IndexError, ValueError):
        return None


def parse_media(text, base_url):
    """(header lines, segments, endlist). Each segment is a dict: seq, uri
    (absolute), extinf (the duration as written), dur, key ((uri, iv hex) or
    None), ad, disc, pdt (the PROGRAM-DATE-TIME line or None), dseq (its
    discontinuity sequence number) and clock (its PROGRAM-DATE-TIME in epoch
    seconds, carried forward by EXTINF when not stated; None if the playlist
    has none). Raises ValueError for encryption other than AES-128."""
    headers, segs, endlist = [], [], False
    seq0, dseq, clock, key, pending = 0, 0, None, None, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith(HEADER_TAGS):
            headers.append(line)
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                seq0 = int(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                dseq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            method = re.search(r"METHOD=([^,]+)", line).group(1)
            if method == "NONE":
                key = None
            elif method == "AES-128":
                iv = re.search(r"IV=0[xX]([0-9a-fA-F]+)", line)
                key = (urllib.parse.urljoin(base_url, re.search(r'URI="([^"]+)"', line).group(1)),
                       iv.group(1).rjust(32, "0") if iv else None)
            else:
                raise ValueError("unsupported encryption " + method)
        elif line == "#EXT-X-DISCONTINUITY":
            pending["disc"] = True
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending["pdt"] = line
        elif line.startswith("#EXTINF:"):
            pending["extinf"] = line.split(":", 1)[1].split(",", 1)[0]
        elif line == "#EXT-X-ENDLIST":
            endlist = True
        elif line.startswith("#"):
            continue  # #PLUTO-* and other tags we don't pass on
        else:
            seq = seq0 + len(segs)
            uri = urllib.parse.urljoin(base_url, line)
            extinf = pending.get("extinf", "0")
            if pending.get("disc"):
                dseq += 1
            stated = parse_pdt(pending["pdt"]) if pending.get("pdt") else None
            if stated is not None:
                clock = stated
            segs.append({
                "seq": seq, "uri": uri, "extinf": extinf, "dur": float(extinf),
                "key": (key[0], key[1] or "%032x" % seq) if key else None,
                "ad": bool(AD_RE.search(urllib.parse.unquote(uri))),
                "disc": pending.get("disc", False), "pdt": pending.get("pdt"),
                "dseq": dseq, "clock": clock,
            })
            if clock is not None:
                clock += float(extinf)
            pending = {}
    return headers, segs, endlist


def render_media(headers, segs, prefix, endlist):
    """Media playlist with every segment at <prefix>/<seq>.ts. The output is
    unencrypted, so the EXT-X-KEY lines are dropped."""
    out = ["#EXTM3U"] + headers
    for s in segs:
        if s["disc"]:
            out.append("#EXT-X-DISCONTINUITY")
        if s["pdt"]:
            out.append(s["pdt"])
        out.append("#EXTINF:%s," % s["extinf"])
        out.append("%s/%d.ts" % (prefix, s["seq"]))
    if endlist:
        out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


# --- segments ----------------------------------------------------------------

def _fps(rate):
    """'30/1' -> 30.0; anything unusable -> None."""
    try:
        n, d = (rate or "").split("/")
        v = float(n) / float(d)
        return v if 1 <= v <= 120 else None
    except (ValueError, ZeroDivisionError):
        return None


def source_args(key, fd=0):
    """(input options, input URL) for a segment arriving on file descriptor
    fd (stdin by default), decrypted by ffmpeg's crypto protocol when the
    segment is AES-128."""
    if key:
        return ["-key", key[0], "-iv", key[1]], "crypto:pipe:%d" % fd
    return [], "pipe:%d" % fd


def parse_probe(out):
    """ffprobe JSON -> {'v': {...}, 'a': {...}} for the first video and audio
    stream (each with 'start'), plus 'start' for the earliest one."""
    info = {}
    for st in json.loads(out or "{}").get("streams", []):
        t = {"video": "v", "audio": "a"}.get(st.get("codec_type"))
        if not t or t in info:
            continue
        try:
            start = float(st.get("start_time"))
        except (TypeError, ValueError):
            continue
        try:
            length = float(st.get("duration"))
        except (TypeError, ValueError):
            length = 0.0
        if t == "v":
            if not st.get("width") or not st.get("height"):
                continue
            info[t] = {"start": start, "length": length, "width": int(st["width"]), "height": int(st["height"]),
                       "fps": st.get("r_frame_rate") if _fps(st.get("r_frame_rate")) else "30/1",
                       "profile": st.get("profile"), "refs": st.get("refs"), "bframes": st.get("has_b_frames")}
        else:
            info[t] = {"start": start, "length": length, "sample_rate": int(st.get("sample_rate") or 48000),
                       "channels": int(st.get("channels") or 2)}
    starts = [v["start"] for v in info.values()]
    if starts:
        info["start"] = min(starts)
    return info


def probe(data, key):
    opts, url = source_args(key)
    r = subprocess.run([FFPROBE, "-v", "error"] + opts + [
        "-show_entries", "stream=codec_type,width,height,r_frame_rate,profile,refs,has_b_frames,sample_rate,channels,start_time,duration",
        "-of", "json", url], input=data, capture_output=True, timeout=20)
    return parse_probe(r.stdout.decode("utf-8", "replace"))


def build_args(key, need, info, dur, ad, video, audio, shift=0.0, at=None, sep=None):
    """ffmpeg arguments that write one normalised MPEG-TS segment to stdout.

    need: the output streams, 'va' (muxed), 'v' or 'a' (one HLS rendition each).
    Show streams found by the probe (info) are copied. Anything missing, and
    everything in an ad, is generated instead (black video in `video`'s size
    and rate, silence in `audio`'s format), so the layout never changes from
    segment to segment. Every timestamp is the source's plus `shift`, except
    an ad's when `at` is given: the slate then starts there.

    sep: (key, probe info, fd) of a separate audio segment to merge in,
    read from that file descriptor; it shares the video's clock, so it takes
    the same shift."""
    start = info.get("start", 0.0)
    copy = "" if ad else "".join(t for t in need if t in info)
    # -copyts keeps the source timestamps (moved by -itsoffset only);
    # -mpegts_copyts stops the muxer adding its own 0.7 s delay. Consecutive
    # segments, built by separate ffmpeg runs, must line up exactly.
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-copyts"]
    if copy:
        opts, url = source_args(key)
        args += opts + ["-itsoffset", "%.6f" % shift, "-i", url]
    inputs, maps, codecs = (1 if copy else 0), [], []
    sep_input = None
    if sep and not ad and "a" in need and "a" not in copy and "a" in sep[1]:
        opts, url = source_args(sep[0], sep[2])
        args += opts + ["-itsoffset", "%.6f" % shift, "-i", url]
        copy, sep_input = copy + "A", inputs
        inputs += 1
    for i, t in enumerate(need):
        if t in copy:
            maps += ["-map", "0:%s:0" % t]
            codecs += ["-c:%d" % i, "copy"]
            continue
        if t == "a" and sep_input is not None:
            maps += ["-map", "%d:a:0" % sep_input]
            codecs += ["-c:%d" % i, "copy"]
            continue
        offset = at if at is not None else info.get(t, {}).get("start", start) + shift
        if t == "v":
            src = "color=c=black:s=%dx%d:r=%s" % (video["width"], video["height"], video["fps"])
            frames = max(1, round(dur * _fps(video["fps"])))
            codecs += ["-c:%d" % i, "libx264", "-preset", "ultrafast", "-tune", "stillimage"] + x264_args(video) + [
                       "-pix_fmt", "yuv420p", "-frames:%d" % i, str(frames)]
        else:
            src = "anullsrc=r=%d:cl=%s" % (audio["sample_rate"], "mono" if audio["channels"] == 1 else "stereo")
            codecs += ["-c:%d" % i, "aac", "-b:%d" % i, "96k"]
        args += ["-itsoffset", "%.6f" % offset, "-t", "%.6f" % max(dur, 0.04), "-f", "lavfi", "-i", src]
        maps += ["-map", "%d:0" % inputs]
        inputs += 1
    pids = []
    for i, t in enumerate(need):
        pids += ["-streamid", "%d:%d" % (i, VIDEO_PID if t == "v" else AUDIO_PID)]
    return args + maps + codecs + pids + [
        "-mpegts_service_id", "1", "-mpegts_pmt_start_pid", "4096", "-mpegts_copyts", "1",
        "-mpegts_flags", "+initial_discontinuity", "-f", "mpegts", "pipe:1"]


# --- ad-break clips -------------------------------------------------------------

_breaks = {"at": 0.0, "clips": [], "known": {}}
_breaks_lock = threading.Lock()


def usable_clip_name(name):
    """Whether a file in BREAKS_DIR is a clip to use: a video extension, not
    hidden, not a partial download (.part, yt-dlp's .fNNN.ext pieces)."""
    low = name.lower()
    return (not low.startswith(".") and low.endswith(BREAK_EXTS)
            and not re.search(r"\.f\d+\.\w+$", low) and ".part" not in low)


def probe_clip(path):
    """(duration s, has audio) of a clip file, or None if unreadable."""
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                        "-of", "json", path], capture_output=True, timeout=30)
    try:
        d = json.loads(r.stdout or b"{}")
        dur = float(d["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None
    types = {st.get("codec_type") for st in d.get("streams", [])}
    return (dur, "audio" in types) if dur >= 1 and "video" in types else None


def break_clips(now=None):
    """[(path, duration, has audio)] in BREAKS_DIR, rescanned every
    BREAKS_RESCAN seconds; each file is probed once per size and mtime."""
    now = time.time() if now is None else now
    with _breaks_lock:
        if not BREAKS_DIR or (_breaks["at"] and now - _breaks["at"] < BREAKS_RESCAN):
            return _breaks["clips"]
        _breaks["at"] = now
        known = _breaks["known"]
    clips = []
    try:
        names = sorted(os.listdir(BREAKS_DIR))
    except OSError as e:
        print("breaks folder unreadable: %s" % type(e).__name__, flush=True)
        names = []
    for name in names:
        path = os.path.join(BREAKS_DIR, name)
        if not usable_clip_name(name):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if now - st.st_mtime < BREAK_SETTLE:
            continue  # still arriving
        sig = (st.st_size, st.st_mtime)
        if path not in known or known[path][0] != sig:
            known[path] = (sig, probe_clip(path))
            if known[path][1] is None:
                print("break clip unreadable, skipped: %s" % name, flush=True)
        if known[path][1]:
            clips.append((path,) + known[path][1])
    with _breaks_lock:
        _breaks["clips"] = clips
    return clips


def clip_for(stream, track, seg):
    """(path, position s, has audio) to fill ad segment seg from, or None.

    A break is a run of consecutive ad segments; its first one we know of
    picks a clip at random (seeded by stream and break, so a retried request
    gets the same bytes). Every clip starts from its beginning and loops; the
    position comes from the segment's date (or its place in the break)."""
    clips = break_clips()
    if not clips:
        return None
    with stream.lock:
        segs = stream.segs[track]
        first = seg["seq"]
        while first - 1 in segs and segs[first - 1]["ad"]:
            first -= 1
        pick = stream.break_pick.get(first)
        if pick is None:
            rnd = random.Random("%s:%d" % (stream.key, first))
            pick = stream.break_pick[first] = (rnd.choice(clips), segs[first]["clock"])
            while len(stream.break_pick) > 32:
                stream.break_pick.pop(next(iter(stream.break_pick)))
        (path, length, has_audio), first_clock = pick
        if seg["clock"] is not None and first_clock is not None:
            elapsed = seg["clock"] - first_clock
        else:
            elapsed = sum(s["dur"] for q, s in segs.items() if first <= q < seg["seq"])
    return path, max(0.0, elapsed) % length, has_audio


def x264_args(video):
    """libx264 settings for generated video (slates, break clips) that match
    the show's H.264 (profile, reference frames, B-frames) as probed. The
    client's decoder gets show and break as one stream (Dispatcharr only
    copy-remuxes), so the parameter sets change in place at every break;
    Android TV hardware decoders flashed green frames there when the slates
    were Baseline with no B-frames against a High show with them."""
    def num(v, lo, hi, default):
        try:
            return min(max(int(v), lo), hi)
        except (TypeError, ValueError):
            return default
    profile = str(video.get("profile") or "High").lower()
    refs = num(video.get("refs"), 1, 16, 3)
    if "baseline" in profile:
        return ["-profile:v", "baseline", "-x264-params", "ref=%d:scenecut=0" % refs]
    params = "cabac=1:ref=%d:bframes=%d:b-adapt=0:b-pyramid=none:weightb=0:scenecut=0" % (
        refs, num(video.get("bframes"), 0, 2, 1))
    if profile.startswith("main"):
        return ["-profile:v", "main", "-x264-params", params + ":8x8dct=0"]
    return ["-profile:v", "high", "-x264-params", params + ":8x8dct=1"]


def clip_args(path, pos, has_audio, dur, at, video, audio):
    """ffmpeg arguments for one ad segment cut from a clip: `dur` seconds from
    `pos` (looping past the end), in the show's size, rate and audio format,
    starting at output time `at`, with the fixed PIDs."""
    fps = video["fps"]
    frames = max(1, round(dur * _fps(fps)))
    w, h = video["width"], video["height"]
    rate, ch = audio["sample_rate"], audio["channels"]
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-stream_loop", "-1", "-ss", "%.3f" % pos, "-i", path]
    if not has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=%d:cl=%s" % (rate, "mono" if ch == 1 else "stereo")]
    return args + [
        "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
        # Timestamps restart when the loop wraps, so frames and samples are
        # renumbered by count before anything that looks at time.
        "-vf", "setpts=N/FRAME_RATE/TB,fps=%s,scale=%d:%d:force_original_aspect_ratio=decrease,"
               "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p" % (fps, w, h, w, h),
        "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast"] + x264_args(video) + [
        "-af", "aresample=%d,asetpts=N/SR/TB,apad,atrim=duration=%.6f" % (rate, max(dur, 0.04)),
        "-ac", str(ch), "-c:a", "aac", "-b:a", "128k",
        "-output_ts_offset", "%.6f" % at,
        "-streamid", "0:%d" % VIDEO_PID, "-streamid", "1:%d" % AUDIO_PID,
        "-mpegts_service_id", "1", "-mpegts_pmt_start_pid", "4096", "-mpegts_copyts", "1",
        "-mpegts_flags", "+initial_discontinuity", "-f", "mpegts", "pipe:1"]


class Stream:
    """One client's view of one channel: Pluto's media playlist URLs and the
    segments listed in them so far."""

    def __init__(self, key, video, audio, subs=None, burn=False):
        self.key = key
        self.burn = burn       # draw the captions into the picture instead of CEA-608
        self.upstream = {"v": video}
        self.audio = audio     # Pluto's separate audio playlist, merged in, or None
        self.subs = subs       # Pluto's subtitle playlist, turned into captions, or None
        self.sub_seen = collections.OrderedDict()  # subtitle segment URLs read
        self.cues = {}         # (start, text) -> (start, end, lines), on the video's clock
        self.cc_origin = None  # caption time the first captioned segment started from
        self.audio_segs = collections.OrderedDict()
        self.need = {"v": "va"}
        self.segs = {t: collections.OrderedDict() for t in self.upstream}
        self.built = {t: collections.OrderedDict() for t in self.upstream}
        self.params = {"v": None, "a": None}   # last show format seen, for slates
        # Output timeline (see run_shift): epoch is the PROGRAM-DATE-TIME that
        # maps to TIMELINE_START; shifts[track][dseq] is the offset applied
        # to every show segment of one discontinuity run.
        self.epoch = None
        self.last_playlist = {}
        self.shifts = {t: {} for t in self.upstream}
        self.run_clock = {t: {} for t in self.upstream}  # block -> date of its first segment seen
        self.anchors = []      # (run date, output time), shared by the tracks
        self.track_end = {}    # track -> furthest output time served
        self.break_pick = {}   # first ad seq of a break -> (clip, that segment's date)
        self.inflight = {}     # (track, seq) -> threading.Event while being built
        self.used = time.time()
        self.lock = threading.Lock()


_streams = {}
_streams_lock = threading.Lock()
_keys = collections.OrderedDict()
_keys_lock = threading.Lock()


def new_stream(video, audio, subs=None, burn=False):
    key = secrets.token_urlsafe(12)
    now = time.time()
    with _streams_lock:
        for k in [k for k, s in _streams.items() if now - s.used > STREAM_TTL]:
            del _streams[k]
        _streams[key] = Stream(key, video, audio, subs, burn)
    return key


def get_stream(key):
    with _streams_lock:
        s = _streams.get(key)
        if s:
            s.used = time.time()
        return s


def aes_key(uri):
    with _keys_lock:
        if uri in _keys:
            return _keys[uri]
    data, _ = fetch_bytes(uri, timeout=10)
    with _keys_lock:
        _keys[uri] = data.hex()
        while len(_keys) > 64:
            _keys.popitem(last=False)
    return data.hex()


def media(stream, track):
    """Our copy of one of Pluto's media playlists. Pluto now and then answers
    a live channel with an empty playlist ending in EXT-X-ENDLIST, which makes
    a client stop for good. A live channel never ends, so ENDLIST is never
    passed on, and an empty answer gets the last good playlist instead (the
    client simply polls again)."""
    text, final = fetch_bytes(stream.upstream[track], timeout=10)
    headers, segs, _ = parse_media(text.decode("utf-8", "replace"), final)
    with stream.lock:
        if not segs:
            if track in stream.last_playlist:
                return stream.last_playlist[track]
            raise RuntimeError("empty playlist")
        clocks = [s["clock"] for s in segs if s["clock"] is not None]
        if stream.epoch is None and clocks:
            stream.epoch = min(clocks)
        known = stream.segs[track]
        for s in segs:
            known[s["seq"]] = s
        while len(known) > SEGMENTS_KEPT:
            known.popitem(last=False)
        body = render_media(headers, segs, "/s/%s/%s" % (stream.key, track), False)
        stream.last_playlist[track] = body
    if stream.audio:
        refresh_audio(stream)
    if stream.subs:
        refresh_subs(stream)
    return body


def refresh_audio(stream):
    """Read Pluto's separate audio playlist into stream.audio_segs."""
    try:
        text, final = fetch_bytes(stream.audio, timeout=10)
        _, segs, _ = parse_media(text.decode("utf-8", "replace"), final)
    except Exception as e:  # noqa: BLE001 - the video still plays, with silence
        print("audio playlist failed: %s" % type(e).__name__, flush=True)
        return
    with stream.lock:
        for s in segs:
            stream.audio_segs[s["seq"]] = s
        while len(stream.audio_segs) > SEGMENTS_KEPT:
            stream.audio_segs.popitem(last=False)


def audio_for(stream, seg):
    """The separate audio segment covering the same time as video segment
    seg (same date, give or take a second), or None."""
    def find():
        with stream.lock:
            cands = [a for a in stream.audio_segs.values()
                     if a["clock"] is not None and seg["clock"] is not None
                     and abs(a["clock"] - seg["clock"]) < 1.0]
            same = stream.audio_segs.get(seg["seq"])
        if cands:
            return min(cands, key=lambda a: abs(a["clock"] - seg["clock"]))
        return same if seg["clock"] is None else None
    found = find()
    if found is None:
        refresh_audio(stream)  # Pluto's audio playlist can lag the video's
        found = find()
    return found


# --- closed captions -----------------------------------------------------------
#
# Pluto's subtitle track is WebVTT: on live channels, the broadcast's own
# roll-up captions redrawn after every word, timed on the video's MPEG-TS
# clock. Players fed through Dispatcharr (one MPEG-TS with video and audio)
# can't receive a text track, but every player that does closed captions reads
# CEA-608 from the H.264 video itself. So each show segment's new words go back
# into its video as roll-up CEA-608 (GStreamer's tttocea608 + h264ccinserter),
# without re-encoding the picture. Ads get none.

def vtt_seconds(s):
    m = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})$", s.strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0


def parse_vtt(text):
    """[(start, end, [lines])] in MPEG-TS seconds. Pluto's live cues are
    already on the video's clock; an X-TIMESTAMP-MAP header, where present,
    maps them onto it."""
    offset = 0.0
    m = re.search(r"X-TIMESTAMP-MAP=([^\r\n]*)", text)
    if m:
        ts, local = re.search(r"MPEGTS:(\d+)", m.group(1)), re.search(r"LOCAL:([\d:.]+)", m.group(1))
        if ts and local and vtt_seconds(local.group(1)) is not None:
            offset = int(ts.group(1)) / 90000.0 - vtt_seconds(local.group(1))
    cues = []
    for block in re.split(r"\r?\n[ \t]*\r?\n", text):
        lines = block.strip("\r\n").splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"^\s*(\S+)\s+-->\s+(\S+)", line)
            if not m:
                continue
            a, b = vtt_seconds(m.group(1)), vtt_seconds(m.group(2))
            if a is not None and b is not None:
                body = [" ".join(re.sub(r"<[^>]*>", "", l).split()) for l in lines[i + 1:]]
                cues.append((a + offset, b + offset, [l for l in body if l]))
            break
    return cues


def caption_words(cues, t0, t1):
    """[(time, text)]: the words to add to roll-up captions between t0 and
    t1. Each of Pluto's cues is the whole caption display after one more
    word, so its new words are its last line minus the previous cue's (or
    all of it when a new line starts). Words close together go as one
    caption: CEA-608 carries about 2 characters per frame, and each caption
    sent costs a few control codes, so one per word drops words in fast
    speech."""
    out, prev = [], ""
    for start, _, lines in sorted(cues):
        if not lines or lines[-1] == prev:
            continue
        cur = lines[-1]
        new = cur[len(prev):] if prev and cur.startswith(prev) else cur
        prev = cur
        new = " ".join(new.split())
        if not new or not t0 <= start < t1:
            continue
        if out and start - out[-1][0] < CC_GROUP and len(out[-1][1]) + len(new) < 32:
            out[-1] = (out[-1][0], out[-1][1] + " " + new)
        else:
            out.append((start, new))
    return out


def srt(words, origin, end):
    """SubRip text for words ([(time, text)]), timed from origin."""
    def fmt(t):
        ms = int(round(max(t, 0.0) * 1000))
        return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
    parts = []
    for i, (t, text) in enumerate(words):
        a = t - origin
        b = max((words[i + 1][0] if i + 1 < len(words) else end) - origin, a + 0.04)
        parts.append("%d\n%s --> %s\n%s\n" % (i + 1, fmt(a), fmt(b), text))
    return "\n".join(parts)


def bottom_row(captions):
    """The bottom row of roll-up captions after showing captions (texts) in
    turn. tttocea608 word-wraps the whole stream as one: after each space it
    starts a new row if the next word would take the row past 31 characters
    (gst-plugins-rs video/closedcaption/src/tttocea608/translate.rs)."""
    row = ""
    for text in captions:
        for w in text.split():
            row = w if not row or len(row) + 1 + len(w) > 31 else row + " " + w
    return row


def have_gst():
    return bool(shutil.which(GST_LAUNCH))


def refresh_subs(stream):
    """Read Pluto's subtitle playlist and any new WebVTT segments in it into
    stream.cues."""
    try:
        text, final = fetch_bytes(stream.subs, timeout=10)
        _, segs, _ = parse_media(text.decode("utf-8", "replace"), final)
    except Exception as e:  # noqa: BLE001 - the stream still plays, without captions
        print("subtitle playlist failed: %s" % type(e).__name__, flush=True)
        return
    for s in segs:
        with stream.lock:
            if s["uri"] in stream.sub_seen:
                continue
        try:
            body, _ = fetch_bytes(s["uri"], timeout=10)
        except Exception:  # noqa: BLE001 - tried again on the next refresh
            continue
        cues = parse_vtt(body.decode("utf-8", "replace"))
        with stream.lock:
            stream.sub_seen[s["uri"]] = True
            while len(stream.sub_seen) > SEGMENTS_KEPT:
                stream.sub_seen.popitem(last=False)
            for c in cues:
                stream.cues[(c[0], "\n".join(c[2]))] = c
            if len(stream.cues) > CUES_KEPT:
                for k in sorted(stream.cues)[:len(stream.cues) - CUES_KEPT]:
                    del stream.cues[k]


def gst_caption_args(fd):
    """gst-launch arguments: MPEG-TS on stdin, SubRip on fd, the video with
    the captions inserted on stdout (as MPEG-TS, timestamps moved; see
    add_captions). caption-meta-order=display: cccombiner attaches captions
    in presentation order, and with B-frames the inserter must put them back
    in decoding order or the letters come out scrambled."""
    return [GST_LAUNCH, "-q",
            "fdsrc", "fd=0", "!", "tsdemux", "!", "h264parse", "!",
            "video/x-h264,stream-format=byte-stream,alignment=au", "!",
            "cccombiner", "name=c", "!", "h264ccinserter", "caption-meta-order=display", "!",
            "h264parse", "!", "mpegtsmux", "!", "fdsink", "fd=1",
            "fdsrc", "fd=%d" % fd, "!", "subparse", "!", "text/x-raw,format=utf8", "!",
            "tttocea608", "!", "ccconverter", "!", "closedcaption/x-cea-708,format=cc_data", "!", "c.caption"]


def cues_until(stream, t1):
    """The stream's cues, read again from Pluto first if none reach t1 yet
    (Pluto's subtitle playlist can lag the video's)."""
    with stream.lock:
        cues = list(stream.cues.values())
    if not cues or max(c[0] for c in cues) < t1 - 1.0:
        refresh_subs(stream)
        with stream.lock:
            cues = list(stream.cues.values())
    return cues


def ass_time(t):
    cs = int(round(max(t, 0.0) * 100))
    return "%d:%02d:%02d.%02d" % (cs // 360000, cs // 6000 % 60, cs // 100 % 60, cs % 100)


def ass_doc(cues, t0, t1, width, height):
    """An ASS subtitle file drawing the cues that overlap t0..t1, on the
    video's own clock (ffmpeg runs with -copyts), in the look of broadcast
    captions: white monospace on black boxes, bottom left. Each of Pluto's
    cues is the whole caption display for its time, so it maps to one event."""
    size = max(12, round(height * 0.05))
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: CC,DejaVu Sans Mono,%d,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,3,%d,0,1,%d,%d,%d,1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            % (width, height, size, max(2, size // 6), round(width * 0.1), round(width * 0.1), round(height * 0.07)))
    events = []
    for start, end, lines in sorted(cues):
        if not lines or end <= t0 or start >= t1:
            continue
        # \N is a line break; braces would start ASS override tags
        text = "\\N".join(l.replace("\\", "/").replace("{", "(").replace("}", ")") for l in lines)
        events.append("Dialogue: 0,%s,%s,CC,,0,0,0,,%s" % (ass_time(start), ass_time(end), text))
    return head + "\n".join(events) + "\n"


def burn_args(key, path, video):
    """ffmpeg arguments: the segment on stdin re-encoded with the captions in
    `path` drawn in, timestamps and audio unchanged."""
    opts, url = source_args(key)
    return [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-copyts"] + opts + [
        "-i", url, "-map", "0:v:0", "-map", "0:a?", "-vf", "subtitles=filename=%s" % path,
        # passthrough + the demuxer's time base: every frame keeps its exact
        # source timestamp (the encoder's default 1/fps grid moved them ~12 ms)
        "-fps_mode", "passthrough", "-enc_time_base:v", "demux",
        "-c:v", "libx264", "-preset", "veryfast"] + x264_args(video) + [
        "-crf", "21", "-maxrate", "5M", "-bufsize", "10M", "-c:a", "copy",
        "-mpegts_copyts", "1", "-f", "mpegts", "pipe:1"]


def burn_captions(stream, seg, data, key, info, video):
    """The segment re-encoded with its captions drawn into the picture, as
    unencrypted MPEG-TS with the same streams and timestamps, or None
    without video. Every show segment is re-encoded, captioned or not, so the
    video's parameters stay the same throughout."""
    if "v" not in info:
        return None
    t0 = info["v"]["start"]
    t1 = t0 + seg["dur"]
    doc = ass_doc(cues_until(stream, t1), t0, t1, video["width"], video["height"])
    with tempfile.NamedTemporaryFile("w", suffix=".ass", dir=BURN_DIR, encoding="utf-8") as f:
        f.write(doc)
        f.flush()
        r = subprocess.run(burn_args(key, f.name, video), input=data, capture_output=True, timeout=BURN_TIMEOUT)
    if r.returncode or not r.stdout:
        raise RuntimeError("burn: " + r.stderr.decode("utf-8", "replace").strip()[-200:])
    return r.stdout


def add_captions(stream, seg, data, key, info):
    """The segment (data, encrypted with key) with captions in its video, as
    unencrypted MPEG-TS with the same streams and timestamps, or None to send
    it as it is (no video, no new words, or a step failed)."""
    if "v" not in info:
        return None
    t0 = info["v"]["start"]
    t1 = t0 + seg["dur"]
    cues = cues_until(stream, t1)
    # Each segment's captions are sent within it, at ~2 characters per frame:
    # words due in its last moments would be cut off, so they go at the start
    # of the next segment instead (up to CC_TAIL late).
    words = [(max(t, t0), w) for t, w in caption_words(cues, t0 - CC_TAIL, t1 - CC_TAIL)]
    if not words:
        return None
    # Every segment's captions come from a new encoder, which starts by
    # writing on the bottom row without rolling it up first: the row the last
    # segment left half full would be overwritten. So its text is sent again
    # ahead of this segment's first words.
    with stream.lock:
        if stream.cc_origin is None:
            stream.cc_origin = t0 - CC_TAIL
        origin = stream.cc_origin
    carry = bottom_row(w for _, w in caption_words(cues, origin, t0 - CC_TAIL))
    if carry:
        # its own caption, with time to send it (2 characters a frame)
        ready = t0 + (len(carry) + 8) / 60.0
        words = [(t0, carry)] + [(max(t, ready), w) for t, w in words]
    plain = data
    if key:
        opts, url = source_args(key)
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-copyts"] + opts + [
            "-i", url, "-map", "0", "-c", "copy", "-mpegts_copyts", "1", "-f", "mpegts", "pipe:1"],
            input=data, capture_output=True, timeout=CC_TIMEOUT)
        if r.returncode or not r.stdout:
            raise RuntimeError("decrypt: " + r.stderr.decode("utf-8", "replace").strip()[-200:])
        plain = r.stdout
    text = srt(words, t0, t1).encode("utf-8")
    ccv, err, code = run_ffmpeg(gst_caption_args, plain, text, timeout=CC_TIMEOUT)
    if code or not ccv:
        raise RuntimeError("gstreamer: " + err.decode("utf-8", "replace").strip()[-200:])
    # GStreamer restarts the clock at its own origin; move the captioned video
    # back onto the source's timestamps and put the other streams beside it.
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
                        "-of", "csv=p=0", "pipe:0"], input=ccv, capture_output=True, timeout=CC_TIMEOUT)
    try:
        moved = t0 - float(r.stdout.decode().split()[0])
    except (IndexError, ValueError):
        raise RuntimeError("captioned video unreadable")
    out, err, code = run_ffmpeg(
        lambda fd: [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-copyts",
                    "-itsoffset", "%.6f" % moved, "-i", "pipe:0", "-i", "pipe:%d" % fd,
                    "-map", "0:v:0", "-map", "1:a?", "-c", "copy", "-mpegts_copyts", "1",
                    "-f", "mpegts", "pipe:1"],
        ccv, plain, timeout=CC_TIMEOUT)
    if code or not out:
        raise RuntimeError("restore: " + err.decode("utf-8", "replace").strip()[-200:])
    return out


def segment(stream, track, seq):
    """The normalised bytes of one segment, or None if it isn't known. A
    segment being built (e.g. ahead, see build_ahead) is waited for rather
    than built twice."""
    while True:
        with stream.lock:
            if seq in stream.built[track]:
                return stream.built[track][seq]
            if seq not in stream.segs[track]:
                return None
            ev = stream.inflight.get((track, seq))
            if ev is None:
                ev = stream.inflight[(track, seq)] = threading.Event()
                break
        if not ev.wait(30):
            raise RuntimeError("segment build timed out")
    try:
        out = _build(stream, track, seq)
    finally:
        with stream.lock:
            stream.inflight.pop((track, seq), None)
        ev.set()
    build_ahead(stream, track, seq + 1)
    return out


def build_ahead(stream, track, seq):
    """Build the next segment in the background if it is an ad cut from a
    clip (the slowest kind), so it is ready when asked for. Chained in order:
    each built segment starts the next."""
    with stream.lock:
        seg = stream.segs[track].get(seq)
        if (seg is None or not seg["ad"] or seq in stream.built[track]
                or (track, seq) in stream.inflight or not break_clips()):
            return

    def run():
        try:
            segment(stream, track, seq)
        except Exception as e:  # noqa: BLE001 - the client's own request retries
            print("build ahead %s/%d failed: %s" % (track, seq, type(e).__name__), flush=True)
    threading.Thread(target=run, daemon=True).start()


def _build(stream, track, seq):
    with stream.lock:
        seg = stream.segs[track].get(seq)
    if seg is None:
        return None
    data, _ = fetch_bytes(seg["uri"])
    key = (aes_key(seg["key"][0]), seg["key"][1]) if seg["key"] else None
    info = probe(data, key)
    need = stream.need[track]
    with stream.lock:
        if not seg["ad"]:
            for t in "va":
                if t in info:
                    stream.params[t] = {k: v for k, v in info[t].items() if k not in ("start", "length")}
        video = stream.params["v"] or {k: v for k, v in info.get("v", DEFAULT_VIDEO).items() if k not in ("start", "length")}
        audio = stream.params["a"] or {k: v for k, v in info.get("a", DEFAULT_AUDIO).items() if k not in ("start", "length")}
    if "start" not in info:
        raise RuntimeError("segment has no readable streams")
    if stream.subs and not seg["ad"]:
        try:
            if stream.burn:
                cc = burn_captions(stream, seg, data, key, info, video)
            else:
                cc = add_captions(stream, seg, data, key, info)
            if cc:
                data, key = cc, None
        except Exception as e:  # noqa: BLE001 - sent without captions instead
            print("captions failed for %s/%d: %s %s" % (track, seq, type(e).__name__, str(e)[:200]), flush=True)
    shift, at = run_shift(stream, track, seg, info)
    sep = None
    if stream.audio and not seg["ad"]:
        aseg = audio_for(stream, seg)
        if aseg is None:
            print("no audio segment for %s/%d: silence" % (track, seq), flush=True)
        else:
            adata, _ = fetch_bytes(aseg["uri"])
            akey = (aes_key(aseg["key"][0]), aseg["key"][1]) if aseg["key"] else None
            ainfo = probe(adata, akey)
            if "a" in ainfo:
                with stream.lock:
                    stream.params["a"] = {k: v for k, v in ainfo["a"].items() if k not in ("start", "length")}
                    audio = stream.params["a"]
                sep = (akey, ainfo, adata)
    out, code, filled = None, None, "black"
    clip = clip_for(stream, track, seg) if seg["ad"] else None
    if clip:
        try:
            r = subprocess.run(clip_args(clip[0], clip[1], clip[2], seg["dur"], at, video, audio),
                               capture_output=True, timeout=CLIP_TIMEOUT)
            if r.returncode == 0 and r.stdout:
                out, code, filled = r.stdout, 0, os.path.basename(clip[0])
            else:
                print("clip segment failed, black instead: %s" % r.stderr.decode("utf-8", "replace")[-200:],
                      flush=True)
        except subprocess.TimeoutExpired:
            print("clip segment too slow, black instead", flush=True)
    if out is None:
        out, err, code = run_ffmpeg(
            lambda fd: build_args(key, need, info, seg["dur"], seg["ad"], video, audio, shift,
                                  at if seg["ad"] else None, sep and (sep[0], sep[1], fd)),
            data, sep[2] if sep else None)
    if code or not out:
        raise RuntimeError("ffmpeg: " + err.decode("utf-8", "replace").strip()[-300:])
    start = at if seg["ad"] else (info[need]["start"] if need in info else info["start"]) + shift
    # A show segment can run past its EXTINF (by 0.7 s seen): record where its
    # streams really end, or the next run is placed on top of the tail.
    end = start + seg["dur"]
    if not seg["ad"]:
        sources = [v for k, v in info.items() if k in "va"] + ([sep[1]["a"]] if sep else [])
        end = max([end] + [v["start"] + shift + v["length"] for v in sources])
    served(stream, track, end)
    if DEBUG:
        print("seg %s %s/%d dseq=%d %s clock=%.3f src=%s shift=%.3f out=%.3f+%s%s" % (
            stream.key[:6], track, seq, seg["dseq"], "AD" if seg["ad"] else "show", seg["clock"] or 0,
            {k: round(v["start"], 3) for k, v in info.items() if k in "va"}, shift, start, seg["extinf"],
            " fill=%s@%.1f" % (filled, clip[1]) if seg["ad"] and clip else ""), flush=True)
    with stream.lock:
        stream.built[track][seq] = out
        while len(stream.built[track]) > BUILT_KEPT:
            stream.built[track].popitem(last=False)
    return out


def run_ffmpeg(make_args, data, extra=None, timeout=30):
    """Run ffmpeg (or any command) with `data` on stdin and, if given, `extra`
    on a second pipe whose descriptor is passed to make_args. (stdout, stderr,
    code)."""
    if extra is None:
        r = subprocess.run(make_args(None), input=data, capture_output=True, timeout=timeout)
        return r.stdout, r.stderr, r.returncode
    rfd, wfd = os.pipe()
    try:
        proc = subprocess.Popen(make_args(rfd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, pass_fds=(rfd,))
    except Exception:
        os.close(wfd)
        raise
    finally:
        os.close(rfd)

    def feed():
        try:
            with os.fdopen(wfd, "wb") as w:
                w.write(extra)
        except OSError:
            pass  # ffmpeg exited early; its error is in stderr
    t = threading.Thread(target=feed, daemon=True)
    t.start()
    try:
        out, err = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    t.join(5)
    return out, err, proc.returncode


def run_shift(stream, track, seg, info):
    """(shift, at) for one segment. Show segments keep their own timestamps
    plus `shift`; ad slates are generated starting at output time `at`.

    Pluto's timestamps jump at every discontinuity (a show at 94000 s, a raw
    ad restarting at 1 s), which a remuxer absorbs badly. Instead every run
    (the segments between two discontinuities) is placed on one continuous
    timeline by its PROGRAM-DATE-TIME: at TIMELINE_START plus the run's
    distance from the stream's epoch, pushed later only to avoid overlapping
    what was already served (Pluto's dates and media durations can disagree by
    a fraction of a second). Show segments keep one shift per run, so their own
    timestamps stay intact. Ad slates ignore the ad's timestamps and sit at
    their date's place. (The anchors are written to be shared by several
    tracks; since separate audio is merged in, there is only one.)"""
    with stream.lock:
        if seg["clock"] is None or stream.epoch is None:
            return 0.0, info["start"]  # no dates in the playlist: leave timestamps alone
        runs = stream.run_clock[track]
        block = block_key(stream, track, seg)
        rc = runs.setdefault(block, seg["clock"])
        anchor = next((a for a in stream.anchors if abs(a[0] - rc) <= RUN_MATCH), None)
        if anchor is None:
            at = TIMELINE_START + (rc - stream.epoch)
            ends = [e for t, e in stream.track_end.items() if t == track or e - at < RUN_MATCH]
            anchor = (rc, max([at] + ends))
            stream.anchors.append(anchor)
            del stream.anchors[:-32]
        at = anchor[1] + (seg["clock"] - anchor[0])
        shifts = stream.shifts[track]
        if block not in shifts:
            need = stream.need[track]
            src = info[need]["start"] if need in info else info["start"]
            shifts[block] = at - src
        for d in (shifts, runs):
            while len(d) > 32:
                d.pop(next(iter(d)))
        return shifts[block], at


def block_key(stream, track, seg):
    """(dseq, first seq) of seg's block: the consecutive segments of one
    discontinuity run that are all show or all ad. Pluto does not always put a
    discontinuity between a show and an ad, and show segments (placed by their
    own timestamps) and ad segments (placed by date) can then disagree by a
    tenth of a second; anchoring each block anew keeps them from overlapping."""
    segs = stream.segs[track]
    first = seg["seq"]
    while (first - 1 in segs and segs[first - 1]["dseq"] == seg["dseq"]
           and segs[first - 1]["ad"] == seg["ad"]):
        first -= 1
    return seg["dseq"], first


def served(stream, track, end):
    """Record how far this track's output now reaches (see run_shift)."""
    with stream.lock:
        stream.track_end[track] = max(stream.track_end.get(track, 0.0), end)


def have_ffmpeg():
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))


def passthrough(bw, video, audio):
    """Without ffmpeg (e.g. the script reached a pod still on the old image):
    the pre-normalising behaviour, pointing the client straight at Pluto. It
    plays, but stalls at ad breaks. ('body', master) when the audio is a
    separate rendition, else ('redirect', the variant)."""
    if not audio:
        return "redirect", video
    return "body", ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Original",DEFAULT=YES,'
                    'AUTOSELECT=YES,URI="%s"\n#EXT-X-STREAM-INF:BANDWIDTH=%d,AUDIO="audio"\n%s\n') % (audio, bw, video)


def stream_session(shared):
    """A fresh session for one stream (see boot), tried twice; the shared
    session only if Pluto's boot API is unreachable, in which case this
    stream may end another one."""
    for attempt in (1, 2):
        try:
            return boot(str(uuid.uuid4()))
        except Exception as e:  # noqa: BLE001
            print("stream boot failed (%d/2): %s" % (attempt, type(e).__name__), flush=True)
    return shared


def open_channel(session, channel, channel_id=None, burn=False):
    """('body', our master playlist) for a channel, or ('redirect', Pluto's own
    master URL) if Pluto's master can't be read: the client then still gets a
    stream, though one that stalls at ad breaks."""
    master_url = stream_url(stream_session(session), channel)
    try:
        text, final = fetch_master(master_url)
        picked = pick_renditions(text, final, MAX_BANDWIDTH)
        if picked:
            bw, video, audio = picked
            if not have_ffmpeg():
                print("ffmpeg/ffprobe not found: passing Pluto's stream through as is", flush=True)
                return passthrough(bw, video, audio)
            subs = None
            if burn:
                subs = pick_subtitles(text, final)
            elif channel_id and ("*" in CC_CHANNELS or channel_id in CC_CHANNELS):
                subs = pick_subtitles(text, final)
                if subs and not have_gst():
                    print("gst-launch-1.0 not found: no captions", flush=True)
                    subs = None
            return "body", master_playlist(new_stream(video, audio, subs, burn), bw)
    except Exception as e:  # noqa: BLE001
        print("master failed: %s" % type(e).__name__, flush=True)
    return "redirect", master_url


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # request lines carry no secrets, but keep it quiet
        pass

    def reply(self, code, body=b"", ctype="text/plain", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        m = re.match(r"^/s/([A-Za-z0-9_-]{16})/([va])(?:\.m3u8|/(\d{1,12})\.ts)$", path)
        if m:
            return self.serve_stream(*m.groups())
        try:
            session, channels = refresh()
        except Exception:  # noqa: BLE001
            return self.reply(503, b"pluto unavailable\n")
        if path == "/healthz":
            return self.reply(200, b"ok\n")
        if path == "/playlist.m3u":
            base = os.environ.get("PUBLIC_BASE") or "http://" + (self.headers.get("Host") or "localhost")
            return self.reply(200, playlist(base, channels).encode(), "audio/x-mpegurl")
        m = re.match(r"^/(ch|cc)/([0-9a-f]{24})\.m3u8$", path)
        if m and m.group(2) in channels:
            kind, value = open_channel(session, channels[m.group(2)], m.group(2), burn=m.group(1) == "cc")
            if kind == "body":
                return self.reply(200, value.encode(), "application/vnd.apple.mpegurl")
            return self.reply(302, headers={"Location": value})
        self.reply(404, b"not found\n")

    def serve_stream(self, key, track, seq):
        stream = get_stream(key)
        if stream is None or track not in stream.upstream:
            return self.reply(404, b"unknown stream\n")
        try:
            if seq is None:
                return self.reply(200, media(stream, track).encode(), "application/vnd.apple.mpegurl")
            data = segment(stream, track, int(seq))
        except Exception as e:  # noqa: BLE001
            print("%s %s failed: %s %s" % ("segment" if seq else "playlist", track, type(e).__name__,
                                          str(e)[:200]), flush=True)
            return self.reply(502, b"upstream failed\n")
        if data is None:
            return self.reply(404, b"unknown segment\n")
        self.reply(200, data, "video/mp2t")


def watch_script(path, interval=30, exit_fn=None):
    """Re-exec when the script changes on disk. The script comes from a ConfigMap
    with no name hash, so a git change updates the mounted file but would never
    restart this process."""
    def mtime():
        return os.stat(path).st_mtime_ns

    def loop():
        seen = mtime()
        while True:
            time.sleep(interval)
            try:
                now = mtime()
            except OSError:
                continue
            if now != seen:
                print("script changed; restarting", flush=True)
                (exit_fn or (lambda: os.execv(sys.executable, [sys.executable, "-I", path])))()
                return

    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    watch_script(os.path.abspath(__file__))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

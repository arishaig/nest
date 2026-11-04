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
(`_ad/creative/` in the URL) are replaced with a black, silent segment of the
same length, at the same timestamps.

Needs ffmpeg and ffprobe on PATH; without them /ch falls back to pointing the
client straight at Pluto (no normalising). Usage and caveats: docs/live-tv.md.
"""
import collections
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
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
AD_RE = re.compile(r"_ad/creative/")
VIDEO_PID, AUDIO_PID = 0x100, 0x101
# Used for a slate when neither the show nor the ad says otherwise.
DEFAULT_VIDEO = {"width": 1280, "height": 720, "fps": "30/1"}
DEFAULT_AUDIO = {"sample_rate": 48000, "channels": 2}
TIMELINE_START = 10.0  # output seconds at the first segment's date
RUN_MATCH = 2.0        # s: video and audio runs this close in date are the same run
DEBUG = os.environ.get("PLUTO_DEBUG") == "1"  # log every segment's placement
SEGMENTS_KEPT = 60     # per track: a few playlist windows
BUILT_KEPT = 3         # per track: finished segments kept for retried requests (~2 MB each)

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
                       "fps": st.get("r_frame_rate") if _fps(st.get("r_frame_rate")) else "30/1"}
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
        "-show_entries", "stream=codec_type,width,height,r_frame_rate,sample_rate,channels,start_time,duration",
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
            codecs += ["-c:%d" % i, "libx264", "-preset", "ultrafast", "-tune", "stillimage",
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


class Stream:
    """One client's view of one channel: Pluto's media playlist URLs and the
    segments listed in them so far."""

    def __init__(self, key, video, audio):
        self.key = key
        self.upstream = {"v": video}
        self.audio = audio     # Pluto's separate audio playlist, merged in, or None
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
        self.run_clock = {t: {} for t in self.upstream}  # dseq -> date of the run's first segment seen
        self.anchors = []      # (run date, output time), shared by the tracks
        self.track_end = {}    # track -> furthest output time served
        self.used = time.time()
        self.lock = threading.Lock()


_streams = {}
_streams_lock = threading.Lock()
_keys = collections.OrderedDict()
_keys_lock = threading.Lock()


def new_stream(video, audio):
    key = secrets.token_urlsafe(12)
    now = time.time()
    with _streams_lock:
        for k in [k for k, s in _streams.items() if now - s.used > STREAM_TTL]:
            del _streams[k]
        _streams[key] = Stream(key, video, audio)
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


def segment(stream, track, seq):
    """The normalised bytes of one segment, or None if it isn't known."""
    with stream.lock:
        if seq in stream.built[track]:
            return stream.built[track][seq]
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
        print("seg %s %s/%d dseq=%d %s clock=%.3f src=%s shift=%.3f out=%.3f+%s" % (
            stream.key[:6], track, seq, seg["dseq"], "AD" if seg["ad"] else "show", seg["clock"] or 0,
            {k: round(v["start"], 3) for k, v in info.items() if k in "va"}, shift, start, seg["extinf"]),
            flush=True)
    with stream.lock:
        stream.built[track][seq] = out
        while len(stream.built[track]) > BUILT_KEPT:
            stream.built[track].popitem(last=False)
    return out


def run_ffmpeg(make_args, data, extra=None):
    """Run ffmpeg with `data` on stdin and, if given, `extra` on a second
    pipe whose descriptor is passed to make_args. (stdout, stderr, code)."""
    if extra is None:
        r = subprocess.run(make_args(None), input=data, capture_output=True, timeout=30)
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
        out, err = proc.communicate(data, timeout=30)
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
        rc = runs.setdefault(seg["dseq"], seg["clock"])
        anchor = next((a for a in stream.anchors if abs(a[0] - rc) <= RUN_MATCH), None)
        if anchor is None:
            at = TIMELINE_START + (rc - stream.epoch)
            ends = [e for t, e in stream.track_end.items() if t == track or e - at < RUN_MATCH]
            anchor = (rc, max([at] + ends))
            stream.anchors.append(anchor)
            del stream.anchors[:-32]
        at = anchor[1] + (seg["clock"] - anchor[0])
        shifts = stream.shifts[track]
        if seg["dseq"] not in shifts:
            need = stream.need[track]
            src = info[need]["start"] if need in info else info["start"]
            shifts[seg["dseq"]] = at - src
        for d in (shifts, runs):
            while len(d) > 32:
                d.pop(next(iter(d)))
        return shifts[seg["dseq"]], at


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


def open_channel(session, channel):
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
            return "body", master_playlist(new_stream(video, audio), bw)
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
        m = re.match(r"^/ch/([0-9a-f]{24})\.m3u8$", path)
        if m and m.group(1) in channels:
            kind, value = open_channel(session, channels[m.group(1)])
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

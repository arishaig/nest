#!/usr/bin/env python3
"""pluto-proxy: M3U playlist and stream redirector for Pluto TV's free channels.

Pluto's stream URLs need a short-lived session token from its own (unofficial)
boot API, so there is no static playlist. This serves:

  /playlist.m3u     channels as an M3U; tvg-id is Pluto's channel id, which is
                    also the id in i.mjh.nz's PlutoTV/us.xml guide
  /ch/<id>.m3u8     302 to the best single quality level of Pluto's stitched
                    stream (fresh token). The master playlist lists every
                    quality plus audio-description and subtitle tracks, and a
                    remuxer that maps everything (Dispatcharr's default ffmpeg
                    profile) copies them all into one huge stream. Where the
                    original audio is a separate rendition (about half the
                    channels) the variant alone is silent, so a one-variant
                    master with that audio track is served instead.
  /healthz          200 once a session and channel list are loaded

Stdlib only. Usage and caveats: docs/live-tv.md.
"""
import json
import os
import re
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
CLIENT_ID = str(uuid.uuid4())
ID_RE = re.compile(r"^[0-9a-f]{24}$")

_lock = threading.Lock()
_state = {"session": None, "channels": None, "at": 0.0}


def fetch_json(url, token=None):
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return json.load(r)


def boot():
    q = urllib.parse.urlencode({
        "appName": "web", "appVersion": "9.0.0", "deviceVersion": "120.0",
        "deviceModel": "web", "deviceMake": "chrome", "deviceType": "web",
        "clientID": CLIENT_ID, "clientModelNumber": "1.0.0",
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


def pick_variant(master, base_url, max_bw=0):
    """Absolute URL of the highest-bandwidth variant (within max_bw if set) in an
    HLS master playlist, or None if it lists no variants."""
    best = _best_variant(master, max_bw)
    return urllib.parse.urljoin(base_url, best[1]) if best else None


def trimmed_master(master, base_url, max_bw=0):
    """A one-variant master for channels whose original audio is a separate
    EXT-X-MEDIA rendition (about half of Pluto's): the bare variant playlist is
    video only, so redirecting to it loses the sound. Keeps the best variant and
    the original audio track (not the audio-description one), with absolute URIs
    and no subtitles. None when the variant carries its own audio."""
    best = _best_variant(master, max_bw)
    group = re.search(r'AUDIO="([^"]+)"', best[0]) if best else None
    if not group:
        return None
    media = [l for l in master.splitlines()
             if l.startswith("#EXT-X-MEDIA:TYPE=AUDIO") and 'GROUP-ID="%s"' % group.group(1) in l
             and "URI=" in l and "describes-video" not in l]
    if not media:
        return None
    audio = next((l for l in media if "DEFAULT=YES" in l), media[0])
    audio = re.sub(r'URI="([^"]*)"', lambda m: 'URI="%s"' % urllib.parse.urljoin(base_url, m.group(1)), audio)
    info = re.sub(r',?SUBTITLES="[^"]*"', "", best[0])
    return "#EXTM3U\n%s\n%s\n%s\n" % (audio, info, urllib.parse.urljoin(base_url, best[1]))


def fetch_master(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", "replace"), r.geturl()


def resolve(session, channel):
    """('body', trimmed master) when the audio is a separate rendition, else
    ('redirect', best-variant URL). Falls back to a redirect to the master if
    Pluto's playlist can't be read or parsed (the client then still gets a
    working, if heavier, stream)."""
    master_url = stream_url(session, channel)
    try:
        text, final = fetch_master(master_url)
        body = trimmed_master(text, final, MAX_BANDWIDTH)
        if body:
            return "body", body
        return "redirect", pick_variant(text, final, MAX_BANDWIDTH) or master_url
    except Exception as e:  # noqa: BLE001
        print("variant pick failed: %s" % type(e).__name__, flush=True)
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
            kind, value = resolve(session, channels[m.group(1)])
            if kind == "body":
                return self.reply(200, value.encode(), "application/vnd.apple.mpegurl")
            return self.reply(302, headers={"Location": value})
        self.reply(404, b"not found\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

"""Offline tests: Pluto's APIs are stubbed. Run: python3 -m unittest discover -s test

The SegmentPipelineTest cases need ffmpeg and ffprobe on PATH and are skipped
without them."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pluto_proxy as pp  # noqa: E402

CID = "5" * 24
SESSION = {"token": "TOK", "params": "a=1&b=2", "stitcher": "https://stitch.example", "channels_api": "https://api.example"}
BASE = "https://stitch.example/v2/stitch/hls/channel/x/master.m3u8?q=1"

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="subtitle/en/playlist.m3u8?sid=S"
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1042180,SUBTITLES="subs"
1042180/playlist.m3u8?sid=S&jwt=TOK
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=3321280,SUBTITLES="subs"
3321280/playlist.m3u8?sid=S&jwt=TOK
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=2294080,SUBTITLES="subs"
2294080/playlist.m3u8?sid=S&jwt=TOK
"""

# Original audio is its own rendition: the variant alone would be silent.
SPLIT_AUDIO = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="subtitle/en/playlist.m3u8?sid=S"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",CHARACTERISTICS="public.accessibility.describes-video",URI="audio/ad/audio.m3u8?sid=S"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English [Original]",DEFAULT=YES,AUTOSELECT=YES,URI="audio/orig/audio.m3u8?sid=S"
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1042180,SUBTITLES="subs",AUDIO="audio"
1042180/playlist.m3u8?sid=S&jwt=TOK
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=3321280,SUBTITLES="subs",AUDIO="audio"
3321280/playlist.m3u8?sid=S&jwt=TOK
"""

# Only an audio-description track: treated as muxed audio.
AD_ONLY = SPLIT_AUDIO.replace(
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English [Original]",DEFAULT=YES,AUTOSELECT=YES,URI="audio/orig/audio.m3u8?sid=S"\n', "")

MEDIA = """#EXTM3U
#EXT-X-VERSION:5
#EXT-X-TARGETDURATION:5
#EXT-X-DISCONTINUITY-SEQUENCE:3
#EXT-X-MEDIA-SEQUENCE:31
#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:40.000Z
#EXT-X-KEY:METHOD=AES-128,URI="https://k.example/show.key",IV=0x0000000000000000000000000000000a
#EXTINF:5,
https://seg.example/052_pluto/clip/abc_Show_S1E1/720pDRM/hls/hls_2400-00025.ts
#EXT-X-KEY:METHOD=AES-128,URI="https://k.example/ad.key"
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:45.000Z
#EXTINF:4.992,
https://seg.example/v1/mp4/c(ts)/p(7_ad%2Fcreative%2F6939_ad%2F720p%2Fvideo_2400.mp4)/id3(p=clik)/
#EXT-X-KEY:METHOD=NONE
#EXTINF:0.033,
https://seg.example/0_ad/creative/691f_ad/720p/20251120_200758/hls/hls_2400_00006.ts
#EXT-X-DISCONTINUITY
#EXTINF:5,
https://seg.example/634_pluto_tv_o_o/clip/58e5_ptv-7424_ad_bumper_animation_dots/1080pDRM/0.ts
#PLUTO-SESSION-ID:356878d3
"""


def fake_fetch(url, token=None):
    if url.endswith("/v2/guide/categories"):
        return {"data": [{"id": "c1", "name": "News + Opinion"}]}
    return {"data": [
        {"id": CID, "name": 'Say "Hi"', "number": 7, "categoryIDs": ["c1"],
         "stitched": {"path": "/stitch/hls/channel/%s/master.m3u8" % CID},
         "images": [{"type": "colorLogoPNG", "url": "https://img/x.png"}]},
        {"id": "6" * 24, "name": "Office", "plutoOfficeOnly": True, "stitched": {"path": "/x"}},
        {"id": "7" * 24, "name": "Unstitched", "stitched": None},
    ]}


class Upstream:
    """Stub for pp.fetch_bytes: URL -> bytes (a callable value is called)."""

    def __init__(self):
        self.files = {}

    def __call__(self, url, timeout=15):
        if url not in self.files:
            raise OSError("404 " + url)
        v = self.files[url]
        return (v() if callable(v) else v), url


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pp.fetch_json = fake_fetch
        cls.boots = []
        pp.boot = lambda client_id=None: cls.boots.append(client_id) or dict(SESSION)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), pp.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.up = Upstream()
        pp.fetch_bytes = self.up
        pp.fetch_master = lambda url: (MASTER, BASE)

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read().decode()

    def test_playlist_filters_and_formats(self):
        status, _, body = self.get("/playlist.m3u")
        self.assertEqual(status, 200)
        self.assertEqual(body.count("#EXTINF"), 1)  # office-only and unstitched dropped
        self.assertIn('tvg-id="%s"' % CID, body)
        self.assertIn("tvg-name=\"Say 'Hi'\"", body)  # quotes can't break the attribute
        self.assertIn('group-title="News + Opinion"', body)
        self.assertIn("http://127.0.0.1:%d/ch/%s.m3u8" % (self.srv.server_port, CID), body)

    def test_channel_falls_back_to_redirect_when_master_unreadable(self):
        pp.fetch_master = lambda url: (_ for _ in ()).throw(OSError("down"))
        status, h, _ = self.get("/ch/%s.m3u8" % CID)
        self.assertEqual(status, 302)
        self.assertEqual(h["Location"], "https://stitch.example/v2/stitch/hls/channel/%s/master.m3u8?a=1&b=2&jwt=TOK&masterJWTPassthrough=true" % CID)

    def test_each_stream_boots_its_own_session(self):
        self.get("/playlist.m3u")
        before = len(self.boots)
        self.get("/ch/%s.m3u8" % CID)
        self.get("/ch/%s.m3u8" % CID)
        ids = self.boots[before:]
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(ids) and ids[0] != ids[1])     # never the shared CLIENT_ID

    def test_without_ffmpeg_passes_pluto_through(self):
        old = pp.FFMPEG
        pp.FFMPEG = "/nonexistent/ffmpeg"
        try:
            status, h, _ = self.get("/ch/%s.m3u8" % CID)
            self.assertEqual(status, 302)
            self.assertEqual(h["Location"], "https://stitch.example/v2/stitch/hls/channel/x/3321280/playlist.m3u8?sid=S&jwt=TOK")
            pp.fetch_master = lambda url: (SPLIT_AUDIO, BASE)
            status, _, body = self.get("/ch/%s.m3u8" % CID)
            self.assertEqual(status, 200)
            self.assertIn('URI="https://stitch.example/v2/stitch/hls/channel/x/audio/orig/audio.m3u8?sid=S"', body)
            self.assertNotIn("/s/", body)
        finally:
            pp.FFMPEG = old

    def test_boot_failure_retries_then_uses_shared_session(self):
        calls = []

        def flaky(client_id=None):
            calls.append(client_id)
            if len(calls) <= 2:
                raise OSError("SERVFAIL")
            return dict(SESSION)
        old, pp.boot = pp.boot, flaky
        try:
            self.assertEqual(pp.stream_session({"shared": 1}), {"shared": 1})
            self.assertEqual(len(calls), 2)
            calls.clear()
            calls.append("x")  # next call: 2nd attempt succeeds
            self.assertEqual(pp.stream_session({"shared": 1}), dict(SESSION))
        finally:
            pp.boot = old

    @unittest.skipUnless(pp.have_ffmpeg(), "needs ffmpeg")
    def test_channel_serves_own_master_muxed(self):
        status, h, body = self.get("/ch/%s.m3u8" % CID)
        self.assertEqual(status, 200)
        self.assertIn("mpegurl", h["Content-Type"])
        self.assertIn("BANDWIDTH=3321280", body)
        self.assertNotIn("EXT-X-MEDIA", body)
        key = body.strip().splitlines()[-1].split("/")[2]
        s = pp.get_stream(key)
        self.assertEqual(s.upstream, {"v": "https://stitch.example/v2/stitch/hls/channel/x/3321280/playlist.m3u8?sid=S&jwt=TOK"})
        self.assertEqual(s.need, {"v": "va"})

    @unittest.skipUnless(pp.have_ffmpeg(), "needs ffmpeg")
    def test_channel_serves_own_master_split_audio(self):
        pp.fetch_master = lambda url: (SPLIT_AUDIO, BASE)
        status, _, body = self.get("/ch/%s.m3u8" % CID)
        self.assertEqual(status, 200)
        self.assertNotIn("EXT-X-MEDIA", body)        # one playlist, audio merged in
        key = body.strip().splitlines()[-1].split("/")[2]
        s = pp.get_stream(key)
        self.assertEqual(s.audio, "https://stitch.example/v2/stitch/hls/channel/x/audio/orig/audio.m3u8?sid=S")
        self.assertEqual(s.need, {"v": "va"})

    def test_pick_renditions(self):
        self.assertEqual(pp.pick_renditions(MASTER, "https://h/p/m.m3u8", 2300000),
                         (2294080, "https://h/p/2294080/playlist.m3u8?sid=S&jwt=TOK", None))
        self.assertIsNone(pp.pick_renditions(MASTER, "https://h/p/m.m3u8", 100))
        self.assertIsNone(pp.pick_renditions("#EXTM3U\n", "https://h/p/m.m3u8"))
        self.assertIsNone(pp.pick_renditions(AD_ONLY, "https://h/p/m.m3u8")[2])  # never the AD track

    def test_parse_media(self):
        headers, segs, endlist = pp.parse_media(MEDIA, "https://stitch.example/a/b.m3u8")
        self.assertFalse(endlist)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:31", headers)
        self.assertEqual([s["seq"] for s in segs], [31, 32, 33, 34])
        self.assertEqual([s["ad"] for s in segs], [False, True, True, False])  # bumper is not an ad
        self.assertEqual([s["dseq"] for s in segs], [3, 4, 4, 5])
        self.assertEqual(segs[0]["key"], ("https://k.example/show.key", "0000000000000000000000000000000a"))
        self.assertEqual(segs[1]["key"], ("https://k.example/ad.key", "%032x" % 32))  # IV = sequence number
        self.assertIsNone(segs[2]["key"])
        self.assertEqual(segs[1]["clock"] - segs[0]["clock"], 5.0)
        self.assertAlmostEqual(segs[3]["clock"] - segs[1]["clock"], 4.992 + 0.033, places=4)  # carried by EXTINF
        self.assertEqual(segs[2]["extinf"], "0.033")
        with self.assertRaises(ValueError):
            pp.parse_media('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"\n#EXTINF:5,\nx.ts\n', "https://h/")

    def test_render_media(self):
        headers, segs, _ = pp.parse_media(MEDIA, "https://stitch.example/a/b.m3u8")
        out = pp.render_media(headers, segs, "/s/K/v", False)
        self.assertNotIn("EXT-X-KEY", out)
        self.assertNotIn("PLUTO", out)
        self.assertEqual(out.count("#EXT-X-DISCONTINUITY\n"), 2)
        self.assertIn("#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:45.000Z\n#EXTINF:4.992,\n/s/K/v/32.ts", out)
        self.assertTrue(out.rstrip().endswith("/s/K/v/34.ts"))

    @unittest.skipUnless(pp.have_ffmpeg(), "needs ffmpeg")
    def test_media_playlist_and_endlist_glitch(self):
        status, _, body = self.get("/ch/%s.m3u8" % CID)
        path = body.strip().splitlines()[-1]
        upstream = pp.get_stream(path.split("/")[2]).upstream["v"]
        self.up.files[upstream] = MEDIA.encode()
        status, _, good = self.get(path)
        self.assertEqual(status, 200)
        self.assertIn(path[:-5] + "/31.ts", good)
        # Pluto's transient "empty and ended" answer: the last good one is served
        self.up.files[upstream] = b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-ENDLIST\n"
        status, _, again = self.get(path)
        self.assertEqual((status, again), (200, good))
        self.up.files[upstream] = (MEDIA + "#EXT-X-ENDLIST\n").encode()
        self.assertNotIn("ENDLIST", self.get(path)[2])

    @unittest.skipUnless(pp.have_ffmpeg(), "needs ffmpeg")
    def test_stream_routes_404(self):
        self.assertEqual(self.get("/s/AAAAAAAAAAAAAAAA/v.m3u8")[0], 404)   # unknown key
        _, _, body = self.get("/ch/%s.m3u8" % CID)
        key = body.strip().splitlines()[-1].split("/")[2]
        self.assertEqual(self.get("/s/%s/a.m3u8" % key)[0], 404)          # muxed: no audio track
        self.assertEqual(self.get("/s/%s/v/99.ts" % key)[0], 404)         # never listed
        self.assertEqual(self.get("/s/%s/v/../x.ts" % key)[0], 404)

    @unittest.skipUnless(pp.have_ffmpeg(), "needs ffmpeg")
    def test_upstream_failure_is_502(self):
        _, _, body = self.get("/ch/%s.m3u8" % CID)
        self.assertEqual(self.get(body.strip().splitlines()[-1])[0], 502)  # upstream not stubbed

    def test_unknown_and_malformed_ids_404(self):
        self.assertEqual(self.get("/ch/%s.m3u8" % ("6" * 24))[0], 404)  # filtered out
        self.assertEqual(self.get("/ch/..%2Fetc.m3u8")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_idle_streams_expire(self):
        old = pp.new_stream("https://v", None)
        pp.get_stream(old).used -= pp.STREAM_TTL + 1
        pp.new_stream("https://v", None)
        self.assertIsNone(pp.get_stream(old))

    def test_watch_script_fires_when_file_changes(self):
        fired = threading.Event()
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("a")
        pp.watch_script(f.name, interval=0.05, exit_fn=fired.set)
        time.sleep(0.2)
        self.assertFalse(fired.is_set())
        os.utime(f.name, ns=(1, 1))
        self.assertTrue(fired.wait(2))
        os.unlink(f.name)

    def test_healthz(self):
        self.assertEqual(self.get("/healthz")[0], 200)


class TimelineTest(unittest.TestCase):
    def stream(self):
        s = pp.Stream("K" * 16, "https://v", None)
        s.epoch = 1000.0
        return s

    @staticmethod
    def place(s, track, dseq, clock, src, dur=5.0, ad=False):
        """Output start of one segment, recorded as served like segment() does."""
        need = s.need[track]
        info = {need[0]: {"start": src}, "start": src}
        shift, at = pp.run_shift(s, track, {"dseq": dseq, "clock": clock}, info)
        out = at if ad else src + shift
        pp.served(s, track, out + dur)
        return out

    def test_runs_land_on_one_timeline(self):
        s = self.stream()
        T = pp.TIMELINE_START
        self.assertAlmostEqual(self.place(s, "v", 0, 1000.0, 94000.0), T)
        self.assertAlmostEqual(self.place(s, "v", 0, 1005.0, 94005.0), T + 5)
        # an ad restarting its clock at 1 s lands 10 s in, by its date
        self.assertAlmostEqual(self.place(s, "v", 1, 1010.0, 1.0), T + 10)
        self.assertAlmostEqual(self.place(s, "v", 1, 1015.0, 6.0), T + 15)

    def test_ad_slates_follow_dates_not_ad_timestamps(self):
        s = self.stream()
        T = pp.TIMELINE_START
        # repackaged ad segments whose own clock restarts every segment
        for i in range(4):
            self.assertAlmostEqual(self.place(s, "v", 1, 1000.0 + 5 * i, 10.0, ad=True), T + 5 * i)

    def test_overlap_is_pushed_after_what_was_served(self):
        s = self.stream()
        self.place(s, "v", 0, 1000.0, 100.0, dur=10.5)              # media ran 0.5 s long
        self.assertAlmostEqual(self.place(s, "v", 1, 1010.0, 1.0), pp.TIMELINE_START + 10.5)

    def test_parse_probe_reads_lengths(self):
        info = pp.parse_probe(json.dumps({"streams": [
            {"codec_type": "video", "width": 1280, "height": 720, "r_frame_rate": "30/1",
             "start_time": "10.0", "duration": "5.7"},
            {"codec_type": "audio", "sample_rate": "48000", "channels": 2, "start_time": "9.98"}]}))
        self.assertEqual(info["v"]["length"], 5.7)
        self.assertEqual(info["a"]["length"], 0.0)
        self.assertEqual(info["start"], 9.98)

    def test_no_dates_leaves_timestamps_alone(self):
        s = self.stream()
        self.assertEqual(pp.run_shift(s, "v", {"dseq": 0, "clock": None}, {"start": 5.0}), (0.0, 5.0))


class BuildArgsTest(unittest.TestCase):
    V = {"width": 1280, "height": 720, "fps": "30/1"}
    A = {"sample_rate": 48000, "channels": 2}

    def test_show_is_copied_with_fixed_pids(self):
        info = {"v": {"start": 5.0}, "a": {"start": 5.1}, "start": 5.0}
        args = pp.build_args(("ab" * 16, "00" * 16), "va", info, 5.0, False, self.V, self.A, shift=2.0)
        j = " ".join(args)
        self.assertIn("-key %s -iv %s -itsoffset 2.000000 -i crypto:pipe:0" % ("ab" * 16, "00" * 16), j)
        self.assertIn("-map 0:v:0 -map 0:a:0", j)
        self.assertIn("-c:0 copy -c:1 copy", j)
        self.assertIn("-streamid 0:256 -streamid 1:257", j)
        self.assertNotIn("lavfi", j)

    def test_ad_is_all_generated_at_shifted_times(self):
        info = {"v": {"start": 1.0}, "a": {"start": 0.98}, "start": 0.98}
        j = " ".join(pp.build_args(None, "va", info, 4.992, True, self.V, self.A, shift=100.0, at=250.0))
        self.assertNotIn("pipe:0", j)
        self.assertIn("-itsoffset 250.000000 -t 4.992000 -f lavfi -i color=c=black:s=1280x720:r=30/1", j)
        self.assertIn("-itsoffset 250.000000 -t 4.992000 -f lavfi -i anullsrc=r=48000:cl=stereo", j)
        self.assertIn("-frames:0 150", j)

    def test_missing_audio_is_filled_with_silence(self):
        info = {"v": {"start": 5.0}, "start": 5.0}
        j = " ".join(pp.build_args(None, "va", info, 5.0, False, self.V, self.A))
        self.assertIn("-i pipe:0", j)
        self.assertIn("-map 0:v:0 -map 1:0", j)
        self.assertIn("-c:1 aac", j)

    def test_split_audio_track(self):
        info = {"a": {"start": 3.0}, "start": 3.0}
        j = " ".join(pp.build_args(None, "a", info, 0.033, True, self.V, self.A))
        self.assertIn("-t 0.040000 -f lavfi -i anullsrc", j)   # never a zero-length segment
        self.assertIn("-streamid 0:257", j)


def ts(args, extra=()):
    """Run ffmpeg to make a test MPEG-TS segment."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error"] + list(args) + list(extra)
                       + ["-f", "mpegts", "pipe:1"], capture_output=True, check=True)
    return r.stdout


def packets(data):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=index,id,codec_type:packet=stream_index,pts_time",
                        "-of", "json", "pipe:0"], input=data, capture_output=True, check=True)
    return json.loads(r.stdout)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class SegmentPipelineTest(unittest.TestCase):
    """A show at 94000 s on PIDs 0x50/0x51, then a raw ad restarting at 1 s on
    0x100/0x101 with its own layout, then the show again: the output must keep
    one layout and one continuous timeline."""

    @staticmethod
    def segment_bytes(start, vpid, apid, color):
        return ts(["-f", "lavfi", "-t", "5", "-i", "testsrc=s=320x180:r=30", "-f", "lavfi", "-t", "5",
                   "-i", "sine=f=440:r=48000", "-vf", "drawbox=c=%s:t=fill" % color,
                   "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                   "-output_ts_offset", str(start), "-mpegts_copyts", "1", "-copyts",
                   "-streamid", "0:%d" % vpid, "-streamid", "1:%d" % apid])

    def test_show_ad_show(self):
        up = Upstream()
        pp.fetch_bytes = up
        up.files["https://s/1.ts"] = self.segment_bytes(94000, 0x50, 0x51, "red")
        up.files["https://s/2.ts"] = self.segment_bytes(94005, 0x50, 0x51, "red")
        up.files["https://a/0_ad/creative/x_ad/hls/1.ts"] = self.segment_bytes(1, 0x100, 0x101, "white")
        up.files["https://s/3.ts"] = self.segment_bytes(94015, 0x50, 0x51, "red")
        up.files["https://up/v.m3u8"] = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:1\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:00.000Z\n"
            "#EXTINF:5,\nhttps://s/1.ts\n#EXTINF:5,\nhttps://s/2.ts\n"
            "#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:10.000Z\n"
            "#EXTINF:5,\nhttps://a/0_ad/creative/x_ad/hls/1.ts\n"
            "#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:15.000Z\n"
            "#EXTINF:5,\nhttps://s/3.ts\n").encode()
        key = pp.new_stream("https://up/v.m3u8", None)
        s = pp.get_stream(key)
        pp.media(s, "v")
        out = b"".join(pp.segment(s, "v", n) for n in (1, 2, 3, 4))
        p = packets(out)
        layout = sorted((st["id"], st["codec_type"]) for st in p["streams"])
        self.assertEqual(layout, [("0x100", "video"), ("0x101", "audio")])
        for want in ("video", "audio"):
            idx = next(st["index"] for st in p["streams"] if st["codec_type"] == want)
            t = sorted(float(x["pts_time"]) for x in p["packets"] if x["stream_index"] == idx)
            self.assertLess(abs(t[0] - pp.TIMELINE_START), 0.1, want)
            self.assertLess(max(b - a for a, b in zip(t, t[1:])), 0.2, want)    # no jumps, no holes
            self.assertGreater(t[-1] - t[0], 19.5, want)
        # the ad's white frames were replaced by black ones
        frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", "12", "-i", "pipe:0", "-frames:v", "1",
                                "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                               input=pp.segment(s, "v", 3), capture_output=True).stdout
        self.assertLess(frame[0], 30)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class SeparateAudioPipelineTest(unittest.TestCase):
    """Video-only show segments with a separate audio playlist, an ad in
    between: the output is one muxed stream, sound copied for the show,
    silence for the ad, both continuous."""

    @staticmethod
    def video(start):
        return ts(["-f", "lavfi", "-t", "5", "-i", "testsrc=s=320x180:r=30", "-c:v", "libx264",
                   "-preset", "ultrafast", "-output_ts_offset", str(start), "-mpegts_copyts", "1",
                   "-copyts", "-streamid", "0:80"])

    @staticmethod
    def audio(start):
        return ts(["-f", "lavfi", "-t", "5", "-i", "sine=f=440:r=48000", "-c:a", "aac",
                   "-output_ts_offset", str(start), "-mpegts_copyts", "1", "-copyts", "-streamid", "0:80"])

    def test_merge(self):
        up = Upstream()
        pp.fetch_bytes = up
        head = "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:1\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:00.000Z\n"
        mid = ("#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:05.000Z\n#EXTINF:5,\n%s\n"
               "#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:10.000Z\n#EXTINF:5,\n%s\n")
        up.files["https://up/v.m3u8"] = (head + "#EXTINF:5,\nhttps://s/v1.ts\n" + mid % (
            "https://s/0_ad/creative/x_ad/v.ts", "https://s/v3.ts")).encode()
        up.files["https://up/a.m3u8"] = (head + "#EXTINF:5,\nhttps://s/a1.ts\n" + mid % (
            "https://s/0_ad/creative/x_ad/a.ts", "https://s/a3.ts")).encode()
        up.files["https://s/v1.ts"], up.files["https://s/a1.ts"] = self.video(94000), self.audio(94000)
        up.files["https://s/0_ad/creative/x_ad/v.ts"] = self.video(1)
        up.files["https://s/v3.ts"], up.files["https://s/a3.ts"] = self.video(94010), self.audio(94010)
        s = pp.get_stream(pp.new_stream("https://up/v.m3u8", "https://up/a.m3u8"))
        pp.media(s, "v")
        out = b"".join(pp.segment(s, "v", n) for n in (1, 2, 3))
        p = packets(out)
        self.assertEqual(sorted((st["id"], st["codec_type"]) for st in p["streams"]),
                         [("0x100", "video"), ("0x101", "audio")])
        for want in ("video", "audio"):
            idx = next(st["index"] for st in p["streams"] if st["codec_type"] == want)
            t = sorted(float(x["pts_time"]) for x in p["packets"] if x["stream_index"] == idx)
            self.assertLess(abs(t[0] - pp.TIMELINE_START), 0.1, want)
            self.assertLess(max(b - a for a, b in zip(t, t[1:])), 0.2, want)
            self.assertGreater(t[-1] - t[0], 14.5, want)
        vol = subprocess.run(["ffmpeg", "-hide_banner", "-i", "pipe:0", "-af", "volumedetect", "-vn", "-f", "null", "-"],
                             input=pp.segment(s, "v", 2), capture_output=True).stderr.decode()
        self.assertIn("max_volume: -91", vol)                         # the ad is silent
        vol = subprocess.run(["ffmpeg", "-hide_banner", "-i", "pipe:0", "-af", "volumedetect", "-vn", "-f", "null", "-"],
                             input=pp.segment(s, "v", 3), capture_output=True).stderr.decode()
        self.assertNotIn("max_volume: -91", vol)                      # the show has sound


if __name__ == "__main__":
    unittest.main()

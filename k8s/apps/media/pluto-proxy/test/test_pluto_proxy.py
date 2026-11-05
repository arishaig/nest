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

# the timing tests below check cues at Pluto's own times; CaptionAdvanceTest covers the advance
pp.CC_ADVANCE = 0.0

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
        self.assertEqual([s["ad"] for s in segs], [False, True, True, True])  # the ad-break bumper is part of the break
        self.assertEqual([s["dseq"] for s in segs], [3, 4, 4, 5])
        self.assertEqual(segs[0]["key"], ("https://k.example/show.key", "0000000000000000000000000000000a"))
        self.assertEqual(segs[1]["key"], ("https://k.example/ad.key", "%032x" % 32))  # IV = sequence number
        self.assertIsNone(segs[2]["key"])
        self.assertEqual(segs[1]["clock"] - segs[0]["clock"], 5.0)
        self.assertAlmostEqual(segs[3]["clock"] - segs[1]["clock"], 4.992 + 0.033, places=4)  # carried by EXTINF
        self.assertEqual(segs[2]["extinf"], "0.033")
        with self.assertRaises(ValueError):
            pp.parse_media('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"\n#EXTINF:5,\nx.ts\n', "https://h/")

    def test_ad_patterns(self):
        """Real segment paths seen 2026-11-04 (hosts dropped)."""
        ads = [
            "/prd/creative/57_ad/6650810d9e19600a860bd9ee_ad/720p/20240524_115909/hls/hls_2400_00000.ts",
            "/v1/mp4/c(ts)/h(default)/max(6)/rev(1)/p(6_ad/creative/69ed2e02b20baf9752aa8814_ad/720p/"
            "20260425_211130_923677051_x_c2ac985e/video_2400.mp4)/id3(p=clik,id=69ed2e02b20baf9752aa8814)/seg-1.ts",
            # repackaged creative without `_ad/creative/` in its path
            "/mp4/v1/c(ts)/h(default)/max(18)/rev(1)/p(52e/6ae88bdc8002ec01f03740dd/720p/"
            "20261102_131052_544305688_x_33c825ca/video_2400.mp4)/id3(p=clik,id=6ae88bdc8002ec01f03740dd)/seg-3.ts",
            "/384_Pluto_TV_OandO/clip/5e1f_ptv-7424_ad_bumper_animation_dots_normal_30_1/720p/hls/hls_2400-00000.ts",
            "/384_Pluto_TV_OandO/clip/5e1f_ptv_7424adbumperanimationdotsinverted01/720p/hls/hls_2400-00000.ts",
        ]
        shows = [
            "/live/v1/prd/NBCNewsNow/ts_aes/20260925_205356/video/720p-2400000/161443230300000.ts",
            "/550_NewID/clip/61730bf6829284001a93fd63_Pinkfong_Playfong_Ep4_S1E4/720pDRM/20241125_082415/"
            "hls/360729-752286/hls_2400-00043.ts",
            "/878_Pluto_TV_OandO/clip/622721a4e1977a00079c93ff_plutotv_kidsbumpers_yourewatching_babysharktv_girl_v1/"
            "1080pDRM/20251117_205900/hls/0-end/hls_2400-00000.ts",
            "/123_pluto/clip/5f00_Padded_Bumpers_Season_1/720p/hls/hls_2400-00001.ts",
        ]
        for u in ads:
            self.assertTrue(pp.AD_RE.search(u), u)
        for u in shows:
            self.assertFalse(pp.AD_RE.search(u), u)

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
        """Output start of the next segment, recorded as served like segment() does."""
        need = s.need[track]
        info = {need[0]: {"start": src}, "start": src}
        seq = len(s.segs[track]) + 1
        seg = s.segs[track][seq] = {"seq": seq, "dseq": dseq, "clock": clock, "ad": ad, "dur": dur}
        shift, at = pp.run_shift(s, track, seg, info)
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

    def test_show_and_ad_in_one_run_never_overlap(self):
        """No discontinuity between show and ad (Scripps News): the show's
        media ran 0.08 s past the date where the ad starts."""
        s = self.stream()
        T = pp.TIMELINE_START
        self.place(s, "v", 0, 1000.0, 50.0, dur=5.08)                      # show ends at T+5.08
        ad = self.place(s, "v", 0, 1005.0, 55.0, ad=True)                  # dates say T+5
        self.assertAlmostEqual(ad, T + 5.08)
        self.assertAlmostEqual(self.place(s, "v", 0, 1010.0, 60.0, ad=True), T + 10.08)
        show = self.place(s, "v", 0, 1015.0, 65.0)                         # back to the show
        self.assertGreaterEqual(show, T + 15.08 - 1e-9)

    def test_no_dates_leaves_timestamps_alone(self):
        s = self.stream()
        self.assertEqual(pp.run_shift(s, "v", {"seq": 1, "dseq": 0, "clock": None, "ad": False}, {"start": 5.0}),
                         (0.0, 5.0))


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


class BreakClipTest(unittest.TestCase):
    CLIPS = [("/b/a.mp4", 30.0, True), ("/b/b.mp4", 140.0, True), ("/b/c.mp4", 600.0, False)]

    def setUp(self):
        self.old = pp.break_clips
        pp.break_clips = lambda now=None: self.CLIPS

    def tearDown(self):
        pp.break_clips = self.old

    def stream(self, ads):
        """A stream whose playlist has a show segment then `ads` ad segments."""
        s = pp.Stream("Q" * 16, "https://v", None)
        segs = [{"seq": 10, "ad": False, "clock": 1000.0, "dur": 5.0}]
        segs += [{"seq": 11 + i, "ad": True, "clock": 1005.0 + 5 * i, "dur": 5.0} for i in range(ads)]
        for x in segs:
            s.segs["v"][x["seq"]] = x
        return s

    def test_file_filter(self):
        ok = ["clip.mp4", "Please stand by.webm", "x.MKV", "a.ts"]
        bad = [".hidden.mp4", "a.mp4.part", "a.f399.mp4", "a.f251.webm", "a.nfo", "a.srt", "a-thumb.jpg", "a.flac"]
        self.assertTrue(all(pp.usable_clip_name(n) for n in ok))
        self.assertFalse(any(pp.usable_clip_name(n) for n in bad))

    def test_one_clip_per_break_from_its_start(self):
        s = self.stream(4)
        picks = [pp.clip_for(s, "v", s.segs["v"][q]) for q in (11, 12, 13, 14)]
        self.assertEqual(len({p[0] for p in picks}), 1)               # same clip through the break
        self.assertEqual([p[1] for p in picks], [0.0, 5.0, 10.0, 15.0])  # from the start, continuous

    def test_retries_and_out_of_order_requests_agree(self):
        s = self.stream(4)
        late = pp.clip_for(s, "v", s.segs["v"][14])
        early = pp.clip_for(s, "v", s.segs["v"][11])
        again = pp.clip_for(s, "v", s.segs["v"][14])
        self.assertEqual(late, again)
        self.assertEqual(early[0], late[0])
        self.assertEqual(late[1] - early[1], 15.0)

    def test_short_clip_loops(self):
        pp.break_clips = lambda now=None: [("/b/a.mp4", 12.0, True)]
        s = self.stream(4)
        self.assertEqual([pp.clip_for(s, "v", s.segs["v"][q])[1] for q in (11, 12, 13, 14)], [0.0, 5.0, 10.0, 3.0])

    def test_breaks_pick_independently(self):
        seen = set()
        for k in range(40):
            s = self.stream(1)
            s.key = "K%015d" % k
            seen.add(pp.clip_for(s, "v", s.segs["v"][11])[0])
        self.assertEqual(seen, {c[0] for c in self.CLIPS})              # random over the folder

    def test_no_clips_means_none(self):
        pp.break_clips = lambda now=None: []
        s = self.stream(1)
        self.assertIsNone(pp.clip_for(s, "v", s.segs["v"][11]))

    def test_clip_args(self):
        j = " ".join(pp.clip_args("/b/c.mp4", 593.5, False, 5.033, 812.25,
                                  {"width": 1216, "height": 684, "fps": "30000/1001"},
                                  {"sample_rate": 44100, "channels": 2}))
        self.assertIn("-stream_loop -1 -ss 593.500 -i /b/c.mp4", j)
        self.assertIn("anullsrc=r=44100:cl=stereo", j)                 # clip without sound
        self.assertIn("scale=1216:684:force_original_aspect_ratio=decrease,pad=1216:684", j)
        self.assertIn("-frames:v 151", j)
        self.assertIn("-output_ts_offset 812.250000", j)
        self.assertIn("-streamid 0:256 -streamid 1:257", j)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class BreakFolderTest(unittest.TestCase):
    def test_scan(self):
        d = tempfile.mkdtemp()
        old = os.path.getmtime(d) - 3600
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=3",
                        "-f", "lavfi", "-i", "sine=d=3", "-shortest", os.path.join(d, "good.mp4")], check=True)
        for n in ("broken.mp4", "x.f399.mp4"):
            with open(os.path.join(d, n), "w") as f:
                f.write("nope")
        for n in ("good.mp4", "broken.mp4", "x.f399.mp4"):
            os.utime(os.path.join(d, n), (old, old))
        shutil.copy(os.path.join(d, "good.mp4"), os.path.join(d, "fresh.mp4"))   # still arriving
        saved = (pp.BREAKS_DIR, dict(pp._breaks))
        pp.BREAKS_DIR = d
        pp._breaks.update({"at": 0.0, "clips": [], "known": {}})
        try:
            clips = pp.break_clips()
            self.assertEqual([os.path.basename(c[0]) for c in clips], ["good.mp4"])
            self.assertAlmostEqual(clips[0][1], 3.0, places=0)
            self.assertTrue(clips[0][2])
        finally:
            pp.BREAKS_DIR = saved[0]
            pp._breaks.clear()
            pp._breaks.update(saved[1])
            shutil.rmtree(d)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class BreakClipPipelineTest(unittest.TestCase):
    """Show, a 3-segment break filled from a 7 s clip in another size, rate
    and audio format (so it loops), show again: one layout, exact frame
    counts, continuous timestamps, and the clip's picture in the break."""

    def test_break_from_clip(self):
        d = tempfile.mkdtemp()
        clip = os.path.join(d, "clip.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=640x480:r=25:d=7",
                        # audio ends 0.5 s before the video, as in real clips: the loop
                        # wrap then restarts the timestamps mid-segment
                        "-f", "lavfi", "-i", "sine=f=880:r=44100:d=6.5", "-c:v", "libx264",
                        "-c:a", "aac", clip], check=True)
        up = Upstream()
        pp.fetch_bytes = up
        seg = SegmentPipelineTest.segment_bytes
        up.files["https://s/1.ts"] = seg(94000, 0x50, 0x51, "red")
        for i in range(3):
            up.files["https://s/0_ad/creative/x_ad/%d.ts" % i] = seg(1 + 5 * i, 0x100, 0x101, "white")
        up.files["https://s/5.ts"] = seg(94020, 0x50, 0x51, "red")
        up.files["https://up/v.m3u8"] = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:1\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:00.000Z\n#EXTINF:5,\nhttps://s/1.ts\n"
            "#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:05.000Z\n"
            + "".join("#EXTINF:5,\nhttps://s/0_ad/creative/x_ad/%d.ts\n" % i for i in range(3))
            + "#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:20.000Z\n"
            "#EXTINF:5,\nhttps://s/5.ts\n").encode()
        old = pp.break_clips
        pp.break_clips = lambda now=None: [(clip, 7.0, True)]
        try:
            s = pp.get_stream(pp.new_stream("https://up/v.m3u8", None))
            pp.media(s, "v")
            parts = [pp.segment(s, "v", n) for n in (1, 2, 3, 4, 5)]
        finally:
            pp.break_clips = old
            shutil.rmtree(d)
        p = packets(b"".join(parts))
        self.assertEqual(sorted((st["id"], st["codec_type"]) for st in p["streams"]),
                         [("0x100", "video"), ("0x101", "audio")])
        for want in ("video", "audio"):
            idx = next(st["index"] for st in p["streams"] if st["codec_type"] == want)
            t = sorted(float(x["pts_time"]) for x in p["packets"] if x["stream_index"] == idx)
            self.assertLess(abs(t[0] - pp.TIMELINE_START), 0.1, want)
            self.assertLess(max(b - a for a, b in zip(t, t[1:])), 0.2, want)
            self.assertGreater(t[-1] - t[0], 24.5, want)
        for n in (2, 3, 4):                        # the break: the clip, in the show's format
            info = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                 "stream=codec_type,width,height,nb_read_frames,sample_rate", "-of", "json", "pipe:0"],
                input=parts[n - 1], capture_output=True).stdout)["streams"]
            v = next(x for x in info if x["codec_type"] == "video")
            a = next(x for x in info if x["codec_type"] == "audio")
            self.assertEqual((v["width"], v["height"], int(v["nb_read_frames"])), (320, 180, 150))
            self.assertEqual(a["sample_rate"], "48000")
            self.assertTrue(232 <= int(a["nb_read_frames"]) <= 238, (n, a["nb_read_frames"]))  # 5 s, even across the loop
            frame = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1", "-vf", "scale=1:1",
                                    "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                                   input=parts[n - 1], capture_output=True).stdout
            self.assertGreater(frame[0], 200, n)    # white clip, not black


VTT = """WEBVTT

12:56:34.755 --> 12:56:34.921 position:10% line:63.33% size:80% align:start
WE'RE DEALING WITH. BUT THE     
SPEAKER WAS FANTASTIC. THE THE  

12:56:34.921 --> 12:56:35.121 position:10% line:63.33% size:80% align:start
SPEAKER WAS FANTASTIC. THE THE  

12:56:35.121 --> 12:56:35.388 position:10% line:63.33% size:80% align:start
SPEAKER WAS FANTASTIC. THE THE  
PEOPLE                          

12:56:35.388 --> 12:56:35.588 position:10% line:63.33% size:80% align:start
SPEAKER WAS FANTASTIC. THE THE  
PEOPLE UNDER                    

12:56:36.588 --> 12:56:36.888 position:10% line:63.33% size:80% align:start
SPEAKER WAS FANTASTIC. THE THE  
PEOPLE UNDER <c.yellow>HIM</c>  
"""


class SlateEncoderTest(unittest.TestCase):
    """Generated video must use the show's H.264 settings: the decoder gets
    show and break as one stream, and an in-place switch from High with
    B-frames to Baseline without flashed green on Android TV."""

    def test_matches_show(self):
        args = pp.x264_args({"profile": "High", "refs": 4, "bframes": 1})
        self.assertEqual(args[:2], ["-profile:v", "high"])
        self.assertIn("cabac=1:ref=4:bframes=1", args[3])
        self.assertIn("8x8dct=1", args[3])
        self.assertEqual(pp.x264_args({"profile": "Main"})[1], "main")
        self.assertIn("8x8dct=0", pp.x264_args({"profile": "Main"})[3])
        self.assertEqual(pp.x264_args({"profile": "Constrained Baseline"})[1], "baseline")
        # unknown show: High with 3 references and a B-frame, like Pluto's
        self.assertIn("ref=3:bframes=1", pp.x264_args({})[3])
        self.assertIn("ref=3:bframes=0", pp.x264_args({"refs": "x", "bframes": 0})[3])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_black_slate_has_show_settings(self):
        video = {"width": 320, "height": 180, "fps": "30/1", "profile": "High", "refs": 3, "bframes": 1}
        r = subprocess.run(pp.build_args(None, "va", {}, 2.0, True, video, pp.DEFAULT_AUDIO, at=10.0),
                           capture_output=True, check=True)
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=profile,has_b_frames", "-of", "json", "pipe:0"],
                             input=r.stdout, capture_output=True, check=True).stdout
        st = json.loads(out)["streams"][0]
        self.assertEqual(st["profile"], "High")
        self.assertEqual(st["has_b_frames"], 1)


class CaptionTextTest(unittest.TestCase):
    """Pluto's WebVTT (a real live cue sequence, 2026-11-04) -> roll-up words."""

    def test_parse_vtt_on_the_video_clock(self):
        cues = pp.parse_vtt(VTT)
        self.assertEqual(len(cues), 5)
        self.assertAlmostEqual(cues[0][0], 12 * 3600 + 56 * 60 + 34.755, places=3)
        self.assertEqual(cues[0][2], ["WE'RE DEALING WITH. BUT THE", "SPEAKER WAS FANTASTIC. THE THE"])
        self.assertEqual(cues[4][2][-1], "PEOPLE UNDER HIM")     # styling tags dropped

    def test_parse_vtt_timestamp_map(self):
        cues = pp.parse_vtt("WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000\n\n"
                            "00:00:01.000 --> 00:00:02.000\nHELLO\n")
        self.assertEqual(cues, [(11.0, 12.0, ["HELLO"])])

    def test_caption_words_sends_only_new_words(self):
        cues = pp.parse_vtt(VTT)
        t0 = cues[0][0]
        words = pp.caption_words(cues, t0, t0 + 5)
        # the first cue's last line, then a new line (PEOPLE) and UNDER grouped
        # with it (0.27 s apart), then HIM a second later on its own
        self.assertEqual([w for _, w in words], ["SPEAKER WAS FANTASTIC. THE THE", "PEOPLE UNDER", "HIM"])
        self.assertAlmostEqual(words[1][0] - t0, 0.366, places=3)
        # a window gets only its own words, but knows the cues before it
        self.assertEqual([w for _, w in pp.caption_words(cues, t0 + 1, t0 + 5)], ["HIM"])
        self.assertEqual(pp.caption_words(cues, t0 + 3, t0 + 5), [])

    def test_bottom_row_wraps_like_tttocea608(self):
        # rows end before a word that would take them past 31 characters,
        # wherever the captions themselves were split
        self.assertEqual(pp.bottom_row(["OFFICE IN THREE DECADES.", "SHE IS", "ALSO THE"]), "ALSO THE")
        self.assertEqual(pp.bottom_row(["OFFICE IN THREE DECADES. SHE IS"]), "OFFICE IN THREE DECADES. SHE IS")
        self.assertEqual(pp.bottom_row(["OFFICE IN THREE DECADES. SHE", "WAS"]), "WAS")    # 32 > 31
        self.assertEqual(pp.bottom_row([]), "")

    def test_srt(self):
        self.assertEqual(pp.srt([(10.5, "A B"), (11.0, "C")], 10.0, 15.0),
                         "1\n00:00:00,500 --> 00:00:01,000\nA B\n\n2\n00:00:01,000 --> 00:00:05,000\nC\n")

    def test_pick_subtitles(self):
        master = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Español",LANGUAGE="es",URI="s/es.m3u8"\n'
                  '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",DEFAULT=NO,URI="subtitle/subs/en/playlist.m3u8?sid=1"\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=1,SUBTITLES="subs"\nv.m3u8\n')
        self.assertEqual(pp.pick_subtitles(master, "https://h/p/m.m3u8"), "https://h/p/subtitle/subs/en/playlist.m3u8?sid=1")
        self.assertEqual(pp.pick_subtitles(MASTER, "https://h/p/m.m3u8"), "https://h/p/subtitle/en/playlist.m3u8?sid=S")
        self.assertIsNone(pp.pick_subtitles("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n", "https://h/p/m.m3u8"))


def pmt_entries(ts):
    """[(pid, es_info bytes)] of the first PMT in ts, and whether its CRC holds."""
    for off in range(0, len(ts) - 187, 188):
        if ts[off + 1] & 0x40 and ((ts[off + 1] & 0x1F) << 8 | ts[off + 2]) == pp.PMT_PID:
            p = off + 4 + (1 + ts[off + 4] if ts[off + 3] & 0x20 else 0)
            s = p + 1 + ts[p]
            end = s + 3 + ((ts[s + 1] & 0x0F) << 8 | ts[s + 2])
            q = s + 12 + ((ts[s + 10] & 0x0F) << 8 | ts[s + 11])
            out = []
            while q + 5 <= end - 4:
                ilen = (ts[q + 3] & 0x0F) << 8 | ts[q + 4]
                out.append(((ts[q + 1] & 0x1F) << 8 | ts[q + 2], bytes(ts[q + 5:q + 5 + ilen])))
                q += 5 + ilen
            return out, pp._crc32_mpeg2(ts[s:end]) == 0
    return None, False


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class SubtitleDeclarationTest(unittest.TestCase):
    def test_every_pmt_lists_the_dvb_track(self):
        seg = ts(["-f", "lavfi", "-t", "1", "-i", "testsrc=s=160x90:r=30", "-f", "lavfi", "-t", "1",
                  "-i", "sine", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                  "-streamid", "0:256", "-streamid", "1:257", "-mpegts_service_id", "1",
                  "-mpegts_pmt_start_pid", "4096"])
        out = pp.declare_subtitles(seg)
        self.assertEqual(len(out), len(seg))
        entries, ok = pmt_entries(out)
        self.assertTrue(ok)                                   # CRC recomputed
        self.assertEqual([pid for pid, _ in entries], [0x100, 0x101, pp.SUB_PID])
        self.assertEqual(dict(entries)[pp.SUB_PID], pp.SUBTITLING_DESCRIPTOR)
        self.assertEqual(pp.declare_subtitles(out), out)      # idempotent
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                            "pipe:0"], input=out, capture_output=True)
        self.assertEqual(r.stderr, b"")
        self.assertIn(b"dvb_subtitle", r.stdout.split())

    def test_dvb_srt(self):
        cues = [(9.0, 10.5, ["BEFORE"]), (10.5, 12.0, ["A & B >> C"]), (14.9, 16.0, ["RUNS ON"]), (20.0, 21.0, ["LATER"])]
        text = pp.dvb_srt(cues, 10.0, 15.0)
        self.assertIn("00:00:00,000 --> 00:00:00,500\nBEFORE", text)          # on screen at the start
        self.assertIn("A & B >> C", text)                                       # subparse escapes these itself
        self.assertIn("00:00:04,900 --> 00:00:06,000\nRUNS ON", text)         # not cut at the segment end
        self.assertIn("00:00:04,980 --> 00:00:04,990\n\u200b", text)          # clears a caption that ended
        self.assertNotIn("LATER", text)
        self.assertIsNone(pp.dvb_srt(cues, 30.0, 35.0))


class CaptionAdvanceTest(unittest.TestCase):
    def test_cues_are_read_earlier(self):
        up = Upstream()
        pp.fetch_bytes = up
        up.files["https://up/s.m3u8"] = b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:5,\nhttps://s/0.vtt\n"
        up.files["https://s/0.vtt"] = b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nHELLO\n"
        old = pp.CC_ADVANCE
        pp.CC_ADVANCE = 4.0
        try:
            s = pp.get_stream(pp.new_stream("https://up/v.m3u8", None, "https://up/s.m3u8"))
            pp.refresh_subs(s)
        finally:
            pp.CC_ADVANCE = old
        self.assertEqual(list(s.cues.values()), [(6.0, 8.0, ["HELLO"])])


class BurnTextTest(unittest.TestCase):
    def test_ass_doc_draws_overlapping_cues(self):
        doc = pp.ass_doc([(10.0, 12.5, ["HELLO {X}", "A\\B"]), (20.0, 21.0, ["LATER"]), (8.0, 9.5, ["EARLIER"])],
                         9.0, 14.0, 1280, 720)
        self.assertIn("PlayResX: 1280\nPlayResY: 720", doc)
        self.assertIn("Dialogue: 0,0:00:10.00,0:00:12.50,CC,,0,0,0,,HELLO (X)\\NA/B", doc)
        self.assertIn("0:00:08.00,0:00:09.50", doc)      # still on screen at 9 s
        self.assertNotIn("LATER", doc)
        self.assertEqual(pp.ass_time(46649.755), "12:57:29.76")

    def test_playlist_has_cc_twin(self):
        old = pp.CC_BURN_CHANNELS
        pp.CC_BURN_CHANNELS = {"a" * 24}
        try:
            body = pp.playlist("http://p", {"a" * 24: {"name": "News", "number": 5, "logo": "", "group": "G"},
                                            "b" * 24: {"name": "Other", "number": 6, "logo": "", "group": "G"}})
        finally:
            pp.CC_BURN_CHANNELS = old
        self.assertIn(",News (CC)\nhttp://p/cc/%s.m3u8" % ("a" * 24), body)
        self.assertNotIn("Other (CC)", body)


def can_burn():
    if not (shutil.which("ffmpeg") and shutil.which("fc-list")):
        return False
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " subtitles " in filters and bool(subprocess.run(["fc-list"], capture_output=True, text=True).stdout.strip())


@unittest.skipUnless(can_burn(), "needs ffmpeg with libass and a font")
class BurnPipelineTest(unittest.TestCase):
    """A (CC) stream: show segments re-encoded with the cue drawn in while it
    is on, timeline and layout unchanged."""

    def test_burned_in(self):
        up = Upstream()
        pp.fetch_bytes = up
        for i in range(2):
            up.files["https://s/%d.ts" % i] = ts([
                "-f", "lavfi", "-t", "5", "-i", "color=c=gray:s=320x180:r=30", "-f", "lavfi", "-t", "5",
                "-i", "sine=f=440:r=48000", "-c:v", "libx264", "-preset", "veryfast", "-bf", "1",
                "-c:a", "aac", "-output_ts_offset", str(94000 + 5 * i), "-mpegts_copyts", "1", "-copyts",
                "-streamid", "0:80", "-streamid", "1:81"])
        up.files["https://s/0.vtt"] = b"WEBVTT\n\n26:06:41.000 --> 26:06:43.000\nHELLO THERE\n"
        up.files["https://up/v.m3u8"] = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:1\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:00.000Z\n"
            "#EXTINF:5,\nhttps://s/0.ts\n#EXTINF:5,\nhttps://s/1.ts\n").encode()
        up.files["https://up/s.m3u8"] = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:5,\nhttps://s/0.vtt\n".encode()
        old = pp.BURN_DIR
        pp.BURN_DIR = tempfile.gettempdir()
        try:
            s = pp.get_stream(pp.new_stream("https://up/v.m3u8", None, "https://up/s.m3u8", burn=True))
            pp.media(s, "v")
            out = b"".join(pp.segment(s, "v", n) for n in (1, 2))
        finally:
            pp.BURN_DIR = old
        p = packets(out)
        self.assertEqual(sorted((st["id"], st["codec_type"]) for st in p["streams"]),
                         [("0x100", "video"), ("0x101", "audio")])
        for want in ("video", "audio"):
            idx = next(st["index"] for st in p["streams"] if st["codec_type"] == want)
            t = sorted(float(x["pts_time"]) for x in p["packets"] if x["stream_index"] == idx)
            self.assertLess(abs(t[0] - pp.TIMELINE_START), 0.1, want)
            self.assertLess(max(b - a for a, b in zip(t, t[1:])), 0.2, want)

        def caption_box(frame_no):
            # bottom-left strip, where the captions go: black box + white text
            px = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-vf",
                                 "select=eq(n\\,%d),crop=160:30:30:135,format=gray" % frame_no,
                                 "-frames:v", "1", "-fps_mode", "vfr", "-f", "rawvideo", "pipe:1"],
                                input=out, capture_output=True).stdout
            return min(px), max(px)
        self.assertLess(caption_box(60)[0], 40)      # 2 s in: cue on (94001-94003)
        self.assertGreater(caption_box(60)[1], 200)
        self.assertGreater(caption_box(240)[0], 90)   # 8 s in: plain gray


def have_caption_elements():
    if not (pp.have_gst() and shutil.which("ffmpeg")):
        return False
    return all(subprocess.run(["gst-inspect-1.0", e], capture_output=True).returncode == 0
               for e in ("tttocea608", "cccombiner", "h264ccinserter", "tsdemux", "mpegtsmux", "subparse",
                         "textrender", "dvbsubenc"))


@unittest.skipUnless(have_caption_elements(), "needs GStreamer with the closed-caption elements")
class CaptionPipelineTest(unittest.TestCase):
    """Show segments with B-frames at 94000 s plus a subtitle playlist: the
    output keeps its layout and timeline, and its video carries the words as
    CEA-608 in order."""

    WORDS = ["HELLO THERE", "THIS IS A TEST", "OF CLOSED", "CAPTIONS"]

    def test_captions_in_video(self):
        up = Upstream()
        pp.fetch_bytes = up
        for i in range(2):
            up.files["https://s/%d.ts" % i] = ts([
                "-f", "lavfi", "-t", "5", "-i", "testsrc=s=320x180:r=30", "-f", "lavfi", "-t", "5",
                "-i", "sine=f=440:r=48000", "-c:v", "libx264", "-preset", "veryfast", "-bf", "2",
                "-c:a", "aac", "-output_ts_offset", str(94000 + 5 * i), "-mpegts_copyts", "1", "-copyts",
                "-streamid", "0:80", "-streamid", "1:81"])
        line = ""
        cues = []
        for n, w in enumerate(self.WORDS):
            line = (line + " " + w).strip() if n != 2 else w     # a new line starts at word 2
            cues.append("%s --> %s\n%s\n" % (self._t(94000.5 + 2 * n), self._t(94001 + 2 * n), line))
        up.files["https://s/0.vtt"] = ("WEBVTT\n\n" + "\n".join(cues[:3])).encode()
        up.files["https://s/1.vtt"] = ("WEBVTT\n\n" + "\n".join(cues[3:])).encode()
        up.files["https://up/v.m3u8"] = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:1\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-11-04T05:00:00.000Z\n"
            "#EXTINF:5,\nhttps://s/0.ts\n#EXTINF:5,\nhttps://s/1.ts\n").encode()
        up.files["https://up/s.m3u8"] = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:1\n"
            "#EXTINF:5,\nhttps://s/0.vtt\n#EXTINF:5,\nhttps://s/1.vtt\n").encode()
        s = pp.get_stream(pp.new_stream("https://up/v.m3u8", None, "https://up/s.m3u8"))
        pp.media(s, "v")
        out = b"".join(pp.segment(s, "v", n) for n in (1, 2))
        p = packets(out)
        self.assertEqual(sorted((st["id"], st["codec_type"]) for st in p["streams"]),
                         [("0x100", "video"), ("0x101", "audio"), ("0x102", "subtitle")])
        for want in ("video", "audio"):
            idx = next(st["index"] for st in p["streams"] if st["codec_type"] == want)
            t = sorted(float(x["pts_time"]) for x in p["packets"] if x["stream_index"] == idx)
            self.assertLess(abs(t[0] - pp.TIMELINE_START), 0.1, want)
            self.assertLess(max(b - a for a, b in zip(t, t[1:])), 0.2, want)
            self.assertGreater(t[-1] - t[0], 9.5, want)
        # DVB pages at the cues' times on the output timeline (cues at 0.5,
        # 2.5, 4.5 and 6.5 s into the stream; the 4.5 s one is shown again
        # at the start of the second segment)
        sub = next(st["index"] for st in p["streams"] if st["codec_type"] == "subtitle")
        pages = sorted(round(float(x["pts_time"]) - pp.TIMELINE_START, 1) for x in p["packets"] if x["stream_index"] == sub)
        for want in (0.5, 2.5, 4.5, 5.0, 6.5):
            self.assertIn(want, pages, pages)
        self.assertTrue(all(0 <= x < 10 for x in pages), pages)
        with tempfile.NamedTemporaryFile(suffix=".ts") as f:
            f.write(out)
            f.flush()
            # real_time: write out every caption update (ffmpeg's decoder
            # otherwise only writes a roll-up line once it scrolls)
            r = subprocess.run(["ffmpeg", "-v", "error", "-real_time", "1", "-f", "lavfi",
                                "-i", "movie=%s[out0+subcc]" % f.name,
                                "-map", "0:s", "-f", "srt", "pipe:1"], capture_output=True, text=True)
        text = " ".join(r.stdout.replace("\n", " ").split())
        last = -1
        for w in self.WORDS:     # every word, in order, unscrambled
            at = text.find(w, last + 1)
            self.assertGreater(at, last, "%r not after position %d in %r" % (w, last, text))
            last = at

    @staticmethod
    def _t(sec):
        return "%02d:%02d:%06.3f" % (sec // 3600, sec // 60 % 60, sec % 60)



if __name__ == "__main__":
    unittest.main()

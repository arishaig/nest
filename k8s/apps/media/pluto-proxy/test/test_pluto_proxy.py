"""Offline tests: Pluto's APIs are stubbed. Run: python3 -m unittest discover -s test"""
import http.client
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pluto_proxy as pp  # noqa: E402

CID = "5" * 24
SESSION = {"token": "TOK", "params": "a=1&b=2", "stitcher": "https://stitch.example", "channels_api": "https://api.example"}


MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="subtitle/en/playlist.m3u8?sid=S"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="AD",CHARACTERISTICS="public.accessibility.describes-video",URI="audio/ad/audio.m3u8?sid=S"
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1042180,SUBTITLES="subs",AUDIO="audio"
1042180/playlist.m3u8?sid=S&jwt=TOK
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=3321280,SUBTITLES="subs",AUDIO="audio"
3321280/playlist.m3u8?sid=S&jwt=TOK
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=2294080,SUBTITLES="subs",AUDIO="audio"
2294080/playlist.m3u8?sid=S&jwt=TOK
"""

# Original audio is its own rendition: the bare variant would be silent.
SPLIT_AUDIO = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="subtitle/en/playlist.m3u8?sid=S"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English [Original]",DEFAULT=YES,AUTOSELECT=YES,URI="audio/orig/audio.m3u8?sid=S"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",CHARACTERISTICS="public.accessibility.describes-video",URI="audio/ad/audio.m3u8?sid=S"
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1042180,SUBTITLES="subs",AUDIO="audio"
1042180/playlist.m3u8?sid=S&jwt=TOK
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=3321280,SUBTITLES="subs",AUDIO="audio"
3321280/playlist.m3u8?sid=S&jwt=TOK
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


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pp.fetch_json = fake_fetch
        pp.boot = lambda: dict(SESSION)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), pp.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

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

    def test_redirect_falls_back_to_master_when_unreadable(self):
        pp.fetch_master = lambda url: (_ for _ in ()).throw(OSError("down"))
        status, h, _ = self.get("/ch/%s.m3u8" % CID)
        self.assertEqual(status, 302)
        self.assertEqual(h["Location"], "https://stitch.example/v2/stitch/hls/channel/%s/master.m3u8?a=1&b=2&jwt=TOK&masterJWTPassthrough=true" % CID)

    def test_redirect_picks_best_variant(self):
        pp.fetch_master = lambda url: (MASTER, "https://stitch.example/v2/stitch/hls/channel/x/master.m3u8?q=1")
        status, h, _ = self.get("/ch/%s.m3u8" % CID)
        self.assertEqual(status, 302)
        self.assertEqual(h["Location"], "https://stitch.example/v2/stitch/hls/channel/x/3321280/playlist.m3u8?sid=S&jwt=TOK")

    def test_split_audio_serves_trimmed_master(self):
        base = "https://h/p/master.m3u8"
        body = pp.trimmed_master(SPLIT_AUDIO, base)
        self.assertIn('NAME="English [Original]"', body)
        self.assertIn('URI="https://h/p/audio/orig/audio.m3u8?sid=S"', body)
        self.assertNotIn("describes-video", body)   # no audio-description track
        self.assertNotIn("SUBTITLES", body)
        self.assertEqual(body.count("#EXT-X-STREAM-INF"), 1)
        self.assertTrue(body.rstrip().endswith("https://h/p/3321280/playlist.m3u8?sid=S&jwt=TOK"))
        self.assertIsNone(pp.trimmed_master(MASTER, base))      # only an AD track: plain redirect
        self.assertIsNone(pp.trimmed_master("#EXTM3U\n", base))

    def test_handler_serves_trimmed_master_for_split_audio(self):
        pp.fetch_master = lambda url: (SPLIT_AUDIO, "https://stitch.example/v2/stitch/hls/channel/x/master.m3u8?q=1")
        status, h, body = self.get("/ch/%s.m3u8" % CID)
        self.assertEqual(status, 200)
        self.assertIn("mpegurl", h["Content-Type"])
        self.assertIn("audio/orig/audio.m3u8", body)

    def test_pick_variant_respects_cap_and_handles_empty(self):
        base = "https://h/p/master.m3u8"
        self.assertEqual(pp.pick_variant(MASTER, base, 2300000), "https://h/p/2294080/playlist.m3u8?sid=S&jwt=TOK")
        self.assertIsNone(pp.pick_variant(MASTER, base, 100))
        self.assertIsNone(pp.pick_variant("#EXTM3U\n", base))

    def test_unknown_and_malformed_ids_404(self):
        self.assertEqual(self.get("/ch/%s.m3u8" % ("6" * 24))[0], 404)  # filtered out
        self.assertEqual(self.get("/ch/..%2Fetc.m3u8")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_watch_script_fires_when_file_changes(self):
        import tempfile, time
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


if __name__ == "__main__":
    unittest.main()

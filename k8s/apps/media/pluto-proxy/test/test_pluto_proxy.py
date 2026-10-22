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

    def test_redirect_carries_token(self):
        status, h, _ = self.get("/ch/%s.m3u8" % CID)
        self.assertEqual(status, 302)
        self.assertEqual(h["Location"], "https://stitch.example/v2/stitch/hls/channel/%s/master.m3u8?a=1&b=2&jwt=TOK&masterJWTPassthrough=true" % CID)

    def test_unknown_and_malformed_ids_404(self):
        self.assertEqual(self.get("/ch/%s.m3u8" % ("6" * 24))[0], 404)  # filtered out
        self.assertEqual(self.get("/ch/..%2Fetc.m3u8")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_healthz(self):
        self.assertEqual(self.get("/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()

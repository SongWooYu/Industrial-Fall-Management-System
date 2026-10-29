import unittest
from unittest.mock import Mock, patch
from flask import Flask
from industrial_fall.webrtc import register_webrtc_routes


class WebRTCTest(unittest.TestCase):
    def setUp(self):
        self.patch = patch("industrial_fall.webrtc.build_opener")
        self.opener = self.patch.start().return_value
        self.addCleanup(self.patch.stop)
        self.upstream = self.opener.open.return_value.__enter__.return_value
        self.upstream.status = 201
        self.upstream.read.return_value = b"v=0\r\n"
        self.upstream.headers = {"Location":"/dahuaclean/whep/abcd-1234"}
        app = Flask(__name__)
        register_webrtc_routes(app)
        self.client = app.test_client()

    def test_same_origin_fixed_mapping_and_cleanup(self):
        response = self.client.post("/api/webrtc/dahua/whep", data="v=0", content_type="application/sdp")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Location"], "/api/webrtc/dahua/whep/abcd-1234")
        self.assertEqual(self.opener.open.call_args.args[0].full_url, "http://127.0.0.1:8889/dahuaclean/whep")
        self.upstream.status = 204
        self.upstream.headers = {}
        response = self.client.delete(response.headers["Location"])
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.opener.open.call_args.args[0].method, "DELETE")

    def test_allowlist_origin_size_and_fallback_error(self):
        self.assertEqual(self.client.post("/api/webrtc/unknown/whep", data="v=0", content_type="application/sdp").status_code, 404)
        self.assertEqual(self.client.post("/api/webrtc/dahua/whep", json={}).status_code, 415)
        self.assertEqual(self.client.post("/api/webrtc/dahua/whep", data="v=0", headers={"Origin":"https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/webrtc/dahua/whep", data="x"*131073, content_type="application/sdp").status_code, 413)
        self.upstream.headers = {"Location":"http://evil.example/dahuaclean/whep/abcd"}
        self.assertEqual(self.client.post("/api/webrtc/dahua/whep", data="v=0", content_type="application/sdp").status_code, 503)
        self.opener.open.side_effect = TimeoutError()
        self.assertEqual(self.client.post("/api/webrtc/dahua/whep", data="v=0", content_type="application/sdp").status_code, 503)

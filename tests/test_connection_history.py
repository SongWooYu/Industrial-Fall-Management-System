import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from industrial_fall.app import create_app
from industrial_fall.connection_history import ConnectionHistory, validate


def observation(**kw):
    return {"connection_id": "a" * 32, "event": "fallback", "transport": "mjpeg",
            "reason": "video_stalled", "overlay": False, **kw}


class HistoryTest(unittest.TestCase):
    def test_only_safe_fields_retained(self):
        history = ConnectionHistory()
        history.record(observation(sdp="PRIVATE", url="rtsp://SECRET", address="PRIVATE",
                                   timestamp="fake", source="server"))
        row = history.snapshot()["events"][0]
        self.assertEqual(row["source"], "browser")
        self.assertNotEqual(row["timestamp"], "fake")
        self.assertNotIn("PRIVATE", json.dumps(row))
        self.assertNotIn("SECRET", json.dumps(row))

    def test_invalid_payloads(self):
        for data in [[], None, observation(event=[]), observation(event="whep_accepted"),
                     observation(connection_id="x"*100), observation(reason="rtsp://secret"),
                     observation(width=float("nan")), observation(elapsed_ms=-1), observation(overlay=1)]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate(data)

    def test_memory_and_rate_bounded(self):
        with patch("industrial_fall.connection_history.time.monotonic", return_value=0) as clock:
            history = ConnectionHistory()
            for i in range(220):
                clock.return_value = i
                self.assertTrue(history.record(observation(sequence=i)))
            self.assertEqual(len(history.items), 200)
            self.assertEqual(history.snapshot()["events"][0]["sequence"], 219)
            for _ in range(80):
                history.record(observation())
            self.assertGreater(history.snapshot()["dropped"], 0)

    def test_restore_rotation_and_partial_last_line(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "connections.jsonl"
            with patch.object(ConnectionHistory, "max_bytes", 1000):
                history = ConnectionHistory(target)
                for i in range(40):
                    history.record(observation(sequence=i))
                history.close()
                files = list(Path(directory).glob("connections.jsonl*"))
                self.assertEqual(len(files), 3)
                self.assertLess(sum(p.stat().st_size for p in files), 3000)
                with target.open("ab") as out:
                    out.write(b'{"incomplete":')
                restored = ConnectionHistory(target)
                self.assertEqual(restored.snapshot()["events"][0]["sequence"], 39)
                self.assertTrue(restored.snapshot()["persistent"])
                restored.record(observation(sequence=40))
                restored.close()
                again = ConnectionHistory(target)
                self.assertEqual(again.snapshot()["events"][0]["sequence"], 40)
                again.close()

    def test_storage_failure_does_not_prevent_memory_logging(self):
        with tempfile.TemporaryDirectory() as directory, patch("industrial_fall.connection_history.DiagnosticLog", side_effect=OSError("full")):
            history = ConnectionHistory(Path(directory) / "log")
            history.record(observation())
            self.assertTrue(history.snapshot()["storage_error"])
            self.assertEqual(len(history.items), 1)

    def test_webrtc_lifecycle_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'connections.jsonl'
            history = ConnectionHistory(path)
            history.record(observation(event='attempt', transport='connecting', reason='automatic'))
            history.server('whep_accepted', 'a' * 32, 'domain_url', transport='webrtc', http_status=201)
            self.assertNotIn('rtc_playing', [r['event'] for r in history.items])
            history.record(observation(event='rtc_playing', transport='webrtc', reason='automatic',
                                       frames_decoded=21, width=1280, height=720, ice_state='connected',
                                       local_candidate='host', remote_candidate='relay', protocol='udp'))
            history.record(observation(event='ended', transport='webrtc', reason='pagehide'))
            history.close()
            restored = ConnectionHistory(path)
            try:
                rows = restored.snapshot()['events']
                self.assertEqual([r['event'] for r in rows], ['ended', 'rtc_playing', 'whep_accepted', 'attempt'])
                self.assertEqual(rows[1]['frames_decoded'], 21)
                self.assertEqual(rows[1]['remote_candidate'], 'relay')
                self.assertEqual(rows[1]['source'], 'browser')
                self.assertEqual(rows[2]['source'], 'server')
            finally:
                restored.close()


class HistoryAPITest(unittest.TestCase):
    def setUp(self):
        self.history = ConnectionHistory()
        self.app = create_app(Mock(), self.history)
        self.client = self.app.test_client()

    def test_browser_api_origin_validation_and_limits(self):
        self.assertEqual(self.client.post("/api/video-connections", json=observation()).status_code, 202)
        self.assertEqual(self.client.post("/api/video-connections", json=observation(), headers={"Origin":"https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/video-connections", data="text").status_code, 415)
        self.assertEqual(self.client.post("/api/video-connections", json=observation(extra="x"*2048)).status_code, 413)
        self.assertEqual(self.client.post("/api/video-connections", json={"event":[]}).status_code, 400)
        with self.history.lock:
            self.history.tokens = 0
        self.assertEqual(self.client.post("/api/video-connections", json=observation()).status_code, 429)

    def test_tunnel_origin_and_domain_not_external_claim(self):
        response = self.client.post("/api/video-connections", json=observation(),
                                    base_url="http://vision.example", environ_overrides={"REMOTE_ADDR":"127.0.0.1"},
                                    headers={"Origin":"https://vision.example", "X-Forwarded-Proto":"https"})
        self.assertEqual(response.status_code, 202)
        result = self.client.get("/api/video-connections")
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertEqual(result.json["events"][0]["access"], "domain_url")
        self.assertEqual(self.client.head("/api/video-connections").status_code, 200)

    def test_browser_api_records_webrtc_receipt_and_end(self):
        for event, fields in [('rtc_playing', {'frames_decoded': 14, 'ice_state': 'connected'}),
                              ('ended', {'reason': 'hidden'})]:
            response = self.client.post('/api/video-connections', json=observation(
                event=event, transport='webrtc', **fields))
            self.assertEqual(response.status_code, 202)
        rows = self.client.get('/api/video-connections').json['events']
        self.assertEqual([r['event'] for r in rows], ['ended', 'rtc_playing'])
        self.assertEqual(rows[1]['frames_decoded'], 14)
        self.assertTrue(all(r['transport'] == 'webrtc' for r in rows))

    def test_whep_success_is_not_video_success_and_error_keeps_code(self):
        with patch("industrial_fall.webrtc.build_opener") as build:
            client = create_app(Mock(), self.history).test_client()
            upstream = build.return_value.open.return_value.__enter__.return_value
            upstream.status = 201
            upstream.read.return_value = b"v=0\r\n"
            upstream.headers = {"Location":"/dahuaclean/whep/session1"}
            response = client.post("/api/webrtc/dahua/whep?cid=test", data="v=0", content_type="application/sdp")
            self.assertEqual(response.status_code, 201)
            self.assertEqual([r["event"] for r in self.history.items], ["whep_accepted"])
            build.return_value.open.side_effect = HTTPError("http://secret", 504, "private detail", {}, None)
            self.assertEqual(client.post("/api/webrtc/dahua/whep?cid=test", data="v=0", content_type="application/sdp").status_code, 503)
            last = self.history.snapshot()["events"][0]
            self.assertEqual(last["event"], "whep_failed")
            self.assertEqual(last["http_status"], 504)
            self.assertNotIn("secret", json.dumps(last))

    def test_mjpeg_direct_client_recorded_without_browser_telemetry(self):
        pipeline = Mock()
        pipeline.subscribe.return_value = True
        pipeline.stop_event.is_set.return_value = False
        pipeline.jpeg.return_value = (1, b"jpeg")
        client = create_app(pipeline, self.history).test_client()
        response = client.get("/stream/dahua?overlay=false", buffered=False)
        response.close()
        self.assertEqual([r["event"] for r in self.history.items], ["mjpeg_open", "mjpeg_sent", "mjpeg_closed"])
        pipeline.unsubscribe.assert_called_once_with(False)
        self.assertNotIn("mjpeg_decoded", [r["event"] for r in self.history.items])

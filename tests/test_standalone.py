import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from industrial_fall.app import create_app
from industrial_fall.config import load_config
from industrial_fall.events import EventJournal, FallTransitions
from industrial_fall.evidence import EvidenceStore
from industrial_fall.pipeline import FallPipeline, render_packet


def track(state="normal", **kwargs):
    return {"track_id": 1, "state": state, "event_id": 1, "visible": True,
            "valid": True, "age_frames": 60, "temporal_probability": .9, **kwargs}


class TransitionTest(unittest.TestCase):
    def test_lifecycle_dedup_and_no_transport(self):
        state = FallTransitions()
        observed = []
        for item in [track(), track("suspected"), track("fallen", fall_event=True),
                     track("fallen", fall_event=True), track("recovering"), track()]:
            observed.extend(k for k, _ in state.update([item], 1))
        self.assertEqual(observed, ["fall_suspected", "fall_confirmed", "fall_recovering", "fall_cleared"])

    def test_lost_person_is_not_proof_of_recovery(self):
        state = FallTransitions()
        state.update([track("fallen", fall_event=True)], 0)
        self.assertEqual(state.update([], 1), [])
        self.assertEqual(state.update([], 3.1), [("tracking_lost", None)])
        self.assertEqual(state.state, "unobserved")
        self.assertEqual(state.update([], 9), [])

    def test_another_fallen_track_prevents_clear(self):
        state = FallTransitions()
        state.update([track("fallen"), track("fallen", track_id=2)], 0)
        events = state.update([track(), track("fallen", track_id=2)], 1)
        self.assertNotIn("fall_cleared", [kind for kind, _ in events])
        self.assertEqual(state.state, "fallen")

    def test_bounded_ids_and_history(self):
        state = FallTransitions()
        journal = EventJournal()
        for tid in range(2000):
            for kind, item in state.update([track("fallen", track_id=tid, fall_event=True)], tid):
                journal.emit(kind, track=item, snapshot=(b"jpeg", {"frame_seq": tid}))
        self.assertEqual(len(state.seen), 256)
        self.assertEqual(len(state.previous), 1)
        self.assertEqual(len(journal.recent()), 50)
        self.assertEqual(len(journal.evidence.items), 50)
        copied = journal.recent()
        copied[0]["track"]["state"] = "tampered"
        self.assertEqual(journal.recent()[0]["track"]["state"], "fallen")

    def test_evidence_expiry_and_size(self):
        store = EvidenceStore(limit=2, ttl=1, max_bytes=4)
        key = "a" * 32
        self.assertFalse(store.put(key, b"large"))
        with patch("industrial_fall.evidence.time.monotonic", return_value=0):
            self.assertTrue(store.put(key, b"jpeg"))
        with patch("industrial_fall.evidence.time.monotonic", return_value=2):
            self.assertIsNone(store.get(key))


class StandaloneTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config/cameras.yaml")
        self.pipeline = FallPipeline(self.config)
        self.client = create_app(self.pipeline).test_client()

    def test_retired_routes_are_gone(self):
        for path in ["/api/faces", "/api/audio/status", "/api/fall-test", "/api/control-mode", "/stream/cam3"]:
            self.assertEqual(self.client.get(path).status_code, 404, path)
        for key in ["mqtt", "face_recognition", "pi4"]:
            self.assertNotIn(key, self.config)

    def test_health_reports_actual_readiness(self):
        self.assertEqual(self.client.get("/health").status_code, 503)
        now = time.monotonic()
        self.pipeline.latest = (1, now, np.zeros((100, 100, 3), np.uint8))
        self.pipeline.last_analysis = now
        self.pipeline.profile = "test"
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.pipeline.analysis_error = "inference failure"
        self.assertEqual(self.client.get("/health").status_code, 503)

    def test_natural_voice_is_single_default_and_static_clip_is_read_only(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('id="audio-voice"', html)
        self.assertNotIn('value="browser"', html)
        self.assertNotIn('id="audio-volume"', html)
        self.assertNotIn('id="connection-history"', html)
        self.assertIn('id="tab-robot"', html)
        response = self.client.get('/static/audio/fall-detected-ko-sunhi-v1.mp3')
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'audio/mpeg')
            self.assertGreater(len(response.data), 1000)
            self.assertLess(len(response.data), 256 * 1024)
            self.assertEqual(self.pipeline.events.recent(), [])
        finally:
            response.close()

    def test_video_warning_is_hidden_until_a_fall_state_arrives(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('현재 AI 판정', html)
        self.assertNotIn('video-alarm-detail', html)
        self.assertIn('aria-atomic="true" hidden><strong id="video-alarm-title"></strong>', html)

    def test_retired_voice_is_not_offered_or_served(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('value="espeak"', html)
        response = self.client.get('/static/audio/fall-detected-ko-espeak-v1.mp3')
        try:
            self.assertEqual(response.status_code, 404)
            self.assertEqual(self.pipeline.events.recent(), [])
        finally:
            response.close()

    def test_absent_invalid_and_stale_are_not_zero_risk(self):
        status = self.pipeline.status()
        self.assertIsNone(status["analysis"]["motion_score"])
        self.assertEqual(status["fall_state"], "unobserved")
        self.pipeline.tracks = [track("fallen")]
        self.assertEqual(self.pipeline.status()["person_count"], 0)

    def test_snapshots_local_only_and_same_frame(self):
        frame = np.zeros((120, 240, 3), np.uint8)
        pose = {"box": [20, 10, 90, 110], "confidence": .9,
                "keypoints": [[50, 50, .9]] * 17}
        packet = (42, time.monotonic(), frame, [pose])
        drawn, metadata = render_packet(packet, "fallen", evidence=True)
        self.assertEqual(metadata["frame_seq"], 42)
        self.assertTrue(metadata["pose_frame_matched"])
        self.assertEqual(metadata["overlay"]["visible_keypoints"], 17)
        self.assertEqual(np.count_nonzero(frame), 0)
        self.assertGreater(np.count_nonzero(drawn), 0)
        event = self.pipeline.events.emit("fall_confirmed", snapshot=(b"jpeg", metadata))
        response = self.client.get(event["snapshot_url"])
        self.assertEqual(response.data, b"jpeg")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_stream_subscriptions_bounded_and_released(self):
        for _ in range(6):
            self.assertTrue(self.pipeline.subscribe(True))
        self.assertEqual(self.client.get("/stream/dahua").status_code, 503)
        for _ in range(6):
            self.pipeline.unsubscribe(True)
        self.pipeline.jpeg_cache[True] = b"jpeg"
        response = self.client.get("/stream/dahua", buffered=False)
        self.assertEqual(self.pipeline.clients[True], 1)
        response.close()
        self.assertEqual(self.pipeline.clients[True], 0)

    def test_no_duplicate_frames_in_analysis(self):
        now = time.monotonic()
        self.pipeline.latest = (1, now, np.zeros((100, 100, 3), np.uint8))
        estimator, runtime = Mock(), Mock()
        estimator.estimate.return_value = []
        runtime.update.return_value = []
        thread = threading.Thread(target=self.pipeline._analysis_loop, args=(estimator, runtime))
        thread.start()
        time.sleep(.18)
        self.pipeline.stop_event.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(estimator.estimate.call_count, 1)

    def test_reconnect_does_not_bridge_motion_windows(self):
        now = time.monotonic()
        self.pipeline.latest = (1, now, np.zeros((100, 100, 3), np.uint8))
        estimator, runtime = Mock(), Mock()
        estimator.estimate.return_value = []
        def advance(*_args):
            self.pipeline.latest = (2, now + 2, np.zeros((100, 100, 3), np.uint8))
            return []
        runtime.update.side_effect = advance
        with self.assertRaisesRegex(RuntimeError, "Input gap"):
            self.pipeline._analysis_loop(estimator, runtime)
        self.assertEqual(estimator.estimate.call_count, 1)

    def test_confirmed_inference_produces_local_matched_evidence(self):
        now = time.monotonic()
        self.pipeline.latest = (42, now, np.zeros((540, 960, 3), np.uint8))
        estimator, runtime = Mock(), Mock()
        estimator.estimate.return_value = [{"box": [20, 10, 90, 110], "confidence": .9,
                                           "keypoints": [[50, 50, .9]] * 17}]
        def confirm(*_args):
            self.pipeline.stop_event.set()
            return [track("fallen", fall_event=True)]
        runtime.update.side_effect = confirm
        self.pipeline._analysis_loop(estimator, runtime)
        event = self.pipeline.events.recent()[0]
        self.assertEqual(event["type"], "fall_confirmed")
        self.assertEqual(event["snapshot"]["frame_seq"], 42)
        self.assertTrue(event["snapshot"]["pose_frame_matched"])
        self.assertGreater(len(self.client.get(event["snapshot_url"]).data), 1000)

    def test_snapshot_failure_does_not_drop_fall_event(self):
        self.pipeline.latest = (1, time.monotonic(), np.zeros((100, 100, 3), np.uint8))
        estimator, runtime = Mock(), Mock()
        estimator.estimate.return_value = []
        def confirm(*_args):
            self.pipeline.stop_event.set()
            return [track("fallen", fall_event=True)]
        runtime.update.side_effect = confirm
        with patch("industrial_fall.pipeline.render_packet", side_effect=RuntimeError("test")):
            with self.assertLogs("industrial_fall.pipeline", level="ERROR"):
                self.pipeline._analysis_loop(estimator, runtime)
        self.assertEqual(self.pipeline.events.recent()[0]["type"], "fall_confirmed")
        self.assertIsNone(self.pipeline.events.recent()[0]["snapshot_url"])

    def test_evidence_and_event_journal_persist_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal1 = EventJournal(storage_dir=tmpdir)
            event = journal1.emit("fall_confirmed", track=track("fallen"), snapshot=(b"persisted-jpeg", {"frame_seq": 7}))
            self.assertTrue(event["snapshot_url"].endswith("/snapshot.jpg"))

            journal2 = EventJournal(storage_dir=tmpdir)
            restored = journal2.recent()
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]["id"], event["id"])
            self.assertEqual(restored[0]["snapshot_url"], event["snapshot_url"])
            self.assertEqual(journal2.evidence.get(event["id"]), b"persisted-jpeg")

    def test_robot_monitor_routes_are_read_only(self):
        response = self.client.get("/api/robot/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("robot", payload)
        self.assertIn("vision", payload)
        self.assertIn("inspection", payload)
        records = payload["inspection"]["records"]
        self.assertGreaterEqual(len(records), 1)
        first = records[0]
        if first["overview_url"]:
            img_resp = self.client.get(first["overview_url"])
            try:
                self.assertEqual(img_resp.status_code, 200)
                self.assertEqual(img_resp.mimetype, "image/jpeg")
                self.assertGreater(len(img_resp.data), 1000)
            finally:
                img_resp.close()
        cfg_resp = self.client.post("/api/robot/config", json={"mode": "helmet", "stream_enabled": False})
        self.assertEqual(cfg_resp.status_code, 200)
        cfg_data = cfg_resp.get_json()
        self.assertEqual(cfg_data["vision"]["mode"], "helmet")
        self.assertFalse(cfg_data["robot"]["stream_enabled"])

    def test_car_ws_server_monitor_endpoints_are_isolated_from_control(self):
        import ast
        from pathlib import Path

        server_path = Path("src/RaspberryPi5_Car/Rasp5Car_source/Rasp5_code/car_ws_serverV3.py")
        source = server_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        funcs = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in (
            "verify_monitor_auth",
            "monitor_snapshot",
            "reject_monitor_commands",
            "monitor_status_handler",
            "monitor_video_handler",
        ):
            self.assertIn(name, funcs, f"Missing {name} in car_ws_serverV3.py")

        for handler_name in ("reject_monitor_commands", "monitor_status_handler", "monitor_video_handler"):
            handler_src = ast.get_source_segment(source, funcs[handler_name]) or ""
            self.assertNotIn("stop_request", handler_src)
            self.assertNotIn("control_socket", handler_src)
            self.assertNotIn("video_sockets", handler_src)

        monitor_client_src = Path("src/industrial_fall/robot_monitor.py").read_text(encoding="utf-8")
        self.assertIn('"/monitor/video"', monitor_client_src)
        self.assertIn('"/monitor/status"', monitor_client_src)
        self.assertIn('"X-Monitor-Key"', monitor_client_src)
        self.assertNotIn("send_json", monitor_client_src)


if __name__ == "__main__":
    unittest.main()

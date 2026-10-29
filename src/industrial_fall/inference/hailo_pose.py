from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np


def decode_hailo_pose_roi(
    roi: Any,
    frame_width: int,
    frame_height: int,
    min_confidence: float = 0.0,
) -> list[dict]:
    """Convert Hailo pose metadata to the existing estimator contract.

    Hailo detections use normalized frame coordinates. Landmark points are
    normalized inside the detection bbox, so both levels must be expanded to
    pixels before the EdgeFall tracker can use them.
    """
    import hailo

    poses: list[dict] = []
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    for detection in detections:
        if detection.get_label() != "person":
            continue
        confidence = float(detection.get_confidence())
        if confidence < min_confidence:
            continue

        landmarks = detection.get_objects_typed(hailo.HAILO_LANDMARKS)
        if not landmarks:
            continue
        points = landmarks[0].get_points()
        if len(points) != 17:
            continue

        bbox = detection.get_bbox()
        x1 = max(0.0, min(float(frame_width - 1), bbox.xmin() * frame_width))
        y1 = max(0.0, min(float(frame_height - 1), bbox.ymin() * frame_height))
        x2 = max(0.0, min(float(frame_width - 1), bbox.xmax() * frame_width))
        y2 = max(0.0, min(float(frame_height - 1), bbox.ymax() * frame_height))
        if x2 <= x1 or y2 <= y1:
            continue

        keypoints = []
        for point in points:
            x = (point.x() * bbox.width() + bbox.xmin()) * frame_width
            y = (point.y() * bbox.height() + bbox.ymin()) * frame_height
            keypoints.append(
                [
                    max(0.0, min(float(frame_width - 1), float(x))),
                    max(0.0, min(float(frame_height - 1), float(y))),
                    float(point.confidence()),
                ]
            )

        pose = {
            "confidence": confidence,
            "box": [x1, y1, x2, y2],
            "keypoints": keypoints,
        }
        unique_ids = detection.get_objects_typed(hailo.HAILO_UNIQUE_ID)
        if len(unique_ids) == 1:
            pose["hailo_track_id"] = int(unique_ids[0].get_id())
        poses.append(pose)
    return poses


class HailoPoseEstimator:
    """Synchronous BGR-frame adapter around the Hailo GStreamer pipeline.

    The pipeline has no overlay or display branch. It letterboxes a copy of
    the input for a YOLOv8 Pose HEF, aggregates metadata back onto the original
    frame coordinates, and returns only bbox/keypoint dictionaries.
    """

    backend_name = "hailo"
    postprocess_confidence_floor = 0.60

    def __init__(
        self,
        hef_path: str,
        postprocess_path: str,
        whole_buffer_path: str,
        confidence: float = 0.0,
        timeout_seconds: float = 3.0,
    ):
        for label, value in (
            ("Hailo HEF", hef_path),
            ("Hailo pose postprocess", postprocess_path),
            ("Hailo whole-buffer cropper", whole_buffer_path),
        ):
            if not Path(value).is_file():
                raise FileNotFoundError(f"{label} is missing: {value}")

        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        self._gst = Gst
        self.hef_path = str(hef_path)
        self.model_name = Path(self.hef_path).name
        self.postprocess_path = str(postprocess_path)
        self.whole_buffer_path = str(whole_buffer_path)
        self.confidence = max(0.0, min(1.0, float(confidence)))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.lock = threading.Lock()
        self._condition = threading.Condition()
        self._pipeline: Any | None = None
        self._appsrc: Any | None = None
        self._bus: Any | None = None
        self._width = 0
        self._height = 0
        self._generation = 0
        self._result: list[dict] = []
        self._callback_error: Exception | None = None
        self._sequence = 0

    def estimate(self, frame_bgr: np.ndarray) -> list[dict]:
        if frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
            raise ValueError("Hailo pose input must be a BGR HxWx3 frame")
        frame = np.ascontiguousarray(frame_bgr, dtype=np.uint8)
        height, width = frame.shape[:2]

        with self.lock:
            if self._pipeline is None:
                self._start(width, height)
            elif width != self._width or height != self._height:
                raise RuntimeError(
                    "Hailo pose frame size changed from "
                    f"{self._width}x{self._height} to {width}x{height}"
                )

            with self._condition:
                generation = self._generation
                self._callback_error = None

            duration = self._gst.SECOND // 10
            buffer = self._gst.Buffer.new_allocate(None, frame.nbytes, None)
            buffer.fill(0, frame.tobytes())
            buffer.pts = self._sequence * duration
            buffer.dts = buffer.pts
            buffer.duration = duration
            self._sequence += 1

            flow = self._appsrc.emit("push-buffer", buffer)
            if flow != self._gst.FlowReturn.OK:
                raise RuntimeError(f"Hailo appsrc push failed: {flow.value_nick}")

            with self._condition:
                completed = self._condition.wait_for(
                    lambda: self._generation > generation,
                    timeout=self.timeout_seconds,
                )
                if not completed:
                    self._raise_bus_error("Hailo pose inference timed out")
                if self._callback_error is not None:
                    raise RuntimeError(
                        f"Hailo pose metadata callback failed: {self._callback_error}"
                    ) from self._callback_error
                return [dict(pose) for pose in self._result]

    def close(self) -> None:
        with self.lock:
            if self._pipeline is None:
                return
            try:
                if self._appsrc is not None:
                    self._appsrc.emit("end-of-stream")
                self._pipeline.set_state(self._gst.State.PAUSED)
                self._pipeline.get_state(2 * self._gst.SECOND)
                self._pipeline.set_state(self._gst.State.READY)
                self._pipeline.get_state(2 * self._gst.SECOND)
            finally:
                self._pipeline.set_state(self._gst.State.NULL)
                self._pipeline.get_state(5 * self._gst.SECOND)
                self._pipeline = None
                self._appsrc = None
                self._bus = None

    def _start(self, width: int, height: int) -> None:
        quoted_hef = self._quote_pipeline_value(self.hef_path)
        quoted_postprocess = self._quote_pipeline_value(self.postprocess_path)
        quoted_whole_buffer = self._quote_pipeline_value(self.whole_buffer_path)
        description = " ".join(
            [
                "appsrc name=hailo_pose_source is-live=true block=true format=time do-timestamp=false",
                f"caps=video/x-raw,format=BGR,width={width},height={height},framerate=10/1",
                "! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream",
                "! videoconvert n-threads=2 qos=false",
                "! video/x-raw,format=RGB,pixel-aspect-ratio=1/1",
                "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0",
                "! hailocropper name=hailo_pose_crop "
                f"so-path={quoted_whole_buffer} function-name=create_crops "
                "use-letterbox=true resize-method=inter-area internal-offset=true",
                "hailoaggregator name=hailo_pose_agg",
                "hailo_pose_crop. ! queue name=hailo_pose_bypass max-size-buffers=2 max-size-bytes=0 max-size-time=0",
                "! hailo_pose_agg.sink_0",
                "hailo_pose_crop. ! queue name=hailo_pose_infer_input max-size-buffers=2 max-size-bytes=0 max-size-time=0",
                "! videoscale n-threads=2 qos=false",
                "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0",
                "! video/x-raw,pixel-aspect-ratio=1/1",
                "! videoconvert n-threads=2 qos=false",
                "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0",
                "! hailonet name=hailo_pose_net "
                f"hef-path={quoted_hef} batch-size=1 "
                "vdevice-group-id=industrial-fall force-writable=true",
                "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0",
                "! hailofilter name=hailo_pose_filter "
                f"so-path={quoted_postprocess} "
                "function-name=filter_letterbox qos=false",
                "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0",
                "! hailo_pose_agg.sink_1",
                "hailo_pose_agg. ! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0",
                "! identity name=hailo_pose_metadata signal-handoffs=true",
                "! fakesink sync=false async=false qos=false",
            ]
        )
        pipeline = self._gst.parse_launch(description)
        appsrc = pipeline.get_by_name("hailo_pose_source")
        cropper = pipeline.get_by_name("hailo_pose_crop")
        network = pipeline.get_by_name("hailo_pose_net")
        postprocess = pipeline.get_by_name("hailo_pose_filter")
        identity = pipeline.get_by_name("hailo_pose_metadata")
        if any(item is None for item in (appsrc, cropper, network, postprocess, identity)):
            pipeline.set_state(self._gst.State.NULL)
            raise RuntimeError("Hailo pose GStreamer elements are unavailable")

        cropper.set_property("so-path", self.whole_buffer_path)
        cropper.set_property("function-name", "create_crops")
        network.set_property("hef-path", self.hef_path)
        postprocess.set_property("so-path", self.postprocess_path)
        identity.connect("handoff", self._on_handoff)

        result = pipeline.set_state(self._gst.State.PLAYING)
        if result == self._gst.StateChangeReturn.FAILURE:
            pipeline.set_state(self._gst.State.NULL)
            raise RuntimeError("Hailo pose GStreamer pipeline failed to start")

        self._pipeline = pipeline
        self._appsrc = appsrc
        self._bus = pipeline.get_bus()
        self._width = width
        self._height = height

    @staticmethod
    def _quote_pipeline_value(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _on_handoff(self, _identity: Any, buffer: Any) -> None:
        try:
            import hailo

            roi = hailo.get_roi_from_buffer(buffer)
            result = decode_hailo_pose_roi(
                roi,
                self._width,
                self._height,
                self.confidence,
            )
            error = None
        except Exception as exc:
            result = []
            error = exc

        with self._condition:
            self._result = result
            self._callback_error = error
            self._generation += 1
            self._condition.notify_all()

    def _raise_bus_error(self, fallback: str) -> None:
        if self._bus is not None:
            message = self._bus.pop_filtered(
                self._gst.MessageType.ERROR | self._gst.MessageType.EOS
            )
            if message is not None and message.type == self._gst.MessageType.ERROR:
                error, debug = message.parse_error()
                detail = f": {debug}" if debug else ""
                raise RuntimeError(f"{fallback}: {error.message}{detail}")
        raise RuntimeError(fallback)

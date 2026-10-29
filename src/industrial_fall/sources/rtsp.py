from __future__ import annotations

import os
from typing import Any

import cv2
import numpy as np

from .base import CameraSource


class RtspSource(CameraSource):
    """RTSP source with a latest-frame-first GStreamer path.

    OpenCV/FFmpeg can keep decoded RTSP frames in an internal queue when the
    consumer briefly slows down. That is useful for file playback but causes
    multi-second live-view delay. The GStreamer appsink is deliberately
    limited to one frame and drops the old frame when the consumer is busy.
    """

    def __init__(
        self,
        url: str,
        transport: str = "tcp",
        low_latency: bool = False,
        backend: str = "auto",
        decode_width: int = 0,
        decode_height: int = 0,
        latency_ms: int = 100,
        codec: str = "h264",
    ):
        self.url = url
        self.transport = transport
        self.low_latency = low_latency
        self.backend = backend.strip().lower()
        self.decode_width = max(0, int(decode_width))
        self.decode_height = max(0, int(decode_height))
        self.latency_ms = max(0, int(latency_ms))
        self.codec = codec.strip().lower()

        self.capture: cv2.VideoCapture | None = None
        self.backend_active = "closed"
        self.buffer_age_ms = 0.0

        self._gst: Any | None = None
        self._gst_pipeline: Any | None = None
        self._gst_sink: Any | None = None
        self._gst_bus: Any | None = None

    def open(self) -> None:
        if not self.url:
            raise RuntimeError("DAHUA_RTSP_URL is not configured")

        if self.backend not in {"auto", "gstreamer", "opencv"}:
            raise RuntimeError(f"Unsupported RTSP backend: {self.backend}")

        if self.backend in {"auto", "gstreamer"}:
            try:
                self._open_gstreamer()
                return
            except Exception:
                self._close_gstreamer()
                if self.backend == "gstreamer":
                    raise

        self._open_opencv()

    def _open_gstreamer(self) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)

        codec_elements = {
            "h264": ("rtph264depay", "h264parse", "avdec_h264"),
            "h265": ("rtph265depay", "h265parse", "avdec_h265"),
        }
        if self.codec not in codec_elements:
            raise RuntimeError(f"Unsupported RTSP codec: {self.codec}")

        depay, parser, decoder = codec_elements[self.codec]
        caps = ["video/x-raw", "format=BGR", "pixel-aspect-ratio=1/1"]
        if self.decode_width > 0:
            caps.append(f"width={self.decode_width}")
        if self.decode_height > 0:
            caps.append(f"height={self.decode_height}")

        protocol = "tcp" if self.transport.lower() == "tcp" else "udp"
        description = " ".join(
            [
                "rtspsrc name=source",
                f"protocols={protocol}",
                f"latency={self.latency_ms}",
                "drop-on-latency=true",
                "buffer-mode=none",
                "tcp-timeout=5000000",
                "!",
                depay,
                "!",
                parser,
                "!",
                decoder,
                "max-threads=2",
                "! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream",
                "! videoconvert",
                "! videoscale",
                "! " + ",".join(caps),
                "! appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false wait-on-eos=false",
            ]
        )

        pipeline = Gst.parse_launch(description)
        source = pipeline.get_by_name("source")
        sink = pipeline.get_by_name("sink")
        if source is None or sink is None:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("GStreamer RTSP pipeline elements are unavailable")

        # Keep credentials out of the pipeline description and safely handle
        # URI characters such as '&' and '@'.
        source.set_property("location", self.url)

        result = pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("GStreamer RTSP pipeline failed to start")

        self._gst = Gst
        self._gst_pipeline = pipeline
        self._gst_sink = sink
        self._gst_bus = pipeline.get_bus()
        self.backend_active = "gstreamer"
        self.buffer_age_ms = 0.0

    def _open_opencv(self) -> None:
        old_options = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        options = f"rtsp_transport;{self.transport}"
        if self.low_latency:
            options += "|fflags;nobuffer|flags;low_delay|max_delay;0|reorder_queue_size;0"
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = options
        try:
            capture = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        finally:
            if old_options is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = old_options

        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError("Dahua RTSP stream open failed")
        self.capture = capture
        self.backend_active = "opencv"
        self.buffer_age_ms = 0.0

    def read(self) -> np.ndarray:
        if self._gst_pipeline is not None:
            return self._read_gstreamer()

        if self.capture is None:
            raise RuntimeError("Dahua RTSP stream is not open")
        ok, frame = self.capture.read()
        if not ok or frame is None:
            raise RuntimeError("Dahua RTSP frame read failed")
        self.buffer_age_ms = 0.0
        return frame

    def _read_gstreamer(self) -> np.ndarray:
        Gst = self._gst
        pipeline = self._gst_pipeline
        sink = self._gst_sink
        if Gst is None or pipeline is None or sink is None:
            raise RuntimeError("GStreamer RTSP stream is not open")

        sample = sink.emit("try-pull-sample", 5 * Gst.SECOND)
        if sample is None:
            message = None
            if self._gst_bus is not None:
                message = self._gst_bus.pop_filtered(
                    Gst.MessageType.ERROR | Gst.MessageType.EOS
                )
            if message is not None and message.type == Gst.MessageType.ERROR:
                error, _ = message.parse_error()
                raise RuntimeError(f"GStreamer RTSP read failed: {error.message}")
            raise RuntimeError("GStreamer RTSP frame timeout")

        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        buffer = sample.get_buffer()

        mapped, map_info = buffer.map(Gst.MapFlags.READ)
        if not mapped:
            raise RuntimeError("GStreamer RTSP frame map failed")
        try:
            frame = np.frombuffer(map_info.data, dtype=np.uint8).reshape(
                (height, width, 3)
            ).copy()
        finally:
            buffer.unmap(map_info)

        pts = int(buffer.pts)
        clock = pipeline.get_clock()
        if clock is not None and pts != int(Gst.CLOCK_TIME_NONE):
            running_time = max(0, int(clock.get_time()) - int(pipeline.get_base_time()))
            self.buffer_age_ms = max(0.0, (running_time - pts) / float(Gst.MSECOND))
        else:
            self.buffer_age_ms = 0.0

        return frame

    def close(self) -> None:
        self._close_gstreamer()

        if self.capture is not None:
            self.capture.release()
            self.capture = None

        self.backend_active = "closed"
        self.buffer_age_ms = 0.0

    def _close_gstreamer(self) -> None:
        if self._gst_pipeline is not None and self._gst is not None:
            self._gst_pipeline.set_state(self._gst.State.NULL)
        self._gst_pipeline = None
        self._gst_sink = None
        self._gst_bus = None
        self._gst = None

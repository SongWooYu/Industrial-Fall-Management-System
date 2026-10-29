#!/usr/bin/env python3
"""Read-only preflight: packages, native ABI and exact pose artifact."""
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    import cv2
    import gi
    import hailo
    import flask
    from industrial_fall.config import load_config
    from industrial_fall.edgefall_shadow import EdgeFallRuntime
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    for name in ("rtspsrc", "avdec_h264", "appsink", "appsrc", "hailonet", "hailocropper", "hailofilter", "hailoaggregator"):
        if not Gst.ElementFactory.find(name):
            raise SystemExit(f"Missing GStreamer plugin: {name}")
    config = load_config(ROOT / "config/cameras.yaml")
    if not config["camera"]["url"].startswith("rtsp"):
        raise SystemExit("Configure DAHUA_RTSP_URL in .env first")
    fall = config["fall_detection"]
    for key in ("model", "hailo_postprocess", "hailo_whole_buffer"):
        if not Path(fall[key]).is_file():
            raise SystemExit(f"Missing artifact: {key}")
    checksum = hashlib.sha256(Path(fall["model"]).read_bytes()).hexdigest()
    if checksum != "e19856699ed47cf866d23265827f960b263f287dab5e54e82c7ce37e12525a2d":
        raise SystemExit("Pose HEF checksum mismatch")
    with EdgeFallRuntime(fall["edgefall"]["library"]) as runtime:
        if runtime.profile != "edgefallkit-hailo8-yolov8s-pose-v1+units-separated-v6":
            raise SystemExit("Unvalidated EdgeFall profile")
        print("Preflight OK:", runtime.profile)
    print("No camera opened; no inference, event or external message generated.")


if __name__ == "__main__":
    main()

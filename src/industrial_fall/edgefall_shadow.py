from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any


class _Detection(ctypes.Structure):
    _fields_ = [
        ("box", ctypes.c_float * 5),
        ("keypoints", ctypes.c_float * 51),
    ]


class _Track(ctypes.Structure):
    _fields_ = [
        ("track_id", ctypes.c_uint64),
        ("event_id", ctypes.c_uint64),
        ("state", ctypes.c_int32),
        ("age", ctypes.c_int32),
        ("missed", ctypes.c_int32),
        ("valid", ctypes.c_int32),
        ("fall_detected", ctypes.c_int32),
        ("fall_event", ctypes.c_int32),
        ("temporal_positive", ctypes.c_int32),
        ("temporal_probability", ctypes.c_float),
        ("box", ctypes.c_float * 5),
        ("hip_y", ctypes.c_float),
        ("torso_angle_deg", ctypes.c_float),
        ("bbox_aspect_ratio", ctypes.c_float),
        ("hip_drop_speed", ctypes.c_float),
        ("hip_drop_distance", ctypes.c_float),
        ("geometry_candidate", ctypes.c_int32),
        ("motion_continuous", ctypes.c_int32),
    ]


STATE_NAMES = ("normal", "suspected", "fallen", "recovering")


class EdgeFallRuntime:
    """Production adapter for EdgeFallKit's Hailo-specific temporal profile.

    The native ABI keeps its historical ``edgefall_shadow_*`` symbol names so
    the validated binary remains reproducible. Local events live in Python.
    """

    def __init__(self, library_path: str, keypoint_confidence: float = 0.25):
        path = Path(library_path)
        if not path.is_file():
            raise FileNotFoundError(f"EdgeFall runtime library is missing: {path}")

        self._library = ctypes.CDLL(str(path))
        abi = getattr(self._library, "edgefall_shadow_abi_version", None)
        if abi is None:
            raise RuntimeError("EdgeFall ABI is outdated; rebuild with setup_edgefall_shadow.sh")
        abi.argtypes = []
        abi.restype = ctypes.c_int
        if abi() != 2:
            raise RuntimeError("EdgeFall ABI mismatch; rebuild native library")
        self._library.edgefall_shadow_create.argtypes = [ctypes.c_float]
        self._library.edgefall_shadow_create.restype = ctypes.c_void_p
        self._library.edgefall_shadow_destroy.argtypes = [ctypes.c_void_p]
        self._library.edgefall_shadow_profile.argtypes = []
        self._library.edgefall_shadow_profile.restype = ctypes.c_char_p
        self._library.edgefall_shadow_last_error.argtypes = [ctypes.c_void_p]
        self._library.edgefall_shadow_last_error.restype = ctypes.c_char_p
        self._library.edgefall_shadow_update.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Detection),
            ctypes.c_size_t,
            ctypes.c_double,
            ctypes.POINTER(_Track),
            ctypes.c_size_t,
        ]
        self._library.edgefall_shadow_update.restype = ctypes.c_size_t

        self._handle = self._library.edgefall_shadow_create(
            ctypes.c_float(keypoint_confidence)
        )
        if not self._handle:
            raise RuntimeError("EdgeFall runtime tracker initialization failed")
        raw_profile = self._library.edgefall_shadow_profile()
        self.profile = raw_profile.decode("utf-8") if raw_profile else "edgefallkit"

    def close(self) -> None:
        if self._handle:
            self._library.edgefall_shadow_destroy(self._handle)
            self._handle = None

    def update(
        self,
        poses: list[dict[str, Any]],
        frame_width: int,
        frame_height: int,
        timestamp_sec: float,
    ) -> list[dict[str, Any]]:
        if not self._handle:
            raise RuntimeError("EdgeFall runtime tracker is closed")
        width = max(1.0, float(frame_width))
        height = max(1.0, float(frame_height))

        values = (_Detection * len(poses))()
        for index, pose in enumerate(poses):
            x1, y1, x2, y2 = (float(value) for value in pose["box"])
            values[index].box[:] = (
                ((x1 + x2) * 0.5) / width,
                ((y1 + y2) * 0.5) / height,
                max(0.0, x2 - x1) / width,
                max(0.0, y2 - y1) / height,
                float(pose["confidence"]),
            )
            flat: list[float] = []
            for x, y, confidence in pose["keypoints"]:
                flat.extend((float(x) / width, float(y) / height, float(confidence)))
            if len(flat) != 51:
                raise ValueError("EdgeFall requires exactly 17 COCO keypoints")
            values[index].keypoints[:] = flat

        capacity = max(16, len(poses) + 16)
        output = (_Track * capacity)()
        count = int(
            self._library.edgefall_shadow_update(
                self._handle,
                values,
                len(poses),
                float(timestamp_sec),
                output,
                capacity,
            )
        )
        raw_error = self._library.edgefall_shadow_last_error(self._handle)
        error = raw_error.decode("utf-8") if raw_error else ""
        if error:
            raise RuntimeError(error)

        tracks: list[dict[str, Any]] = []
        for item in output[:count]:
            state_index = int(item.state)
            state = (
                STATE_NAMES[state_index]
                if 0 <= state_index < len(STATE_NAMES)
                else "normal"
            )
            cx, cy, box_width, box_height, score = map(float, item.box)
            tracks.append(
                {
                    "track_id": int(item.track_id),
                    "event_id": int(item.event_id),
                    "state": state,
                    "age_frames": int(item.age),
                    "missed_frames": int(item.missed),
                    "visible": int(item.missed) == 0,
                    "valid": bool(item.valid),
                    "fall_detected": bool(item.fall_detected),
                    "fall_event": bool(item.fall_event),
                    "temporal_positive": bool(item.temporal_positive),
                    "temporal_probability": round(float(item.temporal_probability), 4),
                    "bbox": [
                        int(round((cx - box_width * 0.5) * width)),
                        int(round((cy - box_height * 0.5) * height)),
                        int(round((cx + box_width * 0.5) * width)),
                        int(round((cy + box_height * 0.5) * height)),
                    ],
                    "confidence": round(score, 4),
                    "hip_y": round(float(item.hip_y), 4),
                    "torso_angle_deg": round(float(item.torso_angle_deg), 2),
                    "bbox_aspect_ratio": round(float(item.bbox_aspect_ratio), 4),
                    "hip_drop_speed": round(float(item.hip_drop_speed), 4),
                    "hip_drop_distance": round(float(item.hip_drop_distance), 4),
                    "geometry_candidate": bool(item.geometry_candidate),
                    "motion_continuous": bool(item.motion_continuous),
                    "decision_reason": (
                        "temporal_and_geometry" if item.fall_event
                        else "alarm_latched" if item.fall_detected
                        else "insufficient_pose" if not item.valid or item.missed
                        else "pose_reacquired" if not item.motion_continuous
                        else "model_not_confirmed" if item.geometry_candidate and not item.temporal_positive
                        else "motion_candidate" if state == "suspected"
                        else "motion_not_confirmed" if item.temporal_positive
                        else "normal"
                    ),
                }
            )
        return tracks

    def __enter__(self) -> "EdgeFallRuntime":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


# Historical research scripts may still import the former class name.
EdgeFallShadow = EdgeFallRuntime

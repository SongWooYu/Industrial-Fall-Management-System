from __future__ import annotations

import unittest

try:
    import hailo

    from industrial_fall.inference.hailo_pose import decode_hailo_pose_roi
except (ImportError, ModuleNotFoundError):
    hailo = None
    decode_hailo_pose_roi = None


class FakePoint:
    def __init__(self, x: float, y: float, confidence: float):
        self._x = x
        self._y = y
        self._confidence = confidence

    def x(self):
        return self._x

    def y(self):
        return self._y

    def confidence(self):
        return self._confidence


class FakeBbox:
    def xmin(self):
        return 0.25

    def ymin(self):
        return 0.20

    def xmax(self):
        return 0.75

    def ymax(self):
        return 0.80

    def width(self):
        return 0.50

    def height(self):
        return 0.60


class FakeLandmarks:
    def get_points(self):
        return [FakePoint(0.5, 0.5, 0.9) for _ in range(17)]


class FakeDetection:
    def get_label(self):
        return "person"

    def get_confidence(self):
        return 0.8

    def get_bbox(self):
        return FakeBbox()

    def get_objects_typed(self, object_type):
        if object_type == hailo.HAILO_LANDMARKS:
            return [FakeLandmarks()]
        return []


class FakeRoi:
    def get_objects_typed(self, object_type):
        if object_type == hailo.HAILO_DETECTION:
            return [FakeDetection()]
        return []


@unittest.skipIf(hailo is None, "Hailo Python bindings are not installed")
class HailoPoseMetadataTest(unittest.TestCase):
    def test_expands_bbox_relative_landmarks_to_frame_pixels(self):
        poses = decode_hailo_pose_roi(FakeRoi(), 960, 540, 0.6)

        self.assertEqual(len(poses), 1)
        self.assertEqual(poses[0]["box"], [240.0, 108.0, 720.0, 432.0])
        self.assertEqual(len(poses[0]["keypoints"]), 17)
        self.assertAlmostEqual(poses[0]["keypoints"][0][0], 480.0)
        self.assertAlmostEqual(poses[0]["keypoints"][0][1], 270.0)
        self.assertAlmostEqual(poses[0]["keypoints"][0][2], 0.9)

    def test_applies_additional_confidence_filter(self):
        self.assertEqual(decode_hailo_pose_roi(FakeRoi(), 960, 540, 0.9), [])


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import subprocess
import tempfile
import unittest


class NativePolicyTest(unittest.TestCase):
    @unittest.skipUnless(Path("runtime/build/edgefall_shadow/tracker_hailo.cpp").exists(), "build EdgeFall first")
    def test_geometry_normalized_but_frozen_model_receives_pixel_hip_y(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "feature-test")
            sources = ["tracker_hailo.cpp", "pose.cpp", "fall_detector.cpp", "temporal_classifier.cpp"]
            subprocess.run(["c++", "-std=c++17", "-O2", "-Iruntime/build/edgefall_shadow",
                            "tests/native_edgefall_features.cpp",
                            *["runtime/build/edgefall_shadow/" + s for s in sources],
                            "-o", target], check=True)
            subprocess.run([target], check=True)

    @unittest.skipUnless(Path("runtime/build/edgefall_shadow/fall_detector.cpp").exists(), "build EdgeFall first")
    def test_native_confirmation_requires_model_and_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "policy-test")
            subprocess.run(["c++", "-std=c++17", "-include", "initializer_list",
                            "-Iruntime/build/edgefall_shadow", "tests/native_edgefall_policy.cpp",
                            "runtime/build/edgefall_shadow/fall_detector.cpp", "-o", target], check=True)
            subprocess.run([target], check=True)

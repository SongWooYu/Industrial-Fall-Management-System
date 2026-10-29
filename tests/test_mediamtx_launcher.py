from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts/run_mediamtx.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("run_mediamtx", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MediaMtxLauncherTest(unittest.TestCase):
    def test_camera_secret_is_in_environment_not_process_arguments(self):
        launcher = load_launcher()
        calls = []

        with (
            patch.object(launcher, "dotenv_values", return_value={"DAHUA_RTSP_URL": "rtsp://user:secret@camera/live"}),
            patch.object(launcher.os, "execve", side_effect=lambda *args: calls.append(args)),
        ):
            launcher.main()

        executable, arguments, environment = calls[0]
        self.assertEqual(executable, launcher.MEDIAMTX)
        self.assertNotIn("secret", " ".join(arguments))
        self.assertEqual(
            environment["MTX_PATHS_DAHUACLEAN_SOURCE"],
            "rtsp://user:secret@camera/live",
        )

    def test_invalid_camera_url_stops_before_exec(self):
        launcher = load_launcher()
        with (
            patch.object(launcher, "dotenv_values", return_value={}),
            patch.object(launcher.os, "execve") as execute,
        ):
            with self.assertRaises(SystemExit):
                launcher.main()
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()

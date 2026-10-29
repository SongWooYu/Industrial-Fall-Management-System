#!/usr/bin/env python3
"""Start MediaMTX with private camera sources loaded from the project dotenv."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


REPOSITORY = Path(__file__).resolve().parents[1]
MEDIAMTX = Path("/usr/local/bin/mediamtx")
CONFIG = REPOSITORY / "config/mediamtx.yml"


def main() -> None:
    camera_url = str(dotenv_values(REPOSITORY / ".env").get("DAHUA_RTSP_URL", ""))
    if not camera_url.startswith(("rtsp://", "rtsps://")) or "\n" in camera_url:
        raise SystemExit("DAHUA_RTSP_URL is missing or invalid")

    environment = os.environ.copy()
    environment["MTX_PATHS_DAHUACLEAN_SOURCE"] = camera_url
    os.execve(
        MEDIAMTX,
        [str(MEDIAMTX), str(CONFIG)],
        environment,
    )


if __name__ == "__main__":
    main()

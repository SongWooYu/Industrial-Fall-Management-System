from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml


ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).resolve()
    project_root = config_path.parent.parent
    _load_dotenv(project_root / ".env")

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data = _expand(data)
    data["project_root"] = str(project_root)

    fall_config = data.get("fall_detection", {})
    if fall_config.get("enabled", False):
        for model_key in (
            "model",
            "hailo_postprocess",
            "hailo_whole_buffer",
        ):
            if not fall_config.get(model_key):
                continue
            pose_model_path = Path(fall_config[model_key])
            if not pose_model_path.is_absolute():
                fall_config[model_key] = str(project_root / pose_model_path)
        edgefall_config = fall_config.get("edgefall", {})
        if edgefall_config.get("library"):
            library_path = Path(edgefall_config["library"])
            if not library_path.is_absolute():
                edgefall_config["library"] = str(project_root / library_path)

    diagnostics = fall_config.get("edgefall", {}).get("diagnostics", {})
    if diagnostics.get("path"):
        diagnostics["path"] = str(project_root / diagnostics["path"])
    if float(fall_config.get("analysis_fps", 15)) != 15:
        raise ValueError("The validated 48-frame EdgeFall profile requires a 15 FPS target")
    return data

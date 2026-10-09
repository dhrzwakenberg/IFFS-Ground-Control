from __future__ import annotations

import json
import os
from pathlib import Path

from .models import AppConfig


def default_config_path() -> Path:
    app_data = os.environ.get("APPDATA")
    root = Path(app_data) if app_data else Path.home() / ".config"
    return root / "IFFS Ground Control" / "config.json"


def load_config(path: Path | None = None) -> tuple[AppConfig, str | None]:
    config_path = path or default_config_path()
    if not config_path.exists():
        return AppConfig(), None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return AppConfig.from_dict(data), None
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return AppConfig(), f"Could not load configuration; defaults are in use: {exc}"


def save_config(config: AppConfig, path: Path | None = None) -> None:
    config.validate()
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(config_path)

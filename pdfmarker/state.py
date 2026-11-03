"""Small JSON stores for preferences and per-document view positions."""
from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from pathlib import Path

MAX_POSITIONS = 500


def _home(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable) or Path.home() / fallback) / "pdf-marker"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict):
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".part")
        temp.write_text(json.dumps(data, indent=1))
        os.replace(temp, path)


def settings_path() -> Path:
    return _home("XDG_CONFIG_HOME", ".config") / "settings.json"


def positions_path() -> Path:
    return _home("XDG_STATE_HOME", ".local/state") / "positions.json"


def setting(name: str, default=None):
    return _read(settings_path()).get(name, default)


def set_setting(name: str, value):
    data = _read(settings_path())
    data[name] = value
    _write(settings_path(), data)


def position(path: str | Path) -> dict | None:
    entry = _read(positions_path()).get(str(path))
    return entry if isinstance(entry, dict) else None


def remember_position(path: str | Path, entry: dict):
    data = _read(positions_path())
    data[str(path)] = {**entry, "time": time.time()}
    if len(data) > MAX_POSITIONS:
        newest = sorted(data.items(), key=lambda item: item[1].get("time", 0) if isinstance(item[1], dict) else 0)
        data = dict(newest[-MAX_POSITIONS:])
    _write(positions_path(), data)

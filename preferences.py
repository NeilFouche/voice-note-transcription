"""
Small persisted user preferences - currently just the model size picked
in the app's model dropdown, so it's remembered across restarts instead
of resetting to the bundled default every time. Deliberately separate
from config.py's TOML-based settings: that's a machine/build-time
override mechanism (edited by hand), this is state the app itself writes
whenever the user changes something in the UI.
"""

import json

from config import settings

_PREFERENCES_PATH_NAME = "_preferences.json"


def _path():
    return settings.data_repo / _PREFERENCES_PATH_NAME


def _load() -> dict:
    path = _path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_model_size() -> str | None:
    """Returns the last-selected model size, or None if the user has
    never changed it (caller should fall back to settings.model_size)."""
    return _load().get("model_size")


def set_model_size(model_size: str):
    data = _load()
    data["model_size"] = model_size
    _save(data)

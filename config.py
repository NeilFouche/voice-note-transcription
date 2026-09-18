"""
App configuration.

Defaults live below and are anchored to two directories:

* BASE_DIR - writable data (logs, the small performance-calibration file,
  config.local.toml). When running from source this is this file's own
  folder; when packaged (PyInstaller), it's the folder containing the
  .exe itself, so the app's working files sit somewhere the user can find
  in Explorer, not buried inside the bundled runtime.
* RESOURCE_DIR - read-only bundled resources (the Afrikaans dictionary,
  the bundled Whisper model). When packaged, PyInstaller extracts/places
  these under sys._MEIPASS, which is a different location from BASE_DIR
  for a folder build - hence the two separate roots.

There's no input/output folder pipeline here - each run processes
whatever files the user picks in the app and saves the result wherever
they choose via a native Save dialog, so there's nothing to anchor beyond
logs and the tiny calibration cache.

To override any setting for this machine without touching tracked code,
create a `config.local.toml` next to this file (see config.example.toml
for the available keys). It's gitignored, so machine-specific paths never
end up in source control.
"""

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = BASE_DIR

LOCAL_CONFIG_PATH = BASE_DIR / "config.local.toml"


@dataclass
class Settings:
    dictionaries_repo: Path
    models_repo: Path
    logs_repo: Path
    data_repo: Path
    target_filename: str = "transcriptions.xlsx"
    general_log_filename: str = "processing_history.log"
    transcription_log_filename: str = "transcription.log"
    errors_log_filename: str = "errors.log"
    model_size: str = "large-v3"
    default_language: str = "af"

    def __post_init__(self):
        self.general_log_path: Path = self.logs_repo / self.general_log_filename
        self.transcription_log_path: Path = self.logs_repo / self.transcription_log_filename
        self.errors_log_path: Path = self.logs_repo / self.errors_log_filename
        self.performance_stats_path: Path = self.data_repo / "_performance_stats.json"

    def ensure_dirs(self):
        """Create every writable directory the app reads from or writes to."""
        for directory in (self.logs_repo, self.data_repo):
            directory.mkdir(parents=True, exist_ok=True)


def _resolve_writable_path(value: str) -> Path:
    """Relative paths resolve against BASE_DIR; absolute paths pass through."""
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def _resolve_resource_path(value: str) -> Path:
    """Relative paths resolve against RESOURCE_DIR; absolute paths pass through."""
    path = Path(value)
    return path if path.is_absolute() else RESOURCE_DIR / path


def _load_local_overrides() -> dict:
    """
    Flatten config.local.toml (if present) into a single {key: value} dict.
    Section headers ([paths], [transcription], ...) exist purely to keep the
    TOML file organised for humans - they aren't part of the lookup key.
    """
    if not LOCAL_CONFIG_PATH.exists():
        return {}

    with open(LOCAL_CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)

    overrides = {}
    for key, value in data.items():
        if isinstance(value, dict):
            overrides.update(value)
        else:
            overrides[key] = value
    return overrides


_overrides = _load_local_overrides()


def _setting(key: str, default):
    return _overrides.get(key, default)


settings = Settings(
    dictionaries_repo=_resolve_resource_path(_setting("dictionaries_repo", "dictionaries")),
    models_repo=_resolve_resource_path(_setting("models_repo", "models")),
    logs_repo=_resolve_writable_path(_setting("logs_repo", "logs")),
    data_repo=_resolve_writable_path(_setting("data_repo", "data")),
    target_filename=_setting("target_filename", "transcriptions.xlsx"),
    general_log_filename=_setting("general_log_filename", "processing_history.log"),
    transcription_log_filename=_setting("transcription_log_filename", "transcription.log"),
    errors_log_filename=_setting("errors_log_filename", "errors.log"),
    model_size=_setting("model_size", "large-v3"),
    default_language=_setting("default_language", "af"),
)

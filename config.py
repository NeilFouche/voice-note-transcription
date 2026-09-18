"""
Pipeline configuration.

Defaults live below and are anchored to two directories:

* BASE_DIR - writable data (Audio Files, Complete, logs, the final output,
  config.local.toml). When running from source this is this file's own
  folder; when packaged (PyInstaller), it's the folder containing the
  .exe itself, so the app's working files sit somewhere the user can find
  in Explorer, not buried inside the bundled runtime.
* RESOURCE_DIR - read-only bundled resources (the Afrikaans dictionary,
  the bundled Whisper model). When packaged, PyInstaller extracts/places
  these under sys._MEIPASS, which is a different location from BASE_DIR
  for a folder build - hence the two separate roots.

Either way the app behaves the same no matter what folder it's launched
from (a double-clicked shortcut, a scheduled task, etc.), not just a
terminal sitting in the project root.

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
    input_dir: Path
    transcript_clean_output: Path
    transcript_error_output: Path
    dictionaries_repo: Path
    models_repo: Path
    processing_repo: Path
    logs_repo: Path
    final_output_dir: Path
    target_filename: str = "transcriptions.xlsx"
    general_log_filename: str = "processing_history.log"
    transcription_log_filename: str = "transcription.log"
    errors_log_filename: str = "errors.log"
    model_size: str = "medium"
    default_language: str = "af"

    def __post_init__(self):
        self.general_log_path: Path = self.logs_repo / self.general_log_filename
        self.transcription_log_path: Path = self.logs_repo / self.transcription_log_filename
        self.errors_log_path: Path = self.logs_repo / self.errors_log_filename
        self.serialized_repo: Path = self.processing_repo / "serialized"
        self.processed_index_path: Path = self.processing_repo / "_processed_index.txt"
        self.performance_stats_path: Path = self.processing_repo / "_performance_stats.json"

    def ensure_dirs(self):
        """Create every writable directory the pipeline reads from or writes to."""
        for directory in (
            self.input_dir,
            self.transcript_clean_output,
            self.transcript_error_output,
            self.processing_repo,
            self.serialized_repo,
            self.final_output_dir,
            self.logs_repo,
        ):
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
    input_dir=_resolve_writable_path(_setting("input_dir", "Audio Files")),
    transcript_clean_output=_resolve_writable_path(_setting("transcript_clean_output", "Complete")),
    transcript_error_output=_resolve_writable_path(_setting("transcript_error_output", "Not Transcribed")),
    dictionaries_repo=_resolve_resource_path(_setting("dictionaries_repo", "dictionaries")),
    models_repo=_resolve_resource_path(_setting("models_repo", "models")),
    processing_repo=_resolve_writable_path(_setting("processing_repo", "processing")),
    logs_repo=_resolve_writable_path(_setting("logs_repo", "logs")),
    final_output_dir=_resolve_writable_path(_setting("final_output_dir", "Transcription")),
    target_filename=_setting("target_filename", "transcriptions.xlsx"),
    general_log_filename=_setting("general_log_filename", "processing_history.log"),
    transcription_log_filename=_setting("transcription_log_filename", "transcription.log"),
    errors_log_filename=_setting("errors_log_filename", "errors.log"),
    model_size=_setting("model_size", "medium"),
    default_language=_setting("default_language", "af"),
)

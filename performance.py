"""
Estimates how long transcription will take, based on this machine's own
measured throughput rather than trying to model CPU specs (core count,
clock speed, etc. are weak predictors of Whisper throughput - AVX support,
thermal throttling and background load matter more than what's easy to
read from the OS).

After every real transcription run, record() folds the observed (audio
seconds processed, wall-clock seconds taken) into
Settings.performance_stats_path, keyed by model size - the user can
switch model sizes at runtime (see transcription.py / app.py's model
picker), and each size has a genuinely different speed, so they're
tracked independently rather than one switch wiping out another size's
calibration history. estimate_seconds() then uses the relevant size's
machine-specific ratio for future estimates. Before a given size has any
real run recorded, it falls back to a rough published-benchmark default
so the UI still shows *something* on first use of that size.
"""

import json

from config import settings

# Rough ballpark for faster-whisper (int8, CPU) from published benchmarks.
# Seconds of processing per second of audio. Deliberately approximate -
# only used until this machine has real measured data for that size.
_DEFAULT_RATE_BY_MODEL = {
    "tiny": 0.15,
    "base": 0.25,
    "small": 0.45,
    "medium": 0.9,
    "large-v2": 1.8,
    "large-v3": 1.8,
    "large": 1.8,
    "large-v3-turbo": 0.5,
}
_DEFAULT_RATE_FALLBACK = 1.0


def _load_all() -> dict:
    if not settings.performance_stats_path.exists():
        return {}
    try:
        data = json.loads(settings.performance_stats_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    # Migrate the old flat (single model_size) format from before per-size
    # tracking existed, so upgrading doesn't just silently drop history.
    if "model_size" in data and "audio_seconds" in data:
        return {data["model_size"]: {
            "audio_seconds": data.get("audio_seconds", 0.0),
            "wall_seconds": data.get("wall_seconds", 0.0),
            "runs": data.get("runs", 0),
        }}

    return data


def _save_all(data: dict):
    settings.performance_stats_path.parent.mkdir(parents=True, exist_ok=True)
    settings.performance_stats_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record(audio_seconds: float, wall_seconds: float, model_size: str):
    """Fold a completed run's real throughput into this model size's stats."""
    if audio_seconds <= 0 or wall_seconds <= 0:
        return

    data = _load_all()
    entry = data.get(model_size, {"audio_seconds": 0.0, "wall_seconds": 0.0, "runs": 0})
    entry["audio_seconds"] += audio_seconds
    entry["wall_seconds"] += wall_seconds
    entry["runs"] += 1
    data[model_size] = entry
    _save_all(data)


def estimated_rate(model_size: str) -> tuple[float, bool, int]:
    """Returns (seconds-of-processing per second-of-audio, is_measured, run_count)."""
    entry = _load_all().get(model_size)
    if entry and entry.get("audio_seconds", 0) > 0:
        return entry["wall_seconds"] / entry["audio_seconds"], True, entry.get("runs", 0)

    return _DEFAULT_RATE_BY_MODEL.get(model_size, _DEFAULT_RATE_FALLBACK), False, 0


def estimate_seconds(audio_seconds: float, model_size: str) -> dict:
    rate, is_measured, runs = estimated_rate(model_size)
    return {
        "estimated_seconds": audio_seconds * rate,
        "is_measured": is_measured,
        "based_on_runs": runs,
    }

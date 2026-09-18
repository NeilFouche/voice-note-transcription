"""
Estimates how long transcription will take, based on this machine's own
measured throughput rather than trying to model CPU specs (core count,
clock speed, etc. are weak predictors of Whisper throughput - AVX support,
thermal throttling and background load matter more than what's easy to
read from the OS).

After every real transcription run, record() folds the observed (audio
seconds processed, wall-clock seconds taken) into Settings.performance_stats_path.
estimate_seconds() then uses that machine-specific ratio for future
estimates. Before any real run has happened, it falls back to a rough
published-benchmark default for the configured model so the UI still shows
*something* on first use.

If model_size changes, prior stats are discarded rather than blended in -
a different model has a different speed, so old numbers would mislead.
"""

import json

from config import settings

# Rough ballpark for faster-whisper (int8, CPU) from published benchmarks.
# Seconds of processing per second of audio. Deliberately approximate -
# only used until this machine has real measured data.
_DEFAULT_RATE_BY_MODEL = {
    "tiny": 0.15,
    "base": 0.25,
    "small": 0.45,
    "medium": 0.9,
    "large-v2": 1.8,
    "large-v3": 1.8,
    "large": 1.8,
}
_DEFAULT_RATE_FALLBACK = 1.0


def _load() -> dict:
    if not settings.performance_stats_path.exists():
        return {}
    try:
        return json.loads(settings.performance_stats_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict):
    settings.performance_stats_path.parent.mkdir(parents=True, exist_ok=True)
    settings.performance_stats_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record(audio_seconds: float, wall_seconds: float):
    """Fold a completed run's real throughput into this machine's stats."""
    if audio_seconds <= 0 or wall_seconds <= 0:
        return

    data = _load()
    if data.get("model_size") != settings.model_size:
        # Numbers from a different model aren't comparable - start fresh.
        data = {"model_size": settings.model_size, "audio_seconds": 0.0, "wall_seconds": 0.0, "runs": 0}

    data["audio_seconds"] = data.get("audio_seconds", 0.0) + audio_seconds
    data["wall_seconds"] = data.get("wall_seconds", 0.0) + wall_seconds
    data["runs"] = data.get("runs", 0) + 1
    _save(data)


def estimated_rate() -> tuple[float, bool, int]:
    """Returns (seconds-of-processing per second-of-audio, is_measured, run_count)."""
    data = _load()
    if data.get("model_size") == settings.model_size and data.get("audio_seconds", 0) > 0:
        return data["wall_seconds"] / data["audio_seconds"], True, data.get("runs", 0)

    return _DEFAULT_RATE_BY_MODEL.get(settings.model_size, _DEFAULT_RATE_FALLBACK), False, 0


def estimate_seconds(audio_seconds: float) -> dict:
    rate, is_measured, runs = estimated_rate()
    return {
        "estimated_seconds": audio_seconds * rate,
        "is_measured": is_measured,
        "based_on_runs": runs,
    }

"""
Transcribes audio files and applies Afrikaans spelling correction, one
file at a time, entirely in memory.

This app processes exactly the batch of files the user selects each run -
there's no watched input folder accumulating history between runs, so
there's no need for the multi-stage disk pipeline (separate per-file
transcript files, a "serialized" intermediate, a dedup index) earlier
versions of this tool used. Each file becomes one (date, time,
transcription) row; nothing is written to disk except the logs.

Expected filename format (for recovering Date/Time - see filenames.py):
    WhatsApp Audio 2026-09-16 at 07.42.13.ogg
    WhatsApp Audio 2026-09-16 at 07.42.13 (1).ogg   (duplicate suffix, also handled)

Requires:
    pip install faster-whisper spylls

Dictionary files needed:
    dictionaries/afrikaans/af_ZA.dic
    dictionaries/afrikaans/af_ZA.aff
"""

import fnmatch
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import huggingface_hub
import tqdm as tqdm_module
from faster_whisper import WhisperModel
from spylls.hunspell import Dictionary

import download_progress
import performance
import progress
from config import settings
from filenames import extract_datetime
from logging_config import general_logger, transcription_logger, error_logger

AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".mp4", ".flac", ".opus"}

# Rules for Dutch/Afrikaans discrimination
RULES = [
    (r"sch", "sk"),
    (r"ische", "iese"),
    (r"ij", "y"),
    (r"y", "i"),
    (r"tie\b", "sie"),
    (r"ct", "ks"),
    (r"([aeiou])v([aeiou])", r"\1w\2"),
    (r"\bc([aou])", r"k\1"),
    (r"^z", r"s"),
    (r"ch", r"g"),
    (r"nc", r"ns"),
    (r"mt", r"m"),
]

# Add pairs as you spot recurring issues. Left side: word as it appears
# in transcripts (any case). Right side: what to replace it with. Applied
# after the RULES-based dictionary correction below.
REPLACEMENTS = {
    "een": "'n",
    "commerciële": "kommersiële",
    "correct": "korrek",
    "commercieel": "kommersieel",
    "rechts": "regs",
    "bykie": "bietjie",
    "biekie": "bietjie",
    "beekie": "bietjie",
}

_REPLACEMENTS_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in REPLACEMENTS) + r")\b",
    re.IGNORECASE,
)


def generate_candidates(word: str):
    candidates = {word}
    for pattern, replacement in RULES:
        new_candidates = set()
        for c in candidates:
            replaced = re.sub(pattern, replacement, c, flags=re.IGNORECASE)
            if replaced != c:
                new_candidates.add(replaced)

        candidates |= new_candidates

    # Only strip the untouched original at the very end - discarding it
    # after each rule would wipe the candidate pool to empty as soon as
    # any one rule failed to match, leaving nothing for later rules to
    # work on (this was a real bug in an earlier version).
    candidates.discard(word)

    return candidates


def correct_word(word: str, dictionary: Dictionary) -> str:
    stripped = word.strip(".,!?:;\"'")
    suffix = word[len(stripped):]

    if not stripped or dictionary.lookup(stripped):
        return word

    for candidate in generate_candidates(stripped):
        if dictionary.lookup(candidate):
            if stripped[0].isupper():
                candidate = candidate.capitalize()
            transcription_logger.info(f"Corrected: {stripped} -> {candidate}")
            return candidate + suffix

    transcription_logger.info(f"Not in dictionary: {stripped}")
    return word


def apply_replacements(text: str) -> str:
    def replace_match(match):
        original = match.group(0)
        replacement = REPLACEMENTS[original.lower()]
        if original[0].isupper():
            replacement = replacement.capitalize()
        transcription_logger.info(f"Replaced: {original} -> {replacement}")
        return replacement

    return _REPLACEMENTS_PATTERN.sub(replace_match, text)


def correct_text(text: str, dictionary: Dictionary) -> str:
    corrected = " ".join(correct_word(w, dictionary) for w in text.split())
    return apply_replacements(corrected)


# Cached lazily and reused across calls - loading the dictionary/model is
# expensive, and transcribe_batch() may be invoked repeatedly within the
# same process (once per run in the desktop app), not just once ever.
# Models are cached per size, since the user can switch sizes at runtime
# (see the model picker in app.py) - keeps switching back to a
# previously-used size instant instead of reloading from scratch.
_dictionary: Dictionary | None = None
_models: dict[str, WhisperModel] = {}


def _get_dictionary() -> Dictionary:
    global _dictionary
    if _dictionary is None:
        general_logger.info("Loading Afrikaans dictionary...")
        _dictionary = Dictionary.from_files(str(settings.dictionaries_repo / "afrikaans" / "af_ZA"))
    return _dictionary


def is_model_available_locally(model_size: str) -> bool:
    """
    True if model_size can be loaded without hitting the network - either
    bundled with the app, or already downloaded/cached from a previous
    run. Used to warn before a switch that's about to trigger a
    multi-hundred-MB-to-several-GB download, rather than the app just
    appearing to hang.
    """
    if (settings.models_repo / model_size).exists():
        return True
    try:
        from faster_whisper.utils import download_model
        download_model(model_size, local_files_only=True)
        return True
    except Exception:
        return False


# The exact set of files faster_whisper.utils.download_model() fetches -
# mirrored here since that function hardcodes tqdm_class to a disabled
# tqdm, giving no way to observe progress through it. download_model_size()
# below calls huggingface_hub directly instead, using this same allow-list,
# so the two stay interchangeable (same files land in the same HF cache
# location either way).
_MODEL_FILE_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]


def _model_total_bytes(repo_id: str) -> int:
    api = huggingface_hub.HfApi()
    info = api.model_info(repo_id, files_metadata=True)
    return sum(
        f.size for f in info.siblings
        if f.size and any(fnmatch.fnmatch(f.rfilename, pattern) for pattern in _MODEL_FILE_PATTERNS)
    )


def download_model_size(model_size: str):
    """
    Downloads model_size's files with real byte-level progress, reported
    through download_progress.py for the UI's explicit "download this
    model now" action (distinct from the automatic on-demand download
    transcribe_batch() falls back to when Transcribe is hit on a model
    that isn't available yet - that path has no progress reporting).

    huggingface_hub's own per-file tqdm bars (the ones with unit="B") do
    update smoothly in real time during the network transfer, so this
    hooks a tqdm subclass into snapshot_download() and aggregates them -
    confirmed empirically, since that behaviour isn't documented. tqdm
    instances get *reused* across files (reset() zeroes them out rather
    than a fresh instance being created per file), so bytes have to be
    banked on reset()/close() before they're lost. The "Fetching N files"
    bar snapshot_download also creates counts files, not bytes (unit is
    unset) - excluded via the unit check.
    """
    from faster_whisper.utils import _MODELS

    repo_id = _MODELS.get(model_size)
    if repo_id is None:
        raise ValueError(f"Unknown model size '{model_size}'")

    download_progress.start(model_size)
    try:
        total_bytes = _model_total_bytes(repo_id)
        lock = threading.Lock()
        completed_bytes = [0]
        current = {"n": 0}

        def report():
            if total_bytes > 0:
                with lock:
                    downloaded = completed_bytes[0] + current["n"]
                download_progress.set_percent(downloaded / total_bytes * 100)

        class _ProgressTqdm(tqdm_module.tqdm):
            def update(self, n=1):
                result = super().update(n)
                if self.unit == "B":
                    with lock:
                        current["n"] = self.n
                    report()
                return result

            def reset(self, total=None):
                if self.unit == "B":
                    with lock:
                        completed_bytes[0] += self.n
                        current["n"] = 0
                return super().reset(total=total)

            def close(self):
                if self.unit == "B":
                    with lock:
                        completed_bytes[0] += self.n
                        current["n"] = 0
                    report()
                super().close()

        general_logger.info(f"Downloading model '{model_size}'...")
        huggingface_hub.snapshot_download(
            repo_id,
            allow_patterns=_MODEL_FILE_PATTERNS,
            tqdm_class=_ProgressTqdm,
        )
        download_progress.set_percent(100.0)
        download_progress.finish()
        general_logger.info(f"Downloaded model '{model_size}'")
    except Exception as e:
        error_logger.exception(f"Failed to download model '{model_size}'")
        download_progress.finish(error=str(e))


def _get_model(model_size: str) -> WhisperModel:
    if model_size not in _models:
        general_logger.info(f"Loading Whisper model ({model_size})...")
        # A bundled local copy (packaged builds ship one or more under
        # models/<size> so transcription works fully offline) takes
        # priority; otherwise fall back to faster-whisper's normal
        # behaviour of downloading from the Hugging Face Hub and caching
        # it - this is what actually performs the download for a size
        # that isn't bundled, once the caller has already warned the user
        # about it via is_model_available_locally().
        local_model_path = settings.models_repo / model_size
        model_source = str(local_model_path) if local_model_path.exists() else model_size
        _models[model_size] = WhisperModel(
            model_source,
            device="cpu",
            compute_type="int8"
        )
    return _models[model_size]


@dataclass
class BatchResult:
    rows: list[tuple] = field(default_factory=list)  # (date | None, time | None, text)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (filename, error message)


def _sort_key(path: Path):
    parsed = extract_datetime(path.name)
    if parsed:
        dt, dup_index = parsed
        return (0, dt, dup_index)
    return (1, path.name, 0)


def transcribe_batch(file_paths: list[Path], model_size: str | None = None) -> tuple[bool, BatchResult]:
    """
    Transcribes every file in file_paths, in chronological order where a
    WhatsApp-style timestamp can be recovered from the filename.

    model_size defaults to settings.model_size (the configured/bundled
    default) if not given - the desktop app's model picker passes the
    user's currently selected size explicitly instead.

    Returns (completed, result). completed is False if the run was
    cancelled partway through (see progress.py) - the file being
    transcribed when cancellation was requested still finishes (a single
    in-flight Whisper call can't be interrupted mid-computation), but
    nothing after it starts. A failed file is recorded in result.errors
    and simply produces no row; nothing is moved anywhere, since this app
    doesn't manage a persistent input folder to keep tidy - the source
    file the caller passed in is left exactly where it was.
    """
    model_size = model_size or settings.model_size

    settings.ensure_dirs()
    dictionary = _get_dictionary()

    if not is_model_available_locally(model_size):
        general_logger.info(f"Model '{model_size}' isn't downloaded yet - fetching it now (one-time, needs internet)...")
        progress.set_stage("downloading_model")

    model = _get_model(model_size)

    ordered = sorted(file_paths, key=_sort_key)
    total = len(ordered)

    result = BatchResult()
    cancelled = False

    audio_seconds_processed = 0.0
    started_at = time.monotonic()

    progress.set_stage("transcribing")

    for i, path in enumerate(ordered, start=1):
        if progress.is_cancel_requested():
            general_logger.info("Cancelling the transcription...")
            cancelled = True
            break

        progress.set_current_file(i, path.name, total)

        try:
            general_logger.info(f"Transcribing: {path.name}")
            segments, info = model.transcribe(
                audio=path,
                beam_size=5,
                language=settings.default_language
            )

            texts = []
            for segment in segments:
                texts.append(segment.text.strip())
                if info.duration:
                    progress.set_file_fraction(segment.end / info.duration)

            corrected = correct_text(" ".join(texts), dictionary)
            progress.set_file_fraction(1.0)
            audio_seconds_processed += info.duration

            parsed = extract_datetime(path.name)
            date_value, time_value = (parsed[0].date(), parsed[0].time()) if parsed else (None, None)
            result.rows.append((date_value, time_value, corrected))
            general_logger.info(f"Transcribed: {path.name}")
        except Exception as e:
            error_logger.exception(f"Failed to transcribe {path.name}")
            result.errors.append((path.name, str(e)))
            progress.set_file_fraction(1.0)

        # Checked again here (not just before the *next* file) so a cancel
        # requested while this file was in flight takes effect right away -
        # otherwise a single-file run (or cancelling during the last file
        # of a batch) would have no further iteration left to catch it.
        if progress.is_cancel_requested():
            general_logger.info("Cancelling the transcription...")
            cancelled = True
            break

    performance.record(audio_seconds_processed, time.monotonic() - started_at, model_size)
    if cancelled:
        general_logger.info("Transcription stage cancelled")
    else:
        general_logger.info(f"Transcription stage complete - {len(result.rows)} file(s) transcribed, {len(result.errors)} failed")

    return not cancelled, result

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

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from faster_whisper import WhisperModel
from spylls.hunspell import Dictionary

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
    (r"tie\b", "sie"),
    (r"ct", "ks"),
    (r"([aeiou])v([aeiou])", r"\1w\2"),
    (r"\bc([aou])", r"k\1"),
    (r"^z", "s"),
    (r"cht", "gt"),
]

# Add pairs as you spot recurring issues. Left side: word as it appears
# in transcripts (any case). Right side: what to replace it with. Applied
# after the RULES-based dictionary correction below.
REPLACEMENTS = {
    "een": "'n",
    "commerciële": "kommersiële",
    "correct": "korrek",
    "commercieel": "kommersieel",
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


# Cached lazily and reused across calls - loading the dictionary and model
# is expensive, and transcribe_batch() may be invoked repeatedly within
# the same process (once per run in the desktop app), not just once ever.
_dictionary: Dictionary | None = None
_model: WhisperModel | None = None


def _get_dictionary() -> Dictionary:
    global _dictionary
    if _dictionary is None:
        general_logger.info("Loading Afrikaans dictionary...")
        _dictionary = Dictionary.from_files(str(settings.dictionaries_repo / "afrikaans" / "af_ZA"))
    return _dictionary


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        general_logger.info("Loading Whisper model...")
        # A bundled local copy (packaged builds ship one under models/<size>
        # so transcription works fully offline) takes priority; otherwise
        # fall back to faster-whisper's normal behaviour of downloading
        # from the Hugging Face Hub and caching it - convenient for
        # running from source without needing every model size on disk.
        local_model_path = settings.models_repo / settings.model_size
        model_source = str(local_model_path) if local_model_path.exists() else settings.model_size
        _model = WhisperModel(
            model_source,
            device="cpu",
            compute_type="int8"
        )
    return _model


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


def transcribe_batch(file_paths: list[Path]) -> tuple[bool, BatchResult]:
    """
    Transcribes every file in file_paths, in chronological order where a
    WhatsApp-style timestamp can be recovered from the filename.

    Returns (completed, result). completed is False if the run was
    cancelled partway through (see progress.py) - the file being
    transcribed when cancellation was requested still finishes (a single
    in-flight Whisper call can't be interrupted mid-computation), but
    nothing after it starts. A failed file is recorded in result.errors
    and simply produces no row; nothing is moved anywhere, since this app
    doesn't manage a persistent input folder to keep tidy - the source
    file the caller passed in is left exactly where it was.
    """
    settings.ensure_dirs()
    dictionary = _get_dictionary()
    model = _get_model()

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

    performance.record(audio_seconds_processed, time.monotonic() - started_at)
    if cancelled:
        general_logger.info("Transcription stage cancelled")
    else:
        general_logger.info(f"Transcription stage complete - {len(result.rows)} file(s) transcribed, {len(result.errors)} failed")

    return not cancelled, result

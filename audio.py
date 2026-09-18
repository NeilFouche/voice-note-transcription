"""
Handles transcribing audio files.

* Transcribes all audio files in 'Audio Files' folder
* Successfully transcribed files:
    * Transcript written to 'Complete' folder
    * Source audio file moved alongside it in 'Complete' (not copied - so
      'Audio Files' ends up empty after a run instead of accumulating
      every file ever dropped in)
    * File names prepended with sequence number
* Unsuccessfully transcribed files:
    * Moved to 'Not Transcribed' folder
    * Respective errors logged in log file

Expected input filename format:
    WhatsApp Audio 2026-09-16 at 07.42.13.ogg
    WhatsApp Audio 2026-09-16 at 07.42.13 (1).ogg   (duplicate suffix, also handled)

Requires:
    pip install faster-whisper spylls

Dictionary files needed:
    dictionaries/afrikaans/af_ZA.dic
    dictionaries/afrikaans/af_ZA.aff
"""

import re
import shutil
import time
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

def build_sequenced_name(audio_path: Path, sequence: int) -> str:
    return f"{sequence:04d}_{audio_path.stem}.txt"

def _archive_audio_file(audio_path: Path, output_name: str):
    """
    Move (not copy) the source audio file to sit alongside its transcript
    in the Complete folder, sharing the transcript's sequence prefix. This
    is what keeps 'Audio Files' empty after a run - the alternative is it
    silently accumulating every file ever dropped in.

    Best-effort: if the move fails (e.g. the file is open elsewhere), the
    transcript itself is already safely written, so this only logs rather
    than raising - losing the source audio copy isn't worth failing an
    otherwise-successful transcription over.
    """
    prefix = output_name.split("_", 1)[0]
    dest_path = settings.transcript_clean_output / f"{prefix}_{audio_path.stem}{audio_path.suffix}"
    try:
        shutil.move(str(audio_path), str(dest_path))
    except OSError:
        error_logger.exception(f"Failed to move {audio_path.name} into {settings.transcript_clean_output}")

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
    # after each rule (as this used to do) meant that as soon as any one
    # rule didn't match, the candidate pool was wiped to empty and every
    # later rule had nothing left to work on. Since "cht" -> "gt" is last
    # in RULES, it almost never got a chance to fire.
    candidates.discard(word)

    return candidates

def correct_word(word: str, dictionary: Dictionary) -> str:
    stripped = word.strip(".,!?:;\"'")
    suffix  = word[len(stripped):]

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

def correct_text(text: str, dictionary: Dictionary) -> str:
    return " ".join(correct_word(w, dictionary) for w in text.split())

# Cached lazily and reused across calls - loading the dictionary and model
# is expensive, and transcribe() may be invoked repeatedly within the same
# process (e.g. once per upload in the web app), not just once per run.
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

def transcribe(only_filenames: set[str] | None = None) -> tuple[bool, set[str]]:
    """
    Returns (completed, touched_transcript_filenames).

    completed is False if the run was cancelled partway through.

    touched_transcript_filenames is the exact set of Complete/*.txt names
    this run produced or confirmed (freshly transcribed, or already known
    via the processed index) - one name per file in only_filenames that
    didn't fail. The web app uses this (not a "not yet serialized" scan)
    to scope replacements/serialize/combine, so a run's result is always
    exactly what was selected - never also picking up some unrelated
    leftover file just because it happens to be missing a downstream
    artifact too.

    only_filenames restricts the run to files whose name is in that set -
    anything else currently sitting in Audio Files is left untouched. This
    is what the web app passes (scoped to exactly what was just uploaded,
    so selecting one file doesn't also sweep up unrelated leftovers from
    other sessions). Left as None (the default) for the CLI/folder-drop
    flow in main.py, where processing everything sitting in the folder is
    the actual intended behaviour - drop files in over time, run the
    pipeline, it catches everything not yet transcribed.
    """
    settings.ensure_dirs()
    input_dir = settings.input_dir

    dictionary = _get_dictionary()
    model = _get_model()

    touched: set[str] = set()

    audio_files = [f for f in input_dir.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS]
    if only_filenames is not None:
        audio_files = [f for f in audio_files if f.name in only_filenames]

    if not audio_files:
        general_logger.info(f"No audio files in {input_dir}")
        return True, touched

    # Sort chronologically by embedded datetime
    def sort_key(f: Path):
        parsed = extract_datetime(f.name)
        if parsed:
            dt, dup_index = parsed
            return (0, dt, dup_index)

        return (1, f.name, 0)

    audio_files.sort(key=sort_key)

    # Determine the next sequence number checking what's already in Settings.output_dir
    existing = sorted(settings.transcript_clean_output.glob("[0-9][0-9][0-9][0-9]_*.txt"))
    next_sequence = 1
    if existing:
        last_num = int(existing[-1].name.split("_", 1)[0])
        next_sequence = last_num + 1

    index_path = settings.processed_index_path
    processed = {}
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                src, out = line.split("\t", 1)
                processed[src] = out

    progress.set_stage("transcribing")

    # Tracked so this machine's real throughput can be measured and used to
    # estimate future runs (see performance.py) - more reliable than
    # guessing from CPU specs.
    audio_seconds_processed = 0.0
    transcribe_started_at = time.monotonic()

    total_files = len(audio_files)
    cancelled = False

    for i, audio_path in enumerate(audio_files, start=1):
        if progress.is_cancel_requested():
            general_logger.info("Cancelling the transcription...")
            cancelled = True
            break

        progress.set_current_file(i, audio_path.name, total_files)

        if audio_path.name in processed:
            general_logger.info(f"Skipping (already transcribed): {audio_path.name}")
            # Leftover from before this file was moved out, or from a
            # previous run whose move failed - clean it up now rather than
            # leaving it sitting in Audio Files indefinitely.
            _archive_audio_file(audio_path, processed[audio_path.name])
            touched.add(processed[audio_path.name])
            progress.set_file_fraction(1.0)
        else:
            try:
                general_logger.info(f"Transcribing: {audio_path.name}")
                segments, info = model.transcribe(
                    audio=audio_path,
                    beam_size=5,
                    language=settings.default_language
                )

                output_name = build_sequenced_name(audio_path, next_sequence)
                output_path = settings.transcript_clean_output / output_name

                with open(output_path, "w", encoding="utf-8") as f:
                    for segment in segments:
                        corrected = correct_text(
                            text=segment.text.strip(),
                            dictionary=dictionary
                        )
                        f.write(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {corrected}\n")
                        if info.duration:
                            progress.set_file_fraction(segment.end / info.duration)

                progress.set_file_fraction(1.0)
                audio_seconds_processed += info.duration

                processed[audio_path.name] = output_name
                with open(index_path, "a", encoding="utf-8") as f:
                    f.write(f"{audio_path.name}\t{output_name}\n")

                general_logger.info(f"Saved to {output_name}")
                _archive_audio_file(audio_path, output_name)
                touched.add(output_name)
                next_sequence += 1
            except Exception:
                error_path = settings.transcript_error_output / audio_path.name
                shutil.move(str(audio_path), str(error_path))
                error_logger.exception(f"Failed to transcribe {audio_path.name}")
                general_logger.info(f"Moved {audio_path.name} to {settings.transcript_error_output} (transcription failed)")
                progress.set_file_fraction(1.0)

        # Checked again here (not just before the *next* file) so a cancel
        # requested while this file was in flight takes effect right away -
        # otherwise a single-file run (or cancelling during the last file
        # of a batch) would have no further iteration left to catch it, and
        # the run would silently finish as if cancel had never been clicked.
        if progress.is_cancel_requested():
            general_logger.info("Cancelling the transcription...")
            cancelled = True
            break

    performance.record(audio_seconds_processed, time.monotonic() - transcribe_started_at)
    if cancelled:
        general_logger.info("Transcription stage cancelled")
    else:
        general_logger.info("Transcription stage complete")
    return not cancelled, touched

if __name__ == "__main__":
    transcribe()

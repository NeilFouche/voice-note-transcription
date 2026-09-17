"""
Handles transcribing audio files.

* Transcribes all audio files in 'Audio Files' folder
* Successfully transcribed files:
    * Transcript written to 'Complete' folder
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
from pathlib import Path
from datetime import datetime

from faster_whisper import WhisperModel
from spylls.hunspell import Dictionary

from config import settings
from logging_config import general_logger, transcription_logger, error_logger

AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".mp4", ".flac", ".opus"}

# Expected input filename format:
# WhatsApp Audio 2026-09-16 at 07.42.13.ogg
FILENAME_PATTERN = re.compile(
  r"WhatsApp Audio (\d{4}-\d{2}-\d{2}) at (\d{2}\.\d{2}\.\d{2})(?: \((\d+)\))?"
)

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

def extract_datetime(filename: str):
    """
    Pull the embedded datetime (and duplicate index) from a Whatsapp audio filename.
    Returns a datetime object, or None if no match.
    """
    match = FILENAME_PATTERN.search(filename)
    if not match:
        return None

    date_str, time_str, dup_index = match.groups()
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H.%M.%S")

    return dt, int(dup_index) if dup_index else 0

def build_sequenced_name(audio_path: Path, sequence: int) -> str:
    return f"{sequence:04d}_{audio_path.stem}.txt"

def generate_candidates(word: str):
    candidates = {word}
    for pattern, replacement in RULES:
        new_candidates = set()
        for c in candidates:
            replaced = re.sub(pattern, replacement, c, flags=re.IGNORECASE)
            if replaced != c:
                new_candidates.add(replaced)

        candidates |= new_candidates
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

def transcribe():
    settings.ensure_dirs()
    input_dir = settings.input_dir

    general_logger.info("Loading Afrikaans dictionary...")
    dictionary = Dictionary.from_files(str(settings.dictionaries_repo / "afrikaans" / "af_ZA"))

    general_logger.info("Loading Whisper model...")
    model = WhisperModel(
        settings.model_size,
        device="cpu",
        compute_type="int8"
    )

    audio_files = [f for f in input_dir.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS]

    if not audio_files:
        general_logger.info(f"No audio files in {input_dir}")
        return

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

    for audio_path in audio_files:
        if audio_path.name in processed:
            general_logger.info(f"Skipping (already transcribed): {audio_path.name}")
            continue

        try:
            general_logger.info(f"Transcribing: {audio_path.name}")
            segments, _ = model.transcribe(
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

            processed[audio_path.name] = output_name
            with open(index_path, "a", encoding="utf-8") as f:
                f.write(f"{audio_path.name}\t{output_name}\n")

            general_logger.info(f"Saved to {output_name}")
            next_sequence += 1
        except Exception:
            error_path = settings.transcript_error_output / audio_path.name
            shutil.move(str(audio_path), str(error_path))
            error_logger.exception(f"Failed to transcribe {audio_path.name}")
            general_logger.info(f"Moved {audio_path.name} to {settings.transcript_error_output} (transcription failed)")
            continue

    general_logger.info("Transcription stage complete")

if __name__ == "__main__":
    transcribe()

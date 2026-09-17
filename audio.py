"""
Handles transcribing audio files.

* Transcribes all audio files in 'Audio Files' folder
* Successfully transcribed files:
    * Transcript written to 'Transcribed' folder
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

def correct_word(word: str, dictionary: Dictionary, corrections_log: list):
    stripped = word.strip(".,!?:;\"'")
    suffix  = word[len(stripped):]

    if not stripped or dictionary.lookup(stripped):
        return word

    for candidate in generate_candidates(stripped):
        if dictionary.lookup(candidate):
            if stripped[0].isupper():
                candidate = candidate.capitalize()
            corrections_log.append((stripped, candidate))
            return candidate + suffix

    return word

def correct_text(text: str, dictionary: Dictionary, log: list) -> str:
    return " ".join(correct_word(w, dictionary, log) for w in text.split())

def transcribe():
    settings.ensure_dirs()
    input_dir = settings.input_dir

    print("Loading Afrikaans dictionary...")
    dictionary = Dictionary.from_files(str(settings.dictionaries_repo / "afrikaans" / "af_ZA"))

    print("Loading Whisper model...")
    model = WhisperModel(
        settings.model_size,
        device="cpu",
        compute_type="int8"
    )

    audio_files = [f for f in input_dir.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS]

    if not audio_files:
        print(f"No audio files in {input_dir}")
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

    index_path = settings.logs_repo / "_processed_index.txt"
    processed = {}
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                src, out = line.split("\t", 1)
                processed[src] = out

    corrections_log = []

    for audio_path in audio_files:
        if audio_path.name in processed:
            print(f"Skipping (already transcribed): {audio_path.name}")
            continue

        try:
            print(f"Transcribing: {audio_path.name}")
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
                        dictionary=dictionary,
                        log=corrections_log
                    )
                    f.write(f"[{segment.start:.2f}s -> {segment.end:.2f}] {corrected}\n")

            processed[audio_path.name] = output_name
            with open(index_path, "a", encoding="utf-8") as f:
                f.write(f"{audio_path.name}\t{output_name}\n")

            print(f"  -> Saved to {output_name}")
            next_sequence += 1
        except Exception as e:
            error_path = settings.transcript_error_output / audio_path.name
            shutil.move(str(audio_path), str(error_path))
            with open(settings.errors_log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{audio_path.name}\t{e}\n")
            print(f"  -> Failed ({e}); moved to {settings.transcript_error_output}")
            continue

    if corrections_log:
        with open(settings.corrections_log_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- Run at {datetime.now().isoformat(timespec='seconds')} ---\n")
            for original, corrected in corrections_log:
                f.write(f"{original} -> {corrected}\n")

        print(f"Logged {len(corrections_log)} spelling correction(s) to {settings.corrections_log_path.name}")

    print("DONE")

if __name__ == "__main__":
    transcribe()

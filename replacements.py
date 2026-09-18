"""
Apply a custom word-replacement dictionary to existing transcript files.

Run this after transcribe_batch.py, any time recurring words need to be
corrected (mistranscriptions, names, jargon, etc.) that the Dutch -> Afrikaans
rule-based correction doesn't catch.

Matching is case-insensitive on whole words only and original capitalisation
of the matched word is preserved in the output. Punctuation directly attached
to a word (commas, periods, etc.) is left alone.
"""

import re

import progress
from config import settings
from logging_config import general_logger, transcription_logger

# Add pairs as you spot recurring issues. Left side: word as it appears
# in transcripts (any case). Right side: what to replace it with.
REPLACEMENTS = {
    "een": "'n",
    "commerciële": "kommersiële",
    "correct": "korrek",
    "commercieel": "kommersieel",
}

def apply_replacements(text: str, replacements: dict[str, str]) -> tuple[str, int]:
    count = 0

    def replace_match(match):
        nonlocal count
        original = match.group(0)
        key = original.lower()
        if key not in replacements:
            return original
        replacement = replacements[key]
        if original[0].isupper():
            replacement = replacement.capitalize()
        transcription_logger.info(f"Replaced: {original} -> {replacement}")
        count += 1
        return replacement

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in replacements) + r")\b",
        re.IGNORECASE
    )
    updated_text = pattern.sub(replace_match, text)
    return updated_text, count

def apply(only_filenames: set[str] | None = None):
    output_dir = settings.transcript_clean_output
    txt_files = [
        f for f in output_dir.glob("*.txt")
        if not f.name.startswith("_")
    ]
    if only_filenames is not None:
        txt_files = [f for f in txt_files if f.name in only_filenames]

    if not txt_files:
        general_logger.info(f"No transcript files found in {output_dir}")
        return

    total_changes = 0

    for i, txt_path in enumerate(txt_files, start=1):
        progress.set_current_file(i, txt_path.name, len(txt_files))

        original_text = txt_path.read_text(encoding="utf-8")
        updated_text, count = apply_replacements(original_text, REPLACEMENTS)

        if updated_text != original_text:
            txt_path.write_text(updated_text, encoding="utf-8")
            general_logger.info(f"Updated: {txt_path} ({count} replacement(s))")
            total_changes += count

        progress.set_file_fraction(1.0)

    if total_changes:
        general_logger.info(f"Applied {total_changes} replacement(s) across {len(txt_files)} file(s)")
    else:
        general_logger.info("No matches found - no files changed.")

if __name__ == "__main__":
    apply()

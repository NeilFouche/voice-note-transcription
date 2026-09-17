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
from pathlib import Path
from datetime import datetime

from config import settings

# Add pairs as you spot recurring issues. Left side: word as it appears
# in transcripts (any case). Right side: what to replace it with.
REPLACEMENTS = {
    "een": "'n",
    "commerciële": "kommersiële",
    "correct": "korrek",
    "commercieel": "kommersieel",
}

def apply_replacements(text: str, replacements: dict[str, str], log: list[tuple[str, str]]) -> str:
    def replace_match(match):
        original = match.group(0)
        key = original.lower()
        if key not in replacements:
            return original
        replacement = replacements[key]
        if original[0].isupper():
            replacement = replacement.capitalize()
        log.append((original, replacement))
        return replacement

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in replacements) + r")\b",
        re.IGNORECASE
    )
    return pattern.sub(replace_match, text)

def apply():
    output_dir = settings.transcript_clean_output
    txt_files = [
        f for f in output_dir.glob("*.txt")
        if not f.name.startswith("_")
    ]

    if not txt_files:
        print(f"No transcript files found in {output_dir}")
        return

    all_changes = []

    for txt_path in txt_files:
        original_text = txt_path.read_text(encoding="utf-8")
        file_changes = []
        updated_text = apply_replacements(original_text, REPLACEMENTS, file_changes)

        if updated_text != original_text:
            txt_path.write_text(updated_text, encoding="utf-8")
            print(f"Updated: {txt_path} ({len(file_changes)} replacement(s))")
            all_changes.extend((txt_path.name, orig, new) for orig, new in file_changes)

    if all_changes:
        with open(settings.general_log_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- Run at {datetime.now().isoformat(timespec='seconds')} ---\n")
            for filename, original, replacement in all_changes:
                f.write(f"{filename}: {original} -> {replacement}\n")

        print(f"\nLogged {len(all_changes)} replacement(s) to {settings.general_log_path.name}")
    else:
        print("No matches found - no files changed.")

if __name__ == "__main__":
    apply()

"""
Shared filename parsing for WhatsApp voice note exports.

Used by audio.py (to sort audio files chronologically before transcribing)
and transcripts.py (to recover each note's original date/time for the
combined Excel output - by the time combine() runs, the timestamp only
survives in the transcript's filename, since serialize() strips segment
timestamps out of the file content).
"""

import re
from datetime import datetime

# Expected input filename format:
# WhatsApp Audio 2026-09-16 at 07.42.13.ogg
# WhatsApp Audio 2026-09-16 at 07.42.13 (1).ogg   (duplicate suffix, also handled)
FILENAME_PATTERN = re.compile(
  r"WhatsApp Audio (\d{4}-\d{2}-\d{2}) at (\d{2}\.\d{2}\.\d{2})(?: \((\d+)\))?"
)

def extract_datetime(filename: str):
    """
    Pull the embedded datetime (and duplicate index) from a Whatsapp audio
    filename. Matches anywhere in the string, so it still works once a
    sequence prefix has been added (e.g. "0007_WhatsApp Audio ...").
    Returns (datetime, duplicate_index), or None if no match.
    """
    match = FILENAME_PATTERN.search(filename)
    if not match:
        return None

    date_str, time_str, dup_index = match.groups()
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H.%M.%S")

    return dt, int(dup_index) if dup_index else 0

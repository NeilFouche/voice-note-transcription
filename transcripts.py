"""
Manages transcripts
"""

import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

import progress
from config import settings
from filenames import extract_datetime
from logging_config import general_logger

SEGMENT_PREFIX = re.compile(r"^\[\d+(?:\.\d+)?s\s*->\s*\d+(?:\.\d+)?s\]\s*")
SEQUENCE_PREFIX = re.compile(r"^(\d+)_")

def serialize_file(text: str, keep_header: bool = False) -> str:
    header_lines = []
    segment_texts = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            header_lines.append(line)
            continue
        stripped = SEGMENT_PREFIX.sub("", line).strip()
        if stripped:
            segment_texts.append(stripped)

    body = " ".join(segment_texts)

    if keep_header and header_lines:
        return " ".join(header_lines) + " " + body

    return body

def serialize(keep_header: bool = False, only_filenames: set[str] | None = None):
    input_dir = settings.transcript_clean_output
    if not input_dir.exists():
        general_logger.info(f"{input_dir} not found")
        return

    output_dir = settings.serialized_repo
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_files = [
        f for f in input_dir.glob("*.txt")
        if not f.name.startswith("_")
    ]
    if only_filenames is not None:
        txt_files = [f for f in txt_files if f.name in only_filenames]

    if not txt_files:
        general_logger.info(f"No transcript files found in {input_dir}")
        return

    for i, txt_path in enumerate(txt_files, start=1):
        progress.set_current_file(i, txt_path.name, len(txt_files))

        original_text = txt_path.read_text(encoding="utf-8")
        serialized = serialize_file(original_text, keep_header)

        output_path = output_dir / txt_path.name
        output_path.write_text(serialized, encoding="utf-8")
        general_logger.info(f"Serialized: {txt_path.name}")
        progress.set_file_fraction(1.0)

    general_logger.info(f"Serialize stage complete - {len(txt_files)} file(s) written to {output_dir}")

def sort_key(path: Path):
    match = SEQUENCE_PREFIX.match(path.name)
    if match:
        return (0, int(match.group(1)))
    return (1, path.name)

def combine(only_filenames: set[str] | None = None):
    """
    Assembles serialized transcripts into one Excel workbook, one row per
    note: Date, Time, Transcription. Date/Time come from the WhatsApp
    timestamp embedded in the original filename (still present via the
    sequence-prefixed transcript filename, e.g. "0007_WhatsApp Audio
    2026-09-16 at 15.42.29.txt") - the segment timestamps themselves were
    already stripped out of the content by serialize(). Files whose name
    doesn't match that pattern (a manually named upload, say) still get a
    row, just with Date/Time left blank rather than guessed.

    only_filenames restricts which serialized files are included - the web
    app passes this so each run's downloaded result reflects exactly what
    was just selected, not every note ever transcribed on this machine.
    Left as None (the default) for the CLI/folder-drop flow in main.py,
    where one continuously growing combined file is the intended design.
    """
    input_dir = settings.serialized_repo
    if not input_dir.exists():
        general_logger.info(f"{input_dir} not found")
        return

    txt_files = [
        f for f in input_dir.glob("*.txt")
        if not f.name.startswith("_")
    ]
    if only_filenames is not None:
        txt_files = [f for f in txt_files if f.name in only_filenames]

    if not txt_files:
        general_logger.info(f"No transcript files found in {input_dir}")
        return

    txt_files.sort(key=sort_key)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Transcriptions"
    sheet.append(["Date", "Time", "Transcription"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"

    row_count = 0

    for i, txt_path in enumerate(txt_files, start=1):
        progress.set_current_file(i, txt_path.name, len(txt_files))

        content = txt_path.read_text(encoding="utf-8").strip()
        if content:
            parsed = extract_datetime(txt_path.name)
            date_value, time_value = (parsed[0].date(), parsed[0].time()) if parsed else (None, None)
            sheet.append([date_value, time_value, content])
            row_count += 1

        progress.set_file_fraction(1.0)

    for row in sheet.iter_rows(min_row=2, max_col=2):
        date_cell, time_cell = row
        if date_cell.value is not None:
            date_cell.number_format = "YYYY-MM-DD"
        if time_cell.value is not None:
            time_cell.number_format = "HH:MM:SS"

    sheet.column_dimensions["A"].width = 12
    sheet.column_dimensions["B"].width = 10
    sheet.column_dimensions["C"].width = 100

    output_dir = settings.final_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / settings.target_filename
    workbook.save(output_path)

    general_logger.info(f"Combine stage complete - combined {row_count} file(s) into {output_path}")

if __name__ == "__main__":
    serialize()
    combine()

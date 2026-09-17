"""
Manages transcripts
"""

import re
from pathlib import Path

from config import settings
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

def serialize(keep_header: bool = False):
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

    if not txt_files:
        general_logger.info(f"No transcript files found in {input_dir}")
        return

    for txt_path in txt_files:
        original_text = txt_path.read_text(encoding="utf-8")
        serialized = serialize_file(original_text, keep_header)

        output_path = output_dir / txt_path.name
        output_path.write_text(serialized, encoding="utf-8")
        general_logger.info(f"Serialized: {txt_path.name}")

    general_logger.info(f"Serialize stage complete - {len(txt_files)} file(s) written to {output_dir}")

def sort_key(path: Path):
    match = SEQUENCE_PREFIX.match(path.name)
    if match:
        return (0, int(match.group(1)))
    return (1, path.name)

def combine(include_filename: bool = False):
    input_dir = settings.serialized_repo
    if not input_dir.exists():
        general_logger.info(f"{input_dir} not found")
        return

    txt_files = [
        f for f in input_dir.glob("*.txt")
        if not f.name.startswith("_")
    ]

    if not txt_files:
        general_logger.info(f"No transcript files found in {input_dir}")
        return

    txt_files.sort(key=sort_key)

    lines = []

    for txt_path in txt_files:
        content = txt_path.read_text(encoding="utf-8").strip()
        if not content:
            continue
        if include_filename:
            lines.append(f"{txt_path.stem}\t{content}")
        else:
            lines.append(content)

    output_dir = settings.final_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / settings.target_filename
    output_path.write_text("\n".join(lines), encoding="utf-8")

    general_logger.info(f"Combine stage complete - combined {len(lines)} file(s) into {output_path}")

if __name__ == "__main__":
    serialize()
    combine()

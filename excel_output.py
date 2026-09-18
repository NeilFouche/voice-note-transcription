"""
Builds the Excel workbook from a batch's transcription rows.
"""

from openpyxl import Workbook
from openpyxl.styles import Font

from logging_config import general_logger


def build_workbook(rows: list[tuple]) -> Workbook:
    """rows: list of (date | None, time | None, transcription text)."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Transcriptions"
    sheet.append(["Date", "Time", "Transcription"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"

    for date_value, time_value, text in rows:
        sheet.append([date_value, time_value, text])

    for row in sheet.iter_rows(min_row=2, max_col=2):
        date_cell, time_cell = row
        if date_cell.value is not None:
            date_cell.number_format = "YYYY-MM-DD"
        if time_cell.value is not None:
            time_cell.number_format = "HH:MM:SS"

    sheet.column_dimensions["A"].width = 12
    sheet.column_dimensions["B"].width = 10
    sheet.column_dimensions["C"].width = 100

    return workbook


def save(rows: list[tuple], output_path) -> None:
    build_workbook(rows).save(output_path)
    general_logger.info(f"Saved {len(rows)} row(s) to {output_path}")

"""
Central logging configuration for the pipeline.

Three loggers, each writing to its own file under Settings.logs_repo:

* general_logger       -> processing_history.log
    Overall pipeline progress: stage start/end, files processed, skipped,
    moved, etc. Also echoed to the console so the tool remains visible
    when run interactively.

* transcription_logger -> transcription.log
    Transcription-specific detail: words not found in the Afrikaans
    dictionary (corrected or not), and replacements applied.

* error_logger         -> errors.log
    Failures encountered at any pipeline stage, with full tracebacks.
"""

import logging

from config import settings

FILE_LOG_FORMAT = "%(asctime)s\t%(levelname)s\t%(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _build_logger(name: str, log_path, also_console: bool = False) -> logging.Logger:
    settings.logs_repo.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(file_handler)

        if also_console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(console_handler)

    return logger


general_logger = _build_logger("voice_notes.general", settings.general_log_path, also_console=True)
transcription_logger = _build_logger("voice_notes.transcription", settings.transcription_log_path)
error_logger = _build_logger("voice_notes.errors", settings.errors_log_path)

import sys

import audio
import replacements
import transcripts
from config import settings
from logging_config import general_logger, error_logger


def main():
    settings.ensure_dirs()
    general_logger.info("=== Pipeline run started ===")
    try:
        audio.transcribe()
        replacements.apply()
        transcripts.serialize()
        transcripts.combine()
        general_logger.info("=== Pipeline run finished ===")
    except Exception:
        error_logger.exception("Pipeline failed")
        general_logger.error("Pipeline failed - see errors.log for details")
        sys.exit(1)


if __name__ == "__main__":
    main()

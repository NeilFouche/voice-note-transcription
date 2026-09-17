import sys
import traceback

import audio
import replacements
import transcripts
from config import settings


def main():
    settings.ensure_dirs()
    try:
        audio.transcribe()
        replacements.apply()
        transcripts.serialize()
        transcripts.combine()
    except Exception:
        print("Pipeline failed:")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

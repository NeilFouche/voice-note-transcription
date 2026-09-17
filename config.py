from dataclasses import dataclass
from pathlib import Path

@dataclass
class Settings:
    input_dir: Path
    transcript_clean_output: Path 
    transcript_error_output: Path
    dictionaries_repo: Path
    processing_repo: Path
    logs_repo: Path
    target_filename: str = "transcriptions.txt"
    general_log_filename: str = "processing_history.log"
    transcription_log_filename: str = "transcription.log"
    errors_log_filename: str = "errors.log"
    model_size: str = "large-v3"
    default_language: str = "af"

    def __post_init__(self):
        self.general_log_path: Path = self.logs_repo / self.general_log_filename
        self.transcription_log_path: Path = self.logs_repo / self.transcription_log_filename
        self.errors_log_path: Path = self.logs_repo / self.errors_log_filename
        self.serialized_repo: Path = self.processing_repo / "serialized"
        self.combined_repo: Path = self.processing_repo / "combined"

    def ensure_dirs(self):
        """Create every directory the pipeline reads from or writes to."""
        for directory in (
            self.input_dir,
            self.transcript_clean_output,
            self.transcript_error_output,
            self.processing_repo,
            self.serialized_repo,
            self.combined_repo,
            self.logs_repo,
        ):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings(
    input_dir=Path("Audio Files"),
    transcript_clean_output=Path("Transcribed"),
    transcript_error_output=Path("Not Transcribed"),
    dictionaries_repo=Path("dictionaries"),
    processing_repo=Path(".processing"),
    logs_repo=Path("logs"),
    model_size="large-v3",
    default_language="af"
)

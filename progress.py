"""
Shared in-process progress state for the web UI's polling endpoint.

Progress is derived from real markers the pipeline reports as it works -
not from a time-based countdown. Each stage owns a percentage band of the
overall bar, sized by how much of a typical run it takes (transcription is
by far the dominant cost; the other stages are simple text processing over
the same small set of files and finish in a fraction of a second each).
Within a stage, progress advances by files completed, and - during
transcription specifically - by how far into the current file's audio
Whisper has actually gotten, using the segment timestamps it already
reports (segment.end vs the file's total duration).

The pipeline runs in a background thread per web request (see webapp.py);
this module is the thread-safe handoff so webapp.py's /status endpoint can
report what that thread is doing right now. The CLI pipeline (main.py)
never reads this - updating it from there is harmless, just unobserved.

Single global state is fine here: the app only ever runs one pipeline job
at a time (enforced by webapp.py's pipeline lock), so there's nothing to
key by job id.
"""

import threading
import time

_lock = threading.Lock()

# Cooperative cancellation: a single in-flight Whisper inference call can't
# be interrupted mid-computation without a much bigger architecture change
# (running it in a killable subprocess), so cancellation is checked at safe
# points between files instead - see audio.py's transcribe() loop. This
# means the file currently being transcribed still finishes; nothing after
# it starts.
_cancel_event = threading.Event()

# (stage -> (start_pct, end_pct)), in pipeline order. Transcription is the
# dominant cost by far, so it owns most of the bar; the rest are thin
# bands that mostly exist so the bar still visibly moves during them.
STAGE_BANDS = {
    "idle": (0, 0),
    "starting": (0, 0),
    "transcribing": (0, 90),
    "replacements": (90, 94),
    "serializing": (94, 97),
    "combining": (97, 100),
}

_state = {
    "running": False,
    "stage": "idle",
    "current_file": None,
    "file_index": 0,
    "file_total": 0,
    "file_fraction": 0.0,  # 0..1 progress through the current file (transcribing stage only)
    "started_at": None,
    "error": None,
    "cancelled": False,
}


def reset():
    with _lock:
        _state.update(
            running=True,
            stage="starting",
            current_file=None,
            file_index=0,
            file_total=0,
            file_fraction=0.0,
            started_at=time.monotonic(),
            error=None,
            cancelled=False,
        )
    _cancel_event.clear()


def request_cancel():
    _cancel_event.set()


def is_cancel_requested() -> bool:
    return _cancel_event.is_set()


def set_stage(stage: str):
    """Move to a new pipeline stage. Resets the per-file counters so the
    new stage's progress starts at the bottom of its own band, rather than
    carrying over the previous stage's file count."""
    with _lock:
        _state["stage"] = stage
        _state["current_file"] = None
        _state["file_index"] = 0
        _state["file_total"] = 0
        _state["file_fraction"] = 0.0


def set_current_file(index: int, filename: str, total: int):
    with _lock:
        _state["file_index"] = index
        _state["current_file"] = filename
        _state["file_total"] = total
        _state["file_fraction"] = 0.0


def set_file_fraction(fraction: float):
    """Sub-file progress within the current file - e.g. how far through
    its audio Whisper has transcribed so far (segment.end / duration).
    Only meaningful during the transcribing stage; harmless elsewhere."""
    with _lock:
        _state["file_fraction"] = max(0.0, min(1.0, fraction))


def finish(error: str | None = None, cancelled: bool = False):
    with _lock:
        _state["running"] = False
        _state["error"] = error
        _state["cancelled"] = cancelled
        if not error and not cancelled:
            _state["stage"] = "done"
        # On error or cancellation, `stage` is deliberately left as
        # whatever it was, so the bar freezes at that point instead of
        # resetting or jumping to "done".


def _percent(state: dict) -> float:
    if state["stage"] == "done":
        return 100.0

    start, end = STAGE_BANDS.get(state["stage"], (0, 0))

    total = state["file_total"]
    if total <= 0:
        return float(start)

    completed_files = max(0, state["file_index"] - 1) + state["file_fraction"]
    fraction_within_stage = min(1.0, completed_files / total)
    return start + fraction_within_stage * (end - start)


def snapshot() -> dict:
    with _lock:
        data = dict(_state)

    data["elapsed_seconds"] = time.monotonic() - data["started_at"] if data["started_at"] is not None else 0
    data["percent"] = round(_percent(data), 1)
    return data

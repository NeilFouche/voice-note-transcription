"""
Shared in-process state for a single in-flight model download, polled by
the desktop app's UI the same way progress.py is for transcription runs.

Kept separate from progress.py since downloading and transcribing are
different concerns with different lifecycles, even though in practice
only one of either ever runs at a time (the UI disables Transcribe while
a download is active, and there's no download affordance while
transcribing).
"""

import threading

_lock = threading.Lock()

_state = {
    "active": False,
    "model_size": None,
    "percent": 0.0,
    "error": None,
}


def start(model_size: str):
    with _lock:
        _state.update(active=True, model_size=model_size, percent=0.0, error=None)


def set_percent(percent: float):
    with _lock:
        if _state["active"]:
            _state["percent"] = max(0.0, min(100.0, percent))


def finish(error: str | None = None):
    with _lock:
        _state["active"] = False
        _state["error"] = error


def snapshot() -> dict:
    with _lock:
        return dict(_state)

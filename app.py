"""
Desktop app entry point (pywebview): a native window wrapping the same
UI a browser would show, but talking to Python directly through
pywebview's js_api bridge instead of over HTTP - no local web server,
no sockets, nothing that looks like "uploading to a website".

File selection uses native OS dialogs (Api.pick_files / pick_folder)
rather than HTML5 drag-and-drop: pywebview's dialogs hand back real file
paths directly, which is both more reliable across the different native
webview backends pywebview can run on and lets Python read the files
directly instead of shuttling their bytes through the JS bridge.
Saving works the same way, via a native Save dialog instead of a browser
download.

Progress is still polled on a 1-second interval from JS (now calling
Api.get_status() instead of fetch("/status")) rather than pushed from
Python - same architecture as before, just a different transport.
"""

import ctypes
import sys
import threading
import time
from pathlib import Path

import av
import webview
from webview import FileDialog

import excel_output
import performance
import progress
import transcription
from config import RESOURCE_DIR, settings
from logging_config import error_logger, general_logger

WINDOW_TITLE = "Voice Note Transcription"
ICON_PATH = RESOURCE_DIR / "icon.ico"


def _probe_duration(path: str) -> float:
    try:
        with av.open(path) as container:
            if container.duration is not None:
                return container.duration / av.time_base
    except Exception:
        pass
    return 0.0


class Api:
    def __init__(self):
        self._window = None
        self._run_lock = threading.Lock()
        self._last_rows: list[tuple] = []
        self._last_errors: list[tuple[str, str]] = []

    def set_window(self, window):
        self._window = window

    # --- file selection --------------------------------------------------

    def pick_files(self):
        paths = self._window.create_file_dialog(
            FileDialog.OPEN,
            allow_multiple=True,
            file_types=("Audio files (*.ogg;*.mp3;*.wav;*.mp4;*.flac;*.opus)", "All files (*.*)"),
        )
        return list(paths) if paths else []

    def pick_folder(self):
        paths = self._window.create_file_dialog(FileDialog.FOLDER)
        if not paths:
            return []
        folder = Path(paths[0])
        return sorted(
            str(p) for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in transcription.AUDIO_EXTENSIONS
        )

    # --- pre-run estimate --------------------------------------------------

    def get_durations(self, paths: list[str]) -> list[float]:
        return [_probe_duration(p) for p in paths]

    def estimate(self, total_seconds: float):
        return performance.estimate_seconds(total_seconds)

    # --- run lifecycle -----------------------------------------------------

    def get_status(self):
        return progress.snapshot()

    def cancel(self):
        # Cooperative: takes effect once the file currently being
        # transcribed finishes (see transcription.py) rather than
        # instantly - there's no safe way to interrupt a single in-flight
        # Whisper inference call. Harmless to call when nothing is running.
        progress.request_cancel()
        return {"status": "cancelling"}

    def transcribe(self, paths: list[str]):
        if not paths:
            return {"status": "error", "detail": "No files selected"}

        if not self._run_lock.acquire(blocking=False):
            return {"status": "error", "detail": "A transcription run is already in progress"}

        progress.reset()
        threading.Thread(target=self._run, args=(paths,), daemon=True).start()
        return {"status": "started"}

    def _run(self, paths: list[str]):
        try:
            file_paths = [Path(p) for p in paths]
            completed, result = transcription.transcribe_batch(file_paths)
            if not completed:
                progress.finish(cancelled=True)
                return

            self._last_rows = result.rows
            self._last_errors = result.errors
            progress.set_stage("saving")
            progress.finish()
        except Exception as e:
            error_logger.exception("Transcription run failed")
            progress.finish(error=str(e))
        finally:
            self._run_lock.release()

    def get_last_errors(self):
        return [{"filename": name, "error": err} for name, err in self._last_errors]

    # --- saving the result ---------------------------------------------------

    def save_as(self):
        if not self._last_rows:
            return {"status": "error", "detail": "No transcript available yet"}

        chosen = self._window.create_file_dialog(
            FileDialog.SAVE,
            save_filename=settings.target_filename,
            file_types=("Excel files (*.xlsx)",),
        )
        if not chosen:
            return {"status": "cancelled"}

        path = chosen[0]
        try:
            excel_output.save(self._last_rows, path)
        except Exception as e:
            error_logger.exception("Failed to save workbook")
            return {"status": "error", "detail": str(e)}

        return {"status": "saved", "path": path}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Note Transcription</title>
<style>
  :root { color-scheme: light dark; }
  [hidden] { display: none !important; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    max-width: 640px;
    margin: 32px auto;
    padding: 0 20px;
    color: #1a1a1a;
    background: #fafafa;
  }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  p.sub { color: #555; margin-top: 0; }
  #dropzone {
    border: 2px dashed #999;
    border-radius: 10px;
    padding: 28px 16px;
    text-align: center;
    color: #555;
    background: #fff;
  }
  #dropzone.has-files { border-style: solid; border-color: #15803d; background: #f0fdf4; color: #166534; }
  #dropzoneIcon { font-size: 1.8rem; line-height: 1; margin-bottom: 8px; }
  .choose-actions { display: flex; gap: 10px; justify-content: center; margin-top: 14px; }
  #fileList { list-style: none; padding: 0; margin: 16px 0; }
  #fileList li {
    padding: 8px 12px;
    background: #fff;
    border: 1px solid #e5e5e5;
    border-radius: 6px;
    margin-bottom: 6px;
    font-size: 0.9rem;
  }
  .meta { font-size: 0.85rem; color: #555; margin-top: 6px; min-height: 1.2em; }
  .link-btn {
    background: none;
    border: none;
    color: #2563eb;
    font-size: 0.85rem;
    cursor: pointer;
    padding: 4px 0;
    text-decoration: underline;
  }
  .actions { display: flex; gap: 10px; align-items: center; margin-top: 14px; }
  button, a.download {
    font-size: 1rem;
    padding: 10px 20px;
    border-radius: 8px;
    border: none;
    cursor: pointer;
    text-decoration: none;
    display: inline-block;
  }
  button { background: #2563eb; color: #fff; }
  button:disabled { background: #b7c6e6; cursor: not-allowed; }
  button.secondary { background: #fff; color: #2563eb; border: 1px solid #2563eb; }
  button.secondary:disabled { background: #fff; color: #b7c6e6; border-color: #b7c6e6; cursor: not-allowed; }
  button.cancel { background: #fff; color: #b91c1c; border: 1px solid #b91c1c; }
  button.cancel:disabled { background: #fff; color: #d1a3a3; border-color: #d1a3a3; }
  a.download { background: #15803d; color: #fff; }
  a.download.disabled { pointer-events: none; opacity: .5; background: #9ca3af; }
  #progressWrap {
    background: #e5e5e5;
    border-radius: 8px;
    height: 10px;
    overflow: hidden;
    margin-top: 16px;
  }
  #progressBar { height: 100%; width: 0%; background: #2563eb; transition: width .3s ease; }
  #status { margin-top: 10px; font-size: 0.95rem; }
  #status.error { color: #b91c1c; }
  #status.done { color: #15803d; }
  #status.busy::after {
    content: "...";
    display: inline-block;
    width: 0;
    overflow: hidden;
    vertical-align: bottom;
    animation: statusDots 1.4s steps(4, end) infinite;
  }
  @keyframes statusDots { to { width: 1em; } }
  @media (prefers-reduced-motion: reduce) {
    #status.busy::after { content: "…"; width: auto; animation: none; }
  }
</style>
</head>
<body>
  <h1>Voice Note Transcription</h1>
  <p class="sub">Choose the files to transcribe, then click <b>Transcribe</b>.</p>

  <div id="dropzone">
    <div id="dropzoneIcon">📂</div>
    <div id="dropzoneText">No files chosen yet</div>
    <div class="choose-actions">
      <button type="button" id="chooseFilesBtn">Choose files</button>
      <button type="button" id="chooseFolderBtn" class="secondary">Choose a folder</button>
    </div>
  </div>
  <ul id="fileList"></ul>
  <button type="button" id="clearBtn" class="link-btn" hidden>Clear selection</button>
  <div id="estimate" class="meta"></div>

  <div class="actions">
    <button type="button" id="cancelBtn" class="cancel" hidden>Cancel</button>
    <button id="transcribeBtn" disabled>Transcribe</button>
    <button type="button" id="saveBtn" disabled>Save Transcript</button>
  </div>

  <div id="progressWrap" hidden><div id="progressBar"></div></div>
  <div id="status"></div>

<script>
  const dropzone = document.getElementById("dropzone");
  const dropzoneIcon = document.getElementById("dropzoneIcon");
  const dropzoneText = document.getElementById("dropzoneText");
  const chooseFilesBtn = document.getElementById("chooseFilesBtn");
  const chooseFolderBtn = document.getElementById("chooseFolderBtn");
  const fileList = document.getElementById("fileList");
  const estimateEl = document.getElementById("estimate");
  const clearBtn = document.getElementById("clearBtn");
  const transcribeBtn = document.getElementById("transcribeBtn");
  const cancelBtn = document.getElementById("cancelBtn");
  const saveBtn = document.getElementById("saveBtn");
  const progressWrap = document.getElementById("progressWrap");
  const progressBar = document.getElementById("progressBar");
  const status = document.getElementById("status");

  const FILE_LIST_DISPLAY_LIMIT = 10;

  let selectedPaths = [];
  let totalAudioSeconds = 0;
  let pollTimer = null;
  let cancelPending = false;

  function basename(path) {
    return path.split(/[\\/]/).pop();
  }

  function formatDuration(totalSeconds) {
    if (!isFinite(totalSeconds) || totalSeconds <= 0) return "0s";
    const m = Math.floor(totalSeconds / 60);
    const s = Math.round(totalSeconds % 60);
    return m === 0 ? s + "s" : m + "m " + s + "s";
  }

  function setBusyStatus(text) {
    status.className = "busy";
    status.textContent = text;
  }

  function setTerminalStatus(text, cls) {
    status.className = cls || "";
    status.textContent = text;
  }

  function updateDropzoneDisplay() {
    if (selectedPaths.length === 0) {
      dropzone.classList.remove("has-files");
      dropzoneIcon.textContent = "📂";
      dropzoneText.textContent = "No files chosen yet";
      return;
    }
    dropzone.classList.add("has-files");
    dropzoneIcon.textContent = "✓";
    let text = selectedPaths.length + " file" + (selectedPaths.length === 1 ? "" : "s") + " selected";
    if (totalAudioSeconds > 0) {
      text += " · " + formatDuration(totalAudioSeconds) + " of audio";
    }
    dropzoneText.textContent = text;
  }

  async function updateEstimate() {
    if (selectedPaths.length === 0) {
      estimateEl.textContent = "";
      totalAudioSeconds = 0;
      return;
    }
    const durations = await window.pywebview.api.get_durations(selectedPaths);
    totalAudioSeconds = durations.reduce((a, b) => a + b, 0);
    updateDropzoneDisplay();

    if (totalAudioSeconds > 0) {
      const data = await window.pywebview.api.estimate(totalAudioSeconds);
      const basis = data.is_measured
        ? "based on " + data.based_on_runs + " previous run" + (data.based_on_runs === 1 ? "" : "s") + " on this computer"
        : "rough estimate - not yet calibrated to this computer";
      estimateEl.textContent = "Estimated transcription time: ~" + formatDuration(data.estimated_seconds) + " (" + basis + ")";
    } else {
      estimateEl.textContent = "";
    }
  }

  function renderFileList() {
    if (selectedPaths.length > 0 && selectedPaths.length <= FILE_LIST_DISPLAY_LIMIT) {
      fileList.hidden = false;
      fileList.innerHTML = "";
      for (const path of selectedPaths) {
        const li = document.createElement("li");
        li.textContent = basename(path);
        fileList.appendChild(li);
      }
    } else {
      fileList.hidden = true;
      fileList.innerHTML = "";
    }
    transcribeBtn.disabled = selectedPaths.length === 0;
    clearBtn.hidden = selectedPaths.length === 0;
    updateDropzoneDisplay();
    updateEstimate();
  }

  function addPaths(paths) {
    const existing = new Set(selectedPaths);
    for (const p of paths) {
      if (!existing.has(p)) {
        selectedPaths.push(p);
        existing.add(p);
      }
    }
    renderFileList();
  }

  function clearSelection() {
    selectedPaths = [];
    renderFileList();
  }

  chooseFilesBtn.addEventListener("click", async () => {
    const paths = await window.pywebview.api.pick_files();
    if (paths && paths.length) addPaths(paths);
  });

  chooseFolderBtn.addEventListener("click", async () => {
    const paths = await window.pywebview.api.pick_folder();
    if (paths && paths.length) addPaths(paths);
  });

  clearBtn.addEventListener("click", () => clearSelection());

  const STAGE_LABELS = {
    starting: "Starting",
    transcribing: "Transcribing",
    saving: "Saving",
  };

  async function pollStatus() {
    const data = await window.pywebview.api.get_status();

    progressBar.style.width = data.percent + "%";

    if (data.cancelled) {
      clearInterval(pollTimer);
      pollTimer = null;
      cancelPending = false;
      setTerminalStatus("Cancelled.");
      cancelBtn.hidden = true;
      progressWrap.hidden = true;
      transcribeBtn.disabled = selectedPaths.length === 0;
      return;
    }

    if (data.error) {
      clearInterval(pollTimer);
      pollTimer = null;
      cancelPending = false;
      setTerminalStatus("Error: " + data.error, "error");
      cancelBtn.hidden = true;
      transcribeBtn.disabled = selectedPaths.length === 0;
      return;
    }

    if (data.stage === "done") {
      clearInterval(pollTimer);
      pollTimer = null;
      cancelPending = false;
      setTerminalStatus("Done - Transcribed in " + formatDuration(data.elapsed_seconds) + ".", "done");
      cancelBtn.hidden = true;
      saveBtn.disabled = false;
      transcribeBtn.disabled = selectedPaths.length === 0;
      return;
    }

    if (cancelPending) {
      setBusyStatus("Cancelling the transcription");
      return;
    }

    let text = STAGE_LABELS[data.stage] || data.stage;
    if (data.current_file && data.file_total) {
      text += " (" + data.file_index + " of " + data.file_total + ": " + data.current_file + ")";
    }
    setBusyStatus(text);
  }

  transcribeBtn.addEventListener("click", async () => {
    transcribeBtn.disabled = true;
    saveBtn.disabled = true;
    cancelPending = false;
    progressWrap.hidden = false;
    progressBar.style.width = "0%";
    setBusyStatus("Starting");

    const response = await window.pywebview.api.transcribe(selectedPaths);
    if (response.status !== "started") {
      setTerminalStatus("Error: " + (response.detail || "Transcription failed"), "error");
      transcribeBtn.disabled = selectedPaths.length === 0;
      progressWrap.hidden = true;
      return;
    }

    cancelBtn.hidden = false;
    cancelBtn.disabled = false;
    pollTimer = setInterval(pollStatus, 1000);
    pollStatus();
  });

  cancelBtn.addEventListener("click", async () => {
    // Cooperative cancellation: the file currently being transcribed still
    // finishes (a single in-flight Whisper call can't be interrupted
    // mid-computation), so this takes effect at the next safe checkpoint
    // rather than instantly.
    cancelPending = true;
    cancelBtn.disabled = true;
    setBusyStatus("Cancelling the transcription");
    await window.pywebview.api.cancel();
  });

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    const response = await window.pywebview.api.save_as();
    if (response.status === "saved") {
      setTerminalStatus("Saved to " + response.path, "done");
    } else if (response.status === "cancelled") {
      // user backed out of the save dialog - leave the prior status alone
    } else {
      setTerminalStatus("Error: " + (response.detail || "Could not save"), "error");
    }
    saveBtn.disabled = false;
  });
</script>
</body>
</html>
"""


def _set_window_icon(title: str, icon_path: Path):
    """
    Sets the titlebar/taskbar icon via the raw Win32 API, since pywebview's
    Windows backend (edgechromium/WebView2 - the modern one, with good
    CSS/JS support) doesn't set a window icon at all; only its older
    winforms backend does. The window frame is still a WinForms Form
    under the hood either way, so this isn't fighting the toolkit, just
    doing what that other backend already does, ourselves.

    Runs as webview.start()'s startup callback, which fires before the
    native window is necessarily visible/findable yet, so this polls
    briefly for the window to exist rather than assuming it's there
    immediately.
    """
    if sys.platform != "win32" or not icon_path.exists():
        return

    try:
        user32 = ctypes.windll.user32
        hwnd = 0
        for _ in range(50):  # up to ~5s
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            return

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        big = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        small = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
    except Exception:
        error_logger.exception("Failed to set window icon")


def main():
    settings.ensure_dirs()
    api = Api()
    window = webview.create_window(
        WINDOW_TITLE,
        html=PAGE,
        js_api=api,
        width=720,
        height=700,
        min_size=(600, 520),
    )
    api.set_window(window)
    general_logger.info("Starting desktop app")
    webview.start(_set_window_icon, args=(WINDOW_TITLE, ICON_PATH))


if __name__ == "__main__":
    main()

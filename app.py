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

import download_progress
import excel_output
import performance
import preferences
import progress
import transcription
from config import RESOURCE_DIR, settings
from logging_config import error_logger, general_logger

WINDOW_TITLE = "Voice Note Transcription"
ICON_PATH = RESOURCE_DIR / "icon.ico"

# Friendly labels for the model picker - end users shouldn't need to know
# what "large-v3" means. Ordered fast-to-accurate; "large-v3" matches
# config.py's default and whatever the packaged build actually bundles -
# it's the only one of the three that tested as acceptable quality.
MODEL_OPTIONS = [
    {"value": "medium", "label": "Fast", "description": "Quicker, less accurate"},
    {"value": "large-v3-turbo", "label": "Balanced", "description": "A middle ground"},
    {"value": "large-v3", "label": "Most accurate", "description": "Recommended"},
]
_MODEL_VALUES = {opt["value"] for opt in MODEL_OPTIONS}


def _read_fluent_bundle_js() -> str:
    """
    Microsoft's official Fluent UI Web Components library (real
    <fluent-radio>, <fluent-button>, etc. custom elements, not CSS
    imitating them), bundled locally with esbuild - see
    vendor/README.md for how to regenerate it. Loaded the same way as
    every other resource here: read back at runtime and inlined into the
    page, since pywebview serves PAGE as an in-memory HTML string with no
    web server behind it for a <script src="..."> to fetch from.
    """
    path = RESOURCE_DIR / "vendor" / "fluent-web-components.min.js"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        general_logger.warning(f"Fluent UI bundle not found at {path} - page will render unstyled")
        return ""


_FLUENT_BUNDLE_JS = _read_fluent_bundle_js()


def _probe_duration(path: str) -> float:
    try:
        with av.open(path) as container:
            if container.duration is not None:
                return container.duration / av.time_base
    except Exception:
        pass
    return 0.0


def _file_size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


class Api:
    def __init__(self):
        self._window = None
        self._run_lock = threading.Lock()
        self._download_lock = threading.Lock()
        self._last_rows: list[tuple] = []
        self._last_errors: list[tuple[str, str]] = []
        # Falls back to the configured/bundled default until the user has
        # ever picked something else via the dropdown.
        self._model_size = preferences.get_model_size() or settings.model_size
        if self._model_size not in _MODEL_VALUES:
            self._model_size = settings.model_size

    def set_window(self, window):
        self._window = window

    # --- model selection --------------------------------------------------

    def get_model_options(self):
        """Every option, whether it's already available offline, and
        which one is currently selected - enough for the UI to render the
        dropdown and a "needs download" hint without a second round trip."""
        return {
            "options": [
                {**opt, "available": transcription.is_model_available_locally(opt["value"])}
                for opt in MODEL_OPTIONS
            ],
            "selected": self._model_size,
        }

    def set_model_size(self, model_size: str):
        if model_size not in _MODEL_VALUES:
            return {"status": "error", "detail": f"Unknown model '{model_size}'"}

        self._model_size = model_size
        preferences.set_model_size(model_size)
        return {
            "status": "ok",
            "available": transcription.is_model_available_locally(model_size),
        }

    def download_model_size(self, model_size: str):
        if model_size not in _MODEL_VALUES:
            return {"status": "error", "detail": f"Unknown model '{model_size}'"}

        if not self._download_lock.acquire(blocking=False):
            return {"status": "error", "detail": "A download is already in progress"}

        threading.Thread(target=self._download, args=(model_size,), daemon=True).start()
        return {"status": "started"}

    def _download(self, model_size: str):
        try:
            transcription.download_model_size(model_size)
        finally:
            self._download_lock.release()

    def get_download_status(self):
        return download_progress.snapshot()

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

    def get_file_info(self, paths: list[str]) -> list[dict]:
        return [{"duration": _probe_duration(p), "size": _file_size(p)} for p in paths]

    def estimate(self, total_seconds: float):
        return performance.estimate_seconds(total_seconds, self._model_size)

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
            completed, result = transcription.transcribe_batch(file_paths, model_size=self._model_size)
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


_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Note Transcription</title>
<script type="module">
__FLUENT_BUNDLE_JS__
</script>
<style>
  :root { color-scheme: light dark; }
  [hidden] { display: none !important; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--colorNeutralBackground3);
    color: var(--colorNeutralForeground1);
    font-family: var(--fontFamilyBase);
    font-size: 14px;
  }
  .page { max-width: 640px; margin: 0 auto; padding: 28px 24px 40px; }
  .page-title { margin: 0; font-size: 28px; font-weight: 600; }
  .page-subtitle { margin: 4px 0 0; color: var(--colorNeutralForeground2); }
  .section-label { margin: 20px 2px 6px; font-size: 13px; font-weight: 600; color: var(--colorNeutralForeground2); }

  .card {
    background: var(--colorNeutralBackground1);
    border: 1px solid var(--colorNeutralStroke2);
    border-radius: var(--borderRadiusLarge);
    margin-top: 16px;
    overflow: hidden;
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px 8px 16px;
    border-bottom: 1px solid var(--colorNeutralStroke2);
  }
  .card-header-text { color: var(--colorNeutralForeground2); font-size: 13px; }

  .row { display: flex; align-items: center; padding: 14px 16px; gap: 14px; }
  .row-icon {
    flex: none;
    width: 32px;
    height: 32px;
    display: grid;
    place-items: center;
    font-size: 17px;
    border-radius: var(--borderRadiusMedium);
    background: var(--colorNeutralBackground3);
  }
  .row-text { flex: 1 1 auto; min-width: 0; }
  .row-title { font-size: 14px; color: var(--colorNeutralForeground1); }
  .row-desc { font-size: 12.5px; color: var(--colorNeutralForeground2); margin-top: 2px; }
  .row-desc.needs-download { color: var(--colorStatusWarningForeground1); }
  .row-value {
    flex: none;
    margin-left: auto;
    color: var(--colorNeutralForeground2);
    font-size: 13px;
    text-align: right;
    padding-left: 12px;
  }

  fluent-radio-group { display: block; }
  label.model-row { cursor: pointer; }
  label.model-row:hover { background: var(--colorNeutralBackground1Hover); }
  .row fluent-radio { flex: none; }

  .row-trailing {
    flex: none;
    display: flex;
    align-items: center;
  }
  .download-icon-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets", sans-serif;
    font-size: 17px;
    line-height: 1;
    color: var(--colorNeutralForeground2);
    padding: 6px;
    border-radius: var(--borderRadiusMedium);
  }
  .download-icon-btn:hover { background: var(--colorNeutralBackground1Hover); color: var(--colorBrandForeground1); }
  .download-progress { width: 72px; }

  .file-list { list-style: none; margin: 0; padding: 4px 0; max-height: 220px; overflow-y: auto; }
  .file-list li { padding: 6px 16px; }
  .file-list .file-name {
    font-size: 13px;
    color: var(--colorNeutralForeground1);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .file-list .file-meta { font-size: 12px; color: var(--colorNeutralForeground2); margin-top: 1px; }

  .button-row { display: flex; gap: 10px; margin-top: 20px; }

  fluent-progress-bar { margin-top: 20px; }

  .status { margin-top: 10px; font-size: 13px; color: var(--colorNeutralForeground2); }
  .status.error { color: var(--colorPaletteRedForeground1); }
  .status.done { color: var(--colorPaletteGreenForeground1); }
  .status.busy::after {
    content: "...";
    display: inline-block;
    width: 0;
    overflow: hidden;
    vertical-align: bottom;
    animation: statusDots 1.4s steps(4, end) infinite;
  }
  @keyframes statusDots { to { width: 1em; } }
  @media (prefers-reduced-motion: reduce) {
    .status.busy::after { content: "…"; width: auto; animation: none; }
  }
</style>
</head>
<body>
  <div class="page">
    <h1 class="page-title">Voice Note Transcription</h1>
    <p class="page-subtitle">Choose the files to transcribe, then click Transcribe.</p>

    <section class="card">
      <div class="row">
        <div class="row-icon">🎙️</div>
        <div class="row-text">
          <div class="row-title">Choose files</div>
          <div class="row-desc">Select one or more audio files</div>
        </div>
        <fluent-button id="chooseFilesBtn">Browse files</fluent-button>
      </div>
      <fluent-divider appearance="subtle"></fluent-divider>
      <div class="row">
        <div class="row-icon">📁</div>
        <div class="row-text">
          <div class="row-title">Choose a folder</div>
          <div class="row-desc">Select every audio file in a folder</div>
        </div>
        <fluent-button id="chooseFolderBtn">Browse folder</fluent-button>
      </div>
    </section>

    <section class="card" id="fileListCard" hidden>
      <div class="card-header">
        <span id="fileListSummary" class="card-header-text"></span>
        <fluent-button id="clearBtn" appearance="transparent" size="small">Clear selection</fluent-button>
      </div>
      <ul id="fileList" class="file-list"></ul>
    </section>

    <div class="section-label">Accuracy</div>
    <section class="card">
      <fluent-radio-group id="modelGroup" name="modelSize" orientation="vertical"></fluent-radio-group>
    </section>

    <section class="card" id="detailsCard" hidden>
      <div class="row">
        <div class="row-icon">⏱️</div>
        <div class="row-text">
          <div class="row-title">Estimated transcription time</div>
        </div>
        <div class="row-value" id="detailsEstimate"></div>
      </div>
    </section>

    <fluent-progress-bar id="progressBar" min="0" max="100" value="0" hidden></fluent-progress-bar>
    <div class="status" id="status"></div>

    <div class="button-row">
      <fluent-button id="cancelBtn" hidden>Cancel</fluent-button>
      <fluent-button id="transcribeBtn" disabled>Transcribe</fluent-button>
      <fluent-button id="saveBtn" disabled>Save Transcript</fluent-button>
    </div>
  </div>

<script>
  const chooseFilesBtn = document.getElementById("chooseFilesBtn");
  const chooseFolderBtn = document.getElementById("chooseFolderBtn");
  const fileListCard = document.getElementById("fileListCard");
  const fileListSummary = document.getElementById("fileListSummary");
  const fileList = document.getElementById("fileList");
  const clearBtn = document.getElementById("clearBtn");
  const modelGroup = document.getElementById("modelGroup");
  const detailsCard = document.getElementById("detailsCard");
  const detailsEstimate = document.getElementById("detailsEstimate");
  const transcribeBtn = document.getElementById("transcribeBtn");
  const cancelBtn = document.getElementById("cancelBtn");
  const saveBtn = document.getElementById("saveBtn");
  const progressBar = document.getElementById("progressBar");
  const status = document.getElementById("status");

  const FILE_LIST_DISPLAY_LIMIT = 10;

  let selectedPaths = [];
  let totalAudioSeconds = 0;
  let pollTimer = null;
  let cancelPending = false;
  let modelDownloadInProgress = false;
  let modelRowsByValue = {};
  let currentSelectedValue = null;

  // The selected-but-not-downloaded row gets an actionable prompt instead
  // of its normal description, pointing at the download icon; every other
  // row (selected-and-available, or not-selected-at-all) just shows its
  // normal description.
  function updateRowDescription(value, isSelected) {
    const row = modelRowsByValue[value];
    if (!row) return;
    const showDownloadPrompt = !row.available && isSelected;
    row.descEl.classList.toggle("needs-download", showDownloadPrompt);
    row.descEl.textContent = showDownloadPrompt
      ? "Click download icon to download model"
      : row.description;
  }

  // Transcribe needs files selected, the currently selected model to
  // already be downloaded, and no download already running against it -
  // otherwise it'd kick off transcribe_batch()'s own on-demand download
  // with no progress reporting, which is exactly what the explicit
  // download icon exists to avoid.
  function canTranscribe() {
    if (selectedPaths.length === 0) return false;
    if (modelDownloadInProgress) return false;
    const row = modelRowsByValue[currentSelectedValue];
    if (row && !row.available) return false;
    return true;
  }

  function updateTranscribeButtonState() {
    transcribeBtn.disabled = !canTranscribe();
  }

  function basename(path) {
    return path.split(/[\\/]/).pop();
  }

  // "parent-folder/filename.ext" instead of the full absolute path - short
  // enough to actually read, while still disambiguating files that share a
  // name across different folders.
  function parentAndName(path) {
    const parts = path.split(/[\\/]/).filter(Boolean);
    const name = parts.pop() || path;
    const parent = parts.pop();
    return parent ? parent + "/" + name : name;
  }

  function formatDuration(totalSeconds) {
    if (!isFinite(totalSeconds) || totalSeconds <= 0) return "0s";
    const m = Math.floor(totalSeconds / 60);
    const s = Math.round(totalSeconds % 60);
    return m === 0 ? s + "s" : m + "m " + s + "s";
  }

  function formatFileSize(bytes) {
    if (!isFinite(bytes) || bytes <= 0) return "";
    if (bytes < 1024) return bytes + " B";
    const kb = bytes / 1024;
    if (kb < 1024) return kb.toFixed(kb < 10 ? 1 : 0) + " KB";
    const mb = kb / 1024;
    return mb.toFixed(mb < 10 ? 1 : 0) + " MB";
  }

  function setBusyStatus(text) {
    status.className = "status busy";
    status.textContent = text;
  }

  function setTerminalStatus(text, cls) {
    status.className = "status" + (cls ? " " + cls : "");
    status.textContent = text;
  }

  async function refreshEstimate() {
    if (totalAudioSeconds <= 0) {
      detailsCard.hidden = true;
      return;
    }
    const data = await window.pywebview.api.estimate(totalAudioSeconds);
    const basis = data.is_measured
      ? "based on " + data.based_on_runs + " previous run" + (data.based_on_runs === 1 ? "" : "s")
      : "approximately";
    detailsEstimate.textContent = "~" + formatDuration(data.estimated_seconds) + " (" + basis + ")";
    detailsCard.hidden = false;
  }

  async function renderFileList() {
    updateTranscribeButtonState();

    if (selectedPaths.length === 0) {
      fileListCard.hidden = true;
      fileList.innerHTML = "";
      totalAudioSeconds = 0;
      detailsCard.hidden = true;
      return;
    }

    fileListCard.hidden = false;
    const infos = await window.pywebview.api.get_file_info(selectedPaths);
    totalAudioSeconds = infos.reduce((sum, info) => sum + info.duration, 0);
    fileListSummary.textContent = selectedPaths.length + " file" + (selectedPaths.length === 1 ? "" : "s") + " selected"
      + (totalAudioSeconds > 0 ? " · " + formatDuration(totalAudioSeconds) + " of audio" : "");

    if (selectedPaths.length <= FILE_LIST_DISPLAY_LIMIT) {
      fileList.hidden = false;
      fileList.innerHTML = "";
      selectedPaths.forEach((path, i) => {
        const info = infos[i];
        const li = document.createElement("li");
        const name = document.createElement("div");
        name.className = "file-name";
        name.textContent = parentAndName(path);
        const meta = document.createElement("div");
        meta.className = "file-meta";
        const metaParts = [];
        if (info.duration > 0) metaParts.push(formatDuration(info.duration));
        if (info.size > 0) metaParts.push(formatFileSize(info.size));
        meta.textContent = metaParts.join(" · ");
        li.appendChild(name);
        li.appendChild(meta);
        fileList.appendChild(li);
      });
    } else {
      fileList.hidden = true;
      fileList.innerHTML = "";
    }

    await refreshEstimate();
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
    downloading_model: "Downloading model - this can take a while depending on internet connection.",
    transcribing: "Transcribing",
    saving: "Saving",
  };

  async function pollStatus() {
    const data = await window.pywebview.api.get_status();

    progressBar.value = data.percent;

    if (data.cancelled) {
      clearInterval(pollTimer);
      pollTimer = null;
      cancelPending = false;
      setTerminalStatus("Cancelled.");
      cancelBtn.hidden = true;
      progressBar.hidden = true;
      updateTranscribeButtonState();
      return;
    }

    if (data.error) {
      clearInterval(pollTimer);
      pollTimer = null;
      cancelPending = false;
      setTerminalStatus("Error: " + data.error, "error");
      cancelBtn.hidden = true;
      updateTranscribeButtonState();
      return;
    }

    if (data.stage === "done") {
      clearInterval(pollTimer);
      pollTimer = null;
      cancelPending = false;
      setTerminalStatus("Done - Transcribed in " + formatDuration(data.elapsed_seconds) + ".", "done");
      cancelBtn.hidden = true;
      saveBtn.disabled = false;
      updateTranscribeButtonState();
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
    progressBar.hidden = false;
    progressBar.value = 0;
    setBusyStatus("Starting");

    const response = await window.pywebview.api.transcribe(selectedPaths);
    if (response.status !== "started") {
      setTerminalStatus("Error: " + (response.detail || "Transcription failed"), "error");
      updateTranscribeButtonState();
      progressBar.hidden = true;
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

  function renderModelOptions(options, selected) {
    modelGroup.innerHTML = "";
    modelRowsByValue = {};
    currentSelectedValue = selected;
    options.forEach((opt, i) => {
      const row = document.createElement("label");
      row.className = "row model-row";

      const radio = document.createElement("fluent-radio");
      radio.value = opt.value;
      // fluent-radio is form-associated, so wrapping it in a <label> is
      // enough to get native label-click forwarding for free - no manual
      // click handler needed (an earlier version added one anyway, which
      // double-fired alongside the native forwarding and caused the radio
      // to immediately re-toggle itself unchecked).

      const text = document.createElement("div");
      text.className = "row-text";
      const title = document.createElement("div");
      title.className = "row-title";
      title.textContent = opt.label;
      const desc = document.createElement("div");
      text.appendChild(title);
      text.appendChild(desc);

      modelRowsByValue[opt.value] = {
        descEl: desc,
        label: opt.label,
        description: opt.description,
        available: opt.available,
      };
      updateRowDescription(opt.value, opt.value === selected);

      const trailing = document.createElement("div");
      trailing.className = "row-trailing";
      if (!opt.available) {
        const downloadBtn = document.createElement("button");
        downloadBtn.type = "button";
        downloadBtn.className = "download-icon-btn";
        downloadBtn.title = "Download this model now";
        downloadBtn.textContent = ""; // Segoe Fluent Icons: CloudDownload
        downloadBtn.addEventListener("click", () => {
          startModelDownload(opt.value, trailing);
        });
        trailing.appendChild(downloadBtn);
      }

      row.appendChild(radio);
      row.appendChild(text);
      row.appendChild(trailing);
      modelGroup.appendChild(row);

      if (i < options.length - 1) {
        const divider = document.createElement("fluent-divider");
        divider.setAttribute("appearance", "subtle");
        modelGroup.appendChild(divider);
      }
    });
    // The checked state is owned by the group, not the individual radios -
    // setting it here finds the child fluent-radio with a matching `value`
    // and checks it (see fluent-radio-group's `value` setter). The group
    // only "sees" newly-slotted radios once its own internal slot-change
    // handling has run, which isn't reliably done after just one
    // microtask or animation frame - so this retries across a few frames
    // until the readback confirms it actually took, instead of guessing
    // at a fixed delay.
    applySelectedModel(selected);
    updateTranscribeButtonState();
  }

  function applySelectedModel(selected, attemptsLeft = 20) {
    modelGroup.value = selected;
    if (modelGroup.value === selected || attemptsLeft <= 0) return;
    requestAnimationFrame(() => applySelectedModel(selected, attemptsLeft - 1));
  }

  async function startModelDownload(modelSize, trailingEl) {
    if (modelDownloadInProgress) return;

    const response = await window.pywebview.api.download_model_size(modelSize);
    if (response.status !== "started") {
      setTerminalStatus("Error: " + (response.detail || "Could not start download"), "error");
      return;
    }

    modelDownloadInProgress = true;
    transcribeBtn.disabled = true;
    trailingEl.innerHTML = "";
    const bar = document.createElement("fluent-progress-bar");
    bar.className = "download-progress";
    bar.min = 0;
    bar.max = 100;
    bar.value = 0;
    trailingEl.appendChild(bar);

    const timer = setInterval(async () => {
      const data = await window.pywebview.api.get_download_status();
      if (!data.active) {
        clearInterval(timer);
        modelDownloadInProgress = false;
        trailingEl.innerHTML = "";
        if (data.error) {
          setTerminalStatus("Error downloading model: " + data.error, "error");
        } else {
          // Downloaded successfully - no icon, and the description goes
          // back to normal (or the "Download model (...)" prompt drops
          // away if this was the selected row).
          const row = modelRowsByValue[modelSize];
          if (row) {
            row.available = true;
            updateRowDescription(modelSize, modelSize === currentSelectedValue);
          }
        }
        updateTranscribeButtonState();
        return;
      }
      bar.value = data.percent;
    }, 500);
  }

  modelGroup.addEventListener("change", async () => {
    const value = modelGroup.value;
    if (!value) return;

    const previousValue = currentSelectedValue;
    currentSelectedValue = value;
    if (previousValue) updateRowDescription(previousValue, false);
    updateRowDescription(value, true);
    updateTranscribeButtonState();

    // The radio the user clicked is already visually checked at this point
    // (that's what just fired this event) - no need to re-fetch and
    // rebuild the whole list, which would fight the group's own
    // checked-state handling for no reason. Just persist the choice and
    // refresh anything that depends on it.
    const response = await window.pywebview.api.set_model_size(value);
    if (response.status !== "ok") {
      setTerminalStatus("Error: " + (response.detail || "Could not change model"), "error");
      return;
    }
    // The estimate's rate depends on which model is selected - refresh it
    // if there's already a selection so the number on screen stays honest.
    if (selectedPaths.length > 0) {
      refreshEstimate();
    }
  });

  async function initModelPicker() {
    const data = await window.pywebview.api.get_model_options();
    renderModelOptions(data.options, data.selected);
  }

  if (window.pywebview) {
    initModelPicker();
  } else {
    window.addEventListener("pywebviewready", initModelPicker);
  }
</script>
</body>
</html>
"""

PAGE = _PAGE_TEMPLATE.replace("__FLUENT_BUNDLE_JS__", _FLUENT_BUNDLE_JS)


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

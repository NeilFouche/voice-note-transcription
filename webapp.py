"""
Local web UI for the voice-note transcription pipeline.

Run this (instead of main.py's folder-drop CLI flow) for the drag-and-drop
browser experience: drop audio files in the page, click Transcribe, watch
progress, download the combined transcript when it's done. Everything
still runs locally - this only serves a UI on 127.0.0.1, nothing leaves
the machine.

Launching main() starts the server and opens the user's default browser to
it automatically, so double-clicking the packaged exe is the whole
"install/run" experience.

The pipeline runs in a background thread per request (see
transcribe_endpoint / _run_pipeline) so the page can poll /status for live
progress instead of the browser just hanging until everything finishes.
"""

import shutil
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import audio
import performance
import progress
import replacements
import transcripts
from config import settings
from logging_config import error_logger, general_logger

HOST = "127.0.0.1"
PORT = 8000

app = FastAPI(title="Voice Note Transcription")

# This is a single-user local tool, not a multi-tenant server - the lock
# just stops two browser tabs (or a double click) from running the
# pipeline concurrently and stepping on each other's files. Held for the
# whole background run, released in _run_pipeline's finally block.
_pipeline_lock = threading.Lock()

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Note Transcription</title>
<style>
  :root { color-scheme: light dark; }
  /* Author-stylesheet rules always beat the browser's built-in
     [hidden]{display:none}, regardless of specificity (origin outranks
     specificity in the cascade) - e.g. the `button{display:inline-block}`
     rule below would otherwise keep a hidden <button> visible. This makes
     [hidden] win unconditionally so el.hidden = true/false always works. */
  [hidden] { display: none !important; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    max-width: 640px;
    margin: 48px auto;
    padding: 0 20px;
    color: #1a1a1a;
    background: #fafafa;
  }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  p.sub { color: #555; margin-top: 0; }
  #dropzone {
    border: 2px dashed #999;
    border-radius: 10px;
    padding: 32px 16px;
    text-align: center;
    color: #555;
    cursor: pointer;
    background: #fff;
    transition: border-color .15s, background .15s;
  }
  #dropzone.dragover { border-color: #2563eb; background: #eff6ff; }
  #dropzone.has-files { border-style: solid; border-color: #15803d; background: #f0fdf4; color: #166534; }
  #dropzoneIcon { font-size: 1.8rem; line-height: 1; margin-bottom: 8px; }
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
  @keyframes statusDots {
    to { width: 1em; }
  }
  @media (prefers-reduced-motion: reduce) {
    #status.busy::after { content: "…"; width: auto; animation: none; }
  }
</style>
</head>
<body>
  <h1>Voice Note Transcription</h1>
  <p class="sub">Select the files to transcribe, then click <b>Transcribe</b>.</p>

  <div id="dropzone">
    <div id="dropzoneIcon">📂</div>
    <div id="dropzoneText">Drag audio files or a whole folder here, or click to choose files</div>
  </div>
  <input type="file" id="fileInput" multiple hidden accept="audio/*">
  <ul id="fileList"></ul>
  <button type="button" id="clearBtn" class="link-btn" hidden>Clear selection</button>
  <div id="estimate" class="meta"></div>

  <div class="actions">
    <button type="button" id="cancelBtn" class="cancel" hidden>Cancel</button>
    <button id="transcribeBtn" disabled>Transcribe</button>
    <a id="downloadBtn" class="download disabled" href="#" aria-disabled="true">Save transcript</a>
  </div>

  <div id="progressWrap" hidden><div id="progressBar"></div></div>
  <div id="status"></div>

<script>
  const dropzone = document.getElementById("dropzone");
  const dropzoneIcon = document.getElementById("dropzoneIcon");
  const dropzoneText = document.getElementById("dropzoneText");
  const fileInput = document.getElementById("fileInput");
  const fileList = document.getElementById("fileList");
  const fileSummary = document.getElementById("fileSummary");
  const estimateEl = document.getElementById("estimate");
  const clearBtn = document.getElementById("clearBtn");
  const transcribeBtn = document.getElementById("transcribeBtn");
  const cancelBtn = document.getElementById("cancelBtn");
  const downloadBtn = document.getElementById("downloadBtn");
  const progressWrap = document.getElementById("progressWrap");
  const progressBar = document.getElementById("progressBar");
  const status = document.getElementById("status");

  const AUDIO_EXTENSIONS = [".ogg", ".mp3", ".wav", ".mp4", ".flac", ".opus"];
  const FILE_LIST_DISPLAY_LIMIT = 10;
  const DURATION_READ_CONCURRENCY = 20;

  let selectedFiles = [];
  let skippedNonAudioCount = 0;
  let totalAudioSeconds = 0;
  let pollTimer = null;
  let cancelPending = false;

  function isAudioFile(file) {
    const name = file.name.toLowerCase();
    return AUDIO_EXTENSIONS.some((ext) => name.endsWith(ext));
  }

  // Runs fn over items with at most `limit` in flight at once - reading
  // duration metadata for hundreds of files at once in one big Promise.all
  // is unnecessary browser load; this keeps it smooth for large folders.
  async function mapWithConcurrency(items, limit, fn) {
    const results = new Array(items.length);
    let next = 0;

    async function worker() {
      while (next < items.length) {
        const current = next++;
        results[current] = await fn(items[current]);
      }
    }

    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
    return results;
  }

  // Drag-and-drop of a folder needs the DataTransferItem entry API, not
  // the plain file list (which flattens/ignores directories). Each
  // directory's reader.readEntries() only returns up to 100 entries per
  // call by spec, so it has to be called repeatedly until it comes back
  // empty - relevant here since folders of voice notes can run into the
  // hundreds.
  async function getFilesFromDataTransfer(dataTransfer) {
    const items = dataTransfer.items;
    if (!items) {
      return Array.from(dataTransfer.files || []);
    }

    const entries = Array.from(items)
      .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
      .filter(Boolean);

    if (entries.length === 0) {
      return Array.from(dataTransfer.files || []);
    }

    const files = [];

    async function walk(entry) {
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        files.push(file);
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        const readBatch = () => new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        let batch;
        do {
          batch = await readBatch();
          for (const child of batch) {
            await walk(child);
          }
        } while (batch.length > 0);
      }
    }

    await Promise.all(entries.map(walk));
    return files;
  }

  function formatDuration(totalSeconds) {
    if (!isFinite(totalSeconds) || totalSeconds <= 0) return "0s";
    const m = Math.floor(totalSeconds / 60);
    const s = Math.round(totalSeconds % 60);
    return m === 0 ? s + "s" : m + "m " + s + "s";
  }

  function readDuration(file) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const el = document.createElement("audio");
      el.preload = "metadata";
      el.onloadedmetadata = () => {
        resolve(isFinite(el.duration) ? el.duration : 0);
        URL.revokeObjectURL(url);
      };
      el.onerror = () => {
        resolve(0);
        URL.revokeObjectURL(url);
      };
      el.src = url;
    });
  }

  // Gives feedback right where the user just interacted, not just in the
  // summary line below. Called twice per selection change: immediately
  // with just the count (duration isn't known yet), then again once
  // updateSummary()'s async duration read finishes.
  function updateDropzoneDisplay() {
    if (selectedFiles.length === 0) {
      dropzone.classList.remove("has-files");
      dropzoneIcon.textContent = "📂";
      dropzoneText.textContent = "Drag audio files or a whole folder here, or click to choose files";
      return;
    }

    dropzone.classList.add("has-files");
    dropzoneIcon.textContent = "✓";
    let text = selectedFiles.length + " file" + (selectedFiles.length === 1 ? "" : "s") + " selected";
    if (totalAudioSeconds > 0) {
      text += " · " + formatDuration(totalAudioSeconds) + " of audio";
    }
    text += " — drag more or click to add";
    dropzoneText.textContent = text;
  }

  async function updateSummary() {
    if (selectedFiles.length === 0) {
      fileSummary.textContent = "";
      estimateEl.textContent = "";
      totalAudioSeconds = 0;
      skippedNonAudioCount = 0;
      return;
    }

    fileSummary.textContent = selectedFiles.length + " file" + (selectedFiles.length === 1 ? "" : "s") + " selected";
    if (skippedNonAudioCount > 0) {
      fileSummary.textContent += " (" + skippedNonAudioCount + " non-audio file" +
        (skippedNonAudioCount === 1 ? "" : "s") + " skipped)";
    }

    const durations = await mapWithConcurrency(selectedFiles, DURATION_READ_CONCURRENCY, readDuration);
    totalAudioSeconds = durations.reduce((a, b) => a + b, 0);
    fileSummary.textContent += " · Total audio duration: " + formatDuration(totalAudioSeconds);
    updateDropzoneDisplay(); // refresh now that the duration is known

    if (totalAudioSeconds > 0) {
      try {
        const res = await fetch("/estimate?seconds=" + totalAudioSeconds);
        const data = await res.json();
        const basis = data.is_measured
          ? "based on " + data.based_on_runs + " previous run" + (data.based_on_runs === 1 ? "" : "s") + " on this computer"
          : "rough estimate - not yet calibrated to this computer";
        estimateEl.textContent = "Estimated transcription time: ~" + formatDuration(data.estimated_seconds) + " (" + basis + ")";
      } catch (err) {
        estimateEl.textContent = "";
      }
    } else {
      estimateEl.textContent = "";
    }
  }

  function renderFileList() {
    // With hundreds of files (a whole folder), listing every name is both
    // impractical to scan and slow to render - past a small count, just
    // show the summary line instead.
    if (selectedFiles.length > 0 && selectedFiles.length <= FILE_LIST_DISPLAY_LIMIT) {
      fileList.hidden = false;
      fileList.innerHTML = "";
      for (const file of selectedFiles) {
        const li = document.createElement("li");
        li.textContent = file.name;
        fileList.appendChild(li);
      }
    } else {
      fileList.hidden = true;
      fileList.innerHTML = "";
    }
    transcribeBtn.disabled = selectedFiles.length === 0;
    clearBtn.hidden = selectedFiles.length === 0;
    updateDropzoneDisplay();
    updateSummary();
  }

  function addFiles(fileListLike) {
    const incoming = Array.from(fileListLike);
    const audioOnly = incoming.filter(isAudioFile);
    skippedNonAudioCount += incoming.length - audioOnly.length;
    selectedFiles = selectedFiles.concat(audioOnly);
    renderFileList();
  }

  function clearFiles() {
    selectedFiles = [];
    skippedNonAudioCount = 0;
    fileInput.value = ""; // so re-picking the same file(s) still fires "change"
    renderFileList();
  }

  function setDownloadEnabled(enabled) {
    downloadBtn.classList.toggle("disabled", !enabled);
    downloadBtn.setAttribute("aria-disabled", String(!enabled));
    downloadBtn.href = enabled ? "/download" : "#";
  }

  dropzone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", (e) => addFiles(e.target.files));

  clearBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    clearFiles();
  });

  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", async (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    const files = await getFilesFromDataTransfer(e.dataTransfer);
    addFiles(files);
  });

  const STAGE_LABELS = {
    starting: "Starting",
    transcribing: "Transcribing",
    replacements: "Applying corrections",
    serializing: "Formatting",
    combining: "Combining files",
  };

  // The "busy"/"terminal" split drives the animated-dots CSS: .busy gets an
  // animated "..." appended via #status.busy::after, so anything still in
  // progress reads as actively working rather than possibly stuck. Static
  // strings deliberately don't include their own "..." - the animation
  // supplies it, so it doesn't end up doubled.
  function setBusyStatus(text) {
    status.className = "busy";
    status.textContent = text;
  }

  function setTerminalStatus(text, cls) {
    status.className = cls || "";
    status.textContent = text;
  }

  async function pollStatus() {
    const res = await fetch("/status");
    const data = await res.json();

    progressBar.style.width = data.percent + "%";

    if (data.cancelled) {
      clearInterval(pollTimer);
      cancelPending = false;
      setTerminalStatus("Cancelled.");
      cancelBtn.hidden = true;
      progressWrap.hidden = true; // the bar would otherwise freeze part-way through its band (e.g. ~90%), reading as stuck rather than stopped
      transcribeBtn.disabled = selectedFiles.length === 0;
      return;
    }

    // Errors leave `stage` as whatever it was when the failure happened
    // (so the bar freezes there instead of jumping around), so check
    // data.error first rather than looking for a dedicated "error" stage.
    if (data.error) {
      clearInterval(pollTimer);
      cancelPending = false;
      setTerminalStatus("Error: " + data.error, "error");
      cancelBtn.hidden = true;
      transcribeBtn.disabled = selectedFiles.length === 0;
      return;
    }

    if (data.stage === "done") {
      clearInterval(pollTimer);
      cancelPending = false;
      setTerminalStatus("Done - Transcribed in " + formatDuration(data.elapsed_seconds) + ".", "done");
      setDownloadEnabled(true);
      cancelBtn.hidden = true;
      transcribeBtn.disabled = selectedFiles.length === 0;
      return;
    }

    // Once cancellation has been requested, keep saying so rather than
    // letting the next poll tick overwrite it with normal per-file
    // progress text - the run is still genuinely in the "transcribing"
    // stage server-side until the in-flight file finishes, which looked
    // like the cancel had silently reverted.
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
    setDownloadEnabled(false);
    cancelPending = false;
    progressWrap.hidden = false;
    progressBar.style.width = "0%";
    setBusyStatus("Uploading");

    const formData = new FormData();
    for (const file of selectedFiles) {
      formData.append("files", file, file.name);
    }

    try {
      const response = await fetch("/transcribe", { method: "POST", body: formData });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || "Transcription failed");
      }
      // Only reveal Cancel once the server has confirmed the run actually
      // started - not while we're still uploading or if the request failed.
      cancelBtn.hidden = false;
      cancelBtn.disabled = false;
      pollTimer = setInterval(pollStatus, 1000);
      pollStatus();
    } catch (err) {
      setTerminalStatus("Error: " + err.message, "error");
      transcribeBtn.disabled = selectedFiles.length === 0;
      progressWrap.hidden = true;
    }
  });

  cancelBtn.addEventListener("click", async () => {
    // Cooperative cancellation: the file currently being transcribed still
    // finishes (a single in-flight Whisper call can't be interrupted
    // mid-computation), so this takes effect at the next safe checkpoint
    // rather than instantly. cancelPending keeps pollStatus() showing this
    // message instead of letting the next tick overwrite it with normal
    // per-file progress text while the in-flight file finishes.
    cancelPending = true;
    cancelBtn.disabled = true;
    setBusyStatus("Cancelling the transcription");
    try {
      await fetch("/cancel", { method: "POST" });
    } catch (err) {
      // Ignore - polling will keep reflecting whatever the server ends up doing.
    }
  });
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/estimate")
def estimate(seconds: float = 0.0):
    return performance.estimate_seconds(seconds)


@app.get("/status")
def status_endpoint():
    return progress.snapshot()


@app.post("/cancel")
def cancel_endpoint():
    # Cooperative: takes effect once the file currently being transcribed
    # finishes (see audio.py's transcribe() loop) rather than instantly -
    # there's no safe way to interrupt a single in-flight Whisper inference
    # call. Harmless to call when nothing is running.
    progress.request_cancel()
    return {"status": "cancelling"}


def _unique_destination(filename: str) -> Path:
    """
    Avoid clobbering (and thus silently skipping, via audio.py's
    already-transcribed index) a file already sitting in input_dir with the
    same name. Uses the same "(1)", "(2)" ... suffix convention audio.py's
    filename pattern already recognises for WhatsApp exports.
    """
    dest = settings.input_dir / filename
    if not dest.exists():
        return dest

    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 1
    while dest.exists():
        dest = settings.input_dir / f"{stem} ({counter}){suffix}"
        counter += 1
    return dest


def _run_pipeline(uploaded_filenames: set[str]):
    """Runs the full pipeline in the background so the request can return
    immediately and the page can poll /status for progress."""
    try:
        # Scoped to exactly what was just uploaded - selecting one file
        # shouldn't also sweep up unrelated leftovers sitting in Audio
        # Files from other sessions. `touched` is the exact set of
        # transcript filenames this produced or confirmed - the contract
        # is X files selected in, X lines out, with nothing else leaking
        # in from elsewhere on this machine.
        completed, touched = audio.transcribe(only_filenames=uploaded_filenames)  # reports its own per-file progress
        if not completed:
            # Cancelled: whatever fully finished before the cancel is
            # already safely in Complete, ready to be picked up by a future
            # run. Deliberately skip the rest of the pipeline rather than
            # assembling a partial "final" transcript the user didn't ask
            # for - that's what "abort" should mean here.
            progress.finish(cancelled=True)
            return

        progress.set_stage("replacements")
        replacements.apply(only_filenames=touched)

        progress.set_stage("serializing")
        transcripts.serialize(only_filenames=touched)

        # Same exact set again - the downloaded result reflects only what
        # was just selected, nothing else ever transcribed on this
        # machine. Historical files in Complete and serialized are left
        # alone, not deleted; they just aren't included in this download.
        progress.set_stage("combining")
        transcripts.combine(only_filenames=touched)

        output_path = settings.final_output_dir / settings.target_filename
        if not output_path.exists():
            raise RuntimeError("No transcript was produced")

        progress.finish()
    except Exception as e:
        error_logger.exception("Web pipeline run failed")
        progress.finish(error=str(e))
    finally:
        _pipeline_lock.release()


@app.post("/transcribe")
def transcribe_endpoint(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    if not _pipeline_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A transcription run is already in progress")

    uploaded_filenames = set()
    try:
        settings.ensure_dirs()
        for upload in files:
            dest = _unique_destination(upload.filename)
            with open(dest, "wb") as f:
                shutil.copyfileobj(upload.file, f)
            uploaded_filenames.add(dest.name)
    except Exception:
        _pipeline_lock.release()
        error_logger.exception("Failed to save uploaded files")
        raise HTTPException(status_code=500, detail="Failed to save uploaded files")

    progress.reset()
    threading.Thread(target=_run_pipeline, args=(uploaded_filenames,), daemon=True).start()

    return {"status": "started"}


@app.get("/download")
def download():
    output_path = settings.final_output_dir / settings.target_filename
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="No transcript available yet")

    # `filename=` sets Content-Disposition, so this is what the browser
    # suggests in the save dialog - independent of wherever output_path
    # actually lives on disk.
    return FileResponse(
        path=output_path,
        filename=settings.target_filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def main():
    settings.ensure_dirs()
    url = f"http://{HOST}:{PORT}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    general_logger.info(f"Starting local server at {url}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

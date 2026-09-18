# PyInstaller spec for the packaged desktop build.
#
# Produces a folder build (not --onefile): a folder containing the exe
# plus an _internal directory with the Python runtime, all dependencies,
# and the bundled resources (Afrikaans dictionary, "medium" Whisper
# model). Chosen over --onefile so startup doesn't re-extract this large
# a dependency tree (ctranslate2, onnxruntime, numpy, the model itself)
# on every launch.
#
# Build with:
#   .tx-venv\Scripts\pyinstaller voice_note_transcription.spec --noconfirm

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
PROJECT_DIR = Path(SPECPATH)

datas = [
    (str(PROJECT_DIR / "dictionaries"), "dictionaries"),
    (str(PROJECT_DIR / "models" / "medium"), "models/medium"),
]
binaries = []
hiddenimports = []

# These have native binaries / dynamic imports that PyInstaller's static
# analysis can't fully see through on its own.
for pkg in ("ctranslate2", "tokenizers", "huggingface_hub", "faster_whisper", "av", "onnxruntime"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["webapp.py"],
    pathex=[str(PROJECT_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Voice Note Transcription",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Voice Note Transcription",
)

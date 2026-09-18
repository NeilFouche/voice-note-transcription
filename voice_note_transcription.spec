# PyInstaller spec for the packaged desktop build.
#
# Produces a folder build (not --onefile): a folder containing the exe
# plus an _internal directory with the Python runtime, all dependencies,
# and the bundled resources (Afrikaans dictionary, "large-v3" Whisper
# model - the only one of the three that tested as acceptable quality,
# so it's the one shipped rather than a smaller/faster default).
# Chosen over --onefile so startup doesn't re-extract this large a
# dependency tree (ctranslate2, onnxruntime, numpy, the model itself)
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
    (str(PROJECT_DIR / "models" / "large-v3"), "models/large-v3"),
    # Bundled as a plain readable file too, not just embedded via EXE()'s
    # icon= below - app.py reads this one back at runtime to set the
    # window's titlebar/taskbar icon (pywebview's WebView2 backend doesn't
    # do this itself), which needs an actual file path, not just the
    # icon baked into the exe's own resource section.
    (str(PROJECT_DIR / "icon.ico"), "."),
    (str(PROJECT_DIR / "vendor" / "fluent-web-components.min.js"), "vendor"),
]
binaries = []
hiddenimports = []

# These have native binaries / dynamic imports that PyInstaller's static
# analysis can't fully see through on its own. pywebview and clr_loader
# (its .NET interop layer on Windows) already have community hooks in
# pyinstaller-hooks-contrib that PyInstaller picks up automatically, so
# they don't need collect_all here.
for pkg in ("ctranslate2", "tokenizers", "huggingface_hub", "faster_whisper", "av", "onnxruntime"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    ["app.py"],
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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_DIR / "icon.ico"),
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

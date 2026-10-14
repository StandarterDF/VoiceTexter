# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спека для VoiceTexter.

Обычная сборка (папка ``dist/VoiceTexter`` — быстрый старт):
    pyinstaller --noconfirm --clean VoiceTexter.spec

Один файл (``dist/VoiceTexter.exe`` — дольше стартует, всё распаковывается
во временную папку):
    set VOICETEXTER_ONEFILE=1
    pyinstaller --noconfirm --clean VoiceTexter.spec

Готовить сборку удобнее скриптом ``build_exe.bat``: он сам поставит
PyInstaller, сгенерирует значок и положит рядом с exe ``models/`` и
``config.json``.

Отладочная сборка с консолью (видно traceback'и):
    set VOICETEXTER_CONSOLE=1
"""

import os

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    copy_metadata,
)

ONEFILE = os.environ.get("VOICETEXTER_ONEFILE", "0") == "1"
# Отладочная сборка с консолью: VOICETEXTER_CONSOLE=1 — видно traceback'и.
CONSOLE = os.environ.get("VOICETEXTER_CONSOLE", "0") == "1"


def _path(*parts: str) -> str:
    return os.path.normpath(os.path.join(SPECPATH, *parts))


datas = [
    (_path("assets", "icon.ico"), "assets"),
    (_path("assets", "icon.png"), "assets"),
    (_path("config.json.example"), "."),
    # Версия приложения для проверки обновлений (updater.app_version).
    (_path("packaging", "version_info.txt"), "packaging"),
]

binaries = []

hiddenimports = [
    # pynput/pystray подтягивают бэкенды динамически — их нужно указать явно.
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "pystray._win32",
]

# CustomTkinter: темы и ассеты лежат в data-файлах пакета.
datas += collect_data_files("customtkinter")

# faster-whisper: вместе с пакетом идёт ONNX-модель Silero VAD (assets/).
datas += collect_data_files("faster_whisper")

# PortAudio (sounddevice) и libsndfile (soundfile) — нативные библиотеки.
datas += collect_data_files("_sounddevice_data")
binaries += collect_dynamic_libs("_sounddevice_data")
datas += collect_data_files("_soundfile_data")
binaries += collect_dynamic_libs("_soundfile_data")

# Метаданные нужны пакетам, читающим свою версию через importlib.metadata.
for _pkg in ("faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub", "av"):
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass


# --- Патч среды выполнения Visual C++ -------------------------------------
# Python-инсталлятор кладёт рядом свою (часто устаревшую) копию vcruntime/
# msvcp140. onnxruntime, собранный новым MSVC, из-за неё падает с
# 0xC0000005 (access violation) прямо на import. Поэтому вместо приложенной
# в Python версии берём системную (Windows\System32) — её же используют
# обычные нативные программы.
_VC_RUNTIME = (
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)


def _use_system_vc_runtime(entries):
    names = {n.lower() for n in _VC_RUNTIME}
    kept = [e for e in entries if os.path.basename(e[0]).lower() not in names]
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    for name in _VC_RUNTIME:
        source = os.path.join(sys32, name)
        if os.path.isfile(source):
            kept.append((name, source, "BINARY"))
    return kept

a = Analysis(
    ["main.py"],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest", "pydoc_data", "setuptools"],
    noarchive=False,
)

a.binaries = _use_system_vc_runtime(a.binaries)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="VoiceTexter",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=CONSOLE,
        disable_windowed_traceback=False,
        icon=_path("assets", "icon.ico"),
        version=_path("packaging", "version_info.txt"),
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="VoiceTexter",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=CONSOLE,
        disable_windowed_traceback=False,
        icon=_path("assets", "icon.ico"),
        version=_path("packaging", "version_info.txt"),
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="VoiceTexter",
    )

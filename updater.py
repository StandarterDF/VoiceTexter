"""Проверка и установка обновлений программы с GitHub Releases.

Работает на стандартной библиотеке (как ``modelstore``): обращается к GitHub
API за последним релизом, сверяет версию, при необходимости скачивает
релизный ``*-win64.zip``, распаковывает и через временный ``.bat``
подменяет файлы после выхода приложения.

Автоустановка возможна только для собранной версии (onedir EXE). При запуске
из исходников доступна лишь проверка — подтягивать нечего.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

import modelstore

API_LATEST = "https://api.github.com/repos/StandarterDF/VoiceTexter/releases/latest"
USER_AGENT = "VoiceTexter-updater/1.0"
CHUNK = 256 * 1024
EXE_NAME = "VoiceTexter.exe"


# ------------------------------------------------------------------ версия


def app_version() -> str:
    """Версия приложения из ``packaging/version_info.txt`` (иначе 0.0.0)."""
    import config

    try:
        text = config.resource_path("packaging/version_info.txt").read_text(
            encoding="utf-8"
        )
        match = re.search(r"filevers=\((\d+),\s*(\d+),\s*(\d+)", text)
        if match:
            return ".".join(match.groups())
    except Exception:  # noqa: BLE001
        pass
    return "0.0.0"


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3])


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


# ------------------------------------------------------------------ проверка


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def fetch_latest(*, proxy_url: Optional[str] = None, timeout: float = 30.0) -> dict:
    """Последний релиз: version, tag, asset_url/name/size, digest, url, notes."""
    with modelstore.open_opener(proxy_url) as opener:
        with opener.open(_request(API_LATEST), timeout=timeout) as resp:
            data = json.load(resp)

    tag = str(data.get("tag_name") or "").strip()
    version = tag.lstrip("vV")
    asset = None
    for item in data.get("assets") or []:
        name = str(item.get("name") or "")
        if name.lower().endswith(".zip"):
            asset = item
            break
    asset = asset or {}
    return {
        "version": version,
        "tag": tag,
        "name": str(data.get("name") or tag),
        "url": data.get("html_url"),
        "notes": data.get("body") or "",
        "asset_url": asset.get("browser_download_url"),
        "asset_name": asset.get("name"),
        "asset_size": asset.get("size"),
        "digest": asset.get("digest"),
    }


# ------------------------------------------------------------ скачивание


def download_asset(
    proxy_url: Optional[str],
    url: str,
    dest: Path,
    *,
    digest: Optional[str] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    timeout: float = 60.0,
) -> Path:
    """Скачать ассет в ``dest`` (через ``.part``), проверить sha256 если есть."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    hasher = hashlib.sha256()
    with modelstore.open_opener(proxy_url) as opener:
        with opener.open(_request(url), timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            done = 0
            with open(part, "wb") as fh:
                while True:
                    if should_stop is not None and should_stop():
                        raise modelstore.DownloadCancelled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    hasher.update(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
    os.replace(part, dest)

    if digest and digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1].strip().lower()
        if hasher.hexdigest().lower() != expected:
            try:
                dest.unlink()
            except OSError:
                pass
            raise RuntimeError("Контрольная сумма обновления не совпала")
    return dest


# ------------------------------------------------------------ установка


def can_self_update() -> bool:
    """True только для собранной onedir-версии (рядом есть ``_internal``)."""
    if not getattr(sys, "frozen", False):
        return False
    meipass = Path(getattr(sys, "_MEIPASS", ""))
    return meipass.name == "_internal"


def extract_release(zip_path: Path, work_dir: Path) -> Path:
    """Распаковать релизный zip. Вернуть папку с содержимым приложения."""
    stage = Path(work_dir) / "staging"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(stage)
    inner = stage / "VoiceTexter"
    return inner if inner.is_dir() else stage


def write_updater(work_dir: Path, src_dir: Path, app_dir: Path, pid: int) -> Path:
    """Создать ``apply_update.bat``: ждём выход PID, зеркалим файлы, стартуем."""
    work_dir = Path(work_dir)
    src_dir = Path(src_dir)
    app_dir = Path(app_dir)
    bat = work_dir / "apply_update.bat"
    log = app_dir / "update_apply.log"
    content = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'set "SRC={src_dir}"\r\n'
        f'set "DST={app_dir}"\r\n'
        f'set "LOG={log}"\r\n'
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "    ping -n 2 127.0.0.1 >nul\r\n"
        "    goto wait\r\n"
        ")\r\n"
        'robocopy "%SRC%\\_internal" "%DST%\\_internal" /MIR /NFL /NDL /NJH /NJS '
        '/NP >>"%LOG%" 2>&1\r\n'
        f'copy /y "%SRC%\\{EXE_NAME}" "%DST%\\{EXE_NAME}" >>"%LOG%" 2>&1\r\n'
        f'start "" "%DST%\\{EXE_NAME}"\r\n'
        'rmdir /s /q "%~dp0staging"\r\n'
        'del /q "%~dp0*.zip" "%~dp0*.part"\r\n'
        "exit /b\r\n"
    )
    bat.write_text(content, encoding="utf-8")
    return bat


def launch_updater(bat_path: Path) -> None:
    """Запустить апдейтер отдельным процессом, не привязывая к приложению."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        ["cmd", "/c", str(bat_path)],
        cwd=str(Path(bat_path).parent),
        creationflags=flags,
        close_fds=True,
    )

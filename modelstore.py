"""Загрузка моделей faster-whisper с Hugging Face.

Специально на стандартной библиотеке (urllib) без huggingface_hub: у него
бывают проблемы с бэкендом Xet и сторонними httpx-стеками, а для простой
задачи «скачать десяток файлов» он не нужен. Заодно работает в собранном
EXE без лишних зависимостей.

Поддерживаются прокси: ``http(s)://`` (штатный urllib) и ``socks5://`` /
``socks5h://`` / ``socks4://`` (через PySocks, если установлен).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import unquote, urlparse

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
USER_AGENT = "VoiceTexter/1.0 (+https://github.com/StandarterDF/VoiceTexter)"
CHUNK = 256 * 1024

# Что скачиваем из репозитория (как в faster_whisper.utils.download_model).
ALLOWED_FILES = {
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
    "vocabulary.json",
}

# Локальный манифест: какие файлы и с какими blobId скачаны (для проверки обновлений).
MANIFEST_NAME = ".vt_manifest.json"


class DownloadCancelled(Exception):
    """Загрузка прервана пользователем."""


# size -> (папка в models/, репозиторий на Hugging Face, примерный размер)
KNOWN_MODELS: dict[str, tuple[str, str, str]] = {
    "tiny": ("whisper-tiny", "Systran/faster-whisper-tiny", "~75 МБ"),
    "base": ("whisper-base", "Systran/faster-whisper-base", "~145 МБ"),
    "small": ("whisper-small", "Systran/faster-whisper-small", "~480 МБ"),
    "medium": ("whisper-medium", "Systran/faster-whisper-medium", "~1.5 ГБ"),
    "large-v3": ("whisper-large-v3", "Systran/faster-whisper-large-v3", "~3 ГБ"),
    "large-v3-turbo": (
        "whisper-large-v3-turbo",
        "deepdml/faster-whisper-large-v3-turbo-ct2",
        "~1.6 ГБ",
    ),
    "distil-large-v3": (
        "whisper-distil-large-v3",
        "Systran/faster-whisper-distil-large-v3",
        "~1.5 ГБ",
    ),
}


def known_sizes() -> list[str]:
    return list(KNOWN_MODELS)


def folder_name(size: str) -> str:
    return KNOWN_MODELS[size][0]


def repo_for(size: str) -> str:
    return KNOWN_MODELS[size][1]


def approx_size(size: str) -> str:
    return KNOWN_MODELS[size][2]


def is_installed(size: str, models_dir) -> bool:
    return (Path(models_dir) / folder_name(size) / "model.bin").exists()


# ------------------------------------------------------------------ прокси


_socks_lock = threading.Lock()
_SOCKS_SCHEMES = ("socks", "socks5", "socks5h", "socks4", "socks4a")


@contextmanager
def _socks_proxy(proxy_url: str) -> Iterator[None]:
    """На время загрузки подменить socket на SOCKS-вариант из PySocks."""
    try:
        import socks  # PySocks
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "Для SOCKS-прокси нужен пакет PySocks (pip install PySocks)"
        ) from exc

    parsed = urlparse(proxy_url)
    scheme = (parsed.scheme or "socks5").lower()
    if scheme not in _SOCKS_SCHEMES:
        raise RuntimeError(f"Неизвестная схема прокси: {scheme}")
    if not parsed.hostname:
        raise RuntimeError("В адресе прокси не указан хост")

    proxy_type = socks.SOCKS4 if scheme.startswith("socks4") else socks.SOCKS5

    with _socks_lock:
        original = socket.socket
        socks.set_default_proxy(
            proxy_type,
            parsed.hostname,
            parsed.port or 1080,
            rdns=scheme.endswith("h"),
            username=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
        )
        socket.socket = socks.socksocket  # type: ignore[misc,assignment]
        try:
            yield
        finally:
            socket.socket = original  # type: ignore[misc]


@contextmanager
def open_opener(
    proxy_url: Optional[str] = None,
) -> Iterator[urllib.request.OpenerDirector]:
    """Opener urllib с учётом прокси (http/https/socks5)."""
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        yield urllib.request.build_opener()
        return

    scheme = (urlparse(proxy_url).scheme or "http").lower()
    if scheme in _SOCKS_SCHEMES:
        with _socks_proxy(proxy_url):
            yield urllib.request.build_opener()
        return

    proxy = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    yield urllib.request.build_opener(proxy)


# ------------------------------------------------------------------ запросы


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def _allowed(name: str) -> bool:
    return name in ALLOWED_FILES or name.startswith("vocabulary.")


def remote_info(opener, repo: str, timeout: float) -> dict:
    """Метаданные файлов модели: {'sha':…, 'files': {имя: {blobId, size}}}.

    Пустой ``files`` означает, что API недоступен.
    """
    url = f"{HF_ENDPOINT}/api/models/{repo}?blobs=true"
    try:
        with opener.open(_request(url), timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:  # noqa: BLE001 - нет сети/API — вернём пусто, вызывающий решит
        return {"sha": None, "files": {}}
    files = {}
    for sib in data.get("siblings", []):
        name = sib.get("rfilename")
        if name and _allowed(name):
            files[name] = {"blobId": sib.get("blobId"), "size": sib.get("size")}
    return {"sha": data.get("sha"), "files": files}


def list_repo_files(
    opener, repo: str, timeout: float
) -> list[tuple[str, Optional[int]]]:
    """Файлы модели в репозитории как [(имя, размер|None)]."""
    info = remote_info(opener, repo, timeout)
    files = [(n, m.get("size")) for n, m in info["files"].items()]
    if not files:
        files = [(name, None) for name in sorted(ALLOWED_FILES)]
    return files


def read_manifest(models_dir, size: str) -> Optional[dict]:
    """Локальный манифест модели: какие файлы и с какими blobId скачаны."""
    path = Path(models_dir) / folder_name(size) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def write_manifest(models_dir, size: str, info: dict) -> None:
    """Сохранить манифест (ревизию + blobId файлов) для проверки обновлений."""
    path = Path(models_dir) / folder_name(size) / MANIFEST_NAME
    try:
        path.write_text(
            json.dumps(
                {
                    "repo": repo_for(size),
                    "sha": info.get("sha"),
                    "files": info.get("files", {}),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _changed_files(target: Path, local: Optional[dict], remote: dict) -> dict:
    """Файлы, которые нужно скачать/перекачать (новые и изменённые)."""
    local_files = (local or {}).get("files", {}) if local else {}
    changed = {}
    for name, meta in remote.items():
        path = target / name
        if not path.exists():
            changed[name] = meta
        elif name in local_files:
            if local_files[name].get("blobId") != meta.get("blobId"):
                changed[name] = meta
        elif meta.get("size") and path.stat().st_size != meta.get("size"):
            # Манифеста нет (старая закачка) — судим по размеру.
            changed[name] = meta
    return changed


def _download_file(
    opener,
    url: str,
    dest: Path,
    timeout: float,
    on_chunk: Callable[[int, Optional[int]], None],
) -> None:
    part = dest.with_name(dest.name + ".part")
    with opener.open(_request(url), timeout=timeout) as resp:
        length = resp.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else None
        done = 0
        with open(part, "wb") as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                on_chunk(done, total)
    os.replace(part, dest)


def _fetch_files(
    opener,
    repo: str,
    target: Path,
    files: dict,
    *,
    on_progress: Optional[Callable[[dict], None]],
    should_stop: Optional[Callable[[], bool]],
    timeout: float,
) -> None:
    """Скачать набор файлов ``{имя: meta}`` в ``target`` с прогрессом."""
    total = sum((m.get("size") or 0) for m in files.values()) or None
    count = len(files)
    downloaded = 0

    def notify(**kwargs) -> None:
        if on_progress is not None:
            try:
                on_progress(kwargs)
            except Exception:  # noqa: BLE001 - прогресс не должен ломать загрузку
                pass

    for index, (name, meta) in enumerate(files.items(), start=1):
        url = f"{HF_ENDPOINT}/{repo}/resolve/main/{name}"
        dest = target / name

        def on_chunk(file_done: int, file_total: Optional[int]) -> None:
            if should_stop is not None and should_stop():
                raise DownloadCancelled()
            percent = None
            if total:
                percent = min(100, int((downloaded + file_done) * 100 / total))
            notify(
                file=name,
                file_done=file_done,
                file_total=file_total,
                downloaded=downloaded + file_done,
                total=total,
                percent=percent,
                index=index,
                count=count,
            )

        try:
            _download_file(opener, url, dest, timeout, on_chunk)
        except DownloadCancelled:
            (dest.with_name(dest.name + ".part")).unlink(missing_ok=True)
            raise
        downloaded += meta.get("size") or dest.stat().st_size

    notify(
        file="",
        file_done=downloaded,
        file_total=downloaded,
        downloaded=downloaded,
        total=downloaded,
        percent=100,
        index=count,
        count=count,
    )


def download(
    size: str,
    models_dir,
    *,
    proxy_url: Optional[str] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    timeout: float = 60.0,
) -> Path:
    """Скачать модель ``size`` в ``models_dir/<папка>``.

    ``on_progress`` вызывается с dict: ``file``, ``file_done``, ``file_total``,
    ``downloaded``, ``total``, ``percent``, ``index``, ``count``.
    ``should_stop`` — функция-отмена: если вернёт True, бросим DownloadCancelled.
    """
    if size not in KNOWN_MODELS:
        raise ValueError(f"Неизвестная модель: {size}")

    repo = repo_for(size)
    target = Path(models_dir) / folder_name(size)
    target.mkdir(parents=True, exist_ok=True)

    with open_opener(proxy_url) as opener:
        info = remote_info(opener, repo, timeout)
        files = info["files"] or {
            name: {"size": None, "blobId": None} for name in sorted(ALLOWED_FILES)
        }
        _fetch_files(
            opener,
            repo,
            target,
            files,
            on_progress=on_progress,
            should_stop=should_stop,
            timeout=timeout,
        )
    write_manifest(models_dir, size, info)
    return target


def check_update(
    size: str,
    models_dir,
    *,
    proxy_url: Optional[str] = None,
    timeout: float = 60.0,
) -> list[str]:
    """Проверить обновления модели, ничего не скачивая.

    Возвращает имена файлов, которые отличаются от удалённых (пусто = актуально).
    """
    if size not in KNOWN_MODELS:
        raise ValueError(f"Неизвестная модель: {size}")
    repo = repo_for(size)
    target = Path(models_dir) / folder_name(size)
    with open_opener(proxy_url) as opener:
        info = remote_info(opener, repo, timeout)
    if not info["files"]:
        raise RuntimeError("Не удалось получить список файлов с Hugging Face")
    return list(_changed_files(target, read_manifest(models_dir, size), info["files"]))


def update(
    size: str,
    models_dir,
    *,
    proxy_url: Optional[str] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    timeout: float = 60.0,
) -> list[str]:
    """Обновить модель: скачать только изменившиеся файлы.

    Возвращает список перекачанных файлов (пусто = уже актуальна).
    """
    if size not in KNOWN_MODELS:
        raise ValueError(f"Неизвестная модель: {size}")
    repo = repo_for(size)
    target = Path(models_dir) / folder_name(size)
    if not target.is_dir():
        raise ValueError(f"Модель {size} не скачана — сначала скачайте её")

    with open_opener(proxy_url) as opener:
        info = remote_info(opener, repo, timeout)
        if not info["files"]:
            raise RuntimeError("Не удалось получить список файлов с Hugging Face")
        changed = _changed_files(target, read_manifest(models_dir, size), info["files"])
        if changed:
            _fetch_files(
                opener,
                repo,
                target,
                changed,
                on_progress=on_progress,
                should_stop=should_stop,
                timeout=timeout,
            )
    write_manifest(models_dir, size, info)
    return list(changed)

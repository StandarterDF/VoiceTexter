"""Настройки VoiceTexter: загрузка/сохранение config.json + список локальных моделей.

Модуль намеренно без зависимостей от GUI — его можно импортировать в тестах.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    """Рабочая папка приложения (рядом с .py либо рядом с .exe).

    В собранном PyInstaller-приложении ``__file__`` указывает внутрь
    временной/_internal-папки, поэтому ``config.json`` и ``models/`` ищем
    рядом с исполняемым файлом — это делает сборку переносимой.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    """Путь к упакованному ресурсу (assets/…): _MEIPASS при сборке, иначе корень."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = ROOT
    candidate = base / relative
    if candidate.exists():
        return candidate
    return ROOT / relative


ROOT = _base_dir()
MODELS_DIR = ROOT / "models"
CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG: dict = {
    "model": "whisper-large-v3-turbo",
    "device": "auto",  # auto | cpu | cuda
    "compute_type": "auto",  # auto | float16 | int8_float16 | int8 | float32
    "language": "ru",  # ru | en | auto
    "input_device": None,  # None = системный по умолчанию, иначе индекс/имя
    "binding": {"kind": "keyboard", "keys": ["ctrl", "alt", "space"]},
    "auto_paste": True,
    "restore_clipboard": True,
    "trailing_space": True,
    "vad_filter": True,
    "beam_size": 1,
    "initial_prompt": "",
    "paste_method": "Ctrl+V",
    "preload_on_start": True,
    "samplerate": 16000,
    "minimize_to_tray": True,  # закрытие окна = свернуть в трей, а не выход
    "start_minimized": False,  # запускать сразу в трее
    "proxy_enabled": False,  # использовать прокси для загрузки моделей
    "proxy_url": "",  # http://host:port или socks5://user:pass@host:port
}


def list_models() -> list[str]:
    """Имена локальных faster-whisper моделей в models/ (папки с model.bin)."""
    if not MODELS_DIR.is_dir():
        return []
    names = [
        p.name
        for p in MODELS_DIR.iterdir()
        if p.is_dir() and (p / "model.bin").exists()
    ]
    return sorted(names)


def model_size_label(name: str) -> str:
    """Человекочитаемый размер папки модели, напр. «1.5 GB»."""
    path = MODELS_DIR / name
    if not path.is_dir():
        return ""
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    if total >= 1024**3:
        return f"{total / 1024**3:.2f} GB"
    return f"{total / 1024**2:.0f} MB"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass

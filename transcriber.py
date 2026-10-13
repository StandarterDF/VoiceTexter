"""Обёртка над faster-whisper: ленивая загрузка модели и транскрипция.

Модель грузится в фоне (см. main.py), объект WhisperModel потокобезопасен для
последовательных вызовов transcribe, поэтому запросы сериализуются локом.
"""

from __future__ import annotations

import ctypes
import threading
from pathlib import Path

import numpy as np


def cuda_available() -> bool:
    """Есть ли NVML/CUDA-драйвер (аналог автодетекта из VoiceTranslator)."""
    if not hasattr(ctypes, "CDLL"):
        return False
    try:
        ctypes.CDLL("nvcuda.dll" if hasattr(ctypes, "WinDLL") else "libcuda.so.1")
        return True
    except OSError:
        return False


def resolve_device(preference: str) -> str:
    """auto -> cuda при наличии драйвера, иначе cpu."""
    pref = (preference or "auto").lower()
    if pref == "auto":
        return "cuda" if cuda_available() else "cpu"
    return pref


def resolve_compute(compute: str, device: str) -> str:
    """auto -> float16 на GPU, int8 на CPU (быстро и экономно по памяти)."""
    compute = (compute or "auto").lower()
    if compute != "auto":
        return compute
    return "float16" if device == "cuda" else "int8"


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._model_key: tuple | None = None
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self.device = "cpu"
        self.compute_type = "int8"

    @property
    def loaded_key(self) -> tuple | None:
        return self._model_key

    def is_loaded(self, model_path: str) -> bool:
        key = self._make_key(model_path)
        return self._model is not None and self._model_key == key

    def _make_key(self, model_path: str) -> tuple:
        return (str(Path(model_path).resolve()), self.device, self.compute_type)

    def load(
        self,
        model_path: str,
        device: str = "auto",
        compute_type: str = "auto",
    ) -> None:
        """Загрузить модель. Повторный вызов для той же связки — no-op."""
        resolved_device = resolve_device(device)
        resolved_compute = resolve_compute(compute_type, resolved_device)
        key = (str(Path(model_path).resolve()), resolved_device, resolved_compute)
        with self._load_lock:
            if self._model is not None and self._model_key == key:
                return
            from faster_whisper import WhisperModel

            model = WhisperModel(
                model_path,
                device=resolved_device,
                compute_type=resolved_compute,
            )
            with self._lock:
                self._model = model
                self._model_key = key
                self.device = resolved_device
                self.compute_type = resolved_compute

    def transcribe(
        self,
        audio: np.ndarray,
        language: str | None = None,
        beam_size: int = 1,
        vad_filter: bool = True,
    ) -> str:
        if self._model is None:
            raise RuntimeError("Модель не загружена")
        lang = None if not language or language == "auto" else language
        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=lang,
                beam_size=max(1, int(beam_size)),
                vad_filter=bool(vad_filter),
                condition_on_previous_text=False,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()

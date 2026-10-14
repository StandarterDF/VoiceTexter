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


# ----------------------------------------------------------- окно энкодера
#
# Whisper/CT2 штатно всегда дополняет мел-спектрограмму до 30 секунд (3000
# кадров) и кодирует её целиком — даже если фраза длится 2 секунды. Это ~6 с
# на CPU и заметная доля времени на GPU, независимо от длины клипа. Поэтому
# дополняем только до реальной длины клипа (с нижним порогом), а не до 30 с.
# Работает и на CPU, и на GPU (на GPU замер: 7 коротких фраз 2.24 с -> 0.70 с).
# 3000 = штатное поведение; меньше = быстрее, но контекст короче.
#
# ВАЖНО: слишком маленькое окно (<= 12 с) заставляет Whisper «зацикливаться» и
# повторять фразу целиком (модель обучалась только на 30-секундных окнах) —
# это и портит текст, и делает распознавание ДОЛЬШЕ (лишние токены). Значение
# 20 с на реальной русской речи даёт чистый текст при ускорении ~1.7x; поэтому
# по умолчанию берём 20, а не 8-10.

_MAX_FRAMES = 3000
_pad_seconds = 20.0
_pad_installed = False


def configure(pad_seconds: float | None = None) -> None:
    """Запомнить желаемое окно паддинга энкодера (в секундах; 30 = штат)."""
    global _pad_seconds
    if pad_seconds is not None:
        try:
            _pad_seconds = float(pad_seconds)
        except (TypeError, ValueError):
            pass


def _current_floor() -> int:
    seconds = max(1.0, min(_MAX_FRAMES / 100.0, _pad_seconds))
    return int(seconds * 100)


def _install_pad_floor() -> None:
    """Патч faster_whisper.transcribe.pad_or_trim: не паддить до 30 с."""
    global _pad_installed
    if _pad_installed:
        return
    try:
        import faster_whisper.transcribe as fw
    except Exception:  # noqa: BLE001
        return
    original = getattr(fw, "pad_or_trim", None)
    if original is None:
        return

    def pad_or_trim(array, length=None, *, axis=-1):
        if length is None:
            length = max(int(array.shape[axis]), _current_floor())
        return original(array, length, axis=axis)

    fw.pad_or_trim = pad_or_trim
    _pad_installed = True


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._model_key: tuple | None = None
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self.device = "cpu"
        self.compute_type = "int8"
        self.pad_seconds = 20.0  # окно энкодера (30 = штатные 30 с)

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
            # Кодируем только реальную длину клипа (см. окно энкодера выше).
            configure(self.pad_seconds)
            _install_pad_floor()

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
        initial_prompt: str | None = None,
    ) -> str:
        if self._model is None:
            raise RuntimeError("Модель не загружена")
        lang = None if not language or language == "auto" else language
        prompt = (initial_prompt or "").strip() or None
        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=lang,
                beam_size=max(1, int(beam_size)),
                vad_filter=bool(vad_filter),
                initial_prompt=prompt,
                condition_on_previous_text=False,
                # Страховка от «зацикливания» Whisper (повтора n-грамм) —
                # особенно заметно при уменьшенном окне энкодера.
                no_repeat_ngram_size=3,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()

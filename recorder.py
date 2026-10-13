"""Запись с микрофона через sounddevice.

Держит один InputStream, копит кадры в память и на ``stop()`` возвращает
одномерный float32-массив (16 кГц, mono), который faster-whisper принимает
напрямую, без промежуточного WAV.
"""

from __future__ import annotations

import threading

import numpy as np
import sounddevice as sd


class Recorder:
    def __init__(self, samplerate: int = 16000, channels: int = 1) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._status_error: str | None = None

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    @property
    def last_error(self) -> str | None:
        return self._status_error

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            self._status_error = str(status)
        self._frames.append(indata.copy())

    def start(self, device=None) -> bool:
        """Начать запись. Возвращает False, если уже идёт или устройство недоступно."""
        with self._lock:
            if self._stream is not None:
                return False
            self._frames = []
            self._status_error = None
            try:
                self._stream = sd.InputStream(
                    device=device,
                    samplerate=self.samplerate,
                    channels=self.channels,
                    dtype="float32",
                    callback=self._callback,
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001
                self._stream = None
                self._status_error = str(exc)
                return False
            return True

    def stop(self) -> np.ndarray | None:
        """Остановить запись и вернуть аудио (float32 1-D) либо None."""
        with self._lock:
            stream = self._stream
            self._stream = None
            frames = self._frames
            self._frames = []
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        if not frames:
            return None
        audio = np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)
        return audio

    def abort(self) -> None:
        """Остановить запись без возврата аудио."""
        with self._lock:
            stream = self._stream
            self._stream = None
            self._frames = []
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass


def list_input_devices() -> list[tuple[int, str]]:
    """Список входных устройств как (индекс, «имя (host api)»)."""
    result: list[tuple[int, str]] = []
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        return result
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0:
            api = ""
            try:
                api = hostapis[dev["hostapi"]]["name"]
            except Exception:  # noqa: BLE001
                pass
            result.append((idx, f"{dev['name']} [{api}]"))
    return result

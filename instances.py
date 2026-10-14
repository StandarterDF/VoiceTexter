"""Обнаружение других запущенных копий VoiceTexter.

Несколько копий одновременно перехватывают один и тот же хоткей, читают
микрофон, вставляют текст в буфер обмена и пишут один ``config.json`` —
поэтому при старте приложение предупреждает, если рядом уже работает
ещё одна копия (в т.ч. другой версии).

Для собранного EXE копии ищутся по имени образа процесса (ловятся и старые
версии). При запуске из исходников используется именованный мьютекс Windows:
все новые копии-исходники видят его и сообщают о себе.
"""

from __future__ import annotations

import os
import sys

MUTEX_NAME = "VoiceTexter.Instance"
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1

_handle = None  # держим handle мьютекса, пока жив процесс
_already_running = False


def _create_mutex() -> tuple[object, bool]:
    """Создать именованный мьютекс. Second bool = он уже существовал."""
    if os.name != "nt":
        return None, False
    import ctypes

    k32 = ctypes.windll.kernel32
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    already = k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    return handle, bool(already)


def init() -> bool:
    """Инициализировать определение копий. True, если другая уже запущена."""
    global _handle, _already_running
    if getattr(sys, "frozen", False):
        return False  # для EXE достаточно перечисления процессов
    _handle, _already_running = _create_mutex()
    return _already_running


def _other_pids_by_name(name: str) -> list[int]:
    """PID'ы процессов с заданным именем образа (без текущего)."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.windll.kernel32
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    pids: list[int] = []
    try:
        if not k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            if (
                entry.szExeFile.lower() == name.lower()
                and entry.th32ProcessID != os.getpid()
            ):
                pids.append(int(entry.th32ProcessID))
            if not k32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)
    return pids


def other_count() -> int:
    """Сколько ещё копий VoiceTexter сейчас запущено (без текущей)."""
    try:
        if getattr(sys, "frozen", False):
            return len(_other_pids_by_name(os.path.basename(sys.executable)))
        return 1 if _already_running else 0
    except Exception:  # noqa: BLE001 - определение копий не должно ронять app
        return 0

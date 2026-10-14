"""Обнаружение других запущенных копий VoiceTexter.

Несколько копий одновременно перехватывают один и тот же хоткей, читают
микрофон, вставляют текст в буфер обмена, пишут один ``config.json`` и, главное,
держат в памяти по полной копии модели — поэтому вторая копия теперь **не
запускается**: при старте приложение проверяет, не работает ли уже другая копия,
и, если да, показывает сообщение и выходит.

Для собранного EXE копии ищутся по имени образа процесса (ловятся и старые
версии, которые ещё не умели блокировать запуск). Дополнительно всегда
создаётся именованный мьютекс Windows — его видят и копии из исходников.

Отключить блокировку (например, для отладки) можно переменной окружения
``VOICETEXTER_ALLOW_MULTI=1``.
"""

from __future__ import annotations

import os
import sys

MUTEX_NAME = "VoiceTexter.Instance"
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1

_handle = None  # держим handle мьютекса, пока жив процесс
_already_running = False
_initialized = False


def _allow_multi() -> bool:
    """Разрешить несколько копий (для отладки/тестов)."""
    return os.environ.get("VOICETEXTER_ALLOW_MULTI", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


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
    """Создать мьютекс (один раз). True, если другая копия уже держит его.

    Идемпотентна: повторный вызов в том же процессе не считает собственную
    копию «чужой» (иначе мьютекс тут же «занят» нами же).
    """
    global _handle, _already_running, _initialized
    if _initialized:
        return _already_running
    _handle, _already_running = _create_mutex()
    _initialized = True
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


def others() -> int:
    """Сколько ещё копий VoiceTexter сейчас запущено (без текущей)."""
    try:
        count = 1 if _already_running else 0
        if getattr(sys, "frozen", False):
            count = max(
                count, len(_other_pids_by_name(os.path.basename(sys.executable)))
            )
        return count
    except Exception:  # noqa: BLE001 - определение копий не должно ронять app
        return 0


def _notify(text: str) -> None:
    """Показать нативное окно Windows (без Tk), не роняя приложение."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            text,
            "VoiceTexter",
            0x00000040,  # MB_ICONINFORMATION | MB_OK
        )
    except Exception:  # noqa: BLE001
        pass


def block_if_running() -> bool:
    """Если уже работает другая копия — сообщить и вернуть True (нужно выйти)."""
    if _allow_multi():
        return False
    try:
        init()
        count = others()
    except Exception:  # noqa: BLE001
        return False
    if not count:
        return False
    _notify(
        "VoiceTexter уже запущен.\n\n"
        "Вторая копия не будет запущена: копии делят микрофон, горячую клавишу, "
        "буфер обмена и config.json, а также держат в памяти по полной копии "
        "модели (лишняя нагрузка на процессор и оперативную память).\n\n"
        "Закройте уже работающее окно (или завершите процесс VoiceTexter.exe) "
        "и запустите снова."
    )
    return True

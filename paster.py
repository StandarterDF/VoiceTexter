"""Вставка текста в активное окно через буфер обмена + горячая клавиша.

На Windows комбинация отправляется нативно через ``SendInput`` с явным
виртуальным кодом (VK_CONTROL + VK_V). pynput для инъекции не используется:
у его Controller бывают случаи, когда приложение «теряет» модификатор и
получает одиночный ``v``.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

import pyperclip

_IS_WINDOWS = sys.platform.startswith("win")

# --- виртуальные коды клавиш -------------------------------------------------
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_V = 0x56
VK_INSERT = 0x2D

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
_EXTENDED = {VK_INSERT}

# пауза между нажатиями: приложения должны успеть «увидеть» модификатор
HOLD = 0.06

if _IS_WINDOWS:
    ULONG_PTR = ctypes.c_size_t

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            # все три варианта обязательны: иначе sizeof(_INPUT) не совпадёт
            # с ожидаемым Windows, и SendInput вернёт 0 (ничего не отправив).
            _fields_ = [
                ("ki", _KEYBDINPUT),
                ("mi", _MOUSEINPUT),
                ("hi", _HARDWAREINPUT),
            ]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT

    def _send_key(vk: int, keyup: bool = False) -> int:
        flags = KEYEVENTF_KEYUP if keyup else 0
        if vk in _EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        inp = _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0),
        )
        return _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def _pynput_fallback(modifier_vk: int, key_vk: int) -> None:  # pragma: no cover
        from pynput.keyboard import Controller, Key

        kb = Controller()
        mod = {VK_CONTROL: Key.ctrl_l, VK_SHIFT: Key.shift}.get(modifier_vk, Key.ctrl_l)
        key = {VK_V: "v", VK_INSERT: Key.insert}.get(key_vk, "v")
        kb.press(mod)
        time.sleep(HOLD)
        kb.press(key)
        time.sleep(HOLD)
        kb.release(key)
        time.sleep(HOLD)
        kb.release(mod)

    def _send_hotkey(modifier_vk: int, key_vk: int) -> None:
        steps = [
            (modifier_vk, False),
            (key_vk, False),
            (key_vk, True),
            (modifier_vk, True),
        ]
        ok = True
        for vk, keyup in steps:
            if _send_key(vk, keyup) == 0:
                ok = False
            time.sleep(HOLD)
        if not ok:
            # SendInput заблокирован (напр. другое окно с иным уровнем прав) —
            # пробуем запасной путь через pynput.
            _pynput_fallback(modifier_vk, key_vk)

else:  # pragma: no cover - не-Windows fallback
    from pynput.keyboard import Controller, Key

    def _send_hotkey(modifier_vk: int, key_vk: int) -> None:
        kb = Controller()
        mod = {VK_CONTROL: Key.ctrl, VK_SHIFT: Key.shift}[modifier_vk]
        key = {VK_V: "v", VK_INSERT: Key.insert}[key_vk]
        with kb.pressed(mod):
            kb.press(key)
            time.sleep(HOLD)
            kb.release(key)


PASTE_METHODS = {
    "Ctrl+V": (VK_CONTROL, VK_V),
    "Shift+Insert": (VK_SHIFT, VK_INSERT),
}


def paste_text(
    text: str,
    restore_clipboard: bool = True,
    trailing_space: bool = False,
    method: str = "Ctrl+V",
) -> None:
    """Положить текст в буфер, нажать горячую клавишу вставки, вернуть буфер.

    Вставка уходит в то окно, что в фокусе на момент вызова, — само окно
    VoiceTexter фокус не забирает.
    """
    if not text:
        return
    if trailing_space and not text.endswith(" "):
        text += " "

    previous: str | None = None
    if restore_clipboard:
        try:
            previous = pyperclip.paste()
        except Exception:  # noqa: BLE001
            previous = None

    try:
        pyperclip.copy(text)
    except Exception:  # noqa: BLE001
        pass

    # Буферу обмена «устояться» и кнопке-биндингу отпуститься.
    time.sleep(0.12)

    modifier_vk, key_vk = PASTE_METHODS.get(method, PASTE_METHODS["Ctrl+V"])
    _send_hotkey(modifier_vk, key_vk)

    if restore_clipboard and previous is not None:
        time.sleep(0.35)
        try:
            pyperclip.copy(previous)
        except Exception:  # noqa: BLE001
            pass

"""Вставка текста в активное окно через буфер обмена + Ctrl+V."""

from __future__ import annotations

import time

import pyperclip
from pynput.keyboard import Controller, Key


def paste_text(
    text: str,
    restore_clipboard: bool = True,
    trailing_space: bool = False,
) -> None:
    """Положить текст в буфер, нажать Ctrl+V и (опционально) вернуть буфер.

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
        # Если буфер недоступен — хотя бы не падаем.
        pass

    time.sleep(0.05)
    keyboard = Controller()
    with keyboard.pressed(Key.ctrl):
        keyboard.press("v")
        keyboard.release("v")

    if restore_clipboard and previous is not None:
        time.sleep(0.25)
        try:
            pyperclip.copy(previous)
        except Exception:  # noqa: BLE001
            pass

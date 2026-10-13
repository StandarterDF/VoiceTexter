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

    Между нажатиями специально стоят небольшие паузы: если отправить Ctrl и V
    подряд без задержки, ряд приложений не успевает «увидеть» модификатор и
    получает просто символ ``v``.
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

    # Дать буферу обмена «устояться» и отпустить кнопку-биндинг.
    time.sleep(0.10)

    keyboard = Controller()
    keyboard.press(Key.ctrl_l)
    time.sleep(0.05)
    keyboard.press("v")
    time.sleep(0.05)
    keyboard.release("v")
    time.sleep(0.05)
    keyboard.release(Key.ctrl_l)

    if restore_clipboard and previous is not None:
        time.sleep(0.30)
        try:
            pyperclip.copy(previous)
        except Exception:  # noqa: BLE001
            pass

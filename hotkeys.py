"""Глобальные горячие клавиши и кнопки мыши (push-to-talk) на pynput.

Биндинг — JSON-совместимый dict:
  {"kind": "keyboard", "keys": ["ctrl", "alt", "space"]}
  {"kind": "mouse", "button": "x2"}

Поведение «пока нажато — пишем»: запись стартует, когда все клавиши комбо
(или кнопка мыши) удерживаются, и останавливается, когда любая из них
отпущена.
"""

from __future__ import annotations

import threading
from typing import Callable

from pynput import keyboard, mouse

_MOD_ALIASES = {
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "shift_l": "shift",
    "shift_r": "shift",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
    "cmd": "cmd",
}

_DISPLAY = {
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "cmd": "Win",
    "space": "Space",
    "enter": "Enter",
    "tab": "Tab",
    "esc": "Esc",
    "backspace": "Backspace",
    "delete": "Del",
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
    "page_up": "PgUp",
    "page_down": "PgDn",
    "caps_lock": "CapsLock",
}

_MOUSE_DISPLAY = {
    "left": "ЛКМ",
    "right": "ПКМ",
    "middle": "СКМ",
    "x1": "Мышь 4 (назад)",
    "x2": "Мышь 5 (вперёд)",
}

_MODIFIER_NAMES = {"ctrl", "alt", "shift", "cmd", "caps_lock"}


def normalize_key(key) -> str | None:
    """pynput key -> короткое строковое имя ('ctrl', 'space', 'f9', 'ы'...)."""
    if isinstance(key, keyboard.Key):
        name = key.name
        return _MOD_ALIASES.get(name, name)
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk:
            return f"vk{key.vk}"
    return None


def normalize_button(button) -> str | None:
    try:
        return button.name
    except AttributeError:
        return str(button)


def binding_label(binding: dict | None) -> str:
    if not binding:
        return "не задано"
    kind = binding.get("kind")
    if kind == "mouse":
        btn = binding.get("button", "?")
        return _MOUSE_DISPLAY.get(btn, f"Мышь {btn}")
    keys = binding.get("keys") or []
    parts = [_DISPLAY.get(k, k.upper() if len(k) == 1 else k) for k in keys]
    return " + ".join(parts) if parts else "не задано"


class HotkeyManager:
    def __init__(
        self,
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None],
    ) -> None:
        self.on_activate = on_activate
        self.on_deactivate = on_deactivate

        self._binding: dict | None = None
        self._active = False
        self._pressed: set[str] = set()
        self._pressed_buttons: set[str] = set()
        self._kb_listener: keyboard.Listener | None = None
        self._mouse_listener: mouse.Listener | None = None
        self._enabled = False
        self._lock = threading.Lock()
        self._capturing = False
        self._capture_cb: Callable[[dict], None] | None = None

    # -- управление жизненным циклом ---------------------------------------

    def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self._kb_listener = keyboard.Listener(
            on_press=self._on_key_press, on_release=self._on_key_release
        )
        self._mouse_listener = mouse.Listener(on_click=self._on_click)
        self._kb_listener.start()
        self._mouse_listener.start()

    def stop(self) -> None:
        self._enabled = False
        for listener in (self._kb_listener, self._mouse_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:  # noqa: BLE001
                    pass
        self._kb_listener = None
        self._mouse_listener = None
        self._pressed.clear()
        self._pressed_buttons.clear()
        if self._active:
            self._active = False
            self.on_deactivate()

    # -- биндинг ------------------------------------------------------------

    def set_binding(self, binding: dict | None) -> None:
        with self._lock:
            self._binding = binding
            self._pressed.clear()
            self._pressed_buttons.clear()
            was_active = self._active
            self._active = False
        if was_active:
            self.on_deactivate()

    @property
    def binding(self) -> dict | None:
        return self._binding

    # -- события клавиатуры -------------------------------------------------

    def _requirements_met(self) -> bool:
        b = self._binding
        if not b:
            return False
        if b.get("kind") == "mouse":
            return b.get("button") in self._pressed_buttons
        keys = set(b.get("keys") or [])
        return bool(keys) and keys.issubset(self._pressed)

    def _evaluate(self) -> None:
        met = self._requirements_met()
        if met and not self._active:
            self._active = True
            self.on_activate()
        elif not met and self._active:
            self._active = False
            self.on_deactivate()

    def _on_key_press(self, key) -> None:
        name = normalize_key(key)
        if name is None:
            return
        if self._capturing:
            self._capture_key(name)
            return
        with self._lock:
            self._pressed.add(name)
        self._evaluate()

    def _on_key_release(self, key) -> None:
        name = normalize_key(key)
        if name is None:
            return
        with self._lock:
            self._pressed.discard(name)
        self._evaluate()

    # -- события мыши -------------------------------------------------------

    def _on_click(self, x, y, button, pressed) -> None:  # noqa: ARG002
        name = normalize_button(button)
        if name is None:
            return
        if self._capturing:
            if pressed:
                self._capture_mouse(name)
            return
        with self._lock:
            if pressed:
                self._pressed_buttons.add(name)
            else:
                self._pressed_buttons.discard(name)
        self._evaluate()

    # -- захват нового биндинга --------------------------------------------

    def begin_capture(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._capturing = True
            self._capture_cb = callback
            self._pressed.clear()
            self._pressed_buttons.clear()
            if self._active:
                self._active = False
                self.on_deactivate()

    def cancel_capture(self) -> None:
        with self._lock:
            self._capturing = False
            self._capture_cb = None

    def _finish_capture(self, binding: dict) -> None:
        with self._lock:
            if not self._capturing:
                return
            self._capturing = False
            cb = self._capture_cb
            self._capture_cb = None
        if cb is not None:
            cb(binding)

    def _capture_key(self, name: str) -> None:
        if name in _MODIFIER_NAMES:
            # ждём основную клавишу; модификаторы уже в _pressed
            with self._lock:
                self._pressed.add(name)
            return
        with self._lock:
            mods = sorted(k for k in self._pressed if k in _MODIFIER_NAMES)
        keys = mods + [name]
        self._finish_capture({"kind": "keyboard", "keys": keys})

    def _capture_mouse(self, name: str) -> None:
        self._finish_capture({"kind": "mouse", "button": name})

"""Значок в системном трее (pystray) — окно можно свернуть, не закрывая.

pystray держит собственный поток и собственный цикл сообщений Windows,
поэтому все действия меню (показать окно / пауза / выход) — это просто
колбэки, которые вызываются **из потока трея**. Они не должны трогать
виджеты Tkinter: см. ``main.VoiceTexterApp`` — там они лишь кладут событие
в потокобезопасную очередь.

Модуль необязательный: если pystray/Pillow нет, ``tray_available()`` вернёт
False, и приложение продолжит работать как раньше (закрытие = выход).
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

try:  # noqa: SIM105 - импорт необязательной зависимости
    from PIL import Image
    import pystray
    from pystray import Menu, MenuItem
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    pystray = None  # type: ignore[assignment]
    Menu = MenuItem = None  # type: ignore[assignment]


APP_NAME = "VoiceTexter"


def tray_available() -> bool:
    """Установлены ли зависимости для трея (pystray + Pillow)."""
    return pystray is not None and Image is not None


def load_image(path) -> "Image.Image | None":
    """Загрузить картинку значка из файла (PNG/ICO). None при ошибке."""
    if Image is None:
        return None
    try:
        return Image.open(str(path)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


class TrayController:
    """Значок в трее с меню: показать окно / пауза / выход.

    Колбэки вызываются из потока трея — они обязаны быть потокобезопасными.
    """

    def __init__(
        self,
        image,
        on_show: Callable[[], None],
        on_toggle_listening: Callable[[], None],
        on_quit: Callable[[], None],
        listening: bool = True,
        title: str = APP_NAME,
        on_failed: Optional[Callable[[], None]] = None,
    ) -> None:
        if not tray_available():
            raise RuntimeError("pystray/Pillow недоступны")
        self._on_show = on_show
        self._on_toggle_listening = on_toggle_listening
        self._on_quit = on_quit
        self._on_failed = on_failed
        self._listening = bool(listening)
        self._title = title

        self._toggle_item = MenuItem(self._toggle_text, self._wrap(on_toggle_listening))
        menu = Menu(
            MenuItem("Открыть окно", self._wrap(on_show), default=True),
            self._toggle_item,
            Menu.SEPARATOR,
            MenuItem("Выход", self._wrap(on_quit)),
        )
        self._icon = pystray.Icon(APP_NAME, image, title, menu)
        self._thread: threading.Thread | None = None

    # -- вспомогательное ---------------------------------------------------

    @staticmethod
    def _wrap(fn: Callable[[], None]):
        def handler(icon, item) -> None:  # noqa: ARG001 - сигнатура pystray
            try:
                fn()
            except Exception:  # noqa: BLE001 - трей не должен падать
                pass

        return handler

    def _toggle_text(self, item) -> str:  # noqa: ARG002
        return "Поставить на паузу" if self._listening else "Возобновить прослушивание"

    # -- жизненный цикл ----------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="VoiceTexterTray", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:  # noqa: BLE001 - без трея приложение обязано жить
            if self._on_failed is not None:
                try:
                    self._on_failed()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001
            pass

    # -- обновление состояния ---------------------------------------------

    def set_listening(self, listening: bool) -> None:
        self._listening = bool(listening)
        self._refresh()

    def set_title(self, title: str) -> None:
        self._title = title
        try:
            self._icon.title = title
        except Exception:  # noqa: BLE001
            pass

    def _refresh(self) -> None:
        try:
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

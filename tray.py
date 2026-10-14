"""Значок в системном трее (pystray) — окно можно свернуть, не закрывая.

pystray держит собственный поток и собственный цикл сообщений Windows,
поэтому все действия меню (показать окно / пауза / быстрые настройки / выход)
— это колбэки, которые вызываются **из потока трея**. Они не должны трогать
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

# Человекочитаемые подписи для быстрых настроек.
DEVICE_LABELS = {"auto": "Авто", "cpu": "CPU", "cuda": "GPU (CUDA)"}
LANGUAGE_LABELS = {"ru": "Русский", "en": "English", "auto": "Авто"}
COMPUTE_LABELS = {
    "auto": "Авто",
    "float16": "float16 (GPU)",
    "int8_float16": "int8_float16",
    "int8": "int8 (CPU)",
    "float32": "float32",
}

# Значения по умолчанию для списков в меню.
DEFAULT_DEVICES = ("auto", "cuda", "cpu")
DEFAULT_COMPUTES = ("auto", "float16", "int8_float16", "int8", "float32")
DEFAULT_LANGUAGES = ("ru", "en", "auto")


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
    """Значок в трее с меню: окно, пауза, быстрые настройки, выход.

    Колбэки вызываются из потока трея — они обязаны быть потокобезопасными.
    ``on_set(name, value)`` повторяет изменение настройки: ``name`` — из
    ``device``/``compute_type``/``model``/``language``/``auto_paste``/``vad``.
    """

    def __init__(
        self,
        image,
        *,
        on_show: Callable[[], None],
        on_toggle_listening: Callable[[], None],
        on_quit: Callable[[], None],
        on_set: Callable[[str, object], None],
        on_reload: Optional[Callable[[], None]] = None,
        on_failed: Optional[Callable[[], None]] = None,
        on_download: Optional[Callable[[str], None]] = None,
        devices=DEFAULT_DEVICES,
        computes=DEFAULT_COMPUTES,
        models=(),
        downloads=(),
        languages=DEFAULT_LANGUAGES,
        state: Optional[dict] = None,
        title: str = APP_NAME,
    ) -> None:
        if not tray_available():
            raise RuntimeError("pystray/Pillow недоступны")

        self._on_show = on_show
        self._on_toggle_listening = on_toggle_listening
        self._on_quit = on_quit
        self._on_set = on_set
        self._on_reload = on_reload
        self._on_failed = on_failed
        self._on_download = on_download
        self._devices = tuple(devices)
        self._computes = tuple(computes)
        self._models = tuple(models)
        self._downloads = tuple(downloads)
        self._languages = tuple(languages)
        self._state: dict = {
            "listening": True,
            "device": "auto",
            "compute_type": "auto",
            "model": (self._models[0] if self._models else ""),
            "language": "ru",
            "auto_paste": True,
            "vad": True,
        }
        if state:
            self._state.update(state)
        self._title = title

        self._icon = pystray.Icon(APP_NAME, image, title, self._build_menu())
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- меню

    def _build_menu(self):
        model_items = list(self._model_group())
        if self._downloads:
            model_items.append(Menu.SEPARATOR)
            model_items.append(
                MenuItem("Скачать модель", Menu(*self._download_group()))
            )
        items = [
            MenuItem("Открыть окно", self._wrap(self._on_show), default=True),
            MenuItem(self._listening_text, self._wrap(self._on_toggle_listening)),
            Menu.SEPARATOR,
            MenuItem(
                "Устройство",
                Menu(*self._radio_group("device", self._devices, DEVICE_LABELS)),
            ),
            MenuItem(
                "Точность",
                Menu(
                    *self._radio_group("compute_type", self._computes, COMPUTE_LABELS)
                ),
            ),
            MenuItem(
                "Модель",
                Menu(*model_items),
                enabled=bool(self._models or self._downloads),
            ),
            MenuItem(
                "Язык",
                Menu(*self._radio_group("language", self._languages, LANGUAGE_LABELS)),
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Вставлять текст автоматически",
                self._wrap(self._make_toggle("auto_paste")),
                checked=self._checked_bool("auto_paste"),
            ),
            MenuItem(
                "Отсекать тишину (VAD)",
                self._wrap(self._make_toggle("vad")),
                checked=self._checked_bool("vad"),
            ),
            MenuItem(
                "Перезагрузить модель", self._wrap(self._on_reload or (lambda: None))
            ),
            Menu.SEPARATOR,
            MenuItem("Выход", self._wrap(self._on_quit)),
        ]
        return Menu(*items)

    def _radio_group(self, setting: str, values, labels: dict) -> list:
        return [
            MenuItem(
                str(labels.get(value, value)),
                self._wrap(self._make_set(setting, value, toggle=False)),
                checked=self._checked_value(setting, value),
                radio=True,
            )
            for value in values
        ]

    def _model_group(self) -> list:
        if not self._models:
            return [MenuItem("нет моделей", None, enabled=False)]
        return [
            MenuItem(
                str(model),
                self._wrap(self._make_set("model", model, toggle=False)),
                checked=self._checked_value("model", model),
                radio=True,
            )
            for model in self._models
        ]

    def _download_group(self) -> list:
        items = []
        for entry in self._downloads:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                size, label = entry[0], entry[1]
            else:
                size, label = entry, entry
            items.append(
                MenuItem(str(label), self._wrap(self._make_download(str(size))))
            )
        return items or [MenuItem("все модели скачаны", None, enabled=False)]

    def _make_download(self, size: str):
        def action() -> None:
            if self._on_download is not None:
                self._on_download(size)

        return action

    # ---------------------------------------------- обёртки для pystray

    @staticmethod
    def _wrap(fn):
        def handler(icon, item) -> None:  # noqa: ARG001 - сигнатура pystray
            try:
                fn()
            except Exception:  # noqa: BLE001 - трей не должен падать
                pass

        return handler

    def _listening_text(self, item) -> str:  # noqa: ARG002
        return (
            "Поставить на паузу"
            if self._state.get("listening")
            else "Возобновить прослушивание"
        )

    def _make_set(self, setting: str, value, *, toggle: bool):
        def action() -> None:
            self._emit_set(setting, value, toggle=toggle)

        return action

    def _make_toggle(self, setting: str):
        def action() -> None:
            self._emit_set(setting, not bool(self._state.get(setting)), toggle=True)

        return action

    def _emit_set(self, setting: str, value, *, toggle: bool) -> None:
        # Мгновенно отражаем выбор в меню; окончательное состояние подтвердит
        # главный поток через update_state().
        if toggle:
            self._state[setting] = bool(value)
        elif setting in self._state:
            self._state[setting] = value
        try:
            self._on_set(setting, value)
        except Exception:  # noqa: BLE001
            pass
        self._refresh()

    def _checked_value(self, setting: str, value):
        def checked(item) -> bool:  # noqa: ARG001
            return self._state.get(setting) == value

        return checked

    def _checked_bool(self, setting: str):
        def checked(item) -> bool:  # noqa: ARG001
            return bool(self._state.get(setting))

        return checked

    # ------------------------------------------------------- жизненный цикл

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

    # ---------------------------------------------------------- состояние

    def update_state(self, **values) -> None:
        """Обновить галочки/подписи меню (вызывается из главного потока)."""
        changed = False
        for key, value in values.items():
            if key in self._state and self._state[key] != value:
                self._state[key] = value
                changed = True
        if changed:
            self._refresh()

    def set_title(self, title: str) -> None:
        self._title = title
        try:
            self._icon.title = title
        except Exception:  # noqa: BLE001
            pass

    def update_models(self, models) -> None:
        """Обновить список локальных моделей (после загрузки новой)."""
        self._models = tuple(models)
        self._rebuild()

    def update_downloads(self, downloads) -> None:
        """Обновить список доступных для загрузки моделей."""
        self._downloads = tuple(downloads)
        self._rebuild()

    def _rebuild(self) -> None:
        try:
            self._icon.menu = self._build_menu()
        except Exception:  # noqa: BLE001
            pass
        self._refresh()

    def _refresh(self) -> None:
        try:
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

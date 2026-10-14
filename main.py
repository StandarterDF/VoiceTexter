"""VoiceTexter — push-to-talk диктовка в любое активное поле.

Держите привязанную клавишу/кнопку мыши → идёт запись микрофона,
отпустили → локальная модель faster-whisper распознаёт речь и вставляет
текст в активное окно (Ctrl+V).

Запуск:  .\\.venv\\Scripts\\python.exe main.py
"""

from __future__ import annotations

import math
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path

import customtkinter as ctk

import config
import instances
import modelstore
import updater
from hotkeys import HotkeyManager, binding_label
from paster import paste_text
from recorder import Recorder, list_input_devices
from textformat import format_transcript
from transcriber import Transcriber, configure as transcriber_configure, cuda_available
from tray import TrayController, load_image, tray_available

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

FONT_FAMILY = "Segoe UI"

PALETTE = {
    "bg": "#0f1115",
    "panel": "#171a21",
    "panel_2": "#1e222c",
    "border": "#272c37",
    "text": "#e8eaf0",
    "muted": "#818899",
    "faint": "#5b6274",
    "accent": "#7c8cff",
    "accent_hover": "#93a0ff",
    "record": "#ff5a6a",
    "busy": "#f2b64c",
    "ok": "#5fd08a",
}

# цвет кольца / заливки орба по состоянию
ORB = {
    "ready": (PALETTE["accent"], PALETTE["accent"]),
    "record": (PALETTE["record"], PALETTE["record"]),
    "busy": (PALETTE["busy"], PALETTE["busy"]),
    "error": (PALETTE["record"], PALETTE["record"]),
    "idle": (PALETTE["border"], PALETTE["panel_2"]),
}

STATUS_COLOR = {
    "ready": PALETTE["text"],
    "record": PALETTE["record"],
    "busy": PALETTE["busy"],
    "error": PALETTE["record"],
    "idle": PALETTE["muted"],
}

HINTS = {
    "ready": "Удерживайте бинд — пойдёт запись, отпустите — текст вставится.",
    "record": "Идёт запись… отпустите бинд, чтобы распознать.",
    "busy": "Распознаю речь…",
    "error": "Что-то не так. Проверьте микрофон и модель.",
    "idle": "Прослушивание приостановлено.",
}

MIN_AUDIO_SECONDS = 0.25

# Подписи для переключателя устройства; в config.json хранятся значения auto/cuda/cpu.
DEVICE_LABELS = {"auto": "Авто", "cuda": "GPU", "cpu": "CPU"}
DEVICE_VALUES = {label: value for value, label in DEVICE_LABELS.items()}


class VoiceTexterApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = config.load_config()
        # Одноразовая миграция окна энкодера: любое окно < 30 с заставляло
        # Whisper галлюцинировать/повторять текст, поэтому поднимаем до штатных
        # 30 с. Маркер не даёт сбрасывать сделанный позже осознанный выбор.
        if int(self.cfg.get("pad_seconds_v", 0) or 0) < 2:
            try:
                if float(self.cfg.get("pad_seconds", 30) or 30) < 30:
                    self.cfg["pad_seconds"] = 30.0
            except (TypeError, ValueError):
                pass
            self.cfg["pad_seconds_v"] = 2
        self.title("VoiceTexter")

        # --- внутреннее состояние (читается и из потоков слушателей) ---
        self._events: queue.Queue = queue.Queue()
        self._recording = False
        self._busy = False
        self._loading = False
        self._downloading = False
        self._download_cancel = threading.Event()
        self._active_folder: str | None = None  # что качается/проверяется сейчас
        self._dl_kind: str = ""  # "download" | "update" | "check"
        self._model_notes: dict[str, str] = {}  # папка -> "update"/"actual"/"error"
        self._listening = True
        self._capturing = False
        self._closing = False
        self._force_quit = False
        self._tray: TrayController | None = None
        self._setup: dict | None = None
        self._state = "idle"
        self._model_detail = ""
        self._phase = 0.0
        self._angle = 0.0
        # кэш настроек как «простые» значения (безопасно читать из потоков)
        self._model_name: str = ""
        self._input_device = None
        self._lang: str = "ru"
        self._beam_size: int = 1
        self._initial_prompt: str = ""
        self._paste_method: str = "Ctrl+V"
        self._vad: bool = True
        self._devices: list[tuple[int, str]] = []

        self.recorder = Recorder(samplerate=int(self.cfg.get("samplerate", 16000)))
        self.transcriber = Transcriber()
        self.transcriber.pad_seconds = float(
            self.cfg.get("pad_seconds", self.cfg.get("cpu_pad_seconds", 30.0))
        )
        transcriber_configure(self.transcriber.pad_seconds)
        self.hotkeys = HotkeyManager(self._on_activate, self._on_deactivate)

        # Именованный мьютекс (идемпотентно): замечаем уже работающую копию.
        instances.init()

        self._build_ui()
        self._center_window()
        self._load_settings_into_ui()
        self._refresh_model_rows_state()
        # Биндинг ставим ДО сохранения конфига, иначе в config.json уйдёт null.
        self.hotkeys.set_binding(self.cfg.get("binding"))
        self._sync_cached_settings()
        self.hotkeys.start()

        self._log("VoiceTexter запущен.")
        self._setup_window_icon()
        self._setup_tray()
        self.after(80, self._drain_events)
        self.after(40, self._animate)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if config.list_models():
            self._set_status("idle", "Готов к работе", "Модель загружается…")
        else:
            self._set_status(
                "idle", "Нет моделей", "Скачайте модель — кнопка ниже или в трее"
            )
            self._log("Локальных моделей нет. Нажмите «Скачать модель».")

        if self.cfg.get("start_minimized") and self._tray is not None:
            self.after(50, self.hide_to_tray)

        if config.list_models() and self.cfg.get("preload_on_start", True):
            self.after(200, self._preload_model)

        # Тихая проверка обновлений — чтобы кнопка «Обновить» появлялась
        # только когда обновление реально есть.
        if config.list_models():
            self.after(2500, lambda: self._check_updates(silent=True))

        if not config.list_models() and not self.cfg.get("start_minimized"):
            # Первый запуск без моделей — сразу предложим настроить и скачать.
            self.after(350, self._show_setup)

    # --------------------------------------------------------------- окно

    def _center_window(self) -> None:
        width, height = 460, 620
        self.minsize(420, 520)
        self.update_idletasks()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    # -------------------------------------------------------- иконка и трей

    def _setup_window_icon(self) -> None:
        ico = config.resource_path("assets/icon.ico")
        png = config.resource_path("assets/icon.png")
        try:
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:  # noqa: BLE001
            pass
        try:
            if png.exists():
                self._icon_photo = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_photo)
        except Exception:  # noqa: BLE001
            pass

    def _device_mode(self) -> str:
        """Выбранный режим устройства: auto | cuda | cpu (из переключателя)."""
        try:
            return DEVICE_VALUES.get(self.device_var.get(), "auto")
        except Exception:  # noqa: BLE001
            return "auto"

    def _set_device_mode(self, value: str) -> None:
        mode = value if value in DEVICE_LABELS else "auto"
        try:
            self.device_var.set(DEVICE_LABELS[mode])
        except Exception:  # noqa: BLE001
            pass

    def _tray_title(self) -> str:
        device = {"auto": "авто", "cuda": "GPU", "cpu": "CPU"}.get(
            self._device_mode(), self._device_mode()
        )
        state = "вкл" if self._listening else "пауза"
        return f"VoiceTexter — {device}, {state}"

    def _tray_state(self) -> dict:
        """Текущие настройки для галочек в меню трея (только главный поток)."""
        return {
            "listening": self._listening,
            "device": self._device_mode(),
            "compute_type": self.compute_combo.get(),
            "model": self._model_name,
            "language": self._lang,
            "auto_paste": self.auto_paste_var.get(),
            "vad": self._vad,
        }

    def _update_tray_state(self) -> None:
        if self._tray is None:
            return
        self._tray.update_state(**self._tray_state())
        self._tray.set_title(self._tray_title())

    def _setup_tray(self) -> None:
        """Создать значок в трее (если установлены pystray и Pillow)."""
        if not tray_available():
            self._log("Трей недоступен (нет pystray/Pillow).")
            return
        image = load_image(config.resource_path("assets/icon.png"))
        if image is None:
            self._log("Не удалось загрузить значок для трея.")
            return
        try:
            self._tray = TrayController(
                image,
                on_show=lambda: self._post(("tray_show",)),
                on_toggle_listening=lambda: self._post(("tray_toggle",)),
                on_quit=lambda: self._post(("tray_quit",)),
                on_set=lambda name, value: self._post(("tray_set", name, value)),
                on_reload=lambda: self._post(("tray_reload",)),
                on_failed=lambda: self._post(("tray_failed",)),
                on_download=lambda size: self._post(("tray_download", size)),
                devices=["auto", "cuda", "cpu"],
                computes=["auto", "float16", "int8_float16", "int8", "float32"],
                models=self._model_values(),
                downloads=self._download_entries(),
                languages=["ru", "en", "auto"],
                state=self._tray_state(),
                title=self._tray_title(),
            )
            self._tray.start()
        except Exception as exc:  # noqa: BLE001
            self._tray = None
            self._log(f"Трей недоступен: {exc}")

    def hide_to_tray(self) -> None:
        """Спрятать окно (и значок на панели задач), оставив приложение жить."""
        if self._tray is None:
            self._on_close()
            return
        self.withdraw()
        self._log("Свёрнуто в трей — приложение продолжает слушать микрофон.")

    def show_window(self) -> None:
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------- быстрые настройки в трее

    def _apply_tray_setting(self, name: str, value) -> None:
        """Применить быструю настройку из меню трея (главный поток)."""
        try:
            if name == "device":
                self._set_device_mode(str(value))
            elif name == "compute_type":
                self.compute_combo.set(str(value))
            elif name == "model":
                self._model_name = str(value)
                self._refresh_model_rows_state()
            elif name == "language":
                self.lang_combo.set(str(value))
            elif name == "auto_paste":
                self.auto_paste_var.set(bool(value))
            elif name == "vad":
                self.vad_var.set(bool(value))
            else:
                return
        except Exception as exc:  # noqa: BLE001
            self._log(f"Не удалось применить настройку из трея: {exc}")
            return

        if name in ("device", "compute_type", "model"):
            # Смена устройства/модели из трея всегда перезагружает модель,
            # даже если автозагрузка при старте отключена.
            self._reload_model()
        else:
            self._on_setting_change()

    def _reload_model(self) -> None:
        """Перезагрузить модель с текущими настройками (устройство/точность)."""
        self._busy = False
        self._sync_cached_settings()
        self._set_status("busy", "Загрузка модели…")
        self._preload_model()

    # ----------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        self.configure(fg_color=PALETTE["bg"])
        self.f_title = ctk.CTkFont(family=FONT_FAMILY, size=16, weight="bold")
        self.f_status = ctk.CTkFont(family=FONT_FAMILY, size=23, weight="bold")
        self.f_body = ctk.CTkFont(family=FONT_FAMILY, size=13)
        self.f_small = ctk.CTkFont(family=FONT_FAMILY, size=12)
        self.f_section = ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold")
        self.f_chip = ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold")
        self.f_label = ctk.CTkFont(family=FONT_FAMILY, size=12)

        # Общая галочка «через прокси» — показывается и в «Моделях», и в «Настройках».
        self.models_proxy_var = ctk.BooleanVar(value=False)
        self.app_proxy_var = ctk.BooleanVar(value=False)

        root = ctk.CTkScrollableFrame(
            self,
            fg_color=PALETTE["bg"],
            scrollbar_button_color=PALETTE["border"],
            scrollbar_button_hover_color=PALETTE["faint"],
            corner_radius=0,
        )
        root.pack(fill="both", expand=True, padx=10, pady=10)
        root.grid_columnconfigure(0, weight=1)

        # --- Заголовок ---
        head = ctk.CTkFrame(root, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            head, text="VoiceTexter", font=self.f_title, text_color=PALETTE["text"]
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            head, text="push-to-talk", font=self.f_small, text_color=PALETTE["faint"]
        ).grid(row=0, column=1, sticky="e")

        # --- Карточка статуса ---
        card = ctk.CTkFrame(root, fg_color=PALETTE["panel"], corner_radius=18)
        card.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        card.grid_columnconfigure(0, weight=1)

        self.orb = tk.Canvas(
            card,
            width=132,
            height=132,
            bg=PALETTE["panel"],
            highlightthickness=0,
        )
        self.orb.grid(row=0, column=0, pady=(12, 2))
        self._orb_ring = self.orb.create_oval(
            20, 20, 112, 112, outline=PALETTE["border"], width=3
        )
        self._orb_inner = self.orb.create_oval(
            44, 44, 88, 88, fill=PALETTE["panel_2"], outline=""
        )
        self._orb_arc = self.orb.create_arc(
            20,
            20,
            112,
            112,
            start=0,
            extent=78,
            style="arc",
            outline=PALETTE["busy"],
            width=3,
            state="hidden",
        )

        self.status_label = ctk.CTkLabel(
            card, text="Готов", font=self.f_status, text_color=PALETTE["text"]
        )
        self.status_label.grid(row=1, column=0, padx=24)
        self.hint_label = ctk.CTkLabel(
            card,
            text="",
            font=self.f_small,
            text_color=PALETTE["muted"],
            wraplength=360,
            justify="center",
        )
        self.hint_label.grid(row=2, column=0, padx=24, pady=(2, 12))

        self.download_card_btn = ctk.CTkButton(
            card,
            text="⬇   Скачать модель",
            font=self.f_body,
            height=40,
            corner_radius=12,
            fg_color="transparent",
            hover_color=PALETTE["panel_2"],
            border_color=PALETTE["accent"],
            border_width=1,
            text_color=PALETTE["accent"],
            command=self._show_setup,
        )
        self.download_card_btn.grid(row=3, column=0, padx=24, pady=(0, 14), sticky="ew")

        # --- Хоткей ---
        hk = ctk.CTkFrame(root, fg_color="transparent")
        hk.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        hk.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hk,
            text="ГОРЯЧАЯ КЛАВИША",
            font=self.f_small,
            text_color=PALETTE["faint"],
        ).grid(row=0, column=0, pady=(0, 6))
        self.bind_label = ctk.CTkButton(
            hk,
            text="—",
            font=self.f_chip,
            height=46,
            corner_radius=12,
            fg_color=PALETTE["panel_2"],
            hover_color=PALETTE["border"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["text"],
            command=self._start_capture,
        )
        self.bind_label.grid(row=1, column=0, sticky="ew")
        self.capture_banner = ctk.CTkLabel(
            hk,
            text="Нажмите клавишу (можно с модификаторами) или кнопку мыши…",
            font=self.f_small,
            text_color=PALETTE["busy"],
        )
        self.capture_banner.grid(row=2, column=0, pady=(6, 0))
        self.capture_banner.grid_remove()

        # --- Основной переключатель ---
        self.toggle_btn = ctk.CTkButton(
            root,
            text="⏸   Прослушивание включено",
            font=self.f_body,
            height=44,
            corner_radius=12,
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            text_color="#0d0f14",
            command=self._toggle_listening,
        )
        self.toggle_btn.grid(row=3, column=0, sticky="ew", pady=(14, 0))

        # --- Обновление программы (сворачиваемое) ---
        upd_wrap, upd = self._make_section(root, "Обновление программы")
        upd_wrap.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self._build_update_section(upd)

        # --- Модели (сворачиваемые) ---
        models_wrap, models = self._make_section(root, "Модели", opened=True)
        models_wrap.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self._build_models_section(models)

        # --- Настройки (сворачиваемые) ---
        settings_wrap, settings = self._make_section(root, "Настройки")
        settings_wrap.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        self._build_settings(settings)

        # --- Журнал (сворачиваемый) ---
        log_wrap, log = self._make_section(root, "Журнал")
        log_wrap.grid(row=7, column=0, sticky="new", pady=(6, 0))
        self.log_box = ctk.CTkTextbox(
            log,
            height=140,
            fg_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["muted"],
            corner_radius=10,
            scrollbar_button_color=PALETTE["border"],
            scrollbar_button_hover_color=PALETTE["faint"],
            font=self.f_small,
        )
        self.log_box.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.log_box.configure(state="disabled")

    def _build_settings(self, parent) -> None:
        row = 0

        def label(text: str) -> None:
            nonlocal row
            ctk.CTkLabel(
                parent, text=text, font=self.f_label, text_color=PALETTE["muted"]
            ).grid(row=row, column=0, padx=(4, 10), pady=6, sticky="w")

        def combo(values, command, master=None):
            return ctk.CTkComboBox(
                master if master is not None else parent,
                values=values,
                command=command,
                fg_color=PALETTE["panel_2"],
                border_color=PALETTE["border"],
                border_width=1,
                button_color=PALETTE["panel_2"],
                button_hover_color=PALETTE["border"],
                dropdown_fg_color=PALETTE["panel_2"],
                dropdown_hover_color=PALETTE["border"],
                text_color=PALETTE["text"],
                dropdown_text_color=PALETTE["text"],
                corner_radius=9,
                height=34,
                font=self.f_body,
            )

        label("Устройство")
        self.device_var = ctk.StringVar(value=DEVICE_LABELS["auto"])
        ctk.CTkSegmentedButton(
            parent,
            values=list(DEVICE_LABELS.values()),
            variable=self.device_var,
            command=lambda _v: self._on_model_change(),
            font=self.f_body,
            fg_color=PALETTE["panel_2"],
            selected_color=PALETTE["accent"],
            selected_hover_color=PALETTE["accent_hover"],
            unselected_color=PALETTE["panel_2"],
            unselected_hover_color=PALETTE["border"],
            text_color=PALETTE["text"],
            corner_radius=9,
            height=34,
        ).grid(row=row, column=1, pady=6, sticky="ew")
        row += 1

        label("Точность")
        self.compute_combo = combo(
            ["auto", "float16", "int8_float16", "int8", "float32"],
            self._on_model_change,
        )
        self.compute_combo.grid(row=row, column=1, pady=6, sticky="ew")
        row += 1

        label("Язык")
        self.lang_combo = combo(["ru", "en", "auto"], self._on_setting_change)
        self.lang_combo.grid(row=row, column=1, pady=6, sticky="ew")
        row += 1

        label("Поиск (beam)")
        self.beam_combo = combo(["1", "3", "5"], self._on_setting_change)
        self.beam_combo.grid(row=row, column=1, pady=6, sticky="ew")
        row += 1

        # Окно энкодера. Уменьшение (< 30 с) даёт ускорение, но Whisper обучен
        # на 30-секундных окнах и на реальной речи начинает галлюцинировать
        # (дописывать лишние слова) — поэтому по умолчанию 30 (штатно).
        label("Окно энкодера (с)")
        self.padwin_combo = combo(["15", "20", "25", "30"], self._on_setting_change)
        self.padwin_combo.grid(row=row, column=1, pady=6, sticky="ew")
        row += 1

        label("Вставка")
        self.paste_combo = combo(["Ctrl+V", "Shift+Insert"], self._on_setting_change)
        self.paste_combo.grid(row=row, column=1, pady=6, sticky="ew")
        row += 1

        label("Микрофон")
        mic_frame = ctk.CTkFrame(parent, fg_color="transparent")
        mic_frame.grid(row=row, column=1, pady=6, sticky="ew")
        mic_frame.grid_columnconfigure(0, weight=1)
        self.mic_combo = combo(
            self._device_values(), self._on_setting_change, master=mic_frame
        )
        self.mic_combo.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            mic_frame,
            text="⟳",
            width=34,
            height=34,
            font=self.f_body,
            fg_color=PALETTE["panel_2"],
            hover_color=PALETTE["border"],
            text_color=PALETTE["muted"],
            corner_radius=9,
            command=self._refresh_devices,
        ).grid(row=0, column=1, padx=(6, 0))
        row += 1

        ctk.CTkButton(
            parent,
            text="Перезагрузить модель",
            font=self.f_body,
            height=36,
            fg_color="transparent",
            hover_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["text"],
            corner_radius=9,
            command=self._preload_model,
        ).grid(row=row, column=0, columnspan=2, pady=(10, 4), sticky="ew")
        row += 1

        ctk.CTkFrame(parent, height=1, fg_color=PALETTE["border"]).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=10
        )
        row += 1

        def check(text: str, var: ctk.BooleanVar) -> None:
            nonlocal row
            ctk.CTkCheckBox(
                parent,
                text=text,
                variable=var,
                command=self._on_setting_change,
                font=self.f_body,
                text_color=PALETTE["text"],
                fg_color=PALETTE["accent"],
                hover_color=PALETTE["accent_hover"],
                border_color=PALETTE["border"],
                checkmark_color="#0d0f14",
                corner_radius=5,
            ).grid(row=row, column=0, columnspan=2, padx=4, pady=5, sticky="w")
            row += 1

        self.auto_paste_var = ctk.BooleanVar(value=True)
        check("Вставлять текст автоматически (Ctrl+V)", self.auto_paste_var)
        self.restore_var = ctk.BooleanVar(value=True)
        check("Возвращать прежнее содержимое буфера", self.restore_var)
        self.space_var = ctk.BooleanVar(value=True)
        check("Добавлять пробел в конце", self.space_var)
        self.vad_var = ctk.BooleanVar(value=True)
        check("Отсекать тишину (VAD)", self.vad_var)

        self.tray_var = ctk.BooleanVar(value=True)
        check("Сворачивать в трей при закрытии окна", self.tray_var)
        self.start_min_var = ctk.BooleanVar(value=False)
        check("Запускать сразу свёрнутым в трей", self.start_min_var)

        ctk.CTkFrame(parent, height=1, fg_color=PALETTE["border"]).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=10
        )
        row += 1

        ctk.CTkLabel(
            parent,
            text="Прокси (URL) — включается галочками в разделах «Модели» и «Обновление программы»",
            font=self.f_small,
            text_color=PALETTE["faint"],
        ).grid(row=row, column=0, columnspan=2, padx=4, pady=(10, 4), sticky="w")
        row += 1

        self.proxy_entry = ctk.CTkEntry(
            parent,
            placeholder_text="http://host:port или socks5://user:pass@host:port",
            fg_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["text"],
            placeholder_text_color=PALETTE["faint"],
            corner_radius=9,
            height=34,
            font=self.f_body,
        )
        self.proxy_entry.grid(row=row, column=0, columnspan=2, padx=2, sticky="ew")
        row += 1

        ctk.CTkLabel(
            parent,
            text="Начальный промпт (для смешанной ru+en речи)",
            font=self.f_small,
            text_color=PALETTE["faint"],
        ).grid(row=row, column=0, columnspan=2, padx=4, pady=(12, 4), sticky="w")
        row += 1
        self.prompt_box = ctk.CTkTextbox(
            parent,
            height=54,
            fg_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["text"],
            corner_radius=10,
            font=self.f_body,
        )
        self.prompt_box.grid(row=row, column=0, columnspan=2, padx=2, sticky="ew")
        row += 1

        ctk.CTkLabel(
            parent,
            text="Проверка — кликните и продиктуйте",
            font=self.f_small,
            text_color=PALETTE["faint"],
        ).grid(row=row, column=0, columnspan=2, padx=4, pady=(12, 4), sticky="w")
        row += 1
        self.test_entry = ctk.CTkTextbox(
            parent,
            height=56,
            fg_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["text"],
            corner_radius=10,
            font=self.f_body,
        )
        self.test_entry.grid(row=row, column=0, columnspan=2, padx=2, sticky="ew")

    # -------------------------------------------------- обновление программы

    def _build_update_section(self, parent) -> None:
        self._app_update: dict | None = None
        self._app_updating = False

        ctk.CTkLabel(
            parent,
            text=f"Установлена версия {updater.app_version()}",
            font=self.f_body,
            text_color=PALETTE["text"],
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, padx=4, sticky="w")

        self.app_update_status = ctk.CTkLabel(
            parent,
            text="",
            font=self.f_small,
            text_color=PALETTE["muted"],
            anchor="w",
            wraplength=380,
            justify="left",
        )
        self.app_update_status.grid(
            row=1, column=0, columnspan=2, padx=4, pady=(2, 0), sticky="ew"
        )

        self.app_update_bar = ctk.CTkProgressBar(
            parent,
            height=8,
            corner_radius=4,
            fg_color=PALETTE["panel_2"],
            progress_color=PALETTE["accent"],
        )
        self.app_update_bar.set(0)
        self.app_update_bar.grid(
            row=2, column=0, columnspan=2, padx=4, pady=(6, 0), sticky="ew"
        )
        self.app_update_bar.grid_remove()

        self.app_check_btn = ctk.CTkButton(
            parent,
            text="⟳   Проверить обновления",
            font=self.f_body,
            height=36,
            corner_radius=9,
            fg_color="transparent",
            hover_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["text"],
            command=self._check_app_update,
        )
        self.app_check_btn.grid(
            row=3, column=0, columnspan=2, padx=4, pady=(8, 0), sticky="ew"
        )

        self.app_install_btn = ctk.CTkButton(
            parent,
            text="⬇   Скачать и установить",
            font=self.f_body,
            height=38,
            corner_radius=10,
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            text_color="#0d0f14",
            command=self._install_app_update,
        )
        self.app_install_btn.grid(
            row=4, column=0, columnspan=2, padx=4, pady=(6, 0), sticky="ew"
        )
        self.app_install_btn.grid_remove()

        ctk.CTkCheckBox(
            parent,
            text="Скачивать обновление программы через прокси",
            variable=self.app_proxy_var,
            command=self._on_setting_change,
            font=self.f_small,
            text_color=PALETTE["text"],
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            border_color=PALETTE["border"],
            checkmark_color="#0d0f14",
            corner_radius=5,
        ).grid(row=5, column=0, columnspan=2, padx=4, pady=(10, 0), sticky="w")

    def _check_app_update(self) -> None:
        if self._app_updating:
            return
        self._app_updating = True
        self._app_update = None
        self.app_install_btn.grid_remove()
        self.app_check_btn.configure(state="disabled")
        self.app_update_status.configure(
            text="Проверяю обновления на GitHub…", text_color=PALETTE["busy"]
        )
        proxy = self._app_proxy()
        threading.Thread(
            target=self._app_check_worker, args=(proxy,), daemon=True
        ).start()

    def _app_check_worker(self, proxy: str) -> None:
        try:
            info = updater.fetch_latest(proxy_url=proxy)
        except Exception as exc:  # noqa: BLE001
            self._post(("app_update_error", f"{type(exc).__name__}: {exc}"))
            return
        self._post(("app_update_checked", info))

    def _install_app_update(self) -> None:
        info = self._app_update
        if info is None or self._app_updating:
            return
        if not updater.can_self_update():
            self._log(
                "Автоустановка доступна только для собранной версии (EXE). "
                "Скачайте архив со страницы релизов GitHub."
            )
            return
        self._app_updating = True
        self.app_check_btn.configure(state="disabled")
        self.app_install_btn.configure(state="disabled")
        self.app_update_status.configure(
            text="Скачиваю обновление…", text_color=PALETTE["busy"]
        )
        self.app_update_bar.set(0)
        self.app_update_bar.grid()
        proxy = self._app_proxy()
        threading.Thread(
            target=self._app_install_worker, args=(info, proxy), daemon=True
        ).start()

    def _app_install_worker(self, info: dict, proxy: str) -> None:
        try:
            app_dir = Path(sys.executable).resolve().parent
            work = app_dir / "update"
            work.mkdir(parents=True, exist_ok=True)
            zip_path = work / (info.get("asset_name") or "VoiceTexter-update.zip")

            def on_progress(done: int, total: int | None) -> None:
                pct = int(done * 100 / total) if total else None
                self._post(("app_update_progress", pct))

            updater.download_asset(
                proxy,
                info["asset_url"],
                zip_path,
                digest=info.get("digest"),
                on_progress=on_progress,
            )
            src = updater.extract_release(zip_path, work)
            bat = updater.write_updater(work, src, app_dir, os.getpid())
            self._post(("app_update_ready", str(bat)))
        except Exception as exc:  # noqa: BLE001
            self._post(("app_update_error", f"{type(exc).__name__}: {exc}"))

    def _on_app_update_checked(self, info: dict) -> None:
        self._app_updating = False
        try:
            self.app_check_btn.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass
        current = updater.app_version()
        latest = str(info.get("version") or "")
        if latest and updater.is_newer(latest, current) and info.get("asset_url"):
            self._app_update = info
            self.app_update_status.configure(
                text=f"Доступна версия {latest} (у вас {current}).",
                text_color=PALETTE["busy"],
            )
            if updater.can_self_update():
                self.app_install_btn.grid()
            else:
                self.app_install_btn.grid_remove()
                self._log(
                    "Автоустановка доступна только для EXE. Страница релиза: "
                    + str(info.get("url") or "")
                )
        else:
            self._app_update = None
            self.app_install_btn.grid_remove()
            self.app_update_status.configure(
                text=f"У вас последняя версия ({current}).",
                text_color=PALETTE["ok"],
            )
        self._log(f"Проверка обновлений: последняя версия {latest or '?'}.")

    def _on_app_update_ready(self, bat_path: str) -> None:
        try:
            updater.launch_updater(Path(bat_path))
        except Exception as exc:  # noqa: BLE001
            self._on_app_update_error(f"{type(exc).__name__}: {exc}")
            return
        self.app_update_status.configure(
            text="Обновление загружено — приложение перезапустится.",
            text_color=PALETTE["ok"],
        )
        self._log("Устанавливаю обновление и перезапускаюсь…")
        self._force_quit = True
        # Гарантированно завершаем процесс, иначе .bat будет вечно ждать PID.
        self.after(400, self._quit_for_update)

    def _quit_for_update(self) -> None:
        """Жёстко выйти, чтобы установщик смог подменить файлы и перезапустить."""
        try:
            self._on_close()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    def _on_app_update_error(self, message: str) -> None:
        self._app_updating = False
        try:
            self.app_update_bar.grid_remove()
            self.app_check_btn.configure(state="normal")
            self.app_install_btn.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass
        self.app_update_status.configure(
            text=f"Не удалось проверить/скачать: {message}",
            text_color=PALETTE["record"],
        )
        self._log(f"Ошибка обновления программы: {message}")

    # ------------------------------------------------------------ модели

    def _build_models_section(self, parent) -> None:
        """Список всех моделей со статусом и кнопкой, общие действия."""
        self.models_rows: dict[str, dict] = {}

        self.models_list = ctk.CTkFrame(parent, fg_color="transparent")
        self.models_list.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.models_list.grid_columnconfigure(0, weight=1)

        self.download_status = ctk.CTkLabel(
            parent, text="", font=self.f_small, text_color=PALETTE["faint"], anchor="w"
        )
        self.download_status.grid(
            row=1, column=0, columnspan=2, padx=4, pady=(8, 0), sticky="ew"
        )

        self.download_bar = ctk.CTkProgressBar(
            parent,
            height=8,
            corner_radius=4,
            fg_color=PALETTE["panel_2"],
            progress_color=PALETTE["accent"],
        )
        self.download_bar.set(0)
        self.download_bar.grid(
            row=2, column=0, columnspan=2, padx=4, pady=(2, 6), sticky="ew"
        )
        self.download_bar.grid_remove()

        ctk.CTkCheckBox(
            parent,
            text="Скачивать модели через прокси",
            variable=self.models_proxy_var,
            command=self._on_setting_change,
            font=self.f_small,
            text_color=PALETTE["text"],
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            border_color=PALETTE["border"],
            checkmark_color="#0d0f14",
            corner_radius=5,
        ).grid(row=3, column=0, columnspan=2, padx=4, pady=(8, 0), sticky="w")

        ctk.CTkLabel(
            parent,
            text="Клик по скачанной модели — сделать её основной.",
            font=self.f_small,
            text_color=PALETTE["faint"],
            anchor="w",
        ).grid(row=4, column=0, columnspan=2, padx=4, pady=(8, 0), sticky="ew")

        self._refresh_models_panel()

    def _model_specs(self) -> list[tuple[str, str | None]]:
        """[(папка, size|None)] — стандартные модели + нестандартные локальные папки."""
        specs = [(modelstore.folder_name(s), s) for s in modelstore.known_sizes()]
        known = {folder for folder, _ in specs}
        for name in config.list_models():
            if name not in known:
                specs.append((name, None))
        return specs

    def _refresh_models_panel(self) -> None:
        if not hasattr(self, "models_list"):
            return
        for child in self.models_list.winfo_children():
            child.destroy()
        self.models_rows = {}
        for i, (folder, size) in enumerate(self._model_specs()):
            self._make_model_row(i, folder, size)
        self._update_primary_action()

    def _update_primary_action(self) -> None:
        """Кнопка «Скачать модель» нужна только когда моделей нет вовсе."""
        if not hasattr(self, "download_card_btn"):
            return
        try:
            if config.list_models():
                self.download_card_btn.grid_remove()
            else:
                self.download_card_btn.grid()
        except Exception:  # noqa: BLE001
            pass

    def _make_model_row(self, index: int, folder: str, size: str | None) -> None:
        row = ctk.CTkFrame(
            self.models_list,
            fg_color=PALETTE["panel_2"],
            corner_radius=9,
            border_width=1,
            border_color=PALETTE["panel_2"],
        )
        row.grid(row=index, column=0, sticky="ew", pady=3)
        row.grid_columnconfigure(0, weight=1)

        approx = f"  ·  {modelstore.approx_size(size)}" if size else ""
        name = ctk.CTkLabel(
            row,
            text=f"{folder}{approx}",
            font=self.f_body,
            text_color=PALETTE["text"],
            anchor="w",
        )
        name.grid(row=0, column=0, sticky="w", padx=(10, 6), pady=7)

        status = ctk.CTkLabel(
            row, text="", font=self.f_small, text_color=PALETTE["muted"]
        )
        status.grid(row=0, column=1, padx=6)

        btn = ctk.CTkButton(
            row,
            text="Скачать",
            width=96,
            height=28,
            font=self.f_small,
            corner_radius=8,
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            text_color="#0d0f14",
            command=lambda f=folder: self._model_row_action(f),
        )
        btn.grid(row=0, column=2, padx=(6, 8), pady=6)

        self.models_rows[folder] = {
            "status": status,
            "btn": btn,
            "size": size,
            "row": row,
            "name": name,
        }
        # Клик по строке делает скачанную модель основной.
        for widget in (row, name, status):
            widget.bind("<Button-1>", lambda _e, f=folder: self._select_model(f))
        self._update_model_row(folder)

    def _select_model(self, folder: str) -> None:
        """Сделать скачанную модель основной (клик по её строке)."""
        if folder == self._model_name:
            return
        if folder not in config.list_models():
            return
        self._model_name = folder
        self._sync_cached_settings()
        self._refresh_model_rows_state()
        self._log(f"Основная модель: {folder}.")
        self._reload_model()

    def _refresh_model_rows_state(self) -> None:
        for folder in list(self.models_rows):
            self._update_model_row(folder)

    def _update_model_row(self, folder: str) -> None:
        info = self.models_rows.get(folder)
        if info is None:
            return
        size, status, btn, row, name = (
            info["size"],
            info["status"],
            info["btn"],
            info["row"],
            info["name"],
        )

        # Подсветка основной (выбранной) модели.
        is_main = folder == self._model_name and folder in config.list_models()
        try:
            row.configure(
                border_color=PALETTE["accent"] if is_main else PALETTE["panel_2"]
            )
            name.configure(text_color=PALETTE["accent"] if is_main else PALETTE["text"])
        except Exception:  # noqa: BLE001
            pass
        prefix = "● " if is_main else ""

        if size is None:
            # Нестандартная локальная папка — только выбрать основной.
            status.configure(text="локальная", text_color=PALETTE["muted"])
            btn.grid_remove()
            return

        installed = modelstore.is_installed(size, config.MODELS_DIR)
        if self._downloading and self._dl_kind == "check" and installed:
            status.configure(text="проверяется…", text_color=PALETTE["busy"])
            btn.grid_remove()
            return

        active_op = self._active_folder == folder
        if self._downloading and active_op and self._dl_kind in ("download", "update"):
            status.configure(
                text="скачивается…" if self._dl_kind == "download" else "обновляется…",
                text_color=PALETTE["busy"],
            )
            btn.configure(
                text="Отмена",
                state="normal",
                fg_color=PALETTE["panel_2"],
                hover_color=PALETTE["border"],
                border_width=0,
                text_color=PALETTE["text"],
            )
            btn.grid()
            return

        note = self._model_notes.get(folder)
        if installed:
            if note == "update":
                status.configure(text="доступно обновление", text_color=PALETTE["busy"])
            elif note == "actual":
                status.configure(text=f"{prefix}актуально", text_color=PALETTE["ok"])
            elif note == "error":
                status.configure(
                    text="проверка не удалась", text_color=PALETTE["muted"]
                )
            else:
                status.configure(
                    text=f"{prefix}скачано · {config.model_size_label(folder)}",
                    text_color=PALETTE["accent"] if is_main else PALETTE["ok"],
                )
            # Кнопка «Обновить» — только когда реально есть обновление.
            if note == "update":
                btn.configure(
                    text="Обновить",
                    state="normal",
                    fg_color=PALETTE["accent"],
                    hover_color=PALETTE["accent_hover"],
                    border_width=0,
                    text_color="#0d0f14",
                )
                btn.grid()
            else:
                btn.grid_remove()
        else:
            status.configure(text="не скачано", text_color=PALETTE["muted"])
            btn.configure(
                text="Скачать",
                state="normal",
                fg_color=PALETTE["accent"],
                hover_color=PALETTE["accent_hover"],
                border_width=0,
                text_color="#0d0f14",
            )
            btn.grid()

    def _model_row_action(self, folder: str) -> None:
        info = self.models_rows.get(folder)
        if info is None or info["size"] is None:
            return
        size = info["size"]
        if (
            self._downloading
            and self._active_folder == folder
            and self._dl_kind in ("download", "update")
        ):
            self._download_cancel.set()
            self._log("Отмена загрузки…")
            return
        if modelstore.is_installed(size, config.MODELS_DIR):
            self._start_update(size, folder)
        else:
            self._start_download(size)

    def _make_section(self, parent, title: str, opened: bool = False):
        """Сворачиваемая секция. Возвращает (обёртку, контент-фрейм)."""
        wrap = ctk.CTkFrame(parent, fg_color=PALETTE["panel"], corner_radius=14)
        wrap.grid_columnconfigure(0, weight=1)
        content = ctk.CTkFrame(wrap, fg_color="transparent")
        content.grid_columnconfigure(1, weight=1)

        state = {"open": bool(opened)}

        def render() -> None:
            if state["open"]:
                content.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
                header.configure(text=f"▾   {title}")
            else:
                content.grid_forget()
                header.configure(text=f"▸   {title}")

        def toggle() -> None:
            state["open"] = not state["open"]
            render()

        header = ctk.CTkButton(
            wrap,
            text=f"▸   {title}",
            anchor="w",
            font=self.f_section,
            height=42,
            corner_radius=12,
            fg_color="transparent",
            hover_color=PALETTE["panel_2"],
            text_color=PALETTE["text"],
            command=toggle,
        )
        header.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        render()
        return wrap, content

    # --------------------------------------------------- инициализация UI

    def _model_values(self) -> list[str]:
        return config.list_models() or ["нет моделей"]

    def _device_values(self) -> list[str]:
        self._devices = list_input_devices()
        return ["Системное по умолчанию"] + [
            f"{i}: {name}" for i, name in self._devices
        ]

    def _load_settings_into_ui(self) -> None:
        models = config.list_models()
        self._model_name = self._pick(models, self.cfg.get("model")) if models else ""
        self._set_device_mode(
            self._pick(["auto", "cuda", "cpu"], self.cfg.get("device"))
        )
        self.compute_combo.set(
            self._pick(
                ["auto", "float16", "int8_float16", "int8", "float32"],
                self.cfg.get("compute_type"),
            )
        )
        self.lang_combo.set(self._pick(["ru", "en", "auto"], self.cfg.get("language")))
        self.beam_combo.set(
            self._pick(["1", "3", "5"], str(self.cfg.get("beam_size", 1)))
        )
        self.padwin_combo.set(
            self._pick(
                ["15", "20", "25", "30"],
                str(
                    int(
                        self.cfg.get("pad_seconds", self.cfg.get("cpu_pad_seconds", 30))
                    )
                ),
            )
        )
        self.paste_combo.set(
            self._pick(
                ["Ctrl+V", "Shift+Insert"], self.cfg.get("paste_method", "Ctrl+V")
            )
        )
        self.prompt_box.delete("1.0", "end")
        self.prompt_box.insert("1.0", str(self.cfg.get("initial_prompt", "") or ""))
        self.mic_combo.set(self._current_device_label())
        self._set_binding_label(self.cfg.get("binding"))
        self.auto_paste_var.set(bool(self.cfg.get("auto_paste", True)))
        self.restore_var.set(bool(self.cfg.get("restore_clipboard", True)))
        self.space_var.set(bool(self.cfg.get("trailing_space", True)))
        self.vad_var.set(bool(self.cfg.get("vad_filter", True)))
        self.tray_var.set(bool(self.cfg.get("minimize_to_tray", True)))
        self.start_min_var.set(bool(self.cfg.get("start_minimized", False)))
        self.models_proxy_var.set(bool(self.cfg.get("proxy_models", False)))
        self.app_proxy_var.set(bool(self.cfg.get("proxy_app", False)))
        self.proxy_entry.delete(0, "end")
        self.proxy_entry.insert(0, str(self.cfg.get("proxy_url", "") or ""))

    @staticmethod
    def _pick(values, current):
        values = list(values or [])
        if current in values:
            return current
        return values[0] if values else ""

    def _current_device_label(self) -> str:
        pref = self.cfg.get("input_device")
        for i, name in self._devices:
            if pref == i or pref == name:
                return f"{i}: {name}"
        return "Системное по умолчанию"

    def _selected_device(self):
        label = self.mic_combo.get()
        if label.startswith("Системное"):
            return None
        try:
            return int(label.split(":", 1)[0])
        except ValueError:
            return None

    def _refresh_devices(self) -> None:
        self.mic_combo.configure(values=self._device_values())
        self.mic_combo.set("Системное по умолчанию")
        self._on_setting_change()

    def _sync_cached_settings(self) -> None:
        """Обновить простые кэши и сохранить конфиг (только из главного потока)."""
        model = self._model_name
        self._input_device = self._selected_device()
        self._lang = self.lang_combo.get()
        try:
            self._beam_size = int(self.beam_combo.get())
        except (ValueError, AttributeError):
            self._beam_size = int(self.cfg.get("beam_size", 1))
        self._initial_prompt = self.prompt_box.get("1.0", "end").strip()
        self._paste_method = self.paste_combo.get() or "Ctrl+V"
        self._vad = self.vad_var.get() if hasattr(self, "vad_var") else True
        try:
            self._pad_seconds = float(self.padwin_combo.get())
        except (ValueError, AttributeError):
            self._pad_seconds = float(
                self.cfg.get("pad_seconds", self.cfg.get("cpu_pad_seconds", 30))
            )
        # Применяем сразу — окно энкодера не требует перезагрузки модели.
        self.transcriber.pad_seconds = self._pad_seconds
        transcriber_configure(self._pad_seconds)
        self.cfg.pop("cpu_pad_seconds", None)  # старый ключ (только CPU)
        self.cfg.update(
            {
                "model": model,
                "device": self._device_mode(),
                "compute_type": self.compute_combo.get(),
                "language": self._lang,
                "input_device": self._input_device,
                "binding": self.hotkeys.binding,
                "auto_paste": self.auto_paste_var.get(),
                "restore_clipboard": self.restore_var.get(),
                "trailing_space": self.space_var.get(),
                "vad_filter": self.vad_var.get(),
                "beam_size": self._beam_size,
                "pad_seconds": self._pad_seconds,
                "initial_prompt": self._initial_prompt,
                "paste_method": self._paste_method,
                "minimize_to_tray": self.tray_var.get(),
                "start_minimized": self.start_min_var.get(),
                "proxy_models": self.models_proxy_var.get(),
                "proxy_app": self.app_proxy_var.get(),
                "proxy_url": self.proxy_entry.get().strip(),
            }
        )
        config.save_config(self.cfg)
        self._update_tray_state()

    def _on_setting_change(self, *_args) -> None:
        self._sync_cached_settings()

    # --------------------------------------------------- орб / состояние

    def _orb_draw(self, state: str) -> None:
        ring, inner = ORB.get(state, ORB["idle"])
        self.orb.itemconfig(self._orb_ring, outline=ring)
        if state == "busy":
            self.orb.itemconfig(self._orb_inner, state="hidden")
            self.orb.itemconfig(self._orb_arc, state="normal", outline=ring)
        else:
            self.orb.itemconfig(self._orb_inner, state="normal", fill=inner)
            self.orb.coords(self._orb_inner, 44, 44, 88, 88)
            self.orb.itemconfig(self._orb_arc, state="hidden")

    def _animate(self) -> None:
        if self._closing:
            return
        if self._state == "record":
            self._phase += 0.38
            r = 19 + 7 * (0.5 + 0.5 * math.sin(self._phase))
            c = 66
            self.orb.coords(self._orb_inner, c - r, c - r, c + r, c + r)
        elif self._state == "busy":
            self._angle = (self._angle + 14) % 360
            self.orb.itemconfig(self._orb_arc, start=self._angle)
        self.after(40, self._animate)

    def _set_status(self, state: str, text: str, sub: str | None = None) -> None:
        self._state = state
        self.status_label.configure(
            text=text, text_color=STATUS_COLOR.get(state, PALETTE["text"])
        )
        self.hint_label.configure(text=sub if sub is not None else HINTS.get(state, ""))
        self._orb_draw(state)

    # ------------------------------------------------------------- события

    def _post(self, event: tuple) -> None:
        self._events.put(event)

    def _drain_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                self._handle_event(self._events.get_nowait())
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "status":
            self._set_status(event[1], event[2])
        elif kind == "log":
            self._log(event[1])
        elif kind == "model_ready":
            self._loading = False
            info = (
                f"{self.transcriber.device.upper()} · {self.transcriber.compute_type}"
            )
            self._model_detail = f"{event[1]} · {info}"
            self._set_status("ready", "Готов", self._model_detail)
            self._log(f"Модель загружена: {event[1]} [{info}]")
        elif kind == "model_error":
            self._loading = False
            self._set_status("error", "Ошибка модели")
            self._log(f"Ошибка загрузки модели: {event[1]}")
        elif kind == "result":
            self._busy = False
            self._set_status("ready", "Готов", self._model_detail)
            text = format_transcript(event[1])
            if text:
                self._log(f"[{len(text)} симв.] {text}")
                if self.auto_paste_var.get():
                    self._paste(text)
            else:
                self._log("Речь не распознана.")
        elif kind == "result_error":
            self._busy = False
            self._set_status("error", "Ошибка распознавания")
            self._log(f"Ошибка распознавания: {event[1]}")
        elif kind == "download_progress":
            _kind, size, p = event
            self._show_download_progress(size, p)
        elif kind == "download_done":
            self._downloading = False
            self._dl_kind = ""
            self._active_folder = None
            self._download_cancel.clear()
            self._hide_download_bars()
            self._clear_download_status()
            self._set_status("ready", "Готов", self._model_detail)
            self._after_download(event[1])
        elif kind == "download_cancelled":
            self._finish_operation()
            self._log("Загрузка отменена.")
        elif kind == "download_error":
            self._finish_operation()
            self._set_status("error", "Ошибка загрузки")
            self._log(f"Не удалось скачать модель: {event[2]}")
        elif kind == "update_status":
            _kind, folder, note = event
            self._model_notes[folder] = note
            self._update_model_row(folder)
        elif kind == "check_done":
            silent = len(event) > 1 and bool(event[1])
            self._finish_operation(silent=silent)
            if not silent:
                pending = [f for f, n in self._model_notes.items() if n == "update"]
                if pending:
                    self._log(f"Есть обновления: {', '.join(pending)}.")
                else:
                    self._log("Все модели актуальны.")
        elif kind == "update_done":
            _kind, size, count = event
            folder = modelstore.folder_name(size)
            self._downloading = False
            self._dl_kind = ""
            self._active_folder = None
            self._download_cancel.clear()
            self._hide_download_bars()
            self._clear_download_status()
            self._model_notes.pop(folder, None)
            if count:
                self._log(f"Модель {folder} обновлена ({count} файл(ов)).")
            else:
                self._log(f"Модель {folder} уже актуальна.")
            self._refresh_models_panel()
            self._update_download_controls()
            self._set_status("ready", "Готов", self._model_detail)
            self._reload_model()
        elif kind == "update_cancelled":
            self._finish_operation()
            self._log("Обновление отменено.")
        elif kind == "update_error":
            self._finish_operation()
            self._set_status("error", "Ошибка обновления")
            self._log(f"Не удалось обновить модель: {event[2]}")
        elif kind == "app_update_checked":
            self._on_app_update_checked(event[1])
        elif kind == "app_update_progress":
            pct = event[1]
            if pct is not None:
                self.app_update_bar.set(pct / 100)
                self.app_update_status.configure(text=f"Скачиваю обновление… {pct}%")
        elif kind == "app_update_ready":
            self._on_app_update_ready(event[1])
        elif kind == "app_update_error":
            self._on_app_update_error(event[1])
        elif kind == "binding":
            self._apply_binding(event[1])
        elif kind == "tray_show":
            self.show_window()
        elif kind == "tray_toggle":
            self._toggle_listening()
        elif kind == "tray_set":
            self._apply_tray_setting(event[1], event[2])
        elif kind == "tray_reload":
            self._reload_model()
        elif kind == "tray_download":
            self._start_download(event[1])
        elif kind == "tray_quit":
            self._force_quit = True
            self._on_close()
        elif kind == "tray_failed":
            self._tray = None
            self.show_window()
            self._log("Значок в трее недоступен — окно показано снова.")

    # --------------------------------------------- запись (из потока hotkey)

    def _on_activate(self) -> None:
        """Вызывается из потока слушателя — только флаги/запись/очередь."""
        if not self._listening or self._capturing:
            return
        if self._busy:
            self._post(("status", "busy", "Распознавание…"))
            return
        if not self.transcriber.is_loaded(str(config.MODELS_DIR / self._model_name)):
            self._post(("status", "busy", "Модель ещё не готова…"))
            return
        if not self.recorder.start(device=self._input_device):
            self._post(
                ("log", f"Не удалось открыть микрофон: {self.recorder.last_error}")
            )
            self._post(("status", "error", "Микрофон недоступен"))
            return
        self._recording = True
        self._post(("status", "record", "Запись"))

    def _on_deactivate(self) -> None:
        """Вызывается из потока слушателя."""
        if not self._recording:
            return
        self._recording = False
        audio = self.recorder.stop()
        if audio is None or len(audio) < self.recorder.samplerate * MIN_AUDIO_SECONDS:
            self._post(("status", "ready", "Готов"))
            return
        self._busy = True
        self._post(("status", "busy", "Распознаю…"))
        threading.Thread(
            target=self._transcribe_worker,
            args=(audio, self._lang, self._beam_size, self._vad, self._initial_prompt),
            daemon=True,
        ).start()

    def _transcribe_worker(
        self, audio, lang: str, beam_size: int, vad: bool, initial_prompt: str
    ) -> None:
        try:
            text = self.transcriber.transcribe(
                audio,
                language=lang,
                beam_size=beam_size,
                vad_filter=vad,
                initial_prompt=initial_prompt,
            )
            self._post(("result", text))
        except Exception as exc:  # noqa: BLE001
            self._post(("result_error", f"{type(exc).__name__}: {exc}"))
            self._post(("log", traceback.format_exc(limit=2)))

    def _paste(self, text: str) -> None:
        try:
            paste_text(
                text,
                restore_clipboard=self.restore_var.get(),
                trailing_space=self.space_var.get(),
                method=self._paste_method,
            )
        except Exception as exc:  # noqa: BLE001
            self._post(("log", f"Ошибка вставки: {exc}"))

    # --------------------------------------------------------- модель

    def _on_model_change(self, *_args) -> None:
        self._sync_cached_settings()
        self._busy = False
        if not config.list_models():
            self._set_status(
                "idle", "Нет моделей", "Скачайте модель — кнопка ниже или в трее"
            )
            return
        self._set_status("busy", "Загрузка модели…")
        if self.cfg.get("preload_on_start", True):
            self._preload_model()

    def _preload_model(self) -> None:
        if self._loading:
            return
        if not config.list_models():
            self._set_status(
                "idle", "Нет моделей", "Скачайте модель — кнопка ниже или в трее"
            )
            return
        model = self._model_name
        path = config.MODELS_DIR / model
        if not path.is_dir():
            self._log(f"Папка модели не найдена: {path}")
            return
        self._loading = True
        device = self._device_mode()
        compute = self.compute_combo.get()
        self._set_status("busy", "Загрузка модели…", model)
        self._log(f"Загрузка {model} ({device}/{compute})…")

        def work() -> None:
            try:
                self.transcriber.load(str(path), device, compute)
                self._post(("model_ready", model))
            except Exception as exc:  # noqa: BLE001
                self._post(("model_error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    # --------------------------------------------------- загрузка моделей

    def _download_entries(self) -> list[tuple[str, str]]:
        """Пары (size, подпись) для моделей, которых ещё нет локально."""
        entries = []
        for size in modelstore.known_sizes():
            if modelstore.is_installed(size, config.MODELS_DIR):
                continue
            entries.append((size, f"{size} — {modelstore.approx_size(size)}"))
        return entries

    def _proxy_url(self) -> str:
        try:
            return self.proxy_entry.get().strip()
        except Exception:  # noqa: BLE001
            return ""

    def _models_proxy(self) -> str:
        """Прокси для загрузки/обновления моделей (галочка в разделе «Модели»)."""
        if not self.models_proxy_var.get():
            return ""
        return self._proxy_url()

    def _app_proxy(self) -> str:
        """Прокси для скачивания обновлений программы (галочка в «Обновлении программы»)."""
        if not self.app_proxy_var.get():
            return ""
        return self._proxy_url()

    def _start_download(self, size: str | None = None) -> None:
        """Запустить загрузку новой модели (size из трея/первичной настройки)."""
        if self._downloading:
            self._log("Загрузка уже идёт — дождитесь окончания.")
            return
        if size is None or size not in modelstore.KNOWN_MODELS:
            self._log("Выберите модель в разделе «Модели».")
            return
        if modelstore.is_installed(size, config.MODELS_DIR):
            self._log(f"Модель {size} уже скачана.")
            return

        proxy = self._models_proxy()
        self._downloading = True
        self._dl_kind = "download"
        self._active_folder = modelstore.folder_name(size)
        self._download_cancel.clear()
        self._update_download_controls()
        self._set_download_bar(0)
        self._set_status(
            "busy", "Загрузка модели…", f"{size} · {modelstore.approx_size(size)}"
        )
        suffix = f" через прокси {proxy}" if proxy else ""
        self._log(f"Скачиваю {size} ({modelstore.approx_size(size)}){suffix}…")
        threading.Thread(
            target=self._download_worker, args=(size, proxy), daemon=True
        ).start()

    def _start_update(self, size: str, folder: str) -> None:
        """Обновить установленную модель — качаются только изменённые файлы."""
        if self._downloading:
            self._log("Другая операция уже идёт — дождитесь окончания.")
            return
        proxy = self._models_proxy()
        self._downloading = True
        self._dl_kind = "update"
        self._active_folder = folder
        self._model_notes.pop(folder, None)
        self._download_cancel.clear()
        self._update_download_controls()
        self._set_download_bar(0)
        self._set_status("busy", "Обновление модели…", folder)
        self._log(f"Проверяю обновления {folder}…")
        threading.Thread(
            target=self._update_worker, args=(size, proxy), daemon=True
        ).start()

    def _check_updates(self, silent: bool = False) -> None:
        """Проверить обновления для всех скачанных стандартных моделей.

        silent=True (автопроверка при старте) не трогает статус и не шумит
        в журнале при ошибках.
        """
        if self._downloading:
            if not silent:
                self._log("Дождитесь окончания текущей операции.")
            return
        installed = [
            s
            for s in modelstore.known_sizes()
            if modelstore.is_installed(s, config.MODELS_DIR)
        ]
        if not installed:
            if not silent:
                self._log("Нет скачанных моделей — сначала скачайте модель.")
            return
        proxy = self._models_proxy()
        self._downloading = True
        self._dl_kind = "check"
        self._active_folder = None
        self._model_notes.clear()
        self._update_download_controls()
        if not silent:
            self._set_status("busy", "Проверка обновлений…")
            self._log("Проверяю обновления моделей…")
        threading.Thread(
            target=self._check_worker,
            args=(installed, proxy, silent),
            daemon=True,
        ).start()

    def _progress_reporter(self, size: str):
        last = {"percent": -1}

        def on_progress(p: dict) -> None:
            if p.get("percent") != last["percent"]:
                last["percent"] = p.get("percent")
                self._post(("download_progress", size, p))

        return on_progress

    # ----------------------------------- первичная настройка (в главном окне)

    def _show_setup(self) -> None:
        """Первичная настройка прямо в главном окне (без отдельного окна)."""
        if self._setup is not None:
            return

        overlay = ctk.CTkFrame(self, fg_color=PALETTE["bg"], corner_radius=0)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.grid_columnconfigure(0, weight=1)
        overlay.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(overlay, fg_color=PALETTE["panel"], corner_radius=18)
        card.grid(row=0, column=0, padx=14, pady=14, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)

        first_run = not config.list_models()
        title = "Добро пожаловать!" if first_run else "Модель и настройки"
        subtitle = (
            "Выберите язык, модель и устройство. Модель скачается сразу, "
            "и приложение будет готово к работе."
            if first_run
            else "Выберите модель для загрузки и параметры распознавания."
        )
        ctk.CTkLabel(
            card,
            text=title,
            font=self.f_title,
            text_color=PALETTE["text"],
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            card,
            text=subtitle,
            font=self.f_small,
            text_color=PALETTE["muted"],
            wraplength=380,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=16)

        ctk.CTkLabel(
            card,
            text="Язык распознавания",
            font=self.f_label,
            text_color=PALETTE["muted"],
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(16, 2))
        lang_var = ctk.StringVar(value=self.lang_combo.get() or "ru")
        ctk.CTkSegmentedButton(
            card,
            values=["ru", "en", "auto"],
            variable=lang_var,
            font=self.f_body,
            fg_color=PALETTE["panel_2"],
            selected_color=PALETTE["accent"],
            selected_hover_color=PALETTE["accent_hover"],
            unselected_color=PALETTE["panel_2"],
            unselected_hover_color=PALETTE["border"],
            text_color=PALETTE["text"],
            corner_radius=9,
            height=34,
        ).grid(row=3, column=0, sticky="ew", padx=16)

        ctk.CTkLabel(
            card,
            text="Модель распознавания",
            font=self.f_label,
            text_color=PALETTE["muted"],
        ).grid(row=4, column=0, sticky="w", padx=16, pady=(14, 2))
        sizes = list(modelstore.known_sizes())
        if not sizes:
            sizes = ["small"]
        labels = [f"{s} — {modelstore.approx_size(s)}" for s in sizes]
        default_size = "large-v3-turbo" if cuda_available() else "small"
        if default_size not in sizes:
            default_size = sizes[0]
        model_combo = ctk.CTkComboBox(
            card,
            values=labels,
            fg_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            button_color=PALETTE["panel_2"],
            button_hover_color=PALETTE["border"],
            dropdown_fg_color=PALETTE["panel_2"],
            dropdown_hover_color=PALETTE["border"],
            text_color=PALETTE["text"],
            dropdown_text_color=PALETTE["text"],
            corner_radius=9,
            height=34,
            font=self.f_body,
        )
        model_combo.set(f"{default_size} — {modelstore.approx_size(default_size)}")
        model_combo.grid(row=5, column=0, sticky="ew", padx=16)

        ctk.CTkLabel(
            card,
            text="Устройство (CPU / GPU)",
            font=self.f_label,
            text_color=PALETTE["muted"],
        ).grid(row=6, column=0, sticky="w", padx=16, pady=(14, 2))
        device_var = ctk.StringVar(value=DEVICE_LABELS[self._device_mode()])
        ctk.CTkSegmentedButton(
            card,
            values=list(DEVICE_LABELS.values()),
            variable=device_var,
            font=self.f_body,
            fg_color=PALETTE["panel_2"],
            selected_color=PALETTE["accent"],
            selected_hover_color=PALETTE["accent_hover"],
            unselected_color=PALETTE["panel_2"],
            unselected_hover_color=PALETTE["border"],
            text_color=PALETTE["text"],
            corner_radius=9,
            height=34,
        ).grid(row=7, column=0, sticky="ew", padx=16)

        proxy_row = ctk.CTkFrame(card, fg_color="transparent")
        proxy_row.grid(row=8, column=0, sticky="ew", padx=16, pady=(14, 0))
        proxy_row.grid_columnconfigure(0, weight=1)
        proxy_var = ctk.BooleanVar(
            value=bool(self.models_proxy_var.get() or self.app_proxy_var.get())
        )
        ctk.CTkCheckBox(
            proxy_row,
            text="Скачивать через прокси",
            variable=proxy_var,
            font=self.f_body,
            text_color=PALETTE["text"],
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            border_color=PALETTE["border"],
            checkmark_color="#0d0f14",
            corner_radius=5,
        ).grid(row=0, column=0, sticky="w")
        proxy_entry = ctk.CTkEntry(
            proxy_row,
            placeholder_text="socks5://host:port или http://host:port",
            fg_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["text"],
            placeholder_text_color=PALETTE["faint"],
            corner_radius=9,
            height=32,
            font=self.f_small,
        )
        proxy_entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        proxy_entry.insert(0, self._proxy_url())

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=9, column=0, sticky="ew", padx=16, pady=(18, 16))
        btns.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            btns,
            text="Пропустить",
            font=self.f_body,
            height=40,
            corner_radius=11,
            fg_color="transparent",
            hover_color=PALETTE["panel_2"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["muted"],
            command=self._hide_setup,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            btns,
            text="Начать",
            font=self.f_body,
            height=40,
            corner_radius=11,
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            text_color="#0d0f14",
            command=self._setup_confirm,
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self._setup = {
            "overlay": overlay,
            "lang": lang_var,
            "model": model_combo,
            "device": device_var,
            "proxy_var": proxy_var,
            "proxy_entry": proxy_entry,
        }

    def _setup_confirm(self) -> None:
        if self._setup is None:
            return
        s = self._setup
        lang = s["lang"].get()
        device = DEVICE_VALUES.get(s["device"].get(), "auto")
        size = s["model"].get().split("—", 1)[0].strip()

        self.lang_combo.set(lang)
        self._set_device_mode(device)
        proxy_on = bool(s["proxy_var"].get())
        self.models_proxy_var.set(proxy_on)
        self.app_proxy_var.set(proxy_on)
        self.proxy_entry.delete(0, "end")
        self.proxy_entry.insert(0, s["proxy_entry"].get().strip())
        self._sync_cached_settings()
        config.save_config(self.cfg)

        self._hide_setup()
        self._log(f"Настройка: язык {lang}, устройство {device}, модель {size}.")
        if size in modelstore.KNOWN_MODELS and not modelstore.is_installed(
            size, config.MODELS_DIR
        ):
            self._start_download(size)
        else:
            self._reload_model()

    def _hide_setup(self) -> None:
        if self._setup is None:
            return
        try:
            self._setup["overlay"].place_forget()
            self._setup["overlay"].destroy()
        except Exception:  # noqa: BLE001
            pass
        self._setup = None

    # --------------------------------------------- прогресс загрузки

    def _set_download_bar(self, pct) -> None:
        try:
            self.download_bar.grid()
            self.download_bar.set(0 if pct is None else pct / 100)
        except Exception:  # noqa: BLE001
            pass

    def _hide_download_bars(self) -> None:
        try:
            self.download_bar.grid_remove()
            self.download_bar.set(0)
        except Exception:  # noqa: BLE001
            pass

    def _clear_download_status(self) -> None:
        try:
            self.download_status.configure(text="")
        except Exception:  # noqa: BLE001
            pass

    def _show_download_progress(self, size: str, p: dict) -> None:
        title = (
            "Обновление модели…" if self._dl_kind == "update" else "Загрузка модели…"
        )
        pct = p.get("percent")
        if pct is None:
            text = f"{size}: {p.get('file_done', 0) // 1048576} МБ"
        else:
            text = f"{size}: {pct}%  ·  {p.get('file', '')}"
        self._set_status("busy", title, text)
        self._set_download_bar(pct)
        try:
            self.download_status.configure(text=text)
        except Exception:  # noqa: BLE001
            pass
        row = self.models_rows.get(modelstore.folder_name(size))
        if row is not None and pct is not None:
            try:
                row["status"].configure(text=f"{pct}%", text_color=PALETTE["busy"])
            except Exception:  # noqa: BLE001
                pass

    def _download_worker(self, size: str, proxy: str) -> None:
        try:
            modelstore.download(
                size,
                config.MODELS_DIR,
                proxy_url=proxy,
                on_progress=self._progress_reporter(size),
                should_stop=self._download_cancel.is_set,
            )
            self._post(("download_done", size))
        except modelstore.DownloadCancelled:
            self._post(("download_cancelled", size))
        except Exception as exc:  # noqa: BLE001
            self._post(("download_error", size, f"{type(exc).__name__}: {exc}"))

    def _update_worker(self, size: str, proxy: str) -> None:
        try:
            changed = modelstore.update(
                size,
                config.MODELS_DIR,
                proxy_url=proxy,
                on_progress=self._progress_reporter(size),
                should_stop=self._download_cancel.is_set,
            )
            self._post(("update_done", size, len(changed)))
        except modelstore.DownloadCancelled:
            self._post(("update_cancelled", size))
        except Exception as exc:  # noqa: BLE001
            self._post(("update_error", size, f"{type(exc).__name__}: {exc}"))

    def _check_worker(self, sizes: list[str], proxy: str, silent: bool = False) -> None:
        for size in sizes:
            if self._download_cancel.is_set():
                break
            folder = modelstore.folder_name(size)
            try:
                changed = modelstore.check_update(
                    size, config.MODELS_DIR, proxy_url=proxy
                )
                note = "update" if changed else "actual"
            except Exception as exc:  # noqa: BLE001
                if silent:
                    # Автопроверка при старте не должна шуметь, если сети нет.
                    continue
                note = "error"
                self._post(("log", f"Не удалось проверить {folder}: {exc}"))
            self._post(("update_status", folder, note))
        self._post(("check_done", silent))

    def _finish_operation(self, silent: bool = False) -> None:
        """Сбросить состояние загрузки/обновления/проверки и обновить панель."""
        self._downloading = False
        self._dl_kind = ""
        self._active_folder = None
        self._download_cancel.clear()
        self._hide_download_bars()
        self._clear_download_status()
        self._refresh_models_panel()
        self._update_download_controls()
        if not silent:
            self._set_status("ready", "Готов", self._model_detail)

    def _update_download_controls(self) -> None:
        for folder in list(self.models_rows):
            self._update_model_row(folder)

    def _after_download(self, size: str) -> None:
        name = modelstore.folder_name(size)
        self._log(f"Модель {size} скачана в models/{name}.")
        self._model_name = name
        self._model_notes[name] = "actual"
        self._sync_cached_settings()
        self._refresh_models_panel()
        if self._tray is not None:
            self._tray.update_models(self._model_values())
            self._tray.update_downloads(self._download_entries())
        self._update_download_controls()
        self._reload_model()

    # --------------------------------------------------------- биндинг

    def _set_binding_label(self, binding) -> None:
        self.bind_label.configure(text=binding_label(binding))

    def _start_capture(self) -> None:
        self._capturing = True
        self.bind_label.configure(text="Нажмите…", fg_color=PALETTE["panel_2"])
        self.capture_banner.grid()
        self.hotkeys.begin_capture(self._on_binding_captured)

    def _on_binding_captured(self, binding: dict) -> None:
        # вызывается из потока слушателя — вернёмся в главный поток
        self._post(("binding", binding))

    def _apply_binding(self, binding: dict) -> None:
        self._capturing = False
        self.cfg["binding"] = binding
        config.save_config(self.cfg)
        self.hotkeys.set_binding(binding)
        self._set_binding_label(binding)
        self.capture_banner.grid_remove()
        self._log(f"Новый бинд: {binding_label(binding)}")

    # --------------------------------------------------------- прочее

    def _toggle_listening(self) -> None:
        self._listening = not self._listening
        if not self._listening:
            self._recording = False
            self.recorder.abort()
            self.toggle_btn.configure(
                text="▶   Прослушивание выключено",
                fg_color=PALETTE["panel_2"],
                hover_color=PALETTE["border"],
                text_color=PALETTE["muted"],
            )
            self._set_status("idle", "Пауза")
        else:
            self.toggle_btn.configure(
                text="⏸   Прослушивание включено",
                fg_color=PALETTE["accent"],
                hover_color=PALETTE["accent_hover"],
                text_color="#0d0f14",
            )
            self._set_status("ready", "Готов", self._model_detail)
            self._log("Прослушивание включено.")
        self._update_tray_state()

    def _log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_close(self) -> None:
        """Крестик окна: свернуть в трей (по умолчанию) либо выйти."""
        if self._closing:
            return
        if (
            not self._force_quit
            and self.cfg.get("minimize_to_tray", True)
            and self._tray is not None
        ):
            self.hide_to_tray()
            return

        self._closing = True
        try:
            self._sync_cached_settings()
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._tray is not None:
                self._tray.stop()
                self._tray = None
            self.recorder.abort()
            self.hotkeys.stop()
            self.destroy()


def main() -> None:
    # Вторая копия не запускается: копии делят микрофон/хоткей/буфер и по
    # полной модели в памяти (лишний расход CPU и RAM). Разрешить несколько
    # копий можно через VOICETEXTER_ALLOW_MULTI=1.
    if instances.block_if_running():
        return
    app = VoiceTexterApp()
    app.mainloop()


if __name__ == "__main__":
    main()

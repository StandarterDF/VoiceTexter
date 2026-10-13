"""VoiceTexter — push-to-talk диктовка в любое активное поле.

Держите привязанную клавишу/кнопку мыши → идёт запись микрофона,
отпустили → локальная модель faster-whisper распознаёт речь и вставляет
текст в активное окно (Ctrl+V).

Запуск:  .\\.venv\\Scripts\\python.exe main.py
"""

from __future__ import annotations

import math
import queue
import threading
import tkinter as tk
import traceback

import customtkinter as ctk

import config
from hotkeys import HotkeyManager, binding_label
from paster import paste_text
from recorder import Recorder, list_input_devices
from transcriber import Transcriber, cuda_available

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


class VoiceTexterApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = config.load_config()
        self.title("VoiceTexter")

        # --- внутреннее состояние (читается и из потоков слушателей) ---
        self._events: queue.Queue = queue.Queue()
        self._recording = False
        self._busy = False
        self._loading = False
        self._listening = True
        self._capturing = False
        self._closing = False
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
        self._vad: bool = True
        self._devices: list[tuple[int, str]] = []

        self.recorder = Recorder(samplerate=int(self.cfg.get("samplerate", 16000)))
        self.transcriber = Transcriber()
        self.hotkeys = HotkeyManager(self._on_activate, self._on_deactivate)

        self._build_ui()
        self._center_window()
        self._load_settings_into_ui()
        # Биндинг ставим ДО сохранения конфига, иначе в config.json уйдёт null.
        self.hotkeys.set_binding(self.cfg.get("binding"))
        self._sync_cached_settings()
        self.hotkeys.start()

        self._set_status("idle", "Готов к работе", "Модель загружается…")
        self._log("VoiceTexter запущен.")
        self.after(80, self._drain_events)
        self.after(40, self._animate)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.cfg.get("preload_on_start", True):
            self.after(200, self._preload_model)

    # --------------------------------------------------------------- окно

    def _center_window(self) -> None:
        width, height = 460, 620
        self.minsize(420, 520)
        self.update_idletasks()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

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

        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=20, pady=14)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(5, weight=1)

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

        # --- Настройки (сворачиваемые) ---
        settings_wrap, settings = self._make_section(root, "Настройки")
        settings_wrap.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self._build_settings(settings)

        # --- Журнал (сворачиваемый) ---
        log_wrap, log = self._make_section(root, "Журнал")
        log_wrap.grid(row=5, column=0, sticky="new", pady=(6, 0))
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

        def combo(values, command):
            return ctk.CTkComboBox(
                parent,
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

        label("Модель")
        self.model_combo = combo(self._model_values(), self._on_model_change)
        self.model_combo.grid(row=row, column=1, pady=6, sticky="ew")
        row += 1

        label("Устройство")
        self.device_combo = combo(["auto", "cuda", "cpu"], self._on_model_change)
        self.device_combo.grid(row=row, column=1, pady=6, sticky="ew")
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

        label("Микрофон")
        mic_frame = ctk.CTkFrame(parent, fg_color="transparent")
        mic_frame.grid(row=row, column=1, pady=6, sticky="ew")
        mic_frame.grid_columnconfigure(0, weight=1)
        self.mic_combo = combo(self._device_values(), self._on_setting_change)
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

    def _make_section(self, parent, title: str):
        """Сворачиваемая секция. Возвращает (обёртку, контент-фрейм)."""
        wrap = ctk.CTkFrame(parent, fg_color=PALETTE["panel"], corner_radius=14)
        wrap.grid_columnconfigure(0, weight=1)
        content = ctk.CTkFrame(wrap, fg_color="transparent")
        content.grid_columnconfigure(1, weight=1)

        state = {"open": False}

        def toggle() -> None:
            state["open"] = not state["open"]
            if state["open"]:
                content.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
                header.configure(text=f"▾   {title}")
            else:
                content.grid_forget()
                header.configure(text=f"▸   {title}")

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
        self.model_combo.set(self._pick(self._model_values(), self.cfg.get("model")))
        self.device_combo.set(
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
        self.prompt_box.delete("1.0", "end")
        self.prompt_box.insert("1.0", str(self.cfg.get("initial_prompt", "") or ""))
        self.mic_combo.set(self._current_device_label())
        self._set_binding_label(self.cfg.get("binding"))
        self.auto_paste_var.set(bool(self.cfg.get("auto_paste", True)))
        self.restore_var.set(bool(self.cfg.get("restore_clipboard", True)))
        self.space_var.set(bool(self.cfg.get("trailing_space", True)))
        self.vad_var.set(bool(self.cfg.get("vad_filter", True)))

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
        model = self.model_combo.get()
        self._model_name = model
        self._input_device = self._selected_device()
        self._lang = self.lang_combo.get()
        try:
            self._beam_size = int(self.beam_combo.get())
        except (ValueError, AttributeError):
            self._beam_size = int(self.cfg.get("beam_size", 1))
        self._initial_prompt = self.prompt_box.get("1.0", "end").strip()
        self._vad = self.vad_var.get() if hasattr(self, "vad_var") else True
        self.cfg.update(
            {
                "model": model,
                "device": self.device_combo.get(),
                "compute_type": self.compute_combo.get(),
                "language": self._lang,
                "input_device": self._input_device,
                "binding": self.hotkeys.binding,
                "auto_paste": self.auto_paste_var.get(),
                "restore_clipboard": self.restore_var.get(),
                "trailing_space": self.space_var.get(),
                "vad_filter": self.vad_var.get(),
                "beam_size": self._beam_size,
                "initial_prompt": self._initial_prompt,
            }
        )
        config.save_config(self.cfg)

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
            text = event[1]
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
        elif kind == "binding":
            self._apply_binding(event[1])

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
            )
        except Exception as exc:  # noqa: BLE001
            self._post(("log", f"Ошибка вставки: {exc}"))

    # --------------------------------------------------------- модель

    def _on_model_change(self, *_args) -> None:
        self._sync_cached_settings()
        self._busy = False
        self._set_status("busy", "Загрузка модели…")
        if self.cfg.get("preload_on_start", True):
            self._preload_model()

    def _preload_model(self) -> None:
        if self._loading:
            return
        model = self.model_combo.get()
        path = config.MODELS_DIR / model
        if not path.is_dir():
            self._log(f"Папка модели не найдена: {path}")
            return
        self._loading = True
        device = self.device_combo.get()
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

    def _log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_close(self) -> None:
        self._closing = True
        try:
            self._sync_cached_settings()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.recorder.abort()
            self.hotkeys.stop()
            self.destroy()


def main() -> None:
    app = VoiceTexterApp()
    app.mainloop()


if __name__ == "__main__":
    main()

"""VoiceTexter — push-to-talk диктовка в любое активное поле.

Держите привязанную клавишу/кнопку мыши → идёт запись микрофона,
отпустили → локальная модель faster-whisper распознаёт речь и вставляет
текст в активное окно (Ctrl+V).

Запуск:  .\\.venv\\Scripts\\python.exe main.py
"""

from __future__ import annotations

import queue
import threading
import traceback

import customtkinter as ctk

import config
from hotkeys import HotkeyManager, binding_label
from paster import paste_text
from recorder import Recorder, list_input_devices
from transcriber import Transcriber, cuda_available

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

COLORS = {
    "ready": "#3ba55d",
    "record": "#e5484d",
    "busy": "#f5a623",
    "error": "#e5484d",
    "idle": "#8b8b8b",
}

MIN_AUDIO_SECONDS = 0.25


class VoiceTexterApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = config.load_config()

        self.title("VoiceTexter — диктовка в активное поле")
        self.geometry("640x760")
        self.minsize(560, 640)

        # --- внутреннее состояние (читается и из потоков слушателей) ---
        self._events: queue.Queue = queue.Queue()
        self._recording = False
        self._busy = False
        self._loading = False
        self._listening = True
        self._capturing = False
        self._closing = False
        # кэш настроек как «простые» значения (безопасно читать из потоков)
        self._model_name: str = ""
        self._input_device = None
        self._lang: str = "ru"
        self._beam_size: int = 1
        self._vad: bool = True
        self._devices: list[tuple[int, str]] = []

        self.recorder = Recorder(samplerate=int(self.cfg.get("samplerate", 16000)))
        self.transcriber = Transcriber()
        self.hotkeys = HotkeyManager(self._on_activate, self._on_deactivate)

        self._build_ui()
        self._load_settings_into_ui()
        # Биндинг ставим ДО сохранения конфига, иначе в config.json уйдёт null.
        self.hotkeys.set_binding(self.cfg.get("binding"))
        self._sync_cached_settings()
        self.hotkeys.start()

        self.after(80, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.cfg.get("preload_on_start", True):
            self.after(300, self._preload_model)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # --- Заголовок / статус ---
        header = ctk.CTkFrame(self, corner_radius=12)
        header.grid(row=0, column=0, padx=14, pady=(14, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            header,
            text="●  Готов",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLORS["idle"],
        )
        self.status_label.grid(row=0, column=0, padx=16, pady=(14, 2), sticky="w")

        self.hint_label = ctk.CTkLabel(
            header, text="", font=ctk.CTkFont(size=12), text_color="#9aa0a6"
        )
        self.hint_label.grid(row=1, column=0, padx=16, pady=(0, 12), sticky="w")

        # --- Параметры ---
        body = ctk.CTkScrollableFrame(self, corner_radius=12)
        body.grid(row=1, column=0, padx=14, pady=6, sticky="nsew")
        body.grid_columnconfigure(1, weight=1)

        row = 0

        def section(title: str) -> None:
            nonlocal row
            ctk.CTkLabel(
                body,
                text=title,
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#7aa2f7",
            ).grid(row=row, column=0, columnspan=2, padx=6, pady=(12, 4), sticky="w")
            row += 1

        def label(text: str) -> None:
            nonlocal row
            ctk.CTkLabel(body, text=text).grid(
                row=row, column=0, padx=6, pady=4, sticky="w"
            )

        section("Модель распознавания")
        label("Модель")
        self.model_combo = ctk.CTkComboBox(
            body, values=self._model_values(), command=self._on_model_change
        )
        self.model_combo.grid(row=row, column=1, padx=6, pady=4, sticky="ew")
        row += 1

        label("Устройство")
        self.device_combo = ctk.CTkComboBox(
            body, values=["auto", "cuda", "cpu"], command=self._on_model_change
        )
        self.device_combo.grid(row=row, column=1, padx=6, pady=4, sticky="ew")
        row += 1

        label("Точность")
        self.compute_combo = ctk.CTkComboBox(
            body,
            values=["auto", "float16", "int8_float16", "int8", "float32"],
            command=self._on_model_change,
        )
        self.compute_combo.grid(row=row, column=1, padx=6, pady=4, sticky="ew")
        row += 1

        label("Язык")
        self.lang_combo = ctk.CTkComboBox(
            body, values=["ru", "en", "auto"], command=self._on_setting_change
        )
        self.lang_combo.grid(row=row, column=1, padx=6, pady=4, sticky="ew")
        row += 1

        self.reload_btn = ctk.CTkButton(
            body, text="Загрузить / перезагрузить модель", command=self._preload_model
        )
        self.reload_btn.grid(
            row=row, column=0, columnspan=2, padx=6, pady=(6, 4), sticky="ew"
        )
        row += 1

        section("Микрофон")
        label("Устройство ввода")
        dev_frame = ctk.CTkFrame(body, fg_color="transparent")
        dev_frame.grid(row=row, column=1, padx=6, pady=4, sticky="ew")
        dev_frame.grid_columnconfigure(0, weight=1)
        self.mic_combo = ctk.CTkComboBox(
            dev_frame, values=self._device_values(), command=self._on_setting_change
        )
        self.mic_combo.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            dev_frame, text="⟳", width=34, command=self._refresh_devices
        ).grid(row=0, column=1, padx=(6, 0))
        row += 1

        section("Горячая клавиша (push-to-talk)")
        label("Бинд")
        bind_frame = ctk.CTkFrame(body, fg_color="transparent")
        bind_frame.grid(row=row, column=1, padx=6, pady=4, sticky="ew")
        bind_frame.grid_columnconfigure(0, weight=1)
        self.bind_label = ctk.CTkLabel(
            bind_frame,
            text="—",
            fg_color="#2b2b2b",
            corner_radius=6,
            height=32,
        )
        self.bind_label.grid(row=0, column=0, sticky="ew")
        self.bind_btn = ctk.CTkButton(
            bind_frame, text="Забиндить", width=90, command=self._start_capture
        )
        self.bind_btn.grid(row=0, column=1, padx=(6, 0))
        row += 1

        self.capture_banner = ctk.CTkLabel(
            body,
            text="Нажмите клавишу (можно с модификаторами) или кнопку мыши…",
            text_color="#f5a623",
        )
        self.capture_banner.grid(
            row=row, column=0, columnspan=2, padx=6, pady=(0, 4), sticky="w"
        )
        self.capture_banner.grid_remove()
        row += 1

        section("Поведение")
        self.auto_paste_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            body,
            text="Вставлять текст автоматически (Ctrl+V)",
            variable=self.auto_paste_var,
            command=self._on_setting_change,
        ).grid(row=row, column=0, columnspan=2, padx=6, pady=3, sticky="w")
        row += 1

        self.restore_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            body,
            text="Возвращать прежнее содержимое буфера обмена",
            variable=self.restore_var,
            command=self._on_setting_change,
        ).grid(row=row, column=0, columnspan=2, padx=6, pady=3, sticky="w")
        row += 1

        self.space_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            body,
            text="Добавлять пробел в конце",
            variable=self.space_var,
            command=self._on_setting_change,
        ).grid(row=row, column=0, columnspan=2, padx=6, pady=3, sticky="w")
        row += 1

        self.vad_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            body,
            text="Отсекать тишину (VAD)",
            variable=self.vad_var,
            command=self._on_setting_change,
        ).grid(row=row, column=0, columnspan=2, padx=6, pady=3, sticky="w")
        row += 1

        section("Тестовое поле (кликните сюда и продиктуйте)")
        self.test_entry = ctk.CTkTextbox(body, height=64)
        self.test_entry.grid(
            row=row, column=0, columnspan=2, padx=6, pady=(0, 6), sticky="ew"
        )
        row += 1

        # --- Вкл/выкл прослушивания ---
        self.toggle_btn = ctk.CTkButton(
            self,
            text="⏸  Приостановить прослушивание",
            height=40,
            command=self._toggle_listening,
        )
        self.toggle_btn.grid(row=2, column=0, padx=14, pady=(6, 4), sticky="ew")

        # --- Лог ---
        self.log_box = ctk.CTkTextbox(self, height=150)
        self.log_box.grid(row=3, column=0, padx=14, pady=(0, 14), sticky="nsew")
        self.log_box.configure(state="disabled")

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
        self._beam_size = int(self.cfg.get("beam_size", 1))
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
            }
        )
        config.save_config(self.cfg)

    def _on_setting_change(self, *_args) -> None:
        self._sync_cached_settings()

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
            info = f"{self.transcriber.device}/{self.transcriber.compute_type}"
            self._set_status("ready", f"Готов · модель {event[1]} ({info})")
            self._log(f"Модель загружена: {event[1]} [{info}]")
        elif kind == "model_error":
            self._loading = False
            self._set_status("error", "Ошибка загрузки модели")
            self._log(f"Ошибка загрузки модели: {event[1]}")
        elif kind == "result":
            self._busy = False
            self._set_status("ready", "Готов")
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

    # --------------------------------------------- запись (из потока hotkey)

    def _on_activate(self) -> None:
        """Вызывается из потока слушателя — только флаги/запись/очередь."""
        if not self._listening or self._capturing:
            return
        if self._busy:
            self._post(("status", "busy", "Идёт распознавание — подождите…"))
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
        self._post(("status", "record", "●  Запись…"))

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
        self._post(("status", "busy", "Распознавание…"))
        threading.Thread(
            target=self._transcribe_worker,
            args=(audio, self._lang, self._beam_size, self._vad),
            daemon=True,
        ).start()

    def _transcribe_worker(self, audio, lang: str, beam_size: int, vad: bool) -> None:
        try:
            text = self.transcriber.transcribe(
                audio, language=lang, beam_size=beam_size, vad_filter=vad
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
        self._set_status("idle", "Модель изменена — загружаю…")
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
        self._set_status("busy", f"Загрузка модели {model}…")
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
        label = binding_label(binding)
        self.bind_label.configure(text=label, fg_color="#2b2b2b")
        self.hint_label.configure(
            text=f"Удерживайте «{label}» — идёт запись; отпустите — текст вставится."
        )

    def _start_capture(self) -> None:
        self._capturing = True
        self.bind_label.configure(text="Нажмите…", fg_color="#3a3a1f")
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
            self.toggle_btn.configure(text="▶  Возобновить прослушивание")
            self._set_status("idle", "Прослушивание приостановлено")
        else:
            self.toggle_btn.configure(text="⏸  Приостановить прослушивание")
            self._set_status("ready", "Готов")
            self._log("Прослушивание включено.")

    def _set_status(self, state: str, text: str) -> None:
        color = COLORS.get(state, COLORS["idle"])
        prefix = "●  " if state in ("ready", "record") else ""
        self.status_label.configure(text=f"{prefix}{text}", text_color=color)

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

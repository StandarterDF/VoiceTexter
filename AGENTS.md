# VoiceTexter — agent guide

## Entrypoint & run

- `.\\.venv\\Scripts\\python.exe main.py` — launches the CustomTkinter GUI (`start.bat` wraps this).
- `setup.bat` / `setup.sh` — create `.venv`, install `requirements.txt`, copy `config.json.example` → `config.json`.
- No CLI flags, no server. The app is a single desktop GUI process.

## Architecture

Flat single-module project (no packages, no `__init__.py`):

```
main.py        — CustomTkinter GUI + app wiring (recording/transcribing/paste orchestration)
config.py      — config.json load/save, model discovery in models/
recorder.py    — sounddevice microphone capture (16 kHz mono float32 numpy)
transcriber.py — faster-whisper wrapper (lazy load, device/compute resolution)
hotkeys.py     — global push-to-talk listener (pynput keyboard + mouse) and binding capture
paster.py      — clipboard + simulated Ctrl+V paste
models/        — local faster-whisper model dirs (gitignored), one folder each with model.bin
```

## Key quirks

- **Threading model is the whole ballgame.** pynput listeners fire on their own
  threads; Tkinter widgets must only be touched on the main thread. So:
  - `HotkeyManager.on_activate` / `on_deactivate` (in `main.py`: `_on_activate` /
    `_on_deactivate`) run **off** the main thread. They may only read the *cached
    primitive* settings (`_model_name`, `_input_device`, `_lang`, `_beam_size`,
    `_vad`), touch the `Recorder`, and `_post()` events to `self._events`.
  - All widget reads/writes happen in `_drain_events` (polled via `after(80, …)`).
  - Settings are cached in `_sync_cached_settings()` (main thread only) — never
    read a `CTkComboBox`/`BooleanVar` from a listener or worker thread.
- **Push-to-talk semantics**: recording starts when *all* keys of the binding
  (or the mouse button) are held, and stops when *any* of them is released
  (`HotkeyManager._evaluate` recomputes on every press/release).
- **Binding format** (JSON, stored in `config.json`):
  - `{"kind": "keyboard", "keys": ["ctrl", "alt", "space"]}`
  - `{"kind": "mouse", "button": "x2"}` (mouse side buttons are `x1`/`x2`).
  Keys are normalized in `hotkeys.normalize_key` (modifiers collapse to
  `ctrl`/`shift`/`alt`/`cmd`; chars lowercased; special keys keep pynput names).
- **Binding capture** is a mode on the same listeners (`_capturing` flag):
  `begin_capture(cb)` → first non-modifier key press (with held modifiers) or the
  first mouse press finishes capture. The callback fires from the listener thread,
  so `main._on_binding_captured` only `_post`s a `("binding", …)` event.
- **Model discovery** = subdirs of `models/` containing `model.bin`
  (`config.list_models`). `large-v3-turbo` is the default. Models are load-on-demand
  in a background thread (`_preload_model`); the hotkey path refuses to record until
  the model reports loaded (`transcriber.is_loaded`).
- **Device/compute resolution**: `auto` → `cuda` iff `transcriber.cuda_available()`
  (loads `nvcuda.dll` / `libcuda.so`), else `cpu`; compute `auto` → `float16` on
  CUDA, `int8` on CPU. Reloading with a new device/compute swaps the model.
- **Paste** = `paster.paste_text`: copy text via pyperclip, tap Ctrl+V with a pynput
  `Controller`, optionally restore the previous clipboard (short sleep so the target
  app can read it). The VoiceTexter window must not steal focus — it doesn't.
- **Recording gate**: `_busy` blocks a new activation while a transcription runs;
  clips shorter than `MIN_AUDIO_SECONDS` (0.25 s) are dropped silently.
- **Shutdown**: `_on_close` saves settings, aborts recording, stops listeners,
  sets `_closing` (stops the `_drain_events` loop), then destroys the window.
  Tk may print harmless `invalid command name …` lines from pending CTk `after`
  callbacks after destroy — expected.

## Config

`config.json` (gitignored; template `config.json.example`) is written on every
setting change and on close. NOTE: the binding must be set on `HotkeyManager`
*and* the label rendered **before** the first `_sync_cached_settings()` in
`__init__`, otherwise the old code path saved `"binding": null`.

## Testing without a real mic/speech

- ASR smoke: load a model with `Transcriber().load(path, "auto", "auto")`, then
  `transcribe(audio_float32_16k, language=…)`. Works offline.
- **Do not put test scripts in a directory that also contains an `inspect.py`
  (or other stdlib-named file)** — Python prepends the script's dir to `sys.path`
  and shadows stdlib imports (e.g. numpy's `import inspect` breaks with
  `module 'inspect' has no attribute 'cleandoc'`).

## Dependencies

`requirements.txt` — `customtkinter`, `faster-whisper`, `sounddevice`, `soundfile`,
`numpy`, `pynput`, `pyperclip`. `faster-whisper` pulls `ctranslate2`, `tokenizers`,
`huggingface-hub`, `onnxruntime`, `av`.

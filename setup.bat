@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [VoiceTexter] Создаю виртуальное окружение .venv ...
python -m venv .venv

echo [VoiceTexter] Устанавливаю зависимости ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

if not exist config.json (
    copy config.json.example config.json >nul
    echo [VoiceTexter] Создан config.json из примера.
)

echo [VoiceTexter] Готово. Запускайте start.bat
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [VoiceTexter] Не найдено виртуальное окружение .venv.
    echo Запустите setup.bat для установки.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py
pause

@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  VoiceTexter - упаковка релиза
rem
rem  Собирает переносимый архив dist\VoiceTexter-<версия>-win64.zip из уже
rem  собранной папки dist\VoiceTexter (модели и config.json не входят).
rem
rem  Сначала выполните build_exe.bat, затем этот скрипт.
rem ---------------------------------------------------------------------------

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [VoiceTexter] Не найдено окружение .venv. Сначала запустите setup.bat.
    pause
    exit /b 1
)

"%PY%" tools\make_release.py %*
if errorlevel 1 (
    echo [VoiceTexter] Не удалось собрать архив.
    pause
    exit /b 1
)

echo [VoiceTexter] Архив готов — его можно приложить к GitHub Release.
pause

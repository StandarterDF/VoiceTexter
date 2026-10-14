@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  VoiceTexter - сборка EXE (PyInstaller)
rem
rem  build_exe.bat          -> папка dist\VoiceTexter\VoiceTexter.exe (рекомендуется)
rem  build_exe.bat onefile  -> один файл dist\VoiceTexter.exe
rem
rem  Скрипт сам поставит зависимости, сгенерирует значок и положит рядом с exe
rem  модели и config.json, чтобы сборку можно было сразу запускать.
rem ---------------------------------------------------------------------------

set "PY=.venv\Scripts\python.exe"
set "ONEFILE=0"
if /i "%~1"=="onefile" set "ONEFILE=1"

if not exist "%PY%" (
    echo [VoiceTexter] Не найдено окружение .venv.
    echo [VoiceTexter] Сначала запустите setup.bat.
    pause
    exit /b 1
)

echo [VoiceTexter] Проверяю зависимости ...
"%PY%" -m pip install -q -r requirements.txt
"%PY%" -m pip install -q -r requirements-build.txt
if errorlevel 1 (
    echo [VoiceTexter] Не удалось установить зависимости сборки.
    pause
    exit /b 1
)

if not exist "assets\icon.ico" (
    echo [VoiceTexter] Генерирую значок ...
    "%PY%" tools\make_icon.py
)

if "%ONEFILE%"=="1" (
    set "VOICETEXTER_ONEFILE=1"
    echo [VoiceTexter] Режим сборки: один файл.
) else (
    set "VOICETEXTER_ONEFILE=0"
    echo [VoiceTexter] Режим сборки: папка.
)

echo [VoiceTexter] Собираю EXE (это может занять несколько минут) ...
"%PY%" -m PyInstaller --noconfirm --clean VoiceTexter.spec
if errorlevel 1 (
    echo [VoiceTexter] Сборка не удалась.
    pause
    exit /b 1
)

if "%ONEFILE%"=="1" (
    set "DIST=dist"
    set "TARGET=dist\VoiceTexter.exe"
) else (
    set "DIST=dist\VoiceTexter"
    set "TARGET=dist\VoiceTexter\VoiceTexter.exe"
)

echo [VoiceTexter] Готовлю рабочую папку: %DIST%
if exist "config.json" (
    copy /y "config.json" "%DIST%\config.json" >nul
) else (
    copy /y "config.json.example" "%DIST%\config.json" >nul
)
if exist "CHANGELOG.md" copy /y "CHANGELOG.md" "%DIST%\CHANGELOG.md" >nul

if not exist "models" (
    echo [VoiceTexter] Папка models не найдена - приложение покажет "нет моделей".
    goto :done
)
if exist "%DIST%\models" goto :done

mklink /J "%DIST%\models" "%~dp0models" >nul 2>&1
if errorlevel 1 (
    echo [VoiceTexter] Символическая ссылка не удалась - копирую модели ...
    xcopy /e /i /y /q "models" "%DIST%\models" >nul
)

:done
echo.
echo [VoiceTexter] Готово!
echo     Запуск:  %TARGET%
echo     Модели:  %DIST%\models
echo     Настройки: %DIST%\config.json
echo.
echo [VoiceTexter] Крестик в окне сворачивает приложение в трей;
echo [VoiceTexter] выход - через пункт "Выход" в меню значка в трее.
pause

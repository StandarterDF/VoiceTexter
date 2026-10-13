#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "[VoiceTexter] Создаю виртуальное окружение .venv ..."
python3 -m venv .venv

echo "[VoiceTexter] Устанавливаю зависимости ..."
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

if [ ! -f config.json ]; then
    cp config.json.example config.json
    echo "[VoiceTexter] Создан config.json из примера."
fi

echo "[VoiceTexter] Готово. Запуск: ./.venv/bin/python main.py"

"""Собрать переносимый ZIP для GitHub Release из готовой папки dist/VoiceTexter.

Модели (``models/``) и личный ``config.json`` в архив **не** попадают: модели
большие, а приложение умеет скачивать их само (Настройки → «Скачать модель»).

Запуск (после ``build_exe.bat``):

    .\\.venv\\Scripts\\python.exe tools\\make_release.py

Версия берётся из git-тега (``vX.Y.Z``), иначе из packaging/version_info.txt.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP_DIR = ROOT / "dist" / "VoiceTexter"

# Что не кладём в архив (верхний уровень папки приложения).
EXCLUDE_TOP = {"models", "config.json"}


def detect_version() -> str:
    """Версия из ближайшего git-тега, иначе из version_info.txt, иначе 0.0.0."""
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        tag = out.stdout.strip()
        if tag:
            return tag.lstrip("v") or "0.0.0"
    except Exception:  # noqa: BLE001 - git может быть недоступен
        pass
    try:
        text = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
        match = re.search(r"filevers=\((\d+),\s*(\d+),\s*(\d+)", text)
        if match:
            return ".".join(match.groups())
    except Exception:  # noqa: BLE001
        pass
    return "0.0.0"


def build_zip(app_dir: Path, out: Path) -> tuple[int, float]:
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(app_dir):
            rel_root = Path(root).relative_to(app_dir)
            if rel_root.parts and rel_root.parts[0] in EXCLUDE_TOP:
                dirs[:] = []
                continue
            if not rel_root.parts:
                dirs[:] = [d for d in dirs if d not in EXCLUDE_TOP]
            for name in files:
                path = Path(root) / name
                rel = path.relative_to(app_dir)
                if rel.parts and rel.parts[0] in EXCLUDE_TOP:
                    continue
                zf.write(path, arcname=str(Path("VoiceTexter") / rel))
                count += 1
    return count, out.stat().st_size / 1024**2


def main() -> int:
    parser = argparse.ArgumentParser(description="ZIP для GitHub Release")
    parser.add_argument("--app-dir", default=str(DEFAULT_APP_DIR))
    parser.add_argument("--out", default=None)
    parser.add_argument("--version", default=None)
    args = parser.parse_args()

    app_dir = Path(args.app_dir)
    if not (app_dir / "VoiceTexter.exe").exists():
        print(
            f"[release] Не найден {app_dir / 'VoiceTexter.exe'}.\n"
            f"[release] Сначала соберите: build_exe.bat",
            file=sys.stderr,
        )
        return 1

    version = args.version or detect_version()
    out = (
        Path(args.out)
        if args.out
        else ROOT / "dist" / f"VoiceTexter-{version}-win64.zip"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    count, size_mb = build_zip(app_dir, out)
    print(f"[release] Готово: {out}")
    print(f"[release] Файлов: {count}, размер архива: {size_mb:.0f} МБ")
    print("[release] Модели в архив не входят — их качают из приложения.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

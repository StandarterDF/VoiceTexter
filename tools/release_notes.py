"""Извлечь примечания к релизу определённой версии из CHANGELOG.md.

Версия берётся из ``--version`` (или из переменной окружения ``GITHUB_REF_NAME``,
или из ``packaging/version_info.txt``). Ведущая ``v`` отбрасывается. Результат
пишется в ``--out`` (по умолчанию ``dist/RELEASE_NOTES.md``).

    .\\.venv\\Scripts\\python.exe tools\\release_notes.py --version v1.0.4

Скрипт не падает, если секция не найдена — тогда пишется краткая заглушка,
чтобы публикация релиза не срывалась.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANGELOG = ROOT / "CHANGELOG.md"
DEFAULT_OUT = ROOT / "dist" / "RELEASE_NOTES.md"

# «## [1.2.3] ...» или «## 1.2.3 ...»
_HEADING_RE = re.compile(r"^##\s+\[?(\d+\.\d+\.\d+)\]?")


def detect_version() -> str:
    """Версия из окружения CI, иначе из git-тега, иначе из version_info.txt."""
    ref = os.environ.get("GITHUB_REF_NAME", "").strip()
    if ref:
        return ref.lstrip("v")

    import subprocess

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
            return tag.lstrip("v")
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


def extract_section(text: str, version: str) -> str | None:
    """Вернуть тело секции ``## [version]`` (без заголовка) или None."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and match.group(1) == version:
            start = i + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        if _HEADING_RE.match(line):
            break
        body.append(line)
    return "\n".join(body).strip()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(description="Примечания к релизу из CHANGELOG.md")
    parser.add_argument("--version", default=None)
    parser.add_argument("--changelog", default=str(DEFAULT_CHANGELOG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    version = (args.version or detect_version()).lstrip("v")
    changelog = Path(args.changelog)
    out = Path(args.out)

    body = None
    if changelog.exists():
        body = extract_section(changelog.read_text(encoding="utf-8"), version)
    if not body:
        body = f"VoiceTexter {version}. См. полный список изменений в CHANGELOG.md."

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body + "\n", encoding="utf-8")
    print(f"[release_notes] Версия {version} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

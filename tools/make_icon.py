"""Генератор иконок приложения (assets/icon.png + assets/icon.ico).

Рисуем аккуратную «орб»-иконку в стиле GUI: фиолетовый круг с белым
микрофоном. Запуск из корня проекта:

    .\\.venv\\Scripts\\python.exe tools\\make_icon.py

Скрипт идемпотентен — просто перезаписывает assets/icon.png и icon.ico.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

SIZE = 1024
BG_TOP = (124, 140, 255)  # PALETTE["accent"]
BG_BOTTOM = (86, 96, 214)
MIC = (255, 255, 255)


def _radial_gradient(size: int) -> Image.Image:
    """Простой вертикальный градиент от BG_TOP к BG_BOTTOM."""
    grad = Image.new("RGB", (1, size))
    px = grad.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(
            round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
        )
    return grad.resize((size, size))


def make_icon(size: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # круг с лёгким «мягким» краем
    circle_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(circle_mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    circle_mask = circle_mask.resize((size, size))
    img.paste(_radial_gradient(size), (0, 0), circle_mask)

    # микрофон: капсула + дужка + ножка + основание
    d = ImageDraw.Draw(img)
    u = size / 100.0  # единица масштаба (проценты от размера)

    cap_w, cap_h = 22 * u, 40 * u
    cx = size / 2
    cap_top = 22 * u
    d.rounded_rectangle(
        (cx - cap_w / 2, cap_top, cx + cap_w / 2, cap_top + cap_h),
        radius=cap_w / 2,
        fill=MIC,
    )

    # дужка (арка вокруг капсулы)
    arc_w = 36 * u
    arc_top = 30 * u
    arc_bottom = arc_top + arc_w
    d.arc(
        (cx - arc_w / 2, arc_top, cx + arc_w / 2, arc_bottom),
        start=0,
        end=180,
        fill=MIC,
        width=round(7 * u),
    )

    # ножка
    d.rounded_rectangle(
        (cx - 3.5 * u, arc_bottom - 2 * u, cx + 3.5 * u, 82 * u),
        radius=3.5 * u,
        fill=MIC,
    )
    # основание
    d.rounded_rectangle(
        (cx - 15 * u, 80 * u, cx + 15 * u, 87 * u),
        radius=3.5 * u,
        fill=MIC,
    )

    return img


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon = make_icon()
    png_path = ASSETS / "icon.png"
    ico_path = ASSETS / "icon.ico"
    icon.save(png_path)
    icon.save(
        ico_path,
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )
    print(f"OK: {png_path}")
    print(f"OK: {ico_path}")


if __name__ == "__main__":
    main()

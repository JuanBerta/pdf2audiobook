"""Generate player-bar control icons (programmatic, MIT-style).

Creates assets/play.png, assets/pause.png, assets/stop.png,
assets/prev.png, assets/next.png as 128x128 RGBA PNGs.

Usage:
    python scripts/make_player_icons.py
"""
import os

from PIL import Image, ImageDraw

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

SIZE = 128
ACCENT = (46, 204, 113, 255)  # green, matches the app icon sound wave
DARK = (23, 42, 77, 255)      # deep blue, matches the app icon background


def _new_canvas():
    return Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))


def draw_play(size=SIZE, color=ACCENT):
    img = _new_canvas()
    d = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    w, h = size * 0.34, size * 0.44
    d.polygon(
        [
            (cx - w / 2, cy - h / 2),
            (cx - w / 2, cy + h / 2),
            (cx + w / 2, cy),
        ],
        fill=color,
    )
    return img


def draw_pause(size=SIZE, color=ACCENT):
    img = _new_canvas()
    d = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    w, gap = size * 0.14, size * 0.10
    h = size * 0.46
    d.rounded_rectangle(
        [cx - w - gap, cy - h / 2, cx - gap, cy + h / 2],
        radius=size * 0.03,
        fill=color,
    )
    d.rounded_rectangle(
        [cx + gap, cy - h / 2, cx + w + gap, cy + h / 2],
        radius=size * 0.03,
        fill=color,
    )
    return img


def draw_stop(size=SIZE, color=ACCENT):
    img = _new_canvas()
    d = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    side = size * 0.44
    d.rounded_rectangle(
        [cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2],
        radius=size * 0.05,
        fill=color,
    )
    return img


def _chevron(d, tip_x, tip_y, direction, color, size=SIZE):
    """Draw a double chevron (>> or <<) pointing along direction (1 or -1)."""
    width, height = size * 0.14, size * 0.26
    for i in range(2):
        ox = tip_x - direction * i * width
        d.polygon(
            [
                (ox, tip_y - height),
                (ox, tip_y + height),
                (ox + direction * width, tip_y),
            ],
            fill=color,
        )


def draw_prev(size=SIZE, color=ACCENT):
    img = _new_canvas()
    d = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    bar_w, bar_h = size * 0.09, size * 0.44
    gap = size * 0.18
    d.rounded_rectangle(
        [cx - gap - bar_w, cy - bar_h / 2, cx - gap, cy + bar_h / 2],
        radius=size * 0.02,
        fill=color,
    )
    _chevron(d, cx + gap, cy, -1, color)
    return img


def draw_next(size=SIZE, color=ACCENT):
    img = _new_canvas()
    d = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    bar_w, bar_h = size * 0.09, size * 0.44
    gap = size * 0.18
    _chevron(d, cx - gap, cy, 1, color)
    d.rounded_rectangle(
        [cx + gap, cy - bar_h / 2, cx + gap + bar_w, cy + bar_h / 2],
        radius=size * 0.02,
        fill=color,
    )
    return img


def main():
    os.makedirs(ASSETS, exist_ok=True)
    targets = {
        "play.png": draw_play(),
        "pause.png": draw_pause(),
        "stop.png": draw_stop(),
        "prev.png": draw_prev(),
        "next.png": draw_next(),
    }
    for name, img in targets.items():
        path = os.path.join(ASSETS, name)
        img.save(path)
        print(f"Wrote {path}")
    print("Done")


if __name__ == "__main__":
    main()

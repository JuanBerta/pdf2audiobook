"""Generate repeat-mode icons for the player bar (programmatic, MIT-style).

Creates assets/repeat_all.png, assets/repeat_one.png, assets/repeat_off.png
as 128x128 RGBA PNGs.

Usage:
    python scripts/make_repeat_icons.py
"""
import os

from PIL import Image, ImageDraw, ImageFont

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

SIZE = 128
ACCENT = (46, 204, 113, 255)  # green, matches the app icon sound wave
DARK = (23, 42, 77, 255)      # deep blue, matches the app icon background


def _arrow_head(d, tip, direction, color, width_scale=1.0):
    """Draw a triangular arrow head centered at `tip`, pointing along direction."""
    dx, dy = direction
    length = SIZE * 0.12 * width_scale
    spread = SIZE * 0.09 * width_scale
    bx = tip[0] - dx * length
    by = tip[1] - dy * length
    d.polygon(
        [
            tip,
            (bx - dy * spread, by + dx * spread),
            (bx + dy * spread, by - dx * spread),
        ],
        fill=color,
    )


def draw_repeat(size=SIZE, color=ACCENT):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = size / 2
    r = size * 0.30
    bbox = [cx - r, cy - r, cx + r, cy + r]
    width = max(6, size // 18)

    # Upper arc (left -> right over the top), lower arc (right -> left under the bottom).
    d.arc(bbox, start=180, end=360, fill=color, width=width)
    d.arc(bbox, start=0, end=180, fill=color, width=width)

    # Arrow heads at the arc endpoints (material-style loop).
    _arrow_head(d, (cx + r, cy), (-1, 0), color)      # top-right: pointing right
    _arrow_head(d, (cx - r, cy), (1, 0), color)       # bottom-left: pointing left
    return img


def draw_repeat_one(size=SIZE, color=ACCENT):
    img = draw_repeat(size, color)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.30))
    except OSError:
        font = ImageFont.load_default()
    text = "1"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    d.text(
        (size / 2 - tw / 2 - bbox[0], size / 2 - th / 2 - bbox[1]),
        text,
        font=font,
        fill=DARK,
    )
    return img


def grayscale_rgba(img):
    """Desaturate RGB while keeping the alpha channel."""
    alpha = img.getchannel("A")
    gray = img.convert("RGB").convert("L").convert("RGB")
    gray.putalpha(alpha)
    return gray


def main():
    os.makedirs(ASSETS, exist_ok=True)
    targets = {
        "repeat_all.png": draw_repeat(),
        "repeat_one.png": draw_repeat_one(),
        "repeat_off.png": grayscale_rgba(draw_repeat()),
    }
    for name, img in targets.items():
        path = os.path.join(ASSETS, name)
        img.save(path)
        print(f"Wrote {path}")
    print("Done")


if __name__ == "__main__":
    main()

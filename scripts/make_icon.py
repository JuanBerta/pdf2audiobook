"""Generate the app icon (drawn programmatically, public-domain style output).

Creates assets/icon.png (256x256) and assets/icon.ico (multi-size).

Usage:
    python scripts/make_icon.py
"""
import os

from PIL import Image, ImageDraw

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def draw_icon(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-rectangle background (deep blue)
    radius = size // 6
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=(23, 42, 77, 255))

    # Open book (white pages, subtle gradient by layering)
    margin = size * 0.24
    book_w = size * 0.52
    book_h = size * 0.34
    cx = size // 2
    top_y = size * 0.26
    bottom_y = top_y + book_h

    # Left page
    d.polygon(
        [
            (cx - book_w / 2, top_y),
            (cx - 2, top_y + 2),
            (cx - 2, bottom_y),
            (cx - book_w / 2, bottom_y + 4),
        ],
        fill=(245, 246, 250, 255),
    )
    # Right page
    d.polygon(
        [
            (cx + book_w / 2, top_y),
            (cx + 2, top_y + 2),
            (cx + 2, bottom_y),
            (cx + book_w / 2, bottom_y + 4),
        ],
        fill=(220, 224, 232, 255),
    )
    # Spine
    d.line([(cx - 2, top_y + 2), (cx - 2, bottom_y)], fill=(160, 168, 182, 255), width=max(2, size // 64))
    d.line([(cx + 2, top_y + 2), (cx + 2, bottom_y)], fill=(160, 168, 182, 255), width=max(2, size // 64))

    # Text lines on the left page
    line_y = top_y + size * 0.08
    for _ in range(4):
        x0 = cx - book_w / 2 + size * 0.06
        x1 = cx - size * 0.06
        d.line([(x0, line_y), (x1, line_y)], fill=(80, 92, 120, 255), width=max(2, size // 96))
        line_y += size * 0.05

    # Sound wave (green) rising from the bottom center
    wave_color = (46, 204, 113, 255)
    base_y = size * 0.86
    amp = size * 0.12
    d.arc(
        [cx - size * 0.30, base_y - amp * 2, cx - size * 0.16, base_y],
        start=0, end=180, fill=wave_color, width=max(3, size // 48),
    )
    d.arc(
        [cx + size * 0.16, base_y - amp * 2, cx + size * 0.30, base_y],
        start=0, end=180, fill=wave_color, width=max(3, size // 48),
    )
    d.arc(
        [cx - size * 0.42, base_y - amp * 3, cx - size * 0.06, base_y],
        start=0, end=180, fill=wave_color, width=max(3, size // 48),
    )
    d.arc(
        [cx + size * 0.06, base_y - amp * 3, cx + size * 0.42, base_y],
        start=0, end=180, fill=wave_color, width=max(3, size // 48),
    )
    # Center play triangle
    d.polygon(
        [
            (cx - size * 0.05, base_y - size * 0.075),
            (cx + size * 0.06, base_y - size * 0.05),
            (cx - size * 0.05, base_y - size * 0.02),
        ],
        fill=(255, 255, 255, 255),
    )
    return img


def main():
    os.makedirs(ASSETS, exist_ok=True)
    img = draw_icon(256)
    png_path = os.path.join(ASSETS, "icon.png")
    img.save(png_path)
    img.save(os.path.join(ASSETS, "icon.png"))  # keep PNG for README
    # ICO with common sizes
    ico_path = os.path.join(ASSETS, "icon.ico")
    img.save(
        ico_path,
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"Wrote {png_path}")
    print(f"Wrote {ico_path}")


if __name__ == "__main__":
    main()

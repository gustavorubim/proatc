"""Draws the Pro ATC app icon and writes build/icon.png, build/icon.ico and the in-app logo SVG.

The mark: a dark rounded square, three rising orange bars (signal strength, and a voice level
meter) and a tan dot. Colours are the brand palette. Run with:

    uvx --with pillow python scripts/make-icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
INK, ORANGE, TAN = "#242832", "#FF9159", "#EAC6A5"

# Geometry on a 1024 canvas, shared by the PNG and the SVG.
SIZE = 1024
TILE_RADIUS = 232
BARS = [(214, 590), (427, 452), (640, 330)]  # (left edge, top edge)
BAR_WIDTH, BAR_BOTTOM, BAR_RADIUS = 170, 800, 54
DOT = (299, 420, 58)  # centre x, centre y, radius


def draw(scale: int = 4) -> Image.Image:
    """Render oversized, then shrink, for smooth edges."""
    big = SIZE * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((0, 0, big - 1, big - 1), radius=TILE_RADIUS * scale, fill=INK)
    for left, top in BARS:
        box = (left * scale, top * scale, (left + BAR_WIDTH) * scale, BAR_BOTTOM * scale)
        pen.rounded_rectangle(box, radius=BAR_RADIUS * scale, fill=ORANGE)
    x, y, r = DOT
    pen.ellipse(((x - r) * scale, (y - r) * scale, (x + r) * scale, (y + r) * scale), fill=TAN)
    return image.resize((SIZE, SIZE), Image.LANCZOS)


def svg() -> str:
    bars = "\n".join(
        f'  <rect x="{left}" y="{top}" width="{BAR_WIDTH}" height="{BAR_BOTTOM - top}" rx="{BAR_RADIUS}" fill="{ORANGE}"/>'
        for left, top in BARS
    )
    x, y, r = DOT
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" role="img" aria-label="Pro ATC">\n'
        f'  <rect width="{SIZE}" height="{SIZE}" rx="{TILE_RADIUS}" fill="{INK}"/>\n'
        f"{bars}\n"
        f'  <circle cx="{x}" cy="{y}" r="{r}" fill="{TAN}"/>\n'
        "</svg>\n"
    )


def main() -> None:
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    icon = draw()
    icon.save(build / "icon.png")
    icon.save(build / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    assets = ROOT / "src" / "renderer" / "src" / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "logo.svg").write_text(svg(), encoding="utf-8", newline="\n")
    print("Wrote build/icon.png, build/icon.ico and src/renderer/src/assets/logo.svg")


if __name__ == "__main__":
    main()

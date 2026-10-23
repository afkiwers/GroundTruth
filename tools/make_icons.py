"""Render the GroundTruth toolbar icons (PNG) for the KiCad plugin manifest.

    .venv/Scripts/python tools/make_icons.py              # writes plugin/icons/groundtruth-*.png and the windows'
                                                          # icon, src/groundtruth/ui/icons/groundtruth-<size>.png
    .venv/Scripts/python tools/make_icons.py --preview X  # X.png: the icons 1:1 and enlarged, light and dark

The design (chosen by the user): a ground symbol inside a current loop with an arrowhead, on a navy badge. It is
drawn on a 24-unit grid with whole-unit strokes, rendered 8× larger and box-downsampled, so straight edges fall
on pixel boundaries and stay sharp at 24 px; the 48 px icon is the same drawing at twice the scale. (A first,
matplotlib version with a soft glow was blurry at toolbar size.)
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "plugin" / "icons"
WINDOW = Path(__file__).resolve().parent.parent / "src" / "groundtruth" / "ui" / "icons"
WINDOW_SIZES = (16, 24, 32, 48, 256)     # Windows picks the title bar's, the taskbar's and Alt+Tab's from these
NAVY = (18, 58, 92, 255)
NAVY_EDGE = (10, 34, 56, 255)
AMBER = (255, 176, 32, 255)
WHITE = (255, 255, 255, 255)
RED = (226, 54, 54, 255)
GREEN = (40, 158, 64, 255)
SS = 8                                   # supersampling factor


def draw(size: int, badge: str = "") -> Image.Image:
    """The icon at `size` px (24 or 48), with an optional badge at the bottom right: "clear" (red disc, white
    cross) or "add" (green disc, white plus). Coordinates are in 24-unit icon space."""
    s = size / 24 * SS                   # supersampled pixels per unit
    img = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1):
        """A box in icon pixels from coordinates on the 0..1 design grid (scaled by s, inclusive end)."""
        return [x0 * s, y0 * s, x1 * s - 1, y1 * s - 1]

    # badge: 1-unit dark edge around a navy rounded square
    d.rounded_rectangle(box(0, 0, 24, 24), radius=5 * s, fill=NAVY_EDGE)
    d.rounded_rectangle(box(1, 1, 23, 23), radius=4 * s, fill=NAVY)
    # current loop: a 2-unit ring open at the top (where the ground stem points), arrowhead at its right end
    cx, cy, r, w = 12, 12.5, 8.5, 2       # ring centre-line radius r, width w
    a0, a1 = -60, 240                      # PIL angles: 0 = east, clockwise; the gap is 240..300 (top)
    d.arc(box(cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2), a0, a1, fill=AMBER,
          width=round(w * s))
    t = math.radians(a0)
    ex, ey = cx + r * math.cos(t), cy + r * math.sin(t)
    tx, ty = -math.sin(t), math.cos(t)     # direction of increasing angle (clockwise on screen)
    nx, ny = math.cos(t), math.sin(t)
    tip = (ex - tx * 3.2, ey - ty * 3.2)   # the arrow points out of the ring's end, into the gap
    base = [(ex + nx * 2.4, ey + ny * 2.4), (ex - nx * 2.4, ey - ny * 2.4)]
    d.polygon([(p[0] * s, p[1] * s) for p in (tip, *base)], fill=AMBER)
    # ground symbol: 2-unit stem and bars on whole units
    d.rectangle(box(11, 4, 13, 11), fill=WHITE)
    d.rectangle(box(6, 11, 18, 13), fill=WHITE)
    d.rectangle(box(8, 14, 16, 16), fill=WHITE)
    d.rectangle(box(10, 17, 14, 19), fill=WHITE)
    if badge == "clear":                   # red disc with a white cross, bottom right
        d.ellipse(box(13, 13, 24, 24), fill=RED, outline=WHITE, width=round(1 * s))
        for (x0, y0, x1, y1) in ((16, 16, 21, 21), (16, 21, 21, 16)):
            d.line([(x0 * s, y0 * s), (x1 * s, y1 * s)], fill=WHITE, width=round(1.6 * s))
    if badge == "add":                     # green disc with a white plus on whole pixels, bottom right
        d.ellipse(box(12, 12, 24, 24), fill=GREEN, outline=WHITE, width=round(1 * s))
        d.rectangle(box(14, 17, 22, 19), fill=WHITE)    # centre 18, 2-unit arms: crisp at 24 px
        d.rectangle(box(17, 14, 19, 22), fill=WHITE)
    return img.resize((size, size), Image.Resampling.BOX)


def write_icons():
    """The badge carries its own background, so the light and dark files are the same picture."""
    for name, size, badge in (("groundtruth-24", 24, ""), ("groundtruth-48", 48, ""),
                              ("groundtruth-dark-24", 24, ""), ("groundtruth-dark-48", 48, ""),
                              ("groundtruth-clear-24", 24, "clear"), ("groundtruth-clear-dark-24", 24, "clear"),
                              ("groundtruth-add-24", 24, "add"), ("groundtruth-add-48", 48, "add"),
                              ("groundtruth-add-dark-24", 24, "add"), ("groundtruth-add-dark-48", 48, "add")):
        draw(size, badge).save(OUT / f"{name}.png")
    # the windows' own icon (title bar, taskbar, Alt+Tab: theme.app_icons), shipped inside the package
    WINDOW.mkdir(exist_ok=True)
    for size in WINDOW_SIZES:
        draw(size).save(WINDOW / f"groundtruth-{size}.png")


def preview(path: Path):
    """1:1 (24, 48, clear 24, add 24) and enlarged ×8 with nearest-neighbour (to judge the pixels), on light
    and dark."""
    icons = [draw(24), draw(48), draw(24, "clear"), draw(24, "add")]
    w, h = 24 + 48 + 24 + 24 + 5 * 16 + 24 * 8 * 2 + 32, 24 * 8 + 32
    sheet = Image.new("RGBA", (w, 2 * h), (0, 0, 0, 0))
    for row, bg in enumerate(((240, 240, 240, 255), (43, 43, 43, 255))):
        y0 = row * h
        sheet.paste(Image.new("RGBA", (w, h), bg), (0, y0))
        x = 16
        for im in icons:
            sheet.alpha_composite(im, (x, y0 + 16))
            x += im.width + 16
        sheet.alpha_composite(icons[0].resize((24 * 8, 24 * 8), Image.Resampling.NEAREST), (x, y0 + 16))
        sheet.alpha_composite(icons[3].resize((24 * 8, 24 * 8), Image.Resampling.NEAREST),
                              (x + 24 * 8 + 16, y0 + 16))
    sheet.save(path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preview", type=Path, help="write a preview sheet instead")
    a = ap.parse_args()
    if a.preview:
        preview(a.preview)
    else:
        write_icons()

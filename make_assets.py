#!/usr/bin/env python3
"""Generate Speedy app icons and branding images.

    python make_assets.py

Writes every platform asset into assets/. Requires cairosvg and Pillow.
The design is drawn once as SVG at a 1024x1024 viewBox and re-rendered at each
target size, so all outputs stay sharp and consistent.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import cairosvg
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent / "assets"
DESKTOP = Path(__file__).parent / "client" / "build"

AUTHOR = "Ayanlowo Olatunji Ayobami"

# Palette mirrors the web UI so the icon feels native to the product.
GRAD = (("#6aa6ff", 0.0), ("#3a78f0", 0.55), ("#2554c4", 1.0))
BG_DARK = "#0b0d12"
INK = "#eef1f5"
MUTED = "#8b93a1"

CANVAS = 1024

ARROW = (
    '<g stroke="#ffffff" stroke-width="116" stroke-linecap="round" '
    'stroke-linejoin="round" fill="none">'
    '<path d="M566 344 L566 636"/>'
    '<path d="M446 560 L566 682 L686 560"/>'
    "</g>"
)

# Trailing bars read as motion; opacity ramps toward the arrow.
STREAKS = (
    '<g fill="#ffffff">'
    '<rect x="240" y="336" width="150" height="32" rx="16" opacity="0.42"/>'
    '<rect x="240" y="440" width="110" height="32" rx="16" opacity="0.60"/>'
    '<rect x="240" y="544" width="72"  height="32" rx="16" opacity="0.78"/>'
    "</g>"
)

_BBOX_CACHE: dict[bool, tuple[int, int, int, int]] = {}


def art_bbox(streaks: bool) -> tuple[int, int, int, int]:
    """Exact bounding box of the raw artwork, measured from rendered pixels.

    Hand-maintained coordinates drift whenever the artwork is tweaked, so the
    box is measured from the alpha channel instead. Cached; it never changes.
    """
    if streaks not in _BBOX_CACHE:
        raw = ARROW + (STREAKS if streaks else "")
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
               f'viewBox="0 0 {CANVAS} {CANVAS}">{raw}</svg>')
        png = cairosvg.svg2png(bytestring=svg.encode(),
                               output_width=CANVAS, output_height=CANVAS)
        bbox = Image.open(io.BytesIO(png)).convert("RGBA").getchannel("A").getbbox()
        if bbox is None:
            raise RuntimeError("artwork rendered empty")
        _BBOX_CACHE[streaks] = bbox
    return _BBOX_CACHE[streaks]


def _art(fill: float, streaks: bool) -> str:
    """Artwork sized so its bounding box spans `fill` of the canvas height."""
    x0, y0, x1, y1 = art_bbox(streaks)
    scale = CANVAS * fill / (y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    inner = ARROW + (STREAKS if streaks else "")
    return (
        f'<g transform="translate({CANVAS / 2} {CANVAS / 2}) '
        f'scale({scale:.5f}) translate({-cx} {-cy})">{inner}</g>'
    )


def _background(rounded: bool) -> str:
    rx = 232 if rounded else 0  # ~iOS squircle radius
    stops = "".join(
        f'<stop offset="{off}" stop-color="{col}"/>' for col, off in GRAD
    )
    return (
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f"{stops}</linearGradient></defs>"
        f'<rect width="1024" height="1024" rx="{rx}" fill="url(#g)"/>'
    )


def build_svg(rounded: bool = False, bg: bool = True, fill: float = 0.70,
              streaks: bool = True, show_art: bool = True) -> str:
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">']
    if bg:
        parts.append(_background(rounded))
    if show_art:
        parts.append(_art(fill, streaks))
    parts.append("</svg>")
    return "".join(parts)


def render(svg: str, size: int) -> Image.Image:
    png = cairosvg.svg2png(bytestring=svg.encode(),
                           output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def icon(size: int, rounded: bool = True) -> Image.Image:
    # Streaks fall below a pixel at tiny sizes, so drop them for legibility.
    return render(build_svg(rounded=rounded, streaks=size >= 48), size)


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    base = Path("/usr/share/fonts/truetype/dejavu")
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = base / name
    if not path.exists():
        path = base / "DejaVuSans.ttf"
    return ImageFont.truetype(str(path), size)


def save(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def make_ios() -> list[Path]:
    # Full-bleed: iOS applies its own corner mask.
    sizes = [1024, 180, 167, 152, 120, 76, 60, 40, 29]
    out = []
    for s in sizes:
        p = OUT / "ios" / f"Icon-{s}.png"
        save(icon(s, rounded=False), p)
        out.append(p)
    return out


def make_android() -> list[Path]:
    out = []
    legacy = {"mdpi": 48, "hdpi": 72, "xhdpi": 96,
              "xxhdpi": 144, "xxxhdpi": 192}
    for density, s in legacy.items():
        p = OUT / "android" / f"mipmap-{density}" / "ic_launcher.png"
        save(icon(s, rounded=True), p)
        out.append(p)

    # Adaptive icons composite a foreground over a background. Only the central
    # 66% circle is guaranteed visible under every launcher mask, so the art is
    # scaled down to sit well inside that safe zone.
    adaptive = {"mdpi": 108, "hdpi": 162, "xhdpi": 216,
                "xxhdpi": 324, "xxxhdpi": 432}
    fg_svg = build_svg(bg=False, fill=0.42)
    bg_svg = build_svg(rounded=False, show_art=False)
    for density, s in adaptive.items():
        d = OUT / "android" / f"mipmap-{density}"
        p = d / "ic_launcher_foreground.png"
        save(render(fg_svg, s), p)
        out.append(p)
        p = d / "ic_launcher_background.png"
        save(render(bg_svg, s), p)
        out.append(p)
    return out


def make_web() -> list[Path]:
    out = []
    for s in (192, 512):
        p = OUT / "web" / f"icon-{s}.png"
        save(icon(s, rounded=True), p)
        out.append(p)

    p = OUT / "web" / "apple-touch-icon.png"
    save(icon(180, rounded=False), p)  # iOS masks this itself
    out.append(p)

    # Multi-resolution .ico so browsers pick a crisp size for the tab.
    ico = OUT / "web" / "favicon.ico"
    ico.parent.mkdir(parents=True, exist_ok=True)
    icon(256, rounded=True).save(
        ico, format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    out.append(ico)

    manifest = {
        "name": "Speedy — Media & File Downloader",
        "short_name": "Speedy",
        "description": ("Fast downloader for video, audio, documents and any "
                        "file you have the right to download. Built by "
                        "Ayanlowo Olatunji Ayobami."),
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": "#0b0d12",
        "theme_color": "#0b0d12",
        "categories": ["utilities", "productivity"],
        "lang": "en",
        "dir": "ltr",
        "author": AUTHOR,
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
             "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    p = OUT / "web" / "manifest.webmanifest"
    p.write_text(json.dumps(manifest, indent=2) + "\n")
    out.append(p)
    return out


def make_desktop() -> list[Path]:
    """Icon used by the Electron desktop build (Windows, macOS, Linux).

    Unrounded and full-bleed: the installers apply their own masking, and a
    pre-rounded icon looks wrong in the Windows taskbar.
    """
    out = []
    for name, size in (("icon.png", 512), ("icon-256.png", 256)):
        p = DESKTOP / name
        save(icon(size, rounded=False), p)
        out.append(p)
    return out


def make_og() -> list[Path]:
    """1200x630 social preview card."""
    W, H = 1200, 630
    base = Image.new("RGBA", (W, H), BG_DARK)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([-140, -260, 720, 470], fill=(58, 120, 240, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(150))
    base = Image.alpha_composite(base, glow)

    badge = icon(220, rounded=True)
    base.alpha_composite(badge, (90, 205))

    d = ImageDraw.Draw(base)
    d.text((352, 268), "Speedy", font=font(96), fill=INK, anchor="lm")
    d.text((356, 348), "Fast media downloader", font=font(38, bold=False),
           fill=MUTED, anchor="lm")

    d.rectangle([356, 392, 456, 398], fill="#3a78f0")

    p = OUT / "social" / "og-image.png"
    save(base.convert("RGB"), p)
    return [p]


def make_logo() -> list[Path]:
    """Transparent horizontal lockup for READMEs and docs.

    Drawn on an oversized canvas, then trimmed to the actual content so the
    file has no dead space and no fixed-size assumptions to drift.
    """
    W, H = 900, 220
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.alpha_composite(icon(150, rounded=True), (10, 35))
    d = ImageDraw.Draw(img)
    d.text((190, 92), "Speedy", font=font(72), fill=INK, anchor="lm")
    d.text((193, 146), "Fast media downloader", font=font(28, bold=False),
           fill=MUTED, anchor="lm")

    bbox = img.getchannel("A").getbbox()
    if bbox:
        pad = 12
        x0, y0, x1, y1 = bbox
        img = img.crop((max(0, x0 - pad), max(0, y0 - pad),
                        min(W, x1 + pad), min(H, y1 + pad)))
    p = OUT / "logo-horizontal.png"
    save(img, p)
    return [p]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon-master.svg").write_text(build_svg(rounded=True))
    (OUT / "icon-master-fullbleed.svg").write_text(build_svg(rounded=False))

    made = [OUT / "icon-master.svg", OUT / "icon-master-fullbleed.svg"]
    save(icon(1024, rounded=True), OUT / "icon-1024.png")
    save(icon(1024, rounded=False), OUT / "icon-1024-fullbleed.png")
    made += [OUT / "icon-1024.png", OUT / "icon-1024-fullbleed.png"]

    made += make_ios() + make_android() + make_web() + make_og() + make_logo()
    made += make_desktop()

    for p in made:
        if p.suffix == ".svg":
            print(f"  {str(p.relative_to(OUT.parent)):52} vector")
        elif p.suffix == ".ico":
            print(f"  {str(p.relative_to(OUT.parent)):52} multi-size")
        elif p.suffix == ".webmanifest":
            print(f"  {str(p.relative_to(OUT.parent)):52} manifest")
        else:
            with Image.open(p) as im:
                print(f"  {str(p.relative_to(OUT.parent)):52} "
                      f"{im.size[0]}x{im.size[1]}")
    print(f"\n{len(made)} files written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

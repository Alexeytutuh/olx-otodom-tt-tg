"""TikTok carousel slides (1080x1920 JPEG) with Pillow. Independent of Telegram: give it a Listing and a
folder, get back JPEG paths. One photo is decoded at a time (JPEG draft mode), so peak memory stays low.

Slide 1: first photo cover-cropped to 9:16 + two orange translucent label plates in TikTok's
         "text with background" style: top '<type> | <m²> m² | Wrocław (district)', bottom = key feature
         (or the price when no feature is detected).
Others : the photo fitted into 9:16 over a blurred, darkened copy of itself (no cropping)."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from parsers.models import Listing

from . import images
from .formatting import slide_labels

log = logging.getLogger("olxbot.slides")

W, H = 1080, 1920
JPEG_QUALITY = 90
FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILE = FONT_DIR / "Montserrat-SemiBold.ttf"

# look of the friend's TikTok example: warm orange plate, see-through, white text, rounded corners
STYLE = {
    "plate_rgba": (240, 138, 36, 195),
    "text_rgb": (255, 255, 255),
    "font_size": 72, "min_font_size": 48,
    "pad_x": 28, "pad_y": 14, "radius": 22, "line_gap": 0,
    "side_margin": 60,
    "top_y": 210,          # top edge of the top plate (below TikTok's top tabs)
    "bottom_cy": 1290,     # centre of the bottom plate (above TikTok's caption area)
}


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_FILE), size, layout_engine=ImageFont.Layout.BASIC)
    except OSError:  # pragma: no cover - bundled font missing
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        return ImageFont.load_default(size)


def _text_w(font, s: str) -> int:
    b = font.getbbox(s)
    return b[2] - b[0]


def _wrap(text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if not cur or _text_w(font, cand) <= max_w:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    # a line must not start with the separator
    return [l[2:] if l.startswith("| ") else l for l in lines]


def _layout(text: str, style=STYLE) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    max_w = W - 2 * style["side_margin"] - 2 * style["pad_x"]
    size = style["font_size"]
    while True:
        font = _font(size)
        if _text_w(font, text) <= max_w or size <= style["min_font_size"]:
            break
        size -= 2
    lines = [text] if _text_w(font, text) <= max_w else _wrap(text, font, max_w)
    return font, lines


def draw_plates(base: Image.Image, text: str, top: Optional[int] = None, center_y: Optional[int] = None,
                style=STYLE) -> Image.Image:
    """Draw TikTok-style label: every line gets its own rounded plate hugging the text; plates touch."""
    font, lines = _layout(text, style)
    asc, desc = font.getmetrics()
    line_h = asc + desc + 2 * style["pad_y"]
    block_h = line_h * len(lines) + style["line_gap"] * (len(lines) - 1)
    y0 = top if top is not None else int(center_y - block_h / 2)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for i, line in enumerate(lines):
        tw = _text_w(font, line)
        x0 = (W - tw) // 2 - style["pad_x"]
        y = y0 + i * (line_h + style["line_gap"])
        d.rounded_rectangle([x0, y, x0 + tw + 2 * style["pad_x"], y + line_h], radius=style["radius"],
                            fill=style["plate_rgba"])
    out = Image.alpha_composite(base.convert("RGBA"), overlay)
    d = ImageDraw.Draw(out)
    for i, line in enumerate(lines):
        y = y0 + i * (line_h + style["line_gap"]) + line_h // 2
        d.text((W // 2, y), line, font=font, fill=style["text_rgb"], anchor="mm")
    return out.convert("RGB")


def cover(im: Image.Image) -> Image.Image:
    return ImageOps.fit(im, (W, H), Image.LANCZOS, centering=(0.5, 0.5))


def fit_on_blur(im: Image.Image) -> Image.Image:
    small = ImageOps.fit(im, (W // 4, H // 4), Image.BILINEAR)
    bg = small.filter(ImageFilter.GaussianBlur(10)).resize((W, H), Image.BILINEAR)
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    scale = min(W / im.width, H / im.height)
    fg = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg


def text_only_slide(top: str, bottom: str) -> Image.Image:
    im = Image.new("RGB", (W, H), (34, 34, 38))
    im = draw_plates(im, top, top=STYLE["top_y"])
    return draw_plates(im, bottom, center_y=H // 2)


def render_cover(photo: Image.Image, top: str, bottom: str) -> Image.Image:
    im = cover(photo)
    im = draw_plates(im, top, top=STYLE["top_y"])
    return draw_plates(im, bottom, center_y=STYLE["bottom_cy"])


def _save(im: Image.Image, path: Path):
    tmp = path.with_suffix(".tmp")
    im.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=False, subsampling=0)
    os.replace(tmp, path)


def generate(lst: Listing, out_dir: str | Path, max_slides: int = 10,
             fetch: Callable[[str], bytes] = None, beat: Callable[[], None] = None) -> list[Path]:
    """Download photos one by one and write slide_01.jpg ... into out_dir. Returns the paths in order."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fetch = fetch or (lambda u: images.download(u, beat=beat))
    top, bottom = slide_labels(lst)
    paths: list[Path] = []
    for url in (lst.photos or [])[: max_slides + 3]:          # a few spares for broken photos
        if len(paths) >= max_slides:
            break
        if beat:
            beat()
        first = not paths
        try:
            data = fetch(images.sized_url(url, "4000x4000" if first else "2048x2048"))
            photo = images.open_image(data, (W, H) if first else (W, W))
            del data
            slide = render_cover(photo, top, bottom) if first else fit_on_blur(photo)
            photo.close()
        except Exception as e:
            log.warning("slide from %s skipped: %s", url[:90], e)
            continue
        p = out / f"slide_{len(paths) + 1:02d}.jpg"
        _save(slide, p)
        slide.close()
        paths.append(p)
    if not paths:
        p = out / "slide_01.jpg"
        _save(text_only_slide(top, bottom), p)
        paths.append(p)
    return paths

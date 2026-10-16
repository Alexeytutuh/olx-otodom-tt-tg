"""TikTok carousel slides (1080x1920 JPEG) with Pillow. Independent of Telegram: give it a Listing and a
folder, get back JPEG paths. One photo is decoded at a time (JPEG draft mode), so peak memory stays low.

Slide 1 (cover) comes in four looks, picked per listing (the same listing always gets the same one, because the
slides are generated again when the draft goes to TikTok):
  A  first photo cover-cropped to 9:16 + two orange translucent plates in TikTok's "text with background" style:
     top '<type> | <m²> | Wrocław (district)', bottom = what is special about the flat (or the district)
  B  the photo blurred over the whole frame + a big white plate (city, type, price) + a white plate with the highlight
  C  sharp photo + one white plate: type, up to three highlights, 📍 address
  D  like A, bigger letters, CAPS
Others : the photo fitted into 9:16 over a blurred, darkened copy of itself (no cropping)."""
from __future__ import annotations

import logging
import os
import zlib
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from parsers.models import Listing

from . import images
from .formatting import SlideTexts, slide_texts

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
    "top_y": 300,          # top edge of the top plate (below TikTok's search bar and top tabs)
    "bottom_cy": 1290,     # centre of the bottom plate (above TikTok's caption area)
}
STYLE_CAPS = {**STYLE, "font_size": 84, "min_font_size": 84, "caps": True, "pad_x": 30, "pad_y": 15}
# one big white plate with several centred lines (styles B and C)
WHITE = {
    "plate_rgba": (255, 255, 255, 244),
    "text_rgb": (18, 18, 18),
    "font_size": 84, "min_font_size": 50,
    "pad_x": 48, "pad_y": 26, "radius": 38, "line_gap": 10,
    "side_margin": 60,
    "top_y": STYLE["top_y"], "bottom_cy": STYLE["bottom_cy"],
}
WHITE_SMALL = {**WHITE, "font_size": 76, "pad_y": 20}
PIN_RGB = (226, 52, 52)

STYLES = ("A", "B", "C", "D")


def pick_style(lst: Listing) -> str:
    """Deterministic: the same listing always gets the same cover."""
    return STYLES[zlib.crc32(lst.dedupe_key.encode()) % len(STYLES)]


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
    if style.get("caps"):
        text = text.upper()
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


def _pin(d: ImageDraw.ImageDraw, x: float, top: float, h: float):
    """Map pin (like 📍) drawn as a shape: no emoji font needed. Occupies x .. x + 0.7 h."""
    r = h * 0.34
    cx, cy = x + 0.35 * h, top + r
    tip = h - r                                    # distance circle centre -> tip
    px, ay = r * (tip * tip - r * r) ** 0.5 / tip, r * r / tip     # tangent points of the drop
    d.polygon([(cx - px, cy + ay), (cx + px, cy + ay), (cx, cy + tip)], fill=PIN_RGB)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PIN_RGB)
    d.ellipse([cx - r * 0.4, cy - r * 0.4, cx + r * 0.4, cy + r * 0.4], fill=(255, 255, 255))


def draw_block(base: Image.Image, lines: list[tuple[str, bool]], top: Optional[int] = None,
               center_y: Optional[int] = None, style=WHITE) -> Image.Image:
    """One rounded plate with several centred lines. A line given as (text, True) starts with the 📍 pin."""
    max_w = W - 2 * style["side_margin"] - 2 * style["pad_x"]
    size = style["font_size"]
    while True:
        font = _font(size)
        asc, desc = font.getmetrics()
        pin_w, gap = round(asc * 0.7), round(asc * 0.25)
        extra = lambda pin: pin_w + gap if pin else 0          # noqa: E731
        if max(_text_w(font, t) + extra(p) for t, p in lines) <= max_w or size <= style["min_font_size"]:
            break
        size -= 2
    rows: list[tuple[str, bool]] = []
    for text, pin in lines:
        if _text_w(font, text) + extra(pin) <= max_w:
            rows.append((text, pin))
        else:                                                   # still too wide at the smallest size
            for k, part in enumerate(_wrap(text, font, max_w - extra(pin))):
                rows.append((part, pin and k == 0))
    row_h = asc + desc + style["line_gap"]
    widths = [_text_w(font, t) + extra(p) for t, p in rows]
    box_w, box_h = max(widths) + 2 * style["pad_x"], row_h * len(rows) - style["line_gap"] + 2 * style["pad_y"]
    y0 = top if top is not None else int(center_y - box_h / 2)
    x0 = (W - box_w) // 2
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=style["radius"],
                                              fill=style["plate_rgba"])
    out = Image.alpha_composite(base.convert("RGBA"), overlay)
    d = ImageDraw.Draw(out)
    for i, ((text, pin), w) in enumerate(zip(rows, widths)):
        cy = y0 + style["pad_y"] + i * row_h + (asc + desc) / 2
        x = (W - w) / 2
        if pin:
            _pin(d, x, cy - asc * 0.5, asc * 1.0)
            x += pin_w + gap
        d.text((x, cy), text, font=font, fill=style["text_rgb"], anchor="lm")
    return out.convert("RGB")


def cover(im: Image.Image) -> Image.Image:
    return ImageOps.fit(im, (W, H), Image.LANCZOS, centering=(0.5, 0.5))


def blur_cover(im: Image.Image) -> Image.Image:
    """The photo blurred over the whole frame (cover style B)."""
    small = ImageOps.fit(im, (W // 8, H // 8), Image.BILINEAR)
    bg = small.filter(ImageFilter.GaussianBlur(2)).resize((W, H), Image.BICUBIC)
    return ImageEnhance.Brightness(bg).enhance(0.92)


def fit_on_blur(im: Image.Image) -> Image.Image:
    small = ImageOps.fit(im, (W // 4, H // 4), Image.BILINEAR)
    bg = small.filter(ImageFilter.GaussianBlur(10)).resize((W, H), Image.BILINEAR)
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    scale = min(W / im.width, H / im.height)
    fg = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg


def text_only_slide(t: SlideTexts) -> Image.Image:
    im = Image.new("RGB", (W, H), (34, 34, 38))
    im = draw_plates(im, t.top, top=STYLE["top_y"])
    return draw_plates(im, t.bottom, center_y=H // 2)


def render_cover(photo: Image.Image, t: SlideTexts, style: str = "A") -> Image.Image:
    if style == "B":
        im = draw_block(blur_cover(photo), [(t.city, False), (t.kind, False)] + ([(t.price, False)] if t.price else []),
                        top=WHITE["top_y"])
        return draw_block(im, [(t.bottom, False)], center_y=WHITE["bottom_cy"], style=WHITE_SMALL)
    if style == "C":
        mid = ", ".join(t.short) or t.price
        lines = [(t.kind, False)] + ([(mid, False)] if mid else []) + [(t.where, True)]
        return draw_block(cover(photo), lines, top=WHITE["top_y"])
    caps = style == "D"
    st = STYLE_CAPS if caps else STYLE
    im = draw_plates(cover(photo), t.top, top=st["top_y"], style=st)
    return draw_plates(im, t.bottom, center_y=st["bottom_cy"], style=st)


def _save(im: Image.Image, path: Path):
    tmp = path.with_suffix(".tmp")
    im.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=False, subsampling=0)
    os.replace(tmp, path)


def generate(lst: Listing, out_dir: str | Path, max_slides: int = 10,
             fetch: Callable[[str], bytes] = None, beat: Callable[[], None] = None,
             style: Optional[str] = None) -> list[Path]:
    """Download photos one by one and write slide_01.jpg ... into out_dir. Returns the paths in order.
    style: cover look 'A'..'D'; None = picked from the listing."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fetch = fetch or (lambda u: images.download(u, beat=beat))
    texts = slide_texts(lst)
    style = style or pick_style(lst)
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
            slide = render_cover(photo, texts, style) if first else fit_on_blur(photo)
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
        _save(text_only_slide(texts), p)
        paths.append(p)
    return paths

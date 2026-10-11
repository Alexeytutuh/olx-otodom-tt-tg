"""Image preparation for TikTok photo posts.

TikTok photo restrictions (https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
-> "Image restrictions"): formats WebP or JPEG (PNG is NOT listed), "Maximum 1080p",
max 20 MB per image; up to 35 images per post
(https://developers.tiktok.com/doc/content-posting-api-reference-photo-post).

We interpret "Maximum 1080p" conservatively as: the image must fit inside
1080x1920 (portrait) or 1920x1080 (landscape), i.e. short side <= 1080 and
long side <= 1920. Everything is re-encoded to baseline-safe JPEG (sRGB/RGB,
EXIF orientation applied, metadata stripped).
"""

from __future__ import annotations

import io
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

MAX_IMAGES = 35
MAX_SHORT_SIDE = 1080
MAX_LONG_SIDE = 1920
MAX_BYTES = 20 * 1024 * 1024
MIN_SIDE_WARN = 360  # not documented for photos; video minimum, used only as a warning
JPEG_QUALITY = 90

# Guard against decompression bombs in a 128 MB container. JPEG is decoded
# downscaled via draft(), other formats are decoded at full size.
Image.MAX_IMAGE_PIXELS = 50_000_000
# PNG/WebP/etc. are decoded at full size (Pillow decodes WebP to RGBA, ~15 B/px peak),
# so cap them to stay well inside mem_limit 128m together with the web server.
MAX_PIXELS_BY_FORMAT = {"JPEG": 50_000_000, "WEBP": 3_000_000}
MAX_PIXELS_OTHER = 5_000_000


@dataclass
class PreparedImage:
    source: str
    rel_path: str
    width: int
    height: int
    bytes: int
    warnings: list[str]


def _target_size(w: int, h: int) -> tuple[int, int]:
    short, long_ = min(w, h), max(w, h)
    scale = min(1.0, MAX_SHORT_SIDE / short, MAX_LONG_SIDE / long_)
    if scale >= 1.0:
        return w, h
    return max(1, round(w * scale)), max(1, round(h * scale))


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.getchannel("A"))
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def convert_image(src: Path) -> tuple[bytes, int, int, list[str]]:
    """Decode -> downscale (cheaply, before any full-size copies) -> rotate -> RGB -> JPEG.

    Memory matters (container mem_limit 128m): JPEGs are decoded with draft()
    (DCT-domain downscale up to 1/8), and the thumbnail is made before EXIF
    rotation / alpha flattening so no extra full-resolution copies are created.
    """
    warnings: list[str] = []
    with Image.open(src) as img:
        fmt = img.format or "?"
        w, h = img.size
        limit = MAX_PIXELS_BY_FORMAT.get(fmt, MAX_PIXELS_OTHER)
        if w * h > limit:
            raise ValueError(f"{src}: {fmt} {w}x{h} is too large to convert within the container memory limit "
                             f"(max {limit / 1e6:.1f} MP for {fmt}); downscale it or convert to JPEG first")
        if getattr(img, "n_frames", 1) > 1:
            warnings.append(f"{fmt} has {img.n_frames} frames; only the first is used")
        orientation = img.getexif().get(0x0112, 1)
        rotated = orientation in (5, 6, 7, 8)
        tw, th = _target_size(*((h, w) if rotated else (w, h)))
        box = (th, tw) if rotated else (tw, th)
        if img.mode not in ("RGB", "RGBA", "L", "LA"):
            img = img.convert("RGBA" if ("transparency" in img.info or img.mode in ("PA", "RGBa")) else "RGB")
        if fmt == "JPEG":
            img.draft("RGB", box)  # DCT-domain downscale to the smallest scale >= box
        if box != img.size:
            img.thumbnail(box, Image.Resampling.LANCZOS, reducing_gap=None)
        else:
            img.load()
        img = ImageOps.exif_transpose(img)
        img = _to_rgb(img)
        tw, th = img.size
        if min(tw, th) < MIN_SIDE_WARN:
            warnings.append(f"small image {tw}x{th}; TikTok may reject or upscale it")
        quality = JPEG_QUALITY
        while True:
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True, subsampling="4:2:0")
            data = buf.getvalue()
            if len(data) <= MAX_BYTES or quality <= 50:
                break
            quality -= 10
        if len(data) > MAX_BYTES:
            raise ValueError(f"{src}: cannot fit under 20 MB")
        return data, tw, th, warnings


def new_batch_dir(media_dir: Path) -> tuple[Path, str]:
    """Unguessable per-run directory so media URLs are not enumerable."""
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    path = media_dir / name
    path.mkdir(parents=True, mode=0o755)
    return path, name


def prepare_images(sources: list[Path], media_dir: Path) -> list[PreparedImage]:
    if not sources:
        raise ValueError("no images given")
    if len(sources) > MAX_IMAGES:
        raise ValueError(f"TikTok allows at most {MAX_IMAGES} photos per post (got {len(sources)})")
    for s in sources:
        if not s.is_file():
            raise FileNotFoundError(s)
    batch, batch_name = new_batch_dir(media_dir)
    out: list[PreparedImage] = []
    for i, src in enumerate(sources, start=1):
        data, w, h, warns = convert_image(src)
        name = f"{i:02d}.jpg"
        dest = batch / name
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.chmod(0o644)
        tmp.replace(dest)
        out.append(PreparedImage(str(src), f"{batch_name}/{name}", w, h, len(data), warns))
    return out


def _font(size: int) -> ImageFont.ImageFont:
    for cand in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def generate_demo_slides(dest_dir: Path, count: int = 3) -> list[Path]:
    """Plain 1080x1920 test slides (no logos/watermarks, per TikTok sharing guidelines)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    palette = [((30, 60, 114), (42, 82, 152)), ((17, 153, 142), (56, 239, 125)),
               ((142, 45, 226), (74, 0, 224)), ((240, 90, 40), (250, 160, 60))]
    paths = []
    big, small = _font(120), _font(56)
    for i in range(count):
        top, bottom = palette[i % len(palette)]
        img = Image.new("RGB", (1080, 1920))
        draw = ImageDraw.Draw(img)
        for y in range(1920):
            t = y / 1919
            draw.line([(0, y), (1080, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
        lines = [(f"{i + 1} / {count}", big), ("Test slide", small), ("Тестовий слайд", small)]
        y = 760
        for text, font in lines:
            bbox = draw.textbbox((0, 0), text, font=font)
            draw.text(((1080 - (bbox[2] - bbox[0])) / 2, y), text, font=font, fill=(255, 255, 255))
            y += (bbox[3] - bbox[1]) + 60
        p = dest_dir / f"demo_{i + 1}.png"
        img.save(p, "PNG")
        paths.append(p)
    return paths

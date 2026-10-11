"""Photo download + light processing with Pillow, one image at a time (low memory).
- telegram_jpeg(): fit into 1280 px, JPEG q85 (what Telegram keeps anyway)
- dhash(): 64-bit difference hash of a photo for duplicate detection (no extra deps)"""
from __future__ import annotations

import io
import logging
import re
import threading
from typing import Callable, Optional

from PIL import Image, ImageOps

log = logging.getLogger("olxbot.images")
Image.MAX_IMAGE_PIXELS = 60_000_000           # refuse decompression bombs (~7700x7700)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

try:
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # pragma: no cover
    cffi_requests = None

_local = threading.local()


def sized_url(url: str, box: str) -> str:
    """OLX/Otodom CDN (apollo.olxcdn.com) serves '.../image;s=WxH[;q=80]': ask for a smaller box."""
    if "olxcdn.com" in url and ";s=" in url:
        return re.sub(r";s=\d+x\d+", f";s={box}", url)
    return url


def _session():
    s = getattr(_local, "session", None)
    if s is None and cffi_requests is not None:
        s = _local.session = cffi_requests.Session(impersonate="chrome")
    return s


def download(url: str, timeout: float = 25, beat: Optional[Callable[[], None]] = None) -> bytes:
    """GET an image. Raises on failure. Accept header without webp -> CDN returns JPEG."""
    headers = {"Accept": "image/jpeg,image/png,image/*;q=0.8", "Accept-Language": "pl-PL,pl;q=0.9"}
    if beat:
        beat()
    s = _session()
    if s is not None:
        r = s.get(url, headers=headers, timeout=(10, timeout))
        if r.status_code != 200:
            raise OSError(f"HTTP {r.status_code} for photo")
        data = r.content
    else:  # pragma: no cover - fallback without curl_cffi
        import urllib.request
        req = urllib.request.Request(url, headers={**headers, "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise OSError("photo too large")
    if beat:
        beat()
    return data


def open_image(data: bytes, want: Optional[tuple[int, int]] = None) -> Image.Image:
    """Decode bytes to RGB, honour EXIF rotation. `want` lets JPEG decode at a reduced scale (draft mode)
    so a 4000x3000 original never needs its full 36 MB bitmap."""
    im = Image.open(io.BytesIO(data))
    if want and im.format == "JPEG":
        im.draft("RGB", want)
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
    return im


def telegram_jpeg(data: bytes, max_side: int = 1280, quality: int = 85) -> tuple[bytes, Image.Image]:
    """Returns (jpeg_bytes, small_image_for_hashing)."""
    im = open_image(data, (max_side, max_side))
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), im


def dhash(im: Image.Image, size: int = 8) -> str:
    """Difference hash: grayscale (size+1)x size, compare neighbours -> 64 bits as 16 hex chars."""
    g = im.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def dhash_from_url(url: str, beat=None) -> Optional[str]:
    try:
        data = download(sized_url(url, "400x400"), beat=beat)
        im = open_image(data, (200, 200))
        return dhash(im)
    except Exception as e:
        log.info("dhash failed for %s: %s", url[:80], e)
        return None

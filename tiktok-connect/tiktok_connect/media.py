"""Safe static serving of carousel images from DATA_DIR/media.

- only .jpg/.jpeg/.png/.webp, no directory listing, no dotfiles
- path traversal / symlink escape blocked via realpath containment check
- TikTok PULL_FROM_URL requires https URLs that do NOT redirect (3xx = invalid):
  https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def resolve_media_path(media_root: Path, url_path: str) -> Path | None:
    """Map the part after /media/ to a file, or None if not allowed/not found."""
    try:
        rel = urllib.parse.unquote(url_path, errors="strict")
    except UnicodeDecodeError:
        return None
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if len(parts) > 4 or any(not _SAFE_SEGMENT.match(p) or ".." in p for p in parts):
        return None
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext not in CONTENT_TYPES:
        return None
    root = os.path.realpath(media_root)
    full = os.path.realpath(os.path.join(root, *parts))
    if os.path.commonpath([root, full]) != root or full == root:
        return None
    if not os.path.isfile(full):
        return None
    return Path(full)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """Return (start, end) inclusive, None for no/ignored range, 'invalid' for 416."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    spec = header[6:].strip()
    try:
        start_s, end_s = spec.split("-", 1)
        if start_s == "":
            n = int(end_s)
            if n <= 0:
                return "invalid"
            return max(0, size - n), size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size or start < 0 or end < start:
        return "invalid"
    return start, min(end, size - 1)

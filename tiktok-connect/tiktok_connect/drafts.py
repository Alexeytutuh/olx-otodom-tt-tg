"""Create a TikTok photo draft (MEDIA_UPLOAD) from image files — used by POST /internal/draft (the bot).

Same steps as test_draft.main, but returns a result dict instead of printing.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import Config
from .images import MAX_IMAGES, prepare_images
from .test_draft import DESCRIPTION_MAX, HINTS, MIN_POLL_INTERVAL, TITLE_MAX, check_public_url, utf16_len
from .tiktok_api import TikTokError
from .tokens import NotConnected, TokenManager

log = logging.getLogger("tiktok_connect.drafts")


def _fail(error: str, message: str) -> dict:
    return {"ok": False, "error": error, "message": message, "hint": HINTS.get(error, "")}


def create_draft(config: Config, tm: TokenManager, sources: list[Path], title: str, description: str,
                 open_id: str | None = None, timeout: float = 60.0, poll_interval: float = 2.0) -> dict:
    """-> {"ok": True, "publish_id", "status"} or {"ok": False, "error", "message", "hint"}."""
    if not 1 <= len(sources) <= MAX_IMAGES:
        return _fail("invalid_param", f"need 1..{MAX_IMAGES} images, got {len(sources)}")
    if utf16_len(title) > TITLE_MAX or utf16_len(description) > DESCRIPTION_MAX:
        return _fail("invalid_param", "title or description too long")
    try:
        account = tm.get_valid_account(open_id)
    except NotConnected as exc:
        return _fail("not_connected", str(exc))
    except TikTokError as exc:
        return _fail(exc.code, exc.describe())
    open_id = account["open_id"]
    if "video.upload" not in (account.get("scope") or "").split(","):
        return _fail("scope_not_authorized", "token lacks video.upload")

    try:
        prepared = prepare_images(sources, config.media_dir)
    except (ValueError, OSError) as exc:
        return _fail("image_preparation_failed", str(exc))
    urls = [config.media_url(p.rel_path) for p in prepared]
    bad = [f"{u}: {err}" for u in urls if (err := check_public_url(u))]
    if bad:
        return _fail("photo_pull_failed", "public URL check failed: " + "; ".join(bad[:2]))

    post_info = {k: v for k, v in (("title", title), ("description", description)) if v}
    payload = {"media_type": "PHOTO", "post_mode": "MEDIA_UPLOAD", "post_info": post_info,
               "source_info": {"source": "PULL_FROM_URL", "photo_images": urls, "photo_cover_index": 0}}
    try:
        try:
            data = tm.client.init_photo_post(account["access_token"], payload)
        except TikTokError as exc:
            if exc.code != "access_token_invalid":
                raise
            account = tm.refresh_account(open_id, force=True)
            data = tm.client.init_photo_post(account["access_token"], payload)
    except TikTokError as exc:
        return _fail(exc.code, exc.describe())
    except NotConnected as exc:
        return _fail("not_connected", str(exc))
    publish_id = data.get("publish_id")
    if not publish_id:
        return _fail("no_publish_id", f"content/init returned {data}")
    log.info("draft init OK publish_id=%s images=%d", publish_id, len(urls))

    deadline = time.monotonic() + timeout
    status = "PROCESSING_DOWNLOAD"
    while time.monotonic() < deadline:
        time.sleep(max(poll_interval, MIN_POLL_INTERVAL))
        try:
            st = tm.client.fetch_status(tm.get_valid_account(open_id)["access_token"], publish_id)
        except TikTokError as exc:
            if exc.retryable:
                continue
            return _fail(exc.code, exc.describe())
        status = st.get("status", "?")
        if status == "FAILED":
            reason = st.get("fail_reason", "") or "failed"
            return _fail(reason, f"TikTok status FAILED: {reason}")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            log.info("draft %s delivered: %s", publish_id, status)
            return {"ok": True, "publish_id": publish_id, "status": status}
    # still processing after the timeout: TikTok usually finishes later on its own
    return {"ok": True, "publish_id": publish_id, "status": status}

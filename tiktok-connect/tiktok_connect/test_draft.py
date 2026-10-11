"""Send a photo carousel to the connected TikTok account as a DRAFT (MEDIA_UPLOAD).

Usage (inside the container):
  docker exec -it tiktok-connect python -m tiktok_connect.test_draft --demo
  docker exec -it tiktok-connect python -m tiktok_connect.test_draft \
      --images /data/in/1.jpg /data/in/2.png --title "iPhone 13" --description "Продам… #olx"

Docs: https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
      https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import ConfigError, load_config, setup_logging
from .images import MAX_IMAGES, generate_demo_slides, prepare_images
from .tiktok_api import TikTokError
from .tokens import NotConnected, TokenManager, mask

TITLE_MAX = 90         # UTF-16 code units, photo posts
DESCRIPTION_MAX = 4000  # UTF-16 code units, photo posts
MIN_POLL_INTERVAL = 2.0  # status/fetch is limited to 30 req/min per user token

HINTS = {
    "url_ownership_unverified": "Domain/URL prefix of the image URLs is not verified for this app (Sandbox "
                                "and Production have separate URL properties — check the Sandbox 'test').",
    "scope_not_authorized": "User did not grant video.upload — reconnect on the web page and allow all permissions.",
    "access_token_invalid": "Token invalid/expired — reconnect on the web page.",
    "spam_risk_too_many_pending_share": "Max 5 pending (unposted) shares per 24h for this user — open/finish or "
                                        "delete earlier drafts in TikTok inbox, or wait.",
    "app_version_check_failed": "TikTok app on the phone must be version 31.8 or newer for MEDIA_UPLOAD.",
    "unaudited_client_can_only_post_to_private_accounts": "Unaudited client: the TikTok account must be set to "
                                                          "private (Settings → Privacy → Private account).",
    "rate_limit_exceeded": "content/init is limited to 6 requests/min per user token; wait a minute.",
    "invalid_param": "Check message; typical causes: >35 images, bad cover index, too long title/description.",
    "file_format_check_failed": "Unsupported image format (only JPEG/WebP).",
    "picture_size_check_failed": "Image dimensions not accepted (max 1080p).",
    "photo_pull_failed": "TikTok could not download the image URL (must be public https, no redirects, no "
                         "Cloudflare bot challenge).",
    "spam_risk_text": "Description text flagged as risky/spammy by TikTok.",
    "auth_removed": "User removed the app's access during processing.",
}

log = logging.getLogger("tiktok_connect.test_draft")


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def check_public_url(url: str, timeout: int = 15) -> str | None:
    """Return None if URL serves an image with 200 and no redirect, else an error string."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "tiktok-connect-urlcheck"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if resp.status != 200:
                return f"HTTP {resp.status}"
            if not ctype.startswith("image/"):
                return f"unexpected Content-Type {ctype!r}"
            return None
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}" + (" (redirect — TikTok does not follow redirects)" if 300 <= exc.code < 400 else "")
    except (urllib.error.URLError, OSError) as exc:
        return f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"


def out(msg: str = "") -> None:
    print(msg, flush=True)


def explain(exc: TikTokError) -> str:
    hint = HINTS.get(exc.code)
    return exc.describe() + (f"\n    hint: {hint}" if hint else "")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tiktok_connect.test_draft",
                                description="Upload a TikTok photo carousel as a draft (MEDIA_UPLOAD, PULL_FROM_URL).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--images", nargs="+", type=Path, help=f"1..{MAX_IMAGES} image files (jpg/png/webp/…)")
    src.add_argument("--demo", action="store_true", help="generate 3 sample 1080x1920 slides")
    p.add_argument("--demo-count", type=int, default=3, help="number of demo slides (default 3)")
    p.add_argument("--title", default=None, help=f"post title (max {TITLE_MAX} UTF-16 units)")
    p.add_argument("--description", default=None, help=f"caption/description (max {DESCRIPTION_MAX} UTF-16 units)")
    p.add_argument("--cover-index", type=int, default=0, help="0-based index of the cover photo (default 0)")
    p.add_argument("--open-id", default=None, help="which connected account (default: last connected)")
    p.add_argument("--aigc", action="store_true", help="set is_aigc=true (AI-generated label)")
    p.add_argument("--creator-info", action="store_true",
                   help="also call creator_info/query (needs video.publish; expected to fail with video.upload only)")
    p.add_argument("--skip-url-check", action="store_true", help="don't HEAD-check public image URLs first")
    p.add_argument("--dry-run", action="store_true", help="prepare images and print payload, don't call TikTok")
    p.add_argument("--poll-interval", type=float, default=5.0, help="seconds between status polls (default 5)")
    p.add_argument("--timeout", type=float, default=300.0, help="max seconds to poll (default 300)")
    p.add_argument("--wait-publish", action="store_true",
                   help="keep polling after SEND_TO_USER_INBOX until the user publishes (PUBLISH_COMPLETE)")
    return p


def poll_status(tm: TokenManager, open_id: str, publish_id: str, interval: float, timeout: float,
                wait_publish: bool) -> int:
    interval = max(interval, MIN_POLL_INTERVAL)
    deadline = time.monotonic() + timeout
    last = None
    while True:
        account = tm.get_valid_account(open_id)
        try:
            data = tm.client.fetch_status(account["access_token"], publish_id)
        except TikTokError as exc:
            if exc.retryable and time.monotonic() < deadline:
                out(f"  status fetch error (will retry): {exc.describe()}")
                time.sleep(interval * 2)
                continue
            out(f"✗ status fetch failed: {explain(exc)}")
            return 1
        status = data.get("status", "?")
        if status != last:
            extra = []
            if data.get("downloaded_bytes") is not None:
                extra.append(f"downloaded_bytes={data['downloaded_bytes']}")
            if data.get("fail_reason"):
                extra.append(f"fail_reason={data['fail_reason']}")
            ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id")
            if ids:
                extra.append(f"post_ids={ids}")
            out(f"  [{time.strftime('%H:%M:%S')}] status={status} {' '.join(extra)}".rstrip())
            last = status
        if status == "FAILED":
            reason = data.get("fail_reason", "")
            out(f"✗ FAILED: {reason}" + (f"\n    hint: {HINTS[reason]}" if reason in HINTS else ""))
            return 1
        if status == "PUBLISH_COMPLETE":
            out("✓ PUBLISH_COMPLETE — the user has posted it from the draft.")
            return 0
        if status == "SEND_TO_USER_INBOX" and not wait_publish:
            out("✓ SEND_TO_USER_INBOX — draft delivered. Open TikTok app → Inbox (notification from the app) "
                "to edit and post.")
            return 0
        if time.monotonic() >= deadline:
            out(f"… timed out after {timeout:.0f}s with status={status}. Re-check later with --timeout.")
            return 3
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    try:
        config = load_config(require_secrets=True)
    except ConfigError as exc:
        out(f"✗ config error: {exc}")
        return 2

    if args.title is not None and utf16_len(args.title) > TITLE_MAX:
        out(f"✗ title too long: {utf16_len(args.title)} > {TITLE_MAX} UTF-16 units")
        return 2
    if args.description is not None and utf16_len(args.description) > DESCRIPTION_MAX:
        out(f"✗ description too long: {utf16_len(args.description)} > {DESCRIPTION_MAX} UTF-16 units")
        return 2

    tm = TokenManager(config)
    open_id = None
    if not args.dry_run:
        try:
            account = tm.get_valid_account(args.open_id)
        except (NotConnected, TikTokError) as exc:
            out(f"✗ {exc}")
            return 1
        open_id = account["open_id"]
        out(f"Account: {account.get('display_name') or '?'} (open_id {mask(open_id, 6)}), scopes: {account.get('scope')}")
        if "video.upload" not in (account.get("scope") or "").split(","):
            out("✗ token lacks video.upload scope — reconnect and allow uploading.")
            return 1
        if args.creator_info:
            try:
                ci = tm.client.creator_info(account["access_token"])
                out("creator_info: " + json.dumps(ci, ensure_ascii=False))
            except TikTokError as exc:
                out(f"creator_info not available (expected for MEDIA_UPLOAD-only apps): {explain(exc)}")

    with tempfile.TemporaryDirectory(prefix="tt-demo-") as tmp:
        if args.demo:
            sources = generate_demo_slides(Path(tmp), max(1, min(args.demo_count, MAX_IMAGES)))
            if args.description is None:
                args.description = "Test carousel from Listing Publisher 🧪 #test"
            if args.title is None:
                args.title = "Тест каруселі / Carousel test"
        else:
            sources = args.images
        try:
            prepared = prepare_images(sources, config.media_dir)
        except (ValueError, OSError) as exc:
            out(f"✗ image preparation failed: {exc}")
            return 2

    if not 0 <= args.cover_index < len(prepared):
        out(f"✗ --cover-index must be within 0..{len(prepared) - 1}")
        return 2

    urls = [config.media_url(p.rel_path) for p in prepared]
    out(f"Prepared {len(prepared)} image(s):")
    for p, u in zip(prepared, urls, strict=True):
        out(f"  {p.width}x{p.height} {p.bytes // 1024} KiB  {u}" + "".join(f"\n    ! {w}" for w in p.warnings))

    if not args.skip_url_check and not args.dry_run:
        bad = [(u, err) for u in urls if (err := check_public_url(u))]
        if bad:
            out("✗ public URL check failed (TikTok must be able to GET these over https without redirects):")
            for u, err in bad:
                out(f"    {u}: {err}")
            out("  Use --skip-url-check to bypass.")
            return 1
        out("Public URL check: OK")

    post_info: dict = {}
    if args.title:
        post_info["title"] = args.title
    if args.description:
        post_info["description"] = args.description
    payload = {
        "media_type": "PHOTO",
        "post_mode": "MEDIA_UPLOAD",
        "post_info": post_info,
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_images": urls,
            "photo_cover_index": args.cover_index,
        },
    }
    if args.aigc:
        payload["is_aigc"] = True
    out("Request payload:\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    if args.dry_run:
        out("(dry run — not calling TikTok)")
        return 0

    try:
        account = tm.get_valid_account(open_id)
        try:
            data = tm.client.init_photo_post(account["access_token"], payload)
        except TikTokError as exc:
            if exc.code != "access_token_invalid":
                raise
            out("  access_token_invalid → forcing refresh and retrying once")
            account = tm.refresh_account(open_id, force=True)
            data = tm.client.init_photo_post(account["access_token"], payload)
    except (TikTokError, NotConnected) as exc:
        out(f"✗ content/init failed: {explain(exc) if isinstance(exc, TikTokError) else exc}")
        return 1
    publish_id = data.get("publish_id")
    if not publish_id:
        out(f"✗ content/init returned no publish_id: {data}")
        return 1
    out(f"✓ content/init OK, publish_id={publish_id}\nPolling status…")
    return poll_status(tm, open_id, publish_id, args.poll_interval, args.timeout, args.wait_publish)


if __name__ == "__main__":
    sys.exit(main())

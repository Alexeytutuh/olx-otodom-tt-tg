"""Render sample output from real current listings: TikTok slides, post text, info card, TikTok caption,
and a PNG mock of how the Telegram post looks.

    python tools/render_samples.py                 # fetch live (OLX + Otodom) and pick 3 listings
    python tools/render_samples.py --json a.json   # use saved normalized listings (list or parser --json output)"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import time
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BOT_DIR), str(BOT_DIR.parent)]

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from parsers.models import Listing  # noqa: E402
from olxbot import formatting as F, images, slides  # noqa: E402


def load_live() -> list[Listing]:
    from parsers.olx import OlxParser
    from parsers.otodom import OtodomParser
    out = []
    out += OlxParser().fetch_new(set())
    out += OtodomParser(max_details=12).fetch_new(set())
    return out


def pick(ls: list[Listing], n: int = 3) -> list[Listing]:
    def score(l):
        lines, razem = F.cost_lines(l)
        return (len(lines) + (2 if razem else 0) + (2 if F.key_feature(l) else 0) + (1 if l.street else 0)
                + (1 if l.kaucja_pln else 0) + min(len(l.photos), 8) / 4)
    chosen, used_rooms, used_src = [], set(), {}
    for l in sorted(ls, key=score, reverse=True):
        if l.rooms in used_rooms or len(l.photos) < 3 or used_src.get(l.source, 0) >= 2:
            continue
        chosen.append(l)
        used_rooms.add(l.rooms)
        used_src[l.source] = used_src.get(l.source, 0) + 1
        if len(chosen) == n:
            break
    return chosen


# ----------------------------------------------------------------------------- Telegram mock
FONT = str(BOT_DIR / "olxbot" / "fonts" / "Montserrat-SemiBold.ttf")
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _f(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(FONT, size)


def telegram_mock(post_html: str, info_html: str, thumbs: list[Image.Image], path: Path):
    W, pad = 600, 16
    reg, bold, small = _f(REG, 17), _f(BOLD, 17), _f(REG, 14)
    grid_h = 0
    tiles = []
    if thumbs:
        cols = 2 if len(thumbs) < 5 else 3
        tw = (W - 2 * pad - (cols - 1) * 3) // cols
        th = int(tw * 0.75)
        first_h = int((W - 2 * pad) * 0.6)
        grid_h = first_h + 3 + ((len(thumbs) - 2) // cols + 1) * (th + 3) if len(thumbs) > 1 else first_h
        tiles = (first_h, tw, th, cols)
    lines = []
    for raw in post_html.split("\n"):
        b = raw.startswith("<b>")
        lines.append((html.unescape(re.sub(r"<[^>]+>", "", raw)), b))
    emoji = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2194-\u21AA\u2753-\u2757]+")
    info_lines = [emoji.sub("•", html.unescape(re.sub(r"<[^>]+>", "", x))) for x in info_html.split("\n")]
    # wrap
    def wrap(text, font, width):
        out, cur = [], ""
        for w in text.split(" "):
            c = f"{cur} {w}".strip()
            if font.getlength(c) <= width:
                cur = c
            else:
                out.append(cur)
                cur = w
        out.append(cur)
        return out
    body = []
    for t, b in lines:
        for x in wrap(t, bold if b else reg, W - 2 * pad - 20):
            body.append((x, b))
    info = [x for t in info_lines for x in wrap(t, small, W - 2 * pad - 20)]
    H = pad + grid_h + 12 + len(body) * 24 + 30 + len(info) * 20 + 50 + pad
    im = Image.new("RGB", (W, H), (171, 196, 140))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([pad - 6, pad - 6, W - pad + 6, pad + grid_h + 12 + len(body) * 24 + 10], 14, fill="white")
    if thumbs:
        first_h, tw, th, cols = tiles
        t0 = thumbs[0].copy()
        from PIL import ImageOps
        im.paste(ImageOps.fit(t0, (W - 2 * pad, first_h)), (pad, pad))
        for i, t in enumerate(thumbs[1:]):
            r, c = divmod(i, cols)
            im.paste(ImageOps.fit(t, (tw, th)), (pad + c * (tw + 3), pad + first_h + 3 + r * (th + 3)))
    y = pad + grid_h + 12
    for t, b in body:
        d.text((pad + 8, y), t, font=bold if b else reg, fill=(20, 20, 20))
        y += 24
    y += 22
    d.rounded_rectangle([pad - 6, y - 8, W - pad + 6, y + len(info) * 20 + 50], 14, fill="white")
    for t in info:
        d.text((pad + 8, y), t, font=small, fill=(40, 40, 40))
        y += 20
    bw = (W - 2 * pad - 8) // 2
    for i, label in enumerate(("Згенерувати", "Посередник")):
        x = pad + i * (bw + 8)
        d.rounded_rectangle([x, y + 6, x + bw, y + 38], 10, fill=(120, 150, 100))
        d.text((x + bw // 2, y + 22), label, font=reg, fill="white", anchor="mm")
    im.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", nargs="*")
    ap.add_argument("--out", default=str(BOT_DIR / "samples"))
    ap.add_argument("-n", type=int, default=3)
    a = ap.parse_args()
    if a.json:
        ls = []
        for p in a.json:
            d = json.load(open(p, encoding="utf-8"))
            ls += [Listing.from_dict(x) for x in (d["listings"] if isinstance(d, dict) else d)]
    else:
        ls = load_live()
    chosen = pick(ls, a.n)
    out = Path(a.out)
    for old in out.glob("sample_*"):
        shutil.rmtree(old, ignore_errors=True)
    all_txt = []
    for i, l in enumerate(chosen, 1):
        d = out / f"sample_{i}_{l.source}_{l.source_id}"
        d.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        paths = slides.generate(l, d / "slides")
        dt_s = time.time() - t0
        post = F.build_post(l)
        info = F.build_info(l)
        cap = F.tiktok_caption(l)
        txt = (f"=== {l.url}\n\n--- POST (Telegram caption, HTML -> shown bold) ---\n{post.plain}\n\n"
               f"--- POST HTML ---\n{post.html}\n\n--- INFO CARD (with buttons) ---\n"
               f"{html.unescape(re.sub(r'<[^>]+>', '', info))}\n\n--- TIKTOK CAPTION ---\n{cap}\n\n"
               f"slides: {len(paths)} in {dt_s:.1f}s; caption fits 1024: {post.fits_caption} "
               f"({F.utf16_len(post.plain)} chars)\n")
        (d / "post.txt").write_text(txt, encoding="utf-8")
        all_txt.append(txt)
        thumbs = []
        for p in paths[:10]:
            im = Image.open(p)
            im.thumbnail((400, 400))
            thumbs.append(im.copy())
        # mock uses the album photos (not the slides): re-download small versions
        album = []
        for u in l.photos[:10]:
            try:
                album.append(images.open_image(images.download(images.sized_url(u, "640x640")), (640, 640)))
            except Exception:
                pass
        telegram_mock(post.html, info, album or thumbs, d / "telegram_mock.png")
        # contact sheet of slides
        sheet = Image.new("RGB", (len(paths) * 216 + 8, 392), "white")
        for k, p in enumerate(paths):
            im = Image.open(p).resize((208, 370))
            sheet.paste(im, (8 + k * 216, 11))
        sheet.save(d / "slides_overview.jpg", quality=85)
        print(f"{d}: {len(paths)} slides, {dt_s:.1f}s")
    (out / "post.txt").write_text("\n\n".join(all_txt), encoding="utf-8")


if __name__ == "__main__":
    main()

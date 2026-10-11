#!/usr/bin/env bash
# Assemble the deployable folder (bot + parsers) and zip it to /workspace/olx-tiktok-bot.zip
set -euo pipefail
BOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_PARSERS="$BOT/../parsers"
OUT_ZIP="${1:-/workspace/olx-tiktok-bot.zip}"
STAGE="$(mktemp -d)/olx-tiktok-bot"
mkdir -p "$STAGE"
cp -a "$BOT"/{Dockerfile,docker-compose.yml,.env.example,.dockerignore,requirements.txt,INSTALL_UA.md,README_UA.md} "$STAGE"/
cp -a "$BOT"/olxbot "$BOT"/tests "$BOT"/tools "$BOT"/deploy "$STAGE"/
mkdir -p "$STAGE/parsers"
# parsers without their own test fixtures (3 MB); the bot's tests use their own fixtures
( cd "$SRC_PARSERS" && cp -a __init__.py cli.py common.py config.py extract.py filters.py models.py olx.py otodom.py \
    smoke_live.py requirements.txt README_parsers.md REVIEW.md "$STAGE/parsers/" )
mkdir -p "$STAGE/samples"
cp -a "$BOT"/samples/post.txt "$STAGE/samples/" 2>/dev/null || true
for d in "$BOT"/samples/sample_*; do
  [ -d "$d" ] || continue
  n="$(basename "$d")"; mkdir -p "$STAGE/samples/$n"
  cp -a "$d"/post.txt "$d"/telegram_mock.png "$d"/slides_overview.jpg "$STAGE/samples/$n/" 2>/dev/null || true
done
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 
find "$STAGE" -name '*.pyc' -delete
rm -f "$OUT_ZIP"
python3 - "$STAGE" "$OUT_ZIP" <<'PY'
import os, sys, zipfile
stage, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, dirs, files in os.walk(stage):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            rel = os.path.relpath(p, stage)
            if rel == ".env" or rel.startswith("data/"):
                continue
            info = zipfile.ZipInfo.from_file(p, rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            if f.endswith(".sh"):
                info.external_attr = (0o755 << 16)
            with open(p, "rb") as fh:
                z.writestr(info, fh.read())
PY
echo "built $OUT_ZIP ($(du -h "$OUT_ZIP" | cut -f1))"
unzip -l "$OUT_ZIP" | tail -1

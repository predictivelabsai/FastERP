#!/usr/bin/env bash
# Build the animated demo GIF from per-screen PNG frames.
#
# Capture frames first (e.g. via Playwright MCP) into docs/demo/frames/ named
# in display order: 01-dashboard.png, 02-deals.png, …  Then run this script.
#
#   bash scripts/build_demo_gif.sh
#
# Output: docs/demo/fasterp-walkthrough.gif  (1 frame ≈ 1.6s, looping)
#
# Generic across the fasthtml-oss-migrations repos — only the output name and
# paths differ. Uses ImageMagick `convert`; falls back to ffmpeg if present.
set -euo pipefail
cd "$(dirname "$0")/.."

FRAMES_DIR="docs/demo/frames"
OUT="docs/demo/fasterp-walkthrough.gif"
DELAY="${DELAY:-160}"        # hundredths of a second between frames
WIDTH="${WIDTH:-1100}"       # downscale width for a smaller GIF

if ! ls "$FRAMES_DIR"/*.png >/dev/null 2>&1; then
  echo "No frames in $FRAMES_DIR/. Capture screenshots there first." >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"

if command -v convert >/dev/null 2>&1; then
  convert -loop 0 -delay "$DELAY" \
    -resize "${WIDTH}x" \
    "$FRAMES_DIR"/*.png \
    -layers Optimize "$OUT"
elif command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -y -framerate "$(awk "BEGIN{print 100/$DELAY}")" \
    -pattern_type glob -i "$FRAMES_DIR/*.png" \
    -vf "scale=${WIDTH}:-1:flags=lanczos" "$OUT"
else
  echo "Need ImageMagick (convert) or ffmpeg installed." >&2
  exit 1
fi

echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"

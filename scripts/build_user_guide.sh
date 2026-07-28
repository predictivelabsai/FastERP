#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" != "" ]; then
  SOURCES=("$1")
else
  SOURCES=(
    "docs/FastERP_user_guide_2026-07-28.md"
    "docs/FastERP_user_guide_2026-07-28_ee.md"
  )
fi

for SOURCE in "${SOURCES[@]}"; do
  BASE="${SOURCE%.md}"
  HTML="${BASE}.html"
  TITLE="FastERP Accounting Workspace User Guide"
  CSS="assets/guide.css"
  if [[ "$SOURCE" == *_ee.md ]]; then
    TITLE="FastERP raamatupidamise tööruumi kasutusjuhend"
    CSS="assets/guide_ee.css"
  fi

  pandoc "$SOURCE" -s -o "$HTML" \
    --from=markdown-implicit_figures \
    --css "$CSS" \
    --metadata pagetitle="$TITLE"
  weasyprint "$HTML" "${BASE}.pdf"
  rm -f "$HTML"
  .venv/bin/python scripts/build_pptx.py "$SOURCE" "${BASE}.pptx"
  echo "Built ${BASE}.pdf and ${BASE}.pptx"
done

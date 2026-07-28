#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

SOURCE="${1:-docs/FastERP_user_guide_2026-07-28.md}"
BASE="${SOURCE%.md}"
HTML="${BASE}.html"

pandoc "$SOURCE" -s -o "$HTML" \
  --from=markdown-implicit_figures \
  --css assets/guide.css \
  --metadata pagetitle="FastERP Accounting Workspace User Guide"
weasyprint "$HTML" "${BASE}.pdf"
rm -f "$HTML"
.venv/bin/python scripts/build_pptx.py "$SOURCE" "${BASE}.pptx"

echo "Built ${BASE}.pdf and ${BASE}.pptx"

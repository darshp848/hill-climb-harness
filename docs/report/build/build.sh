#!/bin/bash
# Build docs/TECHNICAL_REPORT.pdf and docs/TECHNICAL_REPORT.docx from the Markdown report.
# Needs: pandoc, XeLaTeX + DejaVu fonts (PDF), Node + mermaid-cli and a Chromium (docx diagrams), Python with lxml.
#   MMDC=... PUPPETEER_CONFIG=... PYTHON=... docs/report/build/build.sh [pdf|docx|all]
set -euo pipefail
cd "$(dirname "$0")/../.."          # docs/
B=report/build
SRC=(TECHNICAL_REPORT.md report/0*.md report/1*.md)
PY=${PYTHON:-python3}
what=${1:-all}
if [[ $what == pdf || $what == all ]]; then
  pandoc "${SRC[@]}" -o TECHNICAL_REPORT.pdf --pdf-engine=xelatex --toc --lua-filter=$B/pandoc_math.lua \
    -H $B/pdf_header.tex -V geometry:margin=0.75in -V mainfont="DejaVu Serif" -V monofont="DejaVu Sans Mono" \
    -V fontsize=10pt -V title="Hill Climb: technical report and practical guide"
fi
if [[ $what == docx || $what == all ]]; then
  export MERMAID_OUT=${MERMAID_OUT:-$(mktemp -d)}
  pandoc "${SRC[@]}" -o TECHNICAL_REPORT.docx --toc --toc-depth=2 --lua-filter=$B/docx_cleanup.lua \
    --lua-filter=$B/mermaid_png.lua --lua-filter=$B/pandoc_math.lua --reference-doc=$B/reference.docx \
    --metadata-file=$B/meta.yaml
  "$PY" $B/fix_docx.py TECHNICAL_REPORT.docx
fi

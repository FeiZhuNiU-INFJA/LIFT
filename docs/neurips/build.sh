#!/usr/bin/env bash
# Build docs/neurips/main.pdf.
# Prefers tectonic (single-binary, auto-fetches TeX packages).
# Falls back to latexmk (needs a local TeX Live / MiKTeX install).
#
# Usage:
#   bash build.sh            # build once
#   bash build.sh --watch    # rebuild on every save of main.tex / refs.bib

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

build_once() {
    if command -v tectonic >/dev/null 2>&1; then
        echo "[build] tectonic"
        tectonic -X compile main.tex --outdir .
    elif command -v latexmk >/dev/null 2>&1; then
        echo "[build] latexmk (pdflatex + bibtex)"
        latexmk -pdf -bibtex -interaction=nonstopmode main.tex
    else
        cat >&2 <<EOF
[build] error: no LaTeX engine found.

Install one of:
  * tectonic  (recommended, single binary, ~50MB)
      macOS:   brew install tectonic
      Linux:   curl --proto '=https' --tlsv1.2 -fsSL https://drop-sh.fullyjustified.net | sh
      Windows: winget install TectonicProject.Tectonic
  * TeX Live  (full distribution, ~4GB, then use \`latexmk\`)
      https://www.tug.org/texlive/

Then re-run: bash docs/neurips/build.sh
EOF
        exit 1
    fi
    echo "[build] done -> $HERE/main.pdf"
}

# Portable mtime (macOS uses BSD stat, Linux uses GNU stat)
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }

watch_loop() {
    echo "[watch] watching main.tex, refs.bib. Ctrl+C to stop."
    build_once || echo "[watch] initial build failed; will retry on next change"
    local last_tex last_bib cur_tex cur_bib
    last_tex="$(mtime main.tex)"
    last_bib="$(mtime refs.bib)"
    while true; do
        sleep 1
        cur_tex="$(mtime main.tex)"
        cur_bib="$(mtime refs.bib)"
        if [[ "$cur_tex" != "$last_tex" || "$cur_bib" != "$last_bib" ]]; then
            last_tex="$cur_tex"
            last_bib="$cur_bib"
            echo "[watch] change detected -> rebuild"
            build_once || echo "[watch] build failed; still watching"
        fi
    done
}

if [[ "${1:-}" == "--watch" ]]; then
    watch_loop
else
    build_once
fi


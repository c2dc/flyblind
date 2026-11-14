#!/usr/bin/env bash
##################################################################################################################################
# Script: build_fig_teaser.sh (build fig:teaser PDF/PNG)                                                                         #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Compiles fig_teaser.tex via pdflatex; writes ../pdfs/fig_teaser.pdf and optional PNG.                             #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   bash build_fig_teaser.sh                                                                                                     #
##################################################################################################################################

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PDFS="$(cd "$ROOT/../pdfs" && pwd)"
PNGS="$(cd "$ROOT/../pngs" && pwd)"
cd "$ROOT"
python3 generate_fig_teaser_numbers.py   # writes fig_teaser_numbers.tex from the run artifacts
pdflatex -interaction=nonstopmode fig_teaser.tex >/tmp/fig_teaser_build.log 2>&1 || {
  echo "BUILD FAIL"; tail -40 /tmp/fig_teaser_build.log; exit 1
}
# Paper version is cropped to the ink with a 2 pt margin (reproduces Paper Tex/pdfs/fig_teaser.pdf)
pdfcrop --margins 2 fig_teaser.pdf fig_teaser_crop.pdf >/dev/null && mv -f fig_teaser_crop.pdf fig_teaser.pdf
cp -f fig_teaser.pdf "$PDFS/fig_teaser.pdf"
if command -v pdftoppm >/dev/null 2>&1; then
  pdftoppm -png -r 200 fig_teaser.pdf "$PNGS/fig_teaser_tmp"
  mv -f "$PNGS/fig_teaser_tmp-1.png" "$PNGS/fig_teaser.png"
  rm -f "$PNGS/fig_teaser_tmp"*
fi
rm -f fig_teaser.aux fig_teaser.log fig_teaser.pdf
echo "OK → $PDFS/fig_teaser.pdf"

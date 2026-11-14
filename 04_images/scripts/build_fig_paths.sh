#!/usr/bin/env bash
##################################################################################################################################
# Script: build_fig_paths.sh (build fig:paths PDF/PNG)                                                                           #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Compiles fig_paths.tex via pdflatex; writes ../pdfs/fig_paths.pdf and optional PNG.                               #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   bash build_fig_paths.sh                                                                                                      #
##################################################################################################################################

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PDFS="$(cd "$ROOT/../pdfs" && pwd)"
PNGS="$(cd "$ROOT/../pngs" && pwd)"
cd "$ROOT"
pdflatex -interaction=nonstopmode fig_paths.tex >/tmp/fig_paths_build.log 2>&1 || {
  echo "BUILD FAIL"; tail -50 /tmp/fig_paths_build.log; exit 1
}
# Paper version is cropped to the ink with a 2 pt margin (reproduces Paper Tex/pdfs/fig_paths.pdf)
pdfcrop --margins 2 fig_paths.pdf fig_paths_crop.pdf >/dev/null && mv -f fig_paths_crop.pdf fig_paths.pdf
cp -f fig_paths.pdf "$PDFS/fig_paths.pdf"
if command -v pdftoppm >/dev/null 2>&1; then
  pdftoppm -png -r 220 fig_paths.pdf "$PNGS/fig_paths_tmp"
  mv -f "$PNGS/fig_paths_tmp-1.png" "$PNGS/fig_paths.png"
  rm -f "$PNGS/fig_paths_tmp"*
fi
rm -f fig_paths.aux fig_paths.log fig_paths.pdf
pdfinfo "$PDFS/fig_paths.pdf" | grep -E 'Page size|Pages'
echo "OK → $PDFS/fig_paths.pdf"

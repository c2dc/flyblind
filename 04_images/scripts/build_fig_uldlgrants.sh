#!/usr/bin/env bash
##################################################################################################################################
# Script: build_fig_uldlgrants.sh (build fig:uldlgrants PDF/PNG)                                                                 #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Compiles fig_uldlgrants.tex via pdflatex; writes ../pdfs/fig_uldlgrants.pdf and optional PNG.                     #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   bash build_fig_uldlgrants.sh                                                                                                 #
##################################################################################################################################

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PDFS="$(cd "$ROOT/../pdfs" && pwd)"
PNGS="$(cd "$ROOT/../pngs" && pwd)"
cd "$ROOT"
pdflatex -interaction=nonstopmode fig_uldlgrants.tex >/tmp/fig_uldlgrants_build.log 2>&1 || {
  echo "BUILD FAIL"; tail -40 /tmp/fig_uldlgrants_build.log; exit 1
}
# Paper version is cropped to the ink with a 2 pt margin (reproduces Paper Tex/pdfs/fig_uldlgrants.pdf)
pdfcrop --margins 2 fig_uldlgrants.pdf fig_uldlgrants_crop.pdf >/dev/null && mv -f fig_uldlgrants_crop.pdf fig_uldlgrants.pdf
cp -f fig_uldlgrants.pdf "$PDFS/fig_uldlgrants.pdf"
if command -v pdftoppm >/dev/null 2>&1; then
  pdftoppm -png -r 220 fig_uldlgrants.pdf "$PNGS/fig_uldlgrants_tmp"
  mv -f "$PNGS/fig_uldlgrants_tmp-1.png" "$PNGS/fig_uldlgrants.png"
  rm -f "$PNGS/fig_uldlgrants_tmp"*
fi
rm -f fig_uldlgrants.aux fig_uldlgrants.log fig_uldlgrants.pdf
pdfinfo "$PDFS/fig_uldlgrants.pdf" | grep -E 'Page size|Pages'
echo "OK → $PDFS/fig_uldlgrants.pdf"

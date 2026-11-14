#!/usr/bin/env bash
##################################################################################################################################
# Script: build_fig_pipeline.sh (build fig:pipeline PDF/PNG)                                                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Compiles fig_pipeline.tex via pdflatex; writes ../pdfs/fig_pipeline.pdf and optional PNG.                         #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   bash build_fig_pipeline.sh                                                                                                   #
##################################################################################################################################

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PDFS="$(cd "$ROOT/../pdfs" && pwd)"
PNGS="$(cd "$ROOT/../pngs" && pwd)"
cd "$ROOT"
pdflatex -interaction=nonstopmode fig_pipeline.tex >/tmp/fig_pipeline_build.log 2>&1 || {
  echo "BUILD FAIL"; tail -50 /tmp/fig_pipeline_build.log; exit 1
}
# Paper version is cropped to the ink with a 2 pt margin (reproduces Paper Tex/pdfs/fig_pipeline.pdf)
pdfcrop --margins 2 fig_pipeline.pdf fig_pipeline_crop.pdf >/dev/null && mv -f fig_pipeline_crop.pdf fig_pipeline.pdf
cp -f fig_pipeline.pdf "$PDFS/fig_pipeline.pdf"
if command -v pdftoppm >/dev/null 2>&1; then
  pdftoppm -png -r 220 fig_pipeline.pdf "$PNGS/fig_pipeline_tmp"
  mv -f "$PNGS/fig_pipeline_tmp-1.png" "$PNGS/fig_pipeline.png"
  rm -f "$PNGS/fig_pipeline_tmp"*
fi
rm -f fig_pipeline.aux fig_pipeline.log fig_pipeline.pdf
pdfinfo "$PDFS/fig_pipeline.pdf" | grep -E 'Page size|Pages'
echo "OK → $PDFS/fig_pipeline.pdf"

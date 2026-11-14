#!/usr/bin/env bash
##################################################################################################################################
# Script: sync_to_paper.sh (copies the built figure PDFs into the manuscript tree)                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: 04_images/ is the single source for every figure. Scripts live in scripts/, PNG previews land in pngs/ and the    #
# vector versions in pdfs/. This copies pdfs/ into the manuscript so the two never drift. Run build_all.sh first.                #
##################################################################################################################################
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$ROOT/../pdfs" && pwd)"
DST="$(cd "$ROOT/../../../Paper Tex/pdfs" && pwd)"
n=0
for f in "$SRC"/*.pdf; do
  cp -f "$f" "$DST/" && n=$((n+1))
done
echo "copied ${n} PDFs"
echo "  from ${SRC}"
echo "  to   ${DST}"

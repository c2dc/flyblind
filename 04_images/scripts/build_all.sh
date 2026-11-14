#!/usr/bin/env bash
##################################################################################################################################
# Script: build_all.sh (rebuild every paper figure into ../pdfs and ../pngs)                                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Rebuilds TikZ + Matplotlib figures shipped under 03_images/ (pdfs/ and pngs/).                                    #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   bash build_all.sh   # run from 03_images/scripts/                                                                            #
##################################################################################################################################

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "== TikZ =="
bash build_fig_teaser.sh
bash build_fig_uldlgrants.sh
bash build_fig_pipeline.sh
bash build_fig_paths.sh

echo "== Matplotlib =="
python3 generate_fig_pre_attack_drain.py
# fig:orbit belongs to the circular-trajectory campaign and is not used by the current paper.
# Its source runs are outside the released results, so a failure here is expected and not fatal.
python3 generate_fig_orbit.py || echo "   (skipped fig_orbit: needs runs outside the released results)"
python3 generate_fig_route_divergence.py

echo "All figures rebuilt under ../pdfs/ and ../pngs/"

#!/usr/bin/env python3
##################################################################################################################################
# Module: generate_fig_orbit_split_b.py (fig_orbit_split_b — panel (b) attack only)                                              #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Part of the paper / dissertation:                                                                                              #
#                                                                                                                                #
#                                                                                                                                #
# Description: Single-panel variant of fig_orbit, panel (b). Outputs under ../pdfs/ and ../pngs/.                                #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 generate_fig_orbit_split_b.py          # campaign runs (from 03_images/scripts/)                                     #
#   python3 generate_fig_orbit_split_b.py          --check-august   # self-test: rebuilds the August 2026 version                #
##################################################################################################################################

"""
fig_orbit_split_b — panel (b) FlyBlind Attack only (same style as generate_fig_orbit.py).
Runs and rows: select_panels() of generate_fig_orbit.py.

Outputs: ../pdfs/fig_orbit_split_b.pdf and ../pngs/fig_orbit_split_b.png (no pdfcrop, as the original)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt

from generate_fig_orbit import (  # noqa: E402
    ART,
    ICONS,
    OUT_PDF,
    OUT_PNG,
    USENIX_RC,
    _panel,
    build_uav_delay,
    find_artifacts_root,
    select_panels,
)

TAG_A = "(a) Baseline: GCS sees the UAV almost in real time"
TAG_B = "(b) FlyBlind Attack: UAV flies correctly, but GCS sees a stale state"


def _args():
    ap = argparse.ArgumentParser(description="fig_orbit_split_b")
    ap.add_argument("--check-august", action="store_true",
                    help="rebuild the August 2026 version (RUN03) to verify the drawing code")
    ap.add_argument("--out-dir", type=Path, default=None, help="write PDF/PNG here instead of ../pdfs, ../pngs")
    return ap.parse_args()


def _save(fig, args) -> None:
    out_pdf, out_png = (args.out_dir, args.out_dir) if args.out_dir else (OUT_PDF, OUT_PNG)
    out_pdf.mkdir(parents=True, exist_ok=True)
    out_png.mkdir(parents=True, exist_ok=True)
    pdf = out_pdf / "fig_orbit_split_b.pdf"
    png = out_png / "fig_orbit_split_b.png"
    fig.savefig(pdf, bbox_inches="tight", pad_inches=0.02)
    fig.savefig(png, bbox_inches="tight", pad_inches=0.02, dpi=220)
    plt.close(fig)
    print(f"OK → {pdf}")
    print(f"PNG → {png}")


def main() -> None:
    args = _args()
    panels = select_panels(args.check_august)
    uav_delay = build_uav_delay(ICONS / "uav_delay.png")
    print(f"icon → {uav_delay}")

    root = find_artifacts_root(ART)
    plt.rcParams.update(USENIX_RC)

    fig, ax = plt.subplots(1, 1, figsize=(3.35, 3.70))
    fig.subplots_adjust(left=0.12, right=0.98, bottom=0.10, top=0.88)

    _panel(
        ax, root, panels[1][0], panels[1][1], is_attack=True, tag=TAG_B,
        t_lo=panels[1][2], t_hi=panels[1][3], **panels[1][4],
    )
    _save(fig, args)


if __name__ == "__main__":
    main()

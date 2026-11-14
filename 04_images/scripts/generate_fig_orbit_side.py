#!/usr/bin/env python3
##################################################################################################################################
# Module: generate_fig_orbit_side.py (fig_orbit_side — orbit truth vs belief, side-by-side)                                      #
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
# Description: Side-by-side variant of fig_orbit (used in the dissertation). Outputs under ../pdfs/ and ../pngs/.                #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 generate_fig_orbit_side.py             # campaign runs (from 03_images/scripts/)                                     #
#   python3 generate_fig_orbit_side.py             --check-august   # self-test: rebuilds the August 2026 version                #
##################################################################################################################################

"""
fig_orbit_side — same orbit figure as generate_fig_orbit.py, but side-by-side
with a vertical dashed separator between (a) and (b) (dissertation, Cap. 4).
Runs and rows: select_panels() of generate_fig_orbit.py.

Outputs: ../pdfs/fig_orbit_side.pdf and ../pngs/fig_orbit_side.png (no pdfcrop, as the original)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt

from matplotlib.lines import Line2D
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
# Title wrap width of the published side-by-side layout (measured: 56 characters per line).
SIDE_TITLE_CHARS = 56


def _args():
    ap = argparse.ArgumentParser(description="fig_orbit_side")
    ap.add_argument("--check-august", action="store_true",
                    help="rebuild the August 2026 version (RUN03) to verify the drawing code")
    ap.add_argument("--out-dir", type=Path, default=None, help="write PDF/PNG here instead of ../pdfs, ../pngs")
    return ap.parse_args()


def _save(fig, args) -> None:
    out_pdf, out_png = (args.out_dir, args.out_dir) if args.out_dir else (OUT_PDF, OUT_PNG)
    out_pdf.mkdir(parents=True, exist_ok=True)
    out_png.mkdir(parents=True, exist_ok=True)
    pdf = out_pdf / "fig_orbit_side.pdf"
    png = out_png / "fig_orbit_side.png"
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

    # Two squares side-by-side (width ≈ 2 × panel height)
    fig, axes = plt.subplots(1, 2, figsize=(7.10, 3.55))
    fig.subplots_adjust(left=0.06, right=0.98, bottom=0.10, top=0.86, wspace=0.28)

    _panel(
        axes[0], root, panels[0][0], panels[0][1], is_attack=False, tag=TAG_A,
        t_lo=panels[0][2], t_hi=panels[0][3], title_chars=SIDE_TITLE_CHARS, **panels[0][4],
    )
    _panel(
        axes[1], root, panels[1][0], panels[1][1], is_attack=True, tag=TAG_B,
        t_lo=panels[1][2], t_hi=panels[1][3], title_chars=SIDE_TITLE_CHARS, **panels[1][4],
    )

    # Vertical dashed separator across the axes height only (tight bbox safe)
    fig.canvas.draw()
    pos0 = axes[0].get_position()
    pos1 = axes[1].get_position()
    x_sep = 0.5 * (pos0.x1 + pos1.x0)
    y0 = min(pos0.y0, pos1.y0)
    y1 = max(pos0.y1, pos1.y1)
    fig.add_artist(
        Line2D(
            [x_sep, x_sep], [y0, y1],
            transform=fig.transFigure,
            color="#555555",
            lw=0.95,
            linestyle=(0, (3.5, 2.0)),
            clip_on=False,
            zorder=10,
        )
    )
    _save(fig, args)


if __name__ == "__main__":
    main()

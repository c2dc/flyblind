#!/usr/bin/env python3
##################################################################################################################################
# Module: make_gcs_icon.py (draws the ground control station icon used by the route figure)                                      #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: The ground station in this work is a commercial software GCS on a laptop, not a hand-held radio transmitter. The  #
# earlier icon showed a transmitter with sticks and antennas, which implies a direct radio link and contradicts the setup: the   #
# operator sees the vehicle through a map on a screen, fed by telemetry over the cellular network. This draws that instead, as   #
# a laptop whose screen carries a route and a position marker, in the same flat line art as the aircraft icons.                  #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 make_gcs_icon.py            # writes ../icons/gcs_laptop.png                                                         #
##################################################################################################################################
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Rectangle

HERE = Path(__file__).resolve().parent
ICONS = HERE.parent / "icons"
DST = ICONS / "gcs_laptop.png"

LW = 5.0            # stroke weight, matched to the aircraft icons once scaled down
INK = "#000000"
DPI = 300


def main() -> int:
    fig = plt.figure(figsize=(2.6, 1.9))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 260)
    ax.set_ylim(0, 190)
    ax.set_aspect("equal")
    ax.axis("off")

    common = dict(fill=False, ec=INK, lw=LW, joinstyle="round", capstyle="round")

    # Screen bezel and the display area inside it.
    ax.add_patch(Rectangle((52, 58), 156, 112, **common))
    ax.add_patch(Rectangle((66, 72), 128, 84, **common))

    # The map on the screen: a track with a heading marker on it, so the icon reads as
    # "the operator is looking at a position" rather than as a generic computer.
    tx = [76, 96, 112, 132, 150, 170, 184]
    ty = [92, 108, 100, 122, 112, 134, 128]
    ax.plot(tx, ty, color=INK, lw=LW * 0.72, solid_capstyle="round",
            solid_joinstyle="round")
    ax.plot([150], [112], marker="o", ms=7.0, mfc="white", mec=INK,
            mew=LW * 0.62, zorder=3)

    # Base: a shallow trapezoid, wider than the screen, with the front lip closed.
    ax.add_patch(Polygon([(30, 58), (230, 58), (248, 30), (12, 30)], **common))
    ax.plot([104, 156], [44, 44], color=INK, lw=LW * 0.72, solid_capstyle="round")

    ICONS.mkdir(parents=True, exist_ok=True)
    fig.savefig(DST, dpi=DPI, transparent=True)
    plt.close(fig)

    img = plt.imread(DST)
    print(f"OK -> {DST}  {img.shape[1]}x{img.shape[0]} px, "
          f"ink coverage {float((img[..., 3] > 0.5).mean()) * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

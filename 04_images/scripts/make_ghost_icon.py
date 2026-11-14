#!/usr/bin/env python3
##################################################################################################################################
# Module: make_ghost_icon.py (derives the dashed "ghost" aircraft icon from the solid one)                                       #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: The stale position the operator is shown is the same aircraft, drawn as an outline that is broken into dashes so  #
# a reader tells the two apart at a glance without a second shape to learn. The dashes come from a diagonal mask applied to the  #
# alpha channel of icons/uav.png, so the two icons stay in register and keep identical proportions.                              #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 make_ghost_icon.py            # writes ../icons/uav_ghost.png                                                        #
##################################################################################################################################
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
ICONS = HERE.parent / "icons"

SRC = ICONS / "uav.png"
DST = ICONS / "uav_ghost.png"

DASH_PERIOD_PX = 9      # full dash + gap, in source pixels
DUTY = 0.55             # share of the period that stays opaque


def main() -> int:
    img = plt.imread(SRC)
    if img.ndim == 2:
        img = np.dstack([img] * 3 + [np.ones_like(img)])
    if img.shape[2] == 3:
        img = np.dstack([img, np.ones(img.shape[:2])])
    out = img.astype(float).copy()

    h, w = out.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    phase = ((xx + yy) % DASH_PERIOD_PX) / DASH_PERIOD_PX
    keep = (phase < DUTY).astype(float)

    out[..., 3] = out[..., 3] * keep

    DST.parent.mkdir(parents=True, exist_ok=True)
    plt.imsave(DST, np.clip(out, 0.0, 1.0))
    on = float(keep.mean())
    print(f"dash period {DASH_PERIOD_PX}px, duty {DUTY:.2f} -> {on * 100:.0f}% of the stroke kept")
    print(f"OK -> {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
##################################################################################################################################
# Module: make_uav_black_icons.py (builds the solid and ghost aircraft icons from one source silhouette)                         #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Two icons from one drawing, so the aircraft and the position the operator is shown read as the same vehicle.      #
#   uav_black.png        the silhouette on a transparent background                                                              #
#   uav_black_ghost.png  the same outline, hollow and broken into dashes, for the stale position                                 #
# The ghost keeps the exact outer contour, so the two line up pixel for pixel when they coincide.                                #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 make_uav_black_icons.py [SOURCE]      # default: the silhouette already stored beside this script                    #
##################################################################################################################################
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
ICONS = HERE.parent / "icons"
SRC_DEFAULT = ICONS / "_uav_black_source.png"

INK_THRESHOLD = 0.5     # below this luminance the pixel belongs to the aircraft
MARGIN_PX = 6           # transparent border kept around the trimmed drawing
OUTLINE_PX = 18         # wall thickness of the hollow ghost
DASH_PERIOD_PX = 46     # full dash plus gap, measured along the diagonal
DASH_DUTY = 0.58        # share of the period that stays opaque


def _shift_max(a: np.ndarray, r: int) -> np.ndarray:
    out = a.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx > r * r:
                continue
            out = np.maximum(out, np.roll(np.roll(a, dy, axis=0), dx, axis=1))
    return out


def _erode(mask: np.ndarray, r: int) -> np.ndarray:
    """Erosion written as a dilation of the complement, which keeps this dependency-free."""
    return 1.0 - _shift_max(1.0 - mask, r)


def rgba(mask: np.ndarray) -> np.ndarray:
    out = np.zeros(mask.shape + (4,), dtype=float)
    out[..., 3] = np.clip(mask, 0.0, 1.0)
    return out


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC_DEFAULT
    img = plt.imread(src).astype(float)
    if img.max() > 1.0:
        img = img / 255.0
    if img.ndim == 2:
        lum = img
    else:
        lum = img[..., :3].mean(axis=2)
        if img.shape[2] == 4:                       # honour an existing alpha
            lum = np.where(img[..., 3] > 0.5, lum, 1.0)

    mask = (lum < INK_THRESHOLD).astype(float)
    if mask.sum() == 0:
        print("no ink found; check INK_THRESHOLD")
        return 1

    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    r0, r1 = rows[0], rows[-1] + 1
    c0, c1 = cols[0], cols[-1] + 1
    mask = mask[r0:r1, c0:c1]
    mask = np.pad(mask, MARGIN_PX, mode="constant")

    ICONS.mkdir(parents=True, exist_ok=True)
    solid = ICONS / "uav_black.png"
    plt.imsave(solid, rgba(mask))

    inner = _erode(mask, OUTLINE_PX)
    outline = np.clip(mask - inner, 0.0, 1.0)

    h, w = outline.shape
    yy, xx = np.mgrid[0:h, 0:w]
    phase = ((xx + yy) % DASH_PERIOD_PX) / DASH_PERIOD_PX
    ghost = outline * (phase < DASH_DUTY)

    ghostf = ICONS / "uav_black_ghost.png"
    plt.imsave(ghostf, rgba(ghost))

    print(f"source {src.name}  ->  {mask.shape[1]}x{mask.shape[0]} px")
    print(f"  solid   coverage {mask.mean() * 100:5.1f}%  -> {solid.name}")
    print(f"  outline coverage {outline.mean() * 100:5.1f}%  wall {OUTLINE_PX}px")
    print(f"  ghost   coverage {ghost.mean() * 100:5.1f}%  dash {DASH_PERIOD_PX}px duty {DASH_DUTY} -> {ghostf.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

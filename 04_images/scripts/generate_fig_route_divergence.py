#!/usr/bin/env python3
##################################################################################################################################
# Module: generate_fig_route_divergence.py (fig:route-divergence — displayed position vs aircraft on the route)                  #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Three wide, low panels for a two-column float. The whole route with the zoom window, the same window at           #
# baseline, and the same window under attack, where the displayed position sits one leg of the turnaround behind the             #
# aircraft. Reads the campaign runs under 02_results/campaign/. Outputs ../pdfs/ and ../pngs/.                                   #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 generate_fig_route_divergence.py                                                                                     #
##################################################################################################################################

"""
fig:route-divergence — what the operator sees versus where the aircraft is.

Three panels, wide and low, for a two-column float:
  (a) the whole route with the zoom window marked
  (b) BASELINE, same window: the displayed position sits on the aircraft
  (c) ATTACK, same window: the displayed position is one leg behind

Data (not invented), all from runs inside the released artifact:
  truth   c2_telemetry_uav.csv   SITL egress NED, immune to delivery delay
  belief  telemetry_downlink_gcs.csv   the latest LOCAL_POSITION_NED at the GCS
  route   01_scripts/03_sitl/lib/trajectory.py  ROUTE_WAYPOINTS

The attack instant is chosen as the moment of largest divergence that is also
anti-parallel to the flight direction, which is where the ghost and the aircraft
sit on opposite legs of a turnaround. The baseline panel uses the same instant of
the trajectory, so the two zooms are the same place on the same mission.

Outputs: ../pdfs/fig_route_divergence.pdf and ../pngs/fig_route_divergence.png
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.offsetbox import AnnotationBbox, OffsetImage
from matplotlib.patches import FancyBboxPatch, Rectangle

HERE = Path(__file__).resolve().parent          # 04_images/scripts/
IMG = HERE.parent                               # 04_images/
ART = IMG.parent                                # artifacts/
CAMP = ART / "02_results" / "campaign" / "flyblind_20261111_232155" / "S1_core" / "runs"
LIB = ART / "01_scripts" / "03_sitl" / "lib"
ICONS = IMG / "icons"
OUT_PDF = IMG / "pdfs"
OUT_PNG = IMG / "pngs"

ATTACK_RUN = ("20261112_002300_179157_attack_cross_prb106_B0_setpoint-stream_"
              "traj-route_sp10p0hz_seed1_udp_multi-rogue_n4_ph25ms_vid12p0mbps_r1")
BASELINE_RUN = ("20261112_021025_584929_baseline_cross_prb106_B0_setpoint-stream_"
                "traj-route_sp10p0hz_seed1_udp_vid12p0mbps_r1")

# Chosen so the divergence measured at this instant equals the median of the attack window
# reported in the headline table, and so the displayed state sits directly behind the
# aircraft along its heading. The figure therefore carries the same number as the table
# rather than a fourth value, and that number is measured here, not derived from speed
# multiplied by age. Verified by scanning the window: gap 47.8 m, age 13.25 s.
T_ATTACK_REL = 398.75
TRAIL_S = 13.0
HALF_W = 58.0        # half-window across the flight direction
ASPECT = 0.60        # panels are 40% shorter than wide, so the three fit one low strip
HALF_H = HALF_W * ASPECT

C_UAV = "#1A1A1A"      # both aircraft icons are black; solid vs dashed carries the meaning
C_TRUTH = "#B2182B"
C_BELIEF = "#4477AA"
C_ROUTE = "#C9C9C9"
C_EDGE = "#333333"
C_GCS = "#2B2B2B"
C_BADGE = "#1A1A1A"
C_PANEL = "#F7F7F7"

ARROW_HANDLE = object()   # sentinel: this legend entry is drawn, not loaded from a file

Z_UAV = 0.0275        # icon scales, tuned so the aircraft reads at column width
Z_GCS = 0.058
GCS_HALF_W = 0.036     # half the console icon width in axes units, at Z_GCS
THICK_UAV = 2        # stroke dilation in source pixels, so the outline survives scaling
THICK_GCS = 1

RC = {
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Nimbus Roman", "DejaVu Serif"],
    "font.size": 7.2,
    "axes.linewidth": 0.7,
    "savefig.dpi": 300,
}


def _dilate(a: np.ndarray, r: int) -> np.ndarray:
    """Grow the stroke so the drawing survives being scaled down to column width."""
    out = a.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx > r * r:
                continue
            out = np.maximum(out, np.roll(np.roll(a, dy, axis=0), dx, axis=1))
    return out


def text_half_width(ax, text: str, fontsize: float) -> float:
    """Half the rendered width of a string, in axes units.

    Measured from the actual text layout rather than estimated from the character count,
    because the estimate is what let a label slide under the console icon.
    """
    t = ax.text(0.5, 0.5, text, fontsize=fontsize, transform=ax.transAxes, alpha=0.0)
    ax.figure.canvas.draw()
    bb = t.get_window_extent(renderer=ax.figure.canvas.get_renderer())
    t.remove()
    x0, _ = ax.transAxes.inverted().transform((bb.x0, bb.y0))
    x1, _ = ax.transAxes.inverted().transform((bb.x1, bb.y1))
    return abs(x1 - x0) / 2.0


def tinted(name: str, rgb: str, zoom: float, alpha: float = 1.0,
           thicken: int = 0) -> OffsetImage:
    """Line art recoloured in place, so every icon keeps the same drawing and proportions."""
    img = plt.imread(ICONS / name)
    if img.ndim == 2:
        img = np.dstack([img] * 3 + [np.ones_like(img)])
    if img.shape[2] == 3:
        img = np.dstack([img, np.ones(img.shape[:2])])
    r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (1, 3, 5))
    out = img.astype(float).copy()
    # These icons carry the stroke in the alpha channel, so recolouring means setting the
    # three colour planes and leaving alpha alone. Multiplying alpha by the ink as well
    # would thin every anti-aliased edge and wash the drawing out.
    mask = out[..., 3]
    if mask.min() > 0.9:                       # opaque background, fall back to the ink
        mask = 1.0 - out[..., :3].mean(axis=2)
    if thicken:
        mask = _dilate(mask, thicken)
    out[..., 0], out[..., 1], out[..., 2] = r, g, b
    out[..., 3] = np.clip(mask * alpha, 0.0, 1.0)
    return OffsetImage(out, zoom=zoom, interpolation="bilinear")


def track(run: str, fname: str):
    out = []
    with open(CAMP / run / fname, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out.append((int(r["t_mono_ns"]), float(r["x"]), float(r["y"])))
            except (TypeError, ValueError, KeyError):
                continue
    out.sort()
    return out


def displayed_age_s(run: str, bp, t_arrival_ns: int) -> float:
    """How old the displayed sample already was when the next one landed.

    The GCS row carries the autopilot's own time_boot_ms, so the sample can be matched
    back to the instant it left SITL. That difference is the age the operator's screen
    is behind by, and it is what the gap in the panel is a consequence of.
    """
    egress: dict[int, int] = {}
    with open(CAMP / run / "c2_telemetry_uav.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                egress.setdefault(int(r["time_boot_ms"]), int(r["t_mono_ns"]))
            except (TypeError, ValueError, KeyError):
                continue
    boot = None
    with open(CAMP / run / "telemetry_downlink_gcs.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                if int(r["t_mono_ns"]) == bp[0]:
                    boot = int(r["time_boot_ms"])
                    break
            except (TypeError, ValueError, KeyError):
                continue
    if boot is None or boot not in egress:
        raise SystemExit("could not match the displayed sample back to its egress instant")
    return (t_arrival_ns - egress[boot]) / 1e9


def health_badge(run: str) -> str:
    """The conventional instruments, read from the run and refused if they disagree.

    Each clause is checked against the record rather than typed in, so the strip cannot
    drift away from the campaign. If any check fails the figure does not build.
    """
    kpi = json.loads((CAMP / run / "run_kpi_report.json").read_text())
    phys = json.loads((CAMP / run / "physics.json").read_text())
    win = json.loads((CAMP / run / "flyblind_phase_windows.json").read_text())
    lo = win["windows"]["ATTACK"]["t_rel_lo_s"]
    hi = win["windows"]["ATTACK"]["t_rel_hi_s"]
    m_start = win["markers"]["mission_start"]

    gap = kpi["criteria"]["C4_command"]["checks"]["QGC_style_gap_3_5s"]["max_gap_s"]
    if gap >= 3.5:
        raise SystemExit(f"heartbeat gap {gap}s would trip the 3.5 s window")

    if phys["failsafe"]["triggered"]:
        raise SystemExit("failsafe fired in this run; the strip would be false")

    # Mode transitions are stamped from mission start, the windows from T0.
    for tr in phys["flight_state"]["mode_transitions"]:
        t_abs = tr["t_rel_s"] + m_start
        if lo <= t_abs < hi and tr["to"] != "GUIDED":
            raise SystemExit(f"left GUIDED at {t_abs:.1f}s, inside the attack window")

    alerts = 0
    with open(CAMP / run / "statustext_gcs.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                sev = int(r["severity"]); t_abs = float(r["t_rel_s"]) + m_start
            except (TypeError, ValueError, KeyError):
                continue
            if sev <= 4 and lo <= t_abs < hi:
                alerts += 1
    if alerts:
        raise SystemExit(f"{alerts} warning-or-above messages inside the attack window")

    return (f"heartbeat {gap:.2f} s $<$ 3.5 s  $\\cdot$  GUIDED  $\\cdot$  "
            f"no failsafe  $\\cdot$  no alert")


DATASET_KEY = "liu_unveiling_low_altitude_2026"
DATASET_NAME = "Scene B, Low-Altitude 5G dataset"


def cite_label(key: str) -> str:
    """Author-style credit for a reference, read from the compiled bibliography.

    Deliberately not the bracket number: that number moves whenever the reference list is
    reordered, and a figure is a separate file that would keep the stale one. The author
    surname does not move. Falls back to the citation key's own leading surname when the
    paper has not been compiled yet.
    """
    bbl = ART.parent / "Paper Tex" / "main.bbl"
    if bbl.exists():
        lines = bbl.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if line.startswith("\\bibitem") and f"{{{key}}}" in line:
                # Skip the IEEEtran spacing macros that sit between the key and
                # the author line when an entry carries a URL.
                j = i + 1
                while j < len(lines) and lines[j].lstrip().startswith("\\"):
                    j += 1
                authors = lines[j] if j < len(lines) else ""
                first = authors.split(",")[0].strip()
                surname = first.split("~")[-1].split()[-1] if first else ""
                if surname:
                    more = ("," in authors) or (" and " in authors)
                    return surname + (" et al." if more else "")
    stem = key.split("_")[0]
    return stem.capitalize() + " et al."


def t0_of(run: str) -> int:
    return int(json.loads((CAMP / run / "run_meta.json").read_text())["T0_mono_ns"])


def at(series, t_ns: int):
    i = bisect.bisect_right([s[0] for s in series], t_ns) - 1
    return series[i] if i >= 0 else None


def pair(truth, belief, t_ns: int):
    """The paper's belief_vs_truth, so the figure and Tab. 2 use one definition."""
    j = bisect.bisect_right([b[0] for b in belief], t_ns) - 1
    if j < 1:
        return None, None
    return at(truth, belief[j][0]), belief[j - 1]


def route_xy():
    sys.path.insert(0, str(LIB))
    import trajectory as tj  # noqa: E402
    pts = [(w[0], w[1]) for w in tj.ROUTE_WAYPOINTS]
    return np.array([p[0] for p in pts]), np.array([p[1] for p in pts])


def frame(ax):
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_color("#8A8A8A")
        sp.set_linewidth(0.7)


def zoom_panel(ax, rx, ry, truth, belief, t_ns, centre, title, age_s=None, badge=None):
    cx, cy = centre
    ax.set_facecolor(C_PANEL)
    ax.plot(ry, rx, color=C_ROUTE, lw=3.6, solid_capstyle="round", zorder=1)
    ax.set_xlim(cy - HALF_W, cy + HALF_W)
    ax.set_ylim(cx - HALF_H, cx + HALF_H)
    ax.set_aspect("equal", adjustable="box")

    tp, bp = pair(truth, belief, t_ns)
    trail = [p for p in truth if t_ns - TRAIL_S * 1e9 <= p[0] <= t_ns]
    if trail:
        ax.plot([p[2] for p in trail], [p[1] for p in trail], color=C_TRUTH,
                lw=3.2, alpha=0.30, solid_capstyle="round", zorder=2)

    d = math.dist((bp[1], bp[2]), (tp[1], tp[2]))
    if d > 5:
        ax.annotate("", xy=(tp[2], tp[1]), xytext=(bp[2], bp[1]),
                    arrowprops=dict(arrowstyle="-", color=C_EDGE, lw=0.8,
                                    ls=(0, (3, 2)), shrinkA=13, shrinkB=13), zorder=3)
    lbl = f"{d:.0f} m" if d >= 10 else f"{d:.1f} m"
    ax.annotate(lbl, xy=((bp[2] + tp[2]) / 2, (bp[1] + tp[1]) / 2),
                xytext=(0, 16 if d > 5 else 19), textcoords="offset points",
                fontsize=7.4, color=C_EDGE, ha="center", zorder=8,
                bbox=dict(fc="white", ec="#B8B8B8", lw=0.45, alpha=0.97,
                          boxstyle="round,pad=0.2"))

    # The operator and the line of sight they actually have, which ends at the belief.
    gcs_ax = (0.50, 0.165)
    gcs_art = AnnotationBbox(tinted("gcs_laptop.png", C_GCS, Z_GCS, thicken=THICK_GCS),
                             gcs_ax, xycoords="axes fraction", frameon=False, zorder=6)
    ax.add_artist(gcs_art)
    ax.figure.canvas.draw()
    gb = gcs_art.get_window_extent(renderer=ax.figure.canvas.get_renderer())
    gx0, _ = ax.transAxes.inverted().transform((gb.x0, gb.y0))
    gx1, _ = ax.transAxes.inverted().transform((gb.x1, gb.y1))
    # The PNG carries a transparent border, so the artist's box is wider than the drawing.
    # Discount it, otherwise the label is pushed away from an edge that is not there.
    _im = plt.imread(ICONS / "gcs_laptop.png")
    _ink = np.where((_im[..., 3] > 0.5).any(axis=0))[0]
    _margin = (_im.shape[1] - 1 - _ink[-1]) / _im.shape[1]
    gcs_right = max(gx0, gx1) - abs(gx1 - gx0) * _margin
    ax.annotate("", xy=(bp[2], bp[1]), xycoords="data",
                xytext=(gcs_ax[0], gcs_ax[1] + 0.085), textcoords="axes fraction",
                arrowprops=dict(arrowstyle="-|>", color=C_EDGE, lw=0.9,
                                shrinkA=2, shrinkB=11,
                                connectionstyle="arc3,rad=-0.16"), zorder=5)
    # Label placed from the arrow's own geometry, offset to the side it leaves free,
    # so it never lands on the aircraft it points at.
    bx, by = ax.transAxes.inverted().transform(ax.transData.transform((bp[2], bp[1])))
    # The label belongs beside the console, on the side the arrow does not use, so it
    # covers neither the console nor the aircraft the arrow points at.
    arrow_lbl = ("displayed state" if age_s is None
                 else f"displayed state,\n{age_s:.0f} s old")   # two lines, so it stays clear of the console
    lsize = 6.6
    half = text_half_width(ax, arrow_lbl, lsize)
    # Sit the label just off the console's measured right edge, close enough to read as
    # its caption but with a gap that is checked rather than assumed. The scale bar holds
    # the bottom left, so the right is the side that is free in both panels.
    CLEAR = 0.009
    lx = gcs_right + CLEAR + half
    if lx + half > 1.0 - 0.012:                    # would run off the panel
        lx = 1.0 - 0.012 - half
    if lx - half < gcs_right + 0.008:
        raise SystemExit(f"label '{arrow_lbl}' has no room beside the console")
    ax.annotate(arrow_lbl, xy=(lx, gcs_ax[1] - 0.010),
                xycoords="axes fraction", ha="center", va="center",
                fontsize=lsize, color=C_EDGE, zorder=8,
                bbox=dict(fc="white", ec="none", alpha=0.80,
                          boxstyle="round,pad=0.12"))

    # Which way the aircraft is going. The trail says where it has been, but on a leg that
    # doubles back that is ambiguous on its own, and the whole point of the panel is that
    # the stale state sits behind rather than ahead.
    i = bisect.bisect_right([p[0] for p in truth], t_ns) - 1
    k = max(0, i - 8)
    hx, hy = truth[i][1] - truth[k][1], truth[i][2] - truth[k][2]
    hn = math.hypot(hx, hy)
    if hn > 0.5:
        hx, hy = hx / hn, hy / hn
        ax.annotate("", xy=(tp[2] + hy * 24.0, tp[1] + hx * 24.0),
                    xytext=(tp[2] + hy * 9.5, tp[1] + hx * 9.5),
                    arrowprops=dict(arrowstyle="-|>", color=C_TRUTH, lw=1.3,
                                    mutation_scale=8, shrinkA=0, shrinkB=0),
                    zorder=8)

    ax.add_artist(AnnotationBbox(tinted("uav_black_ghost.png", C_UAV, Z_UAV, thicken=3),
                                 (bp[2], bp[1]), frameon=False, zorder=7))
    ax.add_artist(AnnotationBbox(tinted("uav_black.png", C_UAV, Z_UAV, thicken=0),
                                 (tp[2], tp[1]), frameon=False, zorder=9))

    x0, y0 = cy - HALF_W + 7, cx - HALF_H + 6
    ax.plot([x0, x0 + 25], [y0, y0], color=C_EDGE, lw=1.2, solid_capstyle="butt", zorder=8)
    ax.text(x0 + 12.5, y0 + 3.2, "25 m", ha="center", va="bottom", fontsize=6.4, color=C_EDGE)

    if badge:
        # Without this strip the panel reads as "delay causes position error", which is
        # unremarkable. The point is that nothing on the operator's console objects, so
        # the instruments are named here next to the gap they fail to reveal.
        ax.text(0.5, 0.945, badge, transform=ax.transAxes, ha="center", va="top",
                fontsize=5.5, color=C_BADGE, zorder=10,
                bbox=dict(fc="white", ec="#9A9A9A", lw=0.5, alpha=0.96,
                          boxstyle="round,pad=0.26"))

    ax.set_title(title, fontsize=8.0, pad=4.0)
    frame(ax)


def legend_strip(fig, y0: float, height: float):
    ax = fig.add_axes([0.065, y0, 0.87, height])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_facecolor("white")
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.add_patch(FancyBboxPatch((0.004, 0.10), 0.992, 0.80,
                                boxstyle="round,pad=0.012,rounding_size=0.02",
                                fc="white", ec="#A9A9A9", lw=0.6,
                                transform=ax.transAxes, clip_on=False, zorder=0))
    items = [
        ("uav_black.png", C_UAV, 1.0, 0.020, 2, "aircraft (SITL truth)"),
        # The hollow outline carries far less ink than the solid, so it needs a heavier
        # stroke to survive the legend's smaller scale.
        ("uav_black_ghost.png", C_UAV, 1.0, 0.020, 5, "displayed state at the GCS"),
        ("gcs_laptop.png", C_GCS, 1.0, 0.040, 1, "operator's console (QGroundControl)"),
        # Drawn rather than loaded: the handle has to be the same arrow the panels use.
        (ARROW_HANDLE, C_TRUTH, 1.0, 0.0, 0, "direction of flight"),
    ]
    # Lay the three entries out from their measured widths, so the last one cannot run past
    # the edge of the box when a label is edited.
    fsize, gap_icon, gap_item = 6.9, 0.034, 0.040
    widths = [text_half_width(ax, t, fsize) * 2 for *_, t in items]
    span = sum(widths) + len(items) * gap_icon + (len(items) - 1) * gap_item
    x = max(0.030, (1.0 - span) / 2)
    for (name, col, al, zl, th, text), w in zip(items, widths):
        if name is ARROW_HANDLE:
            ax.annotate("", xy=(x - 0.017, 0.5), xytext=(x + 0.019, 0.5),
                        xycoords="data", textcoords="data",
                        arrowprops=dict(arrowstyle="-|>", color=col, lw=1.3,
                                        mutation_scale=8, shrinkA=0, shrinkB=0),
                        zorder=3)
        else:
            ax.add_artist(AnnotationBbox(tinted(name, col, zl, al, thicken=th), (x, 0.5),
                                         frameon=False, zorder=3))
        ax.text(x + gap_icon, 0.5, text, va="center", ha="left", fontsize=fsize,
                color="#1A1A1A")
        x += gap_icon + w + gap_item
    if span > 1.0:
        print(f"  warning: legend needs {span:.2f} of the width; shorten a label")
    return ax


def main() -> int:
    rx, ry = route_xy()

    a_truth = track(ATTACK_RUN, "c2_telemetry_uav.csv")
    a_belief = track(ATTACK_RUN, "telemetry_downlink_gcs.csv")
    a_t = t0_of(ATTACK_RUN) + int(T_ATTACK_REL * 1e9)
    tp, bp = pair(a_truth, a_belief, a_t)
    centre = ((tp[1] + bp[1]) / 2, (tp[2] + bp[2]) / 2)

    b_truth = track(BASELINE_RUN, "c2_telemetry_uav.csv")
    b_belief = track(BASELINE_RUN, "telemetry_downlink_gcs.csv")
    b_t = t0_of(BASELINE_RUN) + int(T_ATTACK_REL * 1e9)

    a_tp, a_bp = pair(a_truth, a_belief, a_t)
    a_age = displayed_age_s(ATTACK_RUN, a_bp, a_belief[
        [b[0] for b in a_belief].index(a_bp[0]) + 1][0])
    BADGE = health_badge(ATTACK_RUN)

    plt.rcParams.update(RC)
    fig = plt.figure(figsize=(7.0, 2.26))
    gs = fig.add_gridspec(1, 3, wspace=0.085, left=0.012, right=0.988,
                          top=0.905, bottom=0.295)

    ax0 = fig.add_subplot(gs[0, 0])
    ax0.set_facecolor(C_PANEL)
    ax0.plot(ry, rx, color="#9C9C9C", lw=1.15, solid_capstyle="round")
    ax0.add_patch(Rectangle((centre[1] - HALF_W, centre[0] - HALF_H),
                            2 * HALF_W, 2 * HALF_H, fc="none", ec=C_EDGE,
                            lw=0.95, ls=(0, (2.6, 1.8)), zorder=4))
    mx, my = (rx.min() + rx.max()) / 2, (ry.min() + ry.max()) / 2
    hw = max((ry.max() - ry.min()) / 2, (rx.max() - rx.min()) / 2 / ASPECT) * 1.14
    ax0.set_xlim(my - hw, my + hw)
    ax0.set_ylim(mx - hw * ASPECT, mx + hw * ASPECT)
    ax0.set_aspect("equal", adjustable="box")
    sx = my + hw * 0.88 - 100
    sy = mx - hw * ASPECT * 0.86
    ax0.plot([sx, sx + 100], [sy, sy], color=C_EDGE, lw=1.2, solid_capstyle="butt", zorder=8)
    ax0.text(sx + 50, sy + hw * 0.030, "100 m", ha="center", va="bottom",
             fontsize=6.4, color=C_EDGE)
    credit = f"{DATASET_NAME} [{cite_label(DATASET_KEY)}]"
    ax0.text(0.025, 0.955, credit, transform=ax0.transAxes, ha="left", va="top",
             fontsize=5.8, color=C_EDGE, zorder=9,
             bbox=dict(fc="white", ec="#9A9A9A", lw=0.5, alpha=0.96,
                       boxstyle="round,pad=0.24"))
    ax0.set_title("(a) route flown, 1.87 km", fontsize=8.0, pad=4.0)
    frame(ax0)

    ax1 = fig.add_subplot(gs[0, 1])
    zoom_panel(ax1, rx, ry, b_truth, b_belief, b_t, centre, "(b) baseline")
    ax2 = fig.add_subplot(gs[0, 2])
    zoom_panel(ax2, rx, ry, a_truth, a_belief, a_t, centre, "(c) under attack",
               age_s=a_age, badge=BADGE)

    legend_strip(fig, 0.040, 0.185)

    OUT_PDF.mkdir(parents=True, exist_ok=True)
    OUT_PNG.mkdir(parents=True, exist_ok=True)
    pdf = OUT_PDF / "fig_route_divergence.pdf"
    png = OUT_PNG / "fig_route_divergence.png"
    fig.savefig(pdf, bbox_inches="tight", pad_inches=0.04)
    fig.savefig(png, bbox_inches="tight", pad_inches=0.04, dpi=220)
    plt.close(fig)

    ta, ba = pair(a_truth, a_belief, a_t)
    tb, bb = pair(b_truth, b_belief, b_t)
    print(f"baseline gap {math.dist((tb[1], tb[2]), (bb[1], bb[2])):.1f} m | "
          f"attack gap {math.dist((ta[1], ta[2]), (ba[1], ba[2])):.1f} m | "
          f"displayed age {a_age:.2f} s")
    print(f"badge: {BADGE}")
    print(f"dataset credit: {DATASET_NAME} [{cite_label(DATASET_KEY)}]")
    print(f"OK -> {pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

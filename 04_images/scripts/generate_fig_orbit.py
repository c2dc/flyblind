#!/usr/bin/env python3
##################################################################################################################################
# Module: generate_fig_orbit.py (fig:orbit — UAV truth vs GCS belief)                                                            #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Builds circular-trajectory truth vs belief figure from the campaign baseline and headline runs                    #
# (N=4, vid. 12 Mbps). Also builds ../icons/uav_delay.png. Outputs under                                                         #
# ../pdfs/ and ../pngs/.                                                                                                         #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 generate_fig_orbit.py                  # campaign runs (from 03_images/scripts/)                                     #
#   python3 generate_fig_orbit.py --check-august   # self-test: rebuilds the August 2026 figure (RUN03)                          #
##################################################################################################################################

"""
fig:orbit — circular UAV trajectory: truth vs belief at the GCS.

Runs (campaign, default): (a) baseline video 12 Mb/s (a_baseline) and (b) attack N=4,
video 12 Mb/s (a_atk_n4), resolved from 03_analysis/out/campaign_manifest.json; of the
replicates, the one with the median ATTACK-window belief p50 (03_analysis/out/belief_by_phase_runs.csv).
Rows: the steady orbit, [orbit_acquired, land_command) = PRE start .. DRAIN end of
flyblind_phase_windows.json, from the first sample already on the orbit radius (within
ORBIT_ENTRY_TOL_M; drops the ~3 s orbit-entry transient).
Marked sample: to keep the published geometry, the sample (a real GCS arrival of the run) whose
true and believed positions are closest to those marked in the August 2026 figure; the reference
positions are recomputed from the August runs with the original rule (panel (a): middle row,
panel (b): largest error). In panel (b) the search is restricted to the ATTACK window.
Belief/truth via local _belief_frame.py (pipeline belief definition).
--check-august rebuilds the August 2026 figure (RUN03 in 02_results_USENIX, rows t_rel >= 120 s)
to prove the drawing code is unchanged (pixel-identical to Paper Tex/pdfs/fig_orbit.pdf).

Also builds ../icons/uav_delay.png (uav + delay composite; delay dominant).
Outputs: ../pdfs/fig_orbit.pdf and ../pngs/fig_orbit.png
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import textwrap
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.legend_handler import HandlerBase
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, HPacker, OffsetImage
from matplotlib.patches import Polygon, Rectangle
from PIL import Image

from _belief_frame import (  # noqa: E402
    COL_BASELINE,
    find_artifacts_root,
    load_belief_frame,
)

HERE = Path(__file__).resolve().parent          # 03_images/scripts/
IMG = HERE.parent                               # 03_images/
ART = IMG.parent                                # artifacts/
ICONS = IMG / "icons"
ICONS_PAPER = ICONS  # flat layout in the artifact package
OUT_PDF = IMG / "pdfs"
OUT_PNG = IMG / "pngs"

ANALYSIS_OUT = ART / "03_analysis" / "out"
BASELINE_CONDITION = "a_baseline"
ATTACK_CONDITION = "a_atk_n4"
# August 2026 figure (self-test only)
AUGUST_BASELINE = ("02_results_USENIX/106_PRB-CROSS-4_ROGUES-BASELINE-600S-12MPBS", "RUN03")
AUGUST_ATTACK = ("02_results_USENIX/106_PRB-CROSS-4_ROGUES-ATK_N4-600S-12MPBS", "RUN03")
AUGUST_T_MIN_S = 120.0
# Campaign runs: the trace starts at the first sample within this distance of the orbit radius
# (removes the ~3 s orbit-entry transient, 0.3-0.5 m outside the circle).
ORBIT_ENTRY_TOL_M = 0.15

COL_ORBIT = "#1a1a1a"
COL_BELIEF = "#c62828"

USENIX_RC = {
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Nimbus Roman", "DejaVu Serif"],
    "mathtext.fontset": "stix",
    "font.size": 8.4,  # ~ +20% vs gallery 7
    "axes.labelsize": 9.0,
    "legend.fontsize": 6.6,
    "xtick.labelsize": 7.8,
    "ytick.labelsize": 7.8,
    "axes.linewidth": 0.7,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "savefig.dpi": 300,
}


def _rgba(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def build_uav_delay(out: Path) -> Path:
    """Composite delay (dominant) + uav (smaller, anchored). Transparent bg."""
    delay = _rgba(ICONS_PAPER / "delay.png")
    uav = _rgba(ICONS_PAPER / "uav.png")

    # Canvas sized for delay dominance
    W, H = 420, 360
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # Delay large, upper-right bias
    delay_h = 280
    dw = int(delay.width * delay_h / delay.height)
    delay_r = delay.resize((dw, delay_h), Image.Resampling.LANCZOS)
    canvas.alpha_composite(delay_r, ((W - dw) // 2 + 20, 20))

    # UAV smaller, lower-left anchor under/into delay
    uav_h = 130
    uw = int(uav.width * uav_h / uav.height)
    uav_r = uav.resize((uw, uav_h), Image.Resampling.LANCZOS)
    canvas.alpha_composite(uav_r, (28, H - uav_h - 18))

    # Trim to content
    bbox = canvas.getbbox()
    if bbox:
        canvas = canvas.crop(bbox)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    # also paper/
    canvas.save(ICONS_PAPER / "uav_delay.png")
    return out


def _icon(path: Path, zoom: float) -> OffsetImage:
    return OffsetImage(plt.imread(path), zoom=zoom)


class _IconHandle:
    """Sentinel legend handle for an OffsetImage icon."""

    def __init__(self, path: Path, zoom: float = 0.024):
        self.image = plt.imread(path)
        self.zoom = zoom


class _HandlerIcon(HandlerBase):
    def create_artists(
        self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans
    ):
        ab = AnnotationBbox(
            OffsetImage(orig_handle.image, zoom=orig_handle.zoom),
            (xdescent + 0.5 * width, ydescent + 0.45 * height),
            xycoords=trans,
            frameon=False,
        )
        return [ab]


def _box_edge_point(
    cx: float,
    cy: float,
    half_w: float,
    half_h: float,
    tx: float,
    ty: float,
) -> tuple[float, float]:
    """Intersection of ray center→(tx,ty) with the axis-aligned rectangle."""
    dx, dy = tx - cx, ty - cy
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return (cx, cy - half_h)
    sx = half_w / abs(dx) if abs(dx) > 1e-12 else np.inf
    sy = half_h / abs(dy) if abs(dy) > 1e-12 else np.inf
    t = min(sx, sy)
    return (cx + dx * t, cy + dy * t)


def _center_icons(ax) -> tuple[float, float, float, float]:
    """gNB at orbit center; GCS box proportional to icons, with clearance."""
    # prior +60%, then +25%
    scale = 1.60 * 1.25
    gnb = _icon(ICONS_PAPER / "gnb.png", zoom=0.055 * scale)
    gcs = _icon(ICONS_PAPER / "gcs.png", zoom=0.038 * scale)
    drone = _icon(ICONS_PAPER / "drone_delay.png", zoom=0.040 * scale)

    # Geometric center of the circle
    ax.add_artist(AnnotationBbox(gnb, (0.0, 0.0), frameon=False, zorder=6))

    # Box scaled with icons (+25%) plus inner margin; top clear of gNB
    box_cx, box_cy = 0.0, -11.15
    half_w, half_h = 16.4, 3.70
    ax.add_patch(
        Rectangle(
            (box_cx - half_w, box_cy - half_h),
            2 * half_w,
            2 * half_h,
            fill=True,
            facecolor="white",
            edgecolor="#444444",
            linewidth=1.15,
            linestyle=(0, (3.0, 2.0)),
            zorder=5,
            clip_on=False,
        )
    )

    pair = HPacker(children=[gcs, drone], pad=0, sep=11, align="center")
    ax.add_artist(
        AnnotationBbox(pair, (box_cx, box_cy), frameon=False, zorder=7)
    )
    ax.annotate(
        "GCS",
        xy=(box_cx, box_cy - half_h),
        xytext=(0, -3),
        textcoords="offset points",
        ha="center",
        va="top",
        fontsize=8.5,
        fontweight="bold",
        zorder=7,
        annotation_clip=False,
    )
    return (box_cx, box_cy, half_w, half_h)


def _add_flight_direction_arrow(ax, radius: float) -> None:
    """Clockwise curved dashed arrow on the east rim (clean filled tip)."""
    t = np.linspace(np.deg2rad(58), np.deg2rad(-42), 80)
    r = float(radius) * 0.88
    xs = r * np.cos(t)
    ys = r * np.sin(t)

    # Tip + tangent at end of arc
    x1, y1 = float(xs[-1]), float(ys[-1])
    dx, dy = float(xs[-1] - xs[-2]), float(ys[-1] - ys[-2])
    L = float(np.hypot(dx, dy)) or 1.0
    ux, uy = dx / L, dy / L
    px, py = -uy, ux
    head_len, head_w = 2.20, 0.95
    tip = np.array([x1, y1])
    base = tip - head_len * np.array([ux, uy])
    left = base + head_w * np.array([px, py])
    right = base - head_w * np.array([px, py])

    # Dashed body ends exactly at the arrowhead base (no gap)
    ax.plot(
        np.r_[xs[:-1], base[0]],
        np.r_[ys[:-1], base[1]],
        color="#111111",
        lw=1.55,
        zorder=3,
        solid_capstyle="butt",
        linestyle=(0, (3.2, 2.0)),
    )
    ax.add_patch(
        Polygon(
            [tip, left, right],
            closed=True,
            facecolor="#111111",
            edgecolor="#111111",
            linewidth=0.2,
            zorder=4,
            joinstyle="miter",
        )
    )


def _dashed_arrow(
    ax,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    color: str,
    lw: float = 1.25,
    head_len: float = 2.0,
    head_w: float = 0.90,
    tip_gap: float = 1.35,
    zorder: float = 4,
) -> None:
    """Straight dashed shaft + clean filled triangular tip (no annotate blob)."""
    dx, dy = x1 - x0, y1 - y0
    L = float(np.hypot(dx, dy)) or 1.0
    ux, uy = dx / L, dy / L
    px, py = -uy, ux

    # Tip stops short of the marker
    tip = np.array([x1, y1]) - tip_gap * np.array([ux, uy])
    base = tip - head_len * np.array([ux, uy])

    ax.plot(
        [x0, base[0]],
        [y0, base[1]],
        color=color,
        lw=lw,
        linestyle=(0, (3.5, 2.2)),
        solid_capstyle="butt",
        zorder=zorder,
    )
    ax.add_patch(
        Polygon(
            [
                tip,
                base + head_w * np.array([px, py]),
                base - head_w * np.array([px, py]),
            ],
            closed=True,
            facecolor=color,
            edgecolor=color,
            linewidth=0.2,
            zorder=zorder + 0.1,
            joinstyle="miter",
        )
    )


def _panel(ax, root: Path, campaign: str, run: str, *, is_attack: bool, tag: str,
           t_lo: float, t_hi: float, title_chars: int | None = None,
           ref_points: list | None = None, pick_window: tuple | None = None,
           entry_tol_m: float | None = None) -> None:
    df = load_belief_frame(root / campaign, run)
    df = df.dropna(subset=["truth_x", "truth_y", "gcs_x", "gcs_y"])
    df = df[(df["t_rel_s"] >= t_lo) & (df["t_rel_s"] < t_hi)].copy()
    if entry_tol_m is not None:
        # drop the orbit-entry transient: start at the first sample already on the orbit radius
        r = np.hypot(df["truth_x"].to_numpy(), df["truth_y"].to_numpy())
        on_orbit = np.flatnonzero(np.abs(r - np.median(r)) <= entry_tol_m)
        if on_orbit.size:
            df = df.iloc[int(on_orbit[0]):].copy()

    # NED: x=North, y=East → plot East right, North up
    east_t = df["truth_y"].to_numpy()
    north_t = df["truth_x"].to_numpy()
    east_b = df["gcs_y"].to_numpy()
    north_b = df["gcs_x"].to_numpy()

    ax.plot(east_t, north_t, color=COL_ORBIT, lw=1.05, alpha=0.95, zorder=1)

    errs = np.sqrt((east_t - east_b) ** 2 + (north_t - north_b) ** 2)
    if ref_points is None:
        i = int(np.nanargmax(errs)) if is_attack else int(len(df) // 2)
    else:
        i = _nearest_to_reference(df, ref_points, pick_window)
    e_t, n_t = float(east_t[i]), float(north_t[i])
    e_b, n_b = float(east_b[i]), float(north_b[i])

    box_cx, box_cy, half_w, half_h = _center_icons(ax)
    origin_b = _box_edge_point(box_cx, box_cy, half_w, half_h, e_b, n_b)

    # Dashed GCS → belief (red); clean triangular tip
    _dashed_arrow(
        ax,
        origin_b[0],
        origin_b[1],
        e_b,
        n_b,
        color=COL_BELIEF,
        lw=1.25,
    )

    # True UAV: drone icon (not a scatter blob)
    ax.add_artist(
        AnnotationBbox(
            _icon(ICONS_PAPER / "uav_black.png", zoom=0.055),
            (e_t, n_t),
            frameon=False,
            zorder=6,
        )
    )
    ax.scatter(
        [e_b], [n_b], s=62, c=COL_BELIEF, zorder=5,
        edgecolors="white", linewidths=0.7, label="UAV believed position",
    )

    r_orbit = float(np.median(np.hypot(east_t, north_t)))
    _add_flight_direction_arrow(ax, r_orbit)

    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("")
    ax.set_ylabel("")

    # (a)/(b) bold title — +30% size; wrap to nearly full axes width
    fig_w = ax.figure.get_figwidth()
    ax_w_in = ax.get_position().width * fig_w
    char_w_in = 0.38 * 9.1 / 72.0  # tight estimate so lines fill the width
    # title_chars pins the wrap width of a given layout (the estimate below reads the axes position,
    # which depends on the data-autoscaled limits at this point, so it can move by one character).
    nchars = title_chars or max(32, int(ax_w_in / char_w_in))
    title = "\n".join(textwrap.wrap(tag, width=nchars))
    ax.set_title(title, fontsize=9.1, fontweight="bold", pad=6, linespacing=1.12)

    # North in the gap between orbit and top spine (not on either line)
    ax.text(
        -4.5, 30.4, "North",
        ha="center", va="bottom", fontsize=7.8, clip_on=True, zorder=8,
    )
    ax.set_xlim(-33.0, 33.0)
    ax.set_ylim(-33.0, 33.8)
    ax.set_xticks(np.arange(-30, 31, 10))
    ax.set_yticks(np.arange(-30, 31, 10))

    # Units (axes are meters)
    ax.text(
        0.5, -0.095, "(Meters)",
        transform=ax.transAxes, ha="center", va="top", fontsize=7.2,
    )

    ax.grid(True, alpha=0.25)
    uav_proxy = _IconHandle(ICONS_PAPER / "uav_black.png", zoom=0.028)
    flight_proxy = Line2D(
        [0], [0], color="#111111", lw=1.40, linestyle=(0, (3.2, 2.0)),
        marker=">", markersize=5.5,
    )
    handles, labels = ax.get_legend_handles_labels()
    handles = [uav_proxy, *handles, flight_proxy]
    labels = ["UAV true position", *labels, "UAV flight direction"]
    ax.legend(
        handles, labels, loc="lower left", frameon=True,
        borderpad=0.25, handletextpad=0.35, fontsize=6.2,
        prop={"weight": "bold", "size": 6.2},
        handler_map={_IconHandle: _HandlerIcon()},
    )


def _nearest_to_reference(df, ref_points: list, pick_window: tuple | None) -> int:
    """Row whose (true, believed) positions are closest to the reference pair (east, north)."""
    (te, tn), (be, bn) = ref_points
    d = ((df["truth_y"] - te) ** 2 + (df["truth_x"] - tn) ** 2
         + (df["gcs_y"] - be) ** 2 + (df["gcs_x"] - bn) ** 2).to_numpy()
    if pick_window is not None:
        t = df["t_rel_s"].to_numpy()
        d = np.where((t >= pick_window[0]) & (t < pick_window[1]), d, np.inf)
    return int(np.argmin(d))


def _marked_points(run_dir: Path, t_lo: float, t_hi: float, is_attack: bool) -> list[tuple[float, float]]:
    """(east, north) of the true and believed positions that _panel marks (same selection rule)."""
    df = load_belief_frame(run_dir, ".").dropna(subset=["truth_x", "truth_y", "gcs_x", "gcs_y"])
    df = df[(df["t_rel_s"] >= t_lo) & (df["t_rel_s"] < t_hi)].reset_index(drop=True)
    errs = np.hypot(df["truth_y"] - df["gcs_y"], df["truth_x"] - df["gcs_x"]).to_numpy()
    i = int(np.nanargmax(errs)) if is_attack else int(len(df) // 2)
    return [(float(df["truth_y"][i]), float(df["truth_x"][i])), (float(df["gcs_y"][i]), float(df["gcs_x"][i]))]


def _campaign_run(manifest: dict, belief_rows: list[dict], condition: str) -> tuple[str, str, dict]:
    """(run_dir relative to artifacts, run_id, phase windows) of the median replicate by ATTACK belief p50."""
    runs = {r["run_id"]: r for r in manifest["runs"] if r["condition"] == condition and r["category"] == "campaign"}
    rows = sorted((r for r in belief_rows if r["run_id"] in runs and r["phase"] == "ATTACK"),
                  key=lambda r: float(r["belief_p50_m"]))
    if not rows or len(rows) != len(runs):
        raise SystemExit(f"belief_by_phase_runs.csv does not cover {condition}; run 03_analysis/run_phase4.py")
    chosen = runs[rows[len(rows) // 2]["run_id"]]
    w = json.loads((ART / chosen["run_dir"] / "flyblind_phase_windows.json").read_text())["windows"]
    return chosen["run_dir"], chosen["run_id"], w


def select_panels(check_august: bool) -> list[tuple[str, str, float, float, dict]]:
    """[(campaign_or_run_dir, run, t_lo, t_hi, extra _panel kwargs)] for panel (a) and panel (b)."""
    august = [(*AUGUST_BASELINE, AUGUST_T_MIN_S, float("inf"), {}),
              (*AUGUST_ATTACK, AUGUST_T_MIN_S, float("inf"), {})]
    if check_august:
        return august
    # reference marker positions = those of the August figure (original rule on the August runs)
    refs = [_marked_points(ART / camp / run, lo, hi, is_attack)
            for (camp, run, lo, hi, _), is_attack in zip(august, (False, True))]
    manifest = json.loads((ANALYSIS_OUT / "campaign_manifest.json").read_text())
    with open(ANALYSIS_OUT / "belief_by_phase_runs.csv", encoding="utf-8") as f:
        belief_rows = list(csv.DictReader(f))
    panels = []
    for cond, is_attack, ref in ((BASELINE_CONDITION, False, refs[0]), (ATTACK_CONDITION, True, refs[1])):
        run_dir, run_id, w = _campaign_run(manifest, belief_rows, cond)
        t_lo, t_hi = float(w["PRE"]["t_rel_lo_s"]), float(w["DRAIN"]["t_rel_hi_s"])
        pick = (float(w["ATTACK"]["t_rel_lo_s"]), float(w["ATTACK"]["t_rel_hi_s"])) if is_attack else None
        print(f"{cond}: {run_id} rows t_rel in [{t_lo:.1f}, {t_hi:.1f}) s; marker nearest to the August one")
        panels.append((run_dir, ".", t_lo, t_hi, {"ref_points": ref, "pick_window": pick,
                                                  "entry_tol_m": ORBIT_ENTRY_TOL_M}))
    return panels


def main() -> None:
    ap = argparse.ArgumentParser(description="fig:orbit")
    ap.add_argument("--check-august", action="store_true",
                    help="rebuild the August 2026 figure (RUN03) to verify the drawing code")
    ap.add_argument("--out-dir", type=Path, default=None, help="write PDF/PNG here instead of ../pdfs, ../pngs")
    args = ap.parse_args()
    panels = select_panels(args.check_august)

    uav_delay = build_uav_delay(ICONS / "uav_delay.png")
    print(f"icon → {uav_delay}")

    root = find_artifacts_root(ART)
    plt.rcParams.update(USENIX_RC)

    fig, axes = plt.subplots(2, 1, figsize=(3.35, 7.55))
    fig.subplots_adjust(left=0.11, right=0.98, bottom=0.055, top=0.90, hspace=0.34)

    _panel(
        axes[0], root, panels[0][0], panels[0][1], is_attack=False,
        tag="(a) Baseline: GCS sees the UAV almost in real time",
        t_lo=panels[0][2], t_hi=panels[0][3], **panels[0][4],
    )
    _panel(
        axes[1], root, panels[1][0], panels[1][1], is_attack=True,
        tag="(b) FlyBlind Attack: UAV flies correctly, but GCS sees a stale state",
        t_lo=panels[1][2], t_hi=panels[1][3], **panels[1][4],
    )

    # Separator only across the axes width (avoids huge side margins in tight bbox)
    fig.canvas.draw()
    pos0 = axes[0].get_position()
    pos1 = axes[1].get_position()
    x0 = min(pos0.x0, pos1.x0)
    x1 = max(pos0.x1, pos1.x1)
    y_sep = 0.5 * (pos0.y0 + pos1.y1)
    fig.add_artist(
        Line2D(
            [x0, x1], [y_sep, y_sep],
            transform=fig.transFigure,
            color="#555555",
            lw=0.95,
            linestyle=(0, (3.5, 2.0)),
            clip_on=False,
            zorder=10,
        )
    )

    out_pdf, out_png = (args.out_dir, args.out_dir) if args.out_dir else (OUT_PDF, OUT_PNG)
    out_pdf.mkdir(parents=True, exist_ok=True)
    out_png.mkdir(parents=True, exist_ok=True)
    pdf = out_pdf / "fig_orbit.pdf"
    png = out_png / "fig_orbit.png"
    fig.savefig(pdf, bbox_inches="tight", pad_inches=0.02)
    fig.savefig(png, bbox_inches="tight", pad_inches=0.02, dpi=220)
    plt.close(fig)
    # Paper version is cropped to the ink with a 2 pt margin (reproduces Paper Tex/pdfs/fig_orbit.pdf)
    tmp = pdf.with_name("fig_orbit_crop.pdf")
    subprocess.run(["pdfcrop", "--margins", "2", str(pdf), str(tmp)], check=True, stdout=subprocess.DEVNULL)
    tmp.replace(pdf)
    print(f"OK → {pdf}")
    print(f"PNG → {png}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
##################################################################################################################################
# Module: generate_fig_pre_attack_drain.py (fig:pre-attack-drain — δ_i temporal signature)                                       #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Plots PRE/ATTACK/DRAIN delivery-age signature from the campaign DRAIN run (3.6a)                                  #
# under 02_results/campaign/. Outputs ../pdfs/fig_pre_attack_drain.pdf and PNG twin.                                             #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 generate_fig_pre_attack_drain.py                  # campaign DRAIN run (from 03_images/scripts/)                     #
#   python3 generate_fig_pre_attack_drain.py --check-august   # self-test: rebuilds the August 2026 figure (DRAIN540)            #
##################################################################################################################################

"""
fig:pre-attack-drain — PRE/ATTACK/DRAIN temporal signature (δ_i).

Data (not invented):
  Run: the drain condition g_drain_video_on (attack N=4, video 12 Mb/s, DRAIN extended by the 180 s ceiling), the
    replicate with the median recovery (03_analysis/out/drain_recovery_runs.csv,
    last_aoi_sample_out_of_band_s), resolved from 03_analysis/out/campaign_manifest.json.
  δ_i curve: δ_i = (t_mono_gcs − t_mono_uav) matched by (system_id, time_boot_ms)
    from {telemetry_downlink_gcs,c2_telemetry_uav}.csv of that run.
  PRE median line: median δ_i in the PRE window of the same run (flyblind_phase_windows.json);
    checked against 03_analysis/out/belief_by_phase_runs.csv.
  OWD p99: run_kpi_report.legs_table.command.owd_p99_s of the same run, the command leg
    of the real C2 traffic in the ATTACK window. This is the definition tab:false-healthy
    and fig:teaser use; the network probe measures a different thing and would put a
    second "OWD p99" next to the same label.
  --check-august rebuilds the August 2026 figure (DRAIN540 run in 02_results_USENIX, PRE 16.53 ms
    and OWD 32.17 ms from the August tables, δ cut at 9 s) to prove the layout code is unchanged.

Time axis (honest + readable):
  Real t = (t_mono − T0)/1e9; boundaries = event_markers attack_onset/end.
  Visual X is piecewise-linear so PRE/ATTACK/DRAIN ≈ 17% / 63% / 20%.
  Within DRAIN, the post-attack plateau (co-tenant UL backlog still served) and the collapse of
  δ back to fresh are sub-remapped (45% / 55%) so the collapse is visible. The curve ends at the
  recovery instant of the harness DRAIN criterion (drain_policy: 10 s window back in the PRE band;
  recovery_time_s in drain_recovery_runs.csv). Tick labels are real seconds.

Outputs: ../pdfs/fig_pre_attack_drain.pdf and ../pngs/fig_pre_attack_drain.png
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

IMG = Path(__file__).resolve().parents[1]       # 03_images/
ARTIFACTS = IMG.parent                          # artifacts/
ANALYSIS_OUT = ARTIFACTS / "03_analysis" / "out"
DRAIN_CONDITION = "g_drain_video_on"
AUGUST_RUN = ARTIFACTS / "02_results_USENIX" / "106_PRB-CROSS-4_ROGUES-ATK_N4-600S-12MPBS_DRAIN540" / (
    "20260822_091233_171968_attack_cross_prb106_B0_setpoint-stream_"
    "traj-circle_sp10p0hz_seed1_udp_multi-rogue_n4_ph0ms_vid12p0mbps_r1"
)
OUT_PDF = IMG / "pdfs"
OUT_PNG = IMG / "pngs"

# Set by configure(): campaign run (default) or the August self-test.
DRAIN_RUN: Path = AUGUST_RUN
PRE_MED_MS = float("nan")
OWD_P99_MS = float("nan")
DELTA_LABEL = r"$\delta_i$ (DRAIN run)"
END_RULE = "recovery"          # "recovery" (campaign) | "below_9s" (August figure)
RECOVERY_T_S = float("nan")    # attack_end -> recovery instant (s), END_RULE == "recovery"

# Visual phase fractions
F_PRE, F_ATK, F_DRN = 0.17, 0.63, 0.20
# Within DRAIN visual: plateau vs decay
F_DRN_PLAT, F_DRN_DEC = 0.45, 0.55

USENIX_RC = {
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Nimbus Roman", "DejaVu Serif"],
    "mathtext.fontset": "stix",
    "font.size": 8,
    "axes.labelsize": 8.5,
    "legend.fontsize": 6.6,
    "xtick.labelsize": 7.0,
    "ytick.labelsize": 7.0,
    "axes.linewidth": 0.7,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "savefig.dpi": 300,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
}

C_PRE_BG, C_PRE_LAB = "#e8f5e9", "#1b5e20"
C_ATK_BG, C_ATK_LAB = "#ffe8d6", "#c62828"
C_DRN_BG, C_DRN_LAB = "#e3f2fd", "#1565c0"
C_DELTA, C_OWD, C_PRE_MED, C_GRID = "#c62828", "#1565c0", "#2e7d32", "#bdbdbd"


def load_markers(run: Path) -> dict[str, float]:
    em = pd.read_csv(run / "event_markers.csv")
    return {str(r.event): float(r.t_rel_s) for r in em.itertuples()}


def load_delta_series(run: Path) -> pd.DataFrame:
    fl = json.loads((run / "four_layer_summary.json").read_text())
    t0 = int(fl["T0_mono_ns"])
    gcs = pd.read_csv(run / "telemetry_downlink_gcs.csv")
    uav = pd.read_csv(run / "c2_telemetry_uav.csv")
    m = gcs.merge(uav, on=["mavlink_system_id", "time_boot_ms"], suffixes=("_gcs", "_uav"))
    m["delta_s"] = (m["t_mono_ns_gcs"] - m["t_mono_ns_uav"]) / 1e9
    m["t_T0"] = (m["t_mono_ns_gcs"] - t0) / 1e9
    return m.sort_values("t_T0").reset_index(drop=True)[["t_T0", "delta_s"]]


def drain_cut(series: pd.DataFrame, t_drn: float) -> tuple[pd.DataFrame, float, float]:
    """Keep DRAIN through the plateau and the collapse; END_RULE sets where the curve stops."""
    dr = series[series["t_T0"] >= t_drn]
    # Onset of fast flush (many samples, falling δ)
    below = dr[dr["delta_s"] < 11.0]
    t_dec = float(below["t_T0"].iloc[0]) if len(below) else float(dr["t_T0"].iloc[-1])
    if END_RULE == "recovery":
        # End at the recovery instant of the harness DRAIN criterion (δ and belief back in the PRE band)
        t_end = t_drn + RECOVERY_T_S
    else:
        # August figure: end once δ crosses below ~9.0 (still above PRE)
        end_cands = dr[dr["delta_s"] < 9.0]
        t_end = float(end_cands["t_T0"].iloc[0]) if len(end_cands) else float(dr["t_T0"].iloc[-1])
    cut = series[series["t_T0"] <= t_end].copy()
    return cut, t_dec, t_end


def make_remap(t_atk: float, t_drn: float, t_dec: float, t_end: float):
    """Real seconds → visual [0,1] with DRAIN sub-remap (plateau + decay)."""

    def real_to_vis(t: np.ndarray) -> np.ndarray:
        t = np.asarray(t, dtype=float)
        out = np.empty_like(t)
        pre = t <= t_atk
        atk = (t > t_atk) & (t <= t_drn)
        plat = (t > t_drn) & (t <= t_dec)
        dec = t > t_dec
        out[pre] = F_PRE * (t[pre] / max(t_atk, 1e-9))
        out[atk] = F_PRE + F_ATK * (t[atk] - t_atk) / max(t_drn - t_atk, 1e-9)
        out[plat] = (
            F_PRE + F_ATK
            + F_DRN * F_DRN_PLAT * (t[plat] - t_drn) / max(t_dec - t_drn, 1e-9)
        )
        out[dec] = (
            F_PRE + F_ATK + F_DRN * F_DRN_PLAT
            + F_DRN * F_DRN_DEC * (t[dec] - t_dec) / max(t_end - t_dec, 1e-9)
        )
        return np.clip(out, 0.0, 1.0)

    return real_to_vis


def build_figure():
    markers = load_markers(DRAIN_RUN)
    t_atk = markers["attack_onset"]
    t_drn = markers["attack_end"]
    raw = load_delta_series(DRAIN_RUN)
    series, t_dec, t_end = drain_cut(raw, t_drn)
    real_to_vis = make_remap(t_atk, t_drn, t_dec, t_end)

    t_real = series["t_T0"].to_numpy()
    d_s = series["delta_s"].to_numpy()
    x_vis = real_to_vis(t_real)

    def p50(mask):
        s = d_s[mask]
        return float(np.median(s)) if s.size else float("nan")

    stats = {
        "ATTACK_p50_s": p50((t_real >= t_atk) & (t_real < t_drn)),
        "DRAIN_p50_s_full_table_window": float(
            np.median(raw.loc[raw["t_T0"] >= t_drn, "delta_s"])
        ),
        "DRAIN_p50_s_plotted": p50(t_real >= t_drn),
        "t_atk": t_atk,
        "t_drn": t_drn,
        "t_dec": t_dec,
        "t_end": t_end,
        "n_plotted": int(len(series)),
    }

    plt.rcParams.update(USENIX_RC)
    # Near-square for \columnwidth (was too flat at ~3.5×1.5)
    fig, ax = plt.subplots(figsize=(3.75, 2.70))
    fig.subplots_adjust(left=0.12, right=0.88, bottom=0.14, top=0.90)
    ax2 = ax.twinx()

    ax.axvspan(0.0, F_PRE, facecolor=C_PRE_BG, edgecolor="none", zorder=0)
    ax.axvspan(F_PRE, F_PRE + F_ATK, facecolor=C_ATK_BG, edgecolor="none", zorder=0)
    ax.axvspan(F_PRE + F_ATK, 1.0, facecolor=C_DRN_BG, edgecolor="none", zorder=0)
    for xv in (F_PRE, F_PRE + F_ATK):
        ax.axvline(xv, color="#555555", ls="--", lw=0.7, zorder=2)

    ax.text(F_PRE / 2, 0.97, "PRE", transform=ax.get_xaxis_transform(),
            ha="center", va="top", color=C_PRE_LAB, fontweight="bold", fontsize=8)
    ax.text(F_PRE + F_ATK / 2, 0.97, "ATTACK", transform=ax.get_xaxis_transform(),
            ha="center", va="top", color=C_ATK_LAB, fontweight="bold", fontsize=8)
    ax.text(F_PRE + F_ATK + F_DRN / 2, 0.97, "DRAIN", transform=ax.get_xaxis_transform(),
            ha="center", va="top", color=C_DRN_LAB, fontweight="bold", fontsize=8)

    (h_pre,) = ax.plot(
        [0.0, F_PRE], [PRE_MED_MS / 1000.0, PRE_MED_MS / 1000.0],
        color=C_PRE_MED, lw=1.85, zorder=4,
        label=rf"PRE med. ({PRE_MED_MS:.1f} ms)",
    )
    (h_d,) = ax.plot(
        x_vis, d_s, color=C_DELTA, lw=1.05, zorder=5,
        label=DELTA_LABEL,
    )
    (h_owd,) = ax2.plot(
        [0.0, 1.0], [OWD_P99_MS, OWD_P99_MS],
        color=C_OWD, lw=1.15, ls="--", zorder=3,
        label=rf"OWD p99 ({OWD_P99_MS:.0f} ms)",
    )

    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 16.0)
    ax2.set_ylim(0.0, 90.0)
    ax.set_ylabel(r"$\delta_i$ (s)")
    ax2.set_ylabel("OWD (ms)", color=C_OWD)
    ax2.tick_params(axis="y", colors=C_OWD)
    ax2.spines["right"].set_color(C_OWD)
    ax.set_xlabel("Time since T0 (s)")

    tick_reals = [0, 60, round(t_atk, 1), 300, 500, round(t_drn, 1), round(t_end, 1)]
    tick_vis = real_to_vis(np.array(tick_reals, dtype=float))
    ax.set_xticks(tick_vis)
    ax.set_xticklabels([f"{t:g}" for t in tick_reals])

    ax.yaxis.grid(True, ls=":", lw=0.5, color=C_GRID, zorder=1)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax2.spines["top"].set_visible(False)

    leg = ax.legend(
        handles=[h_pre, h_d, h_owd], loc="lower right", frameon=True,
        fancybox=False, edgecolor="#cccccc", framealpha=0.92,
        borderpad=0.3, handlelength=1.5,
    )
    leg.get_frame().set_linewidth(0.5)
    return fig, stats


def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _phase_p50_ms(series: pd.DataFrame, run: Path, phase: str) -> float:
    """p50 of δ_i (ms) in a phase window, with the pipeline's own percentile definition
    (freshness_metrics._summary_stats), so the figure equals the analysis tables."""
    import sys
    lib = str(ARTIFACTS / "01_scripts" / "03_sitl" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import freshness_metrics as fm  # noqa: E402
    w = json.loads((run / "flyblind_phase_windows.json").read_text())["windows"][phase]
    m = (series["t_T0"] >= w["t_rel_lo_s"]) & (series["t_T0"] < w["t_rel_hi_s"])
    return float(fm._summary_stats(list(series.loc[m, "delta_s"].to_numpy() * 1000.0))["p50"])


def configure(check_august: bool) -> dict:
    """Select the run and the reference lines; returns the analysis values used for the checks."""
    global DRAIN_RUN, PRE_MED_MS, OWD_P99_MS, DELTA_LABEL, END_RULE, RECOVERY_T_S
    if check_august:
        DRAIN_RUN, PRE_MED_MS, OWD_P99_MS = AUGUST_RUN, 16.53, 32.17
        DELTA_LABEL, END_RULE = r"$\delta_i$ DRAIN540", "below_9s"
        return {}
    manifest = json.loads((ANALYSIS_OUT / "campaign_manifest.json").read_text())
    runs = {r["run_id"]: r for r in manifest["runs"]
            if r["condition"] == DRAIN_CONDITION and r["category"] == "campaign"}
    rec = sorted((r for r in _read_csv(ANALYSIS_OUT / "drain_recovery_runs.csv") if r["run_id"] in runs),
                 key=lambda r: float(r["last_aoi_sample_out_of_band_s"]))
    if len(rec) != len(runs) or not rec or rec[len(rec) // 2]["recovered"] != "True":
        raise SystemExit("drain_recovery_runs.csv does not cover the DRAIN runs; run 03_analysis/run_phase4.py")
    chosen = rec[len(rec) // 2]                       # median replicate by recovery
    DRAIN_RUN = ARTIFACTS / runs[chosen["run_id"]]["run_dir"]
    RECOVERY_T_S = float(chosen["recovery_time_s"])
    kpi = json.loads((DRAIN_RUN / "run_kpi_report.json").read_text())
    OWD_P99_MS = float(kpi["legs_table"]["command"]["owd_p99_s"]) * 1000.0
    series = load_delta_series(DRAIN_RUN)
    PRE_MED_MS = _phase_p50_ms(series, DRAIN_RUN, "PRE")
    ref = {(r["run_id"], r["phase"]): r for r in _read_csv(ANALYSIS_OUT / "belief_by_phase_runs.csv")}
    return {"run_id": chosen["run_id"], "recovery_time_s": RECOVERY_T_S,
            "pre_ref_ms": float(ref[(chosen["run_id"], "PRE")]["aoi_p50_ms"]),
            "attack_ref_ms": float(ref[(chosen["run_id"], "ATTACK")]["aoi_p50_ms"]),
            "attack_mine_ms": _phase_p50_ms(series, DRAIN_RUN, "ATTACK")}


def main() -> None:
    ap = argparse.ArgumentParser(description="fig:pre-attack-drain")
    ap.add_argument("--check-august", action="store_true",
                    help="rebuild the August 2026 figure (DRAIN540) to verify the layout code")
    ap.add_argument("--out-dir", type=Path, default=None, help="write PDF/PNG here instead of ../pdfs, ../pngs")
    args = ap.parse_args()
    ref = configure(args.check_august)
    out_pdf, out_png = (args.out_dir, args.out_dir) if args.out_dir else (OUT_PDF, OUT_PNG)
    out_pdf.mkdir(parents=True, exist_ok=True)
    out_png.mkdir(parents=True, exist_ok=True)
    fig, stats = build_figure()
    pdf = out_pdf / "fig_pre_attack_drain.pdf"
    png = out_png / "fig_pre_attack_drain.png"
    fig.savefig(pdf, bbox_inches="tight", pad_inches=0.06)
    fig.savefig(png, bbox_inches="tight", pad_inches=0.06, dpi=220)
    plt.close(fig)
    # Paper version is cropped to the ink with a 2 pt margin (reproduces Paper Tex/pdfs/fig_pre_attack_drain.pdf)
    tmp = pdf.with_name("fig_pre_attack_drain_crop.pdf")
    subprocess.run(["pdfcrop", "--margins", "2", str(pdf), str(tmp)], check=True, stdout=subprocess.DEVNULL)
    tmp.replace(pdf)
    print("stats", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in stats.items()})
    if args.check_august:
        assert abs(stats["ATTACK_p50_s"] - 12.31) < 0.05
        assert abs(stats["DRAIN_p50_s_full_table_window"] - 10.81) < 0.05
    else:
        # same values as the campaign analysis (same percentile function; both rounded to 1e-3 ms)
        print("run", ref["run_id"], "| PRE med %.3f ms (analysis %.3f) | ATTACK p50 %.3f ms (analysis %.3f) | "
              "OWD p99 %.2f ms | recovery %.1f s" % (PRE_MED_MS, ref["pre_ref_ms"], ref["attack_mine_ms"],
                                                    ref["attack_ref_ms"], OWD_P99_MS, RECOVERY_T_S))
        assert abs(PRE_MED_MS - ref["pre_ref_ms"]) < 0.002
        assert abs(ref["attack_mine_ms"] - ref["attack_ref_ms"]) < 0.002
    print(f"OK → {pdf}")
    print(f"PNG → {png}")


if __name__ == "__main__":
    main()

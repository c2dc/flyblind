##################################################################################################################################
# Module: _belief_frame.py (belief/truth frame loader for fig_orbit)                                                             #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Minimal loader for GCS belief vs UAV truth traces from a run directory.                                           #
# Used by generate_fig_orbit.py under 03_images/scripts/.                                                                        #
##################################################################################################################################

"""Minimal belief/truth frame loader for fig_orbit (artifact-local).

One row per GCS telemetry arrival (telemetry_downlink_gcs.csv, sorted by t_mono_ns):
  gcs_*    belief shown at the GCS = the previous GCS sample (first row: itself)
  truth_*  latest UAV egress position with t_uav <= t_gcs_arrival (c2_telemetry_uav.csv)
  t_rel_s  (t_gcs_arrival - T0_mono_ns) / 1e9, T0 from four_layer_summary.json

This is the belief definition of the pipeline (freshness_metrics.merge_telemetry_downlink) and the
one that produced the paper figure (Paper Tex/pdfs/fig_orbit.pdf, reproduced pixel-identical).
The 2026-08 version of this file joined GCS and UAV rows of the SAME message, which makes belief
equal truth (zero error); it is replaced by the definition above.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

COL_BASELINE = "#2e7d32"


def find_artifacts_root(hint: Path) -> Path:
    """Return artifacts/ root given a path under it (or the root itself)."""
    p = hint.resolve()
    if p.name == "artifacts" and (p / "02_results").is_dir():
        return p
    for cand in [p, *p.parents]:
        if cand.name == "artifacts" and (cand / "02_results").is_dir():
            return cand
        if (cand / "02_results").is_dir() and (cand / "03_images").is_dir():
            return cand
    raise FileNotFoundError(f"artifacts root not found from {hint}")


def _resolve_run_dir(campaign_dir: Path, run: str) -> Path:
    """Accept campaign dir that already contains RUNXX, or the run directory itself."""
    direct = campaign_dir / run
    if direct.is_dir():
        return direct
    if campaign_dir.is_dir() and (campaign_dir / "telemetry_downlink_gcs.csv").is_file():
        return campaign_dir
    raise FileNotFoundError(f"run dir not found: {campaign_dir} / {run}")


def load_belief_frame(campaign_dir: Path, run: str) -> pd.DataFrame:
    """Truth vs GCS-belief NED frame for one run (one row per GCS arrival)."""
    run_dir = _resolve_run_dir(Path(campaign_dir), run)
    t0 = int(json.loads((run_dir / "four_layer_summary.json").read_text())["T0_mono_ns"])
    gcs = pd.read_csv(run_dir / "telemetry_downlink_gcs.csv").sort_values("t_mono_ns").reset_index(drop=True)
    uav = pd.read_csv(run_dir / "c2_telemetry_uav.csv").sort_values("t_mono_ns").reset_index(drop=True)

    t_gcs = gcs["t_mono_ns"].to_numpy()
    idx = np.searchsorted(uav["t_mono_ns"].to_numpy(), t_gcs, side="right") - 1  # latest t_uav <= t_gcs
    ok = idx >= 0
    gx, gy = gcs["x"].to_numpy(), gcs["y"].to_numpy()
    belief_x = np.r_[gx[0], gx[:-1]]       # previous GCS sample (first row: itself)
    belief_y = np.r_[gy[0], gy[:-1]]
    ii = np.where(ok, idx, 0)
    out = pd.DataFrame(
        {
            "t_rel_s": (t_gcs - t0) / 1e9,
            "truth_x": uav["x"].to_numpy()[ii],
            "truth_y": uav["y"].to_numpy()[ii],
            "gcs_x": belief_x,
            "gcs_y": belief_y,
        }
    )[ok]
    return out.sort_values("t_rel_s").reset_index(drop=True)

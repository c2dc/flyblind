#!/usr/bin/env python3
##################################################################################################################################
# Module: delivery_ratio_overlap.py (UAV→GCS NED delivery ratio on temporal overlap only)                                        #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Default metric for the new pipeline (does not modify freshness_metrics.py).                                       #
#                                                                                                                                #
# Reads: c2_telemetry_uav.csv   (column t_mono_ns) telemetry_downlink_gcs.csv  (or c2_arrival / same schema with t_mono_ns)      #
#                                                                                                                                #
# overlap = [max(t_uav_min, t_gcs_min), min(t_uav_max, t_gcs_max)] ratio = n_gcs_in_overlap / n_uav_in_overlap loss_pct = 100 *  #
#   (1 - ratio)                                                                                                                  #
##################################################################################################################################
"""UAV→GCS NED delivery ratio on temporal overlap only.

Default metric for the new pipeline (does not modify freshness_metrics.py).

Reads:
  c2_telemetry_uav.csv   (column t_mono_ns)
  telemetry_downlink_gcs.csv  (or c2_arrival / same schema with t_mono_ns)

overlap = [max(t_uav_min, t_gcs_min), min(t_uav_max, t_gcs_max)]
ratio = n_gcs_in_overlap / n_uav_in_overlap
loss_pct = 100 * (1 - ratio)
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def _times(path: Path, col: str = "t_mono_ns") -> list[int]:
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = row.get(col)
            if v is None or v == "":
                continue
            try:
                out.append(int(v))
            except ValueError:
                continue
    return out


def compute(uav_csv: Path, gcs_csv: Path) -> dict:
    uav = _times(uav_csv)
    gcs = _times(gcs_csv)
    meta = {
        "schema": "flyblind_delivery_ratio_overlap_v1",
        "uav_csv": str(uav_csv),
        "gcs_csv": str(gcs_csv),
        "ned_samples_uav_file": len(uav),
        "ned_samples_gcs_file": len(gcs),
        "ok": False,
    }
    if not uav or not gcs:
        meta["reason"] = "missing UAV or GCS NED rows"
        return meta
    t0 = max(min(uav), min(gcs))
    t1 = min(max(uav), max(gcs))
    if t1 <= t0:
        meta["reason"] = "no temporal overlap"
        meta["t0_ns"] = t0
        meta["t1_ns"] = t1
        return meta
    u_ov = [t for t in uav if t0 <= t <= t1]
    g_ov = [t for t in gcs if t0 <= t <= t1]
    ratio = (len(g_ov) / len(u_ov)) if u_ov else None
    meta.update({
        "ok": True,
        "overlap_t0_ns": t0,
        "overlap_t1_ns": t1,
        "overlap_duration_s": (t1 - t0) / 1e9,
        "ned_samples_uav_overlap": len(u_ov),
        "ned_samples_gcs_overlap": len(g_ov),
        "uav_to_gcs_delivery_ratio_overlap": round(ratio, 6) if ratio is not None else None,
        "loss_percent_overlap": round(100.0 * (1.0 - ratio), 4) if ratio is not None else None,
        "legacy_file_ratio": round(len(gcs) / len(uav), 6) if uav else None,
        "note": "Prefer *_overlap over file-level uav_to_gcs_delivery_ratio (asymmetric windows).",
    })
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description="delivery_ratio on overlap")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    run = Path(args.run_dir)
    gcs = run / "telemetry_downlink_gcs.csv"
    if not gcs.is_file():
        # some runs name it differently
        alt = run / "c2_arrival.csv"
        gcs = alt if alt.is_file() else gcs
    report = compute(run / "c2_telemetry_uav.csv", gcs)
    out = Path(args.out) if args.out else run / "flyblind_delivery_ratio_overlap.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

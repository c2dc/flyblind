#!/usr/bin/env python3
##################################################################################################################################
# Module: belief_by_phase.py (Belief error and telemetry AoI per phase (PRE / ATTACK / DRAIN), recomputed from the raw captures) #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: The per-run pipeline reports belief only for the whole run. This script applies the pipeline's own definition     #
#   (freshness_metrics.merge_telemetry_downlink) and helpers, adding only the phase split: belief = distance between the         #
#   previous GCS sample (frozen belief) and the latest SITL-egress position with t_uav <= t_gcs_arrival; one value per GCS       #
#   arrival AoI    = t_gcs_arrival - t_uav_egress of the same message Each value is assigned to a phase by its GCS arrival time  #
#   with the [lo, hi) windows of flyblind_phase_windows.json (kpi_leg.assign_phase_half_open).                                   #
#                                                                                                                                #
# Validation: the ALL-phase belief p50 must equal the pipeline value in four_layer_summary.json. Outputs (out/):                 #
#   belief_by_phase_runs.csv, belief_by_phase.csv/.json                                                                          #
##################################################################################################################################
"""Belief error and telemetry AoI per phase (PRE / ATTACK / DRAIN), recomputed from the raw captures.

The per-run pipeline reports belief only for the whole run. This script applies the pipeline's own
definition (freshness_metrics.merge_telemetry_downlink) and helpers, adding only the phase split:
  belief = distance between the previous GCS sample (frozen belief) and the latest SITL-egress
           position with t_uav <= t_gcs_arrival; one value per GCS arrival
  AoI    = t_gcs_arrival - t_uav_egress of the same message
Each value is assigned to a phase by its GCS arrival time with the [lo, hi) windows of
flyblind_phase_windows.json (kpi_leg.assign_phase_half_open).

Validation: the ALL-phase belief p50 must equal the pipeline value in four_layer_summary.json.
Outputs (out/): belief_by_phase_runs.csv, belief_by_phase.csv/.json
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONDITIONS, OUT, condition_order, get, included_runs, load_json, load_manifest, pooled,  # noqa: E402
                    run_path, stats, write_csv, write_json)
import freshness_metrics as fm  # noqa: E402
from kpi_leg import assign_phase_half_open  # noqa: E402
from phase_windows import load_t0_mono_ns  # noqa: E402

PHASES = ("PRE", "ATTACK", "DRAIN", "ALL")
TOL_M = 1e-6


def per_phase(rd: Path) -> dict:
    windows = (load_json(rd / "flyblind_phase_windows.json").get("windows")) or {}
    t0 = load_t0_mono_ns(rd)
    with open(rd / "telemetry_downlink_gcs.csv", encoding="utf-8") as f:
        gcs_rows = list(csv.DictReader(f))
    uav_by_key, uav_timeline = {}, []
    if (rd / "c2_telemetry_uav.csv").is_file():
        with open(rd / "c2_telemetry_uav.csv", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = fm._telemetry_uav_lookup_key(row)
                t_uav = fm._telemetry_row_int(row, "t_mono_ns")
                xyz = fm._telemetry_row_xyz(row)
                if key is None or t_uav is None:
                    continue
                uav_by_key[key] = (t_uav, xyz)
                if xyz is not None:
                    uav_timeline.append((t_uav, xyz))
    uav_timeline.sort(key=lambda p: p[0])

    def phase_of(t_ns: int):
        if t0 is None or not windows:
            return None
        return assign_phase_half_open((t_ns - t0) / 1e9, windows)

    belief = {p: [] for p in PHASES}
    aoi = {p: [] for p in PHASES}
    gcs_parsed = []
    for row in gcs_rows:
        t_gcs = fm._telemetry_row_int(row, "t_mono_ns")
        if t_gcs is None:
            continue
        key = fm._telemetry_uav_lookup_key(row)
        hit = uav_by_key.get(key) if key is not None else None
        if hit is not None:
            d = (t_gcs - hit[0]) / 1e6
            aoi["ALL"].append(d)
            ph = phase_of(t_gcs)
            if ph:
                aoi[ph].append(d)
        xyz = fm._telemetry_row_xyz(row)
        if xyz is not None:
            gcs_parsed.append((t_gcs, xyz))
    for i in range(1, len(gcs_parsed)):
        t_arrive, _ = gcs_parsed[i]
        truth = fm._latest_uav_xyz_at_or_before(uav_timeline, t_arrive)
        if truth is None:
            continue
        b = fm._xyz_dist_m(gcs_parsed[i - 1][1], truth)
        belief["ALL"].append(b)
        ph = phase_of(t_arrive)
        if ph:
            belief[ph].append(b)
    return {p: {"belief_m": fm._summary_stats(belief[p]), "aoi_ms": fm._summary_stats(aoi[p])} for p in PHASES}


def main() -> int:
    manifest = load_manifest()
    rows, mismatches = [], []
    for run in included_runs(manifest):
        rd = run_path(run)
        res = per_phase(rd)
        ref = get(load_json(rd / "four_layer_summary.json"), "freshness.telemetry_downlink.belief_validation.belief_p50_m")
        mine = res["ALL"]["belief_m"].get("p50")
        check_ok = (ref is None and mine is None) or (ref is not None and mine is not None and abs(ref - mine) <= TOL_M)
        if not check_ok:
            mismatches.append((run["run_id"], ref, mine))
        for p in PHASES:
            b, a = res[p]["belief_m"], res[p]["aoi_ms"]
            rows.append({"run_id": run["run_id"], "condition": pooled(run["condition"]), "category": run["category"],
                         "phase": p, "n_belief": b.get("n"), "belief_p50_m": b.get("p50"), "belief_p99_m": b.get("p99"),
                         "n_aoi": a.get("n"), "aoi_p50_ms": a.get("p50"), "aoi_p99_ms": a.get("p99"),
                         "pipeline_belief_p50_m_all": ref if p == "ALL" else None,
                         "validation_ok": check_ok if p == "ALL" else None})
    write_csv(OUT / "belief_by_phase_runs.csv", rows, list(rows[0].keys()))
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["condition"], r["category"], r["phase"]), []).append(r)
    agg = []
    for (cid, cat, p), rs in sorted(groups.items(), key=lambda kv: (condition_order(kv[0][0]), PHASES.index(kv[0][2]))):
        a = {"condition": cid, "item": CONDITIONS.get(cid, ("", "", ""))[0], "label": CONDITIONS.get(cid, ("", "", cid))[2],
             "category": cat, "phase": p, "n_runs": len(rs)}
        for m in ("belief_p50_m", "belief_p99_m", "aoi_p50_ms", "aoi_p99_ms"):
            a[m] = stats(r[m] for r in rs)
        agg.append(a)
    write_json(OUT / "belief_by_phase.json", {"schema": "flyblind_belief_by_phase_v1",
                                              "validation": {"tolerance_m": TOL_M, "mismatches": mismatches},
                                              "rows": agg})
    flat = [{**{k: a[k] for k in ("condition", "item", "label", "category", "phase", "n_runs")},
             **{f"{m}_mean": a[m]["mean"] for m in ("belief_p50_m", "belief_p99_m", "aoi_p50_ms", "aoi_p99_ms")},
             **{f"{m}_sd": a[m]["sd"] for m in ("belief_p50_m", "aoi_p50_ms")}} for a in agg]
    write_csv(OUT / "belief_by_phase.csv", flat, list(flat[0].keys()))
    n_runs = len({r["run_id"] for r in rows})
    print(f"[belief] {n_runs} runs; ALL-phase belief p50 equals the pipeline value in "
          f"{n_runs - len(mismatches)}/{n_runs} runs (tolerance {TOL_M} m)")
    for m in mismatches:
        print(f"[belief] MISMATCH {m}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())

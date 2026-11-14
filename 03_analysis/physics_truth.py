#!/usr/bin/env python3
##################################################################################################################################
# Module: physics_truth.py (Physical tracking from the SITL truth, decomposed: start-up transient, along-track lag, cross-track) #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: trk_truth.json reports || p_truth(t) - p_plan(t) || over the whole stream. Its p50/p95 describe the flight, but   #
#   its p99 (~51 m) and maximum (~97 m) do not: they come from the first seconds of the stream, while the vehicle is still       #
#   flying from its hover point after takeoff to the first reference point of the route. That start-up transient is ~1.5 % of    #
#   the samples -- enough to set the p99, not the p95 -- and its size is fixed by geometry, which is why the maximum reads 97.1  #
#   m in every run of every condition.                                                                                           #
#                                                                                                                                #
# This script separates the two and decomposes the rest along the path:                                                          #
#                                                                                                                                #
# acquisition   from the first setpoint until the truth error first stays below ACQ_M for ACQ_HOLD_S (the harness's own          #
#   acquisition rule, 10 m held for 2 s, applied to the TRUTH position; the harness applies it to the position received at the   #
#   GCS, which is the belief) steady        everything after it along         signed component of (p_truth - p_plan) along the   #
#   reference direction; negative means the vehicle trails the moving reference point cross         unsigned component across    #
#   it: the distance from the path itself                                                                                        #
#                                                                                                                                #
# The join replicates freshness_metrics.compute_trk_truth exactly (same clock-offset correction, same 500 ms join bound, same    #
#   helpers), and the whole-stream statistics are checked against trk_truth.json.                                                #
#                                                                                                                                #
# Why this is computed here and not in the harness: the per-run pipeline has a steady-window statistic                           #
#   (steady_tracking_error_truth_m) but it is empty, because setpoints.csv does not persist the trajectory_window column. The    #
#   window is decided inside setpoint_stream.py, where acquisition also restarts the trajectory clock, so it is part of live     #
#   flight logic; recomputing offline from the truth leaves every run untouched.                                                 #
#                                                                                                                                #
# Outputs (out/): physics_truth_runs.csv, physics_truth.csv/.json                                                                #
##################################################################################################################################
"""Physical tracking from the SITL truth, decomposed: start-up transient, along-track lag, cross-track.

trk_truth.json reports || p_truth(t) - p_plan(t) || over the whole stream. Its p50/p95 describe the flight,
but its p99 (~51 m) and maximum (~97 m) do not: they come from the first seconds of the stream, while the
vehicle is still flying from its hover point after takeoff to the first reference point of the route.
That start-up transient is ~1.5 % of the samples -- enough to set the p99, not the p95 -- and its size is
fixed by geometry, which is why the maximum reads 97.1 m in every run of every condition.

This script separates the two and decomposes the rest along the path:

  acquisition   from the first setpoint until the truth error first stays below ACQ_M for ACQ_HOLD_S
                (the harness's own acquisition rule, 10 m held for 2 s, applied to the TRUTH position;
                the harness applies it to the position received at the GCS, which is the belief)
  steady        everything after it
  along         signed component of (p_truth - p_plan) along the reference direction; negative means
                the vehicle trails the moving reference point
  cross         unsigned component across it: the distance from the path itself

The join replicates freshness_metrics.compute_trk_truth exactly (same clock-offset correction, same
500 ms join bound, same helpers), and the whole-stream statistics are checked against trk_truth.json.

Why this is computed here and not in the harness: the per-run pipeline has a steady-window statistic
(steady_tracking_error_truth_m) but it is empty, because setpoints.csv does not persist the
trajectory_window column. The window is decided inside setpoint_stream.py, where acquisition also
restarts the trajectory clock, so it is part of live flight logic; recomputing offline from the truth
leaves every run untouched.

Outputs (out/): physics_truth_runs.csv, physics_truth.csv/.json
"""
from __future__ import annotations

import csv
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONDITIONS, OUT, included_runs, load_json, load_manifest, open_maybe_gz,  # noqa: E402
                    pooled, result_exists, run_path, stats, write_csv, write_json)
import freshness_metrics as fm  # noqa: E402

ACQ_M = 10.0          # setpoint_stream.ACQUISITION_CROSS_TRACK_M
ACQ_HOLD_S = 2.0      # setpoint_stream.ACQUISITION_HOLD_S
MAX_JOIN_GAP_MS = 500.0
TOL_M = 1e-6


def q(xs: list[float], p: float) -> Optional[float]:
    return fm._summary_stats(xs).get(p) if xs else None


def per_run(rd: Path) -> Optional[dict[str, Any]]:
    trk = load_json(rd / "trk_truth.json") if result_exists(rd / "trk_truth.json") else {}
    if not trk.get("ok") or not result_exists(rd / "setpoints.csv") or not result_exists(rd / "c2_telemetry_uav.csv"):
        return None
    offset_ns = int(trk.get("clock_offset_applied_ns") or 0)
    timeline: list[tuple[int, Any]] = []
    with open_maybe_gz(rd / "c2_telemetry_uav.csv") as fh:
        for row in csv.DictReader(fh):
            t, xyz = fm._telemetry_row_int(row, "t_mono_ns"), fm._telemetry_row_xyz(row)
            if t is not None and xyz is not None:
                timeline.append((t, xyz))
    timeline.sort(key=lambda p: p[0])
    times = [t for t, _ in timeline]
    max_gap = int(MAX_JOIN_GAP_MS * 1e6)

    refs: list[tuple[int, tuple[float, float, float]]] = []
    with open_maybe_gz(rd / "setpoints.csv") as fh:
        for row in csv.DictReader(fh):
            try:
                ref = (float(row["x_ref"]), float(row["y_ref"]), float(row.get("z_ref") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            t = fm._telemetry_row_int(row, "t_generation_gcs_ns") or fm._telemetry_row_int(row, "t_send_gcs_ns")
            if t is not None:
                refs.append((t, ref))

    joined: list[dict[str, Any]] = []
    for i, (t_gen, ref) in enumerate(refs):
        t_q = t_gen - offset_ns
        truth = fm._latest_uav_xyz_at_or_before(timeline, t_q)
        if truth is None:
            continue
        # same freshness bound as compute_trk_truth
        lo, hi, best = 0, len(times) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if times[mid] <= t_q:
                best, lo = times[mid], mid + 1
            else:
                hi = mid - 1
        if best is None or t_q - best > max_gap:
            continue
        j = i + 1 if i + 1 < len(refs) else i - 1
        dx, dy = refs[j][1][0] - ref[0], refs[j][1][1] - ref[1]
        if j < i:
            dx, dy = -dx, -dy
        n = math.hypot(dx, dy)
        ex, ey = truth[0] - ref[0], truth[1] - ref[1]
        along = cross = None
        if n > 1e-9:
            tx, ty = dx / n, dy / n
            along, cross = tx * ex + ty * ey, abs(-ty * ex + tx * ey)
        joined.append({"t_s": (t_gen - refs[0][0]) / 1e9, "err": fm._xyz_dist_m(truth, ref),
                       "along": along, "cross": cross, "ref": ref})
    if not joined:
        return None

    # acquisition end: first instant from which the truth error stays below ACQ_M for ACQ_HOLD_S
    acq_end, below_since = None, None
    for r in joined:
        if r["err"] < ACQ_M:
            below_since = r["t_s"] if below_since is None else below_since
            if r["t_s"] - below_since >= ACQ_HOLD_S:
                acq_end = below_since
                break
        else:
            below_since = None
    acq = [r for r in joined if acq_end is None or r["t_s"] < acq_end]
    std = [r for r in joined if acq_end is not None and r["t_s"] >= acq_end]

    # reference speed over the steady part (distance between consecutive reference points per second)
    speeds = []
    for a, b in zip(std, std[1:]):
        dt = b["t_s"] - a["t_s"]
        if dt > 0:
            speeds.append(math.dist(a["ref"][:2], b["ref"][:2]) / dt)
    v = statistics.median(speeds) if speeds else None

    whole = [r["err"] for r in joined]
    ref_stats = trk.get("tracking_error_truth_m") or {}
    match = all(abs((q(whole, k) or 0) - (ref_stats.get(k) or 0)) <= TOL_M for k in ("p50", "p95", "p99", "max"))
    along_s = [r["along"] for r in std if r["along"] is not None]
    cross_s = [r["cross"] for r in std if r["cross"] is not None]
    err_s = [r["err"] for r in std]
    lag_m = -q(along_s, "p50") if along_s else None
    return {
        "n_joined": len(joined), "matches_trk_truth_json": match,
        "whole_p50_m": q(whole, "p50"), "whole_p95_m": q(whole, "p95"),
        "whole_p99_m": q(whole, "p99"), "whole_max_m": q(whole, "max"),
        "acquisition_s": acq_end, "acquisition_share": len(acq) / len(joined) if acq_end is not None else None,
        "acquisition_start_err_m": joined[0]["err"],
        "steady_p50_m": q(err_s, "p50"), "steady_p95_m": q(err_s, "p95"),
        "steady_p99_m": q(err_s, "p99"), "steady_max_m": q(err_s, "max"),
        "along_p50_m": q(along_s, "p50"),
        "cross_p50_m": q(cross_s, "p50"), "cross_p95_m": q(cross_s, "p95"),
        "cross_p99_m": q(cross_s, "p99"), "cross_max_m": q(cross_s, "max"),
        "ref_speed_mps": v, "lag_m": lag_m, "lag_s": (lag_m / v) if (lag_m is not None and v) else None,
    }


METRICS = ("whole_p50_m", "whole_p95_m", "whole_p99_m", "whole_max_m", "acquisition_s", "acquisition_share",
           "acquisition_start_err_m", "steady_p50_m", "steady_p95_m", "steady_p99_m", "steady_max_m",
           "along_p50_m", "cross_p50_m", "cross_p95_m", "cross_p99_m", "cross_max_m", "ref_speed_mps",
           "lag_m", "lag_s")


def main() -> int:
    manifest = load_manifest()
    runs = [r for r in included_runs(manifest, ("campaign",)) if "traj-route" in r["run_dir"]]
    rows = []
    for r in runs:
        res = per_run(run_path(r))
        if res is None:
            continue
        rows.append({"run_id": r["run_id"], "condition": pooled(r["condition"]), **res})
    if not rows:
        print("[physics] no route runs with trk_truth; nothing written")
        return 0
    write_csv(OUT / "physics_truth_runs.csv", rows, list(rows[0].keys()))

    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["condition"], []).append(r)
    agg = [{"condition": c, "label": CONDITIONS.get(c, ("", "", c))[2], "n_runs": len(rs),
            **{m: stats(x[m] for x in rs) for m in METRICS},
            "median": {m: statistics.median([x[m] for x in rs if x[m] is not None]) if any(x[m] is not None for x in rs) else None
                       for m in METRICS}}
           for c, rs in by.items()]
    pooled_all = {m: statistics.median([x[m] for x in rows if x[m] is not None]) for m in METRICS
                  if any(x[m] is not None for x in rows)}
    mismatches = [r["run_id"] for r in rows if not r["matches_trk_truth_json"]]
    write_json(OUT / "physics_truth.json", {
        "schema": "flyblind_physics_truth_v1", "acquisition_rule": {"err_below_m": ACQ_M, "hold_s": ACQ_HOLD_S},
        "n_runs": len(rows), "whole_stream_matches_trk_truth_json": len(rows) - len(mismatches),
        "mismatched_runs": mismatches, "median_over_all_runs": pooled_all, "by_condition": agg})
    flat = [{"condition": a["condition"], "n_runs": a["n_runs"], **{m: a["median"][m] for m in METRICS}} for a in agg]
    write_csv(OUT / "physics_truth.csv", flat, list(flat[0].keys()))

    print(f"[physics] {len(rows)} route runs; whole-stream stats match trk_truth.json in "
          f"{len(rows) - len(mismatches)}/{len(rows)}")
    P = pooled_all
    print(f"  start-up transient: {P['acquisition_s']:.1f} s, {100 * P['acquisition_share']:.1f} % of samples, "
          f"starting {P['acquisition_start_err_m']:.1f} m from the route")
    print(f"  whole stream   p50 {P['whole_p50_m']:.1f}  p95 {P['whole_p95_m']:.1f}  p99 {P['whole_p99_m']:.1f}  max {P['whole_max_m']:.1f} m")
    print(f"  after it       p50 {P['steady_p50_m']:.1f}  p95 {P['steady_p95_m']:.1f}  p99 {P['steady_p99_m']:.1f}  max {P['steady_max_m']:.1f} m")
    print(f"  along-track    p50 {P['along_p50_m']:.1f} m  (trailing the reference by {P['lag_s']:.2f} s at {P['ref_speed_mps']:.2f} m/s)")
    print(f"  cross-track    p50 {P['cross_p50_m']:.2f}  p95 {P['cross_p95_m']:.1f}  p99 {P['cross_p99_m']:.1f}  max {P['cross_max_m']:.1f} m")
    print("[physics] -> out/physics_truth*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

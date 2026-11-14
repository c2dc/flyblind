#!/usr/bin/env python3
##################################################################################################################################
# Module: formulas_walkthrough.py (Every formula and predicate of the paper, explained and computed step by step on real runs)   #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: This is the reader's entry point to the metrics. The other scripts in this folder aggregate over the whole        #
#   campaign; this one takes ONE baseline run and ONE attack run and shows, term by term, how each quantity is defined, where it #
#   lives in the code, which file it is read from, a few concrete deliveries, and the resulting value -- cross-checked against   #
#   what the per-run pipeline reported.                                                                                          #
#                                                                                                                                #
# Sections 1  delivery age            delta_i = t_arrival_GCS - t_egress_UAV 2  belief vs truth         ||p_GCS[i-1] -           #
#   p_truth(t_arrival_i)|| 3  sample freshness        F_GCS,i = 1{delta_i < tau_fresh},   A_fresh = mean of F over the window 4  #
#   network health          H_net = 1{OWD_cmd_p99 < tau_owd  and  avail > tau_avail  and  linkloss = 0} 5  vehicle health        #
#   H_veh = 1{trk_truth_p95 < tau_trk  and  failsafe = 0  and  mode = GUIDED} 6  the predicate           SSS_i = H_net * H_veh * #
#   (1 - F_GCS,i) 7  effect criterion C1     per leg: OWD p50 >= 1 s  or  OWD p99 >= 150 ms 8  queueing model          AoI = (Q  #
#   / C) * (N + 1) 9  destination detector    A_i = t_i - (min_j(t_j - boot_j) + boot_i)   -- no oracle 10  traps                #
#   belief-derived "tracking", whole-run vs attack window, two t_rel origins                                                     #
#                                                                                                                                #
# Every number is computed from the run artifacts with the harness's own helpers (freshness_metrics, kpi_leg, phase_windows), so #
#   a delivery here is the same delivery the pipeline counted.                                                                   #
#                                                                                                                                #
# Usage python3 formulas_walkthrough.py                       # first a_baseline and first a_atk_n4 run python3                  #
#   formulas_walkthrough.py --baseline DIR --attack DIR python3 formulas_walkthrough.py --tau-trk 20 --examples 5                #
#                                                                                                                                #
# Requires out/campaign_manifest.json (python3 build_manifest.py). Section 8 also uses out/slack.json when present (python3      #
#   slack_recovery.py) to cross-check the fitted capacity against the measured one.                                              #
#                                                                                                                                #
# Outputs (out/): formulas_walkthrough.txt (this printout) and formulas_walkthrough.json (every value).                          #
##################################################################################################################################
"""Every formula and predicate of the paper, explained and computed step by step on real runs.

This is the reader's entry point to the metrics. The other scripts in this folder aggregate over the
whole campaign; this one takes ONE baseline run and ONE attack run and shows, term by term, how each
quantity is defined, where it lives in the code, which file it is read from, a few concrete deliveries,
and the resulting value -- cross-checked against what the per-run pipeline reported.

Sections
   1  delivery age            delta_i = t_arrival_GCS - t_egress_UAV
   2  belief vs truth         ||p_GCS[i-1] - p_truth(t_arrival_i)||
   3  sample freshness        F_GCS,i = 1{delta_i < tau_fresh},   A_fresh = mean of F over the window
   4  network health          H_net = 1{OWD_cmd_p99 < tau_owd  and  avail > tau_avail  and  linkloss = 0}
   5  vehicle health          H_veh = 1{trk_truth_p95 < tau_trk  and  failsafe = 0  and  mode = GUIDED}
   6  the predicate           SSS_i = H_net * H_veh * (1 - F_GCS,i)
   7  effect criterion C1     per leg: OWD p50 >= 1 s  or  OWD p99 >= 150 ms
   8  queueing model          AoI = (Q / C) * (N + 1)
   9  destination detector    A_i = t_i - (min_j(t_j - boot_j) + boot_i)   -- no oracle
  10  traps                   belief-derived "tracking", whole-run vs attack window, two t_rel origins

Every number is computed from the run artifacts with the harness's own helpers (freshness_metrics,
kpi_leg, phase_windows), so a delivery here is the same delivery the pipeline counted.

Usage
  python3 formulas_walkthrough.py                       # first a_baseline and first a_atk_n4 run
  python3 formulas_walkthrough.py --baseline DIR --attack DIR
  python3 formulas_walkthrough.py --tau-trk 20 --examples 5

Requires out/campaign_manifest.json (python3 build_manifest.py). Section 8 also uses out/slack.json when
present (python3 slack_recovery.py) to cross-check the fitted capacity against the measured one.

Outputs (out/): formulas_walkthrough.txt (this printout) and formulas_walkthrough.json (every value).
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ARTIFACTS, OUT, get, included_runs, load_json, load_manifest, num,  # noqa: E402
                    open_maybe_gz, rel_to_artifact, result_exists, write_json)
import freshness_metrics as fm  # noqa: E402
from kpi_leg import assign_phase_half_open  # noqa: E402
from phase_windows import load_t0_mono_ns  # noqa: E402
from physics_truth import per_run as physics_per_run  # noqa: E402

# ---------------------------------------------------------------------------------------------------------
# Thresholds. tau_fresh, tau_owd and tau_avail are the campaign's values; tau_trk is configurable and
# section 5 shows the classification does not depend on it above the baseline p95.
TAU_FRESH_MS = 150.0          # sample freshness at the GCS
TAU_OWD_S = 1.0               # command-leg latency: 1.0 s at >= 95 % (Honeywell TestReport-263, DO-377A)
TAU_AVAIL_PCT = 99.9          # command-leg availability (campaign validity gate)
TAU_TRK_DEFAULT_M = 20.0      # physical tracking; any value above the baseline p95 gives the same answer
TAU_DET_S = 1.0               # destination-side detector alarm threshold
Q_MBIT = 80.0                 # RLC_TX_MAXSIZE = 10 MB per RLC entity = 80 Mbit
C1_P50_S, C1_P99_S = 1.0, 0.150

LINE = "-" * 100


class Out:
    """Prints to stdout and keeps a copy for out/formulas_walkthrough.txt."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, s: str = "") -> None:
        print(s)
        self.lines.append(s)

    def title(self, n: int, s: str) -> None:
        self("")
        self("=" * 100)
        self(f" {n}. {s}")
        self("=" * 100)


P = Out()


def f(x: Optional[float], fmt: str = ".3f", unit: str = "") -> str:
    return "--" if x is None else f"{x:{fmt}}{unit}"


# ---------------------------------------------------------------------------------------------------------
# Loading: one record per GCS arrival, with the same pairing rules the pipeline uses.
def load_run(rd: Path) -> dict[str, Any]:
    windows = (load_json(rd / "flyblind_phase_windows.json").get("windows")
               if result_exists(rd / "flyblind_phase_windows.json") else None) or {}
    t0 = load_t0_mono_ns(rd)

    uav_by_key: dict[Any, tuple[int, Any]] = {}
    uav_timeline: list[tuple[int, Any]] = []
    with open_maybe_gz(rd / "c2_telemetry_uav.csv") as fh:
        for row in csv.DictReader(fh):
            key, t_uav, xyz = fm._telemetry_uav_lookup_key(row), fm._telemetry_row_int(row, "t_mono_ns"), fm._telemetry_row_xyz(row)
            if key is None or t_uav is None:
                continue
            uav_by_key[key] = (t_uav, xyz)
            if xyz is not None:
                uav_timeline.append((t_uav, xyz))
    uav_timeline.sort(key=lambda p: p[0])

    recs: list[dict[str, Any]] = []
    prev_xyz = None
    with open_maybe_gz(rd / "telemetry_downlink_gcs.csv") as fh:
        for row in csv.DictReader(fh):
            t_gcs = fm._telemetry_row_int(row, "t_mono_ns")
            if t_gcs is None:
                continue
            key = fm._telemetry_uav_lookup_key(row)
            hit = uav_by_key.get(key) if key is not None else None
            xyz = fm._telemetry_row_xyz(row)
            t_rel = (t_gcs - t0) / 1e9 if t0 is not None else None
            r = {"t_gcs_ns": t_gcs, "t_rel_s": t_rel,
                 "phase": assign_phase_half_open(t_rel, windows) if (t_rel is not None and windows) else None,
                 "time_boot_ms": fm._telemetry_row_int(row, "time_boot_ms"),
                 "t_egress_ns": hit[0] if hit else None,
                 "delta_ms": (t_gcs - hit[0]) / 1e6 if hit else None,
                 "xyz_gcs": xyz, "belief_m": None, "truth_xyz": None, "belief_xyz": None,
                 "csv_t_rel_s": num(float(row["t_rel_s"])) if row.get("t_rel_s") not in (None, "") else None}
            if xyz is not None:
                if prev_xyz is not None:
                    truth = fm._latest_uav_xyz_at_or_before(uav_timeline, t_gcs)
                    if truth is not None:
                        r["belief_m"] = fm._xyz_dist_m(prev_xyz, truth)
                        r["truth_xyz"], r["belief_xyz"] = truth, prev_xyz
                prev_xyz = xyz
            recs.append(r)

    def opt(name: str) -> dict:
        return load_json(rd / name) if result_exists(rd / name) else {}

    return {"dir": rd, "recs": recs, "windows": windows, "t0_mono_ns": t0,
            "meta": opt("run_meta.json"), "kpi": opt("run_kpi_report.json"), "tel": opt("telemetry_downlink_summary.json"),
            "trk": opt("trk_truth.json"), "four": opt("four_layer_summary.json")}


def vals(run: dict, field: str, phase: Optional[str] = None) -> list[float]:
    return [r[field] for r in run["recs"]
            if r[field] is not None and (phase is None or r["phase"] == phase)]


def p50(xs: list[float]) -> Optional[float]:
    return fm._summary_stats(xs).get("p50") if xs else None


def short(rd: Path) -> str:
    return rd.name[:15] + " ... " + ("attack" if "_attack_" in rd.name else "baseline")


# ---------------------------------------------------------------------------------------------------------
def s1_delta(B: dict, A: dict, k: int) -> dict:
    P.title(1, "Delivery age   delta_i = t_arrival_GCS(i) - t_egress_UAV(i)")
    P("Age of the vehicle state carried by telemetry sample i when it reaches the GCS. Sample i is paired")
    P("across the two captures by (system_id, time_boot_ms) or sequence; the egress time is stamped at the")
    P("UAV before the uplink, so delta_i includes every queue the sample waited in.")
    P("  code:  freshness_metrics.merge_telemetry_downlink        file: telemetry_downlink_summary.json")
    P("         field telemetry_arrival_delay_ms")
    out = {}
    for tag, run in (("baseline", B), ("attack", A)):
        P("")
        P(f"  {tag}: {short(run['dir'])}")
        P(f"    {'t_rel (s)':>10} {'phase':>7} {'t_egress (ns)':>16} {'t_arrival (ns)':>16} {'delta_i':>11}")
        ex = [r for r in run["recs"] if r["delta_ms"] is not None and r["phase"] == "ATTACK"][:: 400][:k]
        for r in ex:
            P(f"    {r['t_rel_s']:10.1f} {r['phase']:>7} {r['t_egress_ns']:16d} {r['t_gcs_ns']:16d} {r['delta_ms']:9.1f} ms")
        mine = p50(vals(run, "delta_ms"))
        mine_att = p50(vals(run, "delta_ms", "ATTACK"))
        pipe = num(get(run["tel"], "telemetry_arrival_delay_ms.p50"))
        P(f"    p50 whole run   recomputed {f(mine, '.1f', ' ms')}   pipeline {f(pipe, '.1f', ' ms')}")
        P(f"    p50 ATTACK win  recomputed {f(mine_att, '.1f', ' ms')}")
        out[tag] = {"delta_p50_ms": mine, "delta_p50_attack_ms": mine_att, "pipeline_p50_ms": pipe}
    P("")
    P(f"  => {f(out['baseline']['delta_p50_ms'], '.1f')} ms at baseline  ->  "
      f"{f((out['attack']['delta_p50_ms'] or 0) / 1000, '.2f')} s under attack.")
    return out


def s2_belief(B: dict, A: dict, k: int) -> dict:
    P.title(2, "Belief vs truth   b_i = || p_GCS[i-1] - p_truth(t_arrival_i) ||")
    P("At each arrival i, the operator still holds the PREVIOUS sample (the frozen belief); the truth is the")
    P("latest SITL egress position with t_egress <= t_arrival_i (where the vehicle actually is right then).")
    P("It never looks at the plan, so route geometry cannot enter it. It needs the simulator's truth, so it")
    P("is the experiment's ruler, not something a real operator can compute (see section 9 for that).")
    P("  code:  freshness_metrics.merge_telemetry_downlink -> belief_vs_truth_m")
    P("  file:  telemetry_downlink_summary.json  belief_vs_truth_m;  validated in belief_validation")
    out = {}
    for tag, run in (("baseline", B), ("attack", A)):
        P("")
        P(f"  {tag}: {short(run['dir'])}")
        P(f"    {'t_rel (s)':>10} {'belief (frozen) NED':>26} {'truth NED':>26} {'b_i':>9}")
        ex = [r for r in run["recs"] if r["belief_m"] is not None and r["phase"] == "ATTACK"][:: 300][:k]
        for r in ex:
            b, t = r["belief_xyz"], r["truth_xyz"]
            P(f"    {r['t_rel_s']:10.1f} ({b[0]:7.1f},{b[1]:7.1f},{b[2]:5.1f}) ({t[0]:7.1f},{t[1]:7.1f},{t[2]:5.1f}) {r['belief_m']:7.1f} m")
        whole, att = p50(vals(run, "belief_m")), p50(vals(run, "belief_m", "ATTACK"))
        ref = num(get(run["four"], "freshness.telemetry_downlink.belief_validation.belief_p50_m"))
        st = get(run["tel"], "belief_validation.status")
        ok = (whole is not None and ref is not None and abs(whole - ref) < 1e-6)
        P(f"    p50 whole run   recomputed {f(whole, '.2f', ' m')}   pipeline {f(ref, '.2f', ' m')}   "
          f"{'MATCH' if ok else 'DIFF'}   belief_validation: {st}")
        P(f"    p50 ATTACK win  recomputed {f(att, '.2f', ' m')}")
        out[tag] = {"belief_p50_m": whole, "belief_p50_attack_m": att, "pipeline_p50_m": ref,
                    "matches_pipeline": ok, "validation_status": st}
    P("")
    P("  Why the attack-window value is larger: the whole run includes the pre-attack segment and the final")
    P("  drain, where the state is fresh and b_i ~ 1 m. Both are correct; always say which window you quote.")
    return out


def s3_fresh(B: dict, A: dict) -> dict:
    P.title(3, f"Sample freshness   F_GCS,i = 1{{delta_i < tau_fresh}},  tau_fresh = {TAU_FRESH_MS:.0f} ms")
    P("F = 1 means sample i reached the GCS fresh. A_fresh is the share of fresh deliveries in a window.")
    P(f"tau_fresh sits above the baseline tail (delta p99 ~ tens of ms) and far below the attack regime (seconds).")
    out = {}
    for tag, run in (("baseline", B), ("attack", A)):
        row = {}
        for win in ("ATTACK", None):
            d = vals(run, "delta_ms", win)
            fresh = sum(1 for x in d if x < TAU_FRESH_MS)
            row["attack" if win else "whole"] = {"n": len(d), "fresh": fresh, "A_fresh": fresh / len(d) if d else None}
        out[tag] = row
        a, w = row["attack"], row["whole"]
        P(f"  {tag:8s}  ATTACK window: {a['fresh']:5d}/{a['n']:5d} fresh  ->  A_fresh = {f(a['A_fresh'], '.4f')}"
          f"     whole run: A_fresh = {f(w['A_fresh'], '.4f')}")
    P("")
    P("  Note: the baseline has no adversary, so its ATTACK window is the same time span with nothing")
    P("  injected (a 'virtual' attack window). It is the like-for-like comparison.")
    return out


def s4_hnet(B: dict, A: dict) -> dict:
    P.title(4, "Network health   H_net = 1{ OWD_cmd_p99 < tau_owd  AND  avail > tau_avail  AND  linkloss = 0 }")
    P("What the operator's monitors look at: the COMMAND leg (GCS -> UAV) and loss-of-link declarations.")
    P(f"  tau_owd = {TAU_OWD_S} s (1.0 s at >= 95 %, Honeywell TestReport-263 citing DO-377A)")
    P(f"  tau_avail = {TAU_AVAIL_PCT} % (campaign validity gate)")
    P("  linkloss = loss-of-link declarations a QGC-style monitor would raise (K8)")
    P("  file: run_kpi_report.json -> legs_table.command  (owd_p99_s, delivery_rate_post_warmup,")
    P("        criterion_C4.checks.QGC_style_gap_3_5s.loss_declarations)")
    out = {}
    for tag, run in (("baseline", B), ("attack", A)):
        c = get(run["kpi"], "legs_table.command") or {}
        owd = num(c.get("owd_p99_s"))
        av = num(get(c, "delivery_rate_post_warmup.delivery_rate_post_warmup_pct"))
        k8 = get(c, "criterion_C4.checks.QGC_style_gap_3_5s") or {}
        ll = k8.get("loss_declarations")
        t1, t2, t3 = (owd is not None and owd < TAU_OWD_S), (av is not None and av > TAU_AVAIL_PCT), (ll == 0)
        h = int(t1 and t2 and t3)
        P(f"  {tag:8s}  OWD_cmd_p99 = {f(owd * 1000 if owd is not None else None, '.1f', ' ms')} {'<' if t1 else '>='} 1 s"
          f"  |  avail = {f(av, '.3f', ' %')} {'>' if t2 else '<='} {TAU_AVAIL_PCT} %"
          f"  |  linkloss = {ll}  (max heartbeat gap {f(num(k8.get('max_gap_s')), '.2f', ' s')})"
          f"  ->  H_net = {h}")
        out[tag] = {"owd_cmd_p99_s": owd, "avail_pct": av, "linkloss": ll, "H_net": h}
    P("")
    P("  => H_net = 1 in BOTH runs: the link looks the same to the operator with and without the attack.")
    return out


def s5_hveh(B: dict, A: dict, tau_trk: float) -> dict:
    P.title(5, "Vehicle health   H_veh = 1{ trk_truth_p95 < tau_trk  AND  failsafe = 0  AND  mode = GUIDED }")
    P("trk_truth(t) = || p_truth(t) - p_plan(t) ||: the SITL egress position against the reference at the")
    P("same instant. It is PHYSICAL: the truth is immune to delivery delay, and the join only accepts a truth")
    P("sample within max_join_gap_ms (500 ms) of the reference instant.")
    P("  code: freshness_metrics.compute_trk_truth     file: trk_truth.json -> tracking_error_truth_m")
    P("        run_meta.json -> outcomes.failsafe, outcomes.mode_transitions")
    out = {"tau_trk_m": tau_trk}
    for tag, run in (("baseline", B), ("attack", A)):
        te = get(run["trk"], "tracking_error_truth_m") or {}
        p95, p99 = num(te.get("p95")), num(te.get("p99"))
        o = get(run["meta"], "outcomes") or {}
        fs = o.get("failsafe")
        modes = [(m.get("from"), m.get("to")) for m in (o.get("mode_transitions") or [])]
        only_guided = all(m in (("LAND", "GUIDED"), ("GUIDED", "LAND")) for m in modes)
        h = int(p95 is not None and p95 < tau_trk and fs is False and only_guided)
        P(f"  {tag:8s}  trk_truth p50/p95/p99 = {f(num(te.get('p50')), '.1f')} / {f(p95, '.1f')} / {f(p99, '.1f')} m"
          f"  join rejected: {run['trk'].get('setpoints_skipped_gap')}"
          f"  |  failsafe = {fs}  |  modes {modes}  ->  H_veh = {h}")
        out[tag] = {"trk_truth_p95_m": p95, "trk_truth_p99_m": p99, "failsafe": fs, "modes": modes, "H_veh": h}

    P("")
    P("  Sensitivity to tau_trk (p95, the statistic the predicate uses):")
    P(f"    {'tau_trk (m)':>12} {'H_veh baseline':>16} {'H_veh attack':>14}")
    sweep = []
    for t in (5, 10, 15, 20, 30, 50, 60):
        hb = int(out["baseline"]["trk_truth_p95_m"] < t)
        ha = int(out["attack"]["trk_truth_p95_m"] < t)
        sweep.append({"tau": t, "baseline": hb, "attack": ha})
        P(f"    {t:12d} {hb:16d} {ha:14d}")
    out["sweep_p95"] = sweep
    P("    Baseline and attack get the SAME H_veh at every tau, because trk_truth does not move under attack.")
    P(f"    Any tau above the baseline p95 ({f(out['baseline']['trk_truth_p95_m'], '.1f')} m) gives H_veh = 1 in both.")

    P("")
    P("  Why p95 and not p99 -- the historical problem, reproduced:")
    bp99, ap99 = out["baseline"]["trk_truth_p99_m"], out["attack"]["trk_truth_p99_m"]
    P(f"    trk_truth p99 = {f(bp99, '.1f')} m at BASELINE and {f(ap99, '.1f')} m under attack. That tail is NOT")
    P("    route curves: it is the start-up transient. When the stream begins, the vehicle is still at its")
    P("    hover point after takeoff, far from the first reference point, and the error is large until it")
    P("    reaches the route. physics_truth.py separates it (harness rule: error < 10 m held for 2 s):")
    for tag, run in (("baseline", B), ("attack", A)):
        ph = physics_per_run(run["dir"])
        if ph is None:
            continue
        out[tag]["physics"] = ph
        P(f"      {tag:8s} start-up {ph['acquisition_s']:.1f} s = {100 * ph['acquisition_share']:.1f} % of samples,"
          f" from {ph['acquisition_start_err_m']:.1f} m  |  after it: p95 {ph['steady_p95_m']:.1f}, p99"
          f" {ph['steady_p99_m']:.1f}, max {ph['steady_max_m']:.1f} m")
        P(f"      {'':8s} along-track {ph['along_p50_m']:.1f} m (trailing by {ph['lag_s']:.2f} s), cross-track p50"
          f" {ph['cross_p50_m']:.2f} m, p99 {ph['cross_p99_m']:.1f} m -> the vehicle is ON the path")
    P("    About 1.6 % of the samples is enough to set the p99 but not the p95. With p99 and a tau_trk sized")
    P("    for the flight (~10 m), H_veh = 0 in both runs, so SSS_i = 0 everywhere and the predicate would")
    P("    never fire -- for a reason unrelated to the attack. p95 keeps the physical reading and leaves the")
    P("    start-up transient out.")
    return out


def s6_sss(B: dict, A: dict, hnet: dict, hveh: dict, fresh: dict) -> dict:
    P.title(6, "The predicate   SSS_i = H_net * H_veh * (1 - F_GCS,i)")
    P("H_net and H_veh are WINDOW indicators; only F_GCS,i varies per delivery. When the window holds")
    P("H_net = H_veh = 1 the predicate reduces to  SSS_i = 1 - F_GCS,i : the network and the vehicle look")
    P("healthy and the sample at the GCS is stale. That is the false-healthy condition, made testable.")
    out = {}
    for tag, run in (("baseline", B), ("attack", A)):
        H = hnet[tag]["H_net"] * hveh[tag]["H_veh"]
        d = vals(run, "delta_ms", "ATTACK")
        sss = [H * (0 if x < TAU_FRESH_MS else 1) for x in d]
        share = sum(sss) / len(sss) if sss else None
        af = fresh[tag]["attack"]["A_fresh"]
        P(f"  {tag:8s}  H_net*H_veh = {H}   ATTACK window: {sum(sss)}/{len(sss)} deliveries with SSS_i = 1"
          f"   -> share {f(share, '.4f')}   (1 - A_fresh = {f(None if af is None else 1 - af, '.4f')})")
        out[tag] = {"H": H, "n": len(sss), "n_sss": sum(sss), "share": share}
    P("")
    P("  => baseline: the gates are open but every sample is fresh, so SSS_i = 0.")
    P("     attack:   the gates are open AND almost every sample is stale, so SSS_i = 1. When H = 1 the share")
    P("     of SSS_i = 1 is exactly 1 - A_fresh.")
    P("")
    P("  Closing evidence (not part of the formula, but what makes it 'silent'): operator alerts.")
    for tag, run in (("baseline", B), ("attack", A)):
        n = count_alerts(run)
        out[tag]["alerts_attack_window"] = n
        P(f"    {tag:8s}  STATUSTEXT with severity <= 4 inside the ATTACK window: {n}")
    return out


def count_alerts(run: dict) -> Optional[int]:
    p = run["dir"] / "statustext_gcs.csv"
    t0, w = run["t0_mono_ns"], run["windows"]
    if not result_exists(p) or t0 is None or not w:
        return None
    n = 0
    with open_maybe_gz(p) as fh:
        for row in csv.DictReader(fh):
            try:
                sev, t = int(row["severity"]), int(row["t_mono_ns"])
            except (KeyError, TypeError, ValueError):
                continue
            if sev <= 4 and assign_phase_half_open((t - t0) / 1e9, w) == "ATTACK":
                n += 1
    return n


def s7_c1(B: dict, A: dict) -> dict:
    P.title(7, f"Effect criterion C1   effect = 1{{ OWD_p50 >= {C1_P50_S:.0f} s  OR  OWD_p99 >= {C1_P99_S * 1000:.0f} ms }}  (per leg)")
    P("Pre-registered before the campaign. Evaluated on each leg separately; 'tail only' when p99 crosses")
    P("but p50 stays below 100 ms. file: run_kpi_report.json -> legs_table.<leg>.criterion_C1")
    out = {}
    for tag, run in (("baseline", B), ("attack", A)):
        for leg in ("command", "return"):
            c1 = get(run["kpi"], f"legs_table.{leg}.criterion_C1") or {}
            a50, a99 = num(c1.get("owd_p50_s")), num(c1.get("owd_p99_s"))
            mine = bool((a50 is not None and a50 >= C1_P50_S) or (a99 is not None and a99 >= C1_P99_S))
            P(f"  {tag:8s} {leg:8s}  p50 {f(a50 * 1000 if a50 is not None else None, '10.1f', ' ms')}"
              f"  p99 {f(a99 * 1000 if a99 is not None else None, '10.1f', ' ms')}"
              f"  -> effect {mine}   (pipeline: {c1.get('effect_present')})")
            out[f"{tag}_{leg}"] = {"p50_s": a50, "p99_s": a99, "effect": mine, "pipeline": c1.get("effect_present")}
    P("")
    P("  => under attack the RETURN leg has the effect and the COMMAND leg does not. That pair is the")
    P("     false-healthy condition in two lines: the leg the operator monitors stays green.")
    return out


def s8_model(manifest: dict) -> dict:
    P.title(8, "Queueing model   AoI = (Q / C) * (N + 1)")
    P("With N backlogged co-tenants, proportional-fair gives the victim a share C/(N+1) of the uplink. If the")
    P("victim offers more than that share, its RLC buffer fills; the age of a sample at the head is buffer")
    P(f"over drain rate, Q / (C/(N+1)). Q = RLC_TX_MAXSIZE = 10 MB = {Q_MBIT:.0f} Mbit; C = cell uplink capacity.")
    P("Valid only while demand > C/(N+1); below that there is no backlog and the age stays at baseline.")
    by_n: dict[int, list[float]] = {}
    for run in included_runs(manifest, ("campaign",)):
        cid = run["condition"]
        if not cid.startswith("a_atk_n"):
            continue
        n = int(cid.rsplit("n", 1)[1])
        tel = load_json(ARTIFACTS / run["run_dir"] / "telemetry_downlink_summary.json")
        v = num(get(tel, "telemetry_arrival_delay_ms.p50"))
        if v is not None:
            by_n.setdefault(n, []).append(v / 1000)
    if not by_n:
        P("  (no dose runs a_atk_n1..n4 in the manifest; section skipped)")
        return {}
    med = {n: statistics.median(v) for n, v in sorted(by_n.items())}
    pts = [(n + 1, a) for n, a in med.items() if a > 1.0]          # only where there is a backlog
    out: dict[str, Any] = {"median_aoi_s_by_N": med}
    if len(pts) >= 2:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        slope = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x in xs)
        icpt = my - slope * mx
        cap = Q_MBIT / slope
        out.update({"slope_s_per_cotenant": slope, "intercept_s": icpt, "C_fit_mbps": cap})
        P("")
        P(f"  Least squares over the N with a backlog:  AoI = {slope:.3f} * (N+1) + {icpt:.3f} s")
        P(f"  slope = Q / C   ->   C = {Q_MBIT:.0f} / {slope:.3f} = {cap:.1f} Mb/s")
        P("")
        P(f"    {'N':>3} {'k':>3} {'measured':>10} {'model':>10} {'error':>8}   note")
        for n, a in med.items():
            pred = (Q_MBIT / cap) * (n + 1)
            share = cap / (n + 1)
            if a > 1.0:
                P(f"    {n:3d} {len(by_n[n]):3d} {a:9.2f}s {pred:9.2f}s {100 * (a - pred) / pred:7.1f}%")
            else:
                P(f"    {n:3d} {len(by_n[n]):3d} {a:9.2f}s {'--':>10} {'--':>8}   share C/(N+1) = {share:.1f} Mb/s >"
                  f" 12 Mb/s offered: no backlog, as the model says")
        sl = OUT / "slack.json"
        if sl.is_file():
            br = get(load_json(sl), "summary.by_prb.106.capacity_bracket_mbps")
            if br:
                inside = br[0] <= cap <= br[1]
                P("")
                P(f"  Independent check: the capacity probe (no adversary, victim video raised until it")
                P(f"  self-congests) brackets C(106 PRB) in [{br[0]:.0f}, {br[1]:.0f}] Mb/s. The fitted C = {cap:.1f}"
                  f" Mb/s is {'INSIDE' if inside else 'OUTSIDE'} it.")
                out["capacity_probe_bracket_mbps"] = br
                out["fit_inside_bracket"] = inside
    return out


def implied_drift_ms_per_s(run: dict) -> Optional[float]:
    """Slope of implied_i = t_i - boot_i against arrival time, on a run where D_i is ~constant (baseline).

    It measures how fast the autopilot's boot clock and the GCS clock drift apart. Any non-zero value
    makes a running-minimum calibration age: A_i grows by slope * (time since calibration).
    """
    pts = [(r["t_gcs_ns"] / 1e9, (r["t_gcs_ns"] - r["time_boot_ms"] * 1_000_000) / 1e6)
           for r in run["recs"] if r["time_boot_ms"] is not None]
    if len(pts) < 10:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in pts) / den if den else None


def detector_ages(recs: list[dict], start=None) -> list[tuple[dict, float]]:
    """Running-minimum detector over recs; calibration starts at the first record accepted by `start`."""
    est, out, on = None, [], start is None
    for r in recs:
        if r["time_boot_ms"] is None:
            continue
        if not on:
            on = start(r)
            if not on:
                continue
        implied = r["t_gcs_ns"] - r["time_boot_ms"] * 1_000_000
        est = implied if est is None or implied < est else est
        out.append((r, max(0.0, (r["t_gcs_ns"] - (est + r["time_boot_ms"] * 1_000_000)) / 1e6)))
    return out


def s9_detector(B: dict, A: dict) -> dict:
    P.title(9, f"Destination-side detector   A_i = t_i - (T0_hat + boot_i),  alarm when A_i > {TAU_DET_S:.0f} s")
    P("Needs nothing but what the telemetry already carries: boot_i = time_boot_ms stamped at the source")
    P("before the uplink, and t_i = the local arrival time at the GCS.")
    P("    implied_i = t_i - boot_i          = T0 + D_i      (D_i = transport delay >= 0)")
    P("    T0_hat    = running min(implied)  ~ T0            (the fastest sample, D ~ 0, calibrates the epoch)")
    P("    A_i       = t_i - (T0_hat + boot_i) = D_i - D_min")
    P("No simulator truth is used, so unlike belief_vs_truth this runs on a real ground station (shipped as")
    P("the QGroundControl detector build, --open-qgc-mitigation).")
    out: dict[str, Any] = {}
    for tag, run in (("baseline", B), ("attack", A)):
        ages = detector_ages(run["recs"])
        att = [a for r, a in ages if r["phase"] == "ATTACK"]
        oracle = vals(run, "delta_ms", "ATTACK")
        alarms = sum(1 for a in att if a > TAU_DET_S * 1000)
        first = next((r["t_rel_s"] for r, a in ages if r["phase"] == "ATTACK" and a > TAU_DET_S * 1000), None)
        onset = get(run["windows"], "ATTACK.t_rel_lo_s")
        det, orc = p50(att), p50(oracle)
        P("")
        P(f"  {tag:8s}  ATTACK window: detector A p50 = {f(det, '.1f', ' ms')}   oracle delta p50 = {f(orc, '.1f', ' ms')}")
        P(f"            samples above {TAU_DET_S:.0f} s: {alarms}/{len(att)}"
          + (f"   first alarm at t_rel {first:.1f} s, attack onset {onset:.1f} s (+{first - onset:.1f} s)"
             if first is not None and onset is not None else ""))
        out[tag] = {"detector_p50_ms": det, "oracle_p50_ms": orc, "n_alarm": alarms, "n": len(att),
                    "first_alarm_t_rel_s": first, "attack_onset_t_rel_s": onset}

    drift = implied_drift_ms_per_s(B)
    out["clock_drift_ms_per_s"] = drift
    P("")
    P("  Why the detector does not equal the oracle exactly -- clock drift:")
    if drift is not None:
        horizon_s = (TAU_DET_S * 1000) / abs(drift) if drift else None
        out["false_alarm_horizon_s"] = horizon_s
        P(f"    On the baseline run, where D_i is ~constant, implied_i drifts at {drift:.4f} ms/s"
          f" ({drift * 1000:.0f} ppm): the autopilot's")
        P("    boot clock and the GCS clock do not tick at exactly the same rate (in SITL, simulated time runs")
        P("    slightly behind wall time). The running minimum is captured early and never moves again, so A_i")
        P("    grows by drift x (time since calibration). That is the tens-of-ms gap to the oracle above --")
        P("    irrelevant against a 1 s threshold and a 13 s effect, but it does not stay irrelevant forever:")
        if horizon_s:
            P(f"    at this rate a fresh link would cross the {TAU_DET_S:.0f} s threshold after {horizon_s:.0f} s"
              f" (~{horizon_s / 60:.0f} min) of mission,")
        P("    i.e. a FALSE alarm on a long flight. A deployed detector needs a leaky minimum (or a drift")
        P("    estimate from a known-good phase) rather than a minimum held for the whole mission.")

    # The calibration limitation, demonstrated: the GCS 'connects' only once the backlog is established.
    steady = 0.9 * (out["attack"]["oracle_p50_ms"] or 0.0)        # backlog at its steady-state depth
    late = detector_ages(A["recs"], start=lambda r: r["delta_ms"] is not None and r["delta_ms"] > steady)
    late_att = [a for r, a in late if r["phase"] == "ATTACK"]
    lp = p50(late_att)
    late_alarms = sum(1 for a in late_att if a > TAU_DET_S * 1000)
    out["late_calibration"] = {"start_when_delta_ms_above": steady, "detector_p50_ms": lp,
                               "n_alarm": late_alarms, "n": len(late_att)}
    P("")
    P("  Second limitation, reproduced -- calibration needs a FRESH sample:")
    P("    If the ground station only connected once the backlog was already established, every D_i it ever")
    P("    sees is already ~13 s, T0_hat absorbs that delay, and the detector reads only the variation around")
    P(f"    it. Calibrating only from the first sample whose delay already reached steady state"
      f" (delta > {steady / 1000:.1f} s)")
    P(f"    gives A p50 = {f(lp, '.1f', ' ms')} and {late_alarms}/{len(late_att)} alarms,")
    P(f"    instead of {f(out['attack']['detector_p50_ms'], '.1f', ' ms')} and {out['attack']['n_alarm']}/{out['attack']['n']}."
      " In the campaign the GCS connects before the attack, so")
    P("    calibration is clean; a deployment must calibrate during a known-good phase.")
    return out


def s10_traps(B: dict, A: dict) -> dict:
    P.title(10, "Traps -- numbers that look right and are not")
    out: dict[str, Any] = {}

    P("  (a) 'Tracking error' computed from the BELIEF is not physical.")
    P("      The testbed also reports cross_track / max_deviation (run_meta.json -> outcomes). They are")
    P("      computed from p_GCS, the position as received (setpoint_stream.py: cur = poll.ned_cache[0]),")
    P("      against the plan now -- so they restate delta_i in meters (~ speed x age).")
    P(f"      {'':10s} {'cross_track p99 (belief)':>26} {'trk_truth p95 (truth)':>23} {'trk_truth p99 (truth)':>23}")
    for tag, run in (("baseline", B), ("attack", A)):
        o = get(run["meta"], "outcomes") or {}
        te = get(run["trk"], "tracking_error_truth_m") or {}
        xt = num(o.get("cross_track_p99_m"))
        P(f"      {tag:10s} {f(xt, '.1f', ' m'):>26} {f(num(te.get('p95')), '.1f', ' m'):>23} {f(num(te.get('p99')), '.1f', ' m'):>23}")
        out[f"{tag}_cross_track_p99_m"] = xt
    P("      The belief-derived column jumps under attack; the truth columns do not move. The airframe flies")
    P("      its path. Never use cross_track or max_deviation as evidence of physical deviation, and never")
    P("      put them in H_veh: they rise exactly when the attack works, which would switch the predicate off.")

    P("")
    P("  (b) Whole-run belief understates the attack.")
    wa = p50(vals(A, "belief_m"))
    aa = p50(vals(A, "belief_m", "ATTACK"))
    P(f"      attack run: whole run {f(wa, '.2f', ' m')}   vs   ATTACK window {f(aa, '.2f', ' m')}")
    P("      The pipeline's per-run belief covers the whole measurement window, which includes fresh")
    P("      pre-attack and drain samples. Both numbers are right; quote the window with the number.")
    out["attack_belief_whole_m"], out["attack_belief_window_m"] = wa, aa

    P("")
    P("  (c) Two t_rel origins, about 90 s apart.")
    P("      flyblind_phase_windows.json, event_markers.csv and clock_anchor.json count from T0 (run start).")
    P("      The MAVLink captures' own t_rel_s column (telemetry_downlink_gcs.csv, statustext_gcs.csv, and")
    P("      c2.json times) counts from the mission start. Slicing those columns against the phase windows")
    P("      shifts every event by the offset. Measure it from a file that carries both clocks:")
    P("        offset = (t_mono_ns - t_rel_s * 1e9 - T0_mono_ns) / 1e9")
    for tag, run in (("baseline", B), ("attack", A)):
        r0 = next((r for r in run["recs"] if r["csv_t_rel_s"] is not None), None)
        off = None
        if r0 is not None and run["t0_mono_ns"] is not None:
            off = (r0["t_gcs_ns"] - r0["csv_t_rel_s"] * 1e9 - run["t0_mono_ns"]) / 1e9
        out[f"{tag}_t_rel_offset_s"] = off
        P(f"      {tag:10s} offset = {f(off, '.3f', ' s')}")
    P("      Every script here assigns phases from t_mono_ns against T0_mono_ns, never from that column.")
    return out


def pick(manifest: dict, cond: str) -> Path:
    runs = [r for r in included_runs(manifest, ("campaign",)) if r["condition"] == cond]
    if not runs:
        raise SystemExit(f"no campaign run of condition {cond} in the manifest; pass --baseline/--attack")
    return ARTIFACTS / runs[0]["run_dir"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--baseline", type=Path, help="run directory of a baseline run (default: first a_baseline)")
    ap.add_argument("--attack", type=Path, help="run directory of an attack run (default: first a_atk_n4)")
    ap.add_argument("--tau-trk", type=float, default=TAU_TRK_DEFAULT_M, help="tau_trk in meters (default %(default)s)")
    ap.add_argument("--examples", type=int, default=3, help="example deliveries printed per run (default %(default)s)")
    a = ap.parse_args()

    manifest = load_manifest()
    bdir = a.baseline.resolve() if a.baseline else pick(manifest, "a_baseline")
    adir = a.attack.resolve() if a.attack else pick(manifest, "a_atk_n4")
    B, A = load_run(bdir), load_run(adir)

    P("FlyBlind -- formulas and predicates, computed step by step on real runs")
    P(LINE)
    P(f"  baseline: {bdir.relative_to(ARTIFACTS) if bdir.is_relative_to(ARTIFACTS) else bdir}")
    P(f"  attack:   {adir.relative_to(ARTIFACTS) if adir.is_relative_to(ARTIFACTS) else adir}")
    P(f"  thresholds: tau_fresh {TAU_FRESH_MS:.0f} ms | tau_owd {TAU_OWD_S} s | tau_avail {TAU_AVAIL_PCT} %"
      f" | tau_trk {a.tau_trk:g} m | detector {TAU_DET_S:g} s | Q {Q_MBIT:.0f} Mbit")

    # Recorded relative to the artifact root: an absolute path would publish the directory layout of
    # whoever ran this, which the double-blind submission must not carry.
    res: dict[str, Any] = {"schema": "flyblind_formulas_walkthrough_v1",
                           "baseline_run": rel_to_artifact(bdir), "attack_run": rel_to_artifact(adir),
                           "thresholds": {"tau_fresh_ms": TAU_FRESH_MS, "tau_owd_s": TAU_OWD_S,
                                          "tau_avail_pct": TAU_AVAIL_PCT, "tau_trk_m": a.tau_trk,
                                          "tau_detector_s": TAU_DET_S, "Q_mbit": Q_MBIT}}
    res["1_delivery_age"] = s1_delta(B, A, a.examples)
    res["2_belief_vs_truth"] = s2_belief(B, A, a.examples)
    res["3_freshness"] = fr = s3_fresh(B, A)
    res["4_H_net"] = hn = s4_hnet(B, A)
    res["5_H_veh"] = hv = s5_hveh(B, A, a.tau_trk)
    res["6_SSS"] = s6_sss(B, A, hn, hv, fr)
    res["7_C1"] = s7_c1(B, A)
    res["8_model"] = s8_model(manifest)
    res["9_detector"] = s9_detector(B, A)
    res["10_traps"] = s10_traps(B, A)

    P("")
    P("=" * 100)
    P(" SUMMARY")
    P("=" * 100)
    d1, d2 = res["1_delivery_age"], res["2_belief_vs_truth"]
    P(f"  {'':34s} {'baseline':>14} {'attack':>14}")
    P(f"  {'delivery age p50 (whole run)':34s} {f(d1['baseline']['delta_p50_ms'], '.1f', ' ms'):>14} "
      f"{f((d1['attack']['delta_p50_ms'] or 0) / 1000, '.2f', ' s'):>14}")
    P(f"  {'belief vs truth p50 (whole run)':34s} {f(d2['baseline']['belief_p50_m'], '.1f', ' m'):>14} "
      f"{f(d2['attack']['belief_p50_m'], '.1f', ' m'):>14}")
    P(f"  {'belief vs truth p50 (ATTACK win.)':34s} {f(d2['baseline']['belief_p50_attack_m'], '.1f', ' m'):>14} "
      f"{f(d2['attack']['belief_p50_attack_m'], '.1f', ' m'):>14}")
    P(f"  {'A_fresh (ATTACK window)':34s} {f(fr['baseline']['attack']['A_fresh'], '.4f'):>14} "
      f"{f(fr['attack']['attack']['A_fresh'], '.4f'):>14}")
    P(f"  {'H_net / H_veh':34s} {str(hn['baseline']['H_net']) + ' / ' + str(hv['baseline']['H_veh']):>14} "
      f"{str(hn['attack']['H_net']) + ' / ' + str(hv['attack']['H_veh']):>14}")
    s6 = res["6_SSS"]
    P(f"  {'share of SSS_i = 1 (ATTACK window)':34s} {f(s6['baseline']['share'], '.4f'):>14} {f(s6['attack']['share'], '.4f'):>14}")
    P(f"  {'operator alerts (ATTACK window)':34s} {str(s6['baseline']['alerts_attack_window']):>14} "
      f"{str(s6['attack']['alerts_attack_window']):>14}")
    P("")
    share = s6["attack"]["share"] or 0.0
    if share > 0.5 and hn["attack"]["H_net"] and hv["attack"]["H_veh"]:
        P("  Same link health, same airframe, same silence from the operator's tools -- and a state that is")
        P("  seconds old. That is Silent State Staleness.")
    else:
        P("  The second run does not hold the predicate: its state reaches the GCS fresh (or a health gate is")
        P("  closed), so SSS_i = 0. This is what a working defence, or an attack below the share, looks like.")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "formulas_walkthrough.txt").write_text("\n".join(P.lines) + "\n", encoding="utf-8")
    write_json(OUT / "formulas_walkthrough.json", res)
    print(f"\n[walkthrough] -> out/formulas_walkthrough.txt, out/formulas_walkthrough.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

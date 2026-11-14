#!/usr/bin/env python3
##################################################################################################################################
# Module: slack_recovery.py (Victim uplink slack (capacity probes) and DRAIN recovery time)                                      #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Part A - slack. Baseline runs (no attack) at 106 PRB with increasing companion video; per run the video offered / #
#   goodput / loss (goodput_mac_report.goodput_from_companion), victim UAV MAC UL Mb/s (prb.json), and the ATTACK-window         #
#   (virtual) return-leg OWD p50 and C1 from run_kpi_report.json. The capacity is bracketed by the largest offered load without  #
#   self-congestion (C1 false) and the smallest one with it; the MAC ceiling is the largest victim MAC UL measured in any probe. #
#                                                                                                                                #
# Part B - DRAIN recovery. The harness criterion (drain_policy.evaluate_recovery_from_summary) is applied offline: sliding       #
#   windows of drain_policy.WINDOW_S seconds, fully after attack_end, stepped by STEP_S, each giving delta_p50 (telemetry AoI    #
#   p50, in s, as in compare_ni_runs.py) and belief_p50; the baseline is the p50 of the PRE window of the same run (phase        #
#   windows, [orbit_acquired, attack_onset)). Windows are limited to the DRAIN phase [attack_end, land_command) and to the run's #
#   drain ceiling, so a run whose DRAIN is shorter than the ceiling and does not recover is censored. recovery_time_s is the END #
#   of the first in-band window (>= WINDOW_S by construction).                                                                   #
#                                                                                                                                #
# Outputs (out/): slack_runs.csv, slack.json, drain_recovery_runs.csv, drain_recovery.json, slack_recovery.tex                   #
##################################################################################################################################
"""Victim uplink slack (capacity probes) and DRAIN recovery time.

Part A - slack. Baseline runs (no attack) at 106 PRB with increasing companion video; per run the
video offered / goodput / loss (goodput_mac_report.goodput_from_companion), victim UAV MAC UL Mb/s
(prb.json), and the ATTACK-window (virtual) return-leg OWD p50 and C1 from run_kpi_report.json.
The capacity is bracketed by the largest offered load without self-congestion (C1 false) and the
smallest one with it; the MAC ceiling is the largest victim MAC UL measured in any probe.

Part B - DRAIN recovery. The harness criterion (drain_policy.evaluate_recovery_from_summary) is
applied offline: sliding windows of drain_policy.WINDOW_S seconds, fully after attack_end, stepped
by STEP_S, each giving delta_p50 (telemetry AoI p50, in s, as in compare_ni_runs.py) and belief_p50;
the baseline is the p50 of the PRE window of the same run (phase windows, [orbit_acquired,
attack_onset)). Windows are limited to the DRAIN phase [attack_end, land_command) and to the run's
drain ceiling, so a run whose DRAIN is shorter than the ceiling and does not recover is censored.
recovery_time_s is the END of the first in-band window (>= WINDOW_S by construction).

Outputs (out/): slack_runs.csv, slack.json, drain_recovery_runs.csv, drain_recovery.json, slack_recovery.tex
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONDITIONS, OUT, get, included_runs, load_json, load_manifest, num, pooled, run_path,  # noqa: E402
                    stats, write_csv, write_json)
import drain_policy as dp  # noqa: E402
import freshness_metrics as fm  # noqa: E402
from goodput_mac_report import goodput_from_companion  # noqa: E402
from phase_windows import load_t0_mono_ns  # noqa: E402

# Part A: the capacity ladders (no adversary; the victim's own video is raised until it self-congests).
# Both cells are included and the bracket is reported PER cell bandwidth, because the share C/(N+1)
# that sets the attack's onset is a fraction of that cell's capacity: C(106) and C(51) are different
# numbers and must not be pooled.
SLACK_CONDITIONS = ["a_baseline", "cap_base_vid24", "cap_base_vid32", "cap_base_vid40",
                    "e_base51", "cap51_base_vid16", "cap51_base_vid24"]
# Reference condition per cell: the baseline at the video rate used by that cell's attack runs.
REFERENCE_BASELINE = {106.0: "a_baseline", 51.0: "e_base51"}
RECOVERY_CONDITIONS = ["g_drain_video_on", "g_drain_video_off", "a_atk_n4"]
STEP_S = 1.0
TOL = 1e-6


# ---------------------------------------------------------------- Part A: slack
def slack_row(run: dict) -> dict:
    rd = run_path(run)
    meta = load_json(rd / "run_meta.json")
    rep = load_json(rd / "run_kpi_report.json")
    prb = load_json(rd / "prb.json") if (rd / "prb.json").is_file() else {}
    gp = goodput_from_companion(load_json(rd / "companion_video.json")) if (rd / "companion_video.json").is_file() else {}
    owd = get(rep, "kpi_c2_real.return.phases.ATTACK.owd_s.primary") or {}
    return {"run_id": run["run_id"], "condition": run["condition"], "category": run["category"],
            "gnb_prb": num(get(meta, "topology_stamp.GNB_PRB")) or num(meta.get("gnb_prb")),
            "video_mbps": num(meta.get("companion_video_mbps")) or 0.0,
            "video_offered_mbps": num(gp.get("offered_mbps")), "video_goodput_mbps": num(gp.get("goodput_mbps")),
            "video_lost_pct": num(gp.get("lost_percent")), "uav_mac_ul_mbps": num(get(prb, "per_role.uav.ul_mbps")),
            "cell_ul_mbps_total": num(get(prb, "cell_ul_activity.ul_mbps_total")),
            "return_owd_p50_s": num(owd.get("p50")), "return_owd_p99_s": num(owd.get("p99")),
            "return_C1_effect_present": get(rep, "kpi_c2_real.return.phases.ATTACK.criterion_C1.effect_present")}


def slack(manifest: dict) -> tuple[list[dict], dict]:
    runs = [r for r in included_runs(manifest) if r["condition"] in SLACK_CONDITIONS]
    rows = [slack_row(r) for r in runs]
    clean = [r["video_mbps"] for r in rows if r["return_C1_effect_present"] is False]
    congested = [r["video_mbps"] for r in rows if r["return_C1_effect_present"] is True]
    mac = [r["uav_mac_ul_mbps"] for r in rows if r["uav_mac_ul_mbps"] is not None]
    by_cond = {}
    for r in rows:
        by_cond.setdefault(r["condition"], []).append(r)
    agg = [{"condition": c, "label": CONDITIONS[c][2], "category": rs[0]["category"], "n_runs": len(rs),
            "gnb_prb": rs[0].get("gnb_prb"), "video_mbps": rs[0]["video_mbps"],
            **{m: stats(x[m] for x in rs) for m in ("video_offered_mbps", "video_goodput_mbps", "video_lost_pct",
                                                     "uav_mac_ul_mbps", "cell_ul_mbps_total", "return_owd_p50_s",
                                                     "return_owd_p99_s")},
            "return_C1": {"true": sum(1 for x in rs if x["return_C1_effect_present"] is True), "of": len(rs)}}
           for c, rs in by_cond.items()]
    agg.sort(key=lambda a: (a.get("gnb_prb") or 0, a["video_mbps"]))

    def bracket(subset: list[dict], ref_cond: str | None) -> dict:
        cl = [r["video_mbps"] for r in subset if r["return_C1_effect_present"] is False]
        cg = [r["video_mbps"] for r in subset if r["return_C1_effect_present"] is True]
        mc = [r["uav_mac_ul_mbps"] for r in subset if r["uav_mac_ul_mbps"] is not None]
        ref = [r["uav_mac_ul_mbps"] for r in subset
               if r["condition"] == ref_cond and r["uav_mac_ul_mbps"] is not None]
        lo, hi = (max(cl) if cl else None), (min(cg) if cg else None)
        out = {"n_runs": len(subset), "reference_condition": ref_cond,
               "max_offered_without_self_congestion_mbps": lo,
               "min_offered_with_self_congestion_mbps": hi,
               "capacity_bracket_mbps": [lo, hi] if (lo is not None and hi is not None) else None,
               "victim_mac_ul_ceiling_mbps": max(mc) if mc else None,
               "victim_mac_ul_at_reference_mbps": stats(ref)["mean"] if ref else None,
               "slack_at_reference_mbps": (max(mc) - stats(ref)["mean"]) if (mc and ref) else None}
        # The share that gates the attack's onset, for the dose used by the campaign (N = 4 co-tenants).
        if out["capacity_bracket_mbps"]:
            out["pf_share_at_n4_mbps"] = [lo / 5.0, hi / 5.0]
        else:
            out["pf_share_at_n4_mbps"] = None
        return out

    by_prb = {}
    for prb in sorted({r["gnb_prb"] for r in rows if r["gnb_prb"] is not None}):
        by_prb[f"{prb:.0f}"] = bracket([r for r in rows if r["gnb_prb"] == prb], REFERENCE_BASELINE.get(prb))
    summary = {
        "by_prb": by_prb,
        # Pooled values kept for continuity; read by_prb instead, the ladders are different cells.
        "max_offered_without_self_congestion_mbps": max(clean) if clean else None,
        "min_offered_with_self_congestion_mbps": min(congested) if congested else None,
        "victim_mac_ul_ceiling_mbps": max(mac) if mac else None,
        "note": "per cell bandwidth, capacity is bracketed by the offered loads (C1 on the return leg, "
                "ATTACK-window, no attack); the ceiling is the largest whole-run victim MAC UL measured; "
                "pf_share_at_n4_mbps = bracket/5, the proportional-fair share the victim must exceed for "
                "the attack to have anything to starve",
    }
    return rows, {"rows": agg, "summary": summary}


# ---------------------------------------------------------------- Part B: DRAIN recovery
def samples(rd: Path, t0: int) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(t_rel_s, AoI ms) and (t_rel_s, belief m) per GCS arrival; same definitions and units as belief_by_phase.py."""
    uav_by_key, uav_timeline = {}, []
    with open(rd / "c2_telemetry_uav.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key, t_uav, xyz = fm._telemetry_uav_lookup_key(row), fm._telemetry_row_int(row, "t_mono_ns"), fm._telemetry_row_xyz(row)
            if key is None or t_uav is None:
                continue
            uav_by_key[key] = (t_uav, xyz)
            if xyz is not None:
                uav_timeline.append((t_uav, xyz))
    uav_timeline.sort(key=lambda p: p[0])
    aoi, belief, gcs = [], [], []
    with open(rd / "telemetry_downlink_gcs.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t_gcs = fm._telemetry_row_int(row, "t_mono_ns")
            if t_gcs is None:
                continue
            key = fm._telemetry_uav_lookup_key(row)
            hit = uav_by_key.get(key) if key is not None else None
            if hit is not None:
                aoi.append(((t_gcs - t0) / 1e9, (t_gcs - hit[0]) / 1e6))
            xyz = fm._telemetry_row_xyz(row)
            if xyz is not None:
                gcs.append((t_gcs, xyz))
    for i in range(1, len(gcs)):
        truth = fm._latest_uav_xyz_at_or_before(uav_timeline, gcs[i][0])
        if truth is not None:
            belief.append(((gcs[i][0] - t0) / 1e9, fm._xyz_dist_m(gcs[i - 1][1], truth)))
    return aoi, belief


def p50(series: list[tuple[float, float]], lo: float, hi: float):
    # fm._summary_stats rounds to 3 decimals: AoI is kept in ms (pipeline unit) and converted to s after
    vals = [v for t, v in series if lo <= t < hi]
    return fm._summary_stats(vals).get("p50") if vals else None


def ms_to_s(x):
    return x / 1000.0 if x is not None else None


def recovery(run: dict, pre_ref: dict[str, dict]) -> dict:
    rd = run_path(run)
    w = load_json(rd / "flyblind_phase_windows.json")["windows"]
    pol = load_json(rd / "drain_policy.json")
    t0 = load_t0_mono_ns(rd)
    aoi, belief = samples(rd, t0)
    pre_lo, pre_hi = w["PRE"]["t_rel_lo_s"], w["PRE"]["t_rel_hi_s"]
    attack_end, drain_hi = w["DRAIN"]["t_rel_lo_s"], w["DRAIN"]["t_rel_hi_s"]
    base_delta, base_belief = ms_to_s(p50(aoi, pre_lo, pre_hi)), p50(belief, pre_lo, pre_hi)
    ref = pre_ref.get(run["run_id"]) or {}
    check = (abs(base_belief - float(ref["belief_p50_m"])) <= TOL and abs(base_delta * 1000 - float(ref["aoi_p50_ms"])) <= 1e-3) \
        if ref.get("belief_p50_m") and ref.get("aoi_p50_ms") else None
    ceiling = num(pol.get("drain_ceiling_applied_s")) or 0.0
    drain_len = drain_hi - attack_end
    horizon = min(ceiling, drain_len) if ceiling > 0 else drain_len
    win = []
    t = attack_end + dp.WINDOW_S
    while t <= attack_end + horizon + 1e-9:
        win.append({"t_rel_s": t, "delta_p50_s": ms_to_s(p50(aoi, t - dp.WINDOW_S, t)), "belief_p50_m": p50(belief, t - dp.WINDOW_S, t)})
        t += STEP_S
    res = dp.evaluate_recovery_from_summary(attack_end_s=attack_end, drain_ceiling_s=horizon, samples=win,
                                            baseline_delta_p50_s=base_delta, baseline_belief_p50_m=base_belief)

    def first(flag: str):
        for s in win:
            _, d = dp.in_baseline_band(delta_p50_s=s["delta_p50_s"], belief_p50_m=s["belief_p50_m"],
                                       baseline_delta_p50_s=base_delta, baseline_belief_p50_m=base_belief)
            if d[flag]:
                return s["t_rel_s"] - attack_end
        return None
    lim = dp.band_limits(base_delta, base_belief)
    # sample-level diagnostic (not the harness criterion): last single AoI sample above the band within the horizon
    late = [t - attack_end for t, v in aoi if attack_end <= t < attack_end + horizon and v / 1000.0 > lim["delta_p50_max_s"]]
    return {"run_id": run["run_id"], "condition": pooled(run["condition"]), "video_mode": pol.get("video_mode"),
            "drain_policy_enabled": pol.get("enabled"), "drain_ceiling_applied_s": ceiling,
            "drain_window_s": drain_len, "horizon_s": horizon, "n_windows": len(win),
            "baseline_delta_p50_s": base_delta, "baseline_belief_p50_m": base_belief,
            "band_delta_max_s": lim["delta_p50_max_s"], "band_belief_max_m": lim["belief_p50_max_m"],
            "baseline_matches_belief_by_phase": check,
            "first_window_delta_p50_s": win[0]["delta_p50_s"] if win else None,
            "first_window_belief_p50_m": win[0]["belief_p50_m"] if win else None,
            "recovered": res["recovered"], "recovery_time_s": res["recovery_time_s"],
            "censored": not res["recovered"], "aoi_in_band_after_s": first("ok_delta"),
            "belief_in_band_after_s": first("ok_belief"),
            "last_aoi_sample_out_of_band_s": max(late) if late else None}


def main() -> int:
    manifest = load_manifest()
    rows_a, slack_out = slack(manifest)
    write_csv(OUT / "slack_runs.csv", rows_a, list(rows_a[0].keys()))
    write_json(OUT / "slack.json", {"schema": "flyblind_slack_v1", **slack_out})

    with open(OUT / "belief_by_phase_runs.csv", encoding="utf-8") as f:
        pre_ref = {r["run_id"]: r for r in csv.DictReader(f) if r["phase"] == "PRE"}
    runs_b = [r for r in included_runs(manifest, ("campaign",)) if pooled(r["condition"]) in RECOVERY_CONDITIONS]
    rows_b = [recovery(r, pre_ref) for r in runs_b]
    write_csv(OUT / "drain_recovery_runs.csv", rows_b, list(rows_b[0].keys()))
    agg = []
    for c in RECOVERY_CONDITIONS:
        rs = [r for r in rows_b if r["condition"] == c]
        if rs:
            agg.append({"condition": c, "label": CONDITIONS[c][2], "n_runs": len(rs),
                        "recovered": {"true": sum(r["recovered"] for r in rs), "of": len(rs)},
                        **{m: stats(r[m] for r in rs) for m in ("recovery_time_s", "aoi_in_band_after_s",
                                                                 "belief_in_band_after_s", "last_aoi_sample_out_of_band_s",
                                                                 "drain_window_s",
                                                                 "baseline_delta_p50_s", "baseline_belief_p50_m")}})
    write_json(OUT / "drain_recovery.json", {
        "schema": "flyblind_drain_recovery_v1",
        "criterion": {"window_s": dp.WINDOW_S, "step_s": STEP_S, "delta_abs_s": dp.DELTA_ABS_S,
                      "delta_factor": dp.DELTA_FACTOR, "belief_abs_m": dp.BELIEF_ABS_M,
                      "belief_factor": dp.BELIEF_FACTOR, "baseline": "p50 of the PRE window of the same run",
                      "delta": "telemetry AoI (GCS arrival - SITL egress), s",
                      "recovery_time": "end of the first in-band window after attack_end (>= window_s)",
                      "last_aoi_sample_out_of_band": "sample-level diagnostic: last AoI sample above the band, "
                                                     "seconds after attack_end (not the harness criterion)"},
        "rows": agg})
    write_tex(slack_out, agg)
    bad = [r["run_id"] for r in rows_b if r["baseline_matches_belief_by_phase"] is False]
    print(f"[4.6] slack: {len(rows_a)} runs; recovery: {len(rows_b)} runs; PRE baseline equals belief_by_phase "
          f"in {sum(1 for r in rows_b if r['baseline_matches_belief_by_phase'])}/{len(rows_b)} runs")
    for b in bad:
        print(f"[4.6] MISMATCH PRE baseline {b}")
    return 1 if bad else 0


def write_tex(slack_out: dict, agg: list[dict]) -> None:
    def f(st, scale=1.0, d=1):
        return "--" if st["mean"] is None else f"{st['mean'] * scale:.{d}f}"
    lines = [r"% Auto-generated by 03_analysis/slack_recovery.py (R2). Do NOT edit by hand.",
             r"% (a) Victim UL slack: baseline runs (no attack), 106 PRB; MAC UL and goodput are whole-run values.",
             r"\begin{tabular}{rrrrrc}", r"\toprule",
             r"Video offered (Mb/s) & Goodput (Mb/s) & Loss (\%) & Victim MAC UL (Mb/s) & Return p50 (s) & C1 \\",
             r"\midrule"]
    for a in slack_out["rows"]:
        lines.append(" & ".join([f"{a['video_mbps']:g}", f(a["video_goodput_mbps"]), f(a["video_lost_pct"]),
                                 f(a["uav_mac_ul_mbps"]), f(a["return_owd_p50_s"], 1.0, 2),
                                 f"{a['return_C1']['true']}/{a['return_C1']['of']}"]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", "",
              r"% (b) DRAIN recovery (drain_policy criterion, baseline = PRE window of the same run).",
              r"\begin{tabular}{lcrr}", r"\toprule",
              r"Condition & Recovered & Recovery, 10 s window (s) & Last stale AoI sample (s) \\", r"\midrule"]
    for a in agg:
        if a["recovered"]["true"] == 0:  # censored: DRAIN shorter than the window / ceiling, no recovery observed
            rec, last = f"censored (DRAIN {f(a['drain_window_s'])} s)", "--"
        else:
            rec, last = f(a["recovery_time_s"]), f(a["last_aoi_sample_out_of_band_s"])
        lines.append(" & ".join([a["label"], f"{a['recovered']['true']}/{a['recovered']['of']}", rec, last]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    (OUT / "slack_recovery.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

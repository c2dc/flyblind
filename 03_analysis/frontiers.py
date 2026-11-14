#!/usr/bin/env python3
##################################################################################################################################
# Module: frontiers.py (Effect frontiers (video load, attacker dose, cell bandwidth), ATTACK window)                             #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Each frontier is a list of conditions; the x value of every run (video Mb/s, number of rogue UEs N, gNB PRB) is   #
#   read from that run's run_meta.json / topology stamp, never typed here. Per run: return / command leg   OWD p50, p99 of real  #
#   C2 traffic (run_kpi_report.json, kpi_c2_real, same source as the per-leg KPI table) and C1                                   #
#   effect_present on the return leg belief / AoI                                                                                #
#   ATTACK-window p50 from belief_by_phase_runs.csv (run belief_by_phase.py first) telemetry delivery                            #
#   common.telemetry_delivery: share of the telemetry generated in ATTACK that reaches the GCS mechanism (whole run)  slice-A    #
#   share of UL NPRB samples, victim UAV MAC UL Mb/s, rogue MAC UL Mb/s (prb.json), video offered / goodput / loss               #
#   (goodput_mac_report.goodput_from_companion) The mechanism columns are whole-run values (prb.json and iperf end.sum are not   #
#   phase-sliced).                                                                                                               #
#                                                                                                                                #
# Outputs (out/): frontiers_runs.csv, frontiers.csv/.json, frontiers.tex                                                         #
##################################################################################################################################
"""Effect frontiers (video load, attacker dose, cell bandwidth), ATTACK window.

Each frontier is a list of conditions; the x value of every run (video Mb/s, number of rogue UEs N,
gNB PRB) is read from that run's run_meta.json / topology stamp, never typed here. Per run:
  return / command leg   OWD p50, p99 of real C2 traffic (run_kpi_report.json, kpi_c2_real, same
                         source as the per-leg KPI table) and C1 effect_present on the return leg
  belief / AoI           ATTACK-window p50 from belief_by_phase_runs.csv (run belief_by_phase.py first)
  telemetry delivery     common.telemetry_delivery: share of the telemetry generated in ATTACK that reaches the GCS
  mechanism (whole run)  slice-A share of UL NPRB samples, victim UAV MAC UL Mb/s, rogue MAC UL Mb/s
                         (prb.json), video offered / goodput / loss (goodput_mac_report.goodput_from_companion)
The mechanism columns are whole-run values (prb.json and iperf end.sum are not phase-sliced).

Outputs (out/): frontiers_runs.csv, frontiers.csv/.json, frontiers.tex
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONDITIONS, OUT, get, included_runs, load_json, load_manifest, num, pooled, run_path,  # noqa: E402
                    stats, telemetry_delivery, write_csv, write_json)
from goodput_mac_report import goodput_from_companion  # noqa: E402

PHASE = "ATTACK"

# frontier id -> (title, x variable, attack conditions, reference baselines without attack)
#
# The victim is starved only while its own demand exceeds the proportional-fair share C/(N+1). The video
# frontier therefore crosses that boundary (0 -> 24 Mb/s) and the dose frontier moves the share itself
# (N = 0 -> 4). Together they are the two axes of AoI = (Q/C)(N+1), with Q = RLC_TX_MAXSIZE.
FRONTIERS = {
    # 0 and 4 Mb/s are below the share (no effect), 6 Mb/s sits on it, 8/12/16/24 are above it: the
    # onset and the plateau in one sweep.
    "video_106": ("Video load at 106 PRB, N=4", "video_mbps",
                  ["c_atk_n4_novideo", "b_atk_n4_vid4", "b_atk_n4_vid6", "b_atk_n4_vid8", "a_atk_n4",
                   "d_atk_n4_vid16", "d_atk_n4_vid24"],
                  ["a_baseline", "cap_base_vid24", "cap_base_vid32", "cap_base_vid40"]),
    "video_51": ("Video load at 51 PRB, N=4", "video_mbps",
                 ["e_atk51_n4"],
                 ["e_base51", "cap51_base_vid16", "cap51_base_vid24"]),
    "dose": ("Attacker dose at 106 PRB, video 12 Mb/s", "n_rogues",
             ["a_baseline", "a_atk_n1", "a_atk_n2", "a_atk_n3", "a_atk_n4"],
             []),
    "band": ("Cell bandwidth, N=4, video 8 Mb/s", "gnb_prb",
             ["b_atk_n4_vid8", "e_atk51_n4"],
             ["e_base51"]),
}

METRICS = ("return_owd_p50_s", "return_owd_p99_s", "command_owd_p50_s", "command_owd_p99_s",
           "belief_p50_m", "aoi_p50_ms", "slice_a_share_ul_nprb_pct", "uav_mac_ul_mbps", "rogue_mac_ul_mbps",
           "video_offered_mbps", "video_goodput_mbps", "video_lost_pct", "telemetry_delivery")


def load_belief_rows() -> dict[str, dict]:
    path = OUT / "belief_by_phase_runs.csv"
    if not path.is_file():
        raise SystemExit(f"missing {path}; run belief_by_phase.py first")
    with open(path, encoding="utf-8") as f:
        return {r["run_id"]: r for r in csv.DictReader(f) if r["phase"] == PHASE}


def run_row(run: dict, belief: dict[str, dict]) -> dict:
    rd = run_path(run)
    meta = load_json(rd / "run_meta.json")
    rep = load_json(rd / "run_kpi_report.json")
    prb = load_json(rd / "prb.json") if (rd / "prb.json").is_file() else {}
    stamp = meta.get("topology_stamp") or {}
    r = {"run_id": run["run_id"], "condition": pooled(run["condition"]), "category": run["category"],
         "load_over_limit": get(run, "flags.load_over_limit"),
         "video_mbps": num(meta.get("companion_video_mbps")) or 0.0,
         "n_rogues": int(meta.get("multi_rogue_count") or 0) if meta.get("condition") != "baseline" else 0,
         "gnb_prb": int(stamp["GNB_PRB"]) if stamp.get("GNB_PRB") not in (None, "") else None}
    for leg in ("return", "command"):
        owd = get(rep, f"kpi_c2_real.{leg}.phases.{PHASE}.owd_s.primary") or {}
        r[f"{leg}_owd_p50_s"] = num(owd.get("p50"))
        r[f"{leg}_owd_p99_s"] = num(owd.get("p99"))
    r["return_C1_effect_present"] = get(rep, f"kpi_c2_real.return.phases.{PHASE}.criterion_C1.effect_present")
    b = belief.get(run["run_id"]) or {}
    r["belief_p50_m"] = float(b["belief_p50_m"]) if b.get("belief_p50_m") else None
    r["aoi_p50_ms"] = float(b["aoi_p50_ms"]) if b.get("aoi_p50_ms") else None
    roles = prb.get("per_role") or {}
    r["slice_a_share_ul_nprb_pct"] = num(get(prb, "per_slice.A.share_ul_nprb_pct"))
    r["uav_mac_ul_mbps"] = num(get(roles, "uav.ul_mbps"))
    r["telemetry_delivery"] = telemetry_delivery(rd, PHASE)   # ATTACK-window telemetry delivered
    rogue = [num(v.get("ul_mbps")) for k, v in roles.items() if k.startswith("rogue")]
    r["rogue_mac_ul_mbps"] = sum(x for x in rogue if x is not None) if rogue else 0.0
    if (rd / "companion_video.json").is_file():
        gp = goodput_from_companion(load_json(rd / "companion_video.json"))
        r["video_offered_mbps"], r["video_goodput_mbps"] = num(gp.get("offered_mbps")), num(gp.get("goodput_mbps"))
        r["video_lost_pct"] = num(gp.get("lost_percent"))
    else:  # video 0: no companion stream
        r["video_offered_mbps"] = r["video_goodput_mbps"] = r["video_lost_pct"] = None
    return r


def aggregate(rows: list[dict]) -> dict:
    a = {"n_runs": len(rows), "runs": [r["run_id"] for r in rows],
         "runs_load_over_limit": [r["run_id"] for r in rows if r["load_over_limit"]]}
    for x in ("video_mbps", "n_rogues", "gnb_prb"):
        vals = sorted({r[x] for r in rows})
        a[x] = vals[0] if len(vals) == 1 else vals  # a list here means mixed x within one condition
    for m in METRICS:
        a[m] = stats(r[m] for r in rows)
    c1 = [r["return_C1_effect_present"] for r in rows if isinstance(r["return_C1_effect_present"], bool)]
    a["return_C1_effect_present"] = {"true": sum(c1), "of": len(c1)}
    return a


def main() -> int:
    manifest = load_manifest()
    belief = load_belief_rows()
    wanted = {c for f in FRONTIERS.values() for c in f[2] + f[3]}
    # capacity_probe is included on purpose: the video frontier's reference baselines ARE the capacity
    # ladder (no adversary, victim video raised until it self-congests). Those runs are scored VALID=NO
    # by construction, so filtering on "campaign" alone would silently drop the self-congestion point.
    rows = [run_row(run, belief) for run in included_runs(manifest, ("campaign", "capacity_probe"))
            if pooled(run["condition"]) in wanted]
    write_csv(OUT / "frontiers_runs.csv", rows, list(rows[0].keys()))
    by_cond: dict[str, list[dict]] = {}
    for r in rows:
        by_cond.setdefault(r["condition"], []).append(r)
    agg = {cid: {"condition": cid, "item": CONDITIONS[cid][0], "label": CONDITIONS[cid][2], **aggregate(rs)}
           for cid, rs in by_cond.items()}
    out = {"schema": "flyblind_frontiers_v1", "phase": PHASE,
           "note": "OWD, C1, belief and AoI are ATTACK-window values; mechanism columns are whole-run "
                   "(prb.json, iperf end.sum). x values come from run_meta.json / topology stamp.",
           "frontiers": {}}
    flat = []
    for fid, (title, xvar, attack, refs) in FRONTIERS.items():
        missing = [c for c in attack + refs if c not in agg]
        if missing:
            print(f"[4.3] WARNING: frontier {fid}: no runs for {missing}")
        pts = [dict(agg[c], role="attack" if c not in refs else "reference") for c in attack if c in agg]
        pts += [dict(agg[c], role="reference") for c in refs if c in agg]
        out["frontiers"][fid] = {"title": title, "x": xvar, "points": pts}
        for p in pts:
            f = {"frontier": fid, "x_var": xvar, "x": p[xvar], "role": p["role"],
                 **{k: p[k] for k in ("condition", "item", "label", "n_runs", "video_mbps", "n_rogues", "gnb_prb")},
                 "return_C1": f"{p['return_C1_effect_present']['true']}/{p['return_C1_effect_present']['of']}"}
            for m in METRICS:
                f[f"{m}_mean"], f[f"{m}_sd"] = p[m]["mean"], p[m]["sd"]
            flat.append(f)
    write_json(OUT / "frontiers.json", out)
    write_csv(OUT / "frontiers.csv", flat, list(flat[0].keys()))
    write_tex(out)
    print(f"[4.3] {len(rows)} runs, {len(agg)} conditions, {len(FRONTIERS)} frontiers -> out/frontiers*")
    return 0


def write_tex(out: dict) -> None:
    def f(st, scale=1.0, d=2):
        return "--" if st["mean"] is None else f"{st['mean'] * scale:.{d}f}"
    lines = [r"% Auto-generated by 03_analysis/frontiers.py (R2). Do NOT edit by hand.",
             r"% ATTACK window, means over replicates; C1 = runs with effect on the return leg (k/n).",
             r"% Video goodput and victim MAC UL are whole-run values.",
             r"\begin{tabular}{lrrrrrrrrr}", r"\toprule",
             r"Condition & PRB & N & Video & Return p50 (s) & Return p99 (s) & Command p50 (ms) & "
             r"Belief p50 (m) & Video goodput (Mb/s) & C1 \\", r"\midrule"]
    for i, (fid, fr) in enumerate(out["frontiers"].items()):
        if i:
            lines.append(r"\midrule")
        lines.append(r"\multicolumn{10}{l}{\textit{" + fr["title"] + r"}} \\")
        for p in fr["points"]:
            ref = r" \textit{(ref.)}" if p["role"] == "reference" else ""
            c1 = p["return_C1_effect_present"]
            lines.append(" & ".join([
                p["item"] + ref, str(p["gnb_prb"]), str(p["n_rogues"]), f"{p['video_mbps']:g}",
                f(p["return_owd_p50_s"]), f(p["return_owd_p99_s"]), f(p["command_owd_p50_s"], 1000.0, 1),
                f(p["belief_p50_m"], 1.0, 1), f(p["video_goodput_mbps"], 1.0, 1),
                f"{c1['true']}/{c1['of']}"]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    (OUT / "frontiers.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

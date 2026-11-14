#!/usr/bin/env python3
##################################################################################################################################
# Module: mitigation_table.py (Defence table: what each mitigation does to state freshness, and what it costs on the command     #
#     leg)                                                                                                                       #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Four rows, one testbed configuration each, all other parameters identical (106 PRB, N=4 co-tenants, 12 Mb/s       #
#   video, route): baseline, the undefended attack, uplink slice enforcement (S4) and host AQM (S5). The point of putting them   #
#   in one table is that the two defences act in different places, so the comparison says WHERE the queue that ages the state    #
#   actually is:                                                                                                                 #
#                                                                                                                                #
# slicing  the victim gets its own slice and the slice-aware uplink scheduler serves it first, so the backlog never forms --     #
#   freshness returns to baseline with no PRB reservation (floor 0) AQM      fq_codel is applied on the victim's uplink on the   #
#   host; it can only reorder what is already in the host queue, and the queue that matters is in the RAN scheduler              #
#                                                                                                                                #
# Columns come from the same sources as the other tables (no number is typed here): AoI / belief      ATTACK-window p50 from     #
#   belief_by_phase_runs.csv (run belief_by_phase.py first) per-leg OWD       run_kpi_report.json,                               #
#   kpi_c2_real.<leg>.phases.ATTACK (same source as the per-leg KPI table) C1 effect_present                                     #
#   run_kpi_report.json on the return leg: the                                                                                   #
#   pre-registered "effect is present" test delivery          common.telemetry_delivery: share of the telemetry generated in the #
#   ATTACK window that reaches the GCS (the file-level uav_to_gcs_delivery_ratio mixes capture spans) failsafe / alerts          #
#   run_meta.json outcomes and statustext_gcs.csv, restricted to the ATTACK window                                               #
#                                                                                                                                #
# NOTE on statustext timing: the t_rel_s column of statustext_gcs.csv counts from the mission start, not from T0, while          #
#   flyblind_phase_windows.json counts from T0 (~90 s earlier). Phases are therefore assigned from t_mono_ns against T0_mono_ns, #
#   exactly as belief_by_phase.py does, never from that t_rel_s column.                                                          #
#                                                                                                                                #
# Outputs (out/): mitigation_runs.csv    one row per run mitigation.csv/.json   aggregated over replicates (mean, sd, min, max,  #
#   n) mitigation.tex         LaTeX fragment for the defence section                                                             #
##################################################################################################################################
"""Defence table: what each mitigation does to state freshness, and what it costs on the command leg.

Four rows, one testbed configuration each, all other parameters identical (106 PRB, N=4 co-tenants,
12 Mb/s video, route): baseline, the undefended attack, uplink slice enforcement (S4) and host AQM (S5).
The point of putting them in one table is that the two defences act in different places, so the
comparison says WHERE the queue that ages the state actually is:

  slicing  the victim gets its own slice and the slice-aware uplink scheduler serves it first, so the
           backlog never forms -- freshness returns to baseline with no PRB reservation (floor 0)
  AQM      fq_codel is applied on the victim's uplink on the host; it can only reorder what is already
           in the host queue, and the queue that matters is in the RAN scheduler

Columns come from the same sources as the other tables (no number is typed here):
  AoI / belief      ATTACK-window p50 from belief_by_phase_runs.csv (run belief_by_phase.py first)
  per-leg OWD       run_kpi_report.json, kpi_c2_real.<leg>.phases.ATTACK (same source as the per-leg KPI table)
  C1 effect_present run_kpi_report.json on the return leg: the pre-registered "effect is present" test
  delivery          common.telemetry_delivery: share of the telemetry generated in the ATTACK window
                    that reaches the GCS (the file-level uav_to_gcs_delivery_ratio mixes capture spans)
  failsafe / alerts run_meta.json outcomes and statustext_gcs.csv, restricted to the ATTACK window

NOTE on statustext timing: the t_rel_s column of statustext_gcs.csv counts from the mission start, not
from T0, while flyblind_phase_windows.json counts from T0 (~90 s earlier). Phases are therefore assigned
from t_mono_ns against T0_mono_ns, exactly as belief_by_phase.py does, never from that t_rel_s column.

Outputs (out/):
  mitigation_runs.csv    one row per run
  mitigation.csv/.json   aggregated over replicates (mean, sd, min, max, n)
  mitigation.tex         LaTeX fragment for the defence section
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONDITIONS, OUT, fmt_mean_sd, get, included_runs, load_json, load_manifest,  # noqa: E402
                    pooled, run_path, stats, telemetry_delivery, tex_escape, write_csv, write_json)
from kpi_leg import assign_phase_half_open  # noqa: E402
from phase_windows import load_t0_mono_ns  # noqa: E402

# Row order is the argument: no defence, then the defence that works, then the one that does not.
ROWS = ["a_baseline", "a_atk_n4", "f1_slicing_n4", "f2_aqm_n4"]
PHASE = "ATTACK"
# MAVLink SEVERITY: 0 EMERGENCY .. 4 WARNING .. 6 INFO. "An alert the operator would see" is <= 4.
ALERT_SEVERITY_MAX = 4
METRICS = ("aoi_p50_ms", "belief_p50_m", "return_owd_p50_s", "return_owd_p99_s",
           "command_owd_p50_s", "command_owd_p99_s", "telemetry_delivery_ratio", "n_alerts_attack")


def attack_window_alerts(rd: Path) -> int | None:
    """STATUSTEXT messages of severity <= ALERT_SEVERITY_MAX whose arrival falls inside the ATTACK window."""
    p = rd / "statustext_gcs.csv"
    windows = (load_json(rd / "flyblind_phase_windows.json").get("windows")
               if (rd / "flyblind_phase_windows.json").is_file() else None) or {}
    t0 = load_t0_mono_ns(rd)
    if not p.is_file() or t0 is None or not windows:
        return None
    n = 0
    with open(p, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                sev = int(row["severity"])
                t_ns = int(row["t_mono_ns"])
            except (KeyError, TypeError, ValueError):
                continue
            if sev <= ALERT_SEVERITY_MAX and assign_phase_half_open((t_ns - t0) / 1e9, windows) == PHASE:
                n += 1
    return n


def belief_index() -> dict[str, dict]:
    p = OUT / "belief_by_phase_runs.csv"
    if not p.is_file():
        raise SystemExit(f"missing {p}; run belief_by_phase.py first")
    with open(p, encoding="utf-8") as f:
        return {r["run_id"]: r for r in csv.DictReader(f) if r["phase"] == PHASE}


def row_for(run: dict, bel: dict[str, dict]) -> dict:
    rd = run_path(run)
    meta = load_json(rd / "run_meta.json")
    rep = load_json(rd / "run_kpi_report.json")
    b = bel.get(run["run_id"], {})
    out = {"run_id": run["run_id"], "condition": pooled(run["condition"]), "category": run["category"],
           "aoi_p50_ms": _f(b.get("aoi_p50_ms")), "belief_p50_m": _f(b.get("belief_p50_m")),
           "telemetry_delivery_ratio": telemetry_delivery(rd, PHASE),
           "failsafe": (meta.get("outcomes") or {}).get("failsafe"),
           "n_mode_transitions": len((meta.get("outcomes") or {}).get("mode_transitions") or []),
           "n_alerts_attack": attack_window_alerts(rd),
           "return_C1_effect_present": get(rep, f"kpi_c2_real.return.phases.{PHASE}.criterion_C1.effect_present")}
    for leg in ("command", "return"):
        owd = get(rep, f"kpi_c2_real.{leg}.phases.{PHASE}.owd_s.primary") or {}
        out[f"{leg}_owd_p50_s"] = _f(owd.get("p50"))
        out[f"{leg}_owd_p99_s"] = _f(owd.get("p99"))
    return out


def _f(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def verdict(rs: list[dict]) -> str:
    """Pre-registered C1 on the return leg decides; the AoI is reported next to it, never instead of it."""
    vals = [r["return_C1_effect_present"] for r in rs if r["return_C1_effect_present"] is not None]
    if not vals:
        return "no C1 verdict"
    if not any(vals):
        return "state stays fresh (C1 absent in every replicate)"
    if all(vals):
        return "state ages (C1 present in every replicate)"
    return f"mixed (C1 present in {sum(1 for v in vals if v)}/{len(vals)})"


def main() -> int:
    manifest = load_manifest()
    bel = belief_index()
    runs = [r for r in included_runs(manifest) if pooled(r["condition"]) in ROWS]
    if not runs:
        print("[defence] no runs for the defence table; nothing written")
        return 0
    rows = [row_for(r, bel) for r in runs]
    write_csv(OUT / "mitigation_runs.csv", rows, list(rows[0].keys()))

    by_cond: dict[str, list[dict]] = {}
    for r in rows:
        by_cond.setdefault(r["condition"], []).append(r)
    agg = []
    for cid in ROWS:
        rs = by_cond.get(cid)
        if not rs:
            continue
        agg.append({"condition": cid, "series": CONDITIONS[cid][0], "testbed": CONDITIONS[cid][1],
                    "label": CONDITIONS[cid][2], "n_runs": len(rs),
                    **{m: stats(x[m] for x in rs) for m in METRICS},
                    "failsafe_triggered": {"true": sum(1 for x in rs if x["failsafe"] is True), "of": len(rs)},
                    "return_C1": {"true": sum(1 for x in rs if x["return_C1_effect_present"] is True),
                                  "of": len(rs)},
                    "verdict": verdict(rs)})
    write_json(OUT / "mitigation.json", {"schema": "flyblind_mitigation_v1", "phase": PHASE,
                                         "alert_severity_max": ALERT_SEVERITY_MAX, "rows": agg})
    flat = [{"condition": a["condition"], "label": a["label"], "testbed": a["testbed"], "n_runs": a["n_runs"],
             **{f"{m}_mean": a[m]["mean"] for m in METRICS},
             **{f"{m}_sd": a[m]["sd"] for m in ("aoi_p50_ms", "belief_p50_m")},
             "failsafe": f"{a['failsafe_triggered']['true']}/{a['failsafe_triggered']['of']}",
             "return_C1": f"{a['return_C1']['true']}/{a['return_C1']['of']}",
             "verdict": a["verdict"]} for a in agg]
    write_csv(OUT / "mitigation.csv", flat, list(flat[0].keys()))

    lines = [r"\begin{tabular}{lrrrrrr}", r"\toprule",
             r"Configuration & $n$ & AoI p50 (s) & Belief p50 (m) & Cmd OWD p50 (ms) & Deliv. & Alerts \\",
             r"\midrule"]
    for a in agg:
        lines.append(" & ".join([
            tex_escape(a["label"]), str(a["n_runs"]),
            fmt_mean_sd(a["aoi_p50_ms"], scale=1e-3, digits=2),
            fmt_mean_sd(a["belief_p50_m"], digits=1),
            fmt_mean_sd(a["command_owd_p50_s"], scale=1e3, digits=1),
            fmt_mean_sd(a["telemetry_delivery_ratio"], digits=3),
            f"{a['n_alerts_attack']['mean']:.0f}" if a["n_alerts_attack"]["mean"] is not None else "--",
        ]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    (OUT / "mitigation.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[defence] {len(rows)} runs, {len(agg)} configurations -> out/mitigation*")
    for a in agg:
        aoi = a["aoi_p50_ms"]["mean"]
        print(f"  {a['label'][:52]:52s} n={a['n_runs']} "
              f"AoI {('%.2f s' % (aoi / 1000)) if aoi is not None else '--':>9} | {a['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

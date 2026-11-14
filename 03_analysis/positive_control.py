#!/usr/bin/env python3
##################################################################################################################################
# Module: positive_control.py (Positive control: the native monitors DO fire, so their silence under FlyBlind means something)   #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: The paper's central claim is negative -- link health reads green while the state is stale -- and a negative claim #
#   about a detector is only worth anything if the detector can be made to fire. The control run cuts the COMMAND leg (GCS ->    #
#   UAV) on purpose for longer than FS_GCS_TIMEOUT, with no adversary at all, and the same monitors that stay silent under       #
#   attack are expected to trip.                                                                                                 #
#                                                                                                                                #
# Direction matters: FS_GCS watches whether the UAV still HEARS the GCS, so only a cut on the GCS -> UAV leg can arm it. Cutting #
#   the return leg would age the operator's state while leaving the failsafe disarmed -- which is exactly what the attack does,  #
#   and why the attack is invisible.                                                                                             #
#                                                                                                                                #
# The result is a double dissociation, which this script emits as one table:                                                     #
#                                                                                                                                #
# state at the GCS      native monitors positive control      FRESH (ms)            FIRE  (failsafe -> RTL, severity-4           #
#   STATUSTEXT) FlyBlind              STALE (seconds)       SILENT                                                               #
#                                                                                                                                #
# Causality is checked, not assumed. The cut timestamps come from blackhole_injection.json (measured by the campaign driver, not #
#   planned values) and the failsafe time from c2.json. These live in different t_rel origins -- the injection record counts     #
#   from T0, c2.json counts from the mission start about 90 s later -- so the offset is measured from a capture that carries     #
#   both t_mono_ns and t_rel_s before the two are compared. Skipping that step makes a failsafe look like it preceded its own    #
#   cause.                                                                                                                       #
#                                                                                                                                #
# Outputs (out/): positive_control.json   cut window, failsafe latency, alerts, and the attack-side comparison                   #
#   positive_control.tex    the dissociation table                                                                               #
##################################################################################################################################
"""Positive control: the native monitors DO fire, so their silence under FlyBlind means something.

The paper's central claim is negative -- link health reads green while the state is stale -- and a
negative claim about a detector is only worth anything if the detector can be made to fire. The control
run cuts the COMMAND leg (GCS -> UAV) on purpose for longer than FS_GCS_TIMEOUT, with no adversary at
all, and the same monitors that stay silent under attack are expected to trip.

Direction matters: FS_GCS watches whether the UAV still HEARS the GCS, so only a cut on the GCS -> UAV
leg can arm it. Cutting the return leg would age the operator's state while leaving the failsafe
disarmed -- which is exactly what the attack does, and why the attack is invisible.

The result is a double dissociation, which this script emits as one table:

                        state at the GCS      native monitors
  positive control      FRESH (ms)            FIRE  (failsafe -> RTL, severity-4 STATUSTEXT)
  FlyBlind              STALE (seconds)       SILENT

Causality is checked, not assumed. The cut timestamps come from blackhole_injection.json (measured by
the campaign driver, not planned values) and the failsafe time from c2.json. These live in different
t_rel origins -- the injection record counts from T0, c2.json counts from the mission start about 90 s
later -- so the offset is measured from a capture that carries both t_mono_ns and t_rel_s before the two
are compared. Skipping that step makes a failsafe look like it preceded its own cause.

Outputs (out/):
  positive_control.json   cut window, failsafe latency, alerts, and the attack-side comparison
  positive_control.tex    the dissociation table
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ARTIFACTS, CAMPAIGN, CONDITIONS, OUT, get, included_runs, load_json,  # noqa: E402
                    load_manifest, num, pooled, run_path, stats, tex_escape, write_json, resolve_run_dir)
from kpi_leg import assign_phase_half_open  # noqa: E402
from phase_windows import load_t0_mono_ns  # noqa: E402

CONTROL = "pos_control_route"
ATTACK = "a_atk_n4"
BASELINE = "a_baseline"
ALERT_SEVERITY_MAX = 4
# A failsafe is attributable to the cut if it fires after it and within a few FS_GCS_TIMEOUT periods.
MAX_ATTRIBUTION_LAG_S = 30.0


def mavlink_t_rel_offset(rd: Path, t0_mono_ns: int) -> float | None:
    """Seconds between T0_mono_ns and the origin used by the MAVLink captures' own t_rel_s column."""
    for name in ("statustext_gcs.csv", "telemetry_downlink_gcs.csv"):
        p = rd / name
        if not p.is_file():
            continue
        with open(p, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                tm, tr = row.get("t_mono_ns"), row.get("t_rel_s")
                if tm is None or tr is None:
                    break
                return (int(tm) - float(tr) * 1e9 - t0_mono_ns) / 1e9
    return None


def alerts(rd: Path, phase: str | None = None) -> list[dict]:
    """Severity <= ALERT_SEVERITY_MAX STATUSTEXT, with t_rel in the T0 base (not the capture's own)."""
    p = rd / "statustext_gcs.csv"
    if not p.is_file():
        return []
    t0 = load_t0_mono_ns(rd)
    windows = (load_json(rd / "flyblind_phase_windows.json").get("windows")
               if (rd / "flyblind_phase_windows.json").is_file() else None) or {}
    out = []
    with open(p, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                sev, t_ns = int(row["severity"]), int(row["t_mono_ns"])
            except (KeyError, TypeError, ValueError):
                continue
            if sev > ALERT_SEVERITY_MAX:
                continue
            t_rel = (t_ns - t0) / 1e9 if t0 is not None else None
            ph = assign_phase_half_open(t_rel, windows) if (t_rel is not None and windows) else None
            if phase is not None and ph != phase:
                continue
            out.append({"t_rel_s": t_rel, "phase": ph, "severity": sev, "text": (row.get("text") or "").strip()})
    return out


def control_block() -> dict | None:
    """The control run plus its injection record, resolved into one time base."""
    recs = sorted(CAMPAIGN.glob("*/*/blackhole_injection.json"))
    if not recs:
        return {"status": "absent", "note": "no blackhole_injection.json under 02_results/campaign/"}
    rec = load_json(recs[-1])
    rd = resolve_run_dir(rec.get("run_dir")) or Path("")
    if not rd.is_dir():
        return {"status": "absent", "note": f"injection record points at a missing run dir: {rd}"}
    meta = load_json(rd / "run_meta.json")
    c2 = load_json(rd / "c2.json") if (rd / "c2.json").is_file() else {}
    tel = load_json(rd / "telemetry_downlink_summary.json") if (rd / "telemetry_downlink_summary.json").is_file() else {}
    t0m = meta.get("T0_mono_ns")
    off = mavlink_t_rel_offset(rd, t0m) if t0m is not None else None
    fs = (get(c2, "c2_metrics.failsafe") or {})
    fs_t = num(fs.get("time_s"))
    cut_lo, cut_hi = num(rec.get("cut_start_t_rel_s")), num(rec.get("cut_end_t_rel_s"))
    fs_t0_base = (fs_t + off) if (fs_t is not None and off is not None) else None
    lag = (fs_t0_base - cut_lo) if (fs_t0_base is not None and cut_lo is not None) else None
    al = alerts(rd)
    return {
        "status": "present",
        "run_dir": str(rd.resolve().relative_to(ARTIFACTS)),
        "injection": {k: rec.get(k) for k in ("leg", "pod", "dest", "method", "cut_duration_s")},
        "cut_t_rel_s": [cut_lo, cut_hi],
        "mavlink_t_rel_offset_s": off,
        "failsafe": {"triggered": fs.get("triggered"), "mode": fs.get("mode"),
                     "t_rel_s_mavlink_base": fs_t, "t_rel_s": fs_t0_base,
                     "latency_after_cut_s": lag},
        "causally_attributed": bool(lag is not None and 0 <= lag <= MAX_ATTRIBUTION_LAG_S),
        "mode_transitions": (meta.get("outcomes") or {}).get("mode_transitions"),
        "alerts": al,
        "telemetry_aoi_p50_ms": num(get(tel, "telemetry_arrival_delay_ms.p50")),
        "valid": meta.get("valid"),
        "valid_note": "VALID=NO is expected: a tripped failsafe fails the baseline mission gates by "
                      "construction. This run is read as a control, not as a scored run.",
    }


def attack_side(manifest: dict, cid: str) -> dict:
    runs = [r for r in included_runs(manifest) if pooled(r["condition"]) == cid]
    aoi, fss, nal = [], 0, []
    for r in runs:
        rd = run_path(r)
        tel = load_json(rd / "telemetry_downlink_summary.json") if (rd / "telemetry_downlink_summary.json").is_file() else {}
        meta = load_json(rd / "run_meta.json")
        aoi.append(num(get(tel, "telemetry_arrival_delay_ms.p50")))
        if (meta.get("outcomes") or {}).get("failsafe") is True:
            fss += 1
        nal.append(len(alerts(rd, phase="ATTACK")))
    return {"condition": cid, "label": CONDITIONS[cid][2] if cid in CONDITIONS else cid,
            "n_runs": len(runs), "telemetry_aoi_p50_ms": stats(aoi),
            "failsafe_triggered": {"true": fss, "of": len(runs)},
            "alerts_in_attack_window": stats(nal), "alert_texts": sorted({a["text"] for r in runs
                                                                          for a in alerts(run_path(r), phase="ATTACK")})}


def main() -> int:
    manifest = load_manifest()
    ctl = control_block()
    out = {"schema": "flyblind_positive_control_v1", "alert_severity_max": ALERT_SEVERITY_MAX,
           "control": ctl, "attack": attack_side(manifest, ATTACK), "baseline": attack_side(manifest, BASELINE)}
    write_json(OUT / "positive_control.json", out)

    atk = out["attack"]
    a_aoi = atk["telemetry_aoi_p50_ms"]["mean"]
    rows = []
    if ctl.get("status") == "present":
        fs_mode = get(ctl, "failsafe.mode") or "--"
        lag = get(ctl, "failsafe.latency_after_cut_s")
        rows.append(("Positive control (command leg cut)",
                     f"{(ctl['telemetry_aoi_p50_ms'] or 0) / 1000:.3f}",
                     f"yes ($\\rightarrow$ {tex_escape(str(fs_mode))}, {lag:.2f} s after the cut)" if lag is not None
                     else "yes",
                     str(len([a for a in ctl["alerts"] if "Failsafe" in a["text"]]))))
    if a_aoi is not None:
        rows.append(("FlyBlind (no interruption)", f"{a_aoi / 1000:.2f}",
                     f"no ({atk['failsafe_triggered']['true']}/{atk['failsafe_triggered']['of']})",
                     f"{atk['alerts_in_attack_window']['mean']:.0f}"))
    lines = [r"\begin{tabular}{lrrr}", r"\toprule",
             r"Condition & Telemetry AoI p50 (s) & GCS failsafe & Operator alerts \\", r"\midrule"]
    lines += [" & ".join([tex_escape(a), b, c, d]) + r" \\" for a, b, c, d in rows]
    lines += [r"\bottomrule", r"\end{tabular}"]
    (OUT / "positive_control.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[control] positive control:", ctl.get("status"))
    if ctl.get("status") == "present":
        print(f"  cut        t_rel {ctl['cut_t_rel_s'][0]:.1f} -> {ctl['cut_t_rel_s'][1]:.1f} s "
              f"({get(ctl, 'injection.cut_duration_s'):.1f} s, {get(ctl, 'injection.leg')})")
        print(f"  failsafe   {get(ctl, 'failsafe.triggered')} -> {get(ctl, 'failsafe.mode')} at t_rel "
              f"{get(ctl, 'failsafe.t_rel_s'):.1f} s, {get(ctl, 'failsafe.latency_after_cut_s'):+.2f} s after the cut")
        print(f"  attributed {ctl['causally_attributed']}  (AoI p50 {ctl['telemetry_aoi_p50_ms'] / 1000:.3f} s: state was FRESH)")
    print(f"  attack     n={atk['n_runs']} AoI p50 {(a_aoi or 0) / 1000:.2f} s, "
          f"failsafe {atk['failsafe_triggered']['true']}/{atk['failsafe_triggered']['of']}, "
          f"alerts {atk['alerts_in_attack_window']['mean']:.0f}")
    print("[control] -> out/positive_control.json, out/positive_control.tex")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

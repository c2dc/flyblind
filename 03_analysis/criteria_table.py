#!/usr/bin/env python3
##################################################################################################################################
# Module: criteria_table.py (criteria table out/table_4_2_*: C1 effect-present and C4 per-leg conformance,                       #
#     per condition, leg and phase)                                                                                              #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: C1 and the C4 checks come from run_kpi_report.json (kpi_c2_real.<leg>.phases.<phase>), i.e. the criteria fixed    #
#   before the campaign; this script only aggregates them over replicates and keeps the measured value next to                   #
#   every pass/fail, so baseline and attack can be compared: criteria that already fail at baseline are a testbed floor,         #
#   criteria that flip under attack are the attack effect. K7 (FS_GCS on the command leg) and K8 (QGC-style gaps on the return   #
#   leg) come from run_kpi_report.json (link_vivacity).                                                                          #
#                                                                                                                                #
# Outputs (out/): table_4_2_criteria_runs.csv, table_4_2_criteria.csv/.json, table_4_2_c4.tex                                    #
##################################################################################################################################
"""Criteria table (out/table_4_2_*): C1 (effect present) and C4 (per-leg conformance) per condition, leg and phase.

C1 and the C4 checks come from run_kpi_report.json (kpi_c2_real.<leg>.phases.<phase>), i.e. the
criteria fixed before the campaign; this script only aggregates them over replicates
and keeps the measured value next to every pass/fail, so baseline and attack can be compared:
criteria that already fail at baseline are a testbed floor, criteria that flip under attack are the
attack effect. K7 (FS_GCS on the command leg) and K8 (QGC-style gaps on the return leg) come from
run_kpi_report.json (link_vivacity).

Outputs (out/): table_4_2_criteria_runs.csv, table_4_2_criteria.csv/.json, table_4_2_c4.tex
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONDITIONS, LEGS, OUT, condition_order, get, included_runs, load_json, load_manifest,  # noqa: E402
                    num, pooled, run_path, stats, tex_escape, write_csv, write_json)

PHASES = ("PRE", "ATTACK", "DRAIN")
RELIABILITY_IDS = ("le_40ms_stick", "le_140ms_video", "le_1s_honeywell")


def fraction_value(entry: dict):
    # the per-run JSON key is "fraccao_le_threshold" (spelling kept in the harness); accept both
    return num(entry.get("fraction_le_threshold", entry.get("fraccao_le_threshold")))


def run_rows(run: dict) -> list[dict]:
    rd = run_path(run)
    rep = load_json(rd / "run_kpi_report.json")
    rows = []
    for leg in LEGS:
        for p in PHASES:
            ph = get(rep, f"kpi_c2_real.{leg}.phases.{p}")
            if not isinstance(ph, dict):
                continue
            r = {"run_id": run["run_id"], "condition": pooled(run["condition"]), "category": run["category"],
                 "leg": leg, "phase": p, "n": ph.get("n")}
            c1 = ph.get("criterion_C1") or {}
            r["C1_effect_present"] = c1.get("effect_present")
            r["C1_effect_tail_only"] = c1.get("effect_tail_only")
            for e in ph.get("reliability_22125") or []:
                if e.get("id") in RELIABILITY_IDS:
                    r[f"rel_{e['id']}"] = num(e.get("reliability"))
                    r[f"rel_{e['id']}_pass"] = e.get("pass")
            for e in ph.get("budget_fractions") or []:
                if e.get("id") == "le_1s_honeywell":
                    r["frac_le_1s_honeywell"] = fraction_value(e)
                    r["frac_le_1s_honeywell_pass"] = e.get("pass")
            bt = get(ph, "interruptions.by_threshold") or {}
            r["gaps_gt_3s"] = get(bt, "gt_3s.gap_count")
            r["gaps_gt_5s"] = get(bt, "gt_5s.gap_count")
            r["largest_gap_s"] = num(get(ph, "interruptions.largest_gap_s"))
            for ck, cv in (get(ph, "criterion_C4.checks") or {}).items():
                r[f"C4_{ck}_pass"] = cv.get("pass") if isinstance(cv, dict) else None
            r["C4_partial_pass"] = get(ph, "criterion_C4.pass_aggregate_partial")
            k7 = get(rep, f"link_vivacity.k7_command.{p}") or {}
            k8 = get(rep, f"link_vivacity.k8_return.{p}") or {}
            r["K7_fs_gcs_triggered"] = get(k7, "FS_GCS.triggered")
            r["K7_pass"] = k7.get("pass")
            r["K8_max_gap_s"] = num(k8.get("max_gap_s"))
            r["K8_loss_declarations"] = k8.get("loss_declarations")
            r["K8_pass"] = k8.get("pass")
            rows.append(r)
    return rows


def main() -> int:
    manifest = load_manifest()
    rows = []
    for run in included_runs(manifest, ("campaign",)):
        rows += run_rows(run)
    fields = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("run_id", "condition", "category", "leg", "phase", "n"), k))
    write_csv(OUT / "table_4_2_criteria_runs.csv", rows, fields)

    value_cols = [c for c in fields if c not in ("run_id", "condition", "category", "leg", "phase")
                  and not c.endswith("_pass") and not c.startswith(("C1_", "K7_fs"))]
    bool_cols = [c for c in fields if c.endswith("_pass") or c.startswith("C1_") or c == "K7_fs_gcs_triggered"]
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["condition"], r["leg"], r["phase"]), []).append(r)
    agg = []
    for (cid, leg, p), rs in sorted(groups.items(), key=lambda kv: (condition_order(kv[0][0]), kv[0][1], PHASES.index(kv[0][2]))):
        a = {"condition": cid, "item": CONDITIONS.get(cid, ("", "", ""))[0], "label": CONDITIONS.get(cid, ("", "", cid))[2],
             "leg": leg, "phase": p, "n_runs": len(rs)}
        for c in value_cols:
            a[c] = stats(r.get(c) for r in rs)
        for c in bool_cols:
            vals = [r.get(c) for r in rs if isinstance(r.get(c), bool)]
            a[c] = {"true": sum(vals), "of": len(vals)}
        agg.append(a)
    write_json(OUT / "table_4_2_criteria.json", {"schema": "flyblind_table_4_2_v1", "rows": agg})
    flat = []
    for a in agg:
        f = {k: a[k] for k in ("condition", "item", "label", "leg", "phase", "n_runs")}
        for c in value_cols:
            f[f"{c}_mean"] = a[c]["mean"]
        for c in bool_cols:
            f[c] = f"{a[c]['true']}/{a[c]['of']}" if a[c]["of"] else ""
        flat.append(f)
    write_csv(OUT / "table_4_2_criteria.csv", flat, list(flat[0].keys()))

    # LaTeX: C4 on both legs, baseline vs headline attack, ATTACK window (values + pass count)
    idx = {(a["condition"], a["leg"], a["phase"]): a for a in agg}
    pairs = [("a_baseline", "Baseline"), ("a_atk_n4", "Attack N=4")]
    checks = [("rel_le_40ms_stick", "Reliability $\\le$40 ms $\\ge$99.9\\% (TS 22.125 stick)", 100.0, "\\%"),
              ("frac_le_1s_honeywell", "OWD $\\le$1 s in $\\ge$95\\% (Honeywell citing DO-377A)", 100.0, "\\%"),
              ("rel_le_1s_honeywell", "Reliability $\\le$1 s $\\ge$99.9\\% (TS 22.125)", 100.0, "\\%"),
              ("rel_le_140ms_video", "Reliability $\\le$140 ms $\\ge$99.99\\% (TS 22.125 video)", 100.0, "\\%"),
              ("largest_gap_s", "Largest gap (s); limits 3 s / 5 s", 1.0, " s")]
    pass_col = {"rel_le_40ms_stick": "rel_le_40ms_stick_pass", "frac_le_1s_honeywell": "frac_le_1s_honeywell_pass",
                "rel_le_1s_honeywell": "rel_le_1s_honeywell_pass", "rel_le_140ms_video": "rel_le_140ms_video_pass",
                "largest_gap_s": "C4_no_gap_gt_3s_pass"}

    def cell(a, col, scale, unit):
        if not a or a[col]["mean"] is None:
            return "--"
        pc = a.get(pass_col[col]) or {}
        mark = f" ({pc['true']}/{pc['of']})" if pc.get("of") else ""
        return f"{a[col]['mean'] * scale:.1f}{unit}{mark}"
    lines = [r"% Auto-generated by 03_analysis/criteria_table.py (R2). Do NOT edit by hand.",
             r"% Values are means over replicates; (k/n) = runs that pass the criterion.",
             r"\begin{tabular}{lcccc}", r"\toprule",
             r" & \multicolumn{2}{c}{Command leg (GCS$\to$UAV)} & \multicolumn{2}{c}{Return leg (UAV$\to$GCS)} \\",
             r"Criterion (ATTACK window) & " + " & ".join(n for _, n in pairs) + " & " + " & ".join(n for _, n in pairs) + r" \\",
             r"\midrule"]
    for col, label, scale, unit in checks:
        cells = [cell(idx.get((cid, leg, "ATTACK")), col, scale, unit) for leg in ("command", "return") for cid, _ in pairs]
        if col == "rel_le_140ms_video":
            cells[0] = cells[1] = "n/a"
        lines.append(f"{label} & " + " & ".join(cells) + r" \\")
    k7 = [idx.get((cid, "command", "ATTACK")) for cid, _ in pairs]
    k8 = [idx.get((cid, "return", "ATTACK")) for cid, _ in pairs]
    k7c = [f"{a['K7_fs_gcs_triggered']['true']}/{a['K7_fs_gcs_triggered']['of']} runs" if a else "--" for a in k7]
    k8c = [f"{a['K8_max_gap_s']['mean']:.2f} s" if a and a["K8_max_gap_s"]["mean"] is not None else "--" for a in k8]
    lines.append(r"FS\_GCS failsafe triggered (K7) & " + " & ".join(k7c) + r" & n/a & n/a \\")
    lines.append(r"Largest QGC-style gap (K8; alarm at 3.5 s) & n/a & n/a & " + " & ".join(k8c) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    (OUT / "table_4_2_c4.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[4.2] {len(rows)} run rows, {len(agg)} aggregated rows -> out/table_4_2_*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

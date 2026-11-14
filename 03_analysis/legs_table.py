#!/usr/bin/env python3
##################################################################################################################################
# Module: legs_table.py (per-leg KPI table out/table_4_1_legs*: the same KPIs on both legs, command                              #
#     GCS->UAV and return UAV->GCS, per condition and phase)                                                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Two sources per leg: c2     real C2 traffic: setpoints (command) and LOCAL_POSITION_NED telemetry (return), taken #
#   from run_kpi_report.json (kpi_c2_real), already windowed by phase probe  dedicated per-leg probes (TS 22.125 stick-steering  #
#   format), recomputed per phase from the raw rx/tx records with kpi_leg.compute_owd_kpis (same function the probe itself uses) #
#                                                                                                                                #
# Outputs (out/): table_4_1_legs_runs.csv   one row per run x source x leg x phase table_4_1_legs.csv/.json  aggregated over     #
#   replicates (mean, sd, min, max, n) table_4_1_legs.tex        compact LaTeX table (ATTACK window, real C2 traffic)            #
##################################################################################################################################
"""Per-leg KPI table (out/table_4_1_legs*): the same KPIs on both legs (command GCS->UAV, return UAV->GCS), per condition and phase.

Two sources per leg:
  c2     real C2 traffic: setpoints (command) and LOCAL_POSITION_NED telemetry (return),
         taken from run_kpi_report.json (kpi_c2_real), already windowed by phase
  probe  dedicated per-leg probes (TS 22.125 stick-steering format), recomputed per phase from the
         raw rx/tx records with kpi_leg.compute_owd_kpis (same function the probe itself uses)

Outputs (out/):
  table_4_1_legs_runs.csv   one row per run x source x leg x phase
  table_4_1_legs.csv/.json  aggregated over replicates (mean, sd, min, max, n)
  table_4_1_legs.tex        compact LaTeX table (ATTACK window, real C2 traffic)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONDITIONS, LEGS, OUT, PHASES, condition_order, fmt_mean_sd, get, included_runs,  # noqa: E402
                    load_json, load_manifest, pooled, run_path, stats, tex_escape, write_csv, write_json, open_maybe_gz, result_exists)
from kpi_leg import compute_owd_kpis  # noqa: E402
from phase_windows import load_t0_mono_ns  # noqa: E402

METRICS = ("n", "owd_p50_s", "owd_p95_s", "owd_p99_s", "owd_p99_9_s", "owd_max_s", "ipdv_p99_s", "loss_frac")


def phase_row(ph: dict) -> dict:
    owd = get(ph, "owd_s.primary") or {}
    ipdv = get(ph, "ipdv_s.primary") or {}
    return {"n": ph.get("n"), "owd_p50_s": owd.get("p50"), "owd_p95_s": owd.get("p95"), "owd_p99_s": owd.get("p99"),
            "owd_p99_9_s": owd.get("p99_9"), "owd_max_s": owd.get("max"), "ipdv_p99_s": ipdv.get("p99")}


def read_jsonl(path: Path) -> list[dict]:
    out = []
    with open_maybe_gz(path) as f:                       # the per-leg probe records ship gzipped
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def probe_phases(rd: Path, leg: str, windows: dict, t0: int | None) -> dict:
    d = rd / "sidecars" / f"probes_{leg}"
    kpi_f, rx_f, tx_f = d / f"kpi_{leg}.json", d / f"rx_{leg}.jsonl", d / f"tx_{leg}.jsonl"
    if not all(result_exists(p) for p in (kpi_f, rx_f, tx_f)):
        return {}
    k = load_json(kpi_f)
    rx = [r for r in read_jsonl(rx_f) if not r.get("rejected")]
    tx_seqs = [int(r["seq"]) for r in read_jsonl(tx_f) if r.get("seq") is not None]
    res = compute_owd_kpis(leg=leg, samples_rx=rx, tx_seqs=tx_seqs, rate_hz=float(k.get("rate_hz") or 25.0),
                           duration_s=float(get(k, "delivery_rate_post_warmup.duration_s") or 0.0),
                           payload_bytes=k.get("payload_bytes"), port=k.get("port"),
                           windows=windows, t0_mono_ns=t0)
    out = {p: phase_row(v) for p, v in (res.get("phases") or {}).items()}
    if "ALL" in out:
        out["ALL"]["loss_frac"] = get(res, "loss_by_seq.loss_frac")
    return out


def main() -> int:
    manifest = load_manifest()
    rows = []
    for run in included_runs(manifest):
        rd = run_path(run)
        rep = load_json(rd / "run_kpi_report.json")
        pw = load_json(rd / "flyblind_phase_windows.json")
        windows, t0 = pw.get("windows") or {}, load_t0_mono_ns(rd)
        base = {"run_id": run["run_id"], "condition": pooled(run["condition"]), "category": run["category"],
                "load_over_limit": get(run, "flags.load_over_limit")}
        for leg in LEGS:
            for p in PHASES:
                ph = get(rep, f"kpi_c2_real.{leg}.phases.{p}")
                if isinstance(ph, dict):
                    r = dict(base, source="c2", leg=leg, phase=p, **phase_row(ph))
                    if p == "ALL":
                        r["loss_frac"] = get(rep, f"kpi_c2_real.{leg}.loss_by_seq.loss_frac")
                    rows.append(r)
            for p, v in probe_phases(rd, leg, windows, t0).items():
                rows.append(dict(base, source="probe", leg=leg, phase=p, **v))
    fields = ["run_id", "condition", "category", "load_over_limit", "source", "leg", "phase", *METRICS]
    write_csv(OUT / "table_4_1_legs_runs.csv", rows, fields)

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["condition"], r["category"], r["source"], r["leg"], r["phase"]), []).append(r)
    agg = []
    for (cid, cat, src, leg, p), rs in sorted(groups.items(), key=lambda kv: (condition_order(kv[0][0]), kv[0][2], kv[0][3], PHASES.index(kv[0][4]))):
        a = {"condition": cid, "item": CONDITIONS.get(cid, ("", "", ""))[0], "label": CONDITIONS.get(cid, ("", "", cid))[2],
             "category": cat, "source": src, "leg": leg, "phase": p, "n_runs": len(rs),
             "runs": [r["run_id"] for r in rs], "runs_load_over_limit": [r["run_id"] for r in rs if r.get("load_over_limit")]}
        for m in METRICS:
            a[m] = stats(r.get(m) for r in rs)
        agg.append(a)
    write_json(OUT / "table_4_1_legs.json", {"schema": "flyblind_table_4_1_v1", "rows": agg})
    flat = []
    for a in agg:
        f = {k: a[k] for k in ("condition", "item", "label", "category", "source", "leg", "phase", "n_runs")}
        for m in METRICS:
            f[f"{m}_mean"], f[f"{m}_sd"] = a[m]["mean"], a[m]["sd"]
        flat.append(f)
    write_csv(OUT / "table_4_1_legs.csv", flat, list(flat[0].keys()))

    # LaTeX: ATTACK window, real C2 traffic, OWD p50 / p99 on both legs (ms; s when >= 1 s)
    def cell(st):
        m = st.get("mean")
        if m is None:
            return "--"
        return fmt_mean_sd(st, 1.0, 2) + " s" if m >= 1.0 else fmt_mean_sd(st, 1e3, 1) + " ms"
    idx = {(a["condition"], a["source"], a["leg"], a["phase"]): a for a in agg}
    lines = [r"% Auto-generated by 03_analysis/legs_table.py (R2). Do NOT edit by hand.",
             r"\begin{tabular}{llrcccc}", r"\toprule",
             r"Item & Condition & $n$ & Cmd OWD p50 & Cmd OWD p99 & Ret OWD p50 & Ret OWD p99 \\", r"\midrule"]
    for cid in sorted({a["condition"] for a in agg if a["source"] == "c2"}, key=condition_order):
        c, r = idx.get((cid, "c2", "command", "ATTACK")), idx.get((cid, "c2", "return", "ATTACK"))
        if not c or not r:
            continue
        item, _, label = CONDITIONS.get(cid, ("", "", cid))
        lines.append(f"{item} & {tex_escape(label)} & {c['n_runs']} & {cell(c['owd_p50_s'])} & {cell(c['owd_p99_s'])} & "
                     f"{cell(r['owd_p50_s'])} & {cell(r['owd_p99_s'])} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    (OUT / "table_4_1_legs.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[4.1] {len(rows)} run rows, {len(agg)} aggregated rows -> out/table_4_1_legs.*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

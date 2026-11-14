#!/usr/bin/env python3
##################################################################################################################################
# Module: dl_ul_by_phase.py (Per-phase uplink and downlink rates per role, from the gNB MAC byte counters)                       #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Why this exists: `prb.json` reports `ul_mbps` / `dl_mbps` averaged over the whole capture window (~760 s), which  #
#   mixes PRE, ATTACK and DRAIN. This module differences the same cumulative counters inside each phase window, so the ATTACK    #
#   steady-state rate of every role is separated from the baseline, in both directions.                                          #
#                                                                                                                                #
# Source of truth: `mac-prb-raw.log`, the OAI gNB MAC dump captured per run. Lines of the form                                   #
#                                                                                                                                #
# t=<mono_s> UE <rnti>: MAC:    TX <dl_bytes> RX <ul_bytes> bytes                                                                #
#                                                                                                                                #
# carry cumulative per-UE byte counters. Polarity (gNB transmit = DL, gNB receive = UL) is the one documented and confirmed in   #
#   01_scripts/02_experiments/probes/mac_prb.py.                                                                                 #
#                                                                                                                                #
# TIME ANCHOR (the subtle part): `t=` is host monotonic seconds, and the capture starts a few seconds AFTER the run's T0 (15.1 s #
#   in the run checked while writing this). The `t_rel_s` column of `prb.csv` is capture-relative, NOT run-relative, so slicing  #
#   it directly with `flyblind_phase_windows.json` shifts every window by that offset. Here t_rel is recomputed as `t -          #
#   T0_mono_ns/1e9`, with T0 resolved by the harness's own `load_t0_mono_ns`, so the windows match the ones used by the KPI      #
#   tables.                                                                                                                      #
#                                                                                                                                #
# Role and slice identity come from `prb.json` (`rnti_role_map`, `role_slice_map`, derived from the gNB's native active-slice    #
#   list). Phase windows come from `flyblind_phase_windows.json`.                                                                #
#                                                                                                                                #
# Read-only over 02_results/; outputs go to 03_analysis/out/: dl_ul_by_phase_roles.csv  one row per run x phase x role           #
#   dl_ul_by_phase_runs.csv   one row per run x phase (cell totals and the derived shares) dl_ul_by_phase.csv/.json  aggregated  #
#   over replicates, per condition x phase                                                                                       #
#                                                                                                                                #
# Usage: ./dl_ul_by_phase.py                     every included campaign run in the manifest ./dl_ul_by_phase.py DIR [DIR ...]   #
#   arbitrary run directories (e.g. 02_results/tests/...) ./dl_ul_by_phase.py --attack-pad-s 30   seconds of ATTACK ramp to drop #
#   (default 30)                                                                                                                 #
##################################################################################################################################
"""Per-phase uplink and downlink rates per role, from the gNB MAC byte counters.

Why this exists: `prb.json` reports `ul_mbps` / `dl_mbps` averaged over the whole capture
window (~760 s), which mixes PRE, ATTACK and DRAIN. This module differences the same
cumulative counters inside each phase window, so the ATTACK steady-state rate of every role
is separated from the baseline, in both directions.

Source of truth: `mac-prb-raw.log`, the OAI gNB MAC dump captured per run. Lines of the form

    t=<mono_s> UE <rnti>: MAC:    TX <dl_bytes> RX <ul_bytes> bytes

carry cumulative per-UE byte counters. Polarity (gNB transmit = DL, gNB receive = UL) is the
one documented and confirmed in 01_scripts/02_experiments/probes/mac_prb.py.

TIME ANCHOR (the subtle part): `t=` is host monotonic seconds, and the capture starts a few
seconds AFTER the run's T0 (15.1 s in the run checked while writing this). The `t_rel_s`
column of `prb.csv` is capture-relative, NOT run-relative, so slicing it directly with
`flyblind_phase_windows.json` shifts every window by that offset. Here t_rel is recomputed as
`t - T0_mono_ns/1e9`, with T0 resolved by the harness's own `load_t0_mono_ns`, so the windows
match the ones used by the KPI tables.

Role and slice identity come from `prb.json` (`rnti_role_map`, `role_slice_map`, derived from
the gNB's native active-slice list). Phase windows come from `flyblind_phase_windows.json`.

Read-only over 02_results/; outputs go to 03_analysis/out/:
  dl_ul_by_phase_roles.csv  one row per run x phase x role
  dl_ul_by_phase_runs.csv   one row per run x phase (cell totals and the derived shares)
  dl_ul_by_phase.csv/.json  aggregated over replicates, per condition x phase

Usage:
  ./dl_ul_by_phase.py                     every included campaign run in the manifest
  ./dl_ul_by_phase.py DIR [DIR ...]       arbitrary run directories (e.g. 02_results/tests/...)
  ./dl_ul_by_phase.py --attack-pad-s 30   seconds of ATTACK ramp to drop (default 30)
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ARTIFACTS, OUT, PHASES, condition_order, included_runs, load_json,  # noqa: E402
                    load_manifest, run_path, stats, write_csv, write_json, open_maybe_gz, result_exists)
from phase_windows import load_t0_mono_ns  # noqa: E402

# "t=160933.560 UE 38ba: MAC:    TX          56141 RX         497970 bytes"
RE_MAC = re.compile(r"^t=(\d+\.\d+)\s+UE\s+([0-9a-fA-F]{4}):\s+MAC:\s+TX\s+(\d+)\s+RX\s+(\d+)\s+bytes")

# A window needs at least this much differenced span to give a meaningful rate.
MIN_SPAN_S = 20.0
# Roles aggregated as "the adversary"; everything else is reported individually.
ROGUE_PREFIX = "rogue"

RUN_FIELDS = ["run_id", "condition", "mitigations", "phase", "lo_s", "hi_s", "span_s", "samples",
              "cell_ul_mbps", "cell_dl_mbps", "rogue_ul_mbps", "rogue_dl_mbps",
              "rogue_dl_share_pct", "uav_ul_mbps", "gcs_dl_mbps", "gcs_dl_minus_uav_ul_mbps"]
ROLE_FIELDS = ["run_id", "condition", "mitigations", "phase", "role", "slice",
               "lo_s", "hi_s", "span_s", "samples", "dl_mbps", "ul_mbps"]
AGG_METRICS = ("cell_ul_mbps", "cell_dl_mbps", "rogue_ul_mbps", "rogue_dl_mbps",
               "rogue_dl_share_pct", "uav_ul_mbps", "gcs_dl_mbps")


def parse_mac_log(path: Path, t0_mono_s: float) -> dict[str, list[tuple[float, int, int]]]:
    """rnti -> [(t_rel_s, cumulative_dl_bytes, cumulative_ul_bytes)], ordered as captured."""
    per: dict[str, list[tuple[float, int, int]]] = collections.defaultdict(list)
    with open_maybe_gz(path) as f:                      # the raw MAC dump ships gzipped
        for line in f:
            m = RE_MAC.match(line)
            if m:
                per[m.group(2).lower()].append(
                    (float(m.group(1)) - t0_mono_s, int(m.group(3)), int(m.group(4))))
    return per


def mitigation_key(run_dir: Path) -> str:
    """Comma-separated list of mitigations that were actually on, or 'none' for the stock stack."""
    meta_path = run_dir / "run_meta.json"
    if not meta_path.is_file():
        return "unknown"
    mit = load_json(meta_path).get("mitigations") or {}
    on = sorted(k for k, v in mit.items() if v not in (0, False, None, "0", ""))
    return ",".join(on) if on else "none"


def phase_rates(samples: dict[str, list[tuple[float, int, int]]], lo: float, hi: float,
                ) -> tuple[dict[str, dict[str, float]], float, int]:
    """Difference the counters inside [lo, hi]. Returns per-rnti rates, the span and the sample count."""
    out: dict[str, dict[str, float]] = {}
    spans: list[float] = []
    total = 0
    for rnti, series in samples.items():
        inside = [s for s in series if lo <= s[0] <= hi]
        if len(inside) < 2:
            continue
        span = inside[-1][0] - inside[0][0]
        if span < MIN_SPAN_S:
            continue
        out[rnti] = {"dl_mbps": 8.0 * (inside[-1][1] - inside[0][1]) / span / 1e6,
                     "ul_mbps": 8.0 * (inside[-1][2] - inside[0][2]) / span / 1e6}
        spans.append(span)
        total += len(inside)
    return out, (statistics.fmean(spans) if spans else 0.0), total


def run_rows(run_dir: Path, condition: str, attack_pad_s: float,
             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    """(per-role rows, per-run-phase rows, skip reason)."""
    raw = run_dir / "mac-prb-raw.log"
    prb = run_dir / "prb.json"
    win = run_dir / "flyblind_phase_windows.json"
    for p in (raw, prb, win):
        if not result_exists(p):
            return [], [], f"missing {p.name}"
    t0_ns = load_t0_mono_ns(run_dir)
    if t0_ns is None:
        return [], [], "no T0_mono_ns (clock_anchor.json / run_meta.json)"

    prb_doc = load_json(prb)
    rnti2role: dict[str, str] = {k.lower(): v for k, v in (prb_doc.get("rnti_role_map") or {}).items()}
    role2slice: dict[str, str] = prb_doc.get("role_slice_map") or {}
    windows = (load_json(win).get("windows") or {})
    samples = parse_mac_log(raw, t0_ns / 1e9)
    if not samples:
        return [], [], "no MAC byte-counter lines in mac-prb-raw.log"

    run_id, mit = run_dir.name, mitigation_key(run_dir)
    role_rows: list[dict[str, Any]] = []
    phase_rows: list[dict[str, Any]] = []

    for phase in PHASES:
        w = windows.get(phase)
        if not w:
            continue
        lo, hi = float(w["t_rel_lo_s"]), float(w["t_rel_hi_s"])
        if phase == "ATTACK":
            lo += attack_pad_s          # drop the ramp: steady state only
        if hi - lo < MIN_SPAN_S:
            continue
        per_rnti, span, n_samples = phase_rates(samples, lo, hi)
        if not per_rnti:
            continue

        by_role: dict[str, dict[str, float]] = {}
        for rnti, rates in per_rnti.items():
            role = rnti2role.get(rnti, f"unmapped_{rnti}")
            acc = by_role.setdefault(role, {"dl_mbps": 0.0, "ul_mbps": 0.0})
            acc["dl_mbps"] += rates["dl_mbps"]
            acc["ul_mbps"] += rates["ul_mbps"]

        common = {"run_id": run_id, "condition": condition, "mitigations": mit, "phase": phase,
                  "lo_s": round(lo, 3), "hi_s": round(hi, 3), "span_s": round(span, 3),
                  "samples": n_samples}
        for role in sorted(by_role):
            role_rows.append({**common, "role": role, "slice": role2slice.get(role, ""),
                              "dl_mbps": round(by_role[role]["dl_mbps"], 4),
                              "ul_mbps": round(by_role[role]["ul_mbps"], 4)})

        cell_dl = sum(v["dl_mbps"] for v in by_role.values())
        cell_ul = sum(v["ul_mbps"] for v in by_role.values())
        rog_dl = sum(v["dl_mbps"] for k, v in by_role.items() if k.startswith(ROGUE_PREFIX))
        rog_ul = sum(v["ul_mbps"] for k, v in by_role.items() if k.startswith(ROGUE_PREFIX))
        uav_ul = by_role.get("uav", {}).get("ul_mbps", 0.0)
        gcs_dl = by_role.get("gcs", {}).get("dl_mbps", 0.0)
        phase_rows.append({**common,
                           "cell_ul_mbps": round(cell_ul, 4), "cell_dl_mbps": round(cell_dl, 4),
                           "rogue_ul_mbps": round(rog_ul, 4), "rogue_dl_mbps": round(rog_dl, 4),
                           "rogue_dl_share_pct": round(100.0 * rog_dl / cell_dl, 4) if cell_dl else None,
                           "uav_ul_mbps": round(uav_ul, 4), "gcs_dl_mbps": round(gcs_dl, 4),
                           # > 0 means the downlink delivered everything the uplink admitted
                           "gcs_dl_minus_uav_ul_mbps": round(gcs_dl - uav_ul, 4)})
    return role_rows, phase_rows, None


def collect(run_dirs: list[tuple[Path, str]], attack_pad_s: float,
            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    role_rows: list[dict[str, Any]] = []
    phase_rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for run_dir, condition in run_dirs:
        rr, pr, why = run_rows(run_dir, condition, attack_pad_s)
        if why:
            skipped.append(f"{run_dir.name}: {why}")
            continue
        role_rows += rr
        phase_rows += pr
    return role_rows, phase_rows, skipped


def aggregate(phase_rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """mean/sd/min/max/n over replicates, grouped by `key` x phase."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for r in phase_rows:
        groups[(r[key] or "", r["phase"])].append(r)
    out: list[dict[str, Any]] = []
    for (gk, phase), rows in groups.items():
        entry: dict[str, Any] = {key: gk, "phase": phase, "n_runs": len(rows)}
        for metric in AGG_METRICS:
            st = stats(r.get(metric) for r in rows)
            entry[f"{metric}_mean"] = None if st["mean"] is None else round(st["mean"], 4)
            entry[f"{metric}_sd"] = None if st["sd"] is None else round(st["sd"], 4)
            entry[f"{metric}_min"] = None if st["min"] is None else round(st["min"], 4)
            entry[f"{metric}_max"] = None if st["max"] is None else round(st["max"], 4)
            entry[f"{metric}_median"] = round(
                statistics.median([r[metric] for r in rows if r.get(metric) is not None]), 4
            ) if any(r.get(metric) is not None for r in rows) else None
        out.append(entry)
    order = {p: i for i, p in enumerate(PHASES)}
    return sorted(out, key=lambda e: (e[key], order.get(e["phase"], 99)))


def print_summary(agg: list[dict[str, Any]], key: str, phase: str) -> None:
    rows = [e for e in agg if e["phase"] == phase]
    if not rows:
        print(f"(no rows for phase {phase})")
        return
    print(f"\n== {phase} (median over runs, Mb/s) grouped by {key}")
    head = (f'{key[:30]:<32}{"n":>3}{"UL cell":>9}{"DL cell":>9}{"rogue UL":>9}'
            f'{"rogue DL":>9}{"rogDL%":>8}{"uav UL":>8}{"gcs DL":>8}')
    print(head)
    print("-" * len(head))
    for e in sorted(rows, key=lambda x: -x["n_runs"]):
        def v(m: str, d: int = 2) -> str:
            x = e.get(f"{m}_median")
            return "-".rjust(0) if x is None else f"{x:.{d}f}"
        print(f'{str(e[key])[:30]:<32}{e["n_runs"]:>3}{v("cell_ul_mbps"):>9}{v("cell_dl_mbps"):>9}'
              f'{v("rogue_ul_mbps"):>9}{v("rogue_dl_mbps", 3):>9}{v("rogue_dl_share_pct"):>8}'
              f'{v("uav_ul_mbps"):>8}{v("gcs_dl_mbps"):>8}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="*", type=Path,
                    help="run directories; default is every included campaign run in the manifest")
    ap.add_argument("--attack-pad-s", type=float, default=30.0,
                    help="seconds of ATTACK ramp to drop before differencing (default 30)")
    ap.add_argument("--phase", default="ATTACK", help="phase shown in the printed summary (default ATTACK)")
    ap.add_argument("--no-write", action="store_true", help="print only; do not write out/")
    args = ap.parse_args()

    if args.runs:
        run_dirs = [(p.resolve(), "ad-hoc") for p in args.runs]
        group_key = "mitigations"
    else:
        manifest = load_manifest()
        run_dirs = [(run_path(r), r["condition"]) for r in included_runs(manifest)]
        group_key = "condition"
    print(f"runs: {len(run_dirs)}  attack_pad_s: {args.attack_pad_s}")

    role_rows, phase_rows, skipped = collect(run_dirs, args.attack_pad_s)
    if skipped:
        print(f"skipped {len(skipped)} run(s) without usable MAC capture:")
        for s in skipped[:10]:
            print(f"  - {s}")
        if len(skipped) > 10:
            print(f"  ... and {len(skipped) - 10} more")
    if not phase_rows:
        print("no usable runs; nothing to do")
        return 1

    agg_main = aggregate(phase_rows, group_key)
    print_summary(agg_main, group_key, args.phase)
    if group_key != "mitigations":
        print_summary(aggregate(phase_rows, "mitigations"), "mitigations", args.phase)

    if not args.no_write:
        role_rows.sort(key=lambda r: (condition_order(r["condition"]), r["run_id"], r["phase"], r["role"]))
        phase_rows.sort(key=lambda r: (condition_order(r["condition"]), r["run_id"], r["phase"]))
        write_csv(OUT / "dl_ul_by_phase_roles.csv", role_rows, ROLE_FIELDS)
        write_csv(OUT / "dl_ul_by_phase_runs.csv", phase_rows, RUN_FIELDS)
        agg_fields = [group_key, "phase", "n_runs"] + [
            f"{m}_{s}" for m in AGG_METRICS for s in ("median", "mean", "sd", "min", "max")]
        write_csv(OUT / "dl_ul_by_phase.csv", agg_main, agg_fields)
        write_json(OUT / "dl_ul_by_phase.json", {
            "source": "mac-prb-raw.log (OAI gNB MAC cumulative byte counters), differenced per phase window",
            "time_anchor": "t_rel = t_mono - T0_mono_ns/1e9 (clock_anchor.json); prb.csv t_rel is "
                           "capture-relative and must not be sliced with the phase windows directly",
            "attack_pad_s": args.attack_pad_s,
            "min_span_s": MIN_SPAN_S,
            "group_key": group_key,
            "n_runs": len(run_dirs),
            "n_skipped": len(skipped),
            "skipped": skipped,
            "by_group": agg_main,
            "by_mitigation": aggregate(phase_rows, "mitigations"),
        })
        print(f"\nwrote: {OUT/'dl_ul_by_phase_roles.csv'}")
        print(f"       {OUT/'dl_ul_by_phase_runs.csv'}")
        print(f"       {OUT/'dl_ul_by_phase.csv'}")
        print(f"       {OUT/'dl_ul_by_phase.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

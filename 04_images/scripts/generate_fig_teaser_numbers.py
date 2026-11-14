#!/usr/bin/env python3
##################################################################################################################################
# Module: generate_fig_teaser_numbers.py (fig:teaser — numbers from the run artifacts)                                           #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Writes fig_teaser_numbers.tex (LaTeX macros) with the headline numbers shown in fig:teaser, computed from the     #
# per-run artifacts of the headline condition (attack N=4, video 12 Mb/s). No number is typed by hand.                           #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 generate_fig_teaser_numbers.py                  # campaign headline runs (03_analysis manifest)                      #
#   python3 generate_fig_teaser_numbers.py --check-august   # self-test: reproduces the August 2026 figure values                #
##################################################################################################################################
"""
fig:teaser numbers — same definitions as tab:false-healthy (median over the replicate runs, whole run):

Read from the same analysis outputs tab:false-healthy is built from, so the teaser and the
table agree by construction. They previously came from per-run summaries over the whole run,
which put a different OWD (the network probe) and a different window next to the same labels.

  OWD p99          table_4_1_legs.json   c2 / command / ATTACK  owd_p99_s * 1000     (ms)
  delta p50 / p99  belief_by_phase.json  a_atk_n4 / ATTACK      aoi_p50_ms / p99     (s)
  belief p50       belief_by_phase.json  a_atk_n4 / ATTACK      belief_p50_m         (m)
  delivery         run_kpi_report.json   legs_table.command.delivery_rate_post_warmup
                                         .delivery_rate_post_warmup_pct                (%)
                   (was the network probe's availability, which has no row in the table
                    and measures the probe rather than the C2 traffic)

All four follow the table's convention of the mean across the condition's runs.

Runs: the campaign manifest (03_analysis/out/campaign_manifest.json), condition a_atk_n4,
category "campaign". --check-august recomputes the August 2026 values (02_results_USENIX, RUN01-03)
and asserts they equal the numbers previously typed in the figure.

Output: fig_teaser_numbers.tex next to this script (input by fig_teaser_body.tex).
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 03_images/scripts/
ART = HERE.parents[1]                           # artifacts/
MANIFEST = ART / "03_analysis" / "out" / "campaign_manifest.json"
LEGS = ART / "03_analysis" / "out" / "table_4_1_legs.json"
BELIEF = ART / "03_analysis" / "out" / "belief_by_phase.json"
HEADLINE = "a_atk_n4"
AUGUST_DIR = ART / "02_results_USENIX" / "106_PRB-CROSS-4_ROGUES-ATK_N4-600S-12MPBS"
AUGUST_EXPECTED = {"OWD": "29.66", "Avail": "99.96", "DeltaMed": "11.62", "DeltaTail": "12.16", "Belief": "45.67"}
OUT = HERE / "fig_teaser_numbers.tex"


def get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            raise KeyError(path)
        d = d[k]
    return d


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def headline_runs() -> list[Path]:
    m = load(MANIFEST)
    runs = [ART / r["run_dir"] for r in m["runs"] if r["condition"] == HEADLINE and r["category"] == "campaign"]
    if len(runs) < 3:
        raise SystemExit(f"expected >= 3 headline runs in {MANIFEST}, found {len(runs)}")
    return sorted(runs)


def august_runs() -> list[Path]:
    return sorted(p.parent for p in AUGUST_DIR.glob("RUN0*/four_layer_summary.json"))


def _row(path: Path, **match):
    for r in load(path)["rows"]:
        if all(r.get(k) == v for k, v in match.items()):
            return r
    raise SystemExit(f"no row in {path.name} matching {match}")


def numbers(runs: list[Path]) -> dict[str, str]:
    """Campaign numbers, taken from the analysis outputs the headline table is built from."""
    leg = _row(LEGS, condition=HEADLINE, phase="ATTACK", leg="command", source="c2")
    bel = _row(BELIEF, condition=HEADLINE, phase="ATTACK")

    deliv = [get(load(rd / "run_kpi_report.json"),
                 "legs_table.command.delivery_rate_post_warmup."
                 "delivery_rate_post_warmup_pct") for rd in runs]

    return {
        "OWD": f"{leg['owd_p99_s']['mean'] * 1000.0:.2f}",
        "Avail": f"{statistics.mean(deliv):.2f}",
        "DeltaMed": f"{bel['aoi_p50_ms']['mean'] / 1000.0:.2f}",
        "DeltaTail": f"{bel['aoi_p99_ms']['mean'] / 1000.0:.2f}",
        "Belief": f"{bel['belief_p50_m']['mean']:.1f}",
    }


def numbers_per_run(runs: list[Path]) -> dict[str, str]:
    """The original per-run path, kept so --check-august still validates the 2026 values."""
    cols = {k: [] for k in AUGUST_EXPECTED}
    for rd in runs:
        fl = load(rd / "four_layer_summary.json")
        td = load(rd / "telemetry_downlink_summary.json")
        cols["OWD"].append(get(fl, "network.owd_us.p99") / 1000.0)
        cols["Avail"].append(get(fl, "network.availability.availability_post_warmup_pct"))
        cols["DeltaMed"].append(get(td, "telemetry_arrival_delay_ms.p50") / 1000.0)
        cols["DeltaTail"].append(get(td, "telemetry_arrival_delay_ms.p99") / 1000.0)
        cols["Belief"].append(get(fl, "freshness.telemetry_downlink.belief_validation.belief_p99_m"))
    return {k: f"{statistics.median(v):.2f}" for k, v in cols.items()}


def write(nums: dict[str, str], runs: list[Path], out: Path) -> None:
    lines = ["% Auto-generated by generate_fig_teaser_numbers.py (R2). Do NOT edit by hand.",
             "% Median over the headline runs (attack N=4, video 12 Mb/s), same definitions as tab:false-healthy:",
             *[f"%   {rd.relative_to(ART)}" for rd in runs],
             *[f"\\def\\fbTeaser{k}{{{v}}}" for k, v in nums.items()]]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check-august", action="store_true",
                    help="recompute the August 2026 values and assert they match the old typed numbers")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    runs = august_runs() if args.check_august else headline_runs()
    nums = numbers_per_run(runs) if args.check_august else numbers(runs)
    if args.check_august:
        bad = {k: (nums[k], v) for k, v in AUGUST_EXPECTED.items() if nums[k] != v}
        if bad:
            raise SystemExit(f"August check FAILED (got, expected): {bad}")
        print(f"August check OK: {nums}")
    write(nums, runs, args.out)
    print(f"{len(runs)} runs -> {args.out}: {nums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

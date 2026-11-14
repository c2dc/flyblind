#!/usr/bin/env python3
##################################################################################################################################
# Module: generate_tex_numbers.py (Generates numbers.tex and .tex tables only from final runs (R2))                              #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: No number is typed by hand. Refuses any run that is not marked as final.                                          #
#                                                                                                                                #
# Marking: run_dir/FINAL.json with {"final": true, ...} or the CLI list --final-runs pointing to dirs that ALREADY contain       #
#   FINAL.json.                                                                                                                  #
#                                                                                                                                #
# At this stage there are no real final runs; use --allow-synthetic-fixture to validate with synthetic data marked for testing   #
#   only.                                                                                                                        #
##################################################################################################################################
"""Generates numbers.tex and .tex tables only from final runs (R2).

No number is typed by hand. Refuses any run that is not marked as final.

Marking: run_dir/FINAL.json with {"final": true, ...} or the CLI list
--final-runs pointing to dirs that ALREADY contain FINAL.json.

At this stage there are no real final runs; use --allow-synthetic-fixture to
validate with synthetic data marked for testing only.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_FINAL = "flyblind_final_run_marker_v1"


def is_final_run(run_dir: Path) -> tuple[bool, dict[str, Any]]:
    marker = run_dir / "FINAL.json"
    if not marker.is_file():
        return False, {"error": "FINAL.json missing", "run_dir": str(run_dir)}
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, {"error": f"invalid FINAL.json: {exc}", "run_dir": str(run_dir)}
    if not data.get("final") is True and data.get("final") != 1:
        return False, {"error": "FINAL.json without final=true", "data": data}
    return True, data


def _tex_escape(s: str) -> str:
    return (
        s.replace("\\", "\\textbackslash{}")
        .replace("_", "\\_")
        .replace("%", "\\%")
        .replace("&", "\\&")
        .replace("#", "\\#")
    )


def _macro_name(key: str) -> str:
    # LaTeX \\newcommand names are letters only. Spell digits so p50/p99 survive.
    digit_words = {
        "0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
        "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine",
    }
    parts: list[str] = []
    for ch in key:
        if ch.isalpha():
            parts.append(ch)
        elif ch.isdigit():
            parts.append(digit_words[ch])
        # drop other punctuation
    clean = "".join(parts) or "Num"
    return "FB" + clean[0].upper() + clean[1:]


def collect_numbers(run_dirs: list[Path]) -> dict[str, Any]:
    nums: dict[str, Any] = {
        "schema": "flyblind_tex_numbers_v1",
        "runs": [],
        "macros": {},
    }
    for rd in run_dirs:
        ok, marker = is_final_run(rd)
        if not ok:
            raise SystemExit(f"refused (not final): {rd} → {marker}")
        rel = _load_report(rd)
        entry = {
            "run_dir": str(rd),
            "marker": marker,
            "report": str(rd / "run_kpi_report.json") if (rd / "run_kpi_report.json").is_file() else None,
        }
        # Prefer the run_kpi_report.json table
        table = (rel or {}).get("legs_table") or {}
        cmd = table.get("command") or {}
        ret = table.get("return") or {}
        rid = marker.get("id") or rd.name
        prefix = re.sub(r"[^A-Za-z0-9]", "", rid) or "Run"
        macros = {
            f"{prefix}CmdOwdP50": cmd.get("owd_p50_s"),
            f"{prefix}CmdOwdP99": cmd.get("owd_p99_s"),
            f"{prefix}RetOwdP50": ret.get("owd_p50_s"),
            f"{prefix}RetOwdP99": ret.get("owd_p99_s"),
            f"{prefix}Valid": (rel or {}).get("run_meta_valid"),
        }
        # Also accept explicit numbers block inside FINAL.json
        for k, v in (marker.get("numbers") or {}).items():
            macros[f"{prefix}{k}"] = v
        entry["macros"] = macros
        nums["runs"].append(entry)
        for k, v in macros.items():
            if v is None:
                continue
            nums["macros"][_macro_name(k)] = v
    return nums


def _load_report(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "run_kpi_report.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_tex_numbers(path: Path, nums: dict[str, Any]) -> None:
    lines = [
        "% Auto-generated by instrumentation/generate_tex_numbers.py (R2). Do NOT edit by hand.",
        f"% schema: {nums.get('schema')}",
        "",
    ]
    for macro, val in sorted((nums.get("macros") or {}).items()):
        if isinstance(val, bool):
            tex_val = "true" if val else "false"
        elif isinstance(val, float):
            tex_val = f"{val:.6g}"
        elif isinstance(val, int):
            tex_val = str(val)
        else:
            tex_val = _tex_escape(str(val))
        lines.append(f"\\newcommand{{\\{macro}}}{{{tex_val}}}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_table_tex(path: Path, nums: dict[str, Any]) -> None:
    lines = [
        "% Auto-generated by instrumentation/generate_tex_numbers.py (R2). Do NOT edit by hand.",
        "\\begin{tabular}{lrrrr}",
        "\\hline",
        "run & cmd p50 & cmd p99 & ret p50 & ret p99 \\\\",
        "\\hline",
    ]
    for entry in nums.get("runs") or []:
        m = entry.get("macros") or {}
        # pick first matching keys
        keys = list(m.keys())
        def pick(suffix: str):
            for k in keys:
                if k.endswith(suffix):
                    return m[k]
            return ""
        rid = (entry.get("marker") or {}).get("id") or Path(entry["run_dir"]).name
        lines.append(
            f"{_tex_escape(str(rid))} & "
            f"{pick('CmdOwdP50')} & {pick('CmdOwdP99')} & "
            f"{pick('RetOwdP50')} & {pick('RetOwdP99')} \\\\"
        )
    lines += ["\\hline", "\\end{tabular}", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_synthetic_fixture(root: Path) -> Path:
    """Creates a synthetic run marked FINAL to validate the generator."""
    rd = root / "SYNTH_FINAL_TEST"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "FINAL.json").write_text(
        json.dumps({
            "schema": SCHEMA_FINAL,
            "final": True,
            "id": "SynthA",
            "note": "test fixture for the .tex generator; not a campaign run",
            "numbers": {"ExtraNote": "synthetic"},
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    (rd / "run_kpi_report.json").write_text(
        json.dumps({
            "schema": "flyblind_run_kpi_report_v1",
            "run_meta_valid": True,
            "legs_table": {
                "command": {"owd_p50_s": 0.019, "owd_p99_s": 0.035, "phase": "ATTACK", "n": 100},
                "return": {"owd_p50_s": 1.2, "owd_p99_s": 2.5, "phase": "ATTACK", "n": 200},
            },
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    (rd / "run_meta.json").write_text(
        json.dumps({"valid": True, "run_id": "SYNTH_FINAL_TEST"}, indent=2) + "\n",
        encoding="utf-8",
    )
    return rd


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate numbers.tex / table .tex files from final runs")
    ap.add_argument("--final-runs", nargs="*", type=Path, default=[],
                    help="List of final run_dirs (must contain FINAL.json)")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--allow-synthetic-fixture", action="store_true",
                    help="Create and use a synthetic run marked FINAL (test)")
    ap.add_argument("--refuse-unmarked", action="store_true", default=True,
                    help="Refuse dirs without FINAL.json (always on)")
    args = ap.parse_args()

    runs = list(args.final_runs)
    if args.allow_synthetic_fixture:
        synth = write_synthetic_fixture(args.out_dir / "_fixture")
        runs.append(synth)

    if not runs:
        raise SystemExit("no final run; pass --final-runs or --allow-synthetic-fixture")

    for rd in runs:
        ok, info = is_final_run(rd)
        if not ok:
            raise SystemExit(f"refused: {rd} ({info})")

    nums = collect_numbers(runs)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    numbers = args.out_dir / "numbers.tex"
    table_path = args.out_dir / "legs_table.tex"
    meta = args.out_dir / "numbers_meta.json"
    write_tex_numbers(numbers, nums)
    write_table_tex(table_path, nums)
    meta.write_text(json.dumps(nums, indent=2) + "\n", encoding="utf-8")
    print(f"[tex] {numbers}")
    print(f"[tex] {table_path}")
    print(f"[tex] {meta}")


if __name__ == "__main__":
    main()

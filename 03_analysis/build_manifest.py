#!/usr/bin/env python3
##################################################################################################################################
# Module: build_manifest.py (Build the campaign manifest: which runs enter the analysis, and why the others do not)              #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: The run list is derived from the batch logs written by the campaign runner (no hand-typed run list). Every        #
#   executed run appears in the manifest with a category: campaign        valid run, enters the tables capacity_probe  no        #
#   attack, victim video above capacity; VALID=NO by self-congestion is expected positive_control  command leg cut on purpose;   #
#   VALID=NO by the tripped failsafe is expected excluded        invalid run, with the failed gates as reason Whole attempts     #
#   that produced no valid run are listed in excluded_attempts with the reason.                                                  #
#                                                                                                                                #
# Output: out/campaign_manifest.json and out/campaign_manifest.csv                                                               #
##################################################################################################################################
"""Build the campaign manifest: which runs enter the analysis, and why the others do not.

The run list is derived from the batch logs written by the campaign runner (no hand-typed run list).
Every executed run appears in the manifest with a category:
  campaign        valid run, enters the tables
  capacity_probe  no attack, victim video above capacity; VALID=NO by self-congestion is expected
  positive_control  command leg cut on purpose; VALID=NO by the tripped failsafe is expected
  excluded        invalid run, with the failed gates as reason
Whole attempts that produced no valid run are listed in excluded_attempts with the reason.

Output: out/campaign_manifest.json and out/campaign_manifest.csv
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ARTIFACTS, CAMPAIGN, CONDITIONS, MANIFEST, OUT, condition_order, get, load_json,  # noqa: E402
                    num, resolve_run_dir, write_csv, write_json)

# Campaign attempts that produced no valid run (campaign root name -> reason). Empty: every root of the
# intra-slice campaign completed. Kept as the documented place to record a lost attempt.
EXCLUDED_ATTEMPTS: dict[str, str] = {}

# Runs executed by hand with the runner's exact command (no batch log).
MANUAL_RUNS: list[dict] = []

# Valid runs replaced by a rerun of the same condition (applied only if the rerun is valid,
# below the host-load limit and paper-comparable; otherwise both are kept and a warning is printed).
REPLACEMENTS: dict[str, dict] = {}

# Whole conditions excluded after the fact (condition -> reason); their runs stay listed as "excluded".
EXCLUDED_CONDITIONS: dict[str, str] = {}

# Conditions whose runs are EXPECTED to be scored invalid, with the category they get instead.
# capacity_probe  no adversary, victim video raised until it self-congests: VALID=NO is the result
# positive_control  the command leg is cut on purpose, so the tripped failsafe fails the mission gates
EXPECTED_INVALID_PREFIXES = {
    "capacity_probe": ("cap_", "cap51_"),
    "positive_control": ("pos_",),
}
EXPECTED_INVALID_NOTE = {
    "capacity_probe": "VALID=NO expected (self-congestion above capacity); evaluated by goodput",
    "positive_control": "VALID=NO expected (failsafe tripped on purpose); read as a control, not as a scored run",
}
LOAD_LIMIT = 14.0
CATEGORIES = ("campaign", "capacity_probe", "positive_control", "excluded")



def expected_invalid_category(cid: str) -> str | None:
    for cat, prefixes in EXPECTED_INVALID_PREFIXES.items():
        if cid.startswith(prefixes):
            return cat
    return None


def run_meta_summary(run_dir: Path) -> dict:
    meta = load_json(run_dir / "run_meta.json") if (run_dir / "run_meta.json").is_file() else {}
    hs = load_json(run_dir / "host_summary.json") if (run_dir / "host_summary.json").is_file() else {}
    fl = load_json(run_dir / "four_layer_summary.json") if (run_dir / "four_layer_summary.json").is_file() else {}
    stamp = meta.get("topology_stamp") or {}
    load_max = num(hs.get("loadavg1_max"))
    cond = meta.get("condition")
    return {
        "valid": meta.get("valid"),
        "meta": {
            "condition": cond,
            "multi_rogue_count": meta.get("multi_rogue_count"),
            "companion_video_mbps": meta.get("companion_video_mbps"),
            "gnb_prb": stamp.get("GNB_PRB"),
            "min_ratio_slice_b": stamp.get("MIN_RATIO_SLICE_B"),
            "mitigation_aqm": stamp.get("MITIGATION_AQM"),
            "mitigations": {k: v for k, v in (meta.get("mitigations") or {}).items() if k != "runtime_tc_audit"},
            "c2_5qi": stamp.get("C2_5QI"),
        },
        "flags": {
            "load1_max": load_max,
            "load_over_limit": (load_max is not None and load_max > LOAD_LIMIT),
            "load_gate_enforced": cond == "baseline",
            "paper_comparable": get(fl, "physics.trajectory_acquisition.paper_comparable"),
        },
    }


def failed_gates(detail: dict) -> list[str]:
    out = []
    for g in (detail or {}).get("failed_gates") or []:
        out.append(g.get("gate") if isinstance(g, dict) else str(g))
    return out


def main() -> int:
    runs, sources = [], []
    for blog in sorted(CAMPAIGN.glob("*/*/batch_log.json")):
        night, series = blog.parent.parent.name, blog.parent.name
        if night in EXCLUDED_ATTEMPTS:
            continue
        sources.append(str(blog.relative_to(ARTIFACTS)))
        b = load_json(blog)
        for r in b.get("runs", []):
            cid = re.sub(r"_r\d+$", "", r["id"])
            entry = {"run_id": r["id"], "condition": cid, "night": night, "series": series,
                     "seed": b.get("seed"), "run_dir": None, "category": None, "exclusion_reason": None}
            rd = resolve_run_dir(r.get("run_dir"))
            if rd is not None and rd.is_dir():
                entry["run_dir"] = str(rd.resolve().relative_to(ARTIFACTS))
                entry.update(run_meta_summary(rd))
            else:
                entry["valid"] = r.get("valid")
            exp = expected_invalid_category(cid)
            if r.get("valid") is True and exp is None:
                entry["category"] = "campaign"
            elif exp is not None and entry["run_dir"]:
                # Both outcomes land here: these conditions are read by their own evidence (goodput for the
                # capacity probe, the injection record for the control), never by the VALID verdict.
                entry["category"] = exp
                entry["note"] = EXPECTED_INVALID_NOTE[exp]
            else:
                entry["category"] = "excluded"
                gates = failed_gates(r.get("detail"))
                entry["exclusion_reason"] = ("invalid run; failed gates: " + ", ".join(gates)) if gates else \
                    f"invalid run (rc={r.get('rc')})"
            runs.append(entry)
    for m in MANUAL_RUNS:
        for rd in sorted(CAMPAIGN.glob(m["glob"])):
            if not rd.is_dir():
                continue
            entry = {"run_id": m["run_id"], "condition": m["condition"], "night": "manual", "series": m["series"],
                     "seed": None, "run_dir": str(rd.resolve().relative_to(ARTIFACTS)), "note": m["note"],
                     "exclusion_reason": None}
            entry.update(run_meta_summary(rd))
            entry["category"] = "campaign" if entry.get("valid") is True else "excluded"
            if entry["category"] == "excluded":
                entry["exclusion_reason"] = "manual run is not VALID"
            runs.append(entry)
    # cross-reference: an excluded run whose condition has a manual replacement
    replaced = {m["condition"] for m in MANUAL_RUNS}
    for e in runs:
        if e["category"] == "excluded" and e["condition"] in replaced and e["night"] != "manual":
            e["exclusion_reason"] += "; replaced by the manual rerun of the same condition"
    for e in runs:
        if e["condition"] in EXCLUDED_CONDITIONS and e["category"] == "campaign":
            e["category"] = "excluded"
            e["exclusion_reason"] = EXCLUDED_CONDITIONS[e["condition"]]
    by_id = {e["run_id"]: e for e in runs}
    for old_id, rep in REPLACEMENTS.items():
        orig, new_run = by_id.get(old_id), by_id.get(rep["by"])
        ok = (orig is not None and new_run is not None and new_run["category"] == "campaign"
              and get(new_run, "flags.load_over_limit") is False and get(new_run, "flags.paper_comparable") is True)
        if not ok:
            print(f"[manifest] WARNING: replacement {old_id} -> {rep['by']} not applied (replacement not impeccable or missing)")
            continue
        orig["category"] = "excluded"
        orig["exclusion_reason"] = rep["reason"]
        orig["replaced_by"] = rep["by"]
        new_run["replaces"] = old_id
    runs.sort(key=lambda e: (condition_order(e["condition"]), e["run_id"]))

    summary = {}
    for e in runs:
        s = summary.setdefault(e["condition"], {c: 0 for c in CATEGORIES})
        s[e["category"]] += 1
    unknown = sorted({e["condition"] for e in runs} - set(CONDITIONS))
    manifest = {
        "schema": "flyblind_campaign_manifest_v1",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "excluded_attempts": EXCLUDED_ATTEMPTS,
        "excluded_conditions": EXCLUDED_CONDITIONS,
        "load_limit": LOAD_LIMIT,
        "summary_by_condition": summary,
        "unknown_conditions": unknown,
        "runs": runs,
    }
    write_json(MANIFEST, manifest)
    rows = [{"run_id": e["run_id"], "condition": e["condition"], "category": e["category"], "night": e["night"],
             "series": e["series"], "valid": e.get("valid"), "load1_max": get(e, "flags.load1_max"),
             "load_over_limit": get(e, "flags.load_over_limit"), "load_gate_enforced": get(e, "flags.load_gate_enforced"),
             "exclusion_reason": e.get("exclusion_reason") or "", "run_dir": e.get("run_dir") or ""} for e in runs]
    write_csv(OUT / "campaign_manifest.csv", rows, list(rows[0].keys()) if rows else ["run_id"])
    n = {c: sum(1 for e in runs if e["category"] == c) for c in CATEGORIES}
    print(f"[manifest] {len(runs)} runs from {len(sources)} batch logs + manual: {n}")
    if unknown:
        print(f"[manifest] WARNING: conditions without metadata in common.CONDITIONS: {unknown}")
    print(f"[manifest] -> {MANIFEST.relative_to(ARTIFACTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
##################################################################################################################################
# Module: batch_wrapper.py (Batch wrapper: runs a list of conditions through orchestrate.py)                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Takes the list of conditions of a config, shuffles the order, calls orchestrate and STOPS at the first invalid    #
#   run (run_meta.valid == false). With --continue-on-invalid (run-all) it records the invalid run and keeps going.              #
#                                                                                                                                #
# Validity criteria = the gates in orchestrate's enforced_validity_gates, read from run_meta.json after each run (fields         #
#   gates.enforced + gates.valid). Gates are not re-implemented here.                                                            #
#                                                                                                                                #
# --dry-run mode: prints the shuffled order and the commands without calling orchestrate.                                        #
##################################################################################################################################
"""Batch wrapper: runs a list of conditions through orchestrate.py.

Takes the list of conditions of a config, shuffles the order, calls
orchestrate and STOPS at the first invalid run (run_meta.valid == false).
With --continue-on-invalid (run-all) it records the invalid run and keeps going.

Validity criteria = the gates in orchestrate's enforced_validity_gates,
read from run_meta.json after each run (fields gates.enforced + gates.valid).
Gates are not re-implemented here.

--dry-run mode: prints the shuffled order and the commands without calling
orchestrate.
"""
from __future__ import annotations

import argparse
import json
import random
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
ORCH_PAPER = EXP / "orchestrate.py"

# Documentation mirror of the gates in enforced_validity_gates(orchestrate.py).
# The invalid decision always comes from run_meta["valid"].
VALIDITY_GATES_DOC = {
    "always": [
        "clock",
        "host_instrumentation_integrity",
        "mission_integrity",
        "drone_integrity",
    ],
    "baseline_extra": [
        "host",
        "availability",
        "mission_success",
        "baseline_drone_quality",
    ],
    "attack_extra": ["attack_integrity"],
    "if_c2_proxy": ["proxy"],
    "if_setpoint_stream": ["freshness", "stream"],
    "if_setpoint_stream_and_baseline": ["c2_health"],
    "if_require_prb": ["prb"],
    "source": "orchestrate.enforced_validity_gates / run_meta.gates.valid",
}


def _load_conditions(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "conditions" in raw:
        conds = raw["conditions"]
    elif isinstance(raw, list):
        conds = raw
    else:
        raise SystemExit("conditions file must be a list or {conditions: [...]}")
    out = []
    for i, c in enumerate(conds):
        if isinstance(c, str):
            out.append({"id": c, "argv": c.split()})
        elif isinstance(c, dict):
            if "argv" not in c and "args" not in c:
                raise SystemExit(f"condition[{i}] missing argv/args")
            argv = c.get("argv") or c.get("args")
            if isinstance(argv, str):
                argv = shlex.split(argv)
            out.append({
                "id": c.get("id") or f"cond_{i+1}",
                "argv": list(argv),
                "note": c.get("note"),
            })
        else:
            raise SystemExit(f"condition[{i}] unsupported type")
    return out


def _orchestrate_bin(which: str) -> Path:
    # There is a single orchestrator: orchestrate_mitigation.py was merged into orchestrate.py, and the
    # mitigation testbeds are selected by the start script, not by a separate driver. The argument is
    # kept so existing --orchestrate values ("paper", "mitigation") stay accepted.
    del which
    return ORCH_PAPER


def _read_validity(run_dir: Path) -> tuple[bool | None, dict[str, Any]]:
    meta_path = run_dir / "run_meta.json"
    if not meta_path.is_file():
        return None, {"error": "run_meta.json missing", "run_dir": str(run_dir)}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    valid = meta.get("valid")
    gates = meta.get("gates") or {}
    failed = []
    for g in gates.get("enforced") or []:
        block = gates.get(g) or {}
        if block.get("pass") is False:
            failed.append({
                "gate": g,
                "reason": block.get("reason") or block.get("info") or block,
            })
    return bool(valid), {
        "valid": valid,
        "enforced": gates.get("enforced"),
        "failed_gates": failed,
        "run_id": meta.get("run_id"),
        "run_dir": str(run_dir),
    }


def _guess_run_dir(out_root: Path, before: set[Path]) -> Path | None:
    after = {p for p in out_root.iterdir() if p.is_dir()} if out_root.is_dir() else set()
    new = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
    return new[0] if new else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Batch wrapper 1.9 (random order, stop on invalid)")
    ap.add_argument("--conditions", type=Path, default=None,
                    help="JSON list of conditions (argv per condition)")
    ap.add_argument("--orchestrate", choices=["paper", "mitigation"], default="paper")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for shuffle")
    ap.add_argument("--out-root", type=Path, default=None,
                    help="Campaign out root (passed as --out to orchestrate if set)")
    ap.add_argument("--extra-arg", action="append", default=[],
                    help="Extra argv prepended to every condition (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--continue-on-invalid", action="store_true",
                    help="Run-all: record invalid runs and keep going (default: stop, R1)")
    ap.add_argument("--list-validity-gates", action="store_true",
                    help="Print documented validity gates and exit")
    ap.add_argument("--batch-log", type=Path, default=None,
                    help="Write shuffle order + discard reasons JSON here")
    args = ap.parse_args(argv)

    if args.list_validity_gates:
        print(json.dumps(VALIDITY_GATES_DOC, indent=2, ensure_ascii=False))
        return 0

    if args.conditions is None:
        raise SystemExit("--conditions is required (except with --list-validity-gates)")
    conds = _load_conditions(args.conditions)
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)
    order = list(range(len(conds)))
    rng.shuffle(order)

    orch = _orchestrate_bin(args.orchestrate)
    extra = []
    for e in args.extra_arg:
        extra.extend(shlex.split(e))

    planned = []
    for idx in order:
        c = conds[idx]
        cmd = [sys.executable, str(orch)] + extra + list(c["argv"])
        if args.out_root is not None:
            cmd += ["--out", str(args.out_root)]
        planned.append({
            "order": len(planned) + 1,
            "condition_index": idx,
            "id": c["id"],
            "cmd": cmd,
            "cmd_str": " ".join(shlex.quote(x) for x in cmd),
        })

    batch_log: dict[str, Any] = {
        "schema": "flyblind_batch_wrapper_v1",
        "seed": seed,
        "orchestrate": args.orchestrate,
        "orchestrate_path": str(orch),
        "validity_gates_doc": VALIDITY_GATES_DOC,
        "order": [{"order": p["order"], "id": p["id"], "condition_index": p["condition_index"]}
                  for p in planned],
        "runs": [],
        "stopped_on_invalid": False,
        "continue_on_invalid": bool(args.continue_on_invalid),
        "invalid_runs": [],
        "dry_run": bool(args.dry_run),
    }

    print(
        f"[batch] seed={seed} n={len(planned)} orchestrate={args.orchestrate} "
        f"on_invalid={'continue' if args.continue_on_invalid else 'stop'}",
        flush=True,
    )
    for p in planned:
        print(f"[batch] #{p['order']} {p['id']}: {p['cmd_str']}", flush=True)

    if args.dry_run:
        if args.batch_log:
            args.batch_log.parent.mkdir(parents=True, exist_ok=True)
            args.batch_log.write_text(json.dumps(batch_log, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"dry_run": True, "order": batch_log["order"]}, indent=2))
        return 0

    out_root = args.out_root
    if out_root is not None:
        out_root.mkdir(parents=True, exist_ok=True)

    for p in planned:
        before = set(out_root.iterdir()) if out_root and out_root.is_dir() else set()
        print(f"[batch] running #{p['order']} {p['id']}…", flush=True)
        rc = subprocess.call(p["cmd"])
        run_dir = _guess_run_dir(out_root, before) if out_root else None
        valid = None
        detail: dict[str, Any] = {"rc": rc}
        if run_dir is not None:
            valid, detail = _read_validity(run_dir)
            detail["rc"] = rc
        entry = {
            "order": p["order"],
            "id": p["id"],
            "cmd": p["cmd"],
            "rc": rc,
            "run_dir": str(run_dir) if run_dir else None,
            "valid": valid,
            "detail": detail,
        }
        if valid is False:
            entry["discard_reason"] = {
                "rule": "R1",
                "summary": "run_meta.valid == false",
                "failed_gates": detail.get("failed_gates"),
            }
        elif rc != 0 and valid is not True:
            entry["discard_reason"] = {
                "rule": "R1",
                "summary": f"orchestrate rc={rc} and validity not confirmed",
            }
        batch_log["runs"].append(entry)
        if "discard_reason" not in entry:
            continue
        batch_log["invalid_runs"].append({"order": p["order"], "id": p["id"],
                                          "run_dir": entry["run_dir"]})
        if args.continue_on_invalid:
            print(
                f"[batch] INVALID (run-all, continuing) id={p['id']} "
                f"reason={entry['discard_reason']['summary']} "
                f"gates={detail.get('failed_gates')}",
                flush=True,
            )
            continue
        batch_log["stopped_on_invalid"] = True
        batch_log["stopped_at"] = entry
        print(
            f"[batch] STOP: id={p['id']} reason={entry['discard_reason']['summary']} "
            f"gates={detail.get('failed_gates')}",
            flush=True,
        )
        break

    if args.batch_log:
        args.batch_log.parent.mkdir(parents=True, exist_ok=True)
        args.batch_log.write_text(json.dumps(batch_log, indent=2) + "\n", encoding="utf-8")
        print(f"[batch] log → {args.batch_log}", flush=True)

    if batch_log["invalid_runs"]:
        print(f"[batch] invalid: {len(batch_log['invalid_runs'])}/{len(batch_log['runs'])}",
              flush=True)
    return 1 if batch_log["invalid_runs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

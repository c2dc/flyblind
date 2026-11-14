#!/usr/bin/env python3
##################################################################################################################################
# Module: upf_telemetry.py (UPF CPU soft-saturation telemetry)                                                                   #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Soft attribution probe when UPF CPU is busy; does not veto VALID automatically.                                   #
##################################################################################################################################

"""UPF CPU sampler for FlyBlind — shared-UPF vs RAN mechanism attribution.

Samples oai-upf container CPU via cgroup usage deltas (works without metrics-server).
Writes timeseries CSV + summary JSON for each orchestrate run (baseline and attack).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path


def _kubectl(ctx: str, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    cmd = ["kubectl", "--context", ctx, *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )


def read_upf_usage_nsec(ctx: str, core_ns: str, deploy: str, container: str) -> int | None:
    """Return cumulative CPU usage in nanoseconds, or None on failure."""
    script = (
        "if [ -f /sys/fs/cgroup/cpu.stat ]; then "
        "  grep -E '^usage_usec ' /sys/fs/cgroup/cpu.stat | awk '{print $2 * 1000}'; "
        "elif [ -f /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then "
        "  cat /sys/fs/cgroup/cpuacct/cpuacct.usage; "
        "elif [ -f /sys/fs/cgroup/cpu/cpuacct.usage ]; then "
        "  cat /sys/fs/cgroup/cpu/cpuacct.usage; "
        "else echo MISSING; fi"
    )
    r = _kubectl(
        ctx, "exec", "-n", core_ns, f"deploy/{deploy}", "-c", container, "--",
        "sh", "-c", script,
    )
    if r.returncode != 0:
        return None
    line = (r.stdout or "").strip().splitlines()
    if not line or line[-1] == "MISSING":
        return None
    try:
        return int(re.sub(r"\D", "", line[-1]) or line[-1])
    except ValueError:
        try:
            return int(float(line[-1]))
        except ValueError:
            return None


def pctile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> int:
    ap = argparse.ArgumentParser(description="FlyBlind UPF CPU telemetry (cgroup)")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--context", required=True)
    ap.add_argument("--core-namespace", default="core")
    ap.add_argument("--deploy", default="oai-upf")
    ap.add_argument("--container", default="upf")
    ap.add_argument("--out-csv", type=Path, required=True)
    ap.add_argument("--out-summary", type=Path, default=None)
    ap.add_argument(
        "--saturate-pct", type=float, default=85.0,
        help="soft threshold: cpu_pct_max above this → saturated=true (does not exit non-zero)",
    )
    args = ap.parse_args()

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    prev_usage: int | None = None
    prev_mono = time.monotonic()
    t0 = time.monotonic()
    t_end = t0 + args.duration
    sample_errors = 0

    # Prime reading (no pct yet).
    prev_usage = read_upf_usage_nsec(
        args.context, args.core_namespace, args.deploy, args.container,
    )
    prev_mono = time.monotonic()
    if prev_usage is None:
        sample_errors += 1

    while time.monotonic() < t_end:
        time.sleep(max(0.05, args.interval))
        now = time.monotonic()
        usage = read_upf_usage_nsec(
            args.context, args.core_namespace, args.deploy, args.container,
        )
        row: dict = {
            "t_mono_ns": time.monotonic_ns(),
            "t_rel_s": round(now - t0, 3),
        }
        if usage is None or prev_usage is None:
            sample_errors += 1
            row["cpu_pct"] = None
            row["usage_nsec"] = usage
            row["ok"] = 0
        else:
            dt = max(1e-6, now - prev_mono)
            # usage is ns of CPU time; / wall_ns → fraction of one core (≥100% if multi-threaded).
            cpu_pct = 100.0 * ((usage - prev_usage) / (dt * 1e9))
            if cpu_pct < 0:
                # cgroup reset / pod restart mid-run
                cpu_pct = 0.0
            row["cpu_pct"] = round(cpu_pct, 2)
            row["usage_nsec"] = usage
            row["ok"] = 1
            prev_usage = usage
            prev_mono = now
        rows.append(row)

    cpu_vals = [float(r["cpu_pct"]) for r in rows if r.get("cpu_pct") is not None]
    saturated = bool(cpu_vals) and max(cpu_vals) >= float(args.saturate_pct)
    summary = {
        "schema_version": "upf_telemetry_v1",
        "deploy": args.deploy,
        "core_namespace": args.core_namespace,
        "samples": len(rows),
        "samples_ok": sum(1 for r in rows if r.get("ok") == 1),
        "sample_errors": sample_errors,
        "cpu_pct_max": max(cpu_vals) if cpu_vals else None,
        "cpu_pct_p50": pctile(cpu_vals, 50),
        "cpu_pct_p95": pctile(cpu_vals, 95),
        "cpu_pct_p99": pctile(cpu_vals, 99),
        "cpu_pct_mean": round(statistics.fmean(cpu_vals), 3) if cpu_vals else None,
        "saturate_pct_threshold": float(args.saturate_pct),
        "saturated": saturated,
        "mechanism_hint": (
            "upf_busy_ambiguous" if saturated else
            ("upf_headroom_ok" if cpu_vals else "upf_unsampled")
        ),
    }

    fields = ["t_mono_ns", "t_rel_s", "cpu_pct", "usage_nsec", "ok"]
    with args.out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    out_sum = args.out_summary or args.out_csv.with_name("upf_summary.json")
    out_sum.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    # Soft probe: always exit 0 if we got ≥1 OK sample; else 1 (instrumentation gap).
    return 0 if summary["samples_ok"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

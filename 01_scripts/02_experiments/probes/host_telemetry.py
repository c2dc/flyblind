#!/usr/bin/env python3
##################################################################################################################################
# Module: host_telemetry.py (host loadavg telemetry during runs)                                                                 #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Samples loadavg1 on Kind node host context for measurement-integrity gates.                                       #
##################################################################################################################################

"""Host telemetry sampler for FlyBlind experiments.

v2 adds the signals needed to *rule out* the host as a confounder for
jitter-sensitive C2 metrics: PSI stall pressure (the only direct measure of
runnable-but-waiting time), iowait/steal, available memory/swap and the
sampler's own scheduling lag.  A run whose IPDV moves with ``psi_cpu_some_avg10``
is a host artefact, not a RAN effect.

CSV columns and summary keys from v1 are preserved (additive change).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

PSI_PATHS = {
    "psi_cpu": "/proc/pressure/cpu",
    "psi_io": "/proc/pressure/io",
    "psi_mem": "/proc/pressure/memory",
}


def read_proc_stat() -> dict[str, float]:
    with open("/proc/stat", encoding="utf-8") as f:
        line = f.readline()
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return {}
    vals = list(map(int, parts[1:]))

    def at(i: int) -> int:
        return vals[i] if len(vals) > i else 0

    idle = at(3) + at(4)
    return {
        "cpu_total": float(sum(vals)),
        "cpu_idle": float(idle),
        "cpu_iowait_raw": float(at(4)),
        "cpu_steal_raw": float(at(7)),
    }


def read_meminfo() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemAvailable", "SwapFree", "SwapTotal"):
                    try:
                        out[key] = float(rest.split()[0]) / 1024.0  # MiB
                    except (IndexError, ValueError):
                        pass
    except OSError:
        return {}
    swap_used = None
    if "SwapTotal" in out and "SwapFree" in out:
        swap_used = out["SwapTotal"] - out["SwapFree"]
    return {
        "mem_available_mib": round(out.get("MemAvailable", 0.0), 1),
        "swap_used_mib": round(swap_used, 1) if swap_used is not None else None,
    }


def read_pressure() -> dict[str, float]:
    """PSI ``some avg10`` per resource — percent of time at least one task stalled."""
    out: dict[str, float] = {}
    for name, path in PSI_PATHS.items():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.startswith("some"):
                        continue
                    for tok in line.split():
                        if tok.startswith("avg10="):
                            out[f"{name}_some_avg10"] = float(tok.split("=", 1)[1])
                    break
        except (OSError, ValueError):
            continue
    return out


def sample_once(prev: dict[str, float]) -> dict[str, float]:
    la = open("/proc/loadavg", encoding="utf-8").read().split()
    cur = read_proc_stat()
    row = {
        "loadavg1": float(la[0]),
        "loadavg5": float(la[1]),
        "loadavg15": float(la[2]),
    }
    if prev and cur:
        dt = cur["cpu_total"] - prev.get("cpu_total", 0)
        didle = cur["cpu_idle"] - prev.get("cpu_idle", 0)
        if dt > 0:
            row["cpu_busy_pct"] = round(100.0 * (1.0 - didle / dt), 2)
            row["cpu_iowait_pct"] = round(
                100.0 * (cur["cpu_iowait_raw"] - prev.get("cpu_iowait_raw", 0)) / dt, 2,
            )
            row["cpu_steal_pct"] = round(
                100.0 * (cur["cpu_steal_raw"] - prev.get("cpu_steal_raw", 0)) / dt, 2,
            )
    row.update(cur)
    row.update(read_meminfo())
    row.update(read_pressure())
    return row


def _max(rows: list[dict], key: str):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return max(vals) if vals else None


def _median(rows: list[dict], key: str):
    vals = sorted(r[key] for r in rows if r.get(key) is not None)
    return vals[len(vals) // 2] if vals else None


def main() -> int:
    ap = argparse.ArgumentParser(description="FlyBlind host telemetry")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--out-csv", type=Path, required=True)
    ap.add_argument("--out-summary", type=Path, default=None)
    args = ap.parse_args()

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    prev = read_proc_stat()
    t_start = time.monotonic()
    t_end = t_start + args.duration
    # Absolute deadlines keep the sample grid from drifting when the host is busy;
    # the drift itself is recorded as sampler_lag_s (a host-load signal of its own).
    n = 0
    while True:
        n += 1
        target = t_start + n * args.interval
        if target > t_end:
            break
        now = time.monotonic()
        if target > now:
            time.sleep(target - now)
        cur = read_proc_stat()
        row = sample_once(prev)
        woke = time.monotonic()
        row["t_mono_ns"] = time.monotonic_ns()
        row["t_rel_s"] = round(woke - t_start, 3)
        row["sampler_lag_s"] = round(woke - target, 3)
        rows.append(row)
        prev = cur

    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    if not fields:
        fields = ["t_mono_ns", "t_rel_s", "loadavg1"]
    with args.out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows(rows)

    gaps = [
        round(b["t_rel_s"] - a["t_rel_s"], 3) for a, b in zip(rows, rows[1:])
    ]
    summary = {
        "schema_version": "host_telemetry_v2",
        "samples": len(rows),
        "interval_s": args.interval,
        "loadavg1_max": _max(rows, "loadavg1"),
        "loadavg1_median": _median(rows, "loadavg1"),
        "cpu_busy_pct_max": _max(rows, "cpu_busy_pct") or 0,
        "cpu_busy_pct_median": _median(rows, "cpu_busy_pct"),
        "cpu_iowait_pct_max": _max(rows, "cpu_iowait_pct"),
        "cpu_steal_pct_max": _max(rows, "cpu_steal_pct"),
        "psi_cpu_some_avg10_max": _max(rows, "psi_cpu_some_avg10"),
        "psi_io_some_avg10_max": _max(rows, "psi_io_some_avg10"),
        "psi_mem_some_avg10_max": _max(rows, "psi_mem_some_avg10"),
        "mem_available_mib_min": min(
            (r["mem_available_mib"] for r in rows if r.get("mem_available_mib")),
            default=None,
        ),
        "swap_used_mib_max": _max(rows, "swap_used_mib"),
        "sampler_lag_s_max": _max(rows, "sampler_lag_s"),
        "sample_gap_s_max": max(gaps) if gaps else None,
    }
    out_sum = args.out_summary or args.out_csv.with_name("host_summary.json")
    out_sum.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

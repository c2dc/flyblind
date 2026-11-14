#!/usr/bin/env python3
##################################################################################################################################
# Module: host_load_sampler.py (Light host load sampler (no kubectl))                                                            #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Reads /proc/loadavg and optional /proc/stat for idle%%; for comparing NI runs with vs without sidecars. Writes    #
#   JSONL + summary.                                                                                                             #
##################################################################################################################################
"""Light host load sampler (no kubectl).

Reads /proc/loadavg and optional /proc/stat for idle%%; for comparing NI
runs with vs without sidecars. Writes JSONL + summary.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def read_load():
    with open("/proc/loadavg", encoding="utf-8") as f:
        parts = f.read().split()
    return {
        "t_unix_ns": time.time_ns(),
        "load1": float(parts[0]),
        "load5": float(parts[1]),
        "load15": float(parts[2]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Host loadavg sampler")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--interval-s", type=float, default=2.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary-out", default="")
    args = ap.parse_args()

    rows = []
    end = time.monotonic() + args.duration
    with open(args.out, "w", encoding="utf-8") as f:
        while time.monotonic() < end:
            s = read_load()
            rows.append(s)
            f.write(json.dumps(s) + "\n")
            f.flush()
            time.sleep(args.interval_s)

    loads = [r["load1"] for r in rows]
    summary = {
        "schema": "flyblind_host_load_v1",
        "n_samples": len(loads),
        "load1_p50": sorted(loads)[len(loads) // 2] if loads else None,
        "load1_max": max(loads) if loads else None,
        "load1_mean": (sum(loads) / len(loads)) if loads else None,
    }
    if args.summary_out:
        Path(args.summary_out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

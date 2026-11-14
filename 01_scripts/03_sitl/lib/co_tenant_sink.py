#!/usr/bin/env python3
##################################################################################################################################
# Module: co_tenant_sink.py (UDP sink on ext-DN for co-tenant / pure-PRB validation)                                             #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Runs in oai-ext-dn pod; counts bytes from rogue DL bursts. JSON summary on stdout for orchestrate integrity gates.#
#                                                                                                                                #
# Usage: python3 co_tenant_sink.py --port 5202 --duration 60                                                                     #
##################################################################################################################################

"""UDP receive sink on ext-DN — validates co-tenant / pure-PRB DL traffic.

Deployment
----------
Runs inside the ``oai-ext-dn`` pod. Rogue UEs on slice A send DL bursts here
(typically :5202 for co-tenant load, :5201 for iperf smoke). ``orchestrate.py``
starts this process per run when receiver-side byte accounting is needed.

Contract
--------
Stdout: one JSON object with ``achieved_receiver_mbps`` (same convention as the
rogue-side attack tools). Optional ``--out-summary`` / ``--out-timeseries`` write
the same fields to disk for post-run plots.

Threat model: after L3 separation this sink is the *intended* DL destination —
rogue traffic never targets the victim C2 socket.
"""
from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
import time

# How often we snapshot cumulative counters (100 ms keeps CSV small at ~20k pps).
SAMPLE_INTERVAL_S = 0.1
# recvfrom timeout so the main loop can honour --duration without blocking forever.
RECV_TIMEOUT_S = 0.25


def run_sink(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.listen, args.port))
    sock.settimeout(RECV_TIMEOUT_S)

    t_start = time.monotonic()
    t_end = t_start + args.duration if args.duration > 0 else None
    bytes_recv = 0
    packets_recv = 0
    ts_rows: list[dict] = []
    last_log = t_start

    while True:
        if t_end is not None and time.monotonic() >= t_end:
            break
        try:
            data, _addr = sock.recvfrom(65535)
        except TimeoutError:
            continue
        except OSError:
            break

        packets_recv += 1
        bytes_recv += len(data)
        now = time.monotonic()
        if now - last_log >= SAMPLE_INTERVAL_S:
            ts_rows.append({
                "t_mono_ns": time.monotonic_ns(),
                "packets_received": packets_recv,
                "bytes_received": bytes_recv,
            })
            last_log = now

    duration_actual = time.monotonic() - t_start
    achieved = (
        (bytes_recv * 8.0) / duration_actual / 1e6 if duration_actual > 0 else 0.0
    )
    summary = {
        "mode": "udp_sink",
        "run_id": args.run_id or None,
        "listen": args.listen,
        "port": args.port,
        "duration_actual_s": round(duration_actual, 3),
        "bytes_received": bytes_recv,
        "packets_received": packets_recv,
        "achieved_receiver_mbps": round(achieved, 3),
    }
    print(json.dumps(summary))

    if args.out_summary:
        with open(args.out_summary, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")
    if args.out_timeseries:
        fields = ["t_mono_ns", "packets_received", "bytes_received"]
        with open(args.out_timeseries, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(ts_rows)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FlyBlind co-tenant UDP sink (ext-DN)")
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5202)
    ap.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="seconds; 0 is treated as ~infinite (orchestrator always passes explicit dur)",
    )
    ap.add_argument("--run-id", default="")
    ap.add_argument("--out-summary", default="")
    ap.add_argument("--out-timeseries", default="")
    args = ap.parse_args()
    if args.duration <= 0:
        args.duration = 1e9
    return run_sink(args)


if __name__ == "__main__":
    sys.exit(main())

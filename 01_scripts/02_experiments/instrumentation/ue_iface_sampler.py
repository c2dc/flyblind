#!/usr/bin/env python3
##################################################################################################################################
# Module: ue_iface_sampler.py (oaitun/qdisc sampler with a *single* in-pod process)                                              #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Avoids per-tick `kubectl exec` (CPU load on Kind host / rfsim). One exec runs a Python loop inside UAV nr-ue;     #
#   host only waits and pulls the JSONL at the end.                                                                              #
##################################################################################################################################
"""oaitun/qdisc sampler with a *single* in-pod process.

Avoids per-tick `kubectl exec` (CPU load on Kind host / rfsim). One exec runs a
Python loop inside UAV nr-ue; host only waits and pulls the JSONL at the end.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap


INPOD = textwrap.dedent(
    r"""
    import json, re, subprocess, time, sys
    duration = float(sys.argv[1])
    interval = float(sys.argv[2])
    out_path = sys.argv[3]
    def sh(cmd):
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL)
    def sample():
        try:
            tc = sh('tc -s qdisc show dev oaitun_ue1 2>/dev/null || tc -s qdisc show')
        except Exception:
            tc = ''
        try:
            ip = sh('ip -s link show oaitun_ue1')
        except Exception:
            ip = ''
        dropped = backlog_b = backlog_p = None
        for line in tc.splitlines():
            m = re.search(r'\(dropped\s+(\d+),', line)
            if m: dropped = int(m.group(1))
            m = re.search(r'backlog\s+(\d+)b\s+(\d+)p', line)
            if m:
                backlog_b = int(m.group(1)); backlog_p = int(m.group(2))
        tx_bytes = tx_drop = rx_drop = None
        lines = ip.splitlines()
        for i, line in enumerate(lines):
            if 'TX:' in line and i+1 < len(lines):
                p = lines[i+1].split()
                if len(p) >= 4:
                    tx_bytes = int(p[0]); tx_drop = int(p[3])
            if 'RX:' in line and i+1 < len(lines):
                p = lines[i+1].split()
                if len(p) >= 4:
                    rx_drop = int(p[3])
        return {
            't_unix_ns': time.time_ns(),
            'qdisc_dropped': dropped,
            'qdisc_backlog_bytes': backlog_b,
            'qdisc_backlog_packets': backlog_p,
            'oaitun_tx_bytes': tx_bytes,
            'oaitun_tx_dropped': tx_drop,
            'oaitun_rx_dropped': rx_drop,
        }
    end = time.monotonic() + duration
    rows = []
    with open(out_path, 'w') as f:
        while time.monotonic() < end:
            s = sample()
            rows.append(s)
            f.write(json.dumps(s) + '\n')
            f.flush()
            time.sleep(interval)
    drops = [s['qdisc_dropped'] for s in rows if s['qdisc_dropped'] is not None]
    summary = {
        'schema': 'flyblind_ue_iface_v1',
        'mode': 'in_pod_single_exec',
        'n_samples': len(rows),
        'qdisc_dropped_first': drops[0] if drops else None,
        'qdisc_dropped_last': drops[-1] if drops else None,
        'qdisc_dropped_delta': (drops[-1] - drops[0]) if len(drops) >= 2 else None,
        'max_backlog_packets': max((s['qdisc_backlog_packets'] or 0) for s in rows) if rows else None,
    }
    print(json.dumps(summary))
    """
)


def main() -> None:
    ap = argparse.ArgumentParser(description="oaitun/qdisc sampler (in-pod)")
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--interval-s", type=float, default=2.0, help="default 2s to reduce load")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--out", required=True, help="host path for JSONL")
    ap.add_argument("--summary-out", default="")
    args = ap.parse_args()

    pod_jsonl = "/tmp/flyblind_ue_iface.jsonl"
    # One long-lived exec
    cmd = [
        "kubectl", "--context", args.context, "exec", "-n", args.namespace,
        "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "python3", "-c", INPOD, str(args.duration), str(args.interval_s), pod_jsonl,
    ]
    print(f"[ue_iface] single in-pod sampler duration={args.duration}s interval={args.interval_s}s", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or "")
        sys.stderr.write(proc.stdout or "")
        sys.exit("in-pod sampler failed rc=%s" % proc.returncode)

    summary = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                summary = json.loads(line.strip())
                break
            except json.JSONDecodeError:
                continue
    if summary is None:
        summary = {"schema": "flyblind_ue_iface_v1", "ok": False, "reason": "no summary JSON"}

    # pull JSONL
    pull = subprocess.run(
        [
            "kubectl", "--context", args.context, "exec", "-n", args.namespace,
            "deploy/oai-ue-uav", "-c", "nr-ue", "--", "cat", pod_jsonl,
        ],
        capture_output=True,
        text=True,
    )
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(pull.stdout or "")
    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

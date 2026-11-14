#!/usr/bin/env python3
##################################################################################################################################
# Module: rlc_reject_scraper.py (Scrape RLC AM 'SDU rejected' from UAV nr-ue logs)                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: IMPORTANT: the softmodem logs at most once per second; each line carries N = sdu_rejected since the previous log. #
#   Always sum N — never count lines.                                                                                            #
#                                                                                                                                #
# Also verifies rate-limit empirically (median Δt between consecutive lines).                                                    #
#                                                                                                                                #
# Can: * follow live (`--follow --duration`) * parse a saved log file (`--log-file`) * one-shot dump of current kubectl logs     #
#   (`--once`)                                                                                                                   #
##################################################################################################################################
"""Scrape RLC AM 'SDU rejected' from UAV nr-ue logs.

IMPORTANT: the softmodem logs at most once per second; each line carries N =
sdu_rejected since the previous log. Always sum N — never count lines.

Also verifies rate-limit empirically (median Δt between consecutive lines).

Can:
  * follow live (`--follow --duration`)
  * parse a saved log file (`--log-file`)
  * one-shot dump of current kubectl logs (`--once`)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

RE_REJECT = re.compile(
    r"(?:^|\s)(?P<t>[0-9]+(?:\.[0-9]+)?)\s+\[RLC\].*?warning:\s+(?P<n>\d+)\s+SDU rejected,\s+SDU buffer full"
)
# softmodem sometimes prefixes with t= wall from kubectl --timestamps
RE_REJECT_LOOSE = re.compile(r"warning:\s+(?P<n>\d+)\s+SDU rejected,\s+SDU buffer full")
RE_TS = re.compile(r"(?P<t>[0-9]+(?:\.[0-9]+)?)\s+\[RLC\]")


def parse_lines(lines):
    events = []
    for line in lines:
        m = RE_REJECT.search(line)
        if m:
            events.append({"t_s": float(m.group("t")), "n": int(m.group("n")), "raw": line.rstrip()})
            continue
        m2 = RE_REJECT_LOOSE.search(line)
        if m2:
            mt = RE_TS.search(line)
            events.append({
                "t_s": float(mt.group("t")) if mt else None,
                "n": int(m2.group("n")),
                "raw": line.rstrip(),
            })
    return events


def summarize(events):
    ns = [e["n"] for e in events]
    ts = [e["t_s"] for e in events if e.get("t_s") is not None]
    dts = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
    med_dt = sorted(dts)[len(dts) // 2] if dts else None
    return {
        "schema": "flyblind_rlc_rejects_v1",
        "log_lines": len(events),
        "sdu_rejected_sum": int(sum(ns)) if ns else 0,
        "sdu_rejected_per_line_mean": (sum(ns) / len(ns)) if ns else None,
        "sdu_rejected_per_line_max": max(ns) if ns else None,
        "rate_limit_check": {
            "median_delta_t_s": med_dt,
            "expected_approx_s": 1.0,
            "consistent_with_1hz_cap": (med_dt is not None and 0.5 <= med_dt <= 2.5),
            "code_ref": "nr_rlc_entity_am.c: log at most once per 1000 ms",
        },
        "span_s": (ts[-1] - ts[0]) if len(ts) >= 2 else None,
        "note": "Count sdu_rejected_sum, not log_lines. Compare to companion_video lost_packets.",
    }


def kubectl_logs(ctx, ns, since=None, follow=False):
    cmd = [
        "kubectl", "--context", ctx, "logs", "-n", ns,
        "deploy/oai-ue-uav", "-c", "nr-ue",
    ]
    if since:
        cmd += [f"--since={since}"]
    if follow:
        cmd += ["--follow"]
    return cmd


def main() -> None:
    ap = argparse.ArgumentParser(description="RLC SDU-reject scraper (sum N)")
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--out", required=True)
    ap.add_argument("--events-out", default="", help="JSONL of each reject log line")
    ap.add_argument("--log-file", default="", help="parse saved log instead of kubectl")
    ap.add_argument("--once", action="store_true", help="dump current logs once")
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--duration", type=float, default=0.0, help="with --follow, stop after N seconds")
    ap.add_argument("--since", default="", help="kubectl --since= e.g. 15m")
    args = ap.parse_args()

    events = []
    if args.log_file:
        text = Path(args.log_file).read_text(errors="replace").splitlines()
        events = parse_lines(text)
    elif args.follow:
        cmd = kubectl_logs(args.context, args.namespace, since=args.since or None, follow=True)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        t_end = time.monotonic() + args.duration if args.duration > 0 else None
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                ev = parse_lines([line])
                events.extend(ev)
                if t_end is not None and time.monotonic() >= t_end:
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    else:
        cmd = kubectl_logs(args.context, args.namespace, since=args.since or None, follow=False)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(proc.stderr or "kubectl logs failed")
        events = parse_lines((proc.stdout or "").splitlines())

    summary = summarize(events)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    if args.events_out:
        with open(args.events_out, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

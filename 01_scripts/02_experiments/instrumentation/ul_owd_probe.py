#!/usr/bin/env python3
##################################################################################################################################
# Module: ul_owd_probe.py (UAV→GCS one-way delay probe (same DRB as telemetry/video))                                            #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Lightweight UDP: 64 B payload, 10 Hz, CLOCK_MONOTONIC timestamps on shared Kind time namespace (same assumption   #
#   as probes/owd_probe.py).                                                                                                     #
#                                                                                                                                #
# Direction is the attack-relevant UL leg (UAV PDU → GCS PDU). Does not touch MAVLink :5760. Port default 48001 (avoids NETWORK  #
#   OWD 47999).                                                                                                                  #
#                                                                                                                                #
# Payload: !QIH + pad  (tx_mono_ns, seq uint32, magic 0x554C)                                                                    #
##################################################################################################################################
"""UAV→GCS one-way delay probe (same DRB as telemetry/video).

Lightweight UDP: 64 B payload, 10 Hz, CLOCK_MONOTONIC timestamps on shared
Kind time namespace (same assumption as probes/owd_probe.py).

Direction is the attack-relevant UL leg (UAV PDU → GCS PDU). Does not touch
MAVLink :5760. Port default 48001 (avoids NETWORK OWD 47999).

Payload: !QIH + pad  (tx_mono_ns, seq uint32, magic 0x554C)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

PORT = 48001
MAGIC = 0x554C
PAYLOAD_FMT = "!QIH"  # 8+4+2 = 14; pad to 64
PAYLOAD_LEN = 64

_GCS_CORES = os.environ.get("TM_PIN_GCS", "")
_UAV_CORES = os.environ.get("TM_PIN_UAV", "")


def _pin(deploy: str) -> list[str]:
    c = _UAV_CORES if "uav" in deploy else (_GCS_CORES if "gcs" in deploy else "")
    return ["taskset", "-c", c] if c else []


def make_kexec(ctx: str, ns: str):
    def kexec(deploy: str, script: str, timeout: float | None = None):
        cmd = [
            "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/" + deploy,
            "-c", "nr-ue", "--",
        ] + _pin(deploy) + ["python3", "-c", script]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return kexec


def pct(sorted_vals, p: float):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def main() -> None:
    ap = argparse.ArgumentParser(description="UL OWD probe UAV→GCS")
    ap.add_argument("--gcs-ip", default="", help="default: auto-discover oaitun_ue1")
    ap.add_argument("--uav-ip", default="", help="default: auto-discover oaitun_ue1")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--rate-hz", type=float, default=10.0)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--out", required=True)
    ap.add_argument("--samples-out", default="", help="optional per-sample JSONL path on host")
    args = ap.parse_args()

    ctx, ns = args.context, args.namespace
    if not args.gcs_ip or not args.uav_ip:
        here = Path(__file__).resolve().parent
        r = subprocess.run(
            [sys.executable, str(here / "discover_pdu_ips.py"), "--context", ctx, "--namespace", ns],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            sys.exit("PDU discovery failed: %s" % (r.stderr or r.stdout))
        d = json.loads(r.stdout)
        args.gcs_ip = args.gcs_ip or d["gcs_ip"]
        args.uav_ip = args.uav_ip or d["uav_ip"]
    if not args.gcs_ip or not args.uav_ip:
        sys.exit("--gcs-ip/--uav-ip required (discovery failed)")

    kexec = make_kexec(ctx, ns)
    interval = 1.0 / args.rate_hz
    duration = float(args.duration)
    port = int(args.port)

    # Shared time namespace check (read-only)
    gcs_ns = kexec("oai-ue-gcs", "import os; print(os.readlink('/proc/self/ns/time'))")
    uav_ns = kexec("oai-ue-uav", "import os; print(os.readlink('/proc/self/ns/time'))")
    if gcs_ns.returncode or uav_ns.returncode:
        sys.exit("time ns read failed")
    time_match = (gcs_ns.stdout or "").strip() == (uav_ns.stdout or "").strip()

    recv_script = (
        "import socket, struct, time, json\n"
        "PORT, MAGIC, DUR = %d, %d, %d\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind(('%s', PORT))\n"
        "sock.settimeout(0.2)\n"
        "end = time.monotonic() + DUR + 3\n"
        "rows = []\n"
        "while time.monotonic() < end:\n"
        "    try:\n"
        "        data, addr = sock.recvfrom(128)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    if len(data) < 14:\n"
        "        continue\n"
        "    tx_ns, seq, magic = struct.unpack('!QIH', data[:14])\n"
        "    if magic != MAGIC:\n"
        "        continue\n"
        "    rx_ns = time.monotonic_ns()\n"
        "    rows.append({'seq': seq, 'tx_ns': tx_ns, 'rx_ns': rx_ns,\n"
        "                 'owd_us': (rx_ns - tx_ns) / 1000.0, 'src': addr[0]})\n"
        "open('/tmp/ul_owd_recv.json','w').write(json.dumps(rows))\n"
        "print(json.dumps({'n': len(rows)}))\n"
    ) % (port, MAGIC, int(math.ceil(duration)), args.gcs_ip)

    recv_proc = subprocess.Popen(
        [
            "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-gcs",
            "-c", "nr-ue", "--",
        ]
        + _pin("oai-ue-gcs")
        + ["python3", "-c", recv_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.5)

    send_script = (
        "import socket, struct, time, json\n"
        "DEST, PORT, MAGIC, DUR, IV = '%s', %d, %d, %f, %f\n"
        "PAD = b'\\x00' * (%d - 14)\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.bind(('%s', 0))\n"
        "end = time.monotonic() + DUR\n"
        "seq = 0\n"
        "while time.monotonic() < end:\n"
        "    tx = time.monotonic_ns()\n"
        "    sock.sendto(struct.pack('!QIH', tx, seq & 0xffffffff, MAGIC) + PAD, (DEST, PORT))\n"
        "    seq += 1\n"
        "    time.sleep(IV)\n"
        "print(json.dumps({'packets_sent': seq}))\n"
    ) % (args.gcs_ip, port, MAGIC, duration, interval, PAYLOAD_LEN, args.uav_ip)

    send_res = kexec("oai-ue-uav", send_script, timeout=duration + 60)
    if send_res.returncode != 0:
        recv_proc.kill()
        sys.exit("UL sender failed: %s" % (send_res.stderr or send_res.stdout))

    packets_sent = 0
    for line in reversed((send_res.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                packets_sent = int(json.loads(line).get("packets_sent", 0))
                break
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    try:
        recv_proc.wait(timeout=duration + 30)
    except subprocess.TimeoutExpired:
        recv_proc.kill()
        sys.exit("UL receiver timed out")

    pull = subprocess.run(
        [
            "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-gcs",
            "-c", "nr-ue", "--", "cat", "/tmp/ul_owd_recv.json",
        ],
        capture_output=True,
        text=True,
    )
    if pull.returncode != 0:
        sys.exit("pull recv json failed: %s" % (pull.stderr or pull.stdout))
    try:
        samples = json.loads(pull.stdout or "[]")
    except json.JSONDecodeError:
        sys.exit("invalid recv JSON")

    owds = sorted(float(s["owd_us"]) for s in samples if s.get("owd_us") is not None)
    seqs = [int(s["seq"]) for s in samples]
    lost = 0
    if packets_sent > 0 and seqs:
        expected = set(range(min(seqs), max(seqs) + 1))
        got = set(seqs)
        # also account for trailing not received
        lost = max(0, packets_sent - len(got))
        # sequence holes inside received range
        holes = len(expected - got)
        lost = max(lost, holes)
    elif packets_sent > 0:
        lost = packets_sent

    ipdv = []
    ordered = sorted(samples, key=lambda s: s["seq"])
    prev = None
    for s in ordered:
        if prev is not None:
            ipdv.append(abs(float(s["owd_us"]) - float(prev["owd_us"])))
        prev = s
    ipdv_s = sorted(ipdv)

    report = {
        "schema": "flyblind_ul_owd_v1",
        "direction": "UAV→GCS",
        "same_drb_as_telemetry": True,
        "note_qfi": "UDP on PDU IP uses default DRB of the PDU session (same as companion video / MAVLink UDP)",
        "port": port,
        "payload_bytes": PAYLOAD_LEN,
        "rate_hz": args.rate_hz,
        "duration_s": duration,
        "time_namespace_match": time_match,
        "packets_sent": packets_sent,
        "packets_received": len(samples),
        "loss_count_est": lost,
        "loss_ratio_est": round(lost / packets_sent, 6) if packets_sent else None,
        "owd_us": {
            "n": len(owds),
            "p50": pct(owds, 50),
            "p95": pct(owds, 95),
            "p99": pct(owds, 99),
            "max": owds[-1] if owds else None,
        },
        "jitter_ipdv_us": {
            "n": len(ipdv_s),
            "p50": pct(ipdv_s, 50),
            "p95": pct(ipdv_s, 95),
            "p99": pct(ipdv_s, 99),
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    if args.samples_out:
        with open(args.samples_out, "w", encoding="utf-8") as f:
            for s in samples:
                f.write(json.dumps(s) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

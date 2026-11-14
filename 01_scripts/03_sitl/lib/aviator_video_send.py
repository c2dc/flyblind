#!/usr/bin/env python3
##################################################################################################################################
# Module: aviator_video_send.py (realistic-shape UAV companion video sender: UAV pod -> GCS, UDP)                                #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Companion video with the packet-size shape of real UAV-RC traffic (AVIATOR / Baltaci INFOCOM 2021), paced to a    #
# modern cellular remote-piloting bitrate (Baltaci IMC 2022: full-HD H.264, 2-25 Mbps). Replaces the iperf3 CBR companion.       #
##################################################################################################################################

"""Realistic-shape companion video sender (UAV -> GCS over 5G UL).

Why
---
The iperf3 companion video is constant-bitrate with a single fixed packet size,
which a reviewer can fault as "not real video". This sender keeps the exact same
*role* (the victim's UL demand that the attack contends with) but gives the
traffic a realistic SHAPE: variable packet sizes drawn from the AVIATOR model of
commercial UAV-RC traffic (A. Baltaci et al., "Experimental UAV Data Traffic
Modeling...", IEEE INFOCOM 2021), paced to a modern cellular remote-piloting
bitrate (A. Baltaci et al., ACM IMC 2022: full-HD H.264 at 2-25 Mbps).

Size distribution (measured from the AVIATOR uplink/video model, 30k packets):
  ~86% full-MTU video packets (1515 B), ~10% mid-size, ~4% small control/telemetry.

The MEAN rate is what sets delta = Q/share, so the AoI result is preserved when
the mean matches the iperf run (default 12 Mb/s); only the micro-shape changes
(variable sizes + mild burstiness instead of CBR).

It is a drop-in for the iperf3 client: same UAV->GCS UDP path, same port, and it
writes an iperf3-COMPATIBLE /tmp/companion_video.json so the orchestrator's
parse_companion_video_summary() works unchanged.

Usage (inside the UAV pod)
--------------------------
    python3 aviator_video_send.py <gcs_ip> <port> <mbps> <duration_s> <uav_bind_ip> [out_json]
"""
from __future__ import annotations

import json
import random
import signal
import socket
import struct
import sys
import time

# AVIATOR uplink/video packet-size model (fractions, size-or-range in bytes).
# Derived from aviator.py (Baltaci et al., INFOCOM 2021), uplink mode, 30k packets.
SIZE_MODEL = [
    (0.861, 1515, 1515),   # full-MTU video packets
    (0.100, 400, 1400),    # mid-size video/mixed
    (0.039, 28, 400),      # small control/telemetry
]
HEADER = struct.Struct("!IQ")  # seq (uint32), send monotonic ns (uint64)
MIN_PKT = HEADER.size
BURST_CAP_S = 0.05             # token-bucket depth → mild burstiness, not CBR


def sample_size(rng: random.Random) -> int:
    u = rng.random()
    acc = 0.0
    for frac, lo, hi in SIZE_MODEL:
        acc += frac
        if u <= acc:
            return lo if lo == hi else rng.randint(lo, hi)
    return SIZE_MODEL[0][1]


def main() -> int:
    if len(sys.argv) < 6:
        sys.stderr.write(
            "usage: aviator_video_send.py <gcs_ip> <port> <mbps> <duration_s> <uav_bind_ip> [out_json]\n")
        return 2
    gcs_ip = sys.argv[1]
    port = int(sys.argv[2])
    mbps = float(sys.argv[3])
    duration_s = float(sys.argv[4])
    bind_ip = sys.argv[5]
    out_json = sys.argv[6] if len(sys.argv) > 6 else "/tmp/companion_video.json"

    rate_Bps = mbps * 1e6 / 8.0
    rng = random.Random(0xA71A)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    if bind_ip:
        s.bind((bind_ip, 0))

    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))

    sys.stderr.write(
        f"[AVIATOR-VIDEO] {bind_ip} -> {gcs_ip}:{port} UDP shape=aviator mean={mbps:g}Mbps for {duration_s:g}s\n")
    sys.stderr.flush()

    t0 = time.monotonic()
    t_end = t0 + duration_s
    last = t0
    budget = 0.0
    seq = 0
    bytes_sent = 0
    burst_cap_bytes = BURST_CAP_S * rate_Bps

    while not stop["now"]:
        now = time.monotonic()
        if now >= t_end:
            break
        budget += (now - last) * rate_Bps
        if budget > burst_cap_bytes:
            budget = burst_cap_bytes
        last = now
        size = max(MIN_PKT, sample_size(rng))
        if budget >= size:
            payload = HEADER.pack(seq & 0xFFFFFFFF, time.monotonic_ns()) + b"\x00" * (size - HEADER.size)
            try:
                s.sendto(payload, (gcs_ip, port))
                seq += 1
                bytes_sent += size
                budget -= size
            except OSError:
                time.sleep(0.001)
        else:
            # wait just enough for the next packet's worth of tokens
            time.sleep(min(0.002, max(0.0, (size - budget) / rate_Bps)))

    dur = max(1e-6, time.monotonic() - t0)
    achieved_bps = 8.0 * bytes_sent / dur
    # iperf3-compatible summary so parse_companion_video_summary() works unchanged.
    summary = {
        "start": {"test_start": {"protocol": "UDP"}},
        "end": {"sum": {
            "bits_per_second": achieved_bps,
            "bytes": bytes_sent,
            "packets": seq,
            "lost_percent": 0.0,
            "seconds": dur,
        }},
        "shape": "aviator",
    }
    try:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(summary, f)
    except OSError:
        pass
    sys.stderr.write(
        f"[AVIATOR-VIDEO] done pkts={seq} bytes={bytes_sent} achieved={achieved_bps/1e6:.3f}Mbps dur={dur:.1f}s\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

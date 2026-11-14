#!/usr/bin/env python3
##################################################################################################################################
# Module: qgc_mavlink_mirror.py (one-way MAVLink mirror: GCS pod -> QGroundControl on the host)                                  #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Passive copy of the post-5G MAVLink telemetry so a real GCS (QGroundControl) can display what the operator sees.  #
##################################################################################################################################

"""One-way MAVLink mirror for QGroundControl.

Why one-way
-----------
QGroundControl is not a neutral observer: on connect it requests telemetry
stream rates (and can rewrite ArduPilot ``SR*`` parameters), downloads the full
parameter set, and emits its own heartbeat. Any of those would change the
victim's uplink demand and therefore ``delta = Q / share`` — i.e. the AoI we
measure. See ardupilot.org/dev/docs/mavlink-requesting-data.html.

This mirror removes all three risks *structurally* instead of by configuration:
it only ever COPIES packets outward, so QGC has no path back to the vehicle. It
can render the operator's view but cannot perturb the experiment.

What it taps
------------
Incoming UDP on the GCS pod's PDU interface, from the UAV's PDU IP / C2 port —
i.e. telemetry that has already crossed the 5G uplink. The MAVLink payload is
re-emitted verbatim to the QGC endpoint on the host. Nothing is parsed, dropped
or rewritten, and the measured socket owned by setpoint_stream/mission_runner is
never touched (this is a separate raw socket; requires CAP_NET_RAW).

Usage (run inside the GCS pod)
------------------------------
    python3 qgc_mavlink_mirror.py <uav_pdu_ip> <c2_port> <qgc_host> <qgc_port>

e.g. python3 qgc_mavlink_mirror.py 12.1.1.133 5760 172.18.0.1 14550

Typically launched from the host without copying the file into the pod:
    kubectl exec -i -n ran deploy/oai-ue-gcs -c nr-ue -- \
        python3 - 12.1.1.133 5760 172.18.0.1 14550 < qgc_mavlink_mirror.py

Prints a one-line stats heartbeat to stderr every STATS_EVERY_S seconds so the
caller can confirm the mirror is alive without touching the data path.
"""
from __future__ import annotations

import os
import signal
import socket
import struct
import sys
import time

STATS_EVERY_S = 10.0
RECV_BUF = 65535


def main() -> int:
    if len(sys.argv) != 5:
        sys.stderr.write(
            "usage: qgc_mavlink_mirror.py <uav_pdu_ip> <c2_port> <qgc_host> <qgc_port>\n"
        )
        return 2
    uav_ip, c2_port, qgc_host, qgc_port = (
        sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]),
    )
    dest = (qgc_host, qgc_port)

    # Raw socket: receives inbound UDP (IP header included). Never sends to the vehicle.
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP)
    except PermissionError:
        sys.stderr.write("[mirror] FATAL: need CAP_NET_RAW in this pod\n")
        return 1
    rx.settimeout(2.0)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    stop = {"now": False}

    def _stop(_sig, _frm):
        stop["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    frames = 0
    bytes_out = 0
    t_last = time.monotonic()
    sys.stderr.write(
        f"[mirror] up: {uav_ip}:{c2_port} -> {qgc_host}:{qgc_port} (one-way, pid {os.getpid()})\n"
    )
    sys.stderr.flush()

    while not stop["now"]:
        try:
            pkt = rx.recv(RECV_BUF)
        except socket.timeout:
            pkt = b""
        except OSError:
            break
        if pkt and len(pkt) >= 28:
            ihl = (pkt[0] & 0x0F) * 4
            if len(pkt) >= ihl + 8:
                src = socket.inet_ntoa(pkt[12:16])
                sport, _dport, ulen, _ck = struct.unpack("!HHHH", pkt[ihl:ihl + 8])
                if src == uav_ip and sport == c2_port and ulen >= 8:
                    payload = pkt[ihl + 8: ihl + ulen]
                    if payload:
                        try:
                            tx.sendto(payload, dest)
                            frames += 1
                            bytes_out += len(payload)
                        except OSError:
                            pass  # QGC not listening yet: keep mirroring, never block
        now = time.monotonic()
        if now - t_last >= STATS_EVERY_S:
            sys.stderr.write(
                f"[mirror] frames={frames} bytes={bytes_out} "
                f"rate={bytes_out * 8 / max(1e-9, now - t_last) / 1e6:.3f} Mb/s\n"
            )
            sys.stderr.flush()
            frames = 0
            bytes_out = 0
            t_last = now

    sys.stderr.write("[mirror] stopped\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

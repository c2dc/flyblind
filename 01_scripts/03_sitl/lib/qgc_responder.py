#!/usr/bin/env python3
##################################################################################################################################
# Module: qgc_responder.py (host-side MAVLink shim for a one-way mirror)                                                         #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Purpose: QGroundControl, on connect, asks the vehicle for its parameter                                                        #
# list, GeoFence and Rally points. Our telemetry path to QGC is the one-way                                                      #
# mirror (qgc_mavlink_mirror.py): it only COPIES telemetry outward, so QGC's                                                     #
# requests reach nothing and time out —                                                                                          #
#   "Vehicle 1 did not respond to request for parameters"                                                                        #
#   "GeoFence transfer failed ... maximum retries exceeded"                                                                      #
#   "Rally Point transfer failed ... maximum retries exceeded"                                                                   #
#                                                                                                                                #
# This shim sits between the mirror and QGC, entirely on the host:                                                               #
#                                                                                                                                #
#   mirror(pod) --UDP--> [pod_port] shim [qgc_sock] --UDP--> QGC(:14550)                                                         #
#                                                                                                                                #
# It forwards telemetry to QGC verbatim, and answers QGC's PARAM/MISSION                                                         #
# requests LOCALLY (a minimal, self-consistent set; empty fence/rally). It                                                       #
# has NO socket toward the pod/vehicle for writes — nothing it generates can                                                     #
# ever reach the real UAV. It is read-only toward the vehicle, exactly like                                                      #
# the mirror, and is purely an operator-view convenience: it does not touch                                                      #
# the 5G C2 path, the SITL, any probe, or any measured metric.                                                                   #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 qgc_responder.py <pod_listen_port> <qgc_host> <qgc_port>                                                             #
# e.g.                                                                                                                           #
#   python3 qgc_responder.py 14560 127.0.0.1 14550                                                                               #
# then relaunch the mirror pointing at the shim instead of QGC:                                                                  #
#   qgc_mavlink_mirror.py <uav_ip> 5760 172.18.0.1 14560                                                                         #
##################################################################################################################################
"""One-way-mirror companion: answers QGC metadata requests without touching the UAV."""
import os
import select
import socket
import sys
import time

# QGroundControl speaks MAVLink v2.0; the v1.0 dialect lacks the MISSION_COUNT
# `mission_type` field, so fence/rally replies would not match QGC's request.
# Pin the v2.0 dialect explicitly (independent of the MAVLINK20 env default).
from pymavlink.dialects.v20 import ardupilotmega as mavlink

# Parameter set replayed to QGC. The real, complete ArduCopter set (captured once
# from the SITL into qgc_params.tsv next to this file) makes QGC fully satisfied —
# no "missing params" / "configuration tasks". If that file is absent we fall back
# to a minimal set, which kills the "did not respond" dialog but may still show the
# firmware/config warnings. Either way the shim only ever REPLIES to QGC; it never
# commands the vehicle.
FAKE_PARAMS: list[tuple[str, float, int]] = [
    ("SYSID_THISMAV", 1.0, mavlink.MAV_PARAM_TYPE_INT32),
    ("FRAME_CLASS", 1.0, mavlink.MAV_PARAM_TYPE_INT8),
    ("FRAME_TYPE", 1.0, mavlink.MAV_PARAM_TYPE_INT8),
]
PARAM_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qgc_params.tsv")


def _load_params() -> list[tuple[str, float, int]]:
    """Load the captured param set (index<TAB>name<TAB>value<TAB>type<TAB>count)."""
    rows: list[tuple[int, str, float, int]] = []
    try:
        with open(PARAM_FILE, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 4:
                    continue
                rows.append((int(parts[0]), parts[1], float(parts[2]), int(parts[3])))
    except Exception:
        return []
    rows.sort()
    return [(name, val, ptype) for _, name, val, ptype in rows]


_REAL = _load_params()
PARAMS: list[tuple[str, float, int]] = _REAL if _REAL else FAKE_PARAMS
_PARAM_INDEX = {name: i for i, (name, _, _) in enumerate(PARAMS)}


def main() -> int:
    if len(sys.argv) != 4:
        sys.stderr.write(
            "usage: qgc_responder.py <pod_listen_port> <qgc_host> <qgc_port>\n"
        )
        return 2
    pod_port = int(sys.argv[1])
    qgc_dest = (sys.argv[2], int(sys.argv[3]))

    # sA: receives telemetry from the mirror (pod side). Host-wide bind so the
    # pod can reach it at 172.18.0.1:<pod_port>.
    s_pod = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s_pod.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s_pod.bind(("0.0.0.0", pod_port))

    # sB: faces QGC. We send telemetry to QGC from this socket, so QGC replies
    # (its PARAM/MISSION requests) come back here — never to the pod.
    s_qgc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s_qgc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:  # larger buffers so a full param dump does not drop packets
        s_qgc.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        s_qgc.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except Exception:
        pass
    s_qgc.bind(("127.0.0.1", 0))

    # Encoder that impersonates the autopilot toward QGC only (via s_qgc).
    enc = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    enc.robust_parsing = True
    dec = mavlink.MAVLink(None)
    dec.robust_parsing = True
    vehicle_sysid = 1

    _pset = f"{len(PARAMS)} real params" if _REAL else f"{len(PARAMS)} fallback params"
    sys.stderr.write(
        f"[responder] up: pod:{pod_port} -> QGC {qgc_dest[0]}:{qgc_dest[1]} "
        f"({_pset}; answers params/fence/rally locally; never writes to UAV)\n"
    )
    sys.stderr.flush()

    def send_to_qgc(msg) -> None:
        try:
            s_qgc.sendto(msg.pack(enc), qgc_dest)
        except Exception:
            pass

    def answer_param_list() -> None:
        # Pace the dump so QGC's UDP buffer does not overflow (a burst of ~1400
        # packets drops many). A short gap every chunk keeps it lossless while
        # still finishing in well under a second.
        n = len(PARAMS)
        for i, (pid, val, ptype) in enumerate(PARAMS):
            send_to_qgc(enc.param_value_encode(pid.encode(), float(val), ptype, n, i))
            if (i & 0x1F) == 0x1F:
                time.sleep(0.001)

    def answer_param_read(pid: str, pindex: int) -> None:
        n = len(PARAMS)
        if pid in _PARAM_INDEX:
            i = _PARAM_INDEX[pid]
            name, val, ptype = PARAMS[i]
        elif 0 <= pindex < n:
            name, val, ptype = PARAMS[pindex]
            i = pindex
        else:
            # Unknown param requested by name: reply with a neutral value so QGC
            # does not retry/time out. index=0,count=n keeps the set consistent.
            name, val, ptype, i = (pid or "UNKNOWN"), 0.0, mavlink.MAV_PARAM_TYPE_REAL32, 0
        send_to_qgc(enc.param_value_encode(name.encode(), float(val), ptype, n, i))

    def answer_mission_empty(mission_type: int) -> None:
        # count=0 for the requested plan type (0=mission,1=fence,2=rally).
        try:
            send_to_qgc(
                enc.mission_count_encode(255, 0, 0, mission_type=mission_type)
            )
        except TypeError:
            # Older pymavlink without mission_type kwarg.
            send_to_qgc(enc.mission_count_encode(255, 0, 0))

    def handle_from_qgc(data: bytes) -> None:
        nonlocal vehicle_sysid
        try:
            msgs = dec.parse_buffer(data) or []
        except Exception:
            return
        for m in msgs:
            t = m.get_type()
            if t == "PARAM_REQUEST_LIST":
                answer_param_list()
            elif t == "PARAM_REQUEST_READ":
                pid = getattr(m, "param_id", "") or ""
                if isinstance(pid, bytes):
                    pid = pid.decode(errors="ignore")
                pid = pid.replace("\x00", "").strip()
                answer_param_read(pid, int(getattr(m, "param_index", -1)))
            elif t in ("MISSION_REQUEST_LIST", "MISSION_REQUEST_INT", "MISSION_REQUEST"):
                mt = int(getattr(m, "mission_type", 0) or 0)
                answer_mission_empty(mt)
            # Everything else (HEARTBEAT, COMMAND_LONG, ...) is ignored: we never
            # forward anything toward the pod/UAV.

    while True:
        try:
            rlist, _, _ = select.select([s_pod, s_qgc], [], [], 1.0)
        except Exception:
            continue
        for sock in rlist:
            try:
                data, _src = sock.recvfrom(65535)
            except Exception:
                continue
            if not data:
                continue
            if sock is s_pod:
                # Telemetry from the mirror -> forward verbatim to QGC.
                try:
                    s_qgc.sendto(data, qgc_dest)
                except Exception:
                    pass
                # Learn the vehicle sysid so our replies match (best-effort).
                try:
                    for m in dec.parse_buffer(data) or []:
                        if m.get_type() == "HEARTBEAT":
                            sid = m.get_srcSystem()
                            if sid and sid != vehicle_sysid:
                                vehicle_sysid = sid
                                enc.srcSystem = sid
                except Exception:
                    pass
            else:
                handle_from_qgc(data)


if __name__ == "__main__":
    raise SystemExit(main())

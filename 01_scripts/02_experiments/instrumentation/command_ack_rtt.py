#!/usr/bin/env python3
##################################################################################################################################
# Module: command_ack_rtt.py (COMMAND_ACK RTT at 1 Hz over the 5G path (flight-safe))                                            #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Default path (requires ACK_PROBE_SOCAT=1 at start_testbed): GCS nr-ue → udpout:UAV_PDU:5763 (bind GCS PDU) →      #
#   socat → SITL tcp:5762 Command descends DL; ACK returns UL — same radio path as C2 interest. Does NOT use UDP:5760            #
#   (mission_runner / serial0).                                                                                                  #
#                                                                                                                                #
# Legacy local path (NOT 5G; debug only): --local-sitl-tcp UAV nr-ue → tcp:127.0.0.1:5762                                        #
#                                                                                                                                #
# Dangerous: --via-c2-udp (udp:UAV:5760) — can steal mission C2; forbidden in NI.                                                #
##################################################################################################################################
"""COMMAND_ACK RTT at 1 Hz over the 5G path (flight-safe).

Default path (requires ACK_PROBE_SOCAT=1 at start_testbed):
  GCS nr-ue → udpout:UAV_PDU:5763 (bind GCS PDU) → socat → SITL tcp:5762
  Command descends DL; ACK returns UL — same radio path as C2 interest.
  Does NOT use UDP:5760 (mission_runner / serial0).

Legacy local path (NOT 5G; debug only): --local-sitl-tcp
  UAV nr-ue → tcp:127.0.0.1:5762

Dangerous: --via-c2-udp (udp:UAV:5760) — can steal mission C2; forbidden in NI.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _inner_script(
    conn: str,
    bind_ip: str,
    duration: float,
    rate_hz: float,
    timeout_s: float,
) -> str:
    bind_expr = repr(bind_ip) if bind_ip else "None"
    return f"""
import json, time
from pymavlink import mavutil
conn = {conn!r}
bind_ip = {bind_expr}
duration = {duration!r}
rate_hz = {rate_hz!r}
timeout_s = {timeout_s!r}
if bind_ip:
    udp = conn if conn.startswith('udpout:') else ('udpout:' + conn.replace('udp:', '', 1))
    mav = mavutil.mavlink_connection(udp, source_address=(bind_ip, 0),
                                    source_system=255, source_component=190)
else:
    mav = mavutil.mavlink_connection(conn, source_system=255, source_component=190)
# Wait for *vehicle* HEARTBEAT (sys not 0/255). Fresh SITL blocks on
# serial0=tcp:5761:wait until mission C2 hits :5760; during orchestrate that
# happens first. Standalone ACK before C2 may need a brief :5760 wake.
hb_deadline = time.monotonic() + 60.0
tgt_sys = tgt_comp = 0
while time.monotonic() < hb_deadline:
    try:
        mav.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                               mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
    except Exception:
        pass
    m = mav.recv_match(type='HEARTBEAT', blocking=True, timeout=1.0)
    if m is None:
        continue
    src = int(m.get_srcSystem())
    if src not in (0, 255):
        tgt_sys, tgt_comp = src, int(m.get_srcComponent())
        break
if tgt_sys == 0:
    raise SystemExit('no vehicle HEARTBEAT (is mission C2 on :5760 up? SITL serial0 :wait)')
CMD = mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE
MSG_ID = mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION
interval = 1.0 / rate_hz
end = time.monotonic() + duration
rows = []
seq = 0
while time.monotonic() < end:
    t_purge = time.monotonic() + 0.05
    while time.monotonic() < t_purge:
        m = mav.recv_match(type='COMMAND_ACK', blocking=False)
        if m is None:
            break
    t0 = time.monotonic_ns()
    mav.mav.command_long_send(tgt_sys, tgt_comp, CMD, 0, float(MSG_ID), 0, 0, 0, 0, 0, 0)
    deadline = time.monotonic() + timeout_s
    ok = False
    result = None
    t1 = None
    while time.monotonic() < deadline:
        m = mav.recv_match(type='COMMAND_ACK', blocking=True, timeout=0.2)
        if m is None:
            continue
        if int(m.command) != int(CMD):
            continue
        t1 = time.monotonic_ns()
        result = int(m.result)
        ok = result in (0, mavutil.mavlink.MAV_RESULT_ACCEPTED)
        break
    rtt_ms = ((t1 - t0) / 1e6) if t1 is not None else None
    rows.append({{'seq': seq, 'ok': ok, 'result': result, 'rtt_ms': rtt_ms, 't0_ns': t0, 't1_ns': t1}})
    seq += 1
    spent = (time.monotonic_ns() - t0) / 1e9
    time.sleep(max(0.0, interval - spent))
rtts = sorted(r['rtt_ms'] for r in rows if r['rtt_ms'] is not None)
def pct(vals, p):
    if not vals: return None
    i = min(len(vals)-1, int(round((p/100.0)*(len(vals)-1))))
    return vals[i]
n_lt_1s = sum(1 for v in rtts if v < 1000.0)
pct_lt_1s = (100.0 * n_lt_1s / len(rtts)) if rtts else None
out = {{
  'schema': 'flyblind_command_ack_rtt_v4',
  'command': 'MAV_CMD_REQUEST_MESSAGE',
  'message_id': 'AUTOPILOT_VERSION',
  'flight_safe': True,
  'connection': conn,
  'bind_ip': bind_ip,
  'timeout_s': timeout_s,
  'n': len(rows),
  'n_ok': sum(1 for r in rows if r['ok']),
  'n_timeout': sum(1 for r in rows if r['rtt_ms'] is None),
  'rtt_ms': {{'p50': pct(rtts,50), 'p95': pct(rtts,95), 'p99': pct(rtts,99), 'max': rtts[-1] if rtts else None}},
  'pct_rtt_lt_1s': pct_lt_1s,
  'n_rtt_lt_1s': n_lt_1s,
  'samples': rows,
}}
print(json.dumps(out))
"""


def _discover_ips(ctx: str, ns: str) -> tuple[str, str]:
    r = subprocess.run(
        [sys.executable, str(HERE / "discover_pdu_ips.py"), "--context", ctx, "--namespace", ns],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        sys.exit("PDU IP discovery failed: %s" % (r.stderr or r.stdout))
    data = json.loads(r.stdout)
    return data["gcs_ip"], data["uav_ip"]


def main() -> None:
    ap = argparse.ArgumentParser(description="COMMAND_ACK RTT (default: 5G via UDP:5763)")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--rate-hz", type=float, default=1.0)
    ap.add_argument(
        "--timeout-s",
        type=float,
        default=30.0,
        help="Per-probe COMMAND_ACK wait (s). Plan default 30.",
    )
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--out", required=True)
    ap.add_argument("--gcs-ip", default="", help="default: auto-discover oaitun")
    ap.add_argument("--uav-ip", default="", help="default: auto-discover oaitun")
    ap.add_argument("--ack-udp-port", type=int, default=5763, help="PDU UDP port (ACK_PROBE_UDP_PORT)")
    ap.add_argument(
        "--local-sitl-tcp",
        action="store_true",
        help="DEBUG: UAV→tcp:127.0.0.1:5762 (NOT 5G). Do not use for NI/campaign metrics.",
    )
    ap.add_argument("--sitl-tcp-port", type=int, default=5762)
    ap.add_argument(
        "--via-c2-udp",
        action="store_true",
        help="DANGEROUS: udp:UAV:5760 — can steal mission C2; off by default",
    )
    ap.add_argument("--c2-udp-port", type=int, default=5760)
    args = ap.parse_args()

    gcs_ip, uav_ip = args.gcs_ip, args.uav_ip
    if not args.local_sitl_tcp and (not gcs_ip or not uav_ip):
        dg, du = _discover_ips(args.context, args.namespace)
        gcs_ip = gcs_ip or dg
        uav_ip = uav_ip or du

    if args.via_c2_udp:
        if not uav_ip:
            sys.exit("--via-c2-udp requires --uav-ip or discovery")
        conn = f"udp:{uav_ip}:{args.c2_udp_port}"
        bind_ip = gcs_ip or ""
        mode = "via_c2_udp_DANGEROUS"
        path_note = "Parallel client on socat UDP:5760 → TCP:5761. May divert mission C2."
        deploy, container = "oai-ue-gcs", "nr-ue"
        crosses_5g = True
    elif args.local_sitl_tcp:
        conn = f"tcp:127.0.0.1:{args.sitl_tcp_port}"
        bind_ip = ""
        mode = "local_sitl_tcp_NOT_5G"
        path_note = "UAV local tcp:5762 — not a 5G RTT measurement."
        deploy, container = "oai-ue-uav", "nr-ue"
        crosses_5g = False
    else:
        if not (gcs_ip and uav_ip):
            sys.exit("need GCS/UAV PDU IPs (pass flags or fix discovery)")
        conn = f"udp:{uav_ip}:{args.ack_udp_port}"
        bind_ip = gcs_ip
        mode = "gcs_udp_ack_probe_5g"
        path_note = (
            "GCS udpout:UAV:%d bind GCS PDU → socat → SITL tcp:5762. "
            "Requires ACK_PROBE_SOCAT=1 at start_testbed. C2 :5760 untouched."
            % args.ack_udp_port
        )
        deploy, container = "oai-ue-gcs", "nr-ue"
        crosses_5g = True

    script = _inner_script(conn, bind_ip, args.duration, args.rate_hz, args.timeout_s)
    proc = subprocess.run(
        [
            "kubectl", "--context", args.context, "exec", "-n", args.namespace,
            f"deploy/{deploy}", "-c", container, "--",
            "python3", "-c", script,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or "")
        sys.stderr.write(proc.stdout or "")
        sys.exit("command_ack_rtt failed rc=%s" % proc.returncode)

    data = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if data is None:
        try:
            data = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            sys.exit("no JSON in output")

    data["mode"] = mode
    data["path_note"] = path_note
    data["crosses_5g"] = crosses_5g
    data["gcs_ip"] = gcs_ip
    data["uav_ip"] = uav_ip
    data["timeout_s"] = args.timeout_s
    # Honeywell-style fraction already computed in-pod; recompute if absent.
    if "pct_rtt_lt_1s" not in data:
        rtts = [
            s["rtt_ms"]
            for s in (data.get("samples") or [])
            if s.get("rtt_ms") is not None
        ]
        data["n_rtt_lt_1s"] = sum(1 for v in rtts if v < 1000.0)
        data["pct_rtt_lt_1s"] = (
            (100.0 * data["n_rtt_lt_1s"] / len(rtts)) if rtts else None
        )
    data["leg_note"] = (
        "The RTT mixes both legs: COMMAND goes down the command leg "
        "(GCS→UAV) and COMMAND_ACK comes back on the return leg (UAV→GCS). "
        "Do not attribute the RTT to a single leg."
    )
    data["socat_risk"] = {
        "c2_port_5760_untouched": not args.via_c2_udp,
        "requires_ACK_PROBE_SOCAT": mode == "gcs_udp_ack_probe_5g",
        "parallel_udp_5760_safe": False,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    summary = {k: v for k, v in data.items() if k != "samples"}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

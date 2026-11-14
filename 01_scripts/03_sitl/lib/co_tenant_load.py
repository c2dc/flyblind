#!/usr/bin/env python3
##################################################################################################################################
# Module: co_tenant_load.py (rogue UE UL load to ext-DN — microburst / dual-pulse)                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Silent-deadline PRB contention on slice A; targets ext-DN :5202. Includes bash fragments for orchestrate rogue lau#
#                                                                                                                                #
# Usage: python3 co_tenant_load.py microburst --target EXT --duration 60 ...                                                     #
##################################################################################################################################

"""Co-tenant load generators — UL bursts from rogue UE to ext-DN only.

Silent-deadline experiments use slice-A rogues to contend PRBs without touching
the victim C2 path (L3 DROP A↔B). All modes target ``ext-DN:5202`` via the rogue
PDU bind address (``oaitun_ue1``).

Subcommands
-----------
* ``microburst`` — periodic byte bursts at a mean Mbps (Phase B validation)
* ``dual-pulse`` — UL leg here; DL leg is ``co_tenant_burst_sender`` on ext-DN

``microburst_script`` / ``dual_pulse_script`` return bash fragments for
``orchestrate.py`` to embed in rogue pod launch scripts (sources ``tm_common.sh``).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import socket
import sys
import time

# Max UDP payload per datagram on GTP/oaitun (MTU 1500); larger sendto → EMSGSIZE.
UDP_DATAGRAM_MAX = 1200


def _send_burst_bytes(
    sock: socket.socket,
    target: str,
    port: int,
    nbytes: int,
    fill_byte: bytes = b"\xcd",
) -> tuple[int, int]:
    """Send nbytes as multiple MTU-safe UDP datagrams. Returns (bytes_sent, packets_sent)."""
    sent = 0
    packets = 0
    while sent < nbytes:
        chunk_len = min(UDP_DATAGRAM_MAX, nbytes - sent)
        try:
            sock.sendto(fill_byte * chunk_len, (target, port))
            sent += chunk_len
            packets += 1
        except OSError:
            break
    return sent, packets


def _shell_preamble() -> str:
    return (
        'set -e; . /tmp/tm_common.sh; '
        'ROGUE="$(tm_pdu_ip)"; EXT="$(tm_ext_dn_ip)"; '
        'if [ -z "$EXT" ]; then echo "[CO-TENANT] ERROR: ext-DN IP missing"; exit 1; fi; '
    )


# --- Bash wrappers for orchestrate (rogue pod one-liners) ---


def microburst_script(
    dur: int,
    mean_mbps: float,
    burst_bytes: int,
    period_ms: int,
    path: str,
    bind_iface: str = "",
) -> str:
    """Same-rate microburst UL to ext-DN."""
    bind = bind_iface or '"$ROGUE"'
    return (
        _shell_preamble()
        + 'echo "[CO-TENANT] microburst dur=%(dur)ss mean=%(mbps)sM burst=%(bb)sB period=%(pm)sms path=%(path)s"; '
        % {"dur": dur, "mbps": mean_mbps, "bb": burst_bytes, "pm": period_ms, "path": path}
        + 'python3 /tmp/co_tenant_load.py microburst '
        + '--target "$EXT" --port 5202 --bind %(bind)s --duration %(dur)s '
        % {"bind": bind, "dur": dur}
        + '--mean-mbps %(mbps)s --burst-bytes %(bb)s --period-ms %(pm)s --path %(path)s '
        % {"mbps": mean_mbps, "bb": burst_bytes, "pm": period_ms, "path": path}
        + '--out-summary /tmp/attack_summary.json --out-timeseries /tmp/attack_timeseries.csv '
        + '| tee /tmp/attack_microburst_%(dur)ss.log' % {"dur": dur}
    )


def dual_pulse_script(
    dur: int,
    period_ms: int,
    phase_ms: int,
    ul_bytes: int,
    dl_bytes: int,
    total_mbps: float,
    path: str,
    bind_iface: str = "",
) -> str:
    bind = bind_iface or '"$ROGUE"'
    return (
        _shell_preamble()
        + 'echo "[CO-TENANT] dual-pulse dur=%(dur)ss T=%(pm)sms phase=%(ph)sms total=%(tm)sM path=%(path)s"; '
        % {"dur": dur, "pm": period_ms, "ph": phase_ms, "tm": total_mbps, "path": path}
        + 'python3 /tmp/co_tenant_load.py dual-pulse '
        + '--target "$EXT" --port 5202 --bind %(bind)s --duration %(dur)s '
        % {"bind": bind, "dur": dur}
        + '--period-ms %(pm)s --phase-ms %(ph)s --ul-burst-bytes %(ub)s --dl-burst-bytes %(db)s '
        % {"pm": period_ms, "ph": phase_ms, "ub": ul_bytes, "db": dl_bytes}
        + '--total-mbps %(tm)s --path %(path)s '
        % {"tm": total_mbps, "path": path}
        + '| tee /tmp/attack_dual_pulse_%(dur)ss.log' % {"dur": dur}
    )


def _write_summary(path: str, summary: dict) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")


def _write_timeseries(path: str, rows: list[dict]) -> None:
    if not path:
        return
    fields = ["t_mono_ns", "burst_index", "packets_sent", "bytes_sent", "bytes_sent_cumulative"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def run_microburst(args: argparse.Namespace) -> int:
    target = args.target
    if getattr(args, "phase_ms", 0) > 0:
        time.sleep(args.phase_ms / 1000.0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.bind:
        sock.bind((args.bind, 0))
    burst_bytes = max(1, args.burst_bytes)
    period_s = args.period_ms / 1000.0
    bursts_per_s = max(1.0, (args.mean_mbps * 1e6 / 8.0) / burst_bytes)
    if bursts_per_s * period_s > 1.0:
        bursts_per_period = max(1, int(bursts_per_s * period_s))
    else:
        bursts_per_period = 1
    bursts_requested = max(1, int(math.ceil(args.duration / period_s)))
    t_start = time.monotonic()
    t_end = t_start + args.duration
    sent = 0
    packets_sent = 0
    bursts = 0
    ts_rows: list[dict] = []
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        burst_packets = 0
        burst_sent = 0
        for _ in range(bursts_per_period):
            b_sent, b_pkts = _send_burst_bytes(sock, target, args.port, burst_bytes)
            burst_sent += b_sent
            burst_packets += b_pkts
            sent += b_sent
            packets_sent += b_pkts
        bursts += 1
        ts_rows.append({
            "t_mono_ns": time.monotonic_ns(),
            "burst_index": bursts,
            "packets_sent": burst_packets,
            "bytes_sent": burst_sent,
            "bytes_sent_cumulative": sent,
        })
        elapsed = time.monotonic() - t0
        sleep_s = max(0.0, period_s - elapsed)
        if sleep_s:
            time.sleep(sleep_s)
    duration_actual = time.monotonic() - t_start
    achieved = (sent * 8.0) / duration_actual / 1e6 if duration_actual > 0 else 0.0
    summary = {
        "mode": "microburst",
        "run_id": args.run_id or None,
        "path": args.path,
        "requested_mean_mbps": args.mean_mbps,
        "achieved_sender_mbps": round(achieved, 3),
        "achieved_mean_mbps_sender": round(achieved, 3),
        "achieved_receiver_mbps": None,
        "bytes_sent": sent,
        "bytes_received": 0,
        "packets_sent": packets_sent,
        "packets_received": 0,
        "bursts_requested": bursts_requested,
        "bursts_completed": bursts,
        "duration_actual_s": round(duration_actual, 3),
        "burst_size_payload_bytes": burst_bytes,
        "inter_burst_period_ms": args.period_ms,
        "seed": args.seed,
        "udp_datagram_max_bytes": UDP_DATAGRAM_MAX,
    }
    print(json.dumps(summary))
    out_summary = args.out_summary or args.out
    _write_summary(out_summary, summary)
    _write_timeseries(args.out_timeseries, ts_rows)
    return 0


def run_dual_pulse(args: argparse.Namespace) -> int:
    """UL burst leg (ROGUE -> ext-DN). DL leg runs via co_tenant_burst_sender on ext-DN."""
    target = args.target
    ul_burst_bytes = max(1, args.ul_burst_bytes)
    ul_rate = args.total_mbps / 2.0
    period_s = args.period_ms / 1000.0
    phase_s = max(0.0, args.phase_ms / 1000.0)
    ul_window_s = max(0.001, min(phase_s if phase_s > 0 else period_s / 2.0, period_s))
    bytes_per_period = max(ul_burst_bytes, int((ul_rate * 1e6 / 8.0) * period_s))
    t_end = time.monotonic() + args.duration
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.bind:
        sock.bind((args.bind, 0))
    sent_ul = 0
    packets_ul = 0
    bursts = 0
    ts_rows: list[dict] = []
    while time.monotonic() < t_end:
        t_cycle = time.monotonic()
        burst_packets = 0
        burst_sent = 0
        while burst_sent < bytes_per_period and time.monotonic() < t_end:
            chunk = min(ul_burst_bytes, bytes_per_period - burst_sent)
            b_sent, b_pkts = _send_burst_bytes(
                sock, target, args.port, chunk, b"\xab",
            )
            if b_sent <= 0:
                break
            burst_sent += b_sent
            burst_packets += b_pkts
            sent_ul += b_sent
            packets_ul += b_pkts
        bursts += 1
        ts_rows.append({
            "t_mono_ns": time.monotonic_ns(),
            "burst_index": bursts,
            "packets_sent": burst_packets,
            "bytes_sent": burst_sent,
            "bytes_sent_cumulative": sent_ul,
        })
        rem = period_s - (time.monotonic() - t_cycle)
        if rem > 0:
            time.sleep(rem)
    duration_actual = args.duration
    achieved = (sent_ul * 8.0) / duration_actual / 1e6 if duration_actual > 0 else 0.0
    summary = {
        "mode": "dual-pulse-ul",
        "run_id": args.run_id or None,
        "path": args.path,
        "period_ms": args.period_ms,
        "phase_ms": args.phase_ms,
        "total_mbps_requested": args.total_mbps,
        "ul_mbps_requested": round(ul_rate, 3),
        "dl_mbps_requested": round(ul_rate, 3),
        "achieved_ul_mbps_sender": round(achieved, 3),
        "achieved_mean_mbps_sender": round(achieved, 3),
        "bytes_sent": sent_ul,
        "packets_sent": packets_ul,
        "burst_count": bursts,
        "bytes_per_period": bytes_per_period,
        "ul_window_s": round(ul_window_s, 4),
        "duration_actual_s": round(duration_actual, 3),
        "udp_datagram_max_bytes": UDP_DATAGRAM_MAX,
        "note": "DL leg via co_tenant_burst_sender on ext-DN",
    }
    print(json.dumps(summary))
    out_summary = args.out_summary or args.out
    _write_summary(out_summary, summary)
    _write_timeseries(args.out_timeseries, ts_rows)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FlyBlind co-tenant load (ext-DN / CNI bypass)")
    sub = ap.add_subparsers(dest="mode", required=True)

    mb = sub.add_parser("microburst")
    mb.add_argument("--target", required=True)
    mb.add_argument("--port", type=int, default=5202)
    mb.add_argument("--bind", default="")
    mb.add_argument("--duration", type=float, required=True)
    mb.add_argument("--mean-mbps", type=float, required=True)
    mb.add_argument("--burst-bytes", type=int, required=True)
    mb.add_argument("--period-ms", type=int, required=True)
    mb.add_argument("--phase-ms", type=int, default=0,
                    help="initial delay before first burst (stagger multi-UE attacks)")
    mb.add_argument("--path", default="pdu")
    mb.add_argument("--seed", type=int, default=1)
    mb.add_argument("--run-id", default="")
    mb.add_argument("--out", default="")
    mb.add_argument("--out-summary", default="")
    mb.add_argument("--out-timeseries", default="")

    dp = sub.add_parser("dual-pulse")
    dp.add_argument("--target", required=True)
    dp.add_argument("--port", type=int, default=5202)
    dp.add_argument("--bind", default="")
    dp.add_argument("--duration", type=float, required=True)
    dp.add_argument("--period-ms", type=int, required=True)
    dp.add_argument("--phase-ms", type=int, default=0)
    dp.add_argument("--ul-burst-bytes", type=int, default=50000)
    dp.add_argument("--dl-burst-bytes", type=int, default=50000)
    dp.add_argument("--total-mbps", type=float, default=20.0)
    dp.add_argument("--path", default="pdu")
    dp.add_argument("--run-id", default="")
    dp.add_argument("--out", default="")
    dp.add_argument("--out-summary", default="")
    dp.add_argument("--out-timeseries", default="")

    args = ap.parse_args()
    if args.mode == "microburst":
        return run_microburst(args)
    if args.mode == "dual-pulse":
        return run_dual_pulse(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

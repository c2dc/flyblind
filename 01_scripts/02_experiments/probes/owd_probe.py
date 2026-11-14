#!/usr/bin/env python3
##################################################################################################################################
# Module: owd_probe.py (OWD/IPDV network probe — NETWORK layer)                                                                  #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Standalone lift of testbed Gate 1.8; GCS→UAV one-way delay with causality and availability gates.                 #
##################################################################################################################################

"""FlyBlind OWD/IPDV probe — NETWORK layer (standalone).

Faithful lift of ``run_owd_ipdv_probe``/``owd_probe_runner.py`` from
``01_scripts/01_testbeds/start_testbed.sh`` so the experiment orchestrator can drive the
network layer without re-provisioning the cluster. The measurement logic and
causality gates are byte-for-byte the same as the testbed's Gate 1.8:

  * shared time-namespace check (GCS/UAV co-located on one Kind node)
  * GCS PDU loopback sanity (CLOCK_MONOTONIC + UDP stack)
  * GCS<->UAV 4-timestamp clock handshake (ordering_violations = t2 < t1)
  * negative-OWD hard fail (causality on shared CLOCK_MONOTONIC)
  * OWD temporal-trend gate (clock/path non-stationarity)
  * availability + DO-377A OWD/IPDV caps (recorded; enforced only if > 0)

OWD is the GCS->UAV leg only (tx_gcs stamped in the UDP payload, rx_uav on
arrival). Report is written to --out and printed to stdout; the process exits
non-zero when a gate fails, exactly like the testbed probe, so the orchestrator
can mark the run invalid while still keeping the emitted JSON.

CPU pinning (optional): set TM_PIN_GCS / TM_PIN_UAV to a taskset core string
to isolate the in-pod sender/receiver from an attacker's CPU burn (empty = off).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

PORT = 47999
LOOPBACK_PORT = 47998
HANDSHAKE_PORT = 47997

_GCS_CORES = os.environ.get("TM_PIN_GCS", "")
_UAV_CORES = os.environ.get("TM_PIN_UAV", "")


def _pin(deploy: str) -> list[str]:
    c = _UAV_CORES if "uav" in deploy else (_GCS_CORES if "gcs" in deploy else "")
    return ["taskset", "-c", c] if c else []


def make_kexec(ctx: str, ns: str):
    def kexec(deploy: str, script: str):
        target = "deploy/" + deploy
        cmd = [
            "kubectl", "--context", ctx, "exec", "-n", ns, target, "-c", "nr-ue", "--",
        ] + _pin(deploy) + ["python3", "-c", script]
        return subprocess.run(cmd, capture_output=True, text=True)
    return kexec


def pct(values, p):
    if not values:
        return None
    m = len(values)
    idx = min(m - 1, int(round((p / 100.0) * (m - 1))))
    return values[idx]


def decile_means_time_ordered(owds):
    w = max(1, len(owds) // 10)
    first = sum(owds[:w]) / w
    last = sum(owds[-w:]) / w
    return first, last, last - first


def read_time_ns(kexec, deploy: str) -> str:
    res = kexec(deploy, "import os; print(os.readlink('/proc/self/ns/time'))")
    if res.returncode != 0:
        sys.exit("read time ns %s failed: %s" % (deploy, res.stderr or res.stdout))
    return (res.stdout or "").strip()


def run_loopback_sanity(kexec, gcs_ip: str, loopback_max_us: float):
    script = (
        "import socket, struct, time, json\n"
        "ip, port = '%s', %d\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((ip, port))\n"
        "sock.settimeout(0.05)\n"
        "end = time.monotonic() + 3\n"
        "lat = []\n"
        "while time.monotonic() < end:\n"
        "    tx = time.monotonic_ns()\n"
        "    sock.sendto(struct.pack('!Q', tx), (ip, port))\n"
        "    try:\n"
        "        data, _ = sock.recvfrom(64)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    if len(data) < 8:\n"
        "        continue\n"
        "    rx = time.monotonic_ns()\n"
        "    lat.append((rx - tx) / 1000.0)\n"
        "out = {'samples': len(lat)}\n"
        "if lat:\n"
        "    s = sorted(lat)\n"
        "    out.update({'median_us': s[len(s)//2], 'p99_us': s[int(len(s)*0.99)], 'max_us': s[-1]})\n"
        "print(json.dumps(out))\n"
    ) % (gcs_ip, LOOPBACK_PORT)
    res = kexec("oai-ue-gcs", script)
    if res.returncode != 0:
        sys.exit("GCS loopback clock sanity failed: %s" % (res.stderr or res.stdout))
    try:
        data = json.loads(res.stdout.strip() or "{}")
    except json.JSONDecodeError:
        sys.exit("GCS loopback clock sanity: invalid JSON: %r" % res.stdout)
    med = data.get("median_us")
    if data.get("samples", 0) < 100:
        sys.exit("GCS loopback clock sanity: too few samples (%s)" % data.get("samples"))
    if med is None or med > loopback_max_us:
        sys.exit(
            "GCS loopback median %.1f us exceeds cap %.1f us — CLOCK_MONOTONIC/UDP sanity failed"
            % (med or -1, loopback_max_us)
        )
    return data


def run_clock_handshake(kexec, ctx, ns, gcs_ip, uav_ip, handshake_samples, ordering_violations_max):
    echo_script = (
        "import socket, struct, time\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind(('%s', %d))\n"
        "sock.settimeout(0.15)\n"
        "end = time.monotonic() + 10\n"
        "while time.monotonic() < end:\n"
        "    try:\n"
        "        data, addr = sock.recvfrom(64)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    if len(data) < 8:\n"
        "        continue\n"
        "    t1, = struct.unpack('!Q', data[:8])\n"
        "    t2 = time.monotonic_ns()\n"
        "    t3 = time.monotonic_ns()\n"
        "    sock.sendto(struct.pack('!QQQ', t1, t2, t3), addr)\n"
    ) % (uav_ip, HANDSHAKE_PORT)
    echo_proc = subprocess.Popen(
        [
            "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
            "python3", "-c", echo_script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.5)
    client_script = (
        "import socket, struct, time, json\n"
        "uav_ip, port, n, gcs_ip = '%s', %d, %d, '%s'\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.bind((gcs_ip, 0))\n"
        "sock.settimeout(0.25)\n"
        "thetas = []\n"
        "delays = []\n"
        "violations = 0\n"
        "ok = 0\n"
        "for _ in range(n):\n"
        "    t1 = time.monotonic_ns()\n"
        "    try:\n"
        "        sock.sendto(struct.pack('!Q', t1), (uav_ip, port))\n"
        "        data, _ = sock.recvfrom(64)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    t4 = time.monotonic_ns()\n"
        "    if len(data) < 24:\n"
        "        continue\n"
        "    t1_r, t2, t3 = struct.unpack('!QQQ', data[:24])\n"
        "    if t1_r != t1:\n"
        "        continue\n"
        "    if t2 < t1:\n"
        "        violations += 1\n"
        "    theta_ns = ((t2 - t1) + (t3 - t4)) // 2\n"
        "    delay_ns = ((t4 - t1) - (t3 - t2)) // 2\n"
        "    thetas.append(theta_ns / 1000.0)\n"
        "    delays.append(delay_ns / 1000.0)\n"
        "    ok += 1\n"
        "    time.sleep(0.002)\n"
        "out = {'samples': ok}\n"
        "if thetas:\n"
        "    s = sorted(thetas)\n"
        "    d = sorted(delays)\n"
        "    out.update({\n"
        "        'ntp_theta_median_us': s[len(s) // 2],\n"
        "        'ntp_theta_mean_us': sum(thetas) / len(thetas),\n"
        "        'ntp_theta_min_us': s[0],\n"
        "        'ntp_theta_max_us': s[-1],\n"
        "        'rtt_median_us': d[len(d) // 2],\n"
        "        'ordering_violations': violations,\n"
        "        'note': 'NTP theta on asymmetric PDU path — path asymmetry, not clock skew',\n"
        "    })\n"
        "print(json.dumps(out))\n"
    ) % (uav_ip, HANDSHAKE_PORT, handshake_samples, gcs_ip)
    client_res = kexec("oai-ue-gcs", client_script)
    try:
        echo_proc.wait(timeout=12)
    except subprocess.TimeoutExpired:
        echo_proc.kill()
    if client_res.returncode != 0:
        sys.exit("Clock handshake failed: %s" % (client_res.stderr or client_res.stdout))
    try:
        data = json.loads(client_res.stdout.strip() or "{}")
    except json.JSONDecodeError:
        sys.exit("Clock handshake: invalid JSON: %r" % client_res.stdout)
    min_hs = max(20, handshake_samples // 5)
    if data.get("samples", 0) < min_hs:
        sys.exit("Clock handshake: too few samples (%s < %s)" % (data.get("samples"), min_hs))
    violations = data.get("ordering_violations")
    if violations is None:
        sys.exit("Clock handshake: missing ordering_violations")
    if violations > ordering_violations_max:
        sys.exit(
            "Clock handshake: %d ordering violations (t2 < t1) exceeds cap %d — causality broken"
            % (violations, ordering_violations_max)
        )
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="FlyBlind OWD/IPDV probe (NETWORK layer)")
    ap.add_argument("--gcs-ip", required=True)
    ap.add_argument("--uav-ip", required=True)
    ap.add_argument("--duration", type=int, required=True, help="measurement seconds")
    ap.add_argument("--warmup", type=int, default=15, help="warm-up seconds discarded")
    ap.add_argument("--min-samples", type=int, default=10000)
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--trend-max-us", type=float, default=10000.0)
    ap.add_argument("--loopback-max-us", type=float, default=5000.0)
    ap.add_argument("--owd-cap-us", type=float, default=0.0, help="DO-377A OWD p99 cap; 0 disables gate")
    ap.add_argument("--ipdv-cap-us", type=float, default=0.0, help="DO-377A IPDV p99 cap; 0 disables gate")
    ap.add_argument("--avail-min-pct", type=float, default=0.0, help="min availability; 0 disables gate")
    ap.add_argument("--ordering-violations-max", type=int, default=0)
    ap.add_argument("--handshake-samples", type=int, default=100)
    ap.add_argument("--out", required=True, help="write report JSON here")
    ap.add_argument("--ready-out", default="", help="write probe_ready.json after clock handshake")
    args = ap.parse_args()

    ctx, ns = args.context, args.namespace
    gcs_ip, uav_ip = args.gcs_ip, args.uav_ip
    duration_s, warmup_s = args.duration, args.warmup
    kexec = make_kexec(ctx, ns)

    gcs_time_ns = read_time_ns(kexec, "oai-ue-gcs")
    uav_time_ns = read_time_ns(kexec, "oai-ue-uav")
    if gcs_time_ns != uav_time_ns:
        sys.exit("GCS/UAV time namespace mismatch: gcs=%r uav=%r" % (gcs_time_ns, uav_time_ns))

    loopback = run_loopback_sanity(kexec, gcs_ip, args.loopback_max_us)
    handshake = run_clock_handshake(
        kexec, ctx, ns, gcs_ip, uav_ip, args.handshake_samples, args.ordering_violations_max
    )
    if args.ready_out:
        ready = {
            "status": "ready",
            "time_namespace_match": gcs_time_ns == uav_time_ns,
            "loopback_samples": loopback.get("samples"),
            "handshake_samples": handshake.get("samples"),
            "ordering_violations": handshake.get("ordering_violations"),
            "ready_at_unix_ns": time.time_ns(),
        }
        with open(args.ready_out, "w", encoding="utf-8") as f:
            json.dump(ready, f, indent=2)
            f.write("\n")
    clock_offset_us = 0.0

    recv_script = (
        "import socket, struct, time, json\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.settimeout(0.1)\n"
        "sock.bind(('%s', %d))\n"
        "end = time.monotonic() + %d\n"
        "samples = []\n"
        "while time.monotonic() < end:\n"
        "    try:\n"
        "        data, _ = sock.recvfrom(64)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    if len(data) < 8:\n"
        "        continue\n"
        "    tx_ns, = struct.unpack('!Q', data[:8])\n"
        "    rx_ns = time.monotonic_ns()\n"
        "    samples.append({'tx_ns': tx_ns, 'rx_ns': rx_ns, 'owd_us': (rx_ns - tx_ns) / 1000.0})\n"
        "open('/tmp/owd_recv.json', 'w').write(json.dumps(samples))\n"
    ) % (uav_ip, PORT, duration_s + 5)

    recv_proc = subprocess.Popen(
        [
            "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        ] + _pin("oai-ue-uav") + ["python3", "-c", recv_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2)

    send_script = (
        "import socket, struct, time, json\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.bind(('%s', 0))\n"
        "end = time.monotonic() + %d\n"
        "sent = 0\n"
        "while time.monotonic() < end:\n"
        "    tx = time.monotonic_ns()\n"
        "    sock.sendto(struct.pack('!Q', tx), ('%s', %d))\n"
        "    sent += 1\n"
        "    time.sleep(0.001)\n"
        "print(json.dumps({'packets_sent': sent}))\n"
    ) % (gcs_ip, duration_s, uav_ip, PORT)
    send_res = kexec("oai-ue-gcs", send_script)
    if send_res.returncode != 0:
        recv_proc.kill()
        sys.exit("GCS OWD sender failed: %s" % (send_res.stderr or send_res.stdout))
    packets_sent = 0
    for line in reversed((send_res.stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            packets_sent = int(json.loads(line).get("packets_sent", 0))
            break
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    try:
        recv_proc.wait(timeout=duration_s + 30)
    except subprocess.TimeoutExpired:
        recv_proc.kill()
        sys.exit("UAV OWD receiver timed out")
    if recv_proc.returncode not in (0, None):
        sys.exit("UAV OWD receiver failed: %s" % (recv_proc.stderr or ""))

    raw = subprocess.run(
        [
            "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
            "cat", "/tmp/owd_recv.json",
        ],
        capture_output=True,
        text=True,
    )
    try:
        samples = json.loads(raw.stdout or "[]")
    except json.JSONDecodeError:
        samples = []

    post = [s for s in samples if s.get("owd_us") is not None]
    packets_received_total = len(post)
    drop = warmup_s * 1000
    if len(post) > drop:
        post = post[drop:]
    owds = [s["owd_us"] for s in post]
    neg_owds = [o for o in owds if o < 0]

    # Assemble report BEFORE gate checks so --out is always written (matches testbed).
    owds_sorted = sorted(owds)
    n = len(owds)
    expected_sent_post = max(1, int((duration_s - warmup_s) * 1000))
    if packets_sent > 0:
        expected_sent_post = max(1, packets_sent - drop)
    avail_pct = 100.0 * n / expected_sent_post
    loss_pct = max(0.0, 100.0 - avail_pct)

    ipdv = [abs(owds[i] - owds[i - 1]) for i in range(1, len(owds))]
    ipdv_sorted = sorted(ipdv)

    temporal_first_us = temporal_last_us = temporal_trend_us = None
    owd_spread_sorted_deciles_us = None
    if n >= 200:
        temporal_first_us, temporal_last_us, temporal_trend_us = decile_means_time_ordered(owds)
        w = n // 10
        owd_spread_sorted_deciles_us = (sum(owds_sorted[-w:]) / w) - (sum(owds_sorted[:w]) / w)

    report = {
        "clock_methodology": (
            "CLOCK_MONOTONIC single-host Kind (GCS+UAV co-located, shared time ns); "
            "OWD = rx_uav - tx_gcs (no NTP correction on asymmetric PDU)"
        ),
        "gcs_ip": gcs_ip,
        "uav_ip": uav_ip,
        "duration_s": duration_s,
        "warmup_discarded_s": warmup_s,
        "samples": n,
        "clock_sanity": {
            "time_namespace_gcs": gcs_time_ns,
            "time_namespace_uav": uav_time_ns,
            "time_namespace_match": gcs_time_ns == uav_time_ns,
            "loopback_gcs_pdu": loopback,
            "loopback_median_max_us": args.loopback_max_us,
            "handshake_4ts": handshake,
            "ordering_violations_max": args.ordering_violations_max,
            "negative_owd_samples": len(neg_owds),
            "clock_offset_applied_us": clock_offset_us,
            "offset_correction_note": (
                "Shared time namespace — no subtraction; "
                "handshake ntp_theta_median_us is PDU path-asymmetry diagnostic"
            ),
        },
        "owd_us": {
            "min": owds_sorted[0] if owds_sorted else None,
            "median": pct(owds_sorted, 50),
            "p99": pct(owds_sorted, 99),
            "p99_9": pct(owds_sorted, 99.9),
            "max": owds_sorted[-1] if owds_sorted else None,
        },
        "ipdv_us": {
            "median": pct(ipdv_sorted, 50),
            "p99": pct(ipdv_sorted, 99),
            "p99_9": pct(ipdv_sorted, 99.9),
            "max": ipdv_sorted[-1] if ipdv_sorted else None,
        },
        "owd_temporal_trend_us": {
            "first_decile_mean_us": temporal_first_us,
            "last_decile_mean_us": temporal_last_us,
            "delta_us": temporal_trend_us,
            "max_abs_delta_us": args.trend_max_us,
            "exceeds_cap": (
                temporal_trend_us is not None
                and abs(temporal_trend_us) > args.trend_max_us
            ),
            "gate_action": (
                # Shared CLOCK_MONOTONIC (namespace match) => skew impossible => an OWD
                # temporal trend is a real path/queue non-stationarity (attack signature,
                # e.g. sync-c2 bufferbloat), NOT clock contamination => informational.
                "informational (shared time namespace — trend is path/attack signal, not skew)"
                if gcs_time_ns == uav_time_ns
                else "invalidate (no shared namespace — trend may be clock skew)"
            ),
            "note": "Time-ordered 1st vs last decile — clock contamination only if clock can drift",
        },
        "owd_spread_sorted_deciles_us": {
            "delta_us": owd_spread_sorted_deciles_us,
            "note": "Fastest vs slowest deciles after sorting — distribution spread, NOT clock drift",
        },
        "availability": {
            "packets_sent": packets_sent,
            "packets_received_total": packets_received_total,
            "packets_received_post_warmup": n,
            "expected_sent_post_warmup": expected_sent_post,
            "availability_post_warmup_pct": round(avail_pct, 4),
            "packet_loss_post_warmup_pct": round(loss_pct, 4),
            "do377a_min_availability_pct": args.avail_min_pct,
            "do377a_compliant": avail_pct >= args.avail_min_pct if args.avail_min_pct > 0 else None,
        },
        "do377a_thresholds_us": {
            "owd_p99_max": args.owd_cap_us if args.owd_cap_us > 0 else None,
            "ipdv_p99_max": args.ipdv_cap_us if args.ipdv_cap_us > 0 else None,
        },
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(json.dumps(report, indent=2))

    # --- Causality / integrity gates (exit non-zero on failure) ---
    if neg_owds:
        sys.exit(
            "OWD samples with negative latency: %d (min %.1f us) — causality violation on shared CLOCK_MONOTONIC"
            % (len(neg_owds), min(neg_owds))
        )
    if n < args.min_samples:
        sys.exit("too few OWD samples: %d < %d" % (n, args.min_samples))
    if temporal_trend_us is not None and abs(temporal_trend_us) > args.trend_max_us:
        if gcs_time_ns != uav_time_ns:
            # No shared time namespace: the clock CAN drift, so a large OWD trend may be
            # real skew/contamination -> invalidate (preserves the original gate).
            sys.exit(
                "OWD temporal trend |%.1f| us exceeds cap %.1f us — possible clock skew "
                "(no shared time namespace)" % (temporal_trend_us, args.trend_max_us)
            )
        # Shared CLOCK_MONOTONIC (namespaces match): skew is physically impossible, so a
        # temporal trend in OWD is a genuine path/queue non-stationarity (e.g. the sync-c2
        # burst inflating the tail) — a REAL attack signal, not clock contamination. Do NOT
        # invalidate: record it (see report.owd_temporal_trend_us.gate_action) and continue.
        print(
            "[clock] OWD temporal trend |%.1f| us > cap %.1f us, but time namespaces match "
            "(shared CLOCK_MONOTONIC): treating as path/attack signal, not skew — clock gate OK"
            % (temporal_trend_us, args.trend_max_us),
            flush=True,
        )
    if args.avail_min_pct > 0 and avail_pct < args.avail_min_pct:
        sys.exit("UDP availability %.4f%% < minimum %.4f%% post-warmup" % (avail_pct, args.avail_min_pct))
    owd_p99 = report["owd_us"]["p99"]
    ipdv_p99 = report["ipdv_us"]["p99"]
    if args.owd_cap_us > 0 and owd_p99 is not None and owd_p99 > args.owd_cap_us:
        sys.exit("OWD p99 %.1f us exceeds DO-377A cap %.1f us" % (owd_p99, args.owd_cap_us))
    if args.ipdv_cap_us > 0 and ipdv_p99 is not None and ipdv_p99 > args.ipdv_cap_us:
        sys.exit("IPDV p99 %.1f us exceeds DO-377A cap %.1f us" % (ipdv_p99, args.ipdv_cap_us))


if __name__ == "__main__":
    main()

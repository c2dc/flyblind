#!/usr/bin/env python3
##################################################################################################################################
# Module: leg_owd_probe.py (Symmetric per-leg OWD probe (command | return))                                                      #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: A single program. Role via --leg. Configurable size, rate and port (M3: two instances on the same leg on          #
#   different ports).                                                                                                            #
#                                                                                                                                #
# 24 B header (plan Block A §4.1). The sender writes a TX log (seq, t_tx). The receiver validates magic + leg_id (M2):           #
#   rejections are counted and the run is flagged.                                                                               #
#                                                                                                                                #
# Modes: --bench-loopback   TX+RX on localhost (no testbed) cluster (default)  kubectl exec in the oai-ue-gcs / oai-ue-uav pods  #
#   (nr-ue) --dry-run-kubectl  only prints the kubectl commands (offline)                                                        #
##################################################################################################################################
"""Symmetric per-leg OWD probe (command | return).

A single program. Role via --leg. Configurable size, rate and port
(M3: two instances on the same leg on different ports).

24 B header (plan Block A §4.1). The sender writes a TX log (seq, t_tx).
The receiver validates magic + leg_id (M2): rejections are counted and the run is flagged.

Modes:
  --bench-loopback   TX+RX on localhost (no testbed)
  cluster (default)  kubectl exec in the oai-ue-gcs / oai-ue-uav pods (nr-ue)
  --dry-run-kubectl  only prints the kubectl commands (offline)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

MAGIC = 0x4631  # 'F1'
LEG_ID = {"command": 1, "return": 2}
LEG_NAME = {1: "command", 2: "return"}
HEADER_FMT = "!HBBIQLHH"  # magic,u16; leg,u8; ver,u8; seq,u32; tx,u64; plen,u16; rate_x10,u16; reserved handled as 4B pad
# Explicit pack for clarity:
# magic u16, leg u8, ver u8, seq u32, t_tx u64, payload_len u16, rate_hz_x10 u16, reserved u32 = 24 B
HEADER_STRUCT = struct.Struct("!HBBIQHHI")
assert HEADER_STRUCT.size == 24

DEFAULT_PORT = {"command": 48002, "return": 48003}
DEFAULT_PAYLOAD = {"command": 24, "return": 140}
DEFAULT_RATE_HZ = 25.0


def pack_payload(
    *,
    leg: str,
    seq: int,
    t_tx_ns: int,
    payload_bytes: int,
    rate_hz: float,
) -> bytes:
    if payload_bytes < 24:
        raise ValueError("payload_bytes must be >= 24")
    rate_x10 = int(round(rate_hz * 10.0)) & 0xFFFF
    hdr = HEADER_STRUCT.pack(
        MAGIC,
        LEG_ID[leg] & 0xFF,
        1,
        seq & 0xFFFFFFFF,
        t_tx_ns & 0xFFFFFFFFFFFFFFFF,
        payload_bytes & 0xFFFF,
        rate_x10,
        0,
    )
    if payload_bytes == 24:
        return hdr
    return hdr + (b"\x00" * (payload_bytes - 24))


def unpack_header(data: bytes) -> dict[str, Any]:
    if len(data) < 24:
        return {"ok": False, "reason": "short"}
    magic, leg_id, ver, seq, t_tx, plen, rate_x10, _res = HEADER_STRUCT.unpack(data[:24])
    return {
        "ok": True,
        "magic": magic,
        "leg_id": leg_id,
        "ver": ver,
        "seq": seq,
        "t_tx_mono_ns": t_tx,
        "payload_len_field": plen,
        "rate_hz_x10": rate_x10,
    }


def run_bench(
    *,
    leg: str,
    duration_s: float,
    rate_hz: float,
    payload_bytes: int,
    port: int,
    out_dir: Path,
    warmup_s: float,
) -> dict[str, Any]:
    """UDP loopback on the same host (TX and RX threads)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tx_path = out_dir / f"tx_{leg}.jsonl"
    rx_path = out_dir / f"rx_{leg}.jsonl"
    kpi_path = out_dir / f"kpi_{leg}.json"

    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    recv_sock.bind(("127.0.0.1", port))
    recv_sock.settimeout(0.2)

    stop = threading.Event()
    rx_rows: list[dict[str, Any]] = []
    rejected_count = 0
    expected_leg = LEG_ID[leg]

    def receiver() -> None:
        nonlocal rejected_count
        end = time.monotonic() + duration_s + 2.0
        while time.monotonic() < end and not stop.is_set():
            try:
                data, _addr = recv_sock.recvfrom(max(2048, payload_bytes + 64))
            except socket.timeout:
                continue
            t_rx = time.monotonic_ns()
            hdr = unpack_header(data)
            if not hdr.get("ok"):
                rejected_count += 1
                continue
            if hdr["magic"] != MAGIC or hdr["leg_id"] != expected_leg:
                rejected_count += 1
                rx_rows.append({
                    "rejected": True,
                    "reason": "magic_or_leg_mismatch",
                    "magic": hdr["magic"],
                    "leg_id": hdr["leg_id"],
                    "seq": hdr["seq"],
                    "t_rx_mono_ns": t_rx,
                })
                continue
            owd_s = (t_rx - int(hdr["t_tx_mono_ns"])) / 1e9
            rx_rows.append({
                "rejected": False,
                "seq": int(hdr["seq"]),
                "t_tx_mono_ns": int(hdr["t_tx_mono_ns"]),
                "t_rx_mono_ns": t_rx,
                "owd_s": owd_s,
                "owd_us": owd_s * 1e6,
                "leg": leg,
                "metric_nature": "owd",
                "clock_source": "CLOCK_MONOTONIC",
                "payload_bytes": payload_bytes,
                "rate_hz": rate_hz,
                "port": port,
                "len_datagram": len(data),
            })

    thr = threading.Thread(target=receiver, daemon=True)
    thr.start()
    time.sleep(0.05)

    interval = 1.0 / rate_hz if rate_hz > 0 else 0.04
    tx_rows: list[dict[str, Any]] = []
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_sock.bind(("127.0.0.1", 0))
    end = time.monotonic() + duration_s
    seq = 0
    t0 = time.monotonic_ns()
    while time.monotonic() < end:
        t_tx = time.monotonic_ns()
        pkt = pack_payload(
            leg=leg, seq=seq, t_tx_ns=t_tx,
            payload_bytes=payload_bytes, rate_hz=rate_hz,
        )
        send_sock.sendto(pkt, ("127.0.0.1", port))
        tx_rows.append({
            "seq": seq,
            "t_tx_mono_ns": t_tx,
            "leg": leg,
            "metric_nature": "owd",
            "clock_source": "CLOCK_MONOTONIC",
            "payload_bytes": payload_bytes,
            "rate_hz": rate_hz,
            "port": port,
        })
        seq += 1
        time.sleep(interval)
    stop.set()
    thr.join(timeout=duration_s + 5)
    recv_sock.close()
    send_sock.close()

    with tx_path.open("w", encoding="utf-8") as f:
        for r in tx_rows:
            f.write(json.dumps(r) + "\n")
    with rx_path.open("w", encoding="utf-8") as f:
        for r in rx_rows:
            f.write(json.dumps(r) + "\n")

    # KPI via kpi_leg
    lib = Path(__file__).resolve().parents[1] / "lib"
    sys.path.insert(0, str(lib))
    from kpi_leg import compute_owd_kpis  # noqa: WPS433

    accepted = [r for r in rx_rows if not r.get("rejected")]
    report = compute_owd_kpis(
        leg=leg,
        samples_rx=accepted,
        tx_seqs=[r["seq"] for r in tx_rows],
        rate_hz=rate_hz,
        duration_s=duration_s,
        warmup_s=warmup_s,
        packets_sent=len(tx_rows),
        payload_bytes=payload_bytes,
        port=port,
        clock_source="CLOCK_MONOTONIC",
        metric_nature="owd",
        rejected_header=rejected_count,
        denominator_where="sender_probe_tx_jsonl",
        t0_mono_ns=t0,
    )
    report["mode"] = "bench_loopback"
    report["tx_path"] = str(tx_path)
    report["rx_path"] = str(rx_path)
    with kpi_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    return report


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


def endpoints_for_leg(leg: str) -> tuple[str, str]:
    """(tx_deploy, rx_deploy). command: GCS→UAV; return: UAV→GCS."""
    if leg == "command":
        return "oai-ue-gcs", "oai-ue-uav"
    return "oai-ue-uav", "oai-ue-gcs"


def bind_ip_for_deploy(deploy: str, gcs_ip: str, uav_ip: str) -> str:
    return gcs_ip if "gcs" in deploy else uav_ip


def dest_ip_for_leg(leg: str, gcs_ip: str, uav_ip: str) -> str:
    return uav_ip if leg == "command" else gcs_ip


def tmp_paths(tmp_tag: str = "") -> tuple[str, str]:
    """(rx_tmp, tx_tmp) in the pod. Empty tmp_tag = historical path (unchanged).

    With tmp_tag != "" (e.g. probe B on the same leg, M3) the files get
    their own suffix, so that two instances on the same leg do not collide in the
    same /tmp of the receiving/sending pod.
    """
    suffix = f"_{tmp_tag}" if tmp_tag else ""
    return f"/tmp/leg_rx{suffix}.json", f"/tmp/leg_tx{suffix}.json"


def build_recv_script(
    *,
    bind_ip: str,
    port: int,
    duration_s: float,
    expected_leg_id: int,
    max_datagram: int,
    rx_tmp_path: str = "/tmp/leg_rx.json",
) -> str:
    # Remote script: validate header, write rx_tmp_path
    return (
        "import socket, struct, time, json\n"
        "MAGIC, EXP, PORT, DUR, BIND, MAXD, RXP = %d, %d, %d, %f, %r, %d, %r\n"
        "HDR = struct.Struct('!HBBIQHHI')\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((BIND, PORT))\n"
        "sock.settimeout(0.2)\n"
        "end = time.monotonic() + DUR + 3\n"
        "rows, rej = [], 0\n"
        "while time.monotonic() < end:\n"
        "    try:\n"
        "        data, addr = sock.recvfrom(MAXD)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    if len(data) < 24:\n"
        "        rej += 1\n"
        "        continue\n"
        "    magic, leg_id, ver, seq, t_tx, plen, rate_x10, _r = HDR.unpack(data[:24])\n"
        "    t_rx = time.monotonic_ns()\n"
        "    if magic != MAGIC or leg_id != EXP:\n"
        "        rej += 1\n"
        "        rows.append({'rejected': True, 'reason': 'magic_or_leg_mismatch',\n"
        "                     'magic': magic, 'leg_id': leg_id, 'seq': seq,\n"
        "                     't_rx_mono_ns': t_rx})\n"
        "        continue\n"
        "    owd_s = (t_rx - t_tx) / 1e9\n"
        "    rows.append({'rejected': False, 'seq': seq, 't_tx_mono_ns': t_tx,\n"
        "                 't_rx_mono_ns': t_rx, 'owd_s': owd_s, 'owd_us': owd_s * 1e6,\n"
        "                 'len_datagram': len(data), 'src': addr[0]})\n"
        "open(RXP,'w').write(json.dumps({'rows': rows, 'rejected_count': rej}))\n"
        "print(json.dumps({'n': len(rows), 'rejected_count': rej}))\n"
    ) % (MAGIC, expected_leg_id, port, duration_s, bind_ip, max_datagram, rx_tmp_path)


def build_send_script(
    *,
    bind_ip: str,
    dest_ip: str,
    port: int,
    duration_s: float,
    rate_hz: float,
    payload_bytes: int,
    leg_id: int,
    tx_tmp_path: str = "/tmp/leg_tx.json",
) -> str:
    interval = 1.0 / rate_hz if rate_hz > 0 else 0.04
    rate_x10 = int(round(rate_hz * 10.0)) & 0xFFFF
    pad = max(0, payload_bytes - 24)
    return (
        "import socket, struct, time, json\n"
        "MAGIC, LEG, DEST, PORT, DUR, IV, PLEN, RATEX, BIND, PADN, TXP = "
        "%d, %d, %r, %d, %f, %f, %d, %d, %r, %d, %r\n"
        "HDR = struct.Struct('!HBBIQHHI')\n"
        "PAD = b'\\x00' * PADN\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.bind((BIND, 0))\n"
        "end = time.monotonic() + DUR\n"
        "seq = 0\n"
        "tx_rows = []\n"
        "while time.monotonic() < end:\n"
        "    t_tx = time.monotonic_ns()\n"
        "    pkt = HDR.pack(MAGIC, LEG, 1, seq & 0xffffffff, t_tx & 0xffffffffffffffff,\n"
        "                   PLEN & 0xffff, RATEX, 0) + PAD\n"
        "    sock.sendto(pkt, (DEST, PORT))\n"
        "    tx_rows.append({'seq': seq, 't_tx_mono_ns': t_tx})\n"
        "    seq += 1\n"
        "    time.sleep(IV)\n"
        "open(TXP,'w').write(json.dumps({'rows': tx_rows, 'packets_sent': seq}))\n"
        "print(json.dumps({'packets_sent': seq}))\n"
    ) % (
        MAGIC, leg_id, dest_ip, port, duration_s, interval,
        payload_bytes, rate_x10, bind_ip, pad, tx_tmp_path,
    )


def kubectl_cmd_preview(
    *,
    ctx: str,
    ns: str,
    deploy: str,
    script: str,
    role: str,
) -> dict[str, Any]:
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/" + deploy,
        "-c", "nr-ue", "--",
    ] + _pin(deploy) + ["python3", "-c", "<script_%s_%d_chars>" % (role, len(script))]
    return {"role": role, "deploy": deploy, "argv": cmd, "script_chars": len(script)}


def run_cluster(
    *,
    leg: str,
    duration_s: float,
    rate_hz: float,
    payload_bytes: int,
    port: int,
    out_dir: Path,
    warmup_s: float,
    gcs_ip: str,
    uav_ip: str,
    context: str,
    namespace: str,
    dry_run: bool,
    tmp_tag: str = "",
) -> dict[str, Any]:
    tx_deploy, rx_deploy = endpoints_for_leg(leg)
    tx_bind = bind_ip_for_deploy(tx_deploy, gcs_ip, uav_ip)
    rx_bind = bind_ip_for_deploy(rx_deploy, gcs_ip, uav_ip)
    dest = dest_ip_for_leg(leg, gcs_ip, uav_ip)
    leg_id = LEG_ID[leg]
    max_datagram = max(2048, payload_bytes + 64)
    rx_tmp, tx_tmp = tmp_paths(tmp_tag)

    recv_script = build_recv_script(
        bind_ip=rx_bind, port=port, duration_s=duration_s,
        expected_leg_id=leg_id, max_datagram=max_datagram,
        rx_tmp_path=rx_tmp,
    )
    send_script = build_send_script(
        bind_ip=tx_bind, dest_ip=dest, port=port, duration_s=duration_s,
        rate_hz=rate_hz, payload_bytes=payload_bytes, leg_id=leg_id,
        tx_tmp_path=tx_tmp,
    )

    time_ns_script = "import os; print(os.readlink('/proc/self/ns/time'))"
    plan = {
        "leg": leg,
        "tx_deploy": tx_deploy,
        "rx_deploy": rx_deploy,
        "tx_bind_ip": tx_bind,
        "rx_bind_ip": rx_bind,
        "dest_ip": dest,
        "port": port,
        "payload_bytes": payload_bytes,
        "rate_hz": rate_hz,
        "tmp_tag": tmp_tag or "",
        "rx_tmp": rx_tmp,
        "tx_tmp": tx_tmp,
        "duration_s": duration_s,
        "time_namespace_check": [
            kubectl_cmd_preview(ctx=context, ns=namespace, deploy="oai-ue-gcs",
                                script=time_ns_script, role="read_time_ns_gcs"),
            kubectl_cmd_preview(ctx=context, ns=namespace, deploy="oai-ue-uav",
                                script=time_ns_script, role="read_time_ns_uav"),
        ],
        "recv": kubectl_cmd_preview(
            ctx=context, ns=namespace, deploy=rx_deploy, script=recv_script, role="recv",
        ),
        "send": kubectl_cmd_preview(
            ctx=context, ns=namespace, deploy=tx_deploy, script=send_script, role="send",
        ),
        "pull_rx": [
            "kubectl", "--context", context, "exec", "-n", namespace,
            "deploy/" + rx_deploy, "-c", "nr-ue", "--", "cat", rx_tmp,
        ],
        "pull_tx": [
            "kubectl", "--context", context, "exec", "-n", namespace,
            "deploy/" + tx_deploy, "-c", "nr-ue", "--", "cat", tx_tmp,
        ],
        "note": (
            "command: TX oai-ue-gcs → RX oai-ue-uav; "
            "return: TX oai-ue-uav → RX oai-ue-gcs; "
            "requires time_namespace_match"
        ),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"kubectl_plan_{leg}.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )

    if dry_run:
        return {
            "ok": True,
            "mode": "dry_run_kubectl",
            "time_namespace_match": None,
            "plan": plan,
            "kpi": None,
        }

    kexec = make_kexec(context, namespace)
    gcs_ns = kexec("oai-ue-gcs", time_ns_script)
    uav_ns = kexec("oai-ue-uav", time_ns_script)
    if gcs_ns.returncode or uav_ns.returncode:
        raise RuntimeError("failed to read time namespaces: %s %s" % (gcs_ns.stderr, uav_ns.stderr))
    gcs_t = (gcs_ns.stdout or "").strip()
    uav_t = (uav_ns.stdout or "").strip()
    if gcs_t != uav_t:
        raise RuntimeError("GCS/UAV time namespace mismatch: gcs=%r uav=%r" % (gcs_t, uav_t))

    recv_proc = subprocess.Popen(
        [
            "kubectl", "--context", context, "exec", "-n", namespace,
            "deploy/" + rx_deploy, "-c", "nr-ue", "--",
        ]
        + _pin(rx_deploy)
        + ["python3", "-c", recv_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.5)
    send_res = kexec(tx_deploy, send_script, timeout=duration_s + 60)
    if send_res.returncode != 0:
        recv_proc.kill()
        raise RuntimeError("send failed: %s" % (send_res.stderr or send_res.stdout))
    try:
        recv_proc.wait(timeout=duration_s + 30)
    except subprocess.TimeoutExpired:
        recv_proc.kill()
        raise RuntimeError("recv timed out")

    def _pull(deploy: str, path: str) -> dict:
        r = subprocess.run(
            [
                "kubectl", "--context", context, "exec", "-n", namespace,
                "deploy/" + deploy, "-c", "nr-ue", "--", "cat", path,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError("pull %s failed: %s" % (path, r.stderr or r.stdout))
        return json.loads(r.stdout or "{}")

    tx_doc = _pull(tx_deploy, tx_tmp)
    rx_doc = _pull(rx_deploy, rx_tmp)
    tx_rows = tx_doc.get("rows") or []
    rx_rows = rx_doc.get("rows") or []
    rejected_count = int(rx_doc.get("rejected_count") or 0)

    tx_path = out_dir / f"tx_{leg}.jsonl"
    rx_path = out_dir / f"rx_{leg}.jsonl"
    with tx_path.open("w", encoding="utf-8") as f:
        for r in tx_rows:
            r = dict(r)
            r.update({
                "leg": leg,
                "metric_nature": "owd",
                "clock_source": "CLOCK_MONOTONIC",
                "payload_bytes": payload_bytes,
                "rate_hz": rate_hz,
                "port": port,
            })
            f.write(json.dumps(r) + "\n")
    with rx_path.open("w", encoding="utf-8") as f:
        for r in rx_rows:
            r = dict(r)
            if not r.get("rejected"):
                r.update({
                    "leg": leg,
                    "metric_nature": "owd",
                    "clock_source": "CLOCK_MONOTONIC",
                    "payload_bytes": payload_bytes,
                    "rate_hz": rate_hz,
                    "port": port,
                })
            f.write(json.dumps(r) + "\n")

    lib = Path(__file__).resolve().parents[1] / "lib"
    sys.path.insert(0, str(lib))
    from kpi_leg import compute_owd_kpis  # noqa: WPS433

    accepted = [r for r in rx_rows if not r.get("rejected")]
    t0 = min((int(r["t_tx_mono_ns"]) for r in tx_rows), default=time.monotonic_ns())
    report = compute_owd_kpis(
        leg=leg,
        samples_rx=accepted,
        tx_seqs=[int(r["seq"]) for r in tx_rows],
        rate_hz=rate_hz,
        duration_s=duration_s,
        warmup_s=warmup_s,
        packets_sent=int(tx_doc.get("packets_sent") or len(tx_rows)),
        payload_bytes=payload_bytes,
        port=port,
        clock_source="CLOCK_MONOTONIC",
        metric_nature="owd",
        rejected_header=rejected_count,
        denominator_where="sender_probe_tx_jsonl_cluster",
        t0_mono_ns=t0,
    )
    report["mode"] = "cluster_kubectl"
    report["time_namespace_match"] = True
    report["time_namespace_gcs"] = gcs_t
    report["time_namespace_uav"] = uav_t
    report["tx_deploy"] = tx_deploy
    report["rx_deploy"] = rx_deploy
    report["tx_path"] = str(tx_path)
    report["rx_path"] = str(rx_path)
    kpi_path = out_dir / f"kpi_{leg}.json"
    kpi_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["kpi_path"] = str(kpi_path)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-leg OWD probe (command|return)")
    ap.add_argument("--leg", choices=["command", "return"], required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--rate-hz", type=float, default=None)
    ap.add_argument("--payload-bytes", type=int, default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--warmup-s", type=float, default=0.0)
    ap.add_argument(
        "--tmp-tag",
        default="",
        help="Suffix of the pod /tmp files (M3: 2nd instance on the same leg). "
             "Empty = historical path /tmp/leg_rx.json / leg_tx.json.",
    )
    ap.add_argument("--out-dir", required=True)
    ap.add_argument(
        "--bench-loopback",
        action="store_true",
        help="TX+RX on localhost (no testbed)",
    )
    ap.add_argument(
        "--dry-run-kubectl",
        action="store_true",
        help="Print/write the kubectl plan without running it (offline)",
    )
    ap.add_argument("--gcs-ip", default="")
    ap.add_argument("--uav-ip", default="")
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--context", default="kind-tm-oranslice")
    args = ap.parse_args()

    leg = args.leg
    rate_hz = float(args.rate_hz if args.rate_hz is not None else DEFAULT_RATE_HZ)
    payload_bytes = int(
        args.payload_bytes if args.payload_bytes is not None else DEFAULT_PAYLOAD[leg]
    )
    port = int(args.port if args.port is not None else DEFAULT_PORT[leg])
    if payload_bytes < 24:
        sys.exit("payload-bytes must be >= 24")

    out_dir = Path(args.out_dir)
    if args.bench_loopback:
        report = run_bench(
            leg=leg,
            duration_s=float(args.duration),
            rate_hz=rate_hz,
            payload_bytes=payload_bytes,
            port=port,
            out_dir=out_dir,
            warmup_s=float(args.warmup_s),
        )
        print(json.dumps({
            "ok": True,
            "leg": leg,
            "port": port,
            "payload_bytes": payload_bytes,
            "rate_hz": rate_hz,
            "rejected_header": report.get("rejected_header"),
            "loss": report.get("loss_by_seq"),
            "has_histogram": "owd_histogram" in (report.get("phases") or {}).get("ALL", {}),
            "has_C1": "criterion_C1" in (report.get("phases") or {}).get("ALL", {}),
            "kpi": str(out_dir / f"kpi_{leg}.json"),
        }, indent=2))
        return 0 if not report.get("run_flagged_header_rejections") else 1

    gcs_ip = args.gcs_ip or "10.0.0.1"
    uav_ip = args.uav_ip or "10.0.0.2"
    if not args.dry_run_kubectl and (not args.gcs_ip or not args.uav_ip):
        sys.exit("--gcs-ip and --uav-ip required for cluster mode (or pass --dry-run-kubectl)")

    report = run_cluster(
        leg=leg,
        duration_s=float(args.duration),
        rate_hz=rate_hz,
        payload_bytes=payload_bytes,
        port=port,
        out_dir=out_dir,
        warmup_s=float(args.warmup_s),
        gcs_ip=gcs_ip,
        uav_ip=uav_ip,
        context=args.context,
        namespace=args.namespace,
        dry_run=bool(args.dry_run_kubectl),
        tmp_tag=args.tmp_tag,
    )
    plan = report.get("plan") or {}
    print(json.dumps({
        "ok": report.get("ok", True),
        "mode": report.get("mode"),
        "leg": leg,
        "time_namespace_match": report.get("time_namespace_match"),
        "plan_path": str(out_dir / f"kubectl_plan_{leg}.json"),
        "kpi": report.get("kpi_path"),
        "tx_deploy": plan.get("tx_deploy") or report.get("tx_deploy"),
        "rx_deploy": plan.get("rx_deploy") or report.get("rx_deploy"),
    }, indent=2))
    if report.get("mode") == "dry_run_kubectl":
        return 0
    return 0 if not report.get("run_flagged_header_rejections") else 1


if __name__ == "__main__":
    raise SystemExit(main())

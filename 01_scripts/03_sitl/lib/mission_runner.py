#!/usr/bin/env python3
##################################################################################################################################
# Module: mission_runner.py (ArduCopter mission driver over 5G C2 — GCS pod)                                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: MAVLink C2 via GCS oaitun_ue1 → UPF → UAV proxy → SITL. Waypoint workload, probe mode, CNI attack triggers.       #
# Writes mission_summary.json and CSV logs consumed by orchestrate.py.                                                           #
#                                                                                                                                #
# Usage: python3 mission_runner.py --connection udp:UAV:5760 --bind-ip GCS ...                                                   #
##################################################################################################################################

"""
Repeatable ArduCopter mission driver over 5G C2 (GCS pod).

Data path
---------
GCS ``oaitun_ue1`` → UPF/GTP → UAV ``oaitun_ue1`` → ``mavlink_proxy`` :5760 → SITL :5761

File map
--------
* ``Thresholds`` / ``C2Stats`` / ``PhysicsStats`` / ``MissionPollState`` — run-time accumulators
* ``open_c2_mavlink`` / ``make_mavlink_connection`` — bind GCS PDU, wait heartbeat
* ``arm_sitl_with_retries`` / ``takeoff_sitl_with_retries`` — bootstrap with STATUSTEXT logging
* ``send_command`` / ``send_reposition`` — MAVLink command helpers with ACK polling
* ``poll_messages`` / ``poll_sleep`` — heartbeat gap + physics sampling during flight
* ``run_mission`` — waypoint workload (DO_REPOSITION mission JSON)
* ``main`` — CLI; also supports ``--probe`` (C2 preflight) and ``--attack-trigger`` (CNI sync)

Outputs land in ``mission_summary.json`` + CSV logs consumed by ``orchestrate.py``.
Setpoint-stream workload lives in ``setpoint_stream.py`` but reuses helpers here.
"""
from __future__ import annotations

import csv
import json
import math
import random
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pymavlink import mavutil


# --- Run-time metric containers ---


@dataclass
class Thresholds:
    heartbeat_loss_gap_s: float = 1.5
    command_ack_timeout_s: float = 3.0
    command_late_ack_ms: float = 500.0
    warmup_discard_s: float = 5.0


@dataclass
class C2Stats:
    heartbeats_seen: int = 0
    heartbeat_gaps_over_threshold: int = 0
    max_heartbeat_gap_s: float = 0.0
    commands_sent: int = 0
    commands_acked: int = 0
    commands_lost: int = 0
    commands_late: int = 0
    command_latencies_ms: list[float] = field(default_factory=list)
    failsafe_triggered: bool = False
    failsafe_mode: str = ""
    failsafe_time_s: Optional[float] = None


@dataclass
class MissionPollState:
    """Shared recv/drain state — heartbeats must be polled whenever the link is idle."""

    run_t0: float
    log_rows: list[dict]
    hb_log: list[dict]
    c2: C2Stats
    phys: PhysicsStats
    planned: tuple[float, float]
    thr: Thresholds
    last_hb: list[float]
    pos_cache: list[Optional[tuple[float, float, float]]]
    rel_alt_cache: list[Optional[float]] = field(default_factory=lambda: [None])
    ned_cache: list[Optional[tuple[float, float, float]]] = field(default_factory=lambda: [None])
    measure: bool = False
    landing_requested: bool = False
    intentional_land: bool = False
    # Buffer of (t_mono, command_id, result) for COMMAND_ACK drained by poll_messages,
    # so ack matching survives the message pump instead of being silently discarded.
    acks: list[tuple[float, int, int]] = field(default_factory=list)
    # Reposition command currently in flight, for response latency measurement
    # (setpoint adoption, motion onset, settling time). Filled by the steady loop
    # before each DO_REPOSITION; updated by poll_messages.
    active_cmd: Optional[dict] = None
    # Ideal route segment for cross-track: position at DO_REPOSITION send → target.
    nav_leg_from: Optional[tuple[float, float]] = None
    nav_leg_to: Optional[tuple[float, float]] = None
    telemetry_capture: bool = False
    telemetry_rows: list[dict] = field(default_factory=list)
    # Last time we sent a GCS→vehicle HEARTBEAT (ArduPilot FS_GCS). Must stay
    # well under FS_GCS_TIMEOUT during warm-up/climb gaps when no setpoints flow.
    last_gcs_hb_sent: float = 0.0
    # Plan items 1.4 / 1.4c: every UAV→GCS MAVLink arrival (minus RADIO_STATUS) with
    # monotonic stamp for K8 / VehicleLinkManager-style gaps.
    link_rx_log: list[dict] = field(default_factory=list)
    # GCS HEARTBEAT TX stamps (command-leg vivacity source at GCS).
    gcs_hb_sent_log: list[dict] = field(default_factory=list)
    # STATUSTEXT lines (FS_GCS forensics for K7).
    status_text_log: list[dict] = field(default_factory=list)


@dataclass
class PhysicsStats:
    max_deviation_m: float = 0.0
    samples: int = 0
    geofence_violations: int = 0
    # --- Rich flight-state telemetry (additive; existing metrics above untouched) ---
    # Tracking error: horizontal distance current→active setpoint (POSITION_TARGET).
    track_errors_m: list[float] = field(default_factory=list)
    # Instantaneous cross-track: perpendicular distance to position→target segment of each DO_REPOSITION.
    cross_track_errors_m: list[float] = field(default_factory=list)
    # Attitude (control oscillation): angles in degrees and rates in rad/s.
    att_roll_deg: list[float] = field(default_factory=list)
    att_pitch_deg: list[float] = field(default_factory=list)
    att_rollrate: list[float] = field(default_factory=list)
    att_pitchrate: list[float] = field(default_factory=list)
    att_yawrate: list[float] = field(default_factory=list)
    # EKF (expected inert in SITL: sensors are local to the simulator, not over C2).
    ekf_vel_var: list[float] = field(default_factory=list)
    ekf_pos_horiz_var: list[float] = field(default_factory=list)
    ekf_pos_vert_var: list[float] = field(default_factory=list)
    ekf_compass_var: list[float] = field(default_factory=list)
    # Graded command response: one row per DO_REPOSITION.
    response_rows: list[dict] = field(default_factory=list)
    # Uncommanded flight-mode transitions (finer history than binary failsafe).
    mode_transitions: list[dict] = field(default_factory=list)
    # Unified time series (one row per ATTITUDE sample) for offline analysis.
    flight_rows: list[dict] = field(default_factory=list)
    # Current state (caches to join messages at different rates).
    setpoint: Optional[tuple[float, float, float]] = None
    last_track_err_m: Optional[float] = None
    last_cross_track_m: Optional[float] = None
    last_ekf: dict = field(default_factory=dict)
    last_mode: Optional[str] = None
    last_armed: Optional[bool] = None


# --- Geo / mission helpers ---


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cross_track_distance_m(
    lat: float,
    lon: float,
    lat_a: float,
    lon_a: float,
    lat_b: float,
    lon_b: float,
) -> float:
    """Perpendicular distance from (lat,lon) to straight segment A→B (approx. equirectangular)."""
    lat0 = (lat_a + lat_b) / 2.0
    r = 6371000.0
    cos_lat = math.cos(math.radians(lat0))

    def to_xy(la: float, lo: float) -> tuple[float, float]:
        x = math.radians(lo - lon_a) * r * cos_lat
        y = math.radians(la - lat_a) * r
        return x, y

    bx, by = to_xy(lat_b, lon_b)
    px, py = to_xy(lat, lon)
    seg_len2 = bx * bx + by * by
    if seg_len2 < 1e-6:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * bx + py * by) / seg_len2))
    return math.hypot(px - t * bx, py - t * by)


# --- MAVLink connection + command path ---


def wait_heartbeat(mav: mavutil.mavfile, timeout_s: float = 90) -> None:
    """Wait for vehicle HEARTBEAT. SITL cold-start on first C2 attach can take 30–60s."""
    t0 = time.monotonic()
    last_log = t0
    while time.monotonic() - t0 < timeout_s:
        try:
            mav.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0,
                0,
                mavutil.mavlink.MAV_STATE_ACTIVE,
            )
        except Exception:
            pass
        if mav.wait_heartbeat(timeout=2):
            return
        if time.monotonic() - last_log >= 10:
            elapsed = int(time.monotonic() - t0)
            print("[C2] waiting for HEARTBEAT… %ss (SITL may cold-start on first attach)" % elapsed, flush=True)
            last_log = time.monotonic()
    raise TimeoutError("no HEARTBEAT from vehicle within %ss" % timeout_s)


def open_c2_mavlink(
    connection: str,
    bind_ip: str = "",
    *,
    heartbeat_timeout_s: float = 90,
    attempts: int = 3,
) -> mavutil.mavfile:
    """Connect over 5G PDU path (UDP MAVLink). Retry on refused/reset.

    C2 over 5G is UDP-only. SITL still listens TCP on localhost:5761 behind the
    UAV UDP relay (socat / c2_arrival_forwarder) — that hop is not this path.
    """
    last_err: Optional[BaseException] = None
    mav: Optional[mavutil.mavfile] = None
    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                print(
                    "[C2] UDP reconnect attempt %s/%s (connection refused / reset)"
                    % (attempt, attempts),
                    flush=True,
                )
                time.sleep(3)
            mav = make_mavlink_connection(connection, bind_ip)
            break
        except (ConnectionRefusedError, OSError, TimeoutError, ConnectionError) as exc:
            last_err = exc
            mav = None
    if mav is None:
        assert last_err is not None
        raise last_err
    wait_heartbeat(mav, timeout_s=heartbeat_timeout_s)
    return mav


def wait_gps_ready(mav: mavutil.mavfile, timeout_s: float = 45) -> tuple[float, float, float]:
    """Wait for SITL GPS/EKF — request position stream and poll."""
    try:
        mav.mav.request_data_stream_send(
            mav.target_system,
            mav.target_component,
            mavutil.mavlink.MAV_DATA_STREAM_POSITION,
            4,
            1,
        )
    except Exception:
        pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        msg = mav.recv_match(
            type=["GLOBAL_POSITION_INT", "HOME_POSITION", "GPS_RAW_INT"],
            blocking=True,
            timeout=2,
        )
        if msg is None:
            time.sleep(0.05)
            continue
        if msg.get_type() == "HOME_POSITION" and msg.latitude:
            # Prefer GLOBAL_POSITION_INT when available; HOME alone is too early for arming.
            continue
        if msg.get_type() == "GLOBAL_POSITION_INT" and msg.lat:
            return msg.lat / 1e7, msg.lon / 1e7, msg.alt / 1000.0
    raise TimeoutError("GPS/home not ready within %ss — is SITL fully booted?" % timeout_s)


def get_home(mav: mavutil.mavfile, timeout_s: float = 45) -> tuple[float, float, float]:
    return wait_gps_ready(mav, timeout_s)


def _purge_command_acks(
    mav: mavutil.mavfile,
    poll: Optional[MissionPollState],
    cmd: int,
    purge_s: float = 0.35,
) -> None:
    """Drop stale COMMAND_ACK for ``cmd`` from poll buffer and the socket queue."""
    if poll is not None:
        poll.acks[:] = [a for a in poll.acks if a[1] != cmd]
    deadline = time.monotonic() + purge_s
    while time.monotonic() < deadline:
        drained = False
        if poll is not None:
            poll_messages(mav, poll)
            before = len(poll.acks)
            poll.acks[:] = [a for a in poll.acks if a[1] != cmd]
            drained = len(poll.acks) < before or drained
        while True:
            msg = mav.recv_match(type="COMMAND_ACK", blocking=False)
            if msg is None:
                break
            drained = True
            if int(msg.command) == cmd:
                continue
        if not drained:
            time.sleep(0.02)


def _await_ack(
    mav: mavutil.mavfile,
    cmd: int,
    t0_ns: int,
    timeout_s: float,
    poll: Optional[MissionPollState],
) -> tuple[bool, float]:
    """Wait for the COMMAND_ACK matching ``cmd``.

    With a poll state we keep pumping the link (so heartbeats/positions are not
    starved) and read acks from ``poll.acks``, where poll_messages buffers them —
    this avoids the old race where poll_messages silently dropped the ack.
    """
    terminal_fail = (
        mavutil.mavlink.MAV_RESULT_DENIED,
        mavutil.mavlink.MAV_RESULT_FAILED,
        mavutil.mavlink.MAV_RESULT_TEMPORARILY_REJECTED,
        mavutil.mavlink.MAV_RESULT_UNSUPPORTED,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if poll is not None:
            poll_messages(mav, poll)
            for i in range(len(poll.acks)):
                _, c, r = poll.acks[i]
                if c != cmd:
                    continue
                poll.acks.pop(i)
                lat_ms = (time.monotonic_ns() - t0_ns) / 1e6
                if r == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    return True, lat_ms
                if r in terminal_fail:
                    print(
                        "[C2] COMMAND_ACK cmd=%s result=%s (%s) after %.0f ms"
                        % (cmd, r, _mav_result_name(r), lat_ms),
                        flush=True,
                    )
                    return False, lat_ms
                # IN_PROGRESS / other non-terminal: keep waiting for final ack
                break
            time.sleep(0.02)
        else:
            ack = mav.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.5)
            if ack is None:
                continue
            if ack.command != cmd:
                continue
            lat_ms = (time.monotonic_ns() - t0_ns) / 1e6
            if ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                return True, lat_ms
            if ack.result in terminal_fail:
                print(
                    "[C2] COMMAND_ACK cmd=%s result=%s (%s) after %.0f ms"
                    % (cmd, ack.result, _mav_result_name(ack.result), lat_ms),
                    flush=True,
                )
                return False, lat_ms
    lat_ms = (time.monotonic_ns() - t0_ns) / 1e6
    print(
        "[C2] COMMAND_ACK cmd=%s timeout after %.0f ms"
        % (cmd, lat_ms),
        flush=True,
    )
    return False, lat_ms


def _drop_buffered_acks(poll: Optional[MissionPollState], cmd: int) -> None:
    """Clear stale acks for this command id before (re)sending it, so a late ack
    from a previous same-id command cannot be mis-attributed to the new one."""
    if poll is not None:
        poll.acks[:] = [a for a in poll.acks if a[1] != cmd]

def send_command(
    mav: mavutil.mavfile,
    cmd: int,
    p1: float = 0,
    p2: float = 0,
    p3: float = 0,
    p4: float = 0,
    p5: float = 0,
    p6: float = 0,
    p7: float = 0,
    timeout_s: float = 3.0,
    poll: Optional[MissionPollState] = None,
) -> tuple[bool, float]:
    """Send a COMMAND_LONG and return (accepted, latency_ms)."""
    _drop_buffered_acks(poll, cmd)
    _purge_command_acks(mav, poll, cmd)
    t0 = time.monotonic_ns()
    mav.mav.command_long_send(
        mav.target_system,
        mav.target_component,
        cmd,
        0,
        p1, p2, p3, p4, p5, p6, p7,
    )
    return _await_ack(mav, cmd, t0, timeout_s, poll)


def send_reposition(
    mav: mavutil.mavfile,
    lat: float,
    lon: float,
    alt_m: float,
    timeout_s: float = 3.0,
    poll: Optional[MissionPollState] = None,
) -> tuple[bool, float]:
    """Guided reposition via COMMAND_INT MAV_CMD_DO_REPOSITION — ArduPilot accepts
    this in GUIDED and replies with COMMAND_ACK, so it is measurable C2 (unlike
    SET_POSITION_TARGET, which is fire-and-forget). Returns (accepted, latency_ms)."""
    cmd = mavutil.mavlink.MAV_CMD_DO_REPOSITION
    _drop_buffered_acks(poll, cmd)
    _purge_command_acks(mav, poll, cmd)
    t0 = time.monotonic_ns()
    mav.mav.command_int_send(
        mav.target_system,
        mav.target_component,
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        cmd,
        0,  # current
        0,  # autocontinue
        -1.0,          # p1 ground speed (-1 = default)
        0.0,           # p2 MAV_DO_REPOSITION_FLAGS (0 = stay in guided)
        0.0,           # p3 reserved
        float("nan"),  # p4 yaw (NaN = keep current heading)
        int(lat * 1e7),
        int(lon * 1e7),
        float(alt_m),
    )
    return _await_ack(mav, cmd, t0, timeout_s, poll)


def set_mode_named(mav: mavutil.mavfile, mode: str, timeout_s: float = 15.0) -> None:
    mapping = mav.mode_mapping()
    if mode not in mapping:
        raise TimeoutError("%s not in mode mapping: %s" % (mode, list(mapping.keys())))
    mav.set_mode(mapping[mode])
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        hb = mav.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        if hb and mavutil.mode_string_v10(hb) == mode:
            return
    raise TimeoutError("failed to enter %s mode" % mode)


def set_mode_guided(mav: mavutil.mavfile) -> None:
    set_mode_named(mav, "GUIDED")


def wait_for_heartbeat_mode(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    mode_name: str,
    timeout_s: float = 15.0,
) -> bool:
    """Pump MAVLink until ``poll.phys.last_mode`` matches (state logger must be active)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        poll_messages(mav, poll)
        if poll.phys.last_mode == mode_name:
            return True
        poll_sleep(mav, 0.15, poll)
    return False


def bootstrap_land_then_guided(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    *,
    timeout_s: float = 30.0,
) -> bool:
    """Ensure the state logger observes LAND, then a fresh LAND→GUIDED transition.

    Required when SITL persists in GUIDED after warm-up or a prior run.
    """
    request_flight_state_streams(mav)
    deadline = time.monotonic() + timeout_s
    while poll.phys.last_mode is None and time.monotonic() < deadline:
        poll_messages(mav, poll)
        poll_sleep(mav, 0.2, poll)
    mode0 = poll.phys.last_mode
    print(
        "[C2] state logger active, initial mode=%s" % (mode0 or "?"),
        flush=True,
    )
    if mode0 != "LAND":
        poll.intentional_land = True
        try:
            set_mode_named(mav, "LAND", timeout_s=10.0)
            if not wait_for_heartbeat_mode(mav, poll, "LAND", timeout_s=15.0):
                print(
                    "[C2] bootstrap: LAND not confirmed (mode=%s)"
                    % (poll.phys.last_mode or "?"),
                    flush=True,
                )
                return False
            poll_sleep(mav, 0.5, poll)
            print("[C2] bootstrap: confirmed LAND", flush=True)
        finally:
            poll.intentional_land = False
    else:
        print("[C2] bootstrap: already in LAND", flush=True)
    set_mode_guided(mav)
    if not wait_for_heartbeat_mode(mav, poll, "GUIDED", timeout_s=15.0):
        print(
            "[C2] bootstrap: GUIDED not confirmed (mode=%s)"
            % (poll.phys.last_mode or "?"),
            flush=True,
        )
        return False
    poll_sleep(mav, 0.5, poll)
    print("[C2] bootstrap: LAND->GUIDED ready", flush=True)
    return True


PREARM_ALT_MAX_M = 1.0
PREARM_FAILSAFE_MODES = frozenset(
    {"RTL", "LAND", "SMART_RTL", "AUTO_RTL", "QRTL", "BRAKE", "AUTO_RTL"}
)


def _flight_snapshot(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    sample_s: float = 0.8,
) -> tuple[Optional[float], Optional[bool], Optional[str]]:
    """Return (relative_alt_m, armed, mode_name) after a short pump."""
    deadline = time.monotonic() + sample_s
    rel_alt = poll.rel_alt_cache[0]
    armed: Optional[bool] = None
    mode: Optional[str] = poll.phys.last_mode
    while time.monotonic() < deadline:
        poll_messages(mav, poll)
        rel_alt = poll.rel_alt_cache[0] if poll.rel_alt_cache[0] is not None else rel_alt
        mode = poll.phys.last_mode or mode
        msg = mav.recv_match(type="HEARTBEAT", blocking=False)
        if msg is not None and msg.get_srcSystem() == mav.target_system:
            armed = _heartbeat_armed(msg)
            mode = mavutil.mode_string_v10(msg)
        time.sleep(0.05)
    armed_out: Optional[bool] = armed if armed is not None else poll.phys.last_armed
    return rel_alt, armed_out, mode


def _send_gcs_heartbeat(mav: mavutil.mavfile) -> None:
    try:
        mav.mav.heartbeat_send(
            mavutil.mavlink.MAV_TYPE_GCS,
            mavutil.mavlink.MAV_AUTOPILOT_INVALID,
            0,
            0,
            mavutil.mavlink.MAV_STATE_ACTIVE,
        )
    except Exception:
        pass


# Keep GCS→vehicle traffic alive for ArduPilot FS_GCS (default timeout 10 s).
GCS_HEARTBEAT_PERIOD_S = 1.0


def ensure_gcs_heartbeat(
    mav: mavutil.mavfile,
    poll: Optional[MissionPollState] = None,
    *,
    period_s: float = GCS_HEARTBEAT_PERIOD_S,
) -> None:
    """Send GCS HEARTBEAT at least every ``period_s`` (prevents spurious RTL).

    Called from the cooperative poll loop (``poll_messages`` / ``poll_sleep``),
    not from a dedicated thread. Independent of setpoints: fires on the 1 Hz
    period even when no SET_POSITION is in flight.
    """
    now = time.monotonic()
    if poll is not None:
        if poll.last_gcs_hb_sent > 0 and (now - poll.last_gcs_hb_sent) < period_s:
            return
        poll.last_gcs_hb_sent = now
        poll.gcs_hb_sent_log.append({
            "t_mono_ns": time.monotonic_ns(),
            "t_rel_s": round(now - poll.run_t0, 3),
            "steady_state": poll.measure,
            "mavlink_msg_type": "HEARTBEAT",
            "leg": "command",
            "site": "gcs_tx",
        })
    _send_gcs_heartbeat(mav)


def clear_bootstrap_failsafe_latch(poll: MissionPollState) -> None:
    """Bootstrap LAND→GUIDED is intentional setup, not a mission failsafe event."""
    poll.c2.failsafe_triggered = False
    poll.c2.failsafe_mode = None
    poll.c2.failsafe_time_s = None


def run_pre_arm_gate(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    *,
    altitude_max_m: float = PREARM_ALT_MAX_M,
) -> tuple[bool, dict[str, Any]]:
    """Formal pre-arm checklist (bootstrap). Returns (ok, details)."""
    checks: dict[str, Any] = {}
    poll_messages(mav, poll)
    for _ in range(5):
        poll_messages(mav, poll)
    rel_alt, armed, mode = _flight_snapshot(mav, poll, sample_s=1.0)
    hb_seen = False
    hb_age_s: Optional[float] = None
    if poll.last_hb[0] > 0:
        hb_age_s = time.monotonic() - poll.last_hb[0]
        if hb_age_s <= 10.0:
            hb_seen = True
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not hb_seen:
        _send_gcs_heartbeat(mav)
        poll_messages(mav, poll)
        if poll.last_hb[0] > 0:
            hb_age_s = time.monotonic() - poll.last_hb[0]
            if hb_age_s <= 10.0:
                hb_seen = True
                break
        msg = mav.recv_match(type="HEARTBEAT", blocking=False)
        if msg is not None and msg.get_srcSystem() == mav.target_system:
            hb_seen = True
            if armed is None:
                armed = _heartbeat_armed(msg)
            if mode is None:
                mode = mavutil.mode_string_v10(msg)
            break
        poll_sleep(mav, 0.2, poll)
    checks["heartbeat"] = hb_seen
    checks["heartbeat_age_s"] = round(hb_age_s, 2) if hb_age_s is not None else None
    checks["disarmed"] = armed is False
    checks["on_ground"] = rel_alt is not None and rel_alt <= altitude_max_m + 0.5
    checks["altitude_m"] = round(rel_alt, 2) if rel_alt is not None else None
    checks["altitude_ok"] = rel_alt is not None and rel_alt <= altitude_max_m
    checks["mode"] = mode
    checks["mode_known"] = bool(mode)
    checks["failsafe_inactive"] = (
        not poll.c2.failsafe_triggered
        and (mode is None or mode not in PREARM_FAILSAFE_MODES)
    )
    checks["gps_home_ok"] = poll.ned_cache[0] is not None or poll.pos_cache[0] is not None
    failed = [k for k, v in (
        ("heartbeat", hb_seen),
        ("disarmed", checks["disarmed"]),
        ("on_ground", checks["on_ground"]),
        ("altitude_ok", checks["altitude_ok"]),
        ("mode_known", checks["mode_known"]),
        ("failsafe_inactive", checks["failsafe_inactive"]),
    ) if not v]
    ok = not failed
    details: dict[str, Any] = {
        "ok": ok,
        "checks": checks,
        "failed": failed,
        "altitude_max_m": altitude_max_m,
    }
    if not ok:
        print(
            "[C2] pre-arm gate FAIL: %s (armed=%s alt=%s mode=%s)"
            % (",".join(failed), armed, checks["altitude_m"], mode),
            flush=True,
        )
    else:
        print(
            "[C2] pre-arm gate OK (armed=%s alt=%sm mode=%s)"
            % (armed, checks["altitude_m"], mode),
            flush=True,
        )
    return ok, details


def ensure_sitl_landed_and_disarmed(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    timeout_s: float = 45.0,
) -> None:
    """Reset stale SITL state left from prior runs (SITL persists; missions may abort early).

    ArduPilot rejects NAV_TAKEOFF when the vehicle is still armed in GUIDED from a
    previous session even if it is on the ground. LAND → disarm clears that state.
    """
    poll.intentional_land = True
    try:
        rel_alt, armed, mode = _flight_snapshot(mav, poll)
        if (
            not armed
            and (rel_alt is None or rel_alt <= 1.5)
            and mode in ("GUIDED", "STABILIZE")
        ):
            return
        print(
            "[C2] resetting stale SITL flight state (armed=%s rel_alt=%s mode=%s)"
            % (armed, ("%.1fm" % rel_alt) if rel_alt is not None else "n/a", mode or "?"),
            flush=True,
        )
        if rel_alt is None or rel_alt > 1.5 or armed:
            set_mode_named(mav, "LAND", timeout_s=10.0)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                poll_sleep(mav, 0.5, poll)
                rel_alt, armed, mode = _flight_snapshot(mav, poll, sample_s=0.3)
                if rel_alt is not None and rel_alt <= 0.5 and not armed:
                    print("[C2] landed and disarmed (rel_alt=%.1fm)" % rel_alt, flush=True)
                    poll_sleep(mav, 2.0, poll)
                    return
            print("[C2] land cycle timeout — forcing disarm", flush=True)
        ensure_disarmed_sitl(mav, poll, timeout_s=min(timeout_s, 12.0))
        poll_sleep(mav, 1.0, poll)
    finally:
        poll.intentional_land = False


def wait_sitl_ready_to_arm(
    mav: mavutil.mavfile,
    timeout_s: float = 90,
    min_settle_s: float = 0.0,
    poll: Optional[MissionPollState] = None,
) -> None:
    """Wait until SITL can accept arming (EKF boot or stable GPS on reconnect)."""
    try:
        mav.mav.request_data_stream_send(
            mav.target_system,
            mav.target_component,
            mavutil.mavlink.MAV_DATA_STREAM_POSITION,
            4,
            1,
        )
    except Exception:
        pass
    t0 = time.monotonic()
    gps_count = 0
    ekf_gps_seen = False
    ready_at: float | None = None
    while time.monotonic() - t0 < timeout_s:
        _send_gcs_heartbeat(mav)
        msg = mav.recv_match(
            type=["STATUSTEXT", "GLOBAL_POSITION_INT"],
            blocking=True,
            timeout=2,
        )
        if poll is not None:
            poll_messages(mav, poll)
        if msg is None:
            time.sleep(0.05)
            continue
        if msg.get_type() == "STATUSTEXT":
            text = (getattr(msg, "text", None) or "").strip()
            if text and any(k in text for k in ("using GPS", "EKF", "PreArm", "Arming")):
                print("[C2] STATUSTEXT: %s" % text, flush=True)
            if "using GPS" in text:
                ekf_gps_seen = True
        if msg.get_type() == "GLOBAL_POSITION_INT" and msg.lat:
            gps_count += 1
        gps_ok = ekf_gps_seen and gps_count >= 3
        if not gps_ok and gps_count >= 3 and (time.monotonic() - t0) >= 10:
            gps_ok = True
        if not gps_ok and gps_count >= 8 and (time.monotonic() - t0) >= 5:
            gps_ok = True
        if gps_ok:
            if ready_at is None:
                ready_at = time.monotonic()
            if min_settle_s <= 0 or (time.monotonic() - ready_at) >= min_settle_s:
                return
    raise TimeoutError("SITL not ready to arm within %ss" % timeout_s)


def _mav_result_name(result: int) -> str:
    m = mavutil.mavlink
    names = {
        m.MAV_RESULT_ACCEPTED: "ACCEPTED",
        m.MAV_RESULT_TEMPORARILY_REJECTED: "TEMPORARILY_REJECTED",
        m.MAV_RESULT_DENIED: "DENIED",
        m.MAV_RESULT_UNSUPPORTED: "UNSUPPORTED",
        m.MAV_RESULT_FAILED: "FAILED",
        m.MAV_RESULT_IN_PROGRESS: "IN_PROGRESS",
    }
    cancelled = getattr(m, "MAV_RESULT_CANCELLED", None)
    if cancelled is not None:
        names[cancelled] = "CANCELLED"
    return names.get(result, "RESULT_%s" % result)


def _log_arm_diag(diag: dict[str, Any], attempt: int, attempts: int) -> None:
    texts = diag.get("statustext") or []
    acks = diag.get("command_acks") or []
    print(
        "[C2] arm attempt %s/%s failed after %.0f ms"
        % (attempt, attempts, diag.get("elapsed_ms", 0)),
        flush=True,
    )
    for line in texts[-8:]:
        print("[C2]   STATUSTEXT: %s" % line, flush=True)
    for ack in acks[-4:]:
        print(
            "[C2]   COMMAND_ACK ARM result=%s (%s)"
            % (ack.get("result"), ack.get("result_name")),
            flush=True,
        )
    if diag.get("last_heartbeat_armed") is not None:
        print(
            "[C2]   last HEARTBEAT armed=%s custom_mode=%s"
            % (diag.get("last_heartbeat_armed"), diag.get("last_custom_mode")),
            flush=True,
        )
    if diag.get("timeout"):
        print("[C2]   no ARM ack/HEARTBEAT armed before timeout", flush=True)


def _log_command_failure(
    label: str,
    cmd: int,
    attempt: int,
    attempts: int,
    lat_ms: float,
    mav: mavutil.mavfile,
    poll: Optional[MissionPollState],
) -> None:
    print(
        "[C2] %s attempt %s/%s failed after %.0f ms (cmd=%s)"
        % (label, attempt, attempts, lat_ms, cmd),
        flush=True,
    )
    _drain_statustext(mav, poll, window_s=1.5)
    if poll is not None:
        for _, c, r in poll.acks:
            if c == cmd:
                print(
                    "[C2]   buffered COMMAND_ACK result=%s (%s)"
                    % (r, _mav_result_name(r)),
                    flush=True,
                )


def _drain_statustext(
    mav: mavutil.mavfile,
    poll: Optional[MissionPollState],
    window_s: float = 1.0,
) -> None:
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        if poll is not None:
            poll_messages(mav, poll)
        msg = mav.recv_match(type="STATUSTEXT", blocking=True, timeout=0.25)
        if msg is None:
            continue
        text = (getattr(msg, "text", None) or "").strip()
        if text:
            print("[C2] STATUSTEXT: %s" % text, flush=True)


def _heartbeat_armed(msg: Any) -> bool:
    return bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)


def ensure_disarmed_sitl(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    timeout_s: float = 8.0,
) -> None:
    """Disarm if the previous run left the copter armed (SITL persists across runs)."""
    cmd = mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
    armed: Optional[bool] = None
    deadline = time.monotonic() + min(timeout_s, 3.0)
    while time.monotonic() < deadline:
        poll_messages(mav, poll)
        msg = mav.recv_match(type="HEARTBEAT", blocking=False)
        if msg is not None and msg.get_srcSystem() == mav.target_system:
            armed = _heartbeat_armed(msg)
            break
        time.sleep(0.05)
    if armed is not True:
        return
    print("[C2] disarming stale armed state from prior session", flush=True)
    _drop_buffered_acks(poll, cmd)
    mav.mav.command_long_send(
        mav.target_system,
        mav.target_component,
        cmd,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        poll_messages(mav, poll)
        for i in range(len(poll.acks)):
            _, c, r = poll.acks[i]
            if c != cmd:
                continue
            poll.acks.pop(i)
            print(
                "[C2] COMMAND_ACK DISARM result=%s (%s)"
                % (r, _mav_result_name(r)),
                flush=True,
            )
            if r == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                poll_sleep(mav, 0.5, poll)
                return
            break
        msg = mav.recv_match(type="HEARTBEAT", blocking=False)
        if msg is not None and msg.get_srcSystem() == mav.target_system:
            if not _heartbeat_armed(msg):
                print("[C2] disarmed (HEARTBEAT)", flush=True)
                return
        time.sleep(0.05)
    print("[C2] disarm timeout — continuing with forced arm", flush=True)


def force_arm_sitl(
    mav: mavutil.mavfile,
    timeout_s: float = 10.0,
    poll: Optional[MissionPollState] = None,
) -> tuple[bool, float, dict[str, Any]]:
    """Arm with force flag (21196) — success requires COMMAND_ACK ACCEPTED."""
    cmd = mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
    t0 = time.monotonic_ns()
    diag: dict[str, Any] = {
        "statustext": [],
        "command_acks": [],
        "last_heartbeat_armed": None,
        "last_custom_mode": None,
        "timeout": False,
        "elapsed_ms": 0.0,
    }
    _drop_buffered_acks(poll, cmd)
    mav.mav.command_long_send(
        mav.target_system,
        mav.target_component,
        cmd,
        0,
        1,
        21196,
        0,
        0,
        0,
        0,
        0,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if poll is not None:
            poll_messages(mav, poll)
            for i in range(len(poll.acks)):
                _, c, r = poll.acks[i]
                if c != cmd:
                    continue
                poll.acks.pop(i)
                lat_ms = (time.monotonic_ns() - t0) / 1e6
                result_name = _mav_result_name(r)
                diag["command_acks"].append({"result": r, "result_name": result_name})
                print("[C2] COMMAND_ACK ARM result=%s (%s)" % (r, result_name), flush=True)
                if r == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    diag["elapsed_ms"] = lat_ms
                    poll_sleep(mav, 0.3, poll)
                    return True, lat_ms, diag
                if r in (
                    mavutil.mavlink.MAV_RESULT_DENIED,
                    mavutil.mavlink.MAV_RESULT_FAILED,
                    mavutil.mavlink.MAV_RESULT_TEMPORARILY_REJECTED,
                ):
                    diag["elapsed_ms"] = lat_ms
                    return False, lat_ms, diag
                break
            _drain_statustext_once(mav, poll, diag)
            time.sleep(0.02)
            continue
        msg = mav.recv_match(
            type=["COMMAND_ACK", "STATUSTEXT"],
            blocking=True,
            timeout=1,
        )
        if msg is None:
            continue
        if msg.get_type() == "STATUSTEXT":
            text = (msg.text or "").strip()
            if text:
                diag["statustext"].append(text)
                if any(k in text.lower() for k in ("prearm", "arm", "ekf", "gps", "fail", "error")):
                    print("[C2] STATUSTEXT: %s" % text, flush=True)
            continue
        if msg.command != cmd:
            continue
        lat_ms = (time.monotonic_ns() - t0) / 1e6
        result_name = _mav_result_name(msg.result)
        diag["command_acks"].append({"result": msg.result, "result_name": result_name})
        print("[C2] COMMAND_ACK ARM result=%s (%s)" % (msg.result, result_name), flush=True)
        if msg.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
            diag["elapsed_ms"] = lat_ms
            return True, lat_ms, diag
        if msg.result in (
            mavutil.mavlink.MAV_RESULT_DENIED,
            mavutil.mavlink.MAV_RESULT_FAILED,
            mavutil.mavlink.MAV_RESULT_TEMPORARILY_REJECTED,
        ):
            diag["elapsed_ms"] = lat_ms
            return False, lat_ms, diag
    diag["timeout"] = True
    diag["elapsed_ms"] = (time.monotonic_ns() - t0) / 1e6
    return False, diag["elapsed_ms"], diag


def _drain_statustext_once(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    diag: dict[str, Any],
) -> None:
    msg = mav.recv_match(type="STATUSTEXT", blocking=False)
    if msg is None:
        return
    text = (getattr(msg, "text", None) or "").strip()
    if not text:
        return
    diag["statustext"].append(text)
    if any(k in text.lower() for k in ("prearm", "arm", "ekf", "gps", "fail", "error")):
        print("[C2] STATUSTEXT: %s" % text, flush=True)


def arm_sitl_with_retries(
    mav: mavutil.mavfile,
    *,
    poll: Optional[MissionPollState] = None,
    attempts: int = 3,
    timeout_s: float = 15.0,
    retry_pause_s: float = 2.0,
) -> tuple[bool, float]:
    """Arm with logging and retries (waypoint + setpoint-stream)."""
    if poll is not None:
        ensure_disarmed_sitl(mav, poll)
    last_lat_ms = 0.0
    for attempt in range(1, max(1, attempts) + 1):
        print("[C2] arm attempt %s/%s (timeout=%.0fs)" % (attempt, attempts, timeout_s), flush=True)
        ok, lat_ms, diag = force_arm_sitl(mav, timeout_s=timeout_s, poll=poll)
        last_lat_ms = lat_ms
        if ok:
            return True, lat_ms
        _log_arm_diag(diag, attempt, attempts)
        if attempt >= attempts:
            break
        if poll is not None:
            poll_messages(mav, poll)
            # PreArm/EKF often needs extra settle after the first FAILED on a cold link.
            try:
                wait_sitl_ready_to_arm(mav, timeout_s=30.0, min_settle_s=8.0)
            except TimeoutError:
                pass
            poll_sleep(mav, retry_pause_s, poll)
        else:
            time.sleep(retry_pause_s)
    return False, last_lat_ms


def takeoff_sitl_with_retries(
    mav: mavutil.mavfile,
    alt_m: float,
    poll: MissionPollState,
    *,
    attempts: int = 3,
    timeout_s: float = 15.0,
    retry_pause_s: float = 2.0,
) -> tuple[bool, float]:
    """TAKEOFF with logging and retries."""
    cmd = mavutil.mavlink.MAV_CMD_NAV_TAKEOFF
    last_lat_ms = 0.0
    for attempt in range(1, max(1, attempts) + 1):
        print(
            "[C2] takeoff attempt %s/%s alt=%.1fm (timeout=%.0fs)"
            % (attempt, attempts, alt_m, timeout_s),
            flush=True,
        )
        ok, lat_ms = send_command(
            mav,
            cmd,
            0, 0, 0, 0, 0, 0,
            alt_m,
            timeout_s=timeout_s,
            poll=poll,
        )
        last_lat_ms = lat_ms
        if ok:
            print("[C2] takeoff accepted in %.0f ms" % lat_ms, flush=True)
            return True, lat_ms
        _log_command_failure("takeoff", cmd, attempt, attempts, lat_ms, mav, poll)
        if attempt >= attempts:
            break
        poll_sleep(mav, retry_pause_s, poll)
    return False, last_lat_ms


# --- SITL bootstrap (arm / takeoff / land) ---


def goto_global(
    mav: mavutil.mavfile,
    lat: float,
    lon: float,
    alt_m: float,
) -> None:
    mav.mav.set_position_target_global_int_send(
        0,
        mav.target_system,
        mav.target_component,
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        0b0000111111111000,
        int(lat * 1e7),
        int(lon * 1e7),
        alt_m,
        0, 0, 0,
        0, 0, 0,
        0, 0,
    )


def current_position(
    mav: mavutil.mavfile,
    cache: Optional[list[Optional[tuple[float, float, float]]]] = None,
) -> Optional[tuple[float, float, float]]:
    msg = mav.recv_match(type="GLOBAL_POSITION_INT", blocking=False)
    if msg is not None and msg.lat:
        pos = msg.lat / 1e7, msg.lon / 1e7, msg.alt / 1000.0
        if cache is not None:
            cache[0] = pos
        return pos
    if cache is not None and cache[0] is not None:
        return cache[0]
    return None


def wait_at_point(
    mav: mavutil.mavfile,
    target_lat: float,
    target_lon: float,
    accept_m: float,
    hold_s: float,
    timeout_s: float,
    poll: MissionPollState,
) -> None:
    t_start = time.monotonic()
    arrived = False
    arrive_t = None
    while time.monotonic() - t_start < timeout_s:
        poll_messages(mav, poll)
        pos = current_position(mav, poll.pos_cache)
        if pos:
            lat, lon, alt = pos
            dist = haversine_m(lat, lon, target_lat, target_lon)
            if poll.phys.samples:
                poll.phys.max_deviation_m = max(poll.phys.max_deviation_m, dist)
            if not arrived and dist <= accept_m:
                arrived = True
                arrive_t = time.monotonic()
            if arrived and arrive_t and (time.monotonic() - arrive_t) >= hold_s:
                return
        time.sleep(0.05)
    raise TimeoutError(
        "waypoint timeout target=(%.7f, %.7f) accept=%.1fm" % (target_lat, target_lon, accept_m)
    )


def poll_sleep(mav: mavutil.mavfile, seconds: float, poll: MissionPollState) -> None:
    """Sleep while still draining MAVLink — avoids false heartbeat gaps."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ensure_gcs_heartbeat(mav, poll)
        poll_messages(mav, poll)
        time.sleep(0.05)


def begin_steady_state(poll: MissionPollState) -> float:
    """Start attribution window after bootstrap (connect/EKF/arm/climb)."""
    poll.measure = True
    poll.last_hb[0] = 0.0
    poll.c2.heartbeats_seen = 0
    poll.c2.heartbeat_gaps_over_threshold = 0
    poll.c2.max_heartbeat_gap_s = 0.0
    return time.monotonic() - poll.run_t0


# Thresholds for graded command-response metric.
RESP_ONSET_SPEED_MS = 0.5   # horizontal speed marking "started moving"
RESP_SETTLE_RADIUS_M = 2.0  # radius to consider target "reached"
SETPOINT_MATCH_M = 1.5      # tolerance to match POSITION_TARGET with command target


# --- In-flight polling (heartbeat gaps, NED, flight_state) ---


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


TELEMETRY_DOWNLINK_CSV_FIELDS = [
    "site", "t_mono_ns", "t_rel_s", "mavlink_sequence", "mavlink_system_id",
    "time_boot_ms", "x", "y", "z",
]


def write_mavlink_rx_gcs_csv(out_dir: Path, rows: list[dict]) -> Path:
    """All UAV→GCS MAVLink arrivals (K8), excluding RADIO_STATUS."""
    path = out_dir / "mavlink_rx_gcs.csv"
    fields = [
        "t_mono_ns",
        "t_rel_s",
        "mavlink_msg_type",
        "mavlink_system_id",
        "mavlink_component_id",
        "mavlink_sequence",
        "steady_state",
        "leg",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def write_gcs_hb_sent_csv(out_dir: Path, rows: list[dict]) -> Path:
    path = out_dir / "gcs_hb_sent.csv"
    fields = ["t_mono_ns", "t_rel_s", "steady_state", "mavlink_msg_type", "leg", "site"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def write_statustext_csv(out_dir: Path, rows: list[dict]) -> Path:
    path = out_dir / "statustext_gcs.csv"
    fields = ["t_mono_ns", "t_rel_s", "severity", "text", "steady_state"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def write_heartbeat_csv(out_dir: Path, rows: list[dict]) -> Path:
    path = out_dir / "heartbeat.csv"
    if not rows:
        return path
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path


def write_telemetry_downlink_gcs_csv(out_dir: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path = out_dir / "telemetry_downlink_gcs.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TELEMETRY_DOWNLINK_CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


def request_flight_state_streams(mav: mavutil.mavfile) -> None:
    """Best-effort: raise internal telemetry rate to capture flight state.
    Never raises — if SITL ignores a request, continue with whatever arrives."""
    streams = [
        (mavutil.mavlink.MAV_DATA_STREAM_POSITION, 10),   # GLOBAL_POSITION_INT
        (mavutil.mavlink.MAV_DATA_STREAM_EXTRA1, 10),     # ATTITUDE
        (mavutil.mavlink.MAV_DATA_STREAM_EXTRA2, 5),      # VFR_HUD
        (mavutil.mavlink.MAV_DATA_STREAM_EXTRA3, 3),      # EKF_STATUS_REPORT, etc.
    ]
    for stream_id, hz in streams:
        try:
            mav.mav.request_data_stream_send(
                mav.target_system, mav.target_component, stream_id, hz, 1,
            )
        except Exception:
            pass
    # POSITION_TARGET_GLOBAL_INT has no reliable stream group — request by message ID.
    for msg_id, hz in (
        (mavutil.mavlink.MAVLINK_MSG_ID_POSITION_TARGET_GLOBAL_INT, 5),
        (mavutil.mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED, 20),
    ):
        try:
            mav.mav.command_long_send(
                mav.target_system, mav.target_component,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                msg_id, int(1e6 / hz), 0, 0, 0, 0, 0,
            )
        except Exception:
            pass


def _collect_flight_state(
    msg: Any, mtype: str, poll: MissionPollState, now: float, t_rel: float,
) -> None:
    """Collect internal ArduPilot telemetry (tracking, attitude, EKF, response).
    Fully guarded: any error here must NEVER break the message pump."""
    phys = poll.phys
    measure = poll.measure

    if mtype == "LOCAL_POSITION_NED":
        poll.ned_cache[0] = (float(msg.x), float(msg.y), float(msg.z))
        if poll.telemetry_capture and poll.measure:
            poll.telemetry_rows.append({
                "site": "gcs",
                "t_mono_ns": time.monotonic_ns(),
                "t_rel_s": round(t_rel, 4),
                "mavlink_sequence": _mavlink_sequence(msg),
                "mavlink_system_id": msg.get_srcSystem(),
                "time_boot_ms": getattr(msg, "time_boot_ms", None),
                "x": float(msg.x),
                "y": float(msg.y),
                "z": float(msg.z),
            })
        return

    if mtype == "POSITION_TARGET_GLOBAL_INT":
        slat, slon = msg.lat_int / 1e7, msg.lon_int / 1e7
        phys.setpoint = (slat, slon, getattr(msg, "alt", 0.0))
        ac = poll.active_cmd
        if ac is not None and ac.get("adopt_ms") is None:
            d = haversine_m(slat, slon, ac["tlat"], ac["tlon"])
            if d <= SETPOINT_MATCH_M:
                ac["adopt_ms"] = (now - ac["t_send"]) * 1000.0
        return

    if mtype == "GLOBAL_POSITION_INT" and msg.lat:
        lat, lon = msg.lat / 1e7, msg.lon / 1e7
        gspeed = math.hypot(getattr(msg, "vx", 0) or 0, getattr(msg, "vy", 0) or 0) / 100.0
        if phys.setpoint is not None:
            terr = haversine_m(lat, lon, phys.setpoint[0], phys.setpoint[1])
            phys.last_track_err_m = terr
            if measure:
                phys.track_errors_m.append(terr)
        if measure and poll.nav_leg_from is not None and poll.nav_leg_to is not None:
            lf = poll.nav_leg_from
            lt = poll.nav_leg_to
            xte = cross_track_distance_m(lat, lon, lf[0], lf[1], lt[0], lt[1])
            phys.last_cross_track_m = xte
            phys.cross_track_errors_m.append(xte)
        ac = poll.active_cmd
        if ac is not None:
            if ac.get("onset_ms") is None and gspeed >= RESP_ONSET_SPEED_MS:
                ac["onset_ms"] = (now - ac["t_send"]) * 1000.0
            if ac.get("settle_ms") is None:
                d = haversine_m(lat, lon, ac["tlat"], ac["tlon"])
                if d <= RESP_SETTLE_RADIUS_M:
                    ac["settle_ms"] = (now - ac["t_send"]) * 1000.0
        return

    if mtype == "ATTITUDE":
        roll = math.degrees(msg.roll)
        pitch = math.degrees(msg.pitch)
        if measure:
            phys.att_roll_deg.append(roll)
            phys.att_pitch_deg.append(pitch)
            phys.att_rollrate.append(msg.rollspeed)
            phys.att_pitchrate.append(msg.pitchspeed)
            phys.att_yawrate.append(msg.yawspeed)
            phys.flight_rows.append({
                "t_rel_s": round(t_rel, 3),
                "roll_deg": round(roll, 3),
                "pitch_deg": round(pitch, 3),
                "yaw_deg": round(math.degrees(msg.yaw), 3),
                "rollrate": round(msg.rollspeed, 4),
                "pitchrate": round(msg.pitchspeed, 4),
                "yawrate": round(msg.yawspeed, 4),
                "track_err_m": round(phys.last_track_err_m, 3) if phys.last_track_err_m is not None else "",
                "cross_track_m": round(phys.last_cross_track_m, 3) if phys.last_cross_track_m is not None else "",
                "ekf_vel_var": phys.last_ekf.get("vel", ""),
                "ekf_pos_horiz_var": phys.last_ekf.get("pos_h", ""),
            })
        return

    if mtype == "EKF_STATUS_REPORT":
        vel = getattr(msg, "velocity_variance", None)
        pos_h = getattr(msg, "pos_horiz_variance", None)
        pos_v = getattr(msg, "pos_vert_variance", None)
        comp = getattr(msg, "compass_variance", None)
        phys.last_ekf = {"vel": vel, "pos_h": pos_h, "pos_v": pos_v, "comp": comp}
        if measure:
            if vel is not None:
                phys.ekf_vel_var.append(vel)
            if pos_h is not None:
                phys.ekf_pos_horiz_var.append(pos_h)
            if pos_v is not None:
                phys.ekf_pos_vert_var.append(pos_v)
            if comp is not None:
                phys.ekf_compass_var.append(comp)
        return


def capture_final_flight_snapshot(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    timeout_s: float = 15.0,
) -> None:
    """Pump HEARTBEAT/position until disarmed on ground or timeout (updates last_armed/mode)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        poll_messages(mav, poll)
        rel_alt = poll.rel_alt_cache[0]
        if poll.phys.last_armed is False:
            if rel_alt is None or rel_alt <= 0.5:
                return
        poll_sleep(mav, 0.2, poll)
    poll_messages(mav, poll)


def poll_messages(mav: mavutil.mavfile, poll: MissionPollState) -> None:
    ensure_gcs_heartbeat(mav, poll)
    while True:
        msg = mav.recv_match(blocking=False)
        if msg is None:
            break
        now = time.monotonic()
        t_rel = now - poll.run_t0
        mtype = msg.get_type()
        # K8: interarrival of all UAV→GCS messages except RADIO_STATUS.
        if mtype != "RADIO_STATUS" and mtype != "BAD_DATA":
            poll.link_rx_log.append({
                "t_mono_ns": time.monotonic_ns(),
                "t_rel_s": round(t_rel, 4),
                "mavlink_msg_type": mtype,
                "mavlink_system_id": msg.get_srcSystem(),
                "mavlink_component_id": msg.get_srcComponent(),
                "mavlink_sequence": _mavlink_sequence(msg),
                "steady_state": poll.measure,
                "leg": "return",
            })
        if mtype == "STATUSTEXT":
            try:
                text = msg.text if isinstance(msg.text, str) else msg.text.decode("utf-8", "replace")
            except Exception:
                text = str(getattr(msg, "text", ""))
            poll.status_text_log.append({
                "t_mono_ns": time.monotonic_ns(),
                "t_rel_s": round(t_rel, 3),
                "severity": int(getattr(msg, "severity", -1)),
                "text": text.strip(),
                "steady_state": poll.measure,
            })
        try:
            _collect_flight_state(msg, mtype, poll, now, t_rel)
        except Exception:
            pass  # rich telemetry is best-effort: never interrupts the pump
        if mtype == "HEARTBEAT" and msg.get_srcSystem() == mav.target_system:
            gap: Optional[float] = None
            if poll.last_hb[0] > 0:
                gap = now - poll.last_hb[0]
            armed = _heartbeat_armed(msg)
            poll.phys.last_armed = armed
            poll.hb_log.append({
                "t_rel_s": round(t_rel, 3),
                "steady_state": poll.measure,
                "gap_since_prev_s": round(gap, 3) if gap is not None else None,
                "armed": armed,
            })
            if poll.measure:
                poll.c2.heartbeats_seen += 1
                if gap is not None:
                    poll.c2.max_heartbeat_gap_s = max(poll.c2.max_heartbeat_gap_s, gap)
                    if gap > poll.thr.heartbeat_loss_gap_s:
                        poll.c2.heartbeat_gaps_over_threshold += 1
            poll.last_hb[0] = now
            mode = mavutil.mode_string_v10(msg)
            if mode != poll.phys.last_mode:
                if poll.phys.last_mode is not None:
                    poll.phys.mode_transitions.append({
                        "t_rel_s": round(t_rel, 3),
                        "from": poll.phys.last_mode,
                        "to": mode,
                        "steady_state": poll.measure,
                    })
                poll.phys.last_mode = mode
            if mode in ("RTL", "LAND", "SMART_RTL", "AUTO_RTL") and not poll.c2.failsafe_triggered:
                if mode == "LAND" and (poll.landing_requested or poll.intentional_land):
                    pass
                elif poll.measure or t_rel > poll.thr.warmup_discard_s:
                    poll.c2.failsafe_triggered = True
                    poll.c2.failsafe_mode = mode
                    poll.c2.failsafe_time_s = t_rel
        if mtype == "COMMAND_ACK":
            poll.acks.append((now, int(msg.command), int(msg.result)))
        if mtype == "GLOBAL_POSITION_INT" and msg.lat:
            lat, lon = msg.lat / 1e7, msg.lon / 1e7
            alt = msg.alt / 1000.0
            poll.pos_cache[0] = (lat, lon, alt)
            poll.rel_alt_cache[0] = msg.relative_alt / 1000.0
            dev = haversine_m(lat, lon, poll.planned[0], poll.planned[1])
            poll.phys.max_deviation_m = max(poll.phys.max_deviation_m, dev)
            poll.phys.samples += 1
            if poll.measure or t_rel >= poll.thr.warmup_discard_s:
                poll.log_rows.append({
                    "t_mono_ns": time.monotonic_ns(),
                    "t_rel_s": round(t_rel, 3),
                    "lat": lat,
                    "lon": lon,
                    "alt_m": alt,
                    "planned_lat": poll.planned[0],
                    "planned_lon": poll.planned[1],
                    "deviation_m": round(dev, 2),
                })


def validate_c2_over_5g(connection: str, bind_ip: str) -> None:
    """Reject loopback / host shortcuts — FlyBlind C2 must traverse PDU/UPF/GTP.

    C2 over 5G is UDP-only (MAVLink). TCP MAVLink on the PDU path was removed.
    """
    low = connection.lower()
    for bad in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        if bad in low:
            raise SystemExit(
                "FlyBlind C2 must use UAV PDU IP over 5G (UPF/GTP), not %r in %r"
                % (bad, connection)
            )
    if connection.startswith("tcp:"):
        raise SystemExit(
            "TCP C2 over 5G removed — use udp:UAV_PDU_IP:5760 (MAVLink over UDP). "
            "SITL still uses localhost TCP:5761 behind the UDP relay (socat/forwarder)."
        )
    if bind_ip and bind_ip.split(".")[0] == "127":
        raise SystemExit("FlyBlind --bind-ip must be GCS PDU IP, not loopback")


class _SourceBoundTCP(mavutil.mavtcp):
    """Legacy TCP bind helper — unused (C2 over 5G is UDP-only). Kept for import safety."""

    def __init__(self, device: str, source_ip: str, **kwargs: Any) -> None:
        raise SystemExit(
            "TCP C2 over 5G removed — use udp:UAV_PDU_IP:5760 (MAVLink over UDP)."
        )


def make_mavlink_connection(connection: str, bind_ip: str = "") -> mavutil.mavfile:
    validate_c2_over_5g(connection, bind_ip)
    if bind_ip:
        udp_conn = connection
        if not udp_conn.startswith("udpout:"):
            rest = udp_conn.replace("udp:", "", 1)
            udp_conn = "udpout:" + rest
        return mavutil.mavlink_connection(udp_conn, source_address=(bind_ip, 0))
    return mavutil.mavlink_connection(connection)


# --- Main mission loop (waypoint workload) ---


def run_mission(
    connection: str,
    mission_path: Path,
    thresholds_path: Path,
    out_dir: Path,
    bind_ip: str = "",
    attack_trigger: str = "",
) -> dict[str, Any]:
    mission = load_json(mission_path)
    thr_d = load_json(thresholds_path)
    thr = Thresholds(
        heartbeat_loss_gap_s=float(thr_d.get("heartbeat_loss_gap_s", 1.5)),
        command_ack_timeout_s=float(thr_d.get("command_ack_timeout_s", 3.0)),
        command_late_ack_ms=float(thr_d.get("command_late_ack_ms", 500)),
        warmup_discard_s=float(thr_d.get("warmup_discard_s", 5.0)),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    c2 = C2Stats()
    phys = PhysicsStats()
    run_t0 = time.monotonic()
    poll = MissionPollState(
        run_t0=run_t0,
        log_rows=[],
        hb_log=[],
        c2=c2,
        phys=phys,
        planned=(0.0, 0.0),
        thr=thr,
        last_hb=[0.0],
        pos_cache=[None],
    )

    mav = open_c2_mavlink(connection, bind_ip)

    home_lat, home_lon, _ = get_home(mav)
    poll.planned = (home_lat, home_lon)
    wait_sitl_ready_to_arm(mav)
    ensure_sitl_landed_and_disarmed(mav, poll)
    set_mode_guided(mav)
    request_flight_state_streams(mav)  # raise ATTITUDE/EKF/POSITION_TARGET rate (best-effort)
    poll_sleep(mav, 2.0, poll)

    # Per-command flight log (one row per ACK-able C2 command).
    cmd_rows: list[dict[str, Any]] = []
    cmd_seq = 0

    def record_cmd(cmd_type: str, ok: bool, lat_ms: float, t_mono_ns: int, t_rel: float) -> None:
        nonlocal cmd_seq
        cmd_seq += 1
        c2.commands_sent += 1
        late = 0
        if ok:
            c2.commands_acked += 1
            c2.command_latencies_ms.append(lat_ms)
            if lat_ms > thr.command_late_ack_ms:
                c2.commands_late += 1
                late = 1
        else:
            c2.commands_lost += 1
        cmd_rows.append({
            "seq": cmd_seq,
            "t_rel_s": round(t_rel, 3),
            "t_mono_ns": t_mono_ns,
            "cmd_type": cmd_type,
            "sent": 1,
            "acked": 1 if ok else 0,
            "ack_latency_ms": round(lat_ms, 3) if ok else "",
            "lost": 0 if ok else 1,
            "late": late,
        })

    # Arm (force for SITL — retry while EKF finishes)
    arm_t0 = time.monotonic_ns()
    arm_rel = time.monotonic() - run_t0
    ok, lat_ms = arm_sitl_with_retries(mav, poll=poll, attempts=3, timeout_s=15.0)
    if ok:
        record_cmd("ARM", True, lat_ms, arm_t0, arm_rel)
    else:
        record_cmd("ARM", False, 0.0, time.monotonic_ns(), time.monotonic() - run_t0)
        raise RuntimeError("arm failed after retries")

    # Takeoff
    alt = float(mission["takeoff_alt_m"])
    tk_t0 = time.monotonic_ns()
    tk_rel = time.monotonic() - run_t0
    ok, lat_ms = takeoff_sitl_with_retries(
        mav, alt, poll,
        attempts=3,
        timeout_s=max(15.0, thr.command_ack_timeout_s),
    )
    record_cmd("TAKEOFF", ok, lat_ms, tk_t0, tk_rel)
    if not ok:
        raise RuntimeError("takeoff failed after retries")

    poll_sleep(mav, 8.0, poll)  # climb — polled (no blind sleep)
    steady_start_s = begin_steady_state(poll)

    # Steady-state navigation: repeated DO_REPOSITION commands cycling the mission
    # waypoints. Interval is jittered (uniform) so command timing does not resonate
    # with the ~60ms pulse-attack period, which would bias the measured loss rate.
    steady = mission.get("steady", {}) or {}
    n_cmds = int(steady.get("commands", 24))
    interval = steady.get("interval_s", [4.0, 6.0])
    lo, hi = float(interval[0]), float(interval[1])
    cmd_alt = float(steady.get("alt_m", alt))
    rng = random.Random(steady.get("seed"))
    waypoints = mission.get("waypoints") or [{"lat": 0.0, "lon": 0.0}]

    # Synchronized C2 attack (sync-c2): fire a tiny trigger to the ROGUE over the
    # pod network (CNI, outside 5G) at the EXACT instant of each command, so the ROGUE
    # concentrates a burst in the command round-trip window. Best-effort: if the socket
    # fails, the mission continues normally (the trigger must never break flight).
    trig_sock = None
    trig_addrs: list[tuple[str, int]] = []
    trig_fired = 0
    if attack_trigger:
        try:
            for endpoint in attack_trigger.split(","):
                thost, tport = endpoint.strip().rsplit(":", 1)
                trig_addrs.append((thost, int(tport)))
            trig_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            print("[C2-SYNC] attack trigger -> %s (%d targets, per command)"
                  % (attack_trigger, len(trig_addrs)), flush=True)
        except Exception:
            trig_sock = None
            trig_addrs = []

    def fire_trigger() -> None:
        nonlocal trig_fired
        if trig_sock is not None:
            try:
                for trig_addr in trig_addrs:
                    trig_sock.sendto(b"C", trig_addr)
                    trig_fired += 1
            except Exception:
                pass

    for i in range(n_cmds):
        wp = waypoints[i % len(waypoints)]
        tlat = home_lat + float(wp["lat"])
        tlon = home_lon + float(wp["lon"])
        poll.planned = (tlat, tlon)
        # Close previous command response and open the new one (measured by poll_messages).
        if poll.active_cmd is not None:
            poll.phys.response_rows.append(poll.active_cmd)
        cur = poll.pos_cache[0]
        # Cross-track: line from current position (command instant) to this DO_REPOSITION target.
        leg_from = (cur[0], cur[1]) if cur else (home_lat, home_lon)
        poll.nav_leg_from = leg_from
        poll.nav_leg_to = (tlat, tlon)
        poll.active_cmd = {
            "seq": cmd_seq + 1,
            "t_send": time.monotonic(),
            "tlat": tlat,
            "tlon": tlon,
            "dist0_m": round(haversine_m(cur[0], cur[1], tlat, tlon), 2) if cur else None,
            "adopt_ms": None,
            "onset_ms": None,
            "settle_ms": None,
        }
        fire_trigger()  # sync ROGUE burst with command instant
        c_t0 = time.monotonic_ns()
        c_rel = time.monotonic() - run_t0
        ok, lat_ms = send_reposition(
            mav, tlat, tlon, cmd_alt,
            timeout_s=thr.command_ack_timeout_s,
            poll=poll,
        )
        record_cmd("DO_REPOSITION", ok, lat_ms, c_t0, c_rel)
        poll_sleep(mav, rng.uniform(lo, hi), poll)
    if poll.active_cmd is not None:  # close last in-flight command
        poll.phys.response_rows.append(poll.active_cmd)
        poll.active_cmd = None

    if mission.get("land_after", True):
        poll.landing_requested = True
        l_t0 = time.monotonic_ns()
        l_rel = time.monotonic() - run_t0
        ok, lat_ms = send_command(
            mav,
            mavutil.mavlink.MAV_CMD_NAV_LAND,
            timeout_s=thr.command_ack_timeout_s,
            poll=poll,
        )
        record_cmd("LAND", ok, lat_ms, l_t0, l_rel)
        poll_sleep(mav, 10.0, poll)
        capture_final_flight_snapshot(mav, poll, timeout_s=15.0)

    # Write CSV trajectory + heartbeat forensics + per-command log
    csv_path = out_dir / "trajectory.csv"
    if poll.log_rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(poll.log_rows[0].keys()))
            w.writeheader()
            w.writerows(poll.log_rows)
    hb_path = write_heartbeat_csv(out_dir, poll.hb_log)
    link_rx_path = write_mavlink_rx_gcs_csv(out_dir, poll.link_rx_log)
    gcs_hb_path = write_gcs_hb_sent_csv(out_dir, poll.gcs_hb_sent_log)
    status_path = write_statustext_csv(out_dir, poll.status_text_log)
    commands_csv_path = out_dir / "commands.csv"
    cmd_fields = [
        "seq", "t_rel_s", "t_mono_ns", "cmd_type",
        "sent", "acked", "ack_latency_ms", "lost", "late",
    ]
    with commands_csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cmd_fields)
        w.writeheader()
        w.writerows(cmd_rows)

    def pct(vals: list[float], p: float) -> Optional[float]:
        if not vals:
            return None
        s = sorted(vals)
        idx = int(round((p / 100.0) * (len(s) - 1)))
        return s[idx]

    def _mean(vals: list[float]) -> Optional[float]:
        return sum(vals) / len(vals) if vals else None

    def _stdev(vals: list[float]) -> Optional[float]:
        if len(vals) < 2:
            return None
        m = sum(vals) / len(vals)
        return math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))

    def _rms(vals: list[float]) -> Optional[float]:
        return math.sqrt(sum(x * x for x in vals) / len(vals)) if vals else None

    def _r(x: Optional[float], nd: int = 3) -> Optional[float]:
        return round(x, nd) if isinstance(x, (int, float)) else None

    # --- flight_state.csv (attitude/tracking series) + response.csv (per command) ---
    if phys.flight_rows:
        fs_csv = out_dir / "flight_state.csv"
        with fs_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(phys.flight_rows[0].keys()))
            w.writeheader()
            w.writerows(phys.flight_rows)
    if phys.response_rows:
        resp_csv = out_dir / "response.csv"
        with resp_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=["seq", "t_send", "tlat", "tlon", "dist0_m",
                               "adopt_ms", "onset_ms", "settle_ms"])
            w.writeheader()
            w.writerows(phys.response_rows)

    adopt = [r["adopt_ms"] for r in phys.response_rows if r.get("adopt_ms") is not None]
    onset = [r["onset_ms"] for r in phys.response_rows if r.get("onset_ms") is not None]
    settle = [r["settle_ms"] for r in phys.response_rows if r.get("settle_ms") is not None]
    flight_state_metrics = {
        "note": ("tracking error vs active setpoint, attitude oscillation, "
                 "graded command response; EKF tends to stay inert in SITL "
                 "(sensors local to simulator, not over C2)"),
        "tracking_error_m": {
            "mean": _r(_mean(phys.track_errors_m), 2),
            "median": _r(pct(phys.track_errors_m, 50), 2),
            "p99": _r(pct(phys.track_errors_m, 99), 2),
            "max": _r(max(phys.track_errors_m), 2) if phys.track_errors_m else None,
            "samples": len(phys.track_errors_m),
        },
        "cross_track_error_m": {
            "mean": _r(_mean(phys.cross_track_errors_m), 2),
            "median": _r(pct(phys.cross_track_errors_m, 50), 2),
            "p99": _r(pct(phys.cross_track_errors_m, 99), 2),
            "max": _r(max(phys.cross_track_errors_m), 2) if phys.cross_track_errors_m else None,
            "samples": len(phys.cross_track_errors_m),
        },
        "command_response_ms": {
            "setpoint_adopt": {"median": _r(pct(adopt, 50), 1), "p99": _r(pct(adopt, 99), 1),
                               "max": _r(max(adopt), 1) if adopt else None, "n": len(adopt)},
            "motion_onset": {"median": _r(pct(onset, 50), 1), "p99": _r(pct(onset, 99), 1),
                             "max": _r(max(onset), 1) if onset else None, "n": len(onset)},
            "settle_time": {"median": _r(pct(settle, 50), 1), "p99": _r(pct(settle, 99), 1),
                            "max": _r(max(settle), 1) if settle else None, "n": len(settle)},
            "commands_tracked": len(phys.response_rows),
        },
        "attitude": {
            "roll_std_deg": _r(_stdev(phys.att_roll_deg), 3),
            "pitch_std_deg": _r(_stdev(phys.att_pitch_deg), 3),
            "rollrate_rms": _r(_rms(phys.att_rollrate), 4),
            "pitchrate_rms": _r(_rms(phys.att_pitchrate), 4),
            "yawrate_rms": _r(_rms(phys.att_yawrate), 4),
            "samples": len(phys.att_roll_deg),
        },
        "ekf": {
            "velocity_variance_max": _r(max(phys.ekf_vel_var), 4) if phys.ekf_vel_var else None,
            "pos_horiz_variance_max": _r(max(phys.ekf_pos_horiz_var), 4) if phys.ekf_pos_horiz_var else None,
            "pos_vert_variance_max": _r(max(phys.ekf_pos_vert_var), 4) if phys.ekf_pos_vert_var else None,
            "compass_variance_max": _r(max(phys.ekf_compass_var), 4) if phys.ekf_compass_var else None,
            "samples": len(phys.ekf_vel_var),
        },
        "mode_transitions": phys.mode_transitions,
        "final_mode": phys.last_mode,
        "armed": phys.last_armed,
        "final_armed": phys.last_armed,
    }

    duration_s = round(time.monotonic() - run_t0, 2)
    steady_duration_s = round(duration_s - steady_start_s, 2)
    summary = {
        "c2_path": "5g_pdu_gtp",
        "connection": connection,
        "bind_ip": bind_ip or None,
        "mission": str(mission_path),
        "home": {"lat": home_lat, "lon": home_lon},
        "duration_s": duration_s,
        "measurement_window": {
            "name": "post_climb_steady",
            "started_at_s": round(steady_start_s, 2),
            "duration_s": steady_duration_s,
            "note": "C2 heartbeat gaps counted only after takeoff climb; bootstrap excluded",
        },
        "c2_metrics": {
            "heartbeats_seen": c2.heartbeats_seen,
            "heartbeats_expected_approx": max(0, int(steady_duration_s)),
            "heartbeat_gaps_over_threshold": c2.heartbeat_gaps_over_threshold,
            "max_heartbeat_gap_s": round(c2.max_heartbeat_gap_s, 3),
            "commands_sent": c2.commands_sent,
            "commands_acked": c2.commands_acked,
            "commands_lost": c2.commands_lost,
            "commands_late": c2.commands_late,
            "command_latency_ms": {
                "median": pct(c2.command_latencies_ms, 50),
                "p99": pct(c2.command_latencies_ms, 99),
                "max": max(c2.command_latencies_ms) if c2.command_latencies_ms else None,
            },
            "failsafe": {
                "triggered": c2.failsafe_triggered,
                "mode": c2.failsafe_mode or None,
                "time_s": c2.failsafe_time_s,
            },
        },
        "attack_sync": {"trigger_endpoint": attack_trigger or None, "triggers_fired": trig_fired},
        "physics_metrics": {
            "max_deviation_m": round(phys.max_deviation_m, 2),
            "position_samples": phys.samples,
            "flight_state": flight_state_metrics,
        },
        "thresholds": thr_d,
        "commands_csv": str(commands_csv_path),
        "artifacts": {
            "commands_csv": str(commands_csv_path),
            "trajectory_csv": str(csv_path),
            "heartbeat_csv": str(hb_path),
            "mavlink_rx_gcs_csv": str(link_rx_path),
            "gcs_hb_sent_csv": str(gcs_hb_path),
            "statustext_gcs_csv": str(status_path),
            "flight_state_csv": str(out_dir / "flight_state.csv"),
            "response_csv": str(out_dir / "response.csv"),
        },
        "link_vivacity": {
            "uav_heartbeat_at_gcs_rows": len(poll.hb_log),
            "mavlink_rx_gcs_rows": len(poll.link_rx_log),
            "gcs_hb_sent_rows": len(poll.gcs_hb_sent_log),
            "statustext_rows": len(poll.status_text_log),
            "note": (
                "UAV HEARTBEAT at GCS: heartbeat.csv (existing). "
                "All UAV→GCS msgs (K8): mavlink_rx_gcs.csv. "
                "GCS HEARTBEAT TX: gcs_hb_sent.csv; RX at UAV: proxy_link.csv."
            ),
        },
    }
    summary_path = out_dir / "mission_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def print_flight_log(summary: dict[str, Any]) -> None:
    """Human-readable end-of-mission report + head/tail of commands.csv (for on-the-spot review)."""
    c2 = summary["c2_metrics"]
    mw = summary["measurement_window"]
    lat = c2.get("command_latency_ms") or {}
    phys = summary["physics_metrics"]
    fs = c2["failsafe"]
    hb_gap_thr = (summary.get("thresholds") or {}).get("heartbeat_loss_gap_s", 1.5)

    def num(x: Any) -> str:
        return "%.3f" % x if isinstance(x, (int, float)) else "n/a"

    print("\n=== FLIGHT LOG SUMMARY ===")
    print("Duration: %ss | Steady window: start %ss, dur %ss"
          % (summary["duration_s"], mw["started_at_s"], mw["duration_s"]))
    print("Commands: sent=%d, acked=%d, lost=%d, late=%d"
          % (c2["commands_sent"], c2["commands_acked"], c2["commands_lost"], c2["commands_late"]))
    print("Command latency (ms): median=%s / p99=%s / max=%s"
          % (num(lat["median"]), num(lat["p99"]), num(lat["max"])))
    print("Heartbeat: seen=%d / expected=%d, gaps>%.1fs=%d, max gap=%.3fs"
          % (c2["heartbeats_seen"], c2["heartbeats_expected_approx"],
             hb_gap_thr, c2["heartbeat_gaps_over_threshold"], c2["max_heartbeat_gap_s"]))
    print("Trajectory: max deviation=%s m, samples=%d"
          % (num(phys.get("max_deviation_m")), phys.get("position_samples", 0)))
    if fs["triggered"]:
        print("Failsafe: yes, mode=%s, time=%ss" % (fs["mode"], fs["time_s"]))
    else:
        print("Failsafe: no")

    fstate = phys.get("flight_state") or {}
    if fstate:
        te = fstate.get("tracking_error_m") or {}
        cr = fstate.get("command_response_ms") or {}
        att = fstate.get("attitude") or {}
        adopt = cr.get("setpoint_adopt") or {}
        onset = cr.get("motion_onset") or {}
        settle = cr.get("settle_time") or {}
        print("\n=== FLIGHT STATE (internal ArduPilot telemetry) ===")
        print("Tracking error (m): mean=%s / p99=%s / max=%s (n=%s)"
              % (num(te.get("mean")), num(te.get("p99")), num(te.get("max")), te.get("samples")))
        xte = fstate.get("cross_track_error_m") or {}
        print("Cross-track (m): mean=%s / p99=%s / max=%s (n=%s)"
              % (num(xte.get("mean")), num(xte.get("p99")), num(xte.get("max")), xte.get("samples")))
        print("Command response (ms): adoption med=%s/p99=%s | onset med=%s/p99=%s | settle med=%s/p99=%s"
              % (num(adopt.get("median")), num(adopt.get("p99")),
                 num(onset.get("median")), num(onset.get("p99")),
                 num(settle.get("median")), num(settle.get("p99"))))
        print("Attitude: roll_std=%s° pitch_std=%s° | RMS rates roll=%s pitch=%s yaw=%s rad/s"
              % (num(att.get("roll_std_deg")), num(att.get("pitch_std_deg")),
                 num(att.get("rollrate_rms")), num(att.get("pitchrate_rms")),
                 num(att.get("yawrate_rms"))))
        mts = fstate.get("mode_transitions") or []
        if mts:
            print("Mode transitions: %s"
                  % ", ".join("%s→%s@%ss" % (m["from"], m["to"], m["t_rel_s"]) for m in mts))

    if summary.get("workload") == "setpoint-stream":
        print("\n=== SETPOINT STREAM ===")
        print("Setpoints generated: %s @ %.1f Hz"
              % (summary.get("n_setpoints", 0), summary.get("rate_hz", 0.0)))
        return

    csv_path = Path(summary.get("commands_csv") or (summary.get("artifacts") or {}).get("commands_csv", ""))
    if not str(csv_path):
        return
    try:
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    except OSError:
        rows = []
    print("=== COMMAND TIMELINE (first/last 5) ===")
    print("seq t_rel_s cmd_type acked latency_ms lost late")

    def show(r: dict[str, str]) -> None:
        print("%s %s %s %s %s %s %s" % (
            r["seq"], r["t_rel_s"], r["cmd_type"], r["acked"],
            r["ack_latency_ms"] or "-", r["lost"], r["late"]))

    for r in rows[:5]:
        show(r)
    if len(rows) > 10:
        print("...")
    for r in rows[-5:] if len(rows) > 5 else []:
        show(r)

    lines = csv_path.read_text(encoding="utf-8").splitlines() if csv_path.exists() else []
    print("=== commands.csv (head/tail) ===")
    for ln in lines[:11]:  # header + first 10 rows
        print(ln)
    if len(lines) > 21:
        print("...")
        for ln in lines[-10:]:
            print(ln)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="FlyBlind repeatable ArduPilot mission")
    p.add_argument("--connection", required=True, help="udp:UAV_PDU_IP:5760 (C2 over 5G; UDP-only)")
    p.add_argument("--bind-ip", default="", help="GCS PDU IP on oaitun_ue1 (required for udpout bind)")
    p.add_argument("--mission", type=Path, default=Path(__file__).resolve().parent.parent / "config/mission_waypoints.json")
    p.add_argument("--thresholds", type=Path, default=Path(__file__).resolve().parent.parent / "config/thresholds.json")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--probe",
        action="store_true",
        help="connect, wait for HEARTBEAT, exit 0 (C2 preflight — do not use nc on :5760)",
    )
    p.add_argument("--probe-timeout", type=float, default=90, help="seconds to wait for HEARTBEAT in --probe mode")
    p.add_argument("--attack-trigger", default="",
                   help="HOST:PORT[,HOST:PORT...] CNI endpoints for ROGUE synchronized bursts")
    p.add_argument("--workload", choices=["waypoint", "setpoint-stream"], default="waypoint",
                   help="waypoint=DO_REPOSITION (legacy); setpoint-stream=SET_POSITION_TARGET_LOCAL_NED")
    p.add_argument("--setpoint-rate-hz", type=float, default=10.0)
    p.add_argument("--trajectory", choices=["circle", "figure8", "sine", "line", "route"], default="circle")
    p.add_argument("--trajectory-duration-s", type=float, default=90.0)
    p.add_argument("--trajectory-speed-mps", type=float, default=5.0)
    p.add_argument("--trajectory-radius-m", type=float, default=30.0)
    p.add_argument("--trajectory-amplitude-m", type=float, default=20.0)
    p.add_argument("--trajectory-period-s", type=float, default=44.0)
    p.add_argument("--setpoint-seed", type=int, default=1)
    p.add_argument("--setpoint-yaw-mode", choices=["fixed", "tangent", "none"], default="tangent")
    p.add_argument("--proxy-forward-csv", type=Path, default=None,
                   help="proxy_forward.csv from UAV MAVLink proxy (optional correlation)")
    p.add_argument("--stream-cut-ms", type=float, default=None,
                   help="Gate A: pause setpoint stream for N ms (temporary cut, then resume)")
    p.add_argument("--stream-cut-start-s", type=float, default=15.0,
                   help="Gate A: seconds after trajectory_t0 when pause begins (default 15)")
    p.add_argument("--telemetry-arrival-capture", action="store_true",
                   help="log LOCAL_POSITION_NED arrival timestamps at GCS (downlink timeliness)")
    args = p.parse_args()
    if args.probe:
        open_c2_mavlink(args.connection, args.bind_ip, heartbeat_timeout_s=args.probe_timeout)
        print("[OK] C2 MAVLink HEARTBEAT over 5G PDU path", flush=True)
        return
    if args.workload == "setpoint-stream":
        from setpoint_stream import run_setpoint_stream  # local import avoids circular deps

        summary = run_setpoint_stream(
            args.connection,
            args.bind_ip,
            args.thresholds,
            args.out,
            rate_hz=args.setpoint_rate_hz,
            trajectory_name=args.trajectory,
            trajectory_duration_s=args.trajectory_duration_s,
            trajectory_speed_mps=args.trajectory_speed_mps,
            trajectory_radius_m=args.trajectory_radius_m,
            trajectory_amplitude_m=args.trajectory_amplitude_m,
            trajectory_period_s=args.trajectory_period_s,
            trajectory_seed=args.setpoint_seed,
            yaw_mode=args.setpoint_yaw_mode,
            proxy_forward_csv=args.proxy_forward_csv,
            stream_cut_ms=args.stream_cut_ms,
            stream_cut_start_s=args.stream_cut_start_s,
            attack_trigger=args.attack_trigger,
            telemetry_capture=args.telemetry_arrival_capture,
        )
    else:
        summary = run_mission(args.connection, args.mission, args.thresholds, args.out,
                              args.bind_ip, attack_trigger=args.attack_trigger)
    print(json.dumps(summary, indent=2))
    print_flight_log(summary)


if __name__ == "__main__":
    main()

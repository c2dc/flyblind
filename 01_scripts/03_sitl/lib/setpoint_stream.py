#!/usr/bin/env python3
##################################################################################################################################
# Module: setpoint_stream.py (10 Hz SET_POSITION_TARGET_LOCAL_NED stream — Gate A)                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Continuous setpoint workload with acquisition window, freshness/AoI artifacts, optional stream-cut and C2-sync tri#
##################################################################################################################################

"""Continuous SET_POSITION_TARGET_LOCAL_NED stream (Gate A / freshness workload).

Replaces discrete waypoints with a 10 Hz position+yaw stream over the same 5G C2
path as ``mission_runner``. ArduPilot tracks the reference; we log every setpoint
with monotonic timestamps for AoI / network_arrival_delay analysis.

Pipeline
--------
1. Bootstrap via shared ``mission_runner`` helpers (arm, takeoff, orbit entry)
2. Acquisition window until cross-track < threshold, then ``steady`` phase
3. Stream setpoints from ``trajectory.sample``; optional ``--attack-trigger`` to
   fire CNI sync bursts (see ``c2_sync_flood.py``)
4. Optional ``--stream-cut-ms`` (Gate A temporary C2 pause)
5. Merge proxy CSVs via ``freshness_metrics.merge_proxy_artifacts``

Artifacts: ``setpoints.csv``, ``aoi_timeseries.csv``, ``freshness_summary.json``.
"""
from __future__ import annotations

import csv
import json
import math
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

from pymavlink import mavutil

# Allow running from sitl/lib with sibling imports
_LIB = Path(__file__).resolve().parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from freshness_metrics import (  # noqa: E402
    AoISample,
    SetpointRecord,
    build_freshness_summary,
)
from mission_runner import (  # noqa: E402
    C2Stats,
    MissionPollState,
    PhysicsStats,
    Thresholds,
    begin_steady_state,
    arm_sitl_with_retries,
    bootstrap_land_then_guided,
    capture_final_flight_snapshot,
    clear_bootstrap_failsafe_latch,
    ensure_sitl_landed_and_disarmed,
    get_home,
    load_json,
    make_mavlink_connection,
    open_c2_mavlink,
    poll_messages,
    poll_sleep,
    request_flight_state_streams,
    write_telemetry_downlink_gcs_csv,
    write_mavlink_rx_gcs_csv,
    write_gcs_hb_sent_csv,
    write_statustext_csv,
    write_heartbeat_csv,
    run_pre_arm_gate,
    send_command,
    set_mode_guided,
    takeoff_sitl_with_retries,
    wait_sitl_ready_to_arm,
)
from trajectory import TrajectoryConfig, initial_phase_from_xy, sample, tracking_errors  # noqa: E402

# Ignore velocities, accel, yaw_rate — position + yaw only
TYPE_MASK = (
    mavutil.mavlink.POSITION_TARGET_TYPEMASK_VX_IGNORE
    | mavutil.mavlink.POSITION_TARGET_TYPEMASK_VY_IGNORE
    | mavutil.mavlink.POSITION_TARGET_TYPEMASK_VZ_IGNORE
    | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AX_IGNORE
    | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AY_IGNORE
    | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AZ_IGNORE
    | mavutil.mavlink.POSITION_TARGET_TYPEMASK_YAW_RATE_IGNORE
)

MOTION_ONSET_THRESH_M = 0.15
MOTION_PERSIST_S = 0.05
TARGET_MATCH_M = 1.0
ACQUISITION_CROSS_TRACK_M = 10.0
ACQUISITION_HOLD_S = 2.0
# Paper-comparable orbit start (August frozen: already on circle → acq ≈ 2.0 s).
PAPER_COMPARABLE_ACQ_MAX_S = 2.5
PAPER_COMPARABLE_RADIUS_TOL_M = 5.0
# Optional: fly to orbit entry *before* opening the 10 Hz stream (default off =
# published harness). Enable with PREPOSITION_ORBIT_ENTRY=1 for new repros.
PREPOSITION_ORBIT_ENTRY_ENV = "PREPOSITION_ORBIT_ENTRY"
PREPOSITION_TOL_M = 5.0
PREPOSITION_HOLD_S = 1.0
PREPOSITION_TIMEOUT_S = 90.0


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _paper_comparable_orbit_start(
    *,
    stream_start_radius_m: Optional[float],
    trajectory_radius_m: float,
    acquisition_duration_s: Optional[float],
) -> dict[str, Any]:
    """Flag runs that started the stream far from the circle (home-start race).

    Does **not** fail VALID; marks non-comparable to August frozen medians.
    """
    reasons: list[str] = []
    radius_ok = None
    if stream_start_radius_m is not None and trajectory_radius_m > 0:
        radius_ok = abs(float(stream_start_radius_m) - float(trajectory_radius_m)) <= (
            PAPER_COMPARABLE_RADIUS_TOL_M
        )
        if not radius_ok:
            reasons.append(
                "stream_start_radius_m=%.2f not within %.1fm of R=%.1f"
                % (stream_start_radius_m, PAPER_COMPARABLE_RADIUS_TOL_M, trajectory_radius_m)
            )
    acq_ok = None
    if acquisition_duration_s is not None:
        acq_ok = float(acquisition_duration_s) <= PAPER_COMPARABLE_ACQ_MAX_S
        if not acq_ok:
            reasons.append(
                "acquisition_duration_s=%.3f > %.1f"
                % (acquisition_duration_s, PAPER_COMPARABLE_ACQ_MAX_S)
            )
    comparable = (not reasons) and (radius_ok is not False) and (acq_ok is not False)
    return {
        "paper_comparable": bool(comparable),
        "paper_comparable_reasons": reasons,
        "stream_start_radius_m": (
            round(stream_start_radius_m, 3) if stream_start_radius_m is not None else None
        ),
        "radius_tol_m": PAPER_COMPARABLE_RADIUS_TOL_M,
        "acq_duration_max_s": PAPER_COMPARABLE_ACQ_MAX_S,
        "radius_ok": radius_ok,
        "acq_duration_ok": acq_ok,
    }


def preposition_to_orbit_entry(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    traj: TrajectoryConfig,
    alt_m: float,
    frame: int,
    *,
    tol_m: float = PREPOSITION_TOL_M,
    hold_s: float = PREPOSITION_HOLD_S,
    timeout_s: float = PREPOSITION_TIMEOUT_S,
) -> dict[str, Any]:
    """Fly to circle/figure-8 t=0 entry and hold before opening the stream.

    Recreates the August warm-start condition (vehicle already on the circle when
    setpoints.csv starts) without moving ``attack_onset`` (~136 s). Default path
    skips this (published harness).
    """
    entry = sample(traj, 0.0, alt_m)
    t0 = time.monotonic()
    deadline = t0 + timeout_s
    below_since: Optional[float] = None
    print(
        "[C2] preposition to orbit entry (%.1f, %.1f, %.1f) tol=%.1fm hold=%.1fs"
        % (entry.x_m, entry.y_m, entry.z_m, tol_m, hold_s),
        flush=True,
    )
    while time.monotonic() < deadline:
        send_setpoint_local_ned(mav, entry.x_m, entry.y_m, entry.z_m, entry.yaw_rad, frame)
        for _ in range(4):
            poll_messages(mav, poll)
        ned = poll.ned_cache[0]
        if ned is None:
            poll_sleep(mav, 0.1, poll)
            continue
        dist = math.hypot(float(ned[0]) - entry.x_m, float(ned[1]) - entry.y_m)
        now = time.monotonic()
        if dist <= tol_m:
            if below_since is None:
                below_since = now
            elif now - below_since >= hold_s:
                elapsed = now - t0
                print(
                    "[C2] preposition ok dist=%.2fm in %.1fs"
                    % (dist, elapsed),
                    flush=True,
                )
                return {
                    "enabled": True,
                    "ok": True,
                    "elapsed_s": round(elapsed, 3),
                    "final_dist_m": round(dist, 3),
                    "entry_ned_m": [round(entry.x_m, 3), round(entry.y_m, 3), round(entry.z_m, 3)],
                    "tol_m": tol_m,
                    "hold_s": hold_s,
                }
        else:
            below_since = None
        poll_sleep(mav, 0.1, poll)
    ned = poll.ned_cache[0]
    dist = None
    if ned is not None:
        dist = math.hypot(float(ned[0]) - entry.x_m, float(ned[1]) - entry.y_m)
    elapsed = time.monotonic() - t0
    print(
        "[C2] preposition TIMEOUT dist=%s after %.1fs — continuing to stream"
        % (("%.2f" % dist) if dist is not None else "n/a", elapsed),
        flush=True,
    )
    return {
        "enabled": True,
        "ok": False,
        "elapsed_s": round(elapsed, 3),
        "final_dist_m": round(dist, 3) if dist is not None else None,
        "entry_ned_m": [round(entry.x_m, 3), round(entry.y_m, 3), round(entry.z_m, 3)],
        "tol_m": tol_m,
        "hold_s": hold_s,
        "reason": "timeout",
    }


def _pct(vals: list[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return float(s[int(round((p / 100.0) * (len(s) - 1)))])


def _update_acquisition_end(
    cross_track_m: Optional[float],
    now_mono: float,
    *,
    acquisition_end_mono: Optional[float],
    below_since_mono: Optional[float],
    threshold_m: float = ACQUISITION_CROSS_TRACK_M,
    hold_s: float = ACQUISITION_HOLD_S,
) -> tuple[Optional[float], Optional[float]]:
    """Return (acquisition_end_mono, below_since_mono) after one sample."""
    if acquisition_end_mono is not None or cross_track_m is None:
        return acquisition_end_mono, below_since_mono
    if cross_track_m < threshold_m:
        if below_since_mono is None:
            below_since_mono = now_mono
        elif now_mono - below_since_mono >= hold_s:
            acquisition_end_mono = now_mono
    else:
        below_since_mono = None
    return acquisition_end_mono, below_since_mono


def _effective_trajectory_t(
    now_mono: float,
    orbit_stream_t0_mono: Optional[float],
) -> float:
    """Hold t=0 (orbit entry point) until acquisition; then advance the circle clock."""
    if orbit_stream_t0_mono is None:
        return 0.0
    return max(0.0, now_mono - orbit_stream_t0_mono)


def _rephase_trajectory_on_circle(
    traj: TrajectoryConfig,
    x_act: float,
    y_act: float,
) -> TrajectoryConfig:
    """Align circle/figure-8 phase with the UAV angular position after orbit entry."""
    if traj.name not in ("circle", "figure8"):
        return traj
    if math.hypot(x_act, y_act) < 1.0:
        return traj
    phi = math.atan2(y_act, x_act)
    return TrajectoryConfig(
        name=traj.name,
        duration_s=traj.duration_s,
        speed_mps=traj.speed_mps,
        radius_m=traj.radius_m,
        amplitude_m=traj.amplitude_m,
        period_s=traj.period_s,
        seed=traj.seed,
        phase_offset_rad=phi,
    )


def _mode_transition_integrity(
    mode_transitions: list[dict[str, Any]],
    trajectory_start_rel_s: Optional[float],
) -> tuple[bool, str]:
    """Require LAND->GUIDED before trajectory streaming (logger must start early)."""
    land_guided = [
        m for m in mode_transitions
        if m.get("from") == "LAND" and m.get("to") == "GUIDED"
    ]
    if not land_guided:
        return False, "missing LAND->GUIDED (state logger likely started after GUIDED)"
    if trajectory_start_rel_s is None:
        return True, "LAND->GUIDED observed"
    before_traj = [m for m in land_guided if float(m.get("t_rel_s", 0)) < trajectory_start_rel_s]
    if not before_traj:
        t0 = land_guided[0].get("t_rel_s")
        return False, "LAND->GUIDED@%ss after trajectory start (misaligned window)" % t0
    return True, "LAND->GUIDED@%ss before trajectory" % before_traj[0].get("t_rel_s")


def send_setpoint_local_ned(
    mav: mavutil.mavfile,
    x: float,
    y: float,
    z: float,
    yaw: float,
    frame: int,
    *,
    logical_setpoint_id: int = 0,
) -> None:
    # time_boot_ms is unused by ArduCopter SET_POSITION_TARGET_LOCAL_NED handler
    # (GCS_Mavlink.cpp decodes x/y/z/yaw only). Safe correlation key for arrival
    # capture on the UAV (shared CLOCK_MONOTONIC single-host).
    mav.mav.set_position_target_local_ned_send(
        int(logical_setpoint_id) & 0xFFFFFFFF,
        mav.target_system,
        mav.target_component,
        frame,
        TYPE_MASK,
        x, y, z,
        0, 0, 0,
         0, 0, 0,
        yaw, 0,
    )
    flush = getattr(mav, "flush", None)
    if callable(flush):
        flush()


def load_proxy_arrivals(proxy_forward_csv: Optional[Path]) -> dict[tuple[float, float, float], int]:
    """Map (x,y,z) rounded -> t_arrival_ns from proxy (best-effort)."""
    out: dict[tuple[float, float, float], int] = {}
    if not proxy_forward_csv or not proxy_forward_csv.is_file():
        return out
    with proxy_forward_csv.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if row.get("mavlink_msg_type") != "SET_POSITION_TARGET_LOCAL_NED":
                continue
            try:
                key = (round(float(row["x"]), 2), round(float(row["y"]), 2), round(float(row["z"]), 2))
                out[key] = int(row["t_arrival_ns"])
            except (KeyError, ValueError):
                continue
    return out


def climb_to_altitude_via_setpoints(
    mav: mavutil.mavfile,
    poll: MissionPollState,
    alt_m: float,
    frame: int,
    *,
    timeout_s: float = 30.0,
    target_tol_m: float = 1.0,
) -> tuple[bool, float]:
    """Climb in GUIDED using the same SET_POSITION control path as the stream."""
    t0 = time.monotonic_ns()
    z_target = -float(alt_m)
    deadline = time.monotonic() + timeout_s
    print(
        "[C2] climb via setpoints to %.1fm (timeout=%.0fs)"
        % (alt_m, timeout_s),
        flush=True,
    )
    while time.monotonic() < deadline:
        send_setpoint_local_ned(mav, 0.0, 0.0, z_target, 0.0, frame)
        for _ in range(4):
            poll_messages(mav, poll)
        rel = poll.rel_alt_cache[0]
        if rel is not None and rel >= alt_m - target_tol_m:
            lat_ms = (time.monotonic_ns() - t0) / 1e6
            print(
                "[C2] climb reached rel_alt=%.1fm in %.0f ms" % (rel, lat_ms),
                flush=True,
            )
            return True, lat_ms
        poll_sleep(mav, 0.1, poll)
    lat_ms = (time.monotonic_ns() - t0) / 1e6
    print(
        "[C2] climb timeout rel_alt=%s after %.0f ms"
        % (poll.rel_alt_cache[0], lat_ms),
        flush=True,
    )
    return False, lat_ms


def _cut_window_aoi_stats(
    aoi_samples: list[AoISample],
    cut_start_rel_s: float,
    cut_end_rel_s: float,
    nominal_period_ms: float,
) -> dict[str, Any]:
    """AoI statistics strictly inside the pause window (I-0A validation)."""
    window = [
        s for s in aoi_samples
        if cut_start_rel_s <= s.t_rel_s <= cut_end_rel_s and s.aoi_network_ms is not None
    ]
    vals = [float(s.aoi_network_ms) for s in window if s.aoi_network_ms is not None]
    stale_ms = nominal_period_ms * 2.0
    max_streak = 0
    streak = 0
    for s in aoi_samples:
        if cut_start_rel_s <= s.t_rel_s <= cut_end_rel_s:
            if s.aoi_network_ms is not None and float(s.aoi_network_ms) > stale_ms:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
    miss_n = sum(1 for v in vals if v > stale_ms)
    return {
        "aoi_max_during_cut_ms": round(max(vals), 3) if vals else None,
        "aoi_p999_during_cut_ms": round(_pct(vals, 99.9), 3) if vals else None,
        "deadline_miss_ratio_during_cut": round(miss_n / len(vals), 4) if vals else None,
        "max_stale_streak_during_cut": max_streak,
        "aoi_samples_during_cut": len(vals),
    }


def run_setpoint_stream(
    connection: str,
    bind_ip: str,
    thresholds_path: Path,
    out_dir: Path,
    rate_hz: float = 10.0,
    trajectory_name: str = "circle",
    trajectory_duration_s: float = 90.0,
    trajectory_speed_mps: float = 5.0,
    trajectory_radius_m: float = 30.0,
    trajectory_amplitude_m: float = 20.0,
    trajectory_period_s: float = 44.0,
    trajectory_seed: int = 1,
    takeoff_alt_m: float = 10.0,
    setpoint_frame: Optional[int] = None,
    yaw_mode: str = "tangent",
    proxy_forward_csv: Optional[Path] = None,
    stream_cut_ms: Optional[float] = None,
    stream_cut_start_s: Optional[float] = 15.0,
    attack_trigger: str = "",
    telemetry_capture: bool = False,
) -> dict[str, Any]:
    if rate_hz <= 0:
        raise ValueError("rate_hz must be > 0")
    thr_d = load_json(thresholds_path)
    thr = Thresholds(
        heartbeat_loss_gap_s=float(thr_d.get("heartbeat_loss_gap_s", 1.5)),
        command_ack_timeout_s=float(thr_d.get("command_ack_timeout_s", 3.0)),
        command_late_ack_ms=float(thr_d.get("command_late_ack_ms", 500)),
        warmup_discard_s=float(thr_d.get("warmup_discard_s", 5.0)),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = setpoint_frame if setpoint_frame is not None else mavutil.mavlink.MAV_FRAME_LOCAL_NED
    traj = TrajectoryConfig(
        name=trajectory_name,  # type: ignore[arg-type]
        duration_s=trajectory_duration_s,
        speed_mps=trajectory_speed_mps,
        radius_m=trajectory_radius_m,
        amplitude_m=trajectory_amplitude_m,
        period_s=trajectory_period_s,
        seed=trajectory_seed,
    )
    nominal_period_ms = 1000.0 / rate_hz

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
        ned_cache=[None],
        telemetry_capture=telemetry_capture,
    )

    mav = open_c2_mavlink(connection, bind_ip, heartbeat_timeout_s=120.0, attempts=3)
    poll_sleep(mav, 1.0, poll)
    wait_sitl_ready_to_arm(mav)
    home_lat, home_lon, _ = get_home(mav, timeout_s=30.0)
    ensure_sitl_landed_and_disarmed(mav, poll)
    bootstrap_ok = bootstrap_land_then_guided(mav, poll)
    poll_sleep(mav, 1.0, poll)
    # EKF must be ready in GUIDED (connect-time wait may have been in STABILIZE/LAND).
    wait_sitl_ready_to_arm(mav, timeout_s=120.0, min_settle_s=15.0, poll=poll)
    poll_sleep(mav, 1.0, poll)

    clear_bootstrap_failsafe_latch(poll)
    pre_arm_ok, pre_arm_details = run_pre_arm_gate(mav, poll)
    if not pre_arm_ok:
        raise RuntimeError(
            "pre-arm gate failed: %s" % ",".join(pre_arm_details.get("failed") or [])
        )

    ok, lat_ms = arm_sitl_with_retries(
        mav, poll=poll, attempts=5, timeout_s=15.0, retry_pause_s=3.0,
    )
    if not ok:
        raise RuntimeError(
            "arm failed after 5 attempts (see [C2] STATUSTEXT/COMMAND_ACK above)"
        )
    print("[C2] stream arm ok in %.0f ms" % lat_ms, flush=True)
    poll_sleep(mav, 1.0, poll)
    if poll.phys.last_mode != "GUIDED":
        set_mode_guided(mav)
        poll_sleep(mav, 0.5, poll)
    ok, climb_ms = takeoff_sitl_with_retries(
        mav,
        takeoff_alt_m,
        poll,
        attempts=2,
        timeout_s=max(15.0, thr.command_ack_timeout_s),
    )
    if not ok:
        ok, climb_ms = climb_to_altitude_via_setpoints(
            mav, poll, takeoff_alt_m, frame, timeout_s=35.0,
        )
    if not ok:
        raise RuntimeError(
            "climb to %.1fm failed (takeoff + setpoint climb; see [C2] logs above)"
            % takeoff_alt_m
        )
    print("[C2] stream climb ok in %.0f ms" % climb_ms, flush=True)
    poll_sleep(mav, 3.0, poll)
    steady_start_s = begin_steady_state(poll)

    # Optional: fly to orbit entry before opening the stream (default OFF —
    # published August harness). Keeps attack_onset ~136 s; only moves the
    # vehicle so setpoints.csv starts on-circle like warm-start frozen runs.
    preposition_info: dict[str, Any] = {
        "enabled": False,
        "env": PREPOSITION_ORBIT_ENTRY_ENV,
        "ok": None,
    }
    if _env_flag(PREPOSITION_ORBIT_ENTRY_ENV, default=False):
        # phase_offset=0 → geometric entry (R, 0) for circle; then rephase below.
        preposition_info = preposition_to_orbit_entry(
            mav, poll, traj, takeoff_alt_m, frame,
        )
        preposition_info["env"] = PREPOSITION_ORBIT_ENTRY_ENV
        poll_sleep(mav, 0.5, poll)

    # Align circle/figure-8 phase with the post-climb (or post-preposition) NED.
    initial_ned: Optional[tuple[float, float, float]] = None
    for _ in range(40):
        poll_messages(mav, poll)
        if poll.ned_cache[0] is not None:
            initial_ned = poll.ned_cache[0]
            break
        poll_sleep(mav, 0.1, poll)
    x0, y0, z0 = initial_ned if initial_ned else (0.0, 0.0, -takeoff_alt_m)
    phase0 = initial_phase_from_xy(x0, y0, traj)
    traj = TrajectoryConfig(
        name=trajectory_name,  # type: ignore[arg-type]
        duration_s=trajectory_duration_s,
        speed_mps=trajectory_speed_mps,
        radius_m=trajectory_radius_m,
        amplitude_m=trajectory_amplitude_m,
        period_s=trajectory_period_s,
        seed=trajectory_seed,
        phase_offset_rad=phase0,
    )
    print(
        "[C2] trajectory phase offset=%.2f rad initial_ned=(%.1f, %.1f, %.1f)"
        % (phase0, x0, y0, z0),
        flush=True,
    )

    # Synchronized C2 attack (sync-c2): sends a tiny "trigger" to the ROGUE over the
    # pod network (CNI, off 5G) at the exact instant of each setpoint, so the ROGUE
    # concentrates a burst in the send window. Best-effort: if the socket fails, the
    # stream continues normally (the trigger must never break flight). Same pattern as
    # the waypoint path (mission_runner.run_mission).
    #
    # CAMPAIGN NOTE (not code): at 10 Hz there is 1 trigger every 100 ms. With
    # --sync-burst-ms 250 (c2_sync_flood default) bursts overlap and the attack
    # becomes nearly continuous (loses the "surgical ~5% duty" — becomes disguised
    # volume). To keep surgical mode, the campaign should use a small --sync-burst-ms
    # (<= ~50). The default is intentionally NOT changed here.
    trig_sock = None
    trig_addrs: list[tuple[str, int]] = []
    trig_fired = 0
    skew_gcs_rows: list[dict[str, Any]] = []
    if attack_trigger:
        try:
            for endpoint in attack_trigger.split(","):
                thost, tport = endpoint.strip().rsplit(":", 1)
                trig_addrs.append((thost, int(tport)))
            trig_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            print("[C2-SYNC] attack trigger -> %s (%d targets, per setpoint)"
                  % (attack_trigger, len(trig_addrs)), flush=True)
        except Exception:
            trig_sock = None
            trig_addrs = []

    def fire_trigger(t0_ns: int) -> int | None:
        """Send CNI trigger; returns t1_ns (after sendto) or None."""
        nonlocal trig_fired
        if trig_sock is None:
            return None
        try:
            # Versioned payload: C|<t0_ns> for skew on ROGUE (legacy 'C' still accepted there).
            t1_last = None
            payload = ("C|%d" % t0_ns).encode("ascii")
            for trig_addr in trig_addrs:
                trig_sock.sendto(payload, trig_addr)
                t1_last = time.monotonic_ns()
                trig_fired += 1
                skew_gcs_rows.append({
                    "seq": trig_fired,
                    "t0_gcs_ns": t0_ns,
                    "t1_trigger_sent_ns": t1_last,
                    "d_send_us": round((t1_last - t0_ns) / 1000.0, 3),
                })
            return t1_last
        except Exception:
            return None

    setpoints: list[SetpointRecord] = []
    aoi_samples: list[AoISample] = []
    successful_send_calls = 0
    failed_send_calls = 0
    latest_gen_ns: Optional[int] = None
    latest_id: Optional[int] = None
    traj_id = "%s_%ds_r%.0f" % (trajectory_name, int(trajectory_duration_s), trajectory_radius_m)

    period_s = 1.0 / rate_hz
    t_stream_end = time.monotonic() + trajectory_duration_s
    next_sp = time.monotonic()
    next_aoi = time.monotonic()
    gen_idx = 0
    logical_id = 0
    streaming = True
    cut_pause_start_at: Optional[float] = None
    cut_pause_end_at: Optional[float] = None
    cut_started = False
    cut_ended = False
    cut_start_rel_s_actual: Optional[float] = None
    cut_end_rel_s_actual: Optional[float] = None
    cut_observed_ms: Optional[float] = None
    stream_resumed = False
    setpoints_suppressed = 0
    setpoints_before_cut = 0
    setpoints_after_cut = 0
    cut_enabled = (
        stream_cut_ms is not None
        and stream_cut_ms > 0
        and stream_cut_start_s is not None
        and stream_cut_start_s >= 0
    )

    trajectory_t0_mono: Optional[float] = None
    orbit_stream_t0_mono: Optional[float] = None
    trajectory_start_rel_s: Optional[float] = None
    acquisition_end_mono: Optional[float] = None
    below_since_mono: Optional[float] = None
    acquisition_cross_max = 0.0

    last_pos: Optional[tuple[float, float, float]] = None
    pending_motion: Optional[dict[str, Any]] = None

    while time.monotonic() < t_stream_end:
        now = time.monotonic()

        if cut_pause_start_at is not None and cut_pause_end_at is not None:
            if not cut_started and now >= cut_pause_start_at:
                cut_started = True
                cut_start_rel_s_actual = round(now - run_t0, 4)
                print(
                    "[C2] stream cut pause started rel=%.3fs (requested %.0f ms)"
                    % (cut_start_rel_s_actual, stream_cut_ms or 0),
                    flush=True,
                )
            if cut_started and not cut_ended and now >= cut_pause_end_at:
                cut_ended = True
                cut_end_rel_s_actual = round(now - run_t0, 4)
                cut_observed_ms = round((now - cut_pause_start_at) * 1000.0, 2)
                stream_resumed = True
                print(
                    "[C2] stream cut resumed rel=%.3fs (observed %.1f ms pause)"
                    % (cut_end_rel_s_actual, cut_observed_ms or 0),
                    flush=True,
                )

        in_pause = (
            cut_started
            and not cut_ended
            and cut_pause_start_at is not None
            and cut_pause_end_at is not None
            and cut_pause_start_at <= now < cut_pause_end_at
        )

        poll_messages(mav, poll)
        for _ in range(4):
            poll_messages(mav, poll)

        if streaming and now >= next_sp:
            if in_pause:
                setpoints_suppressed += 1
                next_sp += period_s
                while next_sp < now:
                    setpoints_suppressed += 1
                    next_sp += period_s
            else:
                if trajectory_t0_mono is None:
                    trajectory_t0_mono = now
                    trajectory_start_rel_s = round(now - run_t0, 3)
                    if cut_enabled:
                        cut_pause_start_at = trajectory_t0_mono + float(stream_cut_start_s)
                        cut_pause_end_at = cut_pause_start_at + float(stream_cut_ms) / 1000.0
                    print(
                        "[C2] trajectory_t0 rel=%.3fs (first setpoint)"
                        % trajectory_start_rel_s,
                        flush=True,
                    )
                    if cut_enabled and cut_pause_start_at is not None:
                        print(
                            "[C2] stream cut scheduled rel=%.3fs for %.0f ms "
                            "(trajectory_t0+%.1fs)"
                            % (
                                cut_pause_start_at - run_t0,
                                stream_cut_ms or 0,
                                stream_cut_start_s or 0,
                            ),
                            flush=True,
                        )
                t_traj = _effective_trajectory_t(now, orbit_stream_t0_mono)
                ref = sample(traj, t_traj, takeoff_alt_m)
                yaw = ref.yaw_rad if yaw_mode == "tangent" else (0.0 if yaw_mode == "fixed" else float("nan"))
                t_gen = time.monotonic_ns()
                fire_trigger(t_gen)  # syncs the ROGUE burst with the setpoint instant
                try:
                    send_setpoint_local_ned(
                        mav, ref.x_m, ref.y_m, ref.z_m, yaw, frame,
                        logical_setpoint_id=logical_id + 1,
                    )
                    successful_send_calls += 1
                except Exception:
                    failed_send_calls += 1
                    next_sp += period_s
                    continue
                t_send = time.monotonic_ns()
                logical_id += 1
                gen_idx += 1
                sp = SetpointRecord(
                    logical_setpoint_id=logical_id,
                    generation_index=gen_idx,
                    trajectory_id=traj_id,
                    t_generation_gcs_ns=t_gen,
                    t_send_gcs_ns=t_send,
                    x_ref=ref.x_m,
                    y_ref=ref.y_m,
                    z_ref=ref.z_m,
                )
                latest_gen_ns = t_gen
                latest_id = logical_id
                setpoints.append(sp)
                if cut_ended:
                    setpoints_after_cut += 1
                elif not cut_started:
                    setpoints_before_cut += 1
                next_sp += period_s
                if next_sp < now:
                    next_sp = now + period_s

        # AoI sampling ~100 Hz
        if now >= next_aoi:
            t_ns = time.monotonic_ns()
            aoi_net = ((t_ns - latest_gen_ns) / 1e6) if latest_gen_ns else None
            aoi_samples.append(AoISample(
                t_mono_ns=t_ns,
                t_rel_s=round(now - run_t0, 4),
                latest_setpoint_id=latest_id,
                latest_generation_ns=latest_gen_ns,
                aoi_network_ms=aoi_net,
                aoi_acceptance_ms=None,
                aoi_motion_ms=None,
                current_deadline_state="stale" if aoi_net and aoi_net > nominal_period_ms * 2 else "ok",
            ))
            next_aoi += 0.01
            if next_aoi < now:
                next_aoi = now + 0.01

        # Tracking vs trajectory reference (LOCAL_POSITION_NED when available).
        cur = poll.ned_cache[0]
        if cur and setpoints and trajectory_t0_mono is not None:
            x_act, y_act, z_act = cur
            sp = setpoints[-1]
            sp.x_actual = x_act
            sp.y_actual = y_act
            sp.z_actual = z_act
            t_traj = _effective_trajectory_t(now, orbit_stream_t0_mono)
            errs = tracking_errors(traj, t_traj, takeoff_alt_m, x_act, y_act, z_act)
            sp.tracking_error_m = errs["tracking_error_m"]
            sp.cross_track_error_m = errs["cross_track_error_m"]
            sp.along_track_error_m = errs["along_track_error_m"]
            cross = sp.cross_track_error_m or 0.0
            if acquisition_end_mono is None:
                acquisition_cross_max = max(acquisition_cross_max, cross)
            prev_acq = acquisition_end_mono
            acquisition_end_mono, below_since_mono = _update_acquisition_end(
                sp.cross_track_error_m,
                now,
                acquisition_end_mono=acquisition_end_mono,
                below_since_mono=below_since_mono,
            )
            if prev_acq is None and acquisition_end_mono is not None:
                orbit_stream_t0_mono = acquisition_end_mono
                traj = _rephase_trajectory_on_circle(traj, x_act, y_act)
                print(
                    "[C2] orbit acquired — circle clock starts at phase=%.2f rad"
                    % traj.phase_offset_rad,
                    flush=True,
                )
            if acquisition_end_mono is None:
                window = "acquisition"
            elif now >= acquisition_end_mono:
                window = "steady"
            else:
                window = "acquisition"
            sp.trajectory_window = window
            if pending_motion and sp.t_motion_onset_ns is None:
                dist = math.sqrt(
                    (x_act - pending_motion["x0"]) ** 2
                    + (y_act - pending_motion["y0"]) ** 2
                )
                if dist >= MOTION_ONSET_THRESH_M and (now - pending_motion["t0"]) >= MOTION_PERSIST_S:
                    sp.t_motion_onset_ns = time.monotonic_ns()
            if last_pos:
                dx = x_act - last_pos[0]
                dy = y_act - last_pos[1]
                if math.hypot(dx, dy) > 1e-6 and pending_motion is None:
                    pending_motion = {"x0": x_act, "y0": y_act, "t0": now, "sp_id": sp.logical_setpoint_id}
            last_pos = (x_act, y_act, z_act)

        time.sleep(0.001)

    # LAND
    poll.landing_requested = True
    send_command(mav, mavutil.mavlink.MAV_CMD_NAV_LAND, timeout_s=thr.command_ack_timeout_s, poll=poll)
    poll_sleep(mav, 10.0, poll)
    capture_final_flight_snapshot(mav, poll, timeout_s=15.0)

    # Write CSVs
    sp_rows = []
    for i, sp in enumerate(setpoints):
        sp_rows.append(sp.to_csv_row(setpoints[i - 1] if i else None))
    with (out_dir / "setpoints.csv").open("w", newline="", encoding="utf-8") as f:
        if sp_rows:
            w = csv.DictWriter(f, fieldnames=list(sp_rows[0].keys()))
            w.writeheader()
            w.writerows(sp_rows)

    aoi_fields = [
        "t_mono_ns", "t_rel_s", "latest_setpoint_id", "latest_generation_ns",
        "aoi_network_ms", "aoi_acceptance_ms", "aoi_motion_ms", "current_deadline_state",
    ]
    with (out_dir / "aoi_timeseries.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=aoi_fields)
        w.writeheader()
        for s in aoi_samples:
            w.writerow({
                "t_mono_ns": s.t_mono_ns,
                "t_rel_s": s.t_rel_s,
                "latest_setpoint_id": s.latest_setpoint_id,
                "latest_generation_ns": s.latest_generation_ns,
                "aoi_network_ms": s.aoi_network_ms,
                "aoi_acceptance_ms": s.aoi_acceptance_ms,
                "aoi_motion_ms": s.aoi_motion_ms,
                "current_deadline_state": s.current_deadline_state,
            })

    proxy_delays = []
    if proxy_forward_csv and proxy_forward_csv.is_file():
        with proxy_forward_csv.open(encoding="utf-8") as pf:
            for row in csv.DictReader(pf):
                try:
                    proxy_delays.append(
                        (int(row["t_forward_ns"]) - int(row["t_arrival_ns"])) / 1e6
                    )
                except (KeyError, ValueError):
                    pass

    freshness = build_freshness_summary(
        setpoints, aoi_samples, proxy_delays, nominal_period_ms
    )
    freshness["note"] = (
        "network_arrival_delay / aoi_arrival require post-run merge with "
        "c2_arrival_forwarder capture (--c2-arrival-capture)"
    )
    (out_dir / "freshness_summary.json").write_text(json.dumps(freshness, indent=2) + "\n", encoding="utf-8")

    duration_s = round(time.monotonic() - run_t0, 2)
    steady_duration_s = round(duration_s - steady_start_s, 2)
    track_errs = [sp.tracking_error_m for sp in setpoints if sp.tracking_error_m is not None]
    cross_errs = [sp.cross_track_error_m for sp in setpoints if sp.cross_track_error_m is not None]
    steady_track = [
        sp.tracking_error_m for sp in setpoints
        if sp.trajectory_window == "steady" and sp.tracking_error_m is not None
    ]
    steady_cross = [
        sp.cross_track_error_m for sp in setpoints
        if sp.trajectory_window == "steady" and sp.cross_track_error_m is not None
    ]
    acq_cross = [
        sp.cross_track_error_m for sp in setpoints
        if sp.trajectory_window == "acquisition" and sp.cross_track_error_m is not None
    ]

    track_max = max(track_errs) if track_errs else None
    cross_max = max(cross_errs) if cross_errs else None
    acquisition_end_rel_s = (
        round(acquisition_end_mono - run_t0, 3) if acquisition_end_mono is not None else None
    )
    acquisition_time_s = (
        round(acquisition_end_mono - trajectory_t0_mono, 3)
        if acquisition_end_mono is not None and trajectory_t0_mono is not None
        else None
    )
    stream_start_radius_m: Optional[float] = None
    for sp in setpoints:
        if sp.x_actual is not None and sp.y_actual is not None:
            stream_start_radius_m = math.hypot(float(sp.x_actual), float(sp.y_actual))
            break
    if stream_start_radius_m is None:
        stream_start_radius_m = math.hypot(x0, y0)
    paper_comp = _paper_comparable_orbit_start(
        stream_start_radius_m=stream_start_radius_m,
        trajectory_radius_m=float(trajectory_radius_m),
        acquisition_duration_s=acquisition_time_s,
    )
    if not paper_comp["paper_comparable"]:
        print(
            "[C2] WARN paper_comparable=false: %s"
            % ("; ".join(paper_comp["paper_comparable_reasons"]) or "unknown"),
            flush=True,
        )
    mode_ok, mode_note = _mode_transition_integrity(
        phys.mode_transitions, trajectory_start_rel_s,
    )
    physics_complete = bool(bootstrap_ok and mode_ok)

    stream_cut_summary: dict[str, Any] | None = None
    if cut_enabled and cut_started and cut_end_rel_s_actual is not None and cut_start_rel_s_actual is not None:
        traj_start = trajectory_start_rel_s or cut_start_rel_s_actual
        traj_end_rel = traj_start + float(trajectory_duration_s)
        before_dur = cut_start_rel_s_actual - traj_start
        after_dur = traj_end_rel - cut_end_rel_s_actual
        rate_before = (
            round(setpoints_before_cut / before_dur, 3)
            if before_dur > 0.5 and setpoints_before_cut else None
        )
        rate_after = (
            round(setpoints_after_cut / after_dur, 3)
            if after_dur > 0.5 and setpoints_after_cut else None
        )
        cut_stats = _cut_window_aoi_stats(
            aoi_samples,
            cut_start_rel_s_actual,
            cut_end_rel_s_actual,
            nominal_period_ms,
        )
        stream_cut_summary = {
            "cut_start_rel_s": cut_start_rel_s_actual,
            "cut_end_rel_s": cut_end_rel_s_actual,
            "cut_requested_ms": float(stream_cut_ms),
            "cut_observed_ms": cut_observed_ms,
            "stream_cut_start_s": float(stream_cut_start_s or 0),
            "stream_resumed": bool(stream_resumed),
            "setpoints_suppressed": setpoints_suppressed,
            "setpoints_before_cut": setpoints_before_cut,
            "setpoints_after_cut": setpoints_after_cut,
            "rate_before_cut_hz": rate_before,
            "rate_after_cut_hz": rate_after,
            "nominal_period_ms": nominal_period_ms,
            **cut_stats,
        }
        (out_dir / "stream_cut_summary.json").write_text(
            json.dumps(stream_cut_summary, indent=2) + "\n",
            encoding="utf-8",
        )

    summary = {
        "schema_version": "setpoint_stream_v2",
        "workload": "setpoint-stream",
        "connection": connection,
        "bind_ip": bind_ip or None,
        "rate_hz": rate_hz,
        "trajectory": {
            "name": trajectory_name,
            "duration_s": trajectory_duration_s,
            "speed_mps": trajectory_speed_mps,
            "radius_m": trajectory_radius_m,
            "amplitude_m": trajectory_amplitude_m,
            "period_s": trajectory_period_s,
            "seed": trajectory_seed,
            "phase_offset_rad": round(phase0, 4),
            "initial_position_ned_m": [round(x0, 3), round(y0, 3), round(z0, 3)],
            "trajectory_t0_rel_s": trajectory_start_rel_s,
            "id": traj_id,
        },
        "duration_s": duration_s,
        "n_setpoints": len(setpoints),
        "setpoint_accounting": {
            "generated_setpoints": len(setpoints),
            "successful_send_calls": successful_send_calls,
            "failed_send_calls": failed_send_calls,
        },
        "pre_arm_gate": pre_arm_details,
        "freshness": freshness,
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
                "median": None,
                "p99": None,
                "max": None,
            },
            "failsafe": {
                "triggered": c2.failsafe_triggered,
                "mode": c2.failsafe_mode or None,
                "time_s": c2.failsafe_time_s,
            },
        },
        "physics_metrics": {
            "physics_metrics_complete": physics_complete,
            "bootstrap_land_then_guided": bootstrap_ok,
            "max_deviation_m": round(cross_max, 2) if cross_max is not None else None,
            "position_samples": phys.samples,
            "trajectory_acquisition": {
                "started_at_rel_s": trajectory_start_rel_s,
                "ended_at_rel_s": acquisition_end_rel_s,
                "duration_s": acquisition_time_s,
                "completed": acquisition_end_mono is not None,
                "threshold_m": ACQUISITION_CROSS_TRACK_M,
                "hold_s": ACQUISITION_HOLD_S,
                "orbit_entry_hold_t0": True,
                "orbit_stream_started_at_rel_s": (
                    round(orbit_stream_t0_mono - run_t0, 3) if orbit_stream_t0_mono else None
                ),
                "cross_track_max_m": round(
                    max(acq_cross) if acq_cross else acquisition_cross_max, 2
                ),
                "samples": len(acq_cross),
                "preposition_orbit_entry": preposition_info,
                **paper_comp,
            },
            "steady_state_window": {
                "started_at_rel_s": acquisition_end_rel_s,
                "ended_at_rel_s": round(duration_s, 3),
                "duration_s": round(
                    (duration_s - acquisition_end_rel_s) if acquisition_end_rel_s else None, 3
                ) if acquisition_end_rel_s else None,
                "tracking_error_m": {
                    "median": round(_pct(steady_track, 50), 2) if steady_track else None,
                    "p99": round(_pct(steady_track, 99), 2) if steady_track else None,
                    "max": round(max(steady_track), 2) if steady_track else None,
                    "samples": len(steady_track),
                },
                "cross_track_error_m": {
                    "median": round(_pct(steady_cross, 50), 2) if steady_cross else None,
                    "p99": round(_pct(steady_cross, 99), 2) if steady_cross else None,
                    "max": round(max(steady_cross), 2) if steady_cross else None,
                    "samples": len(steady_cross),
                },
            },
            "flight_state": {
                "mode_transitions": phys.mode_transitions,
                "mode_transition_integrity": mode_ok,
                "mode_transition_note": mode_note,
                "final_mode": phys.last_mode,
                "armed": phys.last_armed,
                "final_armed": phys.last_armed,
                "tracking_error_m": {
                    "scope": "all_setpoints",
                    "median": round(_pct(track_errs, 50), 2) if track_errs else None,
                    "p99": round(_pct(track_errs, 99), 2) if track_errs else None,
                    "max": round(track_max, 2) if track_max is not None else None,
                    "samples": len(track_errs),
                },
                "cross_track_error_m": {
                    "scope": "all_setpoints",
                    "median": round(_pct(cross_errs, 50), 2) if cross_errs else None,
                    "p99": round(_pct(cross_errs, 99), 2) if cross_errs else None,
                    "max": round(max(cross_errs), 2) if cross_errs else None,
                    "samples": len(cross_errs),
                },
            },
        },
        "thresholds": thr_d,
        "landing": {
            "requested": poll.landing_requested,
            "normal": poll.landing_requested and not c2.failsafe_triggered,
        },
        "attack_sync": {
            "trigger_endpoint": attack_trigger or None,
            "triggers_fired": trig_fired,
            "skew_gcs_samples": len(skew_gcs_rows),
            "skew_gcs_send_us_p50": (
                sorted(r["d_send_us"] for r in skew_gcs_rows)[len(skew_gcs_rows) // 2]
                if skew_gcs_rows else None
            ),
            "skew_gcs_send_us_p99": (
                sorted(r["d_send_us"] for r in skew_gcs_rows)[max(0, int(len(skew_gcs_rows) * 0.99) - 1)]
                if skew_gcs_rows else None
            ),
        },
    }
    if skew_gcs_rows:
        skew_path = out_dir / "sync_skew_gcs.csv"
        with skew_path.open("w", newline="", encoding="utf-8") as sf:
            sw = csv.DictWriter(sf, fieldnames=["seq", "t0_gcs_ns", "t1_trigger_sent_ns", "d_send_us"])
            sw.writeheader()
            sw.writerows(skew_gcs_rows)
        summary["attack_sync"]["skew_gcs_csv"] = str(skew_path.name)
    if stream_cut_summary is not None:
        summary["stream_cut"] = stream_cut_summary
    if telemetry_capture and poll.telemetry_rows:
        write_telemetry_downlink_gcs_csv(out_dir, poll.telemetry_rows)
        summary["telemetry_downlink"] = {
            "capture": True,
            "ned_samples_gcs": len(poll.telemetry_rows),
            "message": "LOCAL_POSITION_NED",
        }
    hb_path = write_heartbeat_csv(out_dir, poll.hb_log)
    link_rx_path = write_mavlink_rx_gcs_csv(out_dir, poll.link_rx_log)
    gcs_hb_path = write_gcs_hb_sent_csv(out_dir, poll.gcs_hb_sent_log)
    status_path = write_statustext_csv(out_dir, poll.status_text_log)
    summary["link_vivacity"] = {
        "heartbeat_csv": str(hb_path.name),
        "mavlink_rx_gcs_csv": str(link_rx_path.name),
        "gcs_hb_sent_csv": str(gcs_hb_path.name),
        "statustext_gcs_csv": str(status_path.name),
        "uav_heartbeat_at_gcs_rows": len(poll.hb_log),
        "mavlink_rx_gcs_rows": len(poll.link_rx_log),
        "gcs_hb_sent_rows": len(poll.gcs_hb_sent_log),
        "statustext_rows": len(poll.status_text_log),
        "note": (
            "UAV HEARTBEAT at GCS: heartbeat.csv. "
            "All UAV→GCS msgs (K8): mavlink_rx_gcs.csv. "
            "GCS HEARTBEAT TX: gcs_hb_sent.csv; RX at UAV: proxy_link.csv."
        ),
    }
    (out_dir / "mission_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary

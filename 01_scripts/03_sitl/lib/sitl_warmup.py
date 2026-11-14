#!/usr/bin/env python3
##################################################################################################################################
# Module: sitl_warmup.py (one-shot SITL EKF/arm warm-up on UAV pod)                                                              #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Avoids cold-start arm failures before first orchestrated C2 run; connects directly to tcp:127.0.0.1:5761.         #
#                                                                                                                                #
# Usage: python3 sitl_warmup.py --sitl tcp:127.0.0.1:5761                                                                        #
##################################################################################################################################

"""One-shot SITL warm-up before the first orchestrated C2 run.

Problem
-------
Cold ArduPilot SITL often rejects the first ARM after testbed bootstrap (EKF/GPS
still settling). A failed first mission pollutes campaign statistics.

What this does
--------------
Connects directly to SITL on the UAV pod (``tcp:127.0.0.1:5761``, bypassing the
5G C2 path), waits for GPS/EKF, arms once, disarms, lands — then exits 0.

Called from ``start_testbed.sh`` after SITL is listening; not part of the
experiment orchestrator timing envelope.
"""
from __future__ import annotations

import argparse
import sys
import time

from pymavlink import mavutil

from mission_runner import (
    C2Stats,
    MissionPollState,
    PhysicsStats,
    Thresholds,
    arm_sitl_with_retries,
    poll_sleep,
    request_flight_state_streams,
    send_command,
    set_mode_guided,
    set_mode_named,
    wait_sitl_ready_to_arm,
)

MAX_CONNECT_ATTEMPTS = 5
CONNECT_RETRY_PAUSE_S = 5.0


def run_warmup(connection: str, timeout_s: float = 120.0) -> bool:
    print(
        "[sitl-warmup] connect %s (timeout=%.0fs)" % (connection, timeout_s),
        flush=True,
    )
    mav = None
    last_err: Exception | None = None

    for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
        try:
            if attempt > 1:
                print(
                    "[sitl-warmup] reconnect attempt %s/%s after %s"
                    % (attempt, MAX_CONNECT_ATTEMPTS, last_err),
                    flush=True,
                )
                time.sleep(CONNECT_RETRY_PAUSE_S)
            mav = mavutil.mavlink_connection(connection, source_system=255)
            mav.wait_heartbeat(timeout=min(30.0, timeout_s))
            break
        except Exception as exc:
            last_err = exc
            mav = None

    if mav is None:
        print("[sitl-warmup] connect failed: %s" % last_err, flush=True)
        return False

    # Minimal poll state — we only need arm/disarm, not full mission metrics.
    poll = MissionPollState(
        run_t0=time.monotonic(),
        log_rows=[],
        hb_log=[],
        c2=C2Stats(),
        phys=PhysicsStats(),
        planned=(0.0, 0.0),
        measure=False,
        thr=Thresholds(),
        last_hb=[0.0],
        pos_cache=[None],
        ned_cache=[None],
    )

    wait_sitl_ready_to_arm(mav, timeout_s=timeout_s, min_settle_s=20.0)
    set_mode_guided(mav)
    request_flight_state_streams(mav)
    poll_sleep(mav, 3.0, poll)
    wait_sitl_ready_to_arm(
        mav, timeout_s=min(60.0, timeout_s), min_settle_s=10.0, poll=poll,
    )

    ok, lat_ms = arm_sitl_with_retries(
        mav,
        poll=poll,
        attempts=10,
        timeout_s=20.0,
        retry_pause_s=5.0,
    )
    if not ok:
        print("[sitl-warmup] arm failed after retries", flush=True)
        return False

    print("[sitl-warmup] arm ok in %.0f ms — disarming" % lat_ms, flush=True)
    send_command(
        mav,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        timeout_s=15.0,
        poll=poll,
    )
    poll_sleep(mav, 2.0, poll)
    set_mode_named(mav, "LAND", timeout_s=10.0)
    poll_sleep(mav, 1.0, poll)
    print("[sitl-warmup] OK (LAND)", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Warm up SITL EKF/arm before C2 runs")
    ap.add_argument(
        "--sitl",
        default="tcp:127.0.0.1:5761",
        help="direct SITL MAVLink URL (UAV pod localhost)",
    )
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    return 0 if run_warmup(args.sitl, timeout_s=args.timeout) else 1


if __name__ == "__main__":
    sys.exit(main())

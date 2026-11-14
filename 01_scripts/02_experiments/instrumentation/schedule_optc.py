#!/usr/bin/env python3
##################################################################################################################################
# Module: schedule_optc.py (Option C: run schedule anchored to the stable orbit (setpoint-stream, >= 600 s))                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: All times are seconds since T0 unless the name says otherwise.                                                    #
#                                                                                                                                #
# onset        = margin + T_ORBIT + ORBIT_MARGIN + P_PRE        (rogue sleep: onset - LAUNCH_MIN) burst        = base_envelope - #
#   ceil(legacy_delay)            (3.2a: never depends on drain) traj_end     = attack_end_max + POST_ATTACK_TAIL  | +           #
#   drain_ceiling capture_end  = traj_end + ORBIT_MARGIN + LAND_BOUND + CAPTURE_SLACK                                            #
#                                                                                                                                #
# The trajectory is tied to the attack; the capture envelope is derived from the trajectory end and never feeds back into the    #
#   trajectory length.                                                                                                           #
##################################################################################################################################
"""Option C: run schedule anchored to the stable orbit (setpoint-stream, >= 600 s).

All times are seconds since T0 unless the name says otherwise.

  onset        = margin + T_ORBIT + ORBIT_MARGIN + P_PRE        (rogue sleep: onset - LAUNCH_MIN)
  burst        = base_envelope - ceil(legacy_delay)            (3.2a: never depends on drain)
  traj_end     = attack_end_max + POST_ATTACK_TAIL  | + drain_ceiling
  capture_end  = traj_end + ORBIT_MARGIN + LAND_BOUND + CAPTURE_SLACK

The trajectory is tied to the attack; the capture envelope is derived from the
trajectory end and never feeds back into the trajectory length.
"""
from __future__ import annotations

import argparse
import json
import math
from typing import Any, Optional

OPTC_SCHEMA = "flyblind_schedule_optc_v1"
OPTC_MIN_DURATION_S = 600

# Mission launch -> first logical setpoint / orbit acquired (PREPOSITION_ORBIT_ENTRY=1).
T_TRAJ_START_FROM_LAUNCH_S = 51.3
T_ORBIT_FROM_LAUNCH_S = 53.3
ORBIT_MARGIN_S = 15.0
P_PRE_S = 30.0
# Same value as drain_policy.WINDOW_S.
POST_ATTACK_TAIL_S = 10.0
# LAND ack timeout (3) + poll sleep (10) + final flight snapshot (15).
LAND_BOUND_S = 28.0
CAPTURE_SLACK_S = 5.0
# Rogue process start after T0 (onset sleep is counted from it).
ATTACK_LAUNCH_MIN_S = 14.0
ATTACK_LAUNCH_MAX_S = 18.0
BASE_ENVELOPE_PAD_S = 20.0


def optc_applies(workload: Optional[str], duration_s: float) -> bool:
    return workload == "setpoint-stream" and float(duration_s or 0) >= OPTC_MIN_DURATION_S


def legacy_onset_delay_s(requested_delay_s: float, margin_s: float, takeoff_guard_s: float) -> float:
    """Pre-option-C delay from rogue start; baseline / delay 0 use the virtual margin+guard."""
    delay = float(requested_delay_s or 0.0)
    if delay <= 0 or (margin_s and delay < margin_s):
        return float(margin_s) + float(takeoff_guard_s)
    return delay


def plan_schedule(
    *,
    duration_s: float,
    warmup_s: float,
    mission_len_s: float,
    requested_delay_s: float,
    takeoff_guard_s: float,
    drain_ceiling_s: float = 0.0,
) -> dict[str, Any]:
    margin = (float(duration_s) - float(mission_len_s)) / 2.0
    base_envelope = float(duration_s) + float(warmup_s) + BASE_ENVELOPE_PAD_S
    legacy_delay = legacy_onset_delay_s(requested_delay_s, margin, takeoff_guard_s)
    burst = max(1, int(base_envelope) - int(math.ceil(legacy_delay)))

    onset_anchor = margin + T_ORBIT_FROM_LAUNCH_S + ORBIT_MARGIN_S + P_PRE_S
    onset_delay = max(legacy_delay, onset_anchor - ATTACK_LAUNCH_MIN_S)
    onset_min = ATTACK_LAUNCH_MIN_S + onset_delay
    onset_max = ATTACK_LAUNCH_MAX_S + onset_delay
    attack_end_min = onset_min + burst
    attack_end_max = onset_max + burst

    drain = max(0.0, float(drain_ceiling_s or 0.0))
    tail = drain if drain > 0 else POST_ATTACK_TAIL_S
    traj_start = margin + T_TRAJ_START_FROM_LAUNCH_S
    traj_end = attack_end_max + tail
    capture_end = traj_end + ORBIT_MARGIN_S + LAND_BOUND_S + CAPTURE_SLACK_S
    capture_envelope = int(math.ceil(max(base_envelope + drain, capture_end)))

    return {
        "schema": OPTC_SCHEMA,
        "time_base": "s since T0",
        "duration_s": float(duration_s),
        "warmup_s": float(warmup_s),
        "mission_start_s": margin,
        "base_envelope_s": base_envelope,
        "legacy_onset_delay_s": legacy_delay,
        "burst_s": burst,
        "onset_anchor_s": round(onset_anchor, 3),
        "onset_delay_s": round(onset_delay, 3),
        "planned_onset_s": [round(onset_min, 3), round(onset_max, 3)],
        "planned_attack_end_s": [round(attack_end_min, 3), round(attack_end_max, 3)],
        "virtual_attack_onset_s": round(onset_anchor, 3),
        "virtual_attack_end_s": round(onset_anchor + burst, 3),
        "orbit_acquired_nominal_s": round(margin + T_ORBIT_FROM_LAUNCH_S, 3),
        "orbit_acquired_bound_s": round(margin + T_ORBIT_FROM_LAUNCH_S + ORBIT_MARGIN_S, 3),
        "trajectory_start_nominal_s": round(traj_start, 3),
        "trajectory_end_s": round(traj_end, 3),
        "trajectory_duration_s": round(traj_end - traj_start, 3),
        "trajectory_tail_s": tail,
        "drain_ceiling_s": drain,
        "capture_end_s": round(capture_end, 3),
        "capture_envelope_s": capture_envelope,
        "constants": {
            "T_TRAJ_START_FROM_LAUNCH_S": T_TRAJ_START_FROM_LAUNCH_S,
            "T_ORBIT_FROM_LAUNCH_S": T_ORBIT_FROM_LAUNCH_S,
            "ORBIT_MARGIN_S": ORBIT_MARGIN_S,
            "P_PRE_S": P_PRE_S,
            "POST_ATTACK_TAIL_S": POST_ATTACK_TAIL_S,
            "LAND_BOUND_S": LAND_BOUND_S,
            "CAPTURE_SLACK_S": CAPTURE_SLACK_S,
            "ATTACK_LAUNCH_MIN_S": ATTACK_LAUNCH_MIN_S,
            "ATTACK_LAUNCH_MAX_S": ATTACK_LAUNCH_MAX_S,
            "takeoff_guard_s": float(takeoff_guard_s),
        },
    }


# --------------------------------------------------------------------------- #
# bench (dry-run timeline; layer start offsets mirror orchestrate launch order)
# --------------------------------------------------------------------------- #
LAYER_START_S = {
    "sidecars (command/return probes, host, ACK, RLC, ue_iface)": 0.0,
    "host telemetry": 0.0,
    "UPF telemetry": 0.0,
    "forwarder c2_arrival": 15.0,
    "PRB capture": 15.0,
    "victim video": 15.0,
}
FORWARDER_EXTRA_S = 15.0


def layer_windows(capture_envelope_s: float) -> dict[str, list[float]]:
    out = {}
    for name, start in LAYER_START_S.items():
        extra = FORWARDER_EXTRA_S if name.startswith("forwarder") else 0.0
        out[name] = [start, round(start + float(capture_envelope_s) + extra, 3)]
    return out


def bench_checks(plan: dict[str, Any], *, orbit_s: Optional[float] = None) -> dict[str, Any]:
    orbit = plan["orbit_acquired_nominal_s"] if orbit_s is None else float(orbit_s)
    on_lo, on_hi = plan["planned_onset_s"]
    end_lo, end_hi = plan["planned_attack_end_s"]
    traj_end = plan["trajectory_end_s"]
    land = traj_end
    disarm_bound = land + LAND_BOUND_S
    layers = layer_windows(plan["capture_envelope_s"])
    capture_close = min(hi for _, hi in layers.values())
    checks = {
        "a_attack_within_orbit_trajectory": orbit <= on_lo and end_hi <= traj_end,
        "b_pre_stable_orbit_s": round(on_lo - orbit, 3),
        "b_pre_ge_30": (on_lo - orbit) >= P_PRE_S,
        "b_pre_ge_30_orbit_at_limit": (on_lo - plan["orbit_acquired_bound_s"]) >= P_PRE_S,
        "c_land_s": round(land, 3),
        "c_disarm_bound_s": round(disarm_bound, 3),
        "c_capture_close_s": round(capture_close, 3),
        "c_land_and_disarm_within_capture": disarm_bound <= capture_close,
        "c_planned_capture_end_within": plan["capture_end_s"] <= capture_close,
        "e_burst_s": plan["burst_s"],
        "e_burst_510": plan["burst_s"] == 510,
        "forwarder_covers_last_setpoint": layers["forwarder c2_arrival"][1] >= traj_end,
    }
    if plan["drain_ceiling_s"] > 0:
        checks["d_trajectory_covers_drain"] = traj_end >= end_hi + plan["drain_ceiling_s"]
        checks["d_capture_covers_drain"] = capture_close >= end_hi + plan["drain_ceiling_s"]
    checks["layers"] = layers
    return checks


def legacy_short_run_bench(duration_s: float, warmup_s: float, mission_len_s: float,
                           overhead_s: float = 30.0) -> dict[str, Any]:
    """Short runs keep the legacy trajectory (mission_len - overhead)."""
    margin = (float(duration_s) - float(mission_len_s)) / 2.0
    envelope = float(duration_s) + float(warmup_s) + BASE_ENVELOPE_PAD_S
    traj = max(30.0, float(mission_len_s) - overhead_s)
    traj_start = margin + T_TRAJ_START_FROM_LAUNCH_S
    traj_end = traj_start + traj
    layers = layer_windows(envelope)
    capture_close = min(hi for _, hi in layers.values())
    return {
        "duration_s": duration_s,
        "envelope_s": envelope,
        "mission_start_s": margin,
        "trajectory_start_nominal_s": round(traj_start, 3),
        "trajectory_duration_s": traj,
        "trajectory_end_s": round(traj_end, 3),
        "land_s": round(traj_end, 3),
        "disarm_bound_s": round(traj_end + LAND_BOUND_S, 3),
        "capture_close_s": capture_close,
        "forwarder_end_s": layers["forwarder c2_arrival"][1],
        "land_within_capture": traj_end <= capture_close,
        "disarm_bound_within_capture": traj_end + LAND_BOUND_S <= capture_close,
        "disarm_bound_within_forwarder": traj_end + LAND_BOUND_S <= layers["forwarder c2_arrival"][1],
    }


def bench(warmup_s: float = 10.0, takeoff_guard_s: float = 30.0) -> dict[str, Any]:
    cases = {
        "baseline": dict(requested_delay_s=0.0, drain_ceiling_s=0.0),
        "attack_without_drain": dict(requested_delay_s=45.0, drain_ceiling_s=0.0),
        "attack_with_drain": dict(requested_delay_s=45.0, drain_ceiling_s=180.0),
    }
    out: dict[str, Any] = {"warmup_s": warmup_s, "cases": {}}
    for name, kw in cases.items():
        plan = plan_schedule(duration_s=600, warmup_s=warmup_s, mission_len_s=420,
                             takeoff_guard_s=takeoff_guard_s, **kw)
        out["cases"][name] = {"plan": plan, "checks": bench_checks(plan)}
    out["short_runs_legacy_trajectory"] = {
        "60s": legacy_short_run_bench(60, warmup_s, int(round(60 * 2 / 3))),
        "180s": legacy_short_run_bench(180, warmup_s, 120),
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Option C schedule (dry-run bench)")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--warmup", type=float, default=10.0)
    args = ap.parse_args()
    if args.bench:
        print(json.dumps(bench(args.warmup), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

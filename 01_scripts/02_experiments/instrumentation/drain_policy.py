#!/usr/bin/env python3
##################################################################################################################################
# Module: drain_policy.py (DRAIN recovery policy: when the state counts as recovered after the attack ends)                      #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Convention (recorded in every run that opts in):                                                                  #
#                                                                                                                                #
# After attack_end, capture continues while either: (a) delta_p50 and belief_p50 are still outside the baseline band, or (b) the #
#   drain ceiling has not been reached.                                                                                          #
#                                                                                                                                #
# Baseline band (sliding window of WINDOW_S seconds ending at t): delta_p50_s  <= max(baseline_delta_p50_s + DELTA_ABS_S,        #
#   baseline_delta_p50_s * DELTA_FACTOR) belief_p50_m <= max(baseline_belief_p50_m + BELIEF_ABS_M, baseline_belief_p50_m *       #
#   BELIEF_FACTOR)                                                                                                               #
#                                                                                                                                #
# Defaults: WINDOW_S = 10.0 DELTA_ABS_S = 0.050 DELTA_FACTOR = 1.5 BELIEF_ABS_M = 0.30 BELIEF_FACTOR = 1.5 DRAIN_CEILING_S =     #
#   180.0                                                                                                                        #
#                                                                                                                                #
# Source of baseline_delta_p50 / baseline_belief_p50 (field baseline_source): * pre_window_same_run: PRE window of the same run  #
#   (default when there is no --drain-baseline-run) * separate_baseline_run: separate baseline run (path in baseline_ref_run) *  #
#   unset: flag on but values not resolved yet (filled in during post-processing)                                                #
#                                                                                                                                #
# Live early-stop: envelope extended by drain_ceiling_s; live early-stop is NOT VERIFIED on the offline bench.                   #
##################################################################################################################################
"""DRAIN recovery policy: when the state counts as recovered after the attack ends.

Convention (recorded in every run that opts in):

  After attack_end, capture continues while either:
    (a) delta_p50 and belief_p50 are still outside the baseline band, or
    (b) the drain ceiling has not been reached.

  Baseline band (sliding window of WINDOW_S seconds ending at t):
    delta_p50_s  <= max(baseline_delta_p50_s + DELTA_ABS_S,
                        baseline_delta_p50_s * DELTA_FACTOR)
    belief_p50_m <= max(baseline_belief_p50_m + BELIEF_ABS_M,
                        baseline_belief_p50_m * BELIEF_FACTOR)

  Defaults:
    WINDOW_S = 10.0
    DELTA_ABS_S = 0.050
    DELTA_FACTOR = 1.5
    BELIEF_ABS_M = 0.30
    BELIEF_FACTOR = 1.5
    DRAIN_CEILING_S = 180.0

  Source of baseline_delta_p50 / baseline_belief_p50 (field baseline_source):
    * pre_window_same_run: PRE window of the same run (default when there is no
      --drain-baseline-run)
    * separate_baseline_run: separate baseline run (path in baseline_ref_run)
    * unset: flag on but values not resolved yet (filled in during
      post-processing)

  Live early-stop: envelope extended by drain_ceiling_s; live early-stop is
  NOT VERIFIED on the offline bench.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

SCHEMA = "flyblind_drain_policy_v2"

WINDOW_S = 10.0
DELTA_ABS_S = 0.050
DELTA_FACTOR = 1.5
BELIEF_ABS_M = 0.30
BELIEF_FACTOR = 1.5
DRAIN_CEILING_S = 180.0

BASELINE_SOURCE_PRE = "pre_window_same_run"
BASELINE_SOURCE_SEPARATE = "separate_baseline_run"
BASELINE_SOURCE_UNSET = "unset"


def default_policy(
    *,
    enabled: bool,
    drain_ceiling_s: float = DRAIN_CEILING_S,
    baseline_delta_p50_s: Optional[float] = None,
    baseline_belief_p50_m: Optional[float] = None,
    baseline_ref_run: Optional[str] = None,
    baseline_source: Optional[str] = None,
    video_mode: str = "a_continue",
) -> dict[str, Any]:
    if baseline_source is None:
        if baseline_ref_run:
            baseline_source = BASELINE_SOURCE_SEPARATE
        elif enabled:
            baseline_source = BASELINE_SOURCE_PRE
        else:
            baseline_source = BASELINE_SOURCE_UNSET
    return {
        "schema": SCHEMA,
        "enabled": bool(enabled),
        "video_mode": video_mode,  # a_continue | b_stop_at_attack_end
        "window_s": WINDOW_S,
        "delta_abs_s": DELTA_ABS_S,
        "delta_factor": DELTA_FACTOR,
        "belief_abs_m": BELIEF_ABS_M,
        "belief_factor": BELIEF_FACTOR,
        "drain_ceiling_s": float(drain_ceiling_s),
        "baseline_delta_p50_s": baseline_delta_p50_s,
        "baseline_belief_p50_m": baseline_belief_p50_m,
        "baseline_ref_run": baseline_ref_run,
        "baseline_source": baseline_source,
        "baseline_source_note": {
            BASELINE_SOURCE_PRE: (
                "baseline_delta_p50 and baseline_belief_p50 come from the PRE window "
                "([mission_start, attack_onset)) of the same run, via phase_windows"
            ),
            BASELINE_SOURCE_SEPARATE: (
                "values read from the separate baseline run in baseline_ref_run "
                "(typically freshness/belief of attack=None)"
            ),
            BASELINE_SOURCE_UNSET: (
                "source not resolved yet; filled in during post-processing"
            ),
        }.get(baseline_source, "unknown"),
        "criterion_text": (
            "Back to baseline when, in a sliding window of "
            f"{WINDOW_S:g} s after attack_end, delta_p50 <= "
            f"max(baseline_delta_p50 + {DELTA_ABS_S:g}s, "
            f"baseline_delta_p50 * {DELTA_FACTOR:g}) AND belief_p50 <= "
            f"max(baseline_belief_p50 + {BELIEF_ABS_M:g}m, "
            f"baseline_belief_p50 * {BELIEF_FACTOR:g}), "
            f"or when the ceiling drain_ceiling_s={drain_ceiling_s:g}s is reached. "
            f"Baseline source: {baseline_source}."
        ),
        "live_early_stop": (
            "envelope extended by drain_ceiling_s; live early-stop "
            "requires telemetry polling (NOT VERIFIED on the offline bench)"
        ),
    }


def band_limits(
    baseline_delta_p50_s: float,
    baseline_belief_p50_m: float,
) -> dict[str, float]:
    return {
        "delta_p50_max_s": max(
            baseline_delta_p50_s + DELTA_ABS_S,
            baseline_delta_p50_s * DELTA_FACTOR,
        ),
        "belief_p50_max_m": max(
            baseline_belief_p50_m + BELIEF_ABS_M,
            baseline_belief_p50_m * BELIEF_FACTOR,
        ),
    }


def in_baseline_band(
    *,
    delta_p50_s: Optional[float],
    belief_p50_m: Optional[float],
    baseline_delta_p50_s: float,
    baseline_belief_p50_m: float,
) -> tuple[bool, dict[str, Any]]:
    lim = band_limits(baseline_delta_p50_s, baseline_belief_p50_m)
    ok_delta = (
        isinstance(delta_p50_s, (int, float))
        and float(delta_p50_s) <= lim["delta_p50_max_s"]
    )
    ok_belief = (
        isinstance(belief_p50_m, (int, float))
        and float(belief_p50_m) <= lim["belief_p50_max_m"]
    )
    return ok_delta and ok_belief, {
        "limits": lim,
        "delta_p50_s": delta_p50_s,
        "belief_p50_m": belief_p50_m,
        "ok_delta": ok_delta,
        "ok_belief": ok_belief,
    }


def write_drain_policy(run_dir: Path, policy: dict[str, Any]) -> Path:
    path = Path(run_dir) / "drain_policy.json"
    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    return path


def evaluate_recovery_from_summary(
    *,
    attack_end_s: float,
    drain_ceiling_s: float,
    samples: list[dict[str, Any]],
    baseline_delta_p50_s: float,
    baseline_belief_p50_m: float,
) -> dict[str, Any]:
    recovered_at: Optional[float] = None
    detail: Optional[dict[str, Any]] = None
    for s in samples:
        t = float(s["t_rel_s"])
        if t < attack_end_s:
            continue
        if t - attack_end_s > drain_ceiling_s:
            break
        ok, d = in_baseline_band(
            delta_p50_s=s.get("delta_p50_s"),
            belief_p50_m=s.get("belief_p50_m"),
            baseline_delta_p50_s=baseline_delta_p50_s,
            baseline_belief_p50_m=baseline_belief_p50_m,
        )
        if ok:
            recovered_at = t
            detail = d
            break
    if recovered_at is not None:
        return {
            "recovered": True,
            "recovery_t_rel_s": recovered_at,
            "recovery_time_s": recovered_at - attack_end_s,
            "hit_ceiling": False,
            "detail": detail,
        }
    return {
        "recovered": False,
        "recovery_t_rel_s": None,
        "recovery_time_s": None,
        "hit_ceiling": True,
        "ceiling_s": drain_ceiling_s,
        "detail": None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="DRAIN policy helpers (1.11)")
    ap.add_argument("--print-default", action="store_true")
    ap.add_argument("--drain-ceiling-s", type=float, default=DRAIN_CEILING_S)
    ap.add_argument(
        "--baseline-source",
        choices=[BASELINE_SOURCE_PRE, BASELINE_SOURCE_SEPARATE, BASELINE_SOURCE_UNSET],
        default=None,
    )
    ap.add_argument("--baseline-ref-run", default=None)
    args = ap.parse_args()
    if args.print_default:
        print(json.dumps(
            default_policy(
                enabled=True,
                drain_ceiling_s=args.drain_ceiling_s,
                baseline_source=args.baseline_source,
                baseline_ref_run=args.baseline_ref_run,
            ),
            indent=2,
            ensure_ascii=False,
        ))
        return
    ap.error("use --print-default")


if __name__ == "__main__":
    main()

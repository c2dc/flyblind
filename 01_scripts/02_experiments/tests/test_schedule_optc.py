#!/usr/bin/env python3
##################################################################################################################################
# Module: test_schedule_optc.py (Option C schedule (bench a-e) and orbit/land phase windows)                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
##################################################################################################################################
"""Option C schedule (bench a-e) and orbit/land phase windows."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "instrumentation"))
sys.path.insert(0, str(ROOT))

from instrumentation.phase_windows import (  # noqa: E402
    build_windows,
    derive_mission_markers,
    load_run_windows,
    write_run_phase_windows,
)
from instrumentation.schedule_optc import bench, optc_applies, plan_schedule  # noqa: E402

T0 = 1_000_000_000_000


class TestScheduleOptC(unittest.TestCase):
    def test_bench_checks_a_to_e(self):
        out = bench(10.0)
        for name, case in out["cases"].items():
            c = case["checks"]
            self.assertTrue(c["a_attack_within_orbit_trajectory"], name)
            self.assertTrue(c["b_pre_ge_30"], name)
            self.assertTrue(c["b_pre_ge_30_orbit_at_limit"], name)
            self.assertTrue(c["c_land_and_disarm_within_capture"], name)
            self.assertTrue(c["c_planned_capture_end_within"], name)
            self.assertTrue(c["e_burst_510"], name)
            self.assertTrue(c["forwarder_covers_last_setpoint"], name)
        drain = out["cases"]["attack_with_drain"]["checks"]
        self.assertTrue(drain["d_trajectory_covers_drain"])
        self.assertTrue(drain["d_capture_covers_drain"])

    def test_baseline_same_schedule_as_attack_without_drain(self):
        kw = dict(duration_s=600, warmup_s=10, mission_len_s=420, takeoff_guard_s=30.0)
        base = plan_schedule(requested_delay_s=0.0, **kw)
        att = plan_schedule(requested_delay_s=45.0, **kw)
        for key in ("onset_delay_s", "burst_s", "trajectory_end_s", "capture_envelope_s"):
            self.assertEqual(base[key], att[key], key)

    def test_short_runs_not_option_c(self):
        self.assertFalse(optc_applies("setpoint-stream", 180))
        self.assertFalse(optc_applies("waypoint", 600))
        self.assertTrue(optc_applies("setpoint-stream", 600))
        short = bench(10.0)["short_runs_legacy_trajectory"]
        self.assertTrue(short["180s"]["land_within_capture"])
        self.assertTrue(short["180s"]["disarm_bound_within_capture"])


def _write_run(run: Path, *, condition: str, markers: list[tuple[str, float]], schedule=None):
    (run / "clock_anchor.json").write_text(json.dumps({"T0_mono_ns": T0}))
    with (run / "event_markers.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event", "t_rel_s", "note"])
        for ev, t in markers:
            w.writerow([ev, t, ""])
    with (run / "setpoints.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["logical_setpoint_id", "t_generation_gcs_ns"])
        for i in range(5):
            w.writerow([i + 1, T0 + int((141.3 + i * 100.0) * 1e9)])
    (run / "c2.json").write_text(json.dumps({
        "trajectory": {"trajectory_t0_rel_s": 50.951},
        "physics_metrics": {
            "trajectory_acquisition": {"orbit_stream_started_at_rel_s": 52.952},
            "flight_state": {"mode_transitions": [
                {"t_rel_s": 12.0, "from": "LAND", "to": "GUIDED"},
                {"t_rel_s": 452.0, "from": "GUIDED", "to": "LAND"},
            ]},
        },
    }))
    (run / "run_meta.json").write_text(json.dumps({"condition": condition, "schedule_optc": schedule}))


class TestOrbitLandWindows(unittest.TestCase):
    def test_derive_markers_maps_mission_clock_to_t0(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            _write_run(run, condition="attack", markers=[("run_start", 0.0)])
            m = derive_mission_markers(run)
            self.assertAlmostEqual(m["orbit_acquired"]["t_rel_s"], 141.3 + 52.952 - 50.951, places=3)
            self.assertAlmostEqual(m["land_command"]["t_rel_s"], 541.3, places=3)
            self.assertAlmostEqual(m["guided_to_land_observed"]["t_rel_s"], 141.3 + 452.0 - 50.951, places=3)

    def test_attack_pre_from_orbit_drain_until_land(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            _write_run(run, condition="attack", markers=[
                ("run_start", 0.0), ("mission_start", 90.0),
                ("attack_onset", 190.0), ("attack_end", 500.0), ("run_end", 800.0),
            ])
            data = write_run_phase_windows(run)
            w = data["windows"]
            self.assertAlmostEqual(w["PRE"]["t_rel_lo_s"], 143.301, places=3)
            self.assertEqual(w["PRE"]["t_rel_hi_s"], 190.0)
            self.assertAlmostEqual(w["DRAIN"]["t_rel_hi_s"], 541.3, places=3)
            self.assertFalse(data["virtual_attack"])
            self.assertTrue(data["schedule_check"]["attack_within_orbit_and_trajectory"])
            self.assertEqual(load_run_windows(run), w)

    def test_baseline_uses_virtual_markers(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            sched = {"virtual_attack_onset_s": 188.3, "virtual_attack_end_s": 488.3}
            _write_run(run, condition="baseline", schedule=sched, markers=[
                ("run_start", 0.0), ("mission_start", 90.0), ("run_end", 800.0),
            ])
            data = write_run_phase_windows(run)
            self.assertTrue(data["virtual_attack"])
            self.assertEqual(sorted(data["windows"]), ["ATTACK", "DRAIN", "PRE"])
            self.assertEqual(data["windows"]["ATTACK"]["t_rel_lo_s"], 188.3)
            self.assertIn("virtual_attack_onset", data["markers_added"])

    def test_legacy_markers_unchanged(self):
        w = build_windows({"run_start": 0.0, "mission_start": 90.0, "attack_onset": 120.0,
                           "attack_end": 630.0, "run_end": 700.0})["windows"]
        self.assertEqual(w["PRE"]["t_rel_lo_s"], 90.0)
        self.assertEqual(w["DRAIN"]["t_rel_hi_s"], 700.0)


if __name__ == "__main__":
    unittest.main()

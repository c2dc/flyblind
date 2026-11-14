#!/usr/bin/env python3
##################################################################################################################################
# Module: test_trk_truth.py (unit tests — compute_trk_truth)                                                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: SITL-egress tracking truth join tests (report-only metrics).                                                      #
##################################################################################################################################

"""Unit tests for trk_truth (SITL egress vs planned setpoint)."""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_sitl" / "lib"))

from freshness_metrics import compute_trk_truth  # noqa: E402


class TestTrkTruth(unittest.TestCase):
    def test_truth_near_plan_when_uav_on_ref(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            t0 = 1_000_000_000
            with (run / "c2_telemetry_uav.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f, fieldnames=["site", "t_mono_ns", "mavlink_sequence",
                                   "mavlink_system_id", "time_boot_ms", "x", "y", "z"],
                )
                w.writeheader()
                for i in range(5):
                    w.writerow({
                        "site": "uav", "t_mono_ns": t0 + i * 50_000_000,
                        "mavlink_sequence": i, "mavlink_system_id": 1,
                        "time_boot_ms": 1000 + i * 50,
                        "x": 30.0, "y": 0.0, "z": -10.0,
                    })
            with (run / "setpoints.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "logical_setpoint_id", "t_generation_gcs_ns",
                        "x_ref", "y_ref", "z_ref", "trajectory_window",
                    ],
                )
                w.writeheader()
                for i in range(3):
                    w.writerow({
                        "logical_setpoint_id": i + 1,
                        "t_generation_gcs_ns": t0 + (i + 1) * 100_000_000,
                        "x_ref": 30.0, "y_ref": 0.0, "z_ref": -10.0,
                        "trajectory_window": "steady",
                    })
            out = compute_trk_truth(run)
            self.assertTrue(out["ok"])
            self.assertEqual(out["setpoints_joined"], 3)
            self.assertAlmostEqual(out["tracking_error_truth_m"]["p50"], 0.0, places=3)
            self.assertAlmostEqual(out["steady_tracking_error_truth_m"]["p99"], 0.0, places=3)

    def test_truth_captures_physical_offset(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            t0 = 2_000_000_000
            with (run / "c2_telemetry_uav.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f, fieldnames=["site", "t_mono_ns", "x", "y", "z"],
                )
                w.writeheader()
                w.writerow({"site": "uav", "t_mono_ns": t0, "x": 0.0, "y": 0.0, "z": -10.0})
                w.writerow({"site": "uav", "t_mono_ns": t0 + 200_000_000,
                            "x": 10.0, "y": 0.0, "z": -10.0})
            with (run / "setpoints.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=["logical_setpoint_id", "t_generation_gcs_ns",
                                "x_ref", "y_ref", "z_ref", "trajectory_window"],
                )
                w.writeheader()
                # Plan at (0,0); truth at generation is still (0,0) → err≈0
                w.writerow({
                    "logical_setpoint_id": 1, "t_generation_gcs_ns": t0 + 50_000_000,
                    "x_ref": 0.0, "y_ref": 0.0, "z_ref": -10.0,
                    "trajectory_window": "acquisition",
                })
                # Plan still (0,0); truth has moved to x=10 → err≈10
                w.writerow({
                    "logical_setpoint_id": 2, "t_generation_gcs_ns": t0 + 250_000_000,
                    "x_ref": 0.0, "y_ref": 0.0, "z_ref": -10.0,
                    "trajectory_window": "steady",
                })
            out = compute_trk_truth(run)
            self.assertTrue(out["ok"])
            self.assertEqual(out["setpoints_joined"], 2)
            self.assertAlmostEqual(out["steady_tracking_error_truth_m"]["p50"], 10.0, places=2)


if __name__ == "__main__":
    unittest.main()

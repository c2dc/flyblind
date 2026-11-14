#!/usr/bin/env python3
##################################################################################################################################
# Module: test_trajectory.py (unit tests — trajectory.py)                                                                        #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Reference path sampling and phase alignment tests.                                                                #
##################################################################################################################################

"""Unit tests for setpoint-stream trajectories."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

SITL_LIB = Path(__file__).resolve().parents[2] / "03_sitl" / "lib"
sys.path.insert(0, str(SITL_LIB))

from trajectory import TrajectoryConfig, initial_phase_from_xy, sample, tracking_errors  # noqa: E402


class TestTrajectory(unittest.TestCase):
    def test_circle_on_radius(self):
        cfg = TrajectoryConfig(name="circle", duration_s=60.0, radius_m=30.0, period_s=20.0)
        ref = sample(cfg, 0.0, alt_m=10.0)
        self.assertAlmostEqual(ref.x_m, 30.0, places=3)
        self.assertAlmostEqual(ref.y_m, 0.0, places=3)
        self.assertAlmostEqual(ref.z_m, -10.0, places=3)

    def test_circle_cross_track_zero_at_ref(self):
        cfg = TrajectoryConfig(name="circle", duration_s=60.0, radius_m=25.0, period_s=10.0)
        ref = sample(cfg, 5.0, alt_m=10.0)
        errs = tracking_errors(cfg, 5.0, 10.0, ref.x_m, ref.y_m, ref.z_m)
        self.assertLess(errs["cross_track_error_m"], 0.01)
        self.assertLess(errs["tracking_error_m"], 0.01)

    def test_phase_offset_aligns_t0_with_uav_on_circle(self):
        cfg = TrajectoryConfig(
            name="circle", duration_s=60.0, radius_m=30.0, period_s=20.0,
            phase_offset_rad=initial_phase_from_xy(28.0, 0.0, TrajectoryConfig(
                name="circle", duration_s=60.0, radius_m=30.0, period_s=20.0,
            )),
        )
        ref = sample(cfg, 0.0, alt_m=10.0)
        errs = tracking_errors(cfg, 0.0, 10.0, 28.0, 0.0, -10.0)
        self.assertAlmostEqual(ref.x_m, 30.0, places=2)
        self.assertLess(errs["cross_track_error_m"], 3.0)

    def test_initial_phase_at_center_defaults_zero(self):
        cfg = TrajectoryConfig(name="circle", duration_s=60.0, radius_m=30.0, period_s=20.0)
        self.assertAlmostEqual(initial_phase_from_xy(0.0, 0.0, cfg), 0.0)

    def test_figure8_finite(self):
        cfg = TrajectoryConfig(name="figure8", duration_s=40.0, radius_m=20.0, period_s=8.0)
        for t in (0.0, 2.0, 4.0, 6.0):
            ref = sample(cfg, t, alt_m=10.0)
            self.assertTrue(math.isfinite(ref.x_m))
            self.assertTrue(math.isfinite(ref.y_m))

    def test_sine_longitudinal(self):
        cfg = TrajectoryConfig(name="sine", duration_s=30.0, speed_mps=5.0, amplitude_m=10.0, period_s=5.0)
        ref0 = sample(cfg, 0.0, alt_m=10.0)
        ref5 = sample(cfg, 5.0, alt_m=10.0)
        self.assertGreater(ref5.x_m, ref0.x_m)


if __name__ == "__main__":
    unittest.main()

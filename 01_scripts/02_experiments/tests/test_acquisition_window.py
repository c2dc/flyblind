#!/usr/bin/env python3
##################################################################################################################################
# Module: test_acquisition_window.py (unit tests — setpoint acquisition window)                                                  #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Cross-track acquisition threshold tests for setpoint_stream.                                                      #
##################################################################################################################################

"""Unit tests for pre-arm gate and orbit entry hold."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SITL_LIB = Path(__file__).resolve().parents[2] / "03_sitl" / "lib"
sys.path.insert(0, str(SITL_LIB))

from freshness_metrics import validate_freshness_integrity  # noqa: E402
from setpoint_stream import (  # noqa: E402
    _effective_trajectory_t,
    _rephase_trajectory_on_circle,
    _update_acquisition_end,
)
from trajectory import TrajectoryConfig, sample  # noqa: E402


class TestAcquisitionWindow(unittest.TestCase):
    def test_holds_for_two_seconds_below_threshold(self):
        end, below = _update_acquisition_end(5.0, 10.0, acquisition_end_mono=None, below_since_mono=None)
        self.assertIsNone(end)
        self.assertEqual(below, 10.0)
        end, below = _update_acquisition_end(5.0, 11.5, acquisition_end_mono=end, below_since_mono=below)
        self.assertIsNone(end)
        end, below = _update_acquisition_end(5.0, 12.0, acquisition_end_mono=end, below_since_mono=below)
        self.assertEqual(end, 12.0)

    def test_resets_hold_when_cross_track_spikes(self):
        end, below = _update_acquisition_end(5.0, 1.0, acquisition_end_mono=None, below_since_mono=None)
        end, below = _update_acquisition_end(15.0, 1.5, acquisition_end_mono=end, below_since_mono=below)
        self.assertIsNone(end)
        self.assertIsNone(below)


class TestOrbitEntryHold(unittest.TestCase):
    def test_effective_t_frozen_until_acquisition(self):
        self.assertEqual(_effective_trajectory_t(100.0, None), 0.0)
        self.assertEqual(_effective_trajectory_t(100.0, 50.0), 50.0)

    def test_rephase_aligns_circle_to_uav(self):
        traj = TrajectoryConfig(name="circle", duration_s=180.0, radius_m=30.0, period_s=60.0)
        reph = _rephase_trajectory_on_circle(traj, 0.0, 30.0)
        self.assertAlmostEqual(reph.phase_offset_rad, math.pi / 2, places=5)
        ref = sample(reph, 0.0, 10.0)
        self.assertAlmostEqual(ref.x_m, 0.0, places=3)
        self.assertAlmostEqual(ref.y_m, 30.0, places=3)


class TestFreshnessSanity(unittest.TestCase):
    def test_valid_window_passes(self):
        fresh = {
            "schema_version": "freshness_v2",
            "active_window_start_ns": 1_000_000_000,
            "active_window_end_ns": 91_000_000_000,
            "delivery_ratio": 1.0,
            "capture_ratio": 1.0,
            "correlation_ratio": 1.0,
            "aoi_network_ms": {"p99": 110.0},
        }
        ok, info = validate_freshness_integrity(fresh)
        self.assertTrue(ok, info)


if __name__ == "__main__":
    unittest.main()

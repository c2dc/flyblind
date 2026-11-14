#!/usr/bin/env python3
##################################################################################################################################
# Module: test_belief_validation.py (unit tests — belief_vs_truth validation)                                                    #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Physical consistency checks for belief_vs_truth diagnostic.                                                       #
##################################################################################################################################

"""Unit tests for belief_vs_truth integrity (clock offset + physical check)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITL_LIB = ROOT.parent / "03_sitl" / "lib"
PROBES = ROOT / "probes"
sys.path.insert(0, str(SITL_LIB))
sys.path.insert(0, str(PROBES))

from freshness_metrics import validate_belief_vs_truth  # noqa: E402
from mono_clock_offset import summarize_offsets_ms  # noqa: E402


class TestMonoClockOffsetSummary(unittest.TestCase):
    def test_negligible_offset(self):
        # kubectl jitter ~few ms, no systematic skew
        host_uav = [12.0, 15.0, 9.0, 14.0, 11.0, 13.0, 10.0, 16.0, 12.0, 14.0]
        host_gcs = [11.0, 14.0, 10.0, 13.0, 12.0, 12.0, 11.0, 15.0, 13.0, 13.0]
        s = summarize_offsets_ms(host_uav, host_gcs)
        self.assertLess(s["skew_abs_median_ms"], 5.0)
        self.assertIn("measured inter-pod", s["appendix_line"])

    def test_systematic_skew(self):
        host_uav = [500.0] * 10
        host_gcs = [0.0] * 10
        s = summarize_offsets_ms(host_uav, host_gcs)
        self.assertGreater(s["skew_abs_median_ms"], 400.0)


class TestBeliefPhysicalValidation(unittest.TestCase):
    def _tel(self, belief_p50, belief_p99, gap_p50, gap_p99, n=100):
        return {
            "belief_matched": n,
            "belief_vs_truth_m": {"p50": belief_p50, "p99": belief_p99},
            "inter_arrival_gap_ms": {"p50": gap_p50, "p99": gap_p99},
        }

    def test_physical_consistent_baseline_like(self):
        # 1.9 m belief, gap 400 ms, v=5 m/s → expected 2.0 m
        tel = self._tel(1.9, 2.1, 400.0, 500.0)
        clock = {
            "ok": True,
            "skew_abs_median_ms": 3.0,
            "jitter_spread_ms": 20.0,
            "appendix_line": "measured inter-pod CLOCK_MONOTONIC offset: median 3 ms",
        }
        v = validate_belief_vs_truth(tel, speed_mps=5.0, clock_offset=clock)
        self.assertTrue(v["physical_consistent"])
        self.assertTrue(v["clock_ok"])
        self.assertEqual(v["status"], "validated")
        self.assertAlmostEqual(v["belief_expected_p50_m"], 2.0, places=2)

    def test_physical_inconsistent_flags_diagnostic(self):
        # belief 40 m but gap 300 ms @ 5 m/s → expected 1.5 m
        tel = self._tel(40.0, 55.0, 300.0, 400.0)
        clock = {
            "ok": True,
            "skew_abs_median_ms": 2.0,
            "jitter_spread_ms": 15.0,
            "appendix_line": "ok",
        }
        v = validate_belief_vs_truth(tel, speed_mps=5.0, clock_offset=clock)
        self.assertFalse(v["physical_consistent"])
        self.assertEqual(v["status"], "diagnostic_only")
        self.assertIn("FLAG", v["reason"])

    def test_clock_skew_flags_diagnostic_even_if_phys_ok(self):
        tel = self._tel(1.9, 2.1, 400.0, 500.0)
        clock = {
            "ok": True,
            "skew_abs_median_ms": 500.0,
            "jitter_spread_ms": 20.0,
            "appendix_line": "skewed",
        }
        v = validate_belief_vs_truth(tel, speed_mps=5.0, clock_offset=clock)
        self.assertTrue(v["physical_consistent"])
        self.assertFalse(v["clock_ok"])
        self.assertEqual(v["status"], "diagnostic_only")

    def test_phys_ok_without_clock_is_provisional(self):
        tel = self._tel(1.9, 2.1, 400.0, 500.0)
        v = validate_belief_vs_truth(tel, speed_mps=5.0, clock_offset=None)
        self.assertEqual(v["status"], "validated_physical_only")


class TestBeliefAoIAnchored(unittest.TestCase):
    """When delivery AoI is present, belief age = AoI + gap (not gap alone)."""

    def _tel(self, belief_p50, belief_p99, aoi_p50, aoi_p99, gap_p50=230.0, gap_p99=560.0, n=200):
        return {
            "belief_matched": n,
            "belief_vs_truth_m": {"p50": belief_p50, "p99": belief_p99},
            "telemetry_arrival_delay_ms": {"p50": aoi_p50, "p99": aoi_p99},
            "inter_arrival_gap_ms": {"p50": gap_p50, "p99": gap_p99},
        }

    def _clock_ok(self):
        return {
            "ok": True,
            "skew_abs_median_ms": 2.5,
            "jitter_spread_ms": 35.0,
            "appendix_line": "measured inter-pod CLOCK_MONOTONIC offset: median 2.5 ms",
        }

    def test_bufferbloat_hallucination_validates_against_gap_false_flag(self):
        # Recorded N=4 UL run: belief 6.67/50.5 m, AoI ~12 s, v=7.47 m/s, Ø=60 m.
        # Legacy gap anchor wrongly flagged it; AoI + geometric cap validates it.
        tel = self._tel(6.671, 50.536, 11984.353, 12364.599)
        v = validate_belief_vs_truth(
            tel, speed_mps=7.471, clock_offset=self._clock_ok(),
            trajectory_diameter_m=60.0,
        )
        self.assertEqual(v["anchor"], "delivery_AoI+gap")
        self.assertTrue(v["physical_consistent"])
        self.assertTrue(v["clock_ok"])
        self.assertEqual(v["status"], "validated")
        # belief age anchored on AoI (~12 s), not the 0.23 s gap.
        self.assertGreater(v["belief_age_p50_s"], 11.0)
        # expected max is capped by trajectory diameter (arc would be ~90 m).
        self.assertAlmostEqual(v["belief_expected_p50_m"], 60.0, places=1)

    def test_geometric_cap_flags_beyond_trajectory_extent(self):
        # belief 90 m on a 60 m-wide trajectory is impossible → skew/artifact.
        tel = self._tel(90.0, 120.0, 11984.0, 12364.0)
        v = validate_belief_vs_truth(
            tel, speed_mps=7.471, clock_offset=self._clock_ok(),
            trajectory_diameter_m=60.0,
        )
        self.assertFalse(v["physical_consistent"])
        self.assertEqual(v["status"], "diagnostic_only")
        self.assertIn("FLAG", v["reason"])

    def test_aoi_anchor_without_diameter_uses_arc_bound(self):
        # No geometry available → bound by speed × age (arc). 50 m << ~90 m arc.
        tel = self._tel(6.671, 50.536, 11984.353, 12364.599)
        v = validate_belief_vs_truth(
            tel, speed_mps=7.471, clock_offset=self._clock_ok(),
            trajectory_diameter_m=None,
        )
        self.assertEqual(v["anchor"], "delivery_AoI+gap")
        self.assertTrue(v["physical_consistent"])
        self.assertIsNone(v["trajectory_diameter_m"])
        self.assertGreater(v["belief_expected_p50_m"], 80.0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
##################################################################################################################################
# Module: test_freshness_metrics.py (unit tests — freshness_metrics)                                                             #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: AoI, correlation, and stale-interval metric tests.                                                                #
##################################################################################################################################

"""Unit tests for freshness / AoI metrics (no cluster required)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SITL_LIB = Path(__file__).resolve().parents[2] / "03_sitl" / "lib"
sys.path.insert(0, str(SITL_LIB))

from freshness_metrics import (  # noqa: E402
    SetpointRecord,
    build_aoi_generation_timeseries,
    build_aoi_timeseries,
    build_freshness_summary,
    compute_stale_intervals,
    correlate_proxy_by_order,
    correlate_proxy_by_setpoint_id,
    correlate_proxy_by_stream_order,
    merge_proxy_artifacts,
    merge_telemetry_downlink,
    miss_ratio,
)


def _sp(i: int, t_gen: int, t_send: int, x: float = 0.0) -> SetpointRecord:
    return SetpointRecord(
        logical_setpoint_id=i,
        generation_index=i,
        trajectory_id="test",
        t_generation_gcs_ns=t_gen,
        t_send_gcs_ns=t_send,
        x_ref=x,
        y_ref=0.0,
        z_ref=-10.0,
    )


class TestFreshnessMetrics(unittest.TestCase):
    def test_miss_ratio_network(self):
        vals = [10.0, 30.0, 60.0, None]
        self.assertAlmostEqual(miss_ratio(vals, 50.0), 0.333, places=2)

    def test_stale_intervals(self):
        times = [0, 10_000_000, 20_000_000, 30_000_000, 40_000_000]
        aoi = [10.0, 60.0, 80.0, 40.0, 10.0]
        stale = compute_stale_intervals(times, aoi, 50.0)
        self.assertEqual(stale["number_of_stale_intervals"], 1)
        self.assertAlmostEqual(stale["longest_continuous_stale_interval_ms"], 20.0, places=1)

    def test_correlate_proxy_by_order(self):
        sps = [_sp(1, 1_000_000_000, 1_001_000_000, 1.0), _sp(2, 2_000_000_000, 2_001_000_000, 2.0)]
        fwd = [
            {"mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED", "t_arrival_ns": 1_050_000_000, "t_forward_ns": 1_050_100_000},
            {"mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED", "t_arrival_ns": 2_050_000_000, "t_forward_ns": 2_050_100_000},
        ]
        matched, total = correlate_proxy_by_order(sps, fwd)
        self.assertEqual(matched, 2)
        self.assertEqual(total, 2)
        self.assertAlmostEqual(sps[0].network_arrival_delay_ms(), 49.0, places=1)

    def test_aoi_uses_last_received_setpoint(self):
        sps = [_sp(1, 0, 0), _sp(2, 100_000_000, 100_000_000)]
        sps[0].t_arrival_uav_proxy_ns = 10_000_000
        sps[1].t_arrival_uav_proxy_ns = 110_000_000
        samples = build_aoi_timeseries(sps, 200_000_000, 0, sample_hz=10.0, nominal_period_ms=100.0)
        mid = samples[len(samples) // 2]
        self.assertEqual(mid.latest_setpoint_id, 1)
        last = samples[-1]
        self.assertEqual(last.latest_setpoint_id, 2)
        self.assertGreater(last.aoi_network_ms or 0, 0)

    def test_merge_proxy_artifacts(self):
        sps = [_sp(i, i * 100_000_000, i * 100_000_000 + 1_000_000, float(i)) for i in range(1, 6)]
        fwd = [
            {
                "mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED",
                "t_arrival_ns": i * 100_000_000 + 5_000_000,
                "t_forward_ns": i * 100_000_000 + 5_010_000,
            }
            for i in range(1, 6)
        ]
        t0 = sps[0].t_generation_gcs_ns
        t_active_end = t0 + 500_000_000
        sps2, aoi, meta = merge_proxy_artifacts(
            sps, fwd, [], 100.0, t0, t_active_end_ns=t_active_end, post_stream_s=30.0
        )
        self.assertEqual(meta["proxy_forward_matched"], 5)
        self.assertIn("freshness_summary", meta)
        fresh = meta["freshness_summary"]
        self.assertEqual(fresh.get("schema_version"), "freshness_v2")
        self.assertIn("active_stream", fresh)
        self.assertIn("post_stream", fresh)
        self.assertEqual(fresh["measurement_window_ns"]["t_active_end_ns"], t_active_end)
        self.assertIn("correlation_ratio", fresh)
        self.assertIn("delivery_ratio", fresh)
        self.assertIn("capture_ratio", fresh)
        acct = fresh.get("setpoint_accounting") or {}
        self.assertEqual(acct.get("generated_setpoints"), 5)
        self.assertIsNotNone(sps2[0].network_arrival_delay_ms())

    def test_post_stream_excluded_from_primary_aoi(self):
        sps = [_sp(i, i * 100_000_000, i * 100_000_000 + 1_000_000, float(i)) for i in range(1, 4)]
        fwd = [
            {
                "mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED",
                "t_arrival_ns": i * 100_000_000 + 5_000_000,
                "t_forward_ns": i * 100_000_000 + 5_010_000,
            }
            for i in range(1, 4)
        ]
        t0 = sps[0].t_generation_gcs_ns
        t_active_end = t0 + 200_000_000
        _, aoi_active, meta = merge_proxy_artifacts(
            sps, fwd, [], 100.0, t0, t_active_end_ns=t_active_end, post_stream_s=30.0
        )
        fresh = meta["freshness_summary"]
        self.assertFalse(fresh.get("post_stream_included_in_primary_stats"))
        self.assertGreater(fresh.get("active_samples", 0), 0)
        self.assertGreater(fresh.get("post_stream_samples", 0), 0)

    def test_owd_miss_vs_aoi_miss(self):
        """OWD miss uses per-setpoint delay; AoI miss uses continuous 100 Hz samples."""
        period_ms = 100.0
        sps = [_sp(i, i * 100_000_000, i * 100_000_000 + 5_000_000, float(i)) for i in range(1, 6)]
        for sp in sps:
            sp.t_arrival_uav_proxy_ns = sp.t_send_gcs_ns + 10_000_000  # 10 ms OWD
        aoi = build_aoi_timeseries(sps, 500_000_000, 0, sample_hz=100.0, nominal_period_ms=period_ms)
        summary = build_freshness_summary(sps, aoi, [0.05], period_ms)
        self.assertAlmostEqual(summary["miss_ratio_network"]["50"], 0.0, places=3)
        self.assertGreater(summary["miss_ratio_aoi_network"]["50"], 0.0)

    def test_stream_order_correlation(self):
        sps = [_sp(i, i * 100_000_000, i * 100_000_000 + 1_000_000, float(i)) for i in range(1, 11)]
        fwd = [
            {
                "mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED",
                "t_arrival_ns": i * 100_000_000 + 8_000_000,
                "t_forward_ns": i * 100_000_000 + 8_010_000,
            }
            for i in range(1, 11)
        ]
        matched, total = correlate_proxy_by_stream_order(sps, fwd)
        self.assertEqual(matched, 10)
        self.assertEqual(total, 10)
        self.assertEqual(sps[0].correlation_confidence, "stream_order")

    def test_correlate_proxy_by_setpoint_id(self):
        sps = [_sp(i, i * 100_000_000, i * 100_000_000 + 1_000_000, float(i)) for i in range(1, 6)]
        fwd = [
            {
                "mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED",
                "t_arrival_ns": i * 100_000_000 + 13_000_000,
                "t_forward_ns": i * 100_000_000 + 13_010_000,
                "time_boot_ms": i,
            }
            for i in range(1, 6)
        ]
        matched, total = correlate_proxy_by_setpoint_id(sps, fwd)
        self.assertEqual(matched, 5)
        self.assertEqual(total, 5)
        self.assertAlmostEqual(sps[0].network_arrival_delay_ms(), 12.0, places=1)
        self.assertEqual(sps[0].correlation_confidence, "setpoint_id")

    def test_arrival_aoi_rises_with_delay_generation_flat(self):
        """Synthetic: baseline ~13 ms vs attack ~45 ms; generation AoI ~95 ms both."""
        period_ms = 100.0
        base_delay = 13_000_000
        attack_delay = 45_000_000

        def _run(delay_ns: int) -> tuple[float, float]:
            sps = [_sp(i, i * 100_000_000, i * 100_000_000 + 1_000_000, float(i)) for i in range(1, 11)]
            fwd = [
                {
                    "mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED",
                    "t_arrival_ns": sp.t_send_gcs_ns + delay_ns,
                    "t_forward_ns": sp.t_send_gcs_ns + delay_ns + 10_000,
                    "time_boot_ms": sp.logical_setpoint_id,
                }
                for sp in sps
            ]
            t0 = sps[0].t_generation_gcs_ns
            _, _, meta = merge_proxy_artifacts(
                sps, fwd, [], period_ms, t0, t_active_end_ns=t0 + 900_000_000, post_stream_s=30.0,
            )
            fresh = meta["freshness_summary"]
            gen_aoi = build_aoi_generation_timeseries(sps, t0 + 900_000_000, t0, nominal_period_ms=period_ms)
            gen_p99 = build_freshness_summary(sps, gen_aoi, [0.05], period_ms)["aoi_network_ms"]["p99"]
            arr_p99 = fresh["aoi_network_ms"]["p99"]
            return gen_p99, arr_p99

        gen_b, arr_b = _run(base_delay)
        gen_a, arr_a = _run(attack_delay)
        self.assertAlmostEqual(gen_b, gen_a, delta=5.0)
        self.assertGreater(arr_a, arr_b + 20.0)
        self.assertAlmostEqual(gen_b, 95.0, delta=10.0)

    def test_merge_telemetry_downlink(self):
        import csv
        import math
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            gcs_path = run_dir / "telemetry_downlink_gcs.csv"
            uav_path = run_dir / "c2_telemetry_uav.csv"
            fields = [
                "site", "t_mono_ns", "t_rel_s", "mavlink_sequence", "mavlink_system_id",
                "time_boot_ms", "x", "y", "z",
            ]
            with gcs_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerow({
                    "site": "gcs", "t_mono_ns": 2_000_000_000, "t_rel_s": 1.0,
                    "mavlink_sequence": 10, "mavlink_system_id": 1, "time_boot_ms": 1000,
                    "x": 1.0, "y": 2.0, "z": -10.0,
                })
                w.writerow({
                    "site": "gcs", "t_mono_ns": 2_100_000_000, "t_rel_s": 1.1,
                    "mavlink_sequence": 11, "mavlink_system_id": 1, "time_boot_ms": 1050,
                    "x": 1.1, "y": 2.1, "z": -10.0,
                })
            with uav_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=[c for c in fields if c != "t_rel_s"])
                w.writeheader()
                w.writerow({
                    "site": "uav", "t_mono_ns": 1_950_000_000,
                    "mavlink_sequence": 10, "mavlink_system_id": 1, "time_boot_ms": 1000,
                    "x": 1.0, "y": 2.0, "z": -10.0,
                })
                w.writerow({
                    "site": "uav", "t_mono_ns": 2_040_000_000,
                    "mavlink_sequence": 11, "mavlink_system_id": 1, "time_boot_ms": 1050,
                    "x": 1.1, "y": 2.1, "z": -10.0,
                })
            meta = merge_telemetry_downlink(run_dir, nominal_hz=20.0, active_duration_s=1.0)
            self.assertTrue(meta["ok"])
            summary = meta["telemetry_downlink_summary"]
            self.assertEqual(summary["ned_samples_gcs"], 2)
            self.assertAlmostEqual(summary["telemetry_arrival_delay_ms"]["p50"], 50.0, places=1)
            self.assertAlmostEqual(summary["inter_arrival_gap_ms"]["p50"], 100.0, places=1)
            # Arrival of sample 2: belief=sample1 (1,2,-10), truth=latest UAV ≤ t2 = (1.1,2.1,-10)
            self.assertEqual(summary["belief_matched"], 1)
            self.assertAlmostEqual(summary["belief_vs_truth_m"]["p50"], math.hypot(0.1, 0.1), places=3)

    def test_belief_vs_truth_stale_gcs_vs_sitl_egress(self):
        """GCS holds old NED while SITL has already moved (report-only metric)."""
        import csv
        import math
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            gcs_path = run_dir / "telemetry_downlink_gcs.csv"
            uav_path = run_dir / "c2_telemetry_uav.csv"
            fields = [
                "site", "t_mono_ns", "t_rel_s", "mavlink_sequence", "mavlink_system_id",
                "time_boot_ms", "x", "y", "z",
            ]
            with gcs_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                # First belief at origin.
                w.writerow({
                    "site": "gcs", "t_mono_ns": 2_000_000_000, "t_rel_s": 1.0,
                    "mavlink_sequence": 10, "mavlink_system_id": 1, "time_boot_ms": 1000,
                    "x": 0.0, "y": 0.0, "z": 0.0,
                })
                # Next GCS arrival delayed; payload still intermediate.
                w.writerow({
                    "site": "gcs", "t_mono_ns": 2_200_000_000, "t_rel_s": 1.2,
                    "mavlink_sequence": 11, "mavlink_system_id": 1, "time_boot_ms": 1100,
                    "x": 3.0, "y": 0.0, "z": 0.0,
                })
            with uav_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=[c for c in fields if c != "t_rel_s"])
                w.writeheader()
                w.writerow({
                    "site": "uav", "t_mono_ns": 1_950_000_000,
                    "mavlink_sequence": 10, "mavlink_system_id": 1, "time_boot_ms": 1000,
                    "x": 0.0, "y": 0.0, "z": 0.0,
                })
                w.writerow({
                    "site": "uav", "t_mono_ns": 2_050_000_000,
                    "mavlink_sequence": 11, "mavlink_system_id": 1, "time_boot_ms": 1100,
                    "x": 3.0, "y": 0.0, "z": 0.0,
                })
                # SITL kept moving before GCS got the next sample.
                w.writerow({
                    "site": "uav", "t_mono_ns": 2_150_000_000,
                    "mavlink_sequence": 12, "mavlink_system_id": 1, "time_boot_ms": 1200,
                    "x": 8.0, "y": 0.0, "z": 0.0,
                })
            meta = merge_telemetry_downlink(run_dir, nominal_hz=20.0, active_duration_s=1.0)
            self.assertTrue(meta["ok"])
            summary = meta["telemetry_downlink_summary"]
            self.assertEqual(summary["belief_matched"], 1)
            # belief=(0,0,0), truth at t=2.2s = (8,0,0)
            self.assertAlmostEqual(summary["belief_vs_truth_m"]["p50"], 8.0, places=3)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
##################################################################################################################################
# Module: test_paper_artifacts.py (unit tests — paper_artifacts)                                                                 #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Run metadata block construction tests.                                                                            #
##################################################################################################################################

"""Unit tests for paper_artifacts (additive meta/CSV; no gates)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = SCRIPT_DIR / "lib"
for _p in (LIB_DIR, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import paper_artifacts as pa  # noqa: E402


class TestPaperArtifacts(unittest.TestCase):
    def test_analysis_params_defaults_and_override(self):
        d = pa.build_analysis_params()
        self.assertEqual(d["T_safe_ms"], 500.0)
        self.assertEqual(d["E_safe_m"], 5.0)
        self.assertEqual(d["landing_radius_m"], 5.0)
        args = SimpleNamespace(
            analysis_t_safe_ms=1000.0,
            analysis_e_safe_m=8.0,
            analysis_geofence_radius_m=60.0,
            analysis_landing_radius_m=4.0,
        )
        o = pa.build_analysis_params(args)
        self.assertEqual(o["T_safe_ms"], 1000.0)
        self.assertEqual(o["E_safe_m"], 8.0)
        self.assertEqual(o["geofence_radius_m"], 60.0)
        self.assertEqual(o["landing_radius_m"], 4.0)

    def test_offered_load_multi_rogue(self):
        args = SimpleNamespace(
            attack="multi-rogue",
            multi_rogue_count=4,
            mean_rate_mbps=20.0,
            period_ms=40,
            burst_size_bytes=100000,
        )
        self.assertIsNone(pa.compute_offered_attack_load(args, is_baseline=True))
        load = pa.compute_offered_attack_load(args, is_baseline=False)
        self.assertEqual(load["rogue_count"], 4)
        self.assertEqual(load["offered_aggregate_mbps"], 80.0)
        self.assertEqual(load["offered_bursts_per_s_aggregate"], 100.0)

    def test_offered_load_multi_rogue_sync_records_peak(self):
        args = SimpleNamespace(
            attack="multi-rogue-sync",
            multi_rogue_count=4,
            mean_rate_mbps=20.0,
            period_ms=40,
            burst_size_bytes=100000,
            multi_sync_burst_ms=25.0,
            multi_sync_pps=20000,
            multi_sync_size=1400,
        )
        load = pa.compute_offered_attack_load(args, is_baseline=False)
        self.assertEqual(load["direction"], "ul")
        self.assertTrue(load["sync"])
        self.assertEqual(load["peak_mbps_per_rogue"], 224.0)
        self.assertEqual(load["peak_aggregate_mbps"], 896.0)

    def test_persist_writes_files_and_block(self):
        args = SimpleNamespace(
            attack="multi-rogue",
            multi_rogue_count=4,
            mean_rate_mbps=20.0,
            period_ms=40,
            burst_size_bytes=100000,
            companion_video_mbps=14.0,
            c2_arrival_capture=True,
            telemetry_arrival_capture=True,
            attack_start_after_s=120.0,
            analysis_t_safe_ms=None,
            analysis_e_safe_m=None,
            analysis_geofence_radius_m=None,
            analysis_landing_radius_m=None,
        )
        block = pa.build_paper_artifacts_block(
            args,
            is_baseline=False,
            envelope_s=635.0,
            resolved_delay_s=120.0,
            burst_duration_s=515.0,
            mission_start_s=90.0,
            attack_start_s_abs=120.5,
            attack_end_s_abs=635.0,
            wall_runtime_s=673.0,
        )
        self.assertTrue(block["capture"]["delivery_aoi_ready"])
        self.assertEqual(block["multi_rogue_count"], 4)
        self.assertEqual(block["offered_attack_load"]["offered_aggregate_mbps"], 80.0)
        self.assertEqual(block["attack_onset"]["resolved_delay_s"], 120.0)
        self.assertEqual(
            block["artifact_paths"]["host_telemetry_csv"],
            "host_timeseries.csv",
        )
        events = {r["event"] for r in block["event_markers"]}
        self.assertEqual(
            events,
            {"run_start", "mission_start", "attack_onset", "attack_end", "run_end"},
        )
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            written = pa.persist_run_paper_artifacts(run_dir, block)
            self.assertTrue((run_dir / "analysis_params.json").is_file())
            self.assertTrue((run_dir / "event_markers.csv").is_file())
            params = json.loads((run_dir / "analysis_params.json").read_text(encoding="utf-8"))
            self.assertEqual(params["T_safe_ms"], 500.0)
            text = (run_dir / "event_markers.csv").read_text(encoding="utf-8")
            self.assertIn("attack_onset", text)
            self.assertEqual(written["event_markers_csv"], "event_markers.csv")

    def test_warn_missing_capture_flags(self):
        args = SimpleNamespace(
            workload="setpoint-stream",
            duration=600,
            c2_arrival_capture=False,
            telemetry_arrival_capture=True,
        )
        tips = pa.warn_paper_capture_flags(args)
        self.assertTrue(any("c2-arrival-capture" in t for t in tips))


if __name__ == "__main__":
    unittest.main()

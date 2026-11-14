#!/usr/bin/env python3
##################################################################################################################################
# Module: test_k8_gcs_source.py (K8 reads mavlink_rx_gcs.csv on the T0 base (t_mono_ns), not the mission clock)                  #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
##################################################################################################################################
"""K8 reads mavlink_rx_gcs.csv on the T0 base (t_mono_ns), not the mission clock."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "instrumentation"))

from link_vivacity import analyze_run, compute_k8_return_leg  # noqa: E402
from phase_windows import build_windows, write_clock_anchor  # noqa: E402

T0 = 7_000_000_000_000
MISSION_OFFSET_S = 90.0


def _make_run(run: Path, *, gap: tuple[float, float] | None, with_rx: bool = True,
              orbit: float | None = None) -> None:
    write_clock_anchor(run, t0_mono_ns=T0, t0_unix_ns=1_700_000_000_000_000_000)
    markers = {"run_start": 0.0, "mission_start": 90.0, "attack_onset": 100.0,
               "attack_end": 200.0, "run_end": 300.0}
    if orbit is not None:
        markers["orbit_acquired"] = orbit
    with (run / "event_markers.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event", "t_rel_s", "note"])
        for ev, t in markers.items():
            w.writerow([ev, t, ""])
    (run / "flyblind_phase_windows.json").write_text(json.dumps(build_windows(markers)))
    (run / "c2.json").write_text(json.dumps({"c2_metrics": {"failsafe": {"triggered": False}}}))
    if not with_rx:
        return
    with (run / "mavlink_rx_gcs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["t_mono_ns", "t_rel_s", "mavlink_msg_type", "leg"])
        w.writeheader()
        t = 92.0
        while t < 299.0:
            if not (gap and gap[0] < t < gap[1]):
                w.writerow({
                    "t_mono_ns": T0 + int(t * 1e9),
                    "t_rel_s": round(t - MISSION_OFFSET_S, 4),
                    "mavlink_msg_type": "LOCAL_POSITION_NED",
                    "leg": "return",
                })
            t += 0.25


class TestK8GcsSource(unittest.TestCase):
    def test_gap_lands_in_attack_on_t0_base(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            _make_run(run, gap=(150.0, 156.0))
            viv = analyze_run(run)
            k8 = viv["k8_return"]
            self.assertEqual(k8["ATTACK"]["loss_declarations"], 1)
            self.assertFalse(k8["ATTACK"]["pass"])
            self.assertTrue(k8["PRE"]["pass"])
            self.assertTrue(k8["DRAIN"]["pass"])
            self.assertEqual(viv["inputs"]["gcs_time_base"], "t_mono_ns - T0_mono_ns")
            self.assertEqual(viv["inputs"]["mission_summary_source"], "c2.json")

    def test_healthy_link_passes_every_phase(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            _make_run(run, gap=None)
            k8 = analyze_run(run)["k8_return"]
            for ph in ("PRE", "ATTACK", "DRAIN"):
                self.assertTrue(k8[ph]["pass"], ph)
                self.assertEqual(k8[ph]["loss_declarations"], 0, ph)

    def test_bootstrap_gap_goes_to_preflight_only(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            _make_run(run, gap=(93.0, 98.0), orbit=99.0)
            viv = analyze_run(run)
            preflight = viv["k8_preflight"]
            self.assertTrue(preflight["excluded_from_analysis"])
            self.assertEqual(preflight["loss_declarations"], 1)
            self.assertEqual(preflight["window_duration_s"], 7.0)
            for ph in ("PRE", "ATTACK", "DRAIN"):
                self.assertTrue(viv["k8_return"][ph]["pass"], ph)

    def test_missing_source_is_not_link_loss(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            _make_run(run, gap=None, with_rx=False)
            k8 = analyze_run(run)["k8_return"]
            for ph in ("ALL", "PRE", "ATTACK", "DRAIN"):
                self.assertIsNone(k8[ph]["pass"], ph)
                self.assertEqual(k8[ph]["error"], "no_source", ph)

    def test_empty_rows_direct_call(self):
        out = compute_k8_return_leg(mavlink_rx_rows=[], windows={}, phase="ATTACK")
        self.assertIsNone(out["pass"])
        self.assertEqual(out["leg"], "return")


if __name__ == "__main__":
    unittest.main()

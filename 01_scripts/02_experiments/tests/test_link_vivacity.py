#!/usr/bin/env python3
##################################################################################################################################
# Module: test_link_vivacity.py (bench tests for K7/K8 link vivacity and phase slicing)                                          #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
##################################################################################################################################
"""Bench tests for K7/K8 link vivacity and phase slicing."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "instrumentation"))
sys.path.insert(0, str(HERE / "lib"))

from link_vivacity import (  # noqa: E402
    analyze_run,
    compute_k7_command_leg,
    fill_c4_checks,
    qgc_style_link_loss,
)
from kpi_leg import evaluate_c4_kpi_slice, LEG_COMMAND  # noqa: E402
from mechanism_by_phase import analyze_run as mech_analyze  # noqa: E402
from phase_windows import build_windows, write_clock_anchor  # noqa: E402


class TestK8QgcStyle(unittest.TestCase):
    def test_synthetic_gap_declares_loss(self):
        arrivals = [0.0, 1.0, 2.0, 8.0, 9.0]
        r = qgc_style_link_loss(arrivals, t_lo=0.0, t_hi=10.0)
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["max_gap_s"], 5.0)
        self.assertGreaterEqual(r["loss_declarations"], 1)
        self.assertFalse(r["pass"])

    def test_dense_1hz_passes(self):
        arrivals = [float(i) for i in range(0, 20)]
        r = qgc_style_link_loss(arrivals, t_lo=0.0, t_hi=20.0)
        self.assertEqual(r["loss_declarations"], 0)
        self.assertTrue(r["pass"])


class TestK7UavPrimary(unittest.TestCase):
    def test_uav_rtl_triggers_primary_not_gcs(self):
        # GCS says no failsafe; UAV proxy sees RTL (custom_mode=6).
        summary = {"c2_metrics": {"failsafe": {"triggered": False, "mode": None, "time_s": None}}}
        rows = [
            {
                "direction": "sitl_to_gcs",
                "mavlink_msg_type": "HEARTBEAT",
                "custom_mode": "4",  # GUIDED-ish placeholder before
                "t_rel_s": "5.0",
                "t_unix_ns": "",
            },
            {
                "direction": "sitl_to_gcs",
                "mavlink_msg_type": "HEARTBEAT",
                "custom_mode": "6",  # RTL
                "t_rel_s": "12.0",
                "t_unix_ns": "",
            },
            {
                "direction": "gcs_to_sitl",
                "mavlink_msg_type": "HEARTBEAT",
                "t_rel_s": "11.0",
                "t_unix_ns": "",
            },
            {
                "direction": "gcs_to_sitl",
                "mavlink_msg_type": "HEARTBEAT",
                "t_rel_s": "12.0",
                "t_unix_ns": "",
            },
        ]
        windows = {"ATTACK": {"t_rel_lo_s": 10.0, "t_rel_hi_s": 20.0}}
        k7 = compute_k7_command_leg(
            proxy_link_rows=rows,
            mission_summary=summary,
            status_rows=[],
            windows=windows,
            phase="ATTACK",
        )
        self.assertTrue(k7["FS_GCS"]["triggered"])
        self.assertEqual(k7["FS_GCS"]["primary_source"], "uav_proxy_sitl_to_gcs")
        self.assertFalse(k7["FS_GCS"]["secondary_gcs"]["triggered"])
        self.assertTrue(k7["FS_GCS"]["gcs_discrepancy"])
        self.assertFalse(k7["pass"])

    def test_statustext_failsafe_on_uav(self):
        rows = [{
            "direction": "sitl_to_gcs",
            "mavlink_msg_type": "STATUSTEXT",
            "text": "GCS Failsafe",
            "t_rel_s": "15.0",
        }]
        k7 = compute_k7_command_leg(
            proxy_link_rows=rows,
            mission_summary={"c2_metrics": {"failsafe": {"triggered": False}}},
            status_rows=[],
            windows={"ATTACK": {"t_rel_lo_s": 10.0, "t_rel_hi_s": 20.0}},
            phase="ATTACK",
        )
        self.assertTrue(k7["FS_GCS"]["triggered"])
        self.assertEqual(k7["FS_GCS"]["primary"]["reason"], "statustext")


class TestPhaseSlice17(unittest.TestCase):
    def test_k7_and_mechanism_slice_by_markers(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            t0_mono = 5_000_000_000_000
            t0_unix = 1_700_000_000_000_000_000
            write_clock_anchor(run, t0_mono_ns=t0_mono, t0_unix_ns=t0_unix)
            # Markers: PRE [0,10), ATTACK [10,20), DRAIN [20,30)
            with (run / "event_markers.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["event", "t_rel_s", "note"])
                w.writeheader()
                for ev, t in (
                    ("run_start", 0.0),
                    ("mission_start", 0.0),
                    ("attack_onset", 10.0),
                    ("attack_end", 20.0),
                    ("run_end", 30.0),
                ):
                    w.writerow({"event": ev, "t_rel_s": t, "note": ""})
            pw = build_windows({
                "run_start": 0.0, "mission_start": 0.0,
                "attack_onset": 10.0, "attack_end": 20.0, "run_end": 30.0,
            })
            (run / "flyblind_phase_windows.json").write_text(
                json.dumps(pw, indent=2) + "\n", encoding="utf-8"
            )
            # proxy_link: t_arrival_ns = T0_mono + t*1e9 (shared monotonic)
            # leave t_rel_s empty so postprocess derives from mono
            with (run / "proxy_link.csv").open("w", newline="", encoding="utf-8") as f:
                fields = [
                    "direction", "leg", "t_arrival_ns", "t_forward_ns",
                    "t_unix_ns", "t_rel_s", "mavlink_msg_type", "custom_mode", "text",
                ]
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for t in range(0, 30):
                    mono = t0_mono + int(t * 1e9)
                    unix = t0_unix + int(t * 1e9)
                    w.writerow({
                        "direction": "gcs_to_sitl",
                        "leg": "command",
                        "t_arrival_ns": mono,
                        "t_forward_ns": mono,
                        "t_unix_ns": unix,
                        "t_rel_s": "",
                        "mavlink_msg_type": "HEARTBEAT",
                        "custom_mode": "",
                        "text": "",
                    })
                    cm = "6" if t == 15 else "4"
                    w.writerow({
                        "direction": "sitl_to_gcs",
                        "leg": "return",
                        "t_arrival_ns": mono,
                        "t_forward_ns": mono,
                        "t_unix_ns": unix,
                        "t_rel_s": "",
                        "mavlink_msg_type": "HEARTBEAT",
                        "custom_mode": cm,
                        "text": "",
                    })
            # ue_iface still stamps unix (fallback bridge)
            with (run / "flyblind_ue_iface.jsonl").open("w", encoding="utf-8") as f:
                for t, dropped in ((5, 0), (12, 10), (15, 25), (25, 25)):
                    f.write(json.dumps({
                        "t_unix_ns": t0_unix + int(t * 1e9),
                        "qdisc_dropped": dropped,
                    }) + "\n")
            (run / "flyblind_ue_iface_summary.json").write_text(
                json.dumps({"qdisc_dropped_delta": 25}), encoding="utf-8"
            )
            (run / "mission_summary.json").write_text(
                json.dumps({"c2_metrics": {"failsafe": {"triggered": False}}}),
                encoding="utf-8",
            )

            viv = analyze_run(run)
            self.assertTrue(viv["k7_command"]["ATTACK"]["phase_slice_ok"])
            self.assertEqual(viv["k7_command"]["ATTACK"]["link_arrivals"]["n"], 10)
            self.assertEqual(viv["k7_command"]["PRE"]["link_arrivals"]["n"], 10)
            self.assertEqual(viv["k7_command"]["DRAIN"]["link_arrivals"]["n"], 10)
            self.assertTrue(viv["k7_command"]["ATTACK"]["FS_GCS"]["triggered"])
            self.assertFalse(viv["k7_command"]["PRE"]["FS_GCS"]["triggered"])
            self.assertIn("T0_mono", viv["k7_command"]["ATTACK"]["link_arrivals"]["timeline"])

            mech = mech_analyze(run)
            att = mech["qdisc_drops"]["by_phase"].get("ATTACK") or {}
            self.assertEqual(att.get("qdisc_dropped_delta"), 15)  # 25-10
            pre = mech["qdisc_drops"]["by_phase"].get("PRE") or {}
            self.assertIsNotNone(pre.get("qdisc_dropped_first"))

    def test_forwarder_cmd_passes_t0_mono(self):
        # orchestrate_mitigation.py was merged into orchestrate.py: there is a single orchestrator now.
        src = (HERE / "orchestrate.py").read_text(encoding="utf-8")
        self.assertIn("t0_mono_ns: int | None = None", src)
        self.assertIn('"--t0-mono-ns"', src)
        self.assertIn("t0_mono_ns=T0", src)

    def test_proxy_t_rel_from_mono(self):
        """Bench: t_rel_s = (t_arrival_ns - T0_mono)/1e9."""
        t0 = 1_000_000_000_000
        rows = [{
            "direction": "gcs_to_sitl",
            "mavlink_msg_type": "HEARTBEAT",
            "t_arrival_ns": str(t0 + 12_500_000_000),
            "t_rel_s": "",
        }]
        k7 = compute_k7_command_leg(
            proxy_link_rows=rows,
            mission_summary={"c2_metrics": {"failsafe": {"triggered": False}}},
            status_rows=[],
            windows={"ATTACK": {"t_rel_lo_s": 10.0, "t_rel_hi_s": 20.0}},
            phase="ATTACK",
            t0_mono_ns=float(t0),
        )
        self.assertEqual(k7["link_arrivals"]["n"], 1)
        self.assertTrue(k7["phase_slice_ok"])


class TestC4Fill(unittest.TestCase):
    def test_evaluate_c4_accepts_k7_k8(self):
        k7 = {
            "pass": True,
            "FS_GCS": {"triggered": False, "primary_source": "uav_proxy_sitl_to_gcs"},
            "link_arrivals": {"max_gap_s": 1.1},
        }
        k8 = {
            "pass": True,
            "max_gap_s": 1.0,
            "loss_declarations": 0,
            "pct_time_gap_gt_loss": 0.0,
        }
        c4 = evaluate_c4_kpi_slice(
            leg=LEG_COMMAND,
            fractions=[{"id": "le_1s_honeywell", "fraccao_le_threshold": 0.99, "pass": True}],
            reliabilities=[
                {"id": "le_40ms_stick", "reliability": 0.999, "pass": True},
                {"id": "le_1s_honeywell", "reliability": 0.999, "pass": True},
                {"id": "le_140ms_video", "reliability": 0.9999, "pass": True},
            ],
            interruptions={"by_threshold": {"gt_3s": {"gap_count": 0}, "gt_5s": {"gap_count": 0}}},
            k7_fs_gcs=k7,
            k8_qgc_style=k8,
        )
        self.assertTrue(c4["checks"]["FS_GCS"]["pass"])
        filled = fill_c4_checks({}, k7, k8)
        self.assertTrue(filled["checks"]["FS_GCS"]["pass"])


class TestCliBench(unittest.TestCase):
    def test_bench_synthetic_exit0(self):
        import subprocess
        script = HERE / "instrumentation" / "link_vivacity.py"
        r = subprocess.run(
            [sys.executable, str(script), "--bench-synthetic"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()

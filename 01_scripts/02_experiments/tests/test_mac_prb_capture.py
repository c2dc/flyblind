#!/usr/bin/env python3
##################################################################################################################################
# Module: test_mac_prb_capture.py (unit tests — mac_prb parsing)                                                                 #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: MAC log and PRB proxy parser tests.                                                                               #
##################################################################################################################################

"""Unit tests for mac_prb capture wall-clock timeout (no cluster)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROBES = Path(__file__).resolve().parents[1] / "probes"
sys.path.insert(0, str(PROBES))

import mac_prb  # noqa: E402


def _cleanup(path: str) -> None:
    for suffix in ("", ".meta.json", ".kubectl.err"):
        Path(path + suffix).unlink(missing_ok=True)


# Fake ``kubectl logs -f``: first invocation prints one line then goes silent
# forever (the stall we observed in 600 s runs); later invocations keep
# printing.  A marker file counts invocations across restarts.
STALLING_LOGGER = r"""
import os, sys, time
marker = sys.argv[1]
n = int(open(marker).read().strip() or "0") if os.path.exists(marker) else 0
n += 1
open(marker, "w").write(str(n))
if n == 1:
    print("gnb-line-before-stall", flush=True)
    time.sleep(600)
else:
    while True:
        print("gnb-line-after-restart", flush=True)
        time.sleep(0.2)
"""

EXITING_LOGGER = r"""
import sys, time
print("gnb-line", flush=True)
time.sleep(0.3)
sys.exit(0)
"""


class TestMacPrbCaptureWatchdog(unittest.TestCase):
    """Real subprocesses (not mocks) so pipe/EOF semantics are exercised."""

    def test_capture_restarts_when_stream_goes_silent(self):
        with tempfile.TemporaryDirectory() as td:
            marker = str(Path(td) / "runs.txt")
            raw = str(Path(td) / "raw.log")
            cmd = [sys.executable, "-c", STALLING_LOGGER, marker]
            with patch("mac_prb._kubectl_logs_cmd", return_value=cmd):
                rc = mac_prb.capture("ctx", "ns", 4.0, raw, idle_restart_s=1.5)
            self.assertEqual(rc, 0)
            text = Path(raw).read_text(encoding="utf-8")
            meta = json.loads(Path(raw + ".meta.json").read_text(encoding="utf-8"))
            self.assertIn("gnb-line-before-stall", text)
            self.assertIn("gnb-line-after-restart", text)
            self.assertGreaterEqual(meta["idle_restarts"], 1)
            self.assertTrue(
                any(e["reason"] == "stream_idle" for e in meta["restart_events"])
            )
            # The span must cover most of the request despite the stall.
            self.assertGreaterEqual(meta["effective_capture_span_s"], 1.5)
            self.assertEqual(int(Path(marker).read_text()), meta["kubectl_restarts"] + 1)

    def test_capture_restarts_on_process_exit(self):
        with tempfile.TemporaryDirectory() as td:
            raw = str(Path(td) / "raw.log")
            cmd = [sys.executable, "-c", EXITING_LOGGER]
            with patch("mac_prb._kubectl_logs_cmd", return_value=cmd):
                rc = mac_prb.capture("ctx", "ns", 2.0, raw, idle_restart_s=30.0)
            self.assertEqual(rc, 0)
            meta = json.loads(Path(raw + ".meta.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(meta["kubectl_restarts"], 1)
            self.assertGreater(meta["raw_lines"], 1)

    def test_capture_records_kubectl_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            raw = str(Path(td) / "raw.log")
            script = "import sys, time; sys.stderr.write('boom\\n'); time.sleep(5)"
            cmd = [sys.executable, "-c", script]
            with patch("mac_prb._kubectl_logs_cmd", return_value=cmd):
                mac_prb.capture("ctx", "ns", 1.0, raw, idle_restart_s=30.0)
            self.assertIn("boom", Path(raw + ".kubectl.err").read_text(encoding="utf-8"))


class TestMacPrbCapture(unittest.TestCase):
    def test_capture_exits_on_wall_clock_without_lines(self):
        """select poll must enforce duration even when kubectl logs is silent."""
        proc = MagicMock()
        proc.stdout.fileno.return_value = 1
        proc.poll.return_value = None
        t = [1000.0]

        def mono():
            return t[0]

        def select_side(_r, _w, _x, timeout):
            t[0] += timeout if timeout is not None else 1.0
            return ([], [], [])

        with patch("mac_prb.subprocess.Popen", return_value=proc):
            with patch("mac_prb.time.monotonic", side_effect=mono):
                with patch("mac_prb.select.select", side_effect=select_side):
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as tmp:
                        path = tmp.name
                    try:
                        rc = mac_prb.capture("ctx", "ns", 2.0, path)
                    finally:
                        _cleanup(path)

        self.assertEqual(rc, 0)
        self.assertGreaterEqual(t[0], 1002.0)
        proc.terminate.assert_called_once()

    def test_capture_writes_lines_when_ready(self):
        proc = MagicMock()
        proc.stdout.fileno.return_value = 1
        proc.poll.return_value = None
        t = [0.0]

        def mono():
            return t[0]

        def select_side(_r, _w, _x, timeout):
            t[0] += timeout if timeout is not None else 1.0
            return ([proc.stdout], [], [])

        with patch("mac_prb.subprocess.Popen", return_value=proc):
            with patch("mac_prb.os.read", side_effect=[b"line1\n", b""]):
                with patch("mac_prb.time.monotonic", side_effect=mono):
                    with patch("mac_prb.select.select", side_effect=select_side):
                        with tempfile.NamedTemporaryFile(
                            mode="w", suffix=".log", delete=False,
                        ) as tmp:
                            path = tmp.name
                        try:
                            mac_prb.capture("ctx", "ns", 2.0, path)
                            text = Path(path).read_text(encoding="utf-8")
                            meta = json.loads(
                                Path(path + ".meta.json").read_text(encoding="utf-8"),
                            )
                        finally:
                            _cleanup(path)

        self.assertIn("line1", text)
        self.assertTrue(text.startswith("t="))
        self.assertEqual(meta["raw_lines"], 1)

    def test_capture_does_not_stop_on_eof_before_duration(self):
        proc = MagicMock()
        proc.stdout.fileno.return_value = 1
        proc.poll.return_value = None
        t = [1000.0]

        def mono():
            return t[0]

        def select_side(_r, _w, _x, timeout):
            t[0] += timeout if timeout is not None else 1.0
            return ([], [], [])

        with patch("mac_prb.subprocess.Popen", return_value=proc):
            with patch("mac_prb.time.monotonic", side_effect=mono):
                with patch("mac_prb.select.select", side_effect=select_side):
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as tmp:
                        path = tmp.name
                    try:
                        mac_prb.capture("ctx", "ns", 2.0, path)
                        meta = json.loads(Path(path + ".meta.json").read_text(encoding="utf-8"))
                    finally:
                        _cleanup(path)

        self.assertGreaterEqual(t[0], 1002.0)
        self.assertGreaterEqual(meta["wall_capture_span_s"], 2.0)
        proc.terminate.assert_called_once()


class TestMacPrbParseDlUlAxis(unittest.TestCase):
    def test_dl_delta_uses_mac_tx_not_rx(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw.log"
            rnti_map = Path(td) / "rnti.map"
            csv_out = Path(td) / "out.csv"
            json_out = Path(td) / "out.json"
            rnti_map.write_text("aa01 rogue\nbb02 gcs\n", encoding="utf-8")
            # OAI: TX=DL, RX=UL. Rogue DL (TX) grows; GCS UL (RX) grows.
            raw.write_text(
                "t=1.0 UE RNTI aa01 CU-UE-ID 1\n"
                "t=1.0 UE aa01: MAC:    TX        10000 RX         1000 bytes\n"
                "t=1.0 UE RNTI bb02 CU-UE-ID 2\n"
                "t=1.0 UE bb02: MAC:    TX         5000 RX      1000000 bytes\n"
                "t=2.0 UE aa01: MAC:    TX      10100000 RX         1100 bytes\n"
                "t=2.0 UE bb02: MAC:    TX         6000 RX      2000000 bytes\n",
                encoding="utf-8",
            )
            roleslice = '{"rogue":"A","gcs":"B","uav":"B"}'
            rc = mac_prb.parse(str(raw), str(rnti_map), roleslice, str(csv_out), str(json_out))
            self.assertEqual(rc, 0)
            out = json.loads(json_out.read_text(encoding="utf-8"))
            # DL delta: rogue TX 10.1M-10k ~ 9.99M on slice A; gcs TX 1k on slice B
            self.assertGreater(out["per_slice"]["A"]["dl_delta_bytes"], 9_000_000)
            self.assertLess(out["per_slice"]["B"]["dl_delta_bytes"], 10_000)
            self.assertGreater(out["per_slice"]["B"]["ul_delta_bytes"], 900_000)
            self.assertIn("ul_delta_share_pct", out)
            self.assertAlmostEqual(out["dl_delta_share_pct"]["A"], 99.99, delta=0.1)

    def test_normalize_rnti_does_not_strip_internal_zeros(self):
        self.assertEqual(mac_prb.normalize_rnti("010b"), "010b")
        self.assertEqual(mac_prb.normalize_rnti("0x10b"), "010b")
        self.assertEqual(mac_prb.normalize_rnti("10b"), "010b")
        self.assertEqual(mac_prb.normalize_rnti("52f8"), "52f8")

    def test_overlay_repairs_stale_rntimap_from_capture_cu(self):
        """Stale map (old GCS/UAV RNTIs) must not leave live RNTIs as ?unmapped."""
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw.log"
            rnti_map = Path(td) / "rnti.map"
            csv_out = Path(td) / "out.csv"
            json_out = Path(td) / "out.json"
            # Stale map like the broken run: old gcs/uav RNTIs not in dumps.
            rnti_map.write_text(
                "010b gcs\n010f uav\n3950 rogue\n62f4 rogue2\n273e rogue3\n98bb rogue4\n",
                encoding="utf-8",
            )
            raw.write_text(
                "t=1.0 UE RNTI 3950 CU-UE-ID 1 in-sync\n"
                "t=1.0 UE 3950: ulsch_rounds 1/0/0/0, NPRB 5 MCS (0) 9\n"
                "t=1.0 UE RNTI 62f4 CU-UE-ID 2 in-sync\n"
                "t=1.0 UE 62f4: ulsch_rounds 1/0/0/0, NPRB 5 MCS (0) 9\n"
                "t=1.0 UE RNTI 273e CU-UE-ID 3 in-sync\n"
                "t=1.0 UE 273e: ulsch_rounds 1/0/0/0, NPRB 5 MCS (0) 9\n"
                "t=1.0 UE RNTI 98bb CU-UE-ID 4 in-sync\n"
                "t=1.0 UE 98bb: ulsch_rounds 1/0/0/0, NPRB 5 MCS (0) 9\n"
                "t=1.0 UE RNTI 52f8 CU-UE-ID 5 in-sync\n"
                "t=1.0 UE 52f8: ulsch_rounds 1/0/0/0, NPRB 8 MCS (0) 20\n"
                "t=1.0 UE 52f8: MAC:    TX        10000 RX      1000000 bytes\n"
                "t=1.0 UE RNTI 13bb CU-UE-ID 6 in-sync\n"
                "t=1.0 UE 13bb: ulsch_rounds 1/0/0/0, NPRB 5 MCS (0) 9\n"
                "t=1.0 UE 13bb: MAC:    TX         8000 RX         5000 bytes\n"
                "t=2.0 UE 52f8: MAC:    TX        12000 RX      5000000 bytes\n"
                "t=2.0 UE 13bb: MAC:    TX         9000 RX         6000 bytes\n",
                encoding="utf-8",
            )
            roleslice = (
                '{"rogue":"A","rogue2":"A","rogue3":"A","rogue4":"A","gcs":"B","uav":"B"}'
            )
            rc = mac_prb.parse(str(raw), str(rnti_map), roleslice, str(csv_out), str(json_out))
            self.assertEqual(rc, 0)
            out = json.loads(json_out.read_text(encoding="utf-8"))
            roles = set(out["per_role"])
            self.assertIn("gcs", roles)
            self.assertIn("uav", roles)
            self.assertIn("rogue", roles)
            self.assertNotIn("unknown", roles)
            # Slice B must absorb the heavy UL (video-like), not '?'
            self.assertIn("B", out["per_slice"])
            self.assertNotIn("?", out.get("ul_delta_share_pct", {}))
            self.assertGreater(out["per_slice"]["B"]["ul_delta_bytes"], 1_000_000)

    def test_map_roles_from_cu_attach_order(self):
        cu = {"1": "3950", "2": "62f4", "5": "52f8", "6": "13bb"}
        roles = ["rogue", "rogue2", "gcs", "uav"]
        m = mac_prb.map_roles_from_cu(cu, roles)
        self.assertEqual(m["3950"], "rogue")
        self.assertEqual(m["62f4"], "rogue2")
        self.assertEqual(m["52f8"], "gcs")
        self.assertEqual(m["13bb"], "uav")

    def test_native_slice_map_reads_gnb_lines(self):
        text = (
            "t=1.0 [NR_MAC] I Slice 1, NSSAI 1.ffffff, rrmPolicy.min_ratio 0\n"
            "t=1.0 [NR_MAC] I Slice 2, NSSAI 1.000001, rrmPolicy.min_ratio 0\n"
            "t=1.0 Active slices for UE 20ad = [ 2 ]\n"
            "t=1.0 Active slices for UE 70e1 = [ 1 ]\n"
        )
        rnti_slice, nssai = mac_prb.native_slice_map(text)
        self.assertEqual(rnti_slice, {"20ad": "B", "70e1": "A"})
        self.assertEqual(nssai, {"1": "1.ffffff", "2": "1.000001"})

    def test_attach_order_misidentification_is_repaired_by_native_slice(self):
        """CU-UE-ID order labelled the video UAV as a rogue; slice B lost its traffic."""
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw.log"
            rnti_map = Path(td) / "rnti.map"
            csv_out = Path(td) / "out.csv"
            json_out = Path(td) / "out.json"
            rnti_map.write_text("", encoding="utf-8")
            raw.write_text(
                "t=0.0 [NR_MAC] I Slice 1, NSSAI 1.ffffff, rrmPolicy.min_ratio 0\n"
                "t=0.0 [NR_MAC] I Slice 2, NSSAI 1.000001, rrmPolicy.min_ratio 0\n"
                "t=0.0 UE RNTI 70e1 CU-UE-ID 1 in-sync\n"
                "t=0.0 UE RNTI 20ad CU-UE-ID 2 in-sync\n"
                "t=0.0 UE RNTI 8d94 CU-UE-ID 3 in-sync\n"
                "t=0.0 Active slices for UE 70e1 = [ 1 ]\n"
                "t=0.0 Active slices for UE 20ad = [ 2 ]\n"
                "t=0.0 Active slices for UE 8d94 = [ 2 ]\n"
                "t=0.0 UE 70e1: ulsch_rounds 10/0/0/0, NPRB 5 MCS (0) 9\n"
                "t=0.0 UE 70e1: MAC:    TX         1000 RX         1000 bytes\n"
                "t=0.0 UE 20ad: ulsch_rounds 10/0/0/0, NPRB 40 MCS (0) 20\n"
                "t=0.0 UE 20ad: MAC:    TX         1000 RX         1000 bytes\n"
                "t=0.0 UE 8d94: ulsch_rounds 10/0/0/0, NPRB 5 MCS (0) 9\n"
                "t=0.0 UE 8d94: MAC:    TX         1000 RX         1000 bytes\n"
                "t=10.0 UE 70e1: MAC:    TX         2000 RX         2000 bytes\n"
                # OAI TX=DL, RX=UL: UAV has huge UL (RX); GCS has huge DL (TX).
                "t=10.0 UE 20ad: MAC:    TX        50000 RX    900000000 bytes\n"
                "t=10.0 UE 8d94: MAC:    TX    890000000 RX        60000 bytes\n",
                encoding="utf-8",
            )
            roleslice = (
                '{"rogue":"A","rogue2":"A","rogue3":"A","rogue4":"A","gcs":"B","uav":"B"}'
            )
            rc = mac_prb.parse(str(raw), str(rnti_map), roleslice, str(csv_out), str(json_out))
            self.assertEqual(rc, 0)
            out = json.loads(json_out.read_text(encoding="utf-8"))
            ident = out["slice_identity"]
            self.assertEqual(ident["slice_source"], "gnb_native_active_slices")
            self.assertEqual(ident["role_identity_source"], "companion_video_signature")
            self.assertEqual(out["rnti_role_map"]["20ad"], "uav")
            self.assertEqual(out["rnti_role_map"]["8d94"], "gcs")
            self.assertEqual(out["rnti_role_map"]["70e1"], "rogue")
            # The video UL belongs to slice B, not to the rogue slice.
            self.assertGreater(out["ul_delta_share_pct"]["B"], 99.0)
            self.assertLess(out["ul_delta_share_pct"].get("A", 0.0), 1.0)
            self.assertGreater(out["per_role"]["uav"]["ul_mbps"], 500.0)
            self.assertGreater(out["per_role"]["gcs"]["dl_mbps"], 500.0)

    def test_native_slice_absent_keeps_role_map_behaviour(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw.log"
            rnti_map = Path(td) / "rnti.map"
            csv_out = Path(td) / "out.csv"
            json_out = Path(td) / "out.json"
            rnti_map.write_text("aa01 rogue\nbb02 gcs\n", encoding="utf-8")
            raw.write_text(
                "t=1.0 UE aa01: MAC:    TX         1000 RX        10000 bytes\n"
                "t=2.0 UE aa01: MAC:    TX         1100 RX        20000 bytes\n",
                encoding="utf-8",
            )
            roleslice = '{"rogue":"A","gcs":"B","uav":"B"}'
            mac_prb.parse(str(raw), str(rnti_map), roleslice, str(csv_out), str(json_out))
            out = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(out["slice_identity"]["slice_source"], "role_map_fallback")
            self.assertEqual(out["rnti_role_map"]["aa01"], "rogue")

    def test_cell_ul_activity_absolute_rounds_and_proxy(self):
        """Δulsch_rounds / window → absolute rps; grant proxy vs 400 UL slots/s."""
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw.log"
            rnti_map = Path(td) / "rnti.map"
            csv_out = Path(td) / "out.csv"
            json_out = Path(td) / "out.json"
            rnti_map.write_text("aa01 rogue\nbb02 gcs\n", encoding="utf-8")
            # 10s window: rogue +1000 rounds, gcs +100 rounds.
            raw.write_text(
                "t=0.0 UE RNTI aa01 CU-UE-ID 1\n"
                "t=0.0 UE aa01: ulsch_rounds 1000/0/0/0, NPRB 106 MCS (0) 13\n"
                "t=0.0 UE aa01: MAC:    TX         1000 RX      1000000 bytes\n"
                "t=0.0 UE RNTI bb02 CU-UE-ID 2\n"
                "t=0.0 UE bb02: ulsch_rounds 5000/0/0/0, NPRB 8 MCS (0) 20\n"
                "t=0.0 UE bb02: MAC:    TX         2000 RX       200000 bytes\n"
                "t=10.0 UE aa01: ulsch_rounds 2000/0/0/0, NPRB 106 MCS (0) 13\n"
                "t=10.0 UE aa01: MAC:    TX         1100 RX     11000000 bytes\n"
                "t=10.0 UE bb02: ulsch_rounds 5100/0/0/0, NPRB 9 MCS (0) 20\n"
                "t=10.0 UE bb02: MAC:    TX         2100 RX      1200000 bytes\n",
                encoding="utf-8",
            )
            roleslice = '{"rogue":"A","gcs":"B","uav":"B"}'
            rc = mac_prb.parse(str(raw), str(rnti_map), roleslice, str(csv_out), str(json_out))
            self.assertEqual(rc, 0)
            out = json.loads(json_out.read_text(encoding="utf-8"))
            cell = out["cell_ul_activity"]
            self.assertAlmostEqual(cell["ul_rounds_per_s_total"], 110.0, places=1)
            self.assertAlmostEqual(cell["per_role"]["rogue"]["ul_rounds_per_s"], 100.0, places=1)
            self.assertAlmostEqual(cell["per_role"]["gcs"]["ul_rounds_per_s"], 10.0, places=1)
            self.assertAlmostEqual(cell["grant_activity_proxy_pct"], 27.5, places=1)
            self.assertEqual(cell["per_role"]["gcs"]["last_nprb_p50"], 8.5)
            # Legacy share still present (additive change).
            self.assertIn("share_ul_nprb_pct", out["per_slice"]["A"])


if __name__ == "__main__":
    unittest.main()

##################################################################################################################################
# Module: test_probe25_return_b.py (Second return-leg probe (probe B) and per-instance tmp paths)                                #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Covers: - tmp_paths(): empty tag = historical path; tag != "" = suffixed. - build_recv_script / build_send_script #
#   honor the /tmp path passed in. - run_cluster (dry-run) uses the suffixed path when tmp_tag is given, and the historical one  #
#   when it is not (command and return probe A stay byte-identical). - run_sidecars: FULL without opt-in does NOT start probe B; #
#   with --with-return-probe-b (or env RETURN_PROBE_B=1) it starts leg_owd_return_b with its own port/format.                    #
##################################################################################################################################
"""Second return-leg probe (probe B) and per-instance tmp paths.

Covers:
  - tmp_paths(): empty tag = historical path; tag != "" = suffixed.
  - build_recv_script / build_send_script honor the /tmp path passed in.
  - run_cluster (dry-run) uses the suffixed path when tmp_tag is given, and the
    historical one when it is not (command and return probe A stay byte-identical).
  - run_sidecars: FULL without opt-in does NOT start probe B; with --with-return-probe-b
    (or env RETURN_PROBE_B=1) it starts leg_owd_return_b with its own port/format.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
sys.path.insert(0, str(EXP / "probes"))

import leg_owd_probe as probe  # noqa: E402

RUN_SIDECARS = EXP / "instrumentation" / "run_sidecars.py"


class TestTmpPaths(unittest.TestCase):
    def test_empty_tag_is_historic(self):
        rx, tx = probe.tmp_paths("")
        self.assertEqual(rx, "/tmp/leg_rx.json")
        self.assertEqual(tx, "/tmp/leg_tx.json")

    def test_tagged_is_suffixed(self):
        rx, tx = probe.tmp_paths("b")
        self.assertEqual(rx, "/tmp/leg_rx_b.json")
        self.assertEqual(tx, "/tmp/leg_tx_b.json")

    def test_two_instances_do_not_collide(self):
        a_rx, a_tx = probe.tmp_paths("")
        b_rx, b_tx = probe.tmp_paths("b")
        self.assertNotEqual(a_rx, b_rx)
        self.assertNotEqual(a_tx, b_tx)


class TestScriptsHonorPath(unittest.TestCase):
    def test_recv_default_and_tagged(self):
        default = probe.build_recv_script(
            bind_ip="10.0.0.1", port=48003, duration_s=5.0,
            expected_leg_id=2, max_datagram=2048,
        )
        self.assertIn("/tmp/leg_rx.json", default)
        tagged = probe.build_recv_script(
            bind_ip="10.0.0.1", port=48004, duration_s=5.0,
            expected_leg_id=2, max_datagram=2048,
            rx_tmp_path="/tmp/leg_rx_b.json",
        )
        self.assertIn("/tmp/leg_rx_b.json", tagged)

    def test_send_default_and_tagged(self):
        default = probe.build_send_script(
            bind_ip="10.0.0.2", dest_ip="10.0.0.1", port=48003, duration_s=5.0,
            rate_hz=25.0, payload_bytes=140, leg_id=2,
        )
        self.assertIn("/tmp/leg_tx.json", default)
        tagged = probe.build_send_script(
            bind_ip="10.0.0.2", dest_ip="10.0.0.1", port=48004, duration_s=5.0,
            rate_hz=10.0, payload_bytes=64, leg_id=2,
            tx_tmp_path="/tmp/leg_tx_b.json",
        )
        self.assertIn("/tmp/leg_tx_b.json", tagged)


class TestRunClusterDryRunTag(unittest.TestCase):
    def _plan(self, tmp_tag, port):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            probe.run_cluster(
                leg="return", duration_s=5.0, rate_hz=10.0, payload_bytes=64,
                port=port, out_dir=out, warmup_s=0.0,
                gcs_ip="10.1.1.1", uav_ip="10.1.1.2",
                context="kind-tm-oranslice", namespace="ran",
                dry_run=True, tmp_tag=tmp_tag,
            )
            return json.loads((out / "kubectl_plan_return.json").read_text())

    def test_default_keeps_historic_path(self):
        p = self._plan("", 48003)
        self.assertEqual(p["tmp_tag"], "")
        self.assertEqual(p["rx_tmp"], "/tmp/leg_rx.json")
        self.assertEqual(p["pull_rx"][-1], "/tmp/leg_rx.json")
        self.assertEqual(p["pull_tx"][-1], "/tmp/leg_tx.json")

    def test_tagged_uses_suffixed_path(self):
        p = self._plan("b", 48004)
        self.assertEqual(p["tmp_tag"], "b")
        self.assertEqual(p["rx_tmp"], "/tmp/leg_rx_b.json")
        self.assertEqual(p["pull_rx"][-1], "/tmp/leg_rx_b.json")
        self.assertEqual(p["pull_tx"][-1], "/tmp/leg_tx_b.json")
        self.assertEqual(p["port"], 48004)
        self.assertEqual(p["payload_bytes"], 64)
        self.assertEqual(p["rate_hz"], 10.0)


class TestRunSidecarsReturnProbeB(unittest.TestCase):
    def _run_dry(self, extra_args=None, env=None):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "sidecars"
            cmd = [
                sys.executable, str(RUN_SIDECARS),
                "--out-dir", str(out), "--duration", "300",
                "--profile", "FULL",
                "--gcs-ip", "12.1.1.66", "--uav-ip", "12.1.1.67",
                "--dry-run",
            ] + (extra_args or [])
            run_env = dict(os.environ)
            for k in ("RETURN_PROBE_B", "RETURN_PROBE_B_PAYLOAD_BYTES",
                      "RETURN_PROBE_B_RATE_HZ", "RETURN_PROBE_B_PORT"):
                run_env.pop(k, None)
            if env:
                run_env.update(env)
            subprocess.run(cmd, check=True, capture_output=True, text=True, env=run_env)
            return json.loads((out / "flyblind_sidecars_meta.json").read_text())

    def test_default_full_has_no_probe_b(self):
        meta = self._run_dry()
        self.assertNotIn("leg_owd_return_b", meta["children"])
        self.assertIn("leg_owd_return", meta["children"])
        self.assertFalse(meta["return_probe_b"]["enabled"])

    def test_flag_adds_probe_b(self):
        meta = self._run_dry(extra_args=["--with-return-probe-b"])
        self.assertIn("leg_owd_return_b", meta["children"])
        self.assertIn("leg_owd_return", meta["children"])
        b = meta["return_probe_b"]
        self.assertTrue(b["enabled"])
        self.assertEqual(b["payload_bytes"], 64)
        self.assertEqual(b["rate_hz"], 10.0)
        self.assertEqual(b["port"], 48004)
        self.assertEqual(b["out_subdir"], "probes_return_b")

    def test_env_adds_probe_b_with_overrides(self):
        meta = self._run_dry(env={"RETURN_PROBE_B": "1",
                                   "RETURN_PROBE_B_PORT": "48009",
                                   "RETURN_PROBE_B_RATE_HZ": "12"})
        self.assertIn("leg_owd_return_b", meta["children"])
        b = meta["return_probe_b"]
        self.assertTrue(b["enabled"])
        self.assertEqual(b["port"], 48009)
        self.assertEqual(b["rate_hz"], 12.0)


if __name__ == "__main__":
    unittest.main()

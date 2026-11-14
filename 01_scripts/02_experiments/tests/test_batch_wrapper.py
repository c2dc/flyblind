#!/usr/bin/env python3
##################################################################################################################################
# Module: test_batch_wrapper.py (Batch wrapper: default stop-on-invalid vs --continue-on-invalid (run-all))                      #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
##################################################################################################################################
"""Batch wrapper: default stop-on-invalid vs --continue-on-invalid (run-all)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

EXP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXP))

from instrumentation import batch_wrapper as bw  # noqa: E402


class BatchWrapperInvalidPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conds = self.tmp / "conditions.json"
        self.conds.write_text(json.dumps({"conditions": [
            {"id": "a", "argv": "--condition baseline"},
            {"id": "b", "argv": "--condition baseline"},
            {"id": "c", "argv": "--condition baseline"},
        ]}))
        self.out = self.tmp / "out"
        self.log = self.tmp / "batch_log.json"

    def _run(self, *extra):
        calls = []
        validity = iter([True, False, True])

        def fake_call(cmd):
            calls.append(cmd)
            return 0

        def fake_read(run_dir):
            ok = next(validity)
            return ok, {"valid": ok, "failed_gates": [] if ok else [{"gate": "x"}]}

        with patch.object(bw.subprocess, "call", side_effect=fake_call), \
             patch.object(bw, "_guess_run_dir", side_effect=lambda root, before: root / "rd"), \
             patch.object(bw, "_read_validity", side_effect=fake_read), \
             redirect_stdout(io.StringIO()):
            rc = bw.main([
                "--conditions", str(self.conds), "--seed", "1",
                "--out-root", str(self.out), "--batch-log", str(self.log), *extra,
            ])
        return rc, calls, json.loads(self.log.read_text())

    def test_default_stops_on_first_invalid(self):
        rc, calls, log = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(log["stopped_on_invalid"])
        self.assertFalse(log["continue_on_invalid"])
        self.assertEqual(len(log["invalid_runs"]), 1)

    def test_continue_on_invalid_runs_all(self):
        rc, calls, log = self._run("--continue-on-invalid")
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 3)
        self.assertFalse(log["stopped_on_invalid"])
        self.assertTrue(log["continue_on_invalid"])
        self.assertEqual(len(log["runs"]), 3)
        self.assertEqual(len(log["invalid_runs"]), 1)
        self.assertIn("discard_reason", log["runs"][1])


if __name__ == "__main__":
    unittest.main()

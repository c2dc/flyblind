#!/usr/bin/env python3
##################################################################################################################################
# Module: test_stream_cut_gate.py (unit tests — Gate A stream cut)                                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Stream-cut integrity gate tests for baseline condition.                                                           #
##################################################################################################################################

"""Unit tests for I-0A stream-cut validation gate (no cluster)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

EXP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXP))

import orchestrate as orch  # noqa: E402


def _write_cut_summary(
    run_dir: Path,
    *,
    cut_ms: float = 100.0,
    observed_ms: float = 100.0,
    resumed: bool = True,
    suppressed: int = 1,
    aoi_max: float = 200.0,
    rate_before: float = 10.0,
    rate_after: float = 10.0,
) -> None:
    payload = {
        "cut_start_rel_s": 15.0,
        "cut_end_rel_s": 15.1,
        "cut_requested_ms": cut_ms,
        "cut_observed_ms": observed_ms,
        "stream_cut_start_s": 15.0,
        "stream_resumed": resumed,
        "setpoints_suppressed": suppressed,
        "setpoints_before_cut": 150,
        "setpoints_after_cut": 440,
        "rate_before_cut_hz": rate_before,
        "rate_after_cut_hz": rate_after,
        "aoi_max_during_cut_ms": aoi_max,
        "aoi_p999_during_cut_ms": aoi_max * 0.95,
        "deadline_miss_ratio_during_cut": 0.5,
        "max_stale_streak_during_cut": 10,
    }
    (run_dir / "stream_cut_summary.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


class StreamCutGateTests(unittest.TestCase):
    def test_valid_temporary_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_cut_summary(run_dir, cut_ms=100.0, aoi_max=210.0, suppressed=1)
            ok, info = orch.evaluate_stream_cut_gate(
                run_dir, 100.0, 10.0, 60.0, n_setpoints=590,
            )
            self.assertTrue(ok, info)
            self.assertAlmostEqual(info["peak_expected_ms"], 200.0, places=1)

    def test_rejects_permanent_stop_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_cut_summary(
                run_dir,
                cut_ms=100.0,
                aoi_max=59000.0,
                suppressed=1,
                resumed=True,
            )
            ok, info = orch.evaluate_stream_cut_gate(run_dir, 100.0, 10.0, 60.0, n_setpoints=15)
            self.assertFalse(ok)
            self.assertIn("permanent", info.get("reason", ""))

    def test_rejects_no_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_cut_summary(run_dir, resumed=False, aoi_max=200.0)
            ok, info = orch.evaluate_stream_cut_gate(run_dir, 100.0, 10.0, 60.0)
            self.assertFalse(ok)
            self.assertIn("resume", info.get("reason", ""))


if __name__ == "__main__":
    unittest.main()

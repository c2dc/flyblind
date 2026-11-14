#!/usr/bin/env python3
##################################################################################################################################
# Module: test_mission_arm.py (unit tests — SITL arm helpers)                                                                    #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: mission_runner arm/retry logic tests (mocked MAVLink).                                                            #
##################################################################################################################################

"""Unit tests for SITL arm helpers (no cluster)."""
from __future__ import annotations

import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SITL_LIB = Path(__file__).resolve().parents[2] / "03_sitl" / "lib"
sys.path.insert(0, str(SITL_LIB))

from mission_runner import (  # noqa: E402
    _log_arm_diag,
    _mav_result_name,
    arm_sitl_with_retries,
)
from pymavlink import mavutil  # noqa: E402


class TestMissionArm(unittest.TestCase):
    def test_mav_result_name(self):
        self.assertEqual(_mav_result_name(mavutil.mavlink.MAV_RESULT_ACCEPTED), "ACCEPTED")
        self.assertEqual(_mav_result_name(mavutil.mavlink.MAV_RESULT_DENIED), "DENIED")

    def test_log_arm_diag_prints_statustext(self):
        diag = {
            "elapsed_ms": 1000.0,
            "statustext": ["PreArm: Need 3D Fix", "Arming motors"],
            "command_acks": [{"result": 4, "result_name": "DENIED"}],
            "last_heartbeat_armed": False,
            "last_custom_mode": 4,
            "timeout": False,
        }
        buf = StringIO()
        with patch("sys.stdout", buf):
            _log_arm_diag(diag, 1, 3)
        out = buf.getvalue()
        self.assertIn("PreArm", out)
        self.assertIn("DENIED", out)

    def test_arm_retries_calls_force_arm(self):
        calls = {"n": 0}

        def fake_force(_mav, timeout_s=10.0, poll=None):
            calls["n"] += 1
            if calls["n"] < 2:
                return False, 50.0, {"statustext": ["PreArm: Waiting"], "command_acks": [], "timeout": True, "elapsed_ms": 50.0}
            return True, 80.0, {"statustext": ["Arming motors"], "command_acks": [{"result": 0, "result_name": "ACCEPTED"}], "elapsed_ms": 80.0}

        class FakeMav:
            pass

        with patch("mission_runner.force_arm_sitl", side_effect=fake_force):
            with patch("mission_runner.ensure_disarmed_sitl"):
                with patch("mission_runner.poll_sleep"):
                    with patch("mission_runner.poll_messages"):
                        ok, lat = arm_sitl_with_retries(FakeMav(), attempts=3, timeout_s=1.0, retry_pause_s=0.01)
        self.assertTrue(ok)
        self.assertAlmostEqual(lat, 80.0)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()

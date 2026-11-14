#!/usr/bin/env python3
##################################################################################################################################
# Module: test_c2_forwarder_delta24.py (unit tests - forwarder telemetry stamp (b))                                              #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: stamp (b): oaitun ingress on the return leg; matchable per message.                                               #
##################################################################################################################################

"""Unit tests for the three-point stamping on the return leg.

(a) t_mono_ns          SITL egress read at the forwarder (UAV side)
(b) t_tun_ingress_ns   same message pushed to the oaitun uplink (just before sendto)
(c) at the GCS         telemetry_downlink_gcs.csv (not covered here; produced by mission_runner)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROBES = Path(__file__).resolve().parents[1] / "probes"
sys.path.insert(0, str(PROBES))

import c2_arrival_forwarder as fw  # noqa: E402


class _FakeNED:
    """Minimal stand-in for a parsed LOCAL_POSITION_NED message."""

    def __init__(self, seq, sysid, time_boot_ms, x, y, z):
        self._seq = seq
        self._sysid = sysid
        self.time_boot_ms = time_boot_ms
        self.x, self.y, self.z = x, y, z

    def get_seq(self):
        return self._seq

    def get_srcSystem(self):
        return self._sysid


class TestForwarderStampB(unittest.TestCase):
    def test_schema_has_tun_ingress(self):
        self.assertIn("t_tun_ingress_ns", fw.TELEMETRY_CSV_FIELDS)
        # (a) must still precede (b) in the schema.
        self.assertLess(
            fw.TELEMETRY_CSV_FIELDS.index("t_mono_ns"),
            fw.TELEMETRY_CSV_FIELDS.index("t_tun_ingress_ns"),
        )

    def test_row_carries_both_stamps_and_key(self):
        msg = _FakeNED(seq=7, sysid=1, time_boot_ms=123456, x=1.0, y=2.0, z=-10.0)
        t_a = 1_000_000_000
        t_b = 1_000_030_000  # 30 us later
        row = fw._telemetry_row_from_msg(msg, t_a, t_b)
        self.assertEqual(row["t_mono_ns"], t_a)
        self.assertEqual(row["t_tun_ingress_ns"], t_b)
        # matchable by sequence and time_boot_ms
        self.assertEqual(row["mavlink_sequence"], 7)
        self.assertEqual(row["time_boot_ms"], 123456)
        self.assertEqual(row["mavlink_system_id"], 1)
        # drone-side parcel (a)->(b) recoverable and non-negative here
        self.assertEqual(row["t_tun_ingress_ns"] - row["t_mono_ns"], 30_000)

    def test_row_keys_subset_of_fields(self):
        msg = _FakeNED(seq=1, sysid=1, time_boot_ms=10, x=0.0, y=0.0, z=0.0)
        row = fw._telemetry_row_from_msg(msg, 5, 6)
        for k in row:
            self.assertIn(k, fw.TELEMETRY_CSV_FIELDS)

    def test_missing_tun_stamp_is_blank(self):
        # If the datagram is not forwarded (no client yet), (b) stays blank, (a) kept.
        msg = _FakeNED(seq=2, sysid=1, time_boot_ms=20, x=0.0, y=0.0, z=0.0)
        row = fw._telemetry_row_from_msg(msg, 42, None)
        self.assertEqual(row["t_mono_ns"], 42)
        self.assertEqual(row["t_tun_ingress_ns"], "")


if __name__ == "__main__":
    unittest.main()

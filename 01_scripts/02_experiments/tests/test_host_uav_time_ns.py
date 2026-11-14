#!/usr/bin/env python3
##################################################################################################################################
# Module: test_host_uav_time_ns.py (unit tests — host vs UAV pod CLOCK_MONOTONIC gate)                                           #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Pure-function tests for evaluate_host_uav_clock and parse_timens_offsets (no cluster).                            #
#                                                                                                                                #
# Usage: python3 -m unittest tests.test_host_uav_time_ns                                                                         #
##################################################################################################################################

"""Unit tests for the host vs UAV pod shared-clock gate (no cluster)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

EXP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXP))

from instrumentation.host_uav_time_ns import (  # noqa: E402
    ZERO_OFFSETS,
    bench_cases,
    evaluate_host_uav_clock,
    parse_timens_offsets,
)

HOST = "time:[4026531834]"
POD = "time:[4026532902]"
T1, T2 = 1_000_000_000, 1_127_800_000


class TestParseTimensOffsets(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(
            parse_timens_offsets(ZERO_OFFSETS),
            {"monotonic": [0, 0], "boottime": [0, 0]},
        )

    def test_garbage(self) -> None:
        self.assertIsNone(parse_timens_offsets(None))
        self.assertIsNone(parse_timens_offsets("monotonic 0\n"))
        self.assertIsNone(parse_timens_offsets("monotonic 0 0\n"))


class TestEvaluateHostUavClock(unittest.TestCase):
    def test_inode_match(self) -> None:
        r = evaluate_host_uav_clock(HOST, HOST, HOST, ZERO_OFFSETS, 1_050_000_000, T1, T2)
        self.assertTrue(r["ok"])
        self.assertEqual(r["basis"], "inode_match")

    def test_kind_zero_offsets_in_window(self) -> None:
        r = evaluate_host_uav_clock(HOST, POD, POD, ZERO_OFFSETS, 1_050_000_000, T1, T2)
        self.assertTrue(r["ok"])
        self.assertEqual(r["basis"], "zero_offsets_and_bracket")
        self.assertEqual(r["bracket_window_ms"], 127.8)

    def test_nonzero_offset_fails(self) -> None:
        off = "monotonic      172800         0\nboottime            0         0\n"
        r = evaluate_host_uav_clock(HOST, POD, POD, off, 1_050_000_000, T1, T2)
        self.assertFalse(r["ok"])
        self.assertIn("non-zero or unreadable offsets", r["error"])

    def test_out_of_window_fails(self) -> None:
        r = evaluate_host_uav_clock(HOST, POD, POD, ZERO_OFFSETS, 2_000_000_000, T1, T2)
        self.assertFalse(r["ok"])
        self.assertIn("outside the host [t1, t2]", r["error"])

    def test_offsets_of_children_ns_not_trusted(self) -> None:
        r = evaluate_host_uav_clock(
            HOST, POD, "time:[4026532999]", ZERO_OFFSETS, 1_050_000_000, T1, T2,
        )
        self.assertFalse(r["ok"])
        self.assertIn("time_for_children", r["error"])

    def test_missing_bracket_fails(self) -> None:
        r = evaluate_host_uav_clock(HOST, POD, POD, ZERO_OFFSETS, None, T1, T2)
        self.assertFalse(r["ok"])

    def test_bench_cases_all_pass(self) -> None:
        self.assertTrue(all(row["pass"] for row in bench_cases()))


if __name__ == "__main__":
    unittest.main()

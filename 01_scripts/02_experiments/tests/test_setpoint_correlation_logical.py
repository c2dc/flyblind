#!/usr/bin/env python3
##################################################################################################################################
# Module: test_setpoint_correlation_logical.py (Setpoint correlation uses logical setpoints only (preposition frames excluded))  #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
##################################################################################################################################
"""Setpoint correlation uses logical setpoints only (preposition frames excluded)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SITL_LIB = Path(__file__).resolve().parents[2] / "03_sitl" / "lib"
sys.path.insert(0, str(SITL_LIB))

from freshness_metrics import (  # noqa: E402
    SetpointRecord,
    _pick_proxy_correlation,
    drop_preposition_frames,
    logical_ids_embedded,
    merge_proxy_artifacts,
)

PERIOD_NS = 100_000_000
OWD_NS = 12_000_000


def _sp(i: int, t_send: int) -> SetpointRecord:
    return SetpointRecord(
        logical_setpoint_id=i,
        generation_index=i,
        trajectory_id="test",
        t_generation_gcs_ns=t_send - 1_000_000,
        t_send_gcs_ns=t_send,
        x_ref=float(i),
        y_ref=0.0,
        z_ref=-10.0,
    )


def _frame(t_arrival: int, boot_ms: int) -> dict:
    return {
        "mavlink_msg_type": "SET_POSITION_TARGET_LOCAL_NED",
        "t_arrival_ns": t_arrival,
        "t_forward_ns": t_arrival + 20_000,
        "time_boot_ms": boot_ms,
    }


def _stream_with_preposition(n_pre: int, n_logical: int, lost: set[int]):
    """n_pre preposition frames (id 0) before the stream; ids in `lost` never arrive."""
    t_first = 10 * 1_000_000_000
    sps = [_sp(i, t_first + (i - 1) * PERIOD_NS) for i in range(1, n_logical + 1)]
    fwd = [_frame(t_first - (n_pre - k) * PERIOD_NS + OWD_NS, 0) for k in range(n_pre)]
    fwd += [_frame(sp.t_send_gcs_ns + OWD_NS, sp.logical_setpoint_id)
            for sp in sps if sp.logical_setpoint_id not in lost]
    return sps, fwd


class TestLogicalSetpointCorrelation(unittest.TestCase):
    def test_preposition_frames_detected_and_dropped(self):
        sps, fwd = _stream_with_preposition(69, 50, set())
        self.assertTrue(logical_ids_embedded(sps, fwd))
        self.assertEqual(len(drop_preposition_frames(fwd)), 50)

    def test_exact_id_wins_over_order_with_preposition_and_loss(self):
        lost = {20, 21, 22}
        sps, fwd = _stream_with_preposition(69, 50, lost)
        matched, total, method, info = _pick_proxy_correlation(sps, fwd)
        self.assertEqual(method, "setpoint_id")
        self.assertEqual(info["selection"], "logical_setpoint_id_exact")
        self.assertEqual(matched, 47)
        self.assertEqual(total, 47)
        self.assertGreaterEqual(info["matches_by_method"]["ordinal"], matched)
        for sp in sps:
            if sp.logical_setpoint_id in lost:
                self.assertIsNone(sp.t_arrival_uav_proxy_ns)
            else:
                self.assertAlmostEqual(sp.network_arrival_delay_ms(), OWD_NS / 1e6, places=3)

    def test_merge_counts_logical_only(self):
        lost = {48, 49, 50}
        sps, fwd = _stream_with_preposition(69, 50, lost)
        t0 = sps[0].t_generation_gcs_ns
        _, _, meta = merge_proxy_artifacts(
            sps, fwd, [], 100.0, t0,
            t_active_end_ns=sps[-1].t_generation_gcs_ns + PERIOD_NS, post_stream_s=0.0,
        )
        acct = meta["setpoint_accounting"]
        self.assertEqual(meta["correlation_method"], "setpoint_id+accept_xyz")
        self.assertEqual(acct["unique_setpoints_seen_by_proxy"], 47)
        self.assertEqual(acct["correlated_setpoints"], 47)
        self.assertEqual(acct["uncorrelated_setpoints"], 3)
        self.assertEqual(acct["proxy_forward_preposition_frames_excluded"], 69)
        self.assertEqual(meta["correlation_ratio"], 1.0)
        delays = [sp.network_arrival_delay_ms() for sp in sps if sp.t_arrival_uav_proxy_ns]
        self.assertTrue(all(d > 0 for d in delays))

    def test_legacy_frames_without_ids_keep_max_matched(self):
        sps = [_sp(i, 10 * 1_000_000_000 + (i - 1) * PERIOD_NS) for i in range(1, 11)]
        fwd = [_frame(sp.t_send_gcs_ns + OWD_NS, 1000 + 50 * k) for k, sp in enumerate(sps)]
        self.assertFalse(logical_ids_embedded(sps, fwd))
        matched, _, method, info = _pick_proxy_correlation(sps, fwd)
        self.assertEqual(info["selection"], "max_matched")
        self.assertNotEqual(method, "setpoint_id")
        self.assertEqual(matched, 10)


if __name__ == "__main__":
    unittest.main()

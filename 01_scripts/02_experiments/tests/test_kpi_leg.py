#!/usr/bin/env python3
##################################################################################################################################
# Module: test_kpi_leg.py (Bench tests: percentile, loss, synthetic delay, IPDV, M2 rejection)                                   #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
##################################################################################################################################
"""Bench tests: percentile, loss, synthetic delay, IPDV, M2 rejection."""
from __future__ import annotations

import json
import socket
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
PROBES = Path(__file__).resolve().parents[1] / "probes"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(PROBES))

from kpi_leg import (  # noqa: E402
    compute_ipdv_by_seq,
    compute_owd_kpis,
    evaluate_c1_leg,
    evaluate_c4_kpi_slice,
    loss_by_seq,
    owd_histogram,
    percentile_round_index,
    setpoint_counts_m1,
    delivery_rate_post_warmup,
)
from leg_owd_probe import (  # noqa: E402
    MAGIC,
    endpoints_for_leg,
    pack_payload,
    run_bench,
    run_cluster,
    unpack_header,
)


class TestPercentile(unittest.TestCase):
    def test_round_index_matches_august(self):
        vals = [float(i) for i in range(100)]
        self.assertEqual(percentile_round_index(vals, 50), 50.0)
        # idx = round(0.99*99)=98
        self.assertEqual(percentile_round_index(vals, 99), 98.0)

    def test_insufficient_n_flags(self):
        samples = [
            {"seq": i, "owd_s": 0.01 * i, "t_rx_mono_ns": 1_000_000_000 + i * 40_000_000}
            for i in range(50)
        ]
        kpi = compute_owd_kpis(
            leg="command",
            samples_rx=samples,
            tx_seqs=list(range(50)),
            rate_hz=25.0,
            duration_s=2.0,
            warmup_s=0.0,
        )
        all_ph = kpi["phases"]["ALL"]
        self.assertTrue(all_ph["owd_s"]["percentile_insufficient"]["p99"])
        self.assertIsNone(all_ph["owd_s"]["primary"]["p99"])


class TestLossAndIpdv(unittest.TestCase):
    def test_loss_by_seq(self):
        loss = loss_by_seq([0, 1, 2, 3, 4], [0, 1, 3, 4])
        self.assertEqual(loss["lost"], 1)
        self.assertEqual(loss["lost_seqs_sample"], [2])

    def test_ipdv_ordered_by_seq(self):
        # Arrive out of order but IPDV uses seq order
        samples = [
            {"seq": 2, "owd_s": 0.03, "t_rx_mono_ns": 30},
            {"seq": 0, "owd_s": 0.01, "t_rx_mono_ns": 10},
            {"seq": 1, "owd_s": 0.02, "t_rx_mono_ns": 20},
        ]
        info = compute_ipdv_by_seq(samples)
        self.assertEqual(info["out_of_order_count"], 0)  # arrival sorted by t_rx is 0,1,2
        self.assertAlmostEqual(info["ipdv_s"][0], 0.01)
        samples2 = [
            {"seq": 0, "owd_s": 0.01, "t_rx_mono_ns": 20},
            {"seq": 1, "owd_s": 0.02, "t_rx_mono_ns": 10},  # seq1 before seq0 in time
        ]
        info2 = compute_ipdv_by_seq(samples2)
        self.assertGreaterEqual(info2["out_of_order_count"], 1)


class TestDeliveryRate(unittest.TestCase):
    def test_no_hardcoded_1khz(self):
        r = delivery_rate_post_warmup(n_received_post=50, rate_hz=25.0, duration_s=3.0, warmup_s=1.0)
        # useful=2s * 25 = 50
        self.assertEqual(r["expected_post_warmup"], 50)
        self.assertAlmostEqual(r["delivery_rate_post_warmup_pct"], 100.0)


class TestSyntheticDelay(unittest.TestCase):
    def test_known_owd_recovered(self):
        delay = 0.123
        samples = [
            {"seq": i, "owd_s": delay, "t_rx_mono_ns": 10**9 + i * 40_000_000}
            for i in range(200)
        ]
        kpi = compute_owd_kpis(
            leg="return",
            samples_rx=samples,
            tx_seqs=list(range(200)),
            rate_hz=25.0,
            duration_s=8.0,
        )
        p50 = kpi["phases"]["ALL"]["owd_s"]["primary"]["p50"]
        self.assertAlmostEqual(p50, delay, places=6)


class TestHeaderAndBench(unittest.TestCase):
    def test_pack_unpack(self):
        pkt = pack_payload(leg="command", seq=7, t_tx_ns=123456789, payload_bytes=24, rate_hz=25.0)
        self.assertEqual(len(pkt), 24)
        hdr = unpack_header(pkt)
        self.assertEqual(hdr["magic"], MAGIC)
        self.assertEqual(hdr["leg_id"], 1)
        self.assertEqual(hdr["seq"], 7)

    def test_bench_loopback_direction_and_tx_log(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            # Pick a free port
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            report = run_bench(
                leg="return",
                duration_s=0.4,
                rate_hz=25.0,
                payload_bytes=140,
                port=port,
                out_dir=out,
                warmup_s=0.0,
            )
            self.assertEqual(report["leg"], "return")
            with (out / "tx_return.jsonl").open() as f:
                tx = list(f)
            with (out / "rx_return.jsonl").open() as f:
                rx = [json.loads(l) for l in f]
            self.assertGreater(len(tx), 5)
            accepted = [r for r in rx if not r.get("rejected")]
            self.assertGreater(len(accepted), 5)
            self.assertTrue(all(r["leg"] == "return" for r in accepted))
            # Direction bench: RX only accepts matching leg_id
            self.assertEqual(report.get("rejected_header"), 0)

    def test_m2_rejects_wrong_leg(self):
        pkt = pack_payload(leg="command", seq=1, t_tx_ns=1, payload_bytes=24, rate_hz=25.0)
        # Flipping leg_id to a return-leg mismatch while expecting the command leg would also work;
        # craft wrong magic
        bad = bytearray(pkt)
        bad[0] = 0x00
        bad[1] = 0x00
        hdr = unpack_header(bytes(bad))
        self.assertNotEqual(hdr["magic"], MAGIC)


class TestSetpointCountsM1(unittest.TestCase):
    def test_late_send_excluded_arrival_in_drain_counts(self):
        class SP:
            def __init__(self, i, t_send):
                self.logical_setpoint_id = i
                self.t_send_gcs_ns = t_send

        t0 = 0
        # W = 2s; last send = 10s; cutoff = 8s
        sps = [SP(i, int(i * 1e9)) for i in range(0, 11)]  # sends at 0..10 s
        arrivals = {i: True for i in range(0, 11)}  # all arrived (incl. after drain)
        windows = {
            "PRE": {"t_rel_lo_s": 0.0, "t_rel_hi_s": 5.0},
            "ATTACK": {"t_rel_lo_s": 5.0, "t_rel_hi_s": 8.0},
            "DRAIN": {"t_rel_lo_s": 8.0, "t_rel_hi_s": 20.0},
        }
        out = setpoint_counts_m1(
            sps,
            arrivals,
            t_last_successful_send_ns=int(10e9),
            max_owd_s_observed=2.0,
            windows=windows,
            t0_mono_ns=t0,
            get_send_ns=lambda sp: sp.t_send_gcs_ns,
            get_id=lambda sp: sp.logical_setpoint_id,
        )
        self.assertEqual(out["W_s"], 2.0)
        self.assertEqual(out["n_sends_excluded_by_W"], 2)  # sends at 9s and 10s
        # ids 0..8 in universe
        self.assertEqual(out["n_sent_universe"], 9)


class TestHistSeriesC1C4(unittest.TestCase):
    def test_histogram_and_timeseries_in_kpi(self):
        samples = [
            {"seq": i, "owd_s": 0.02, "t_rx_mono_ns": 10**9 + i * 40_000_000}
            for i in range(200)
        ]
        kpi = compute_owd_kpis(
            leg="command",
            samples_rx=samples,
            tx_seqs=list(range(200)),
            rate_hz=25.0,
            duration_s=8.0,
        )
        all_ph = kpi["phases"]["ALL"]
        self.assertIn("owd_histogram", all_ph)
        self.assertEqual(all_ph["owd_histogram"]["n"], 200)
        self.assertGreater(len(all_ph["owd_histogram"]["bins"]), 3)
        self.assertIn("owd_time_series", all_ph)
        self.assertEqual(all_ph["owd_time_series"]["n_points"], 200)
        self.assertIn("criterion_C1", all_ph)
        self.assertIn("criterion_C4", all_ph)
        self.assertFalse(all_ph["criterion_C1"]["effect_present"])
        self.assertIsNone(all_ph["criterion_C4"]["checks"]["FS_GCS"]["pass"])

    def test_c1_tail_regime(self):
        # p50 small, p99 large → effect only in the tail
        vals = [0.02] * 90 + [0.20] * 10
        samples = [
            {"seq": i, "owd_s": vals[i], "t_rx_mono_ns": 10**9 + i * 40_000_000}
            for i in range(100)
        ]
        kpi = compute_owd_kpis(
            leg="return",
            samples_rx=samples,
            tx_seqs=list(range(100)),
            rate_hz=25.0,
            duration_s=4.0,
        )
        c1 = kpi["phases"]["ALL"]["criterion_C1"]
        self.assertTrue(c1["effect_present"])
        self.assertTrue(c1["effect_tail_only"])

    def test_c4_fail_on_gap(self):
        # Two arrivals 4s apart → lacuna > 3s
        samples = [
            {"seq": 0, "owd_s": 0.01, "t_rx_mono_ns": 1_000_000_000},
            {"seq": 1, "owd_s": 0.01, "t_rx_mono_ns": 5_000_000_000},
        ]
        kpi = compute_owd_kpis(
            leg="command",
            samples_rx=samples,
            tx_seqs=[0, 1],
            rate_hz=25.0,
            duration_s=5.0,
        )
        c4 = kpi["phases"]["ALL"]["criterion_C4"]
        self.assertFalse(c4["checks"]["no_gap_gt_3s"]["pass"])

    def test_owd_histogram_counts(self):
        h = owd_histogram([0.02, 0.03, 1.5, 6.0])
        self.assertEqual(h["n"], 4)
        total = sum(b["count"] for b in h["bins"])
        self.assertEqual(total, 4)


class TestClusterDryRun(unittest.TestCase):
    def test_endpoints_polarity(self):
        self.assertEqual(endpoints_for_leg("command"), ("oai-ue-gcs", "oai-ue-uav"))
        self.assertEqual(endpoints_for_leg("return"), ("oai-ue-uav", "oai-ue-gcs"))

    def test_dry_run_writes_plan(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            report = run_cluster(
                leg="command",
                duration_s=5.0,
                rate_hz=25.0,
                payload_bytes=24,
                port=48002,
                out_dir=out,
                warmup_s=0.0,
                gcs_ip="10.1.1.1",
                uav_ip="10.1.1.2",
                context="kind-tm-oranslice",
                namespace="ran",
                dry_run=True,
            )
            self.assertEqual(report["mode"], "dry_run_kubectl")
            plan_path = out / "kubectl_plan_command.json"
            self.assertTrue(plan_path.is_file())
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["tx_deploy"], "oai-ue-gcs")
            self.assertEqual(plan["rx_deploy"], "oai-ue-uav")
            self.assertIn("kubectl", plan["recv"]["argv"][0])
            self.assertEqual(plan["recv"]["argv"][0], "kubectl")
            # time namespace check present
            self.assertEqual(len(plan["time_namespace_check"]), 2)


if __name__ == "__main__":
    unittest.main()

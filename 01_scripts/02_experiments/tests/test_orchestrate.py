#!/usr/bin/env python3
##################################################################################################################################
# Module: test_orchestrate.py (unit tests — orchestrate)                                                                         #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Mitigation-stack orchestrate unit tests.                                                                          #
##################################################################################################################################

"""Unit tests for mitigation-only orchestration (no Kubernetes cluster)."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

EXP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXP))

import orchestrate as orch  # noqa: E402
import results_layout as layout  # noqa: E402


def _args(**overrides):
    base = {
        "mitigation_rate_limit_a": False,
        "mitigation_aqm_ul": False,
        "mitigation_ul_rrmpolicy": False,
        "min_ratio_slice_a": 0,
        "attack_path": "pdu",
        "attack": "multi-rogue",
        "multi_rogue_count": 4,
        "rate_limit_a_mbps": 8.0,
        "rate_limit_a_burst_kb": 64,
        "rate_limit_a_latency_ms": 50,
        "aqm_ul_interface": "auto",
        "aqm_ul_rate_mbps": 14.0,
        "aqm_ul_burst_kb": 32,
        "aqm_ul_target_ms": 5.0,
        "aqm_ul_interval_ms": 100.0,
        "aqm_ul_limit_packets": 1024,
        "aqm_ul_ecn": True,
        "c2_proxy": False,
        "workload": "setpoint-stream",
        "require_prb": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class RuntimeTcMitigationTests(unittest.TestCase):
    @patch.object(orch, "_tc_exec")
    @patch.object(orch, "_detect_pdu_iface", return_value="oaitun_ue1")
    def test_rate_limit_is_aggregate_and_split_across_rogues(self, _iface, tc_exec):
        tc_exec.return_value = subprocess.CompletedProcess(
            [], 0, stdout='[{"kind":"tbf"}]', stderr=""
        )
        args = _args(mitigation_rate_limit_a=True)
        audit = orch.apply_runtime_mitigations(args, "kind-x", "ran")
        self.assertTrue(audit["pass"])
        self.assertEqual(audit["rate_limit_a"]["aggregate_cap_mbps"], 8.0)
        self.assertEqual(audit["rate_limit_a"]["per_rogue_cap_mbps"], 2.0)
        self.assertEqual(len(audit["rate_limit_a"]["entries"]), 4)
        for call in tc_exec.call_args_list:
            self.assertIn("rate 2.000000mbit", call.args[3])

    @patch.object(orch, "_tc_exec")
    @patch.object(orch, "_detect_pdu_iface", return_value="oaitun_ue1")
    def test_aqm_uses_uav_pdu_ue1(self, detect, tc_exec):
        tc_exec.return_value = subprocess.CompletedProcess(
            [], 0, stdout='[{"kind":"htb"},{"kind":"fq_codel"}]', stderr=""
        )
        args = _args(mitigation_aqm_ul=True)
        audit = orch.apply_runtime_mitigations(args, "kind-x", "ran")
        self.assertTrue(audit["aqm_ul"]["verified"])
        detect.assert_called_once_with("kind-x", "ran", "oai-ue-uav", "oaitun_ue1")
        script = tc_exec.call_args.args[3]
        self.assertIn("htb default 10", script)
        self.assertIn("fq_codel", script)
        self.assertIn("target 5ms", script)

    def test_mitigation_integrity_is_enforced(self):
        args = _args(mitigation_aqm_ul=True)
        enforced = orch.enforced_validity_gates(args, is_baseline=False)
        self.assertIn("mitigation_integrity", enforced)

    def test_results_name_contains_all_active_defenses(self):
        args = _args(
            condition="mitigation",
            reservation=60,
            min_ratio_slice_a=60,
            mitigation_rate_limit_a=True,
            mitigation_aqm_ul=True,
            mitigation_ul_rrmpolicy=True,
        )
        token = layout.build_scenario_token(args)
        self.assertIn("MITIGATION_A60", token)
        self.assertIn("MITIGATION_PRB60", token)
        self.assertIn("MITIGATION_RATELIMIT_A", token)
        self.assertIn("MITIGATION_AQM_UL", token)
        self.assertIn("MITIGATION_UL_RRMPOLICY", token)

    def test_results_name_a60_contrast_without_b_floor(self):
        args = _args(
            condition="attack",
            reservation=0,
            min_ratio_slice_a=60,
            mitigation_ul_rrmpolicy=True,
        )
        token = layout.build_scenario_token(args)
        self.assertIn("MITIGATION_A60", token)
        self.assertNotIn("MITIGATION_PRB", token)
        self.assertEqual(
            token,
            "ATK_N4_AND_MITIGATION_A60_AND_MITIGATION_UL_RRMPOLICY",
        )


class StampDefaultsTests(unittest.TestCase):
    def test_stamp_enables_aqm_and_ul_rrm_and_syncs_reservation(self):
        args = _args(topology="cross", reservation=0)
        orch.apply_mitigation_stamp_defaults(args, {
            "MITIGATION_AQM": "1",
            "MITIGATION_UL_RRMPOLICY": "1",
            "MIN_RATIO_SLICE_B": "60",
            "MIN_RATIO_SLICE_A": "0",
        })
        self.assertTrue(args.mitigation_aqm_ul)
        self.assertTrue(args.mitigation_ul_rrmpolicy)
        self.assertEqual(args.reservation, 60)
        self.assertEqual(args.min_ratio_slice_a, 0)

    def test_stamp_syncs_min_ratio_slice_a(self):
        args = _args(topology="cross", reservation=0, min_ratio_slice_a=0)
        orch.apply_mitigation_stamp_defaults(args, {
            "MITIGATION_UL_RRMPOLICY": "1",
            "MIN_RATIO_SLICE_A": "60",
            "MIN_RATIO_SLICE_B": "0",
        })
        self.assertEqual(args.min_ratio_slice_a, 60)
        self.assertEqual(args.reservation, 0)
        self.assertTrue(args.mitigation_ul_rrmpolicy)

    @patch.object(orch, "_tc_exec")
    @patch.object(orch, "_detect_pdu_iface", return_value="oaitun_ue1")
    def test_aqm_shared_ue_claim_without_5qi(self, _iface, tc_exec):
        tc_exec.return_value = subprocess.CompletedProcess(
            [], 0, stdout='[{"kind":"htb"},{"kind":"fq_codel"}]', stderr=""
        )
        args = _args(mitigation_aqm_ul=True)
        audit = orch.apply_runtime_mitigations(args, "kind-x", "ran")
        self.assertTrue(audit["aqm_ul"]["shared_ue_ul_with_c2"])
        self.assertEqual(audit["aqm_ul"]["claim"], "shared_ue_ul_host_aqm")
        self.assertEqual(audit["aqm_ul"]["role"], "end_host_queue_control_ablation")


class AqmQdiscSamplingTests(unittest.TestCase):
    def test_extract_prefers_fq_codel_leaf(self):
        payload = json.dumps([
            {"kind": "htb", "backlog": 9999, "qlen": 99, "drops": 1},
            {
                "kind": "fq_codel",
                "backlog": 1200,
                "qlen": 3,
                "drops": 7,
                "ecn_mark": 2,
                "ce_mark": 1,
                "bytes": 100000,
                "packets": 80,
            },
        ])
        sample = orch.extract_aqm_qdisc_sample(payload)
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertTrue(sample["fq_codel_present"])
        self.assertEqual(sample["backlog_bytes"], 1200)
        self.assertEqual(sample["qlen"], 3)
        self.assertEqual(sample["drops"], 7)
        self.assertEqual(sample["ecn_mark"], 2)

    def test_summarize_marks_local_queue_ok(self):
        samples = [
            {"backlog_bytes": 100, "qlen": 1, "drops": 0, "ecn_mark": 0},
            {"backlog_bytes": 400, "qlen": 2, "drops": 1, "ecn_mark": 0},
            {"backlog_bytes": 200, "qlen": 1, "drops": 1, "ecn_mark": 1},
            {"backlog_bytes": 50, "qlen": 0, "drops": 2, "ecn_mark": 1},
            {"backlog_bytes": 80, "qlen": 1, "drops": 2, "ecn_mark": 1},
        ]
        summary = orch.summarize_aqm_qdisc_timeseries(
            samples, healthy_max_backlog_kb=64.0, healthy_max_qlen=64,
        )
        self.assertTrue(summary["local_queue_ok"])
        self.assertEqual(summary["max_backlog_bytes"], 400)
        self.assertEqual(summary["drops_delta"], 2)
        self.assertEqual(summary["ecn_mark_delta"], 1)

    def test_summarize_rejects_deep_backlog(self):
        samples = [
            {"backlog_bytes": 100, "qlen": 1, "drops": 0, "ecn_mark": 0},
            {"backlog_bytes": 200000, "qlen": 5, "drops": 0, "ecn_mark": 0},
            {"backlog_bytes": 100, "qlen": 1, "drops": 0, "ecn_mark": 0},
            {"backlog_bytes": 100, "qlen": 1, "drops": 0, "ecn_mark": 0},
            {"backlog_bytes": 100, "qlen": 1, "drops": 0, "ecn_mark": 0},
        ]
        summary = orch.summarize_aqm_qdisc_timeseries(
            samples, healthy_max_backlog_kb=64.0, healthy_max_qlen=64,
        )
        self.assertFalse(summary["local_queue_ok"])
        self.assertIn("max_backlog_bytes", summary["local_queue_ok_reason"])


RRM_JSON = (
    '{"rrmPolicyRatio": ['
    '{"sst": 1, "sd": 16777215, "min_ratio": 0, "max_ratio": 100, "dedicated_ratio": 0},'
    '{"sst": 1, "sd": 1, "min_ratio": 60, "max_ratio": 100, "dedicated_ratio": 0}'
    "]}"
)


class UlRrmPolicyTests(unittest.TestCase):
    @patch.object(orch.time, "sleep")
    @patch.object(orch, "kctl")
    def test_activation_reads_ratios_and_waits_for_ack(self, kctl, _sleep):
        kctl.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=RRM_JSON, stderr=""),  # cat rrmPolicy.json
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),  # switch write
            subprocess.CompletedProcess(
                [], 0, stdout="[TM_UL_RRMPOLICY] active: NSSAI-keyed UL two-level scheduler\n",
                stderr="",
            ),
        ]
        args = _args(mitigation_ul_rrmpolicy=True)
        audit = orch.configure_ul_rrmpolicy(args, "kind-x", "ran")
        self.assertTrue(audit["pass"])
        self.assertEqual(audit["keying"], "s_nssai")
        self.assertEqual(audit["policy"], "enabled=1\n")
        self.assertEqual(len(audit["rrm_policy_ratio"]), 2)
        self.assertEqual(audit["rrm_policy_ratio"][1]["min_ratio"], 60)
        self.assertEqual(
            getattr(args, "_testbed_mitigation_audit")["ul_rrmpolicy_runtime"], audit,
        )

    @patch.object(orch.time, "sleep")
    @patch.object(orch, "kctl")
    def test_missing_ack_disables_switch_and_raises(self, kctl, _sleep):
        no_ack = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        kctl.side_effect = (
            [subprocess.CompletedProcess([], 0, stdout=RRM_JSON, stderr="")]
            + [subprocess.CompletedProcess([], 0, stdout="", stderr="")]
            + [no_ack] * 20
            + [subprocess.CompletedProcess([], 0, stdout="", stderr="")]  # rm -f cleanup
        )
        args = _args(mitigation_ul_rrmpolicy=True)
        with self.assertRaisesRegex(RuntimeError, "did not acknowledge"):
            orch.configure_ul_rrmpolicy(args, "kind-x", "ran")
        rm_call = kctl.call_args_list[-1]
        self.assertIn("rm", rm_call.args)
        self.assertIn("/tmp/tm_ul_rrmpolicy.conf", rm_call.args)

    @patch.object(orch, "kctl")
    def test_disabled_flag_removes_policy_file(self, kctl):
        kctl.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        args = _args(mitigation_ul_rrmpolicy=False)
        audit = orch.configure_ul_rrmpolicy(args, "kind-x", "ran")
        self.assertEqual(audit, {"enabled": False, "pass": True})
        self.assertIn("/tmp/tm_ul_rrmpolicy.conf", kctl.call_args.args)

    def test_mitigation_integrity_enforced_and_token(self):
        args = _args(mitigation_ul_rrmpolicy=True)
        enforced = orch.enforced_validity_gates(args, is_baseline=False)
        self.assertIn("mitigation_integrity", enforced)
        token = layout.build_scenario_token(
            _args(condition="mitigation", reservation=60, mitigation_ul_rrmpolicy=True)
        )
        self.assertIn("MITIGATION_UL_RRMPOLICY", token)


class PaperCampaignDefaultTests(unittest.TestCase):
    def test_paper_campaign_defaults(self):
        a = orch.parse_args(["--condition", "baseline"])
        self.assertEqual(a.workload, "setpoint-stream")
        self.assertEqual(a.setpoint_rate_hz, 10.0)
        self.assertEqual(a.trajectory, "route")
        self.assertEqual(a.attack, "multi-rogue")
        self.assertEqual(a.topology, "cross")
        self.assertEqual(a.gnb_prb, 106)
        # Campaign-invariant defaults: a condition only has to state what varies, so these guard the
        # configuration the reported runs were taken under (see 01_scripts/04_campaign/campaign.sh).
        self.assertEqual(a.duration, 600)
        self.assertEqual(a.warmup, 10)
        self.assertEqual(a.trajectory_speed_mps, 4.0)
        self.assertEqual(a.companion_video_mbps, 12.0)
        self.assertEqual(a.companion_video_shape, "aviator")
        self.assertEqual(a.sidecars, "FULL")
        self.assertTrue(a.c2_arrival_capture)
        self.assertTrue(a.telemetry_arrival_capture)
        self.assertTrue(a.require_prb)
        off = orch.parse_args([
            "--condition", "baseline",
            "--workload", "waypoint",
            "--no-c2-arrival-capture",
            "--no-telemetry-arrival-capture",
            "--no-require-prb",
        ])
        self.assertEqual(off.workload, "waypoint")
        self.assertFalse(off.c2_arrival_capture)
        self.assertFalse(off.telemetry_arrival_capture)
        self.assertFalse(off.require_prb)

    def test_results_tag_rate_limit_control_folder(self):
        tagged = orch.parse_args([
            "--condition", "baseline",
            "--companion-video-mbps", "12",
            "--duration", "60",
            "--results-tag", "RATELIMIT_A",
        ])
        self.assertEqual(tagged.results_tag, "RATELIMIT_A")
        self.assertFalse(tagged.mitigation_rate_limit_a)
        name = layout.build_campaign_dirname(tagged, stamp_extra_rogues=3)
        self.assertEqual(
            name,
            "106_PRB-CROSS-4_ROGUES-BASELINE_AND_MITIGATION_RATELIMIT_A-60S-12MPBS",
        )
        paper = orch.parse_args([
            "--condition", "baseline",
            "--companion-video-mbps", "12",
            "--duration", "60",
        ])
        paper_name = layout.build_campaign_dirname(paper, stamp_extra_rogues=3)
        self.assertEqual(paper_name, "106_PRB-CROSS-4_ROGUES-BASELINE-60S-12MPBS")
        self.assertNotEqual(name, paper_name)


class MultiRogueModeCliTests(unittest.TestCase):
    def test_parses_multi_rogue_dl(self):
        args = orch.parse_args(["--condition", "attack", "--attack", "multi-rogue-dl"])
        self.assertEqual(args.attack, "multi-rogue-dl")
        self.assertEqual(orch.rogue_count_for_args(args), 4)

    def test_cli_rejects_legacy_attack_names(self):
        for name in (
            "pulse", "pulse-c2", "flood-c2", "cbr", "combined", "combined-c2",
            "sync-c2", "sync-prb", "microburst", "dual-pulse",
        ):
            with self.assertRaises(SystemExit):
                orch.parse_args(["--condition", "attack", "--attack", name])

    def test_parses_multi_rogue_dl_sync(self):
        args = orch.parse_args([
            "--condition", "attack", "--attack", "multi-rogue-dl-sync",
            "--sync-open-loop",
        ])
        self.assertEqual(args.attack, "multi-rogue-dl-sync")
        self.assertTrue(orch.sync_open_loop_enabled(args))
        self.assertIn("multi-rogue-dl-sync", orch.DL_FAMILY_ATTACKS)

    def test_parses_multi_rogue_sync_defaults(self):
        args = orch.parse_args(["--condition", "attack", "--attack", "multi-rogue-sync"])
        self.assertEqual(args.attack, "multi-rogue-sync")
        self.assertEqual(args.multi_sync_burst_ms, 25.0)
        self.assertEqual(args.multi_sync_pps, 20000)
        self.assertEqual(args.multi_sync_size, 1400)

    def test_multi_rogue_sync_is_ul_to_ext_dn_only(self):
        args = orch.parse_args([
            "--condition", "attack", "--attack", "multi-rogue-sync",
            "--multi-rogue-count", "2", "--sync-open-loop",
        ])

        class FakeResult:
            stdout = "10.244.0.50"

        with patch.object(orch, "attack_taskset", return_value=""), \
             patch.object(orch, "kctl", return_value=FakeResult()), \
             patch.object(
                 orch, "_rogue_bash_deploy",
                 side_effect=lambda _c, _n, deploy, script: [deploy, script],
             ):
            commands = orch.multi_rogue_sync_attack_commands(
                "ctx", "ns", args, 800, "12.1.1.67", "12.1.1.66",
            )
        self.assertEqual(len(commands), 2)
        for _deploy, (_name, script) in commands:
            self.assertIn('--target "$EXT"', script)
            self.assertIn("PURE UL PRB", script)
            self.assertNotIn("12.1.1.67", script)
            self.assertNotIn("12.1.1.66", script)

    def test_multi_rogue_dl_targets_only_rogue_pool(self):
        args = orch.parse_args([
            "--condition", "attack", "--attack", "multi-rogue-dl",
            "--multi-rogue-count", "2",
        ])
        ips = {
            "oai-ue-rogue": "12.1.1.190",
            "oai-ue-rogue2": "12.1.1.191",
        }
        with patch.object(orch, "pdu_ip", side_effect=lambda _c, _n, app: ips[app]):
            cmd, targets = orch.multi_rogue_dl_command(
                "ctx", "ns", args, 800, "12.1.1.67", "12.1.1.66", "rid",
            )
        self.assertEqual(targets, ["12.1.1.190", "12.1.1.191"])
        self.assertIn("--mode multi-rogue-dl", cmd[-1])
        self.assertIn("PURE DL PRB", cmd[-1])

    def test_multi_rogue_sync_rejects_continuous_duty_cycle(self):
        with self.assertRaisesRegex(SystemExit, "bursty"):
            orch.parse_args([
                "--condition", "attack", "--attack", "multi-rogue-sync",
                "--setpoint-rate-hz", "10", "--multi-sync-burst-ms", "100",
            ])

    def test_integrity_requires_every_synchronized_rogue(self):
        log = "[ROGUE-SYNC] burst #1: 100 pkts in 25ms mode=open-loop"
        summary = {
            "mode": "multi-rogue-sync", "run_id": "rid",
            "rogue_count": 3, "rogue_count_requested": 4,
        }
        ok, info = orch.evaluate_attack_integrity(
            "multi-rogue-sync", None, 0, None, summary, {}, "rid", log,
            attack_procs=[object()], triggers_fired=0,
        )
        self.assertFalse(ok)
        self.assertFalse(info["identity_gate_pass"])
        summary["rogue_count"] = 4
        ok, info = orch.evaluate_attack_integrity(
            "multi-rogue-sync", None, 0, None, summary, {}, "rid", log,
            attack_procs=[object()], triggers_fired=0,
        )
        self.assertTrue(ok)
        self.assertEqual(info["threat_model"], "pure_ul_prb_contention_sync")

    def test_integrity_dl_requires_attacker_pool_targets(self):
        summary = {
            "run_id": "rid",
            "target_count": 2,
            "targets": ["12.1.1.190", "12.1.1.191"],
            "requested_aggregate_mbps": 40.0,
            "achieved_sender_mbps": 39.0,
        }
        ok, info = orch.evaluate_attack_integrity(
            "multi-rogue-dl", object(), 0, None, summary, {}, "rid", "",
        )
        self.assertTrue(ok, info)
        summary["targets"][1] = "12.1.1.67"
        ok, _ = orch.evaluate_attack_integrity(
            "multi-rogue-dl", object(), 0, None, summary, {}, "rid", "",
        )
        self.assertFalse(ok)


class AttackOnsetOptcTests(unittest.TestCase):
    def _args(self, duration, delay, *extra):
        return orch.parse_args([
            "--condition", "attack", "--attack", "multi-rogue",
            "--duration", str(duration), "--attack-start-after-s", str(delay),
            *extra,
        ])

    def test_600s_setpoint_stream_zero_delay_uses_optc(self):
        args = self._args(600, 0, "--warmup", "10")
        delay, burst = orch.resolve_attack_onset(args, 630)
        sched = orch.optc_schedule(args)
        self.assertAlmostEqual(delay, 174.3, places=6)
        self.assertEqual(burst, 510)
        self.assertAlmostEqual(delay, sched["onset_delay_s"], places=6)
        self.assertEqual(burst, sched["burst_s"])

    def test_zero_and_45_delay_agree_under_optc(self):
        self.assertEqual(
            orch.resolve_attack_onset(self._args(600, 0, "--warmup", "10"), 630),
            orch.resolve_attack_onset(self._args(600, 45, "--warmup", "10"), 630),
        )

    def test_waypoint_and_short_runs_unchanged(self):
        self.assertEqual(
            orch.resolve_attack_onset(self._args(600, 0, "--workload", "waypoint"), 630),
            (0.0, 630),
        )
        self.assertEqual(orch.resolve_attack_onset(self._args(180, 0), 210), (0.0, 210))
        self.assertEqual(orch.resolve_attack_onset(self._args(60, 45), 90)[0], 45.0)

    def test_guard_consistent_and_divergent(self):
        args = self._args(600, 0, "--warmup", "10")
        self.assertIsNone(orch.optc_attack_onset_mismatch(args))
        with patch.object(orch, "resolve_attack_onset", return_value=(0.0, 630)):
            msg = orch.optc_attack_onset_mismatch(args)
        self.assertIn("planned sleep=174.3s", msg)
        baseline = orch.parse_args(["--condition", "baseline", "--duration", "600"])
        self.assertIsNone(orch.optc_attack_onset_mismatch(baseline))


if __name__ == "__main__":
    unittest.main()

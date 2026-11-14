#!/usr/bin/env python3
##################################################################################################################################
# Module: test_results_layout.py (unit tests — results_layout)                                                                   #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Run directory and matrix path tests.                                                                              #
##################################################################################################################################

"""Tests for artifacts/02_results two-level campaign/run naming."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = SCRIPT_DIR / "lib"
for _p in (LIB_DIR, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import results_layout as rl  # noqa: E402


class TestResultsLayout(unittest.TestCase):
    def test_default_root_under_artifacts(self):
        # The results root is 02_results at the top of the artifact. Its parent is whatever the
        # artifact directory is called in the checkout, so only the layout below it is asserted.
        root = rl.default_results_root()
        self.assertEqual(root.name, "02_results")
        self.assertTrue((root.parent / "01_scripts").is_dir(), f"not an artifact root: {root.parent}")

    def test_baseline_campaign_name(self):
        args = SimpleNamespace(
            condition="baseline",
            topology="cross",
            gnb_prb=106,
            reservation=0,
            duration=600,
            companion_video_mbps=12.0,
            attack=None,
            multi_rogue_count=4,
        )
        name = rl.build_campaign_dirname(args, rogue_capacity=4)
        self.assertEqual(name, "106_PRB-CROSS-4_ROGUES-BASELINE-600S-12MPBS")

    def test_attack_n2_and_mitigation_campaign(self):
        args = SimpleNamespace(
            condition="attack",
            topology="cross",
            gnb_prb=106,
            reservation=60,
            duration=60,
            companion_video_mbps=12.0,
            attack="multi-rogue",
            multi_rogue_count=2,
            mitigation_rate_limit_a=False,
            mitigation_5qi=False,
            mitigation_aqm_ul=False,
        )
        name = rl.build_campaign_dirname(args, rogue_capacity=4)
        self.assertEqual(
            name,
            "106_PRB-CROSS-4_ROGUES-ATK_N2_AND_MITIGATION_PRB60-60S-12MPBS",
        )

    def test_new_multi_rogue_modes_have_distinct_tokens(self):
        common = dict(
            condition="attack", topology="cross", gnb_prb=106,
            reservation=0, duration=60, companion_video_mbps=0.0,
            multi_rogue_count=4, mitigation_rate_limit_a=False,
            mitigation_5qi=False, mitigation_aqm_ul=False,
        )
        dl = rl.build_campaign_dirname(
            SimpleNamespace(**common, attack="multi-rogue-dl"), rogue_capacity=4,
        )
        sync = rl.build_campaign_dirname(
            SimpleNamespace(**common, attack="multi-rogue-sync"), rogue_capacity=4,
        )
        self.assertIn("ATK_N4_DL", dl)
        self.assertIn("ATK_N4_UL_SYNC", sync)
        dl_sync = rl.build_campaign_dirname(
            SimpleNamespace(**common, attack="multi-rogue-dl-sync"), rogue_capacity=4,
        )
        self.assertIn("ATK_N4_DL_SYNC", dl_sync)

    def test_run_subdirname(self):
        self.assertEqual(rl.build_run_subdirname(1), "RUN01")
        self.assertEqual(rl.build_run_subdirname(3), "RUN03")
        self.assertEqual(rl.build_run_subdirname(1, recovery=True), "RUN01_rec")

    def test_zero_mbps_and_collision_suffix(self):
        args = SimpleNamespace(
            condition="baseline",
            topology="cross",
            gnb_prb=106,
            reservation=0,
            duration=600,
            companion_video_mbps=0.0,
            attack=None,
            multi_rogue_count=1,
        )
        name = rl.build_campaign_dirname(args, rogue_capacity=4)
        self.assertTrue(name.endswith("0MPBS"))
        with tempfile.TemporaryDirectory() as td:
            campaign = Path(td)
            (campaign / "RUN01").mkdir()
            got = rl.allocate_unique_dirname(campaign, "RUN01")
            self.assertEqual(got, "RUN01_a")
            (campaign / got).mkdir()
            got2 = rl.allocate_unique_dirname(campaign, "RUN01")
            self.assertEqual(got2, "RUN01_b")

    def test_allocate_unique_filename_keeps_extension(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            (parent / "analysis_params.json").write_text("{}", encoding="utf-8")
            got = rl.allocate_unique_filename(parent, "analysis_params.json")
            self.assertEqual(got, "analysis_params_a.json")
            (parent / got).write_text("{}", encoding="utf-8")
            got2 = rl.allocate_unique_filename(parent, "analysis_params.json")
            self.assertEqual(got2, "analysis_params_b.json")
            matrix = "matrix_baseline_cross_prb106_60s.json"
            (parent / matrix).write_text("{}", encoding="utf-8")
            got3 = rl.allocate_unique_filename(parent, matrix)
            self.assertEqual(got3, "matrix_baseline_cross_prb106_60s_a.json")

    def test_resolve_campaign_dir_paper_vs_legacy(self):
        args = SimpleNamespace(
            condition="baseline",
            topology="cross",
            gnb_prb=106,
            reservation=0,
            duration=600,
            companion_video_mbps=12.0,
            attack=None,
            multi_rogue_count=4,
        )
        root = Path("/tmp/results_root")
        paper = rl.resolve_campaign_dir(args, root, paper_layout=True, stamp_extra_rogues=3)
        self.assertEqual(paper.name, "106_PRB-CROSS-4_ROGUES-BASELINE-600S-12MPBS")
        self.assertEqual(paper.parent, root)
        legacy = rl.resolve_campaign_dir(args, root, paper_layout=False)
        self.assertEqual(legacy, root)

    def test_resolve_run_dirname_paper_is_runxx(self):
        args = SimpleNamespace(
            condition="baseline", topology="cross", gnb_prb=106, reservation=0,
            duration=600, companion_video_mbps=12.0, attack=None, multi_rogue_count=4,
        )
        with tempfile.TemporaryDirectory() as td:
            campaign = Path(td)
            r1 = rl.resolve_run_dirname(
                args, 1, campaign, paper_layout=True, legacy_slug="x",
            )
            self.assertEqual(r1, "RUN01")

    def test_scenario_rank2_tokens(self):
        args = SimpleNamespace(
            condition="attack",
            attack="multi-rogue",
            multi_rogue_count=1,
            reservation=0,
            mitigation_rate_limit_a=True,
            mitigation_5qi=True,
            mitigation_aqm_ul=False,
        )
        # MITIGATION_5QI (multi-PDU 5QI C2/video split) is out of scope on the pinned ORANSlice
        # 2024.w28 -- the nrUE has no multi-PDU parser (OAI MR !3769) -- so the flag is accepted but
        # contributes no token. See the module docstring of lib/results_layout.py.
        sc = rl.build_scenario_token(args)
        self.assertEqual(sc, "ATK_N1_AND_MITIGATION_RATELIMIT_A")

    def test_results_tag_names_rate_limit_control_without_dataplane_flag(self):
        baseline = SimpleNamespace(
            condition="baseline", topology="cross", gnb_prb=106,
            reservation=0, duration=60, companion_video_mbps=12.0,
            attack=None, multi_rogue_count=4,
            mitigation_rate_limit_a=False, mitigation_5qi=False,
            mitigation_aqm_ul=False, results_tag="RATELIMIT_A",
        )
        attack = SimpleNamespace(
            condition="attack", topology="cross", gnb_prb=106,
            reservation=0, duration=60, companion_video_mbps=12.0,
            attack="multi-rogue", multi_rogue_count=4,
            mitigation_rate_limit_a=True, mitigation_5qi=False,
            mitigation_aqm_ul=False, results_tag="RATELIMIT_A",
        )
        b = rl.build_campaign_dirname(baseline, rogue_capacity=4)
        a = rl.build_campaign_dirname(attack, rogue_capacity=4)
        self.assertEqual(
            b, "106_PRB-CROSS-4_ROGUES-BASELINE_AND_MITIGATION_RATELIMIT_A-60S-12MPBS",
        )
        self.assertEqual(
            a, "106_PRB-CROSS-4_ROGUES-ATK_N4_AND_MITIGATION_RATELIMIT_A-60S-12MPBS",
        )
        paper_b0 = rl.build_campaign_dirname(
            SimpleNamespace(
                condition="baseline", topology="cross", gnb_prb=106,
                reservation=0, duration=60, companion_video_mbps=12.0,
                attack=None, multi_rogue_count=4,
            ),
            rogue_capacity=4,
        )
        self.assertEqual(paper_b0, "106_PRB-CROSS-4_ROGUES-BASELINE-60S-12MPBS")
        self.assertNotEqual(b, paper_b0)


if __name__ == "__main__":
    unittest.main()

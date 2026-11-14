#!/usr/bin/env python3
##################################################################################################################################
# Module: kpi_budgets.py (Per-leg latency budgets, with explicit source and role)                                                #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Plan rule R6: the 1.0 s threshold cites Honeywell TestReport-263, which cites DO-377A; never "according to        #
#   DO-377A".                                                                                                                    #
##################################################################################################################################
"""Per-leg latency budgets, with explicit source and role.

Plan rule R6: the 1.0 s threshold cites Honeywell TestReport-263, which cites
DO-377A; never "according to DO-377A".
"""
from __future__ import annotations

from typing import Any

# threshold_s, source, role: pass_fail | pass_fail_return_only | informative
BUDGETS: list[dict[str, Any]] = [
    {
        "threshold_s": 0.040,
        "source": "TS 22.125 Tab. 7.2-1 direct stick steering",
        "role": "pass_fail",
        "id": "le_40ms_stick",
    },
    {
        "threshold_s": 0.140,
        "source": "TS 22.125 Tab. 7.2-2 video Non-VLOS",
        "role": "pass_fail_return_only",
        "id": "le_140ms_video",
        "note": "pass/fail only on the return leg; informative on the command leg",
    },
    {
        "threshold_s": 0.155,
        "source": 'AURA WP 2024 "communicate"',
        "role": "informative",
        "id": "le_155ms_aura_communicate",
    },
    {
        "threshold_s": 0.5,
        "source": "AURA WP 2024 status reporting",
        "role": "informative",
        "id": "le_0p5s_aura_status",
    },
    {
        "threshold_s": 1.0,
        "source": "Honeywell TestReport-263 citing DO-377A",
        "role": "pass_fail",
        "id": "le_1s_honeywell",
        "pass_if_frac_ge": 0.95,
        "note": "passes if the fraction <= 1.0 s is >= 95%",
    },
]

RELIABILITY_TARGET_STICK = 0.999  # TS 22.125 C2
RELIABILITY_TARGET_VIDEO = 0.9999  # TS 22.125 video Non-VLOS (return leg only)

GAP_THRESHOLDS_S = (
    {"threshold_s": 3.0, "source": "AURA / TET surface 3 s (secondary citation)"},
    {"threshold_s": 5.0, "source": "Honeywell / TET cruise 5 s (secondary citation)"},
)


def budgets_for_leg(leg: str) -> list[dict[str, Any]]:
    """Copy of the budgets with the effective role for the leg."""
    out = []
    for b in BUDGETS:
        row = dict(b)
        role = b["role"]
        if role == "pass_fail_return_only":
            row["effective_role"] = "pass_fail" if leg == "return" else "informative"
        else:
            row["effective_role"] = role
        out.append(row)
    return out

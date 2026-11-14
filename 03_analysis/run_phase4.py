#!/usr/bin/env python3
##################################################################################################################################
# Module: run_phase4.py (Run the whole analysis in dependency order)                                                             #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: build_manifest    which runs enter the analysis, and why the others do not legs_table        the same KPIs on     #
#   both legs, per condition and phase (the asymmetry) criteria_table    C1/C4 per leg and phase, plus K7 (FS_GCS) and K8        #
#   (QGC-style gaps) belief_by_phase   belief and telemetry AoI per phase  [the three steps below read its per-run CSV]          #
#   dl_ul_by_phase    per-phase uplink/downlink per role from the gNB MAC counters (the mechanism) frontiers         video, dose #
#   and bandwidth frontiers in the ATTACK window slack_recovery    capacity bracket per cell bandwidth, and DRAIN recovery       #
#   mitigation_table  the two defences against baseline and the undefended attack positive_control  the interruption control:    #
#   the same monitors do fire physics_truth     physical tracking from the SITL truth: start-up transient, along-track lag,      #
#   cross-track                                                                                                                  #
#                                                                                                                                #
# Read-only over 02_results/; all outputs go to 03_analysis/out/. Re-run after new campaign runs to refresh every table. Every   #
#   number in out/ is generated: nothing is typed by hand.                                                                       #
##################################################################################################################################
"""Run the whole analysis in dependency order.

  build_manifest    which runs enter the analysis, and why the others do not
  legs_table        the same KPIs on both legs, per condition and phase (the asymmetry)
  criteria_table    C1/C4 per leg and phase, plus K7 (FS_GCS) and K8 (QGC-style gaps)
  belief_by_phase   belief and telemetry AoI per phase  [the three steps below read its per-run CSV]
  dl_ul_by_phase    per-phase uplink/downlink per role from the gNB MAC counters (the mechanism)
  frontiers         video, dose and bandwidth frontiers in the ATTACK window
  slack_recovery    capacity bracket per cell bandwidth, and DRAIN recovery
  mitigation_table  the two defences against baseline and the undefended attack
  positive_control  the interruption control: the same monitors do fire
  physics_truth     physical tracking from the SITL truth: start-up transient, along-track lag, cross-track

Read-only over 02_results/; all outputs go to 03_analysis/out/. Re-run after new campaign runs to
refresh every table. Every number in out/ is generated: nothing is typed by hand.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Order matters: belief_by_phase writes the per-run CSV that frontiers, slack_recovery and
# mitigation_table read, so it has to run before them.
STEPS = ["build_manifest.py", "legs_table.py", "criteria_table.py", "belief_by_phase.py",
         "dl_ul_by_phase.py", "frontiers.py", "slack_recovery.py", "mitigation_table.py",
         "positive_control.py", "physics_truth.py"]


def main() -> int:
    for step in STEPS:
        print(f"== {step}", flush=True)
        rc = subprocess.call([sys.executable, str(HERE / step)], cwd=str(HERE))
        if rc != 0:
            print(f"== {step} failed (rc={rc}); stopping", flush=True)
            return rc
    print("== done: outputs in 03_analysis/out/", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

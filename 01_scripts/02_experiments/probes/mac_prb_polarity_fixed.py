#!/usr/bin/env python3
##################################################################################################################################
# Module: mac_prb_polarity_fixed.py (Alias retained for audit paths — polarity fix landed in mac_prb.py (2026-10))               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
##################################################################################################################################
"""Alias retained for audit paths — polarity fix landed in mac_prb.py (2026-10)."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    target = Path(__file__).with_name("mac_prb.py")
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")

##################################################################################################################################
# Module: __init__.py (unittest path bootstrap for 02_experiments)                                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Adds lib/ and 03_sitl/lib to sys.path for unittest discover.                                                      #
##################################################################################################################################

"""Path bootstrap so unittest finds lib/ and 03_sitl/lib from 02_experiments/."""
from __future__ import annotations

import sys
from pathlib import Path

EXP = Path(__file__).resolve().parents[1]
LIB = EXP / "lib"
SITL_LIB = EXP.parent / "03_sitl" / "lib"
PROBES = EXP / "probes"
for _p in (EXP, LIB, SITL_LIB, PROBES):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

#!/usr/bin/env python3
##################################################################################################################################
# Module: harq_retx_from_mac_prb.py (HARQ UL retx proxy from mac-prb-raw.log (ulsch_rounds a/b/c/d))                             #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: OAI nrMAC_stats line: ulsch_rounds R0/R1/R2/R3 where R0 = first TX successes, R1+ = HARQ retransmissions          #
#   (histogram).                                                                                                                 #
#                                                                                                                                #
# RLC AM txpdu_retx_* is NOT printed at info level; mark as not exported here.                                                   #
##################################################################################################################################
"""HARQ UL retx proxy from mac-prb-raw.log (ulsch_rounds a/b/c/d).

OAI nrMAC_stats line:
  ulsch_rounds R0/R1/R2/R3
where R0 = first TX successes, R1+ = HARQ retransmissions (histogram).

RLC AM txpdu_retx_* is NOT printed at info level; mark as not exported here.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

RE_UE = re.compile(
    r"UE\s+(?P<rnti>[0-9a-fA-F]+):\s+ulsch_rounds\s+"
    r"(?P<r0>\d+)/(?P<r1>\d+)/(?P<r2>\d+)/(?P<r3>\d+)"
)


def parse_file(path: Path) -> dict[str, dict]:
    """Last-seen cumulative counters per RNTI."""
    last: dict[str, dict] = {}
    with path.open(errors="replace") as f:
        for line in f:
            m = RE_UE.search(line)
            if not m:
                continue
            rnti = m.group("rnti").lower()
            r0, r1, r2, r3 = (int(m.group(k)) for k in ("r0", "r1", "r2", "r3"))
            last[rnti] = {
                "ulsch_r0": r0,
                "ulsch_r1": r1,
                "ulsch_r2": r2,
                "ulsch_r3": r3,
                "ulsch_retx_sum": r1 + r2 + r3,
                "ulsch_total": r0 + r1 + r2 + r3,
            }
    return last


def first_last(path: Path) -> dict[str, dict]:
    first: dict[str, dict] = {}
    last: dict[str, dict] = {}
    with path.open(errors="replace") as f:
        for line in f:
            m = RE_UE.search(line)
            if not m:
                continue
            rnti = m.group("rnti").lower()
            r0, r1, r2, r3 = (int(m.group(k)) for k in ("r0", "r1", "r2", "r3"))
            rec = {
                "ulsch_r0": r0,
                "ulsch_r1": r1,
                "ulsch_r2": r2,
                "ulsch_r3": r3,
                "ulsch_retx_sum": r1 + r2 + r3,
                "ulsch_total": r0 + r1 + r2 + r3,
            }
            first.setdefault(rnti, rec)
            last[rnti] = rec
    out = {}
    for rnti, L in last.items():
        F = first.get(rnti, {k: 0 for k in L})
        delta = {k: L[k] - F.get(k, 0) for k in L}
        tot = delta["ulsch_total"]
        out[rnti] = {
            "first": F,
            "last": L,
            "delta": delta,
            "retx_fraction": (delta["ulsch_retx_sum"] / tot) if tot > 0 else None,
        }
    return out


def map_roles(prb_path: Path | None, per_rnti: dict) -> dict:
    if not prb_path or not prb_path.is_file():
        return {"by_rnti": per_rnti}
    prb = json.loads(prb_path.read_text())
    role_map = prb.get("rnti_role_map") or {}
    # rnti_role_map may be {role: rnti} or {rnti: role}
    rnti_to_role = {}
    for k, v in role_map.items():
        if re.fullmatch(r"[0-9a-fA-F]+", str(k)):
            rnti_to_role[str(k).lower()] = v
        else:
            rnti_to_role[str(v).lower()] = k
    by_role = {}
    for rnti, data in per_rnti.items():
        role = rnti_to_role.get(rnti, "unknown")
        by_role[role] = {"rnti": rnti, **data}
    return {"by_rnti": per_rnti, "by_role": by_role, "rnti_role_map": rnti_to_role}


def main() -> None:
    ap = argparse.ArgumentParser(description="HARQ UL from mac-prb-raw")
    ap.add_argument("--mac-prb-raw", required=True)
    ap.add_argument("--prb-json", default="", help="optional for RNTI→role")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    path = Path(args.mac_prb_raw)
    per = first_last(path)
    mapped = map_roles(Path(args.prb_json) if args.prb_json else None, per)
    uav = (mapped.get("by_role") or {}).get("uav")
    report = {
        "schema": "flyblind_harq_ul_v1",
        "source": str(path),
        "harq_proxy": "ulsch_rounds R0/R1/R2/R3 from nrMAC_stats",
        "rlc_am_txpdu_retx": "not_exported_at_info_log_level",
        "uav": uav,
        **mapped,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(json.dumps({
        "uav_retx_fraction": (uav or {}).get("retx_fraction"),
        "uav_delta": (uav or {}).get("delta"),
        "n_rnti": len(per),
    }, indent=2))


if __name__ == "__main__":
    main()

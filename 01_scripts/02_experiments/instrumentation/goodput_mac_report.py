#!/usr/bin/env python3
##################################################################################################################################
# Module: goodput_mac_report.py (Video goodput (GCS/iperf feedback) beside MAC UAV UL)                                           #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Uses polarity-fixed prb.json field per_role.uav.ul_mbps. Goodput from companion_video.json end.sum: bytes * (1 -  #
#   lost_percent/100) * 8 / seconds Note: end.sum.bits_per_second is offered rate, not goodput.                                  #
##################################################################################################################################
"""Video goodput (GCS/iperf feedback) beside MAC UAV UL.

Uses polarity-fixed prb.json field per_role.uav.ul_mbps.
Goodput from companion_video.json end.sum:
  bytes * (1 - lost_percent/100) * 8 / seconds
Note: end.sum.bits_per_second is offered rate, not goodput.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def goodput_from_companion(cv: dict) -> dict:
    end = cv.get("end") or {}
    s = end.get("sum") or {}
    bps_offered = s.get("bits_per_second")
    lost_pct = s.get("lost_percent")
    seconds = s.get("seconds")
    bytes_sent = s.get("bytes")
    lost_packets = s.get("lost_packets")
    packets = s.get("packets")
    gp = None
    if bytes_sent is not None and lost_pct is not None and seconds and seconds > 0:
        gp = bytes_sent * (1.0 - float(lost_pct) / 100.0) * 8.0 / float(seconds) / 1e6
    return {
        "offered_mbps": (bps_offered / 1e6) if bps_offered else None,
        "goodput_mbps": gp,
        "lost_percent": lost_pct,
        "lost_packets": lost_packets,
        "packets": packets,
        "seconds": seconds,
        "bytes_sent": bytes_sent,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Video goodput + MAC UL")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    run = Path(args.run_dir)
    cv = json.loads((run / "companion_video.json").read_text()) if (run / "companion_video.json").is_file() else {}
    prb = json.loads((run / "prb.json").read_text()) if (run / "prb.json").is_file() else {}
    vid = goodput_from_companion(cv)
    mac = (prb.get("per_role") or {}).get("uav") or {}
    mac_ul = mac.get("ul_mbps")
    report = {
        "schema": "flyblind_goodput_mac_v1",
        "run_dir": str(run),
        "polarity": "prb.json RX=UL (fixed parser)",
        "mac_uav_ul_mbps": mac_ul,
        "video": vid,
        "mac_minus_goodput_mbps": (mac_ul - vid["goodput_mbps"])
        if mac_ul is not None and vid.get("goodput_mbps") is not None
        else None,
        "pred_80_over_mac_s": (80.0 / mac_ul) if mac_ul else None,
        "pred_80_over_goodput_s": (80.0 / vid["goodput_mbps"])
        if vid.get("goodput_mbps")
        else None,
    }
    out = Path(args.out) if args.out else run / "flyblind_goodput_mac.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

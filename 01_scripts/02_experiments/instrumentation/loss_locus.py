#!/usr/bin/env python3
##################################################################################################################################
# Module: loss_locus.py (Locate residual video loss vs RLC rejects + oaitun drops)                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Compares: companion_video lost_packets rlc sdu_rejected_sum  (from flyblind_rlc_rejects.json) qdisc_dropped_delta #
#   (from flyblind_ue_iface_summary.json)                                                                                        #
#                                                                                                                                #
# If rejects ≈ lost_packets → locus dominated by RLC admission. If rejects ≪ lost and qdisc delta ≈ 0 → residual between socket  #
#   and RLC (or accounting mismatch) — marked for investigation, not invented.                                                   #
##################################################################################################################################
"""Locate residual video loss vs RLC rejects + oaitun drops.

Compares:
  companion_video lost_packets
  rlc sdu_rejected_sum  (from flyblind_rlc_rejects.json)
  qdisc_dropped_delta   (from flyblind_ue_iface_summary.json)

If rejects ≈ lost_packets → locus dominated by RLC admission.
If rejects ≪ lost and qdisc delta ≈ 0 → residual between socket and RLC
  (or accounting mismatch) — marked for investigation, not invented.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(p: Path):
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def main() -> None:
    ap = argparse.ArgumentParser(description="Video loss locus")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    run = Path(args.run_dir)

    gp = load(run / "flyblind_goodput_mac.json") or {}
    rlc = load(run / "flyblind_rlc_rejects.json") or {}
    iface = load(run / "flyblind_ue_iface_summary.json") or {}
    # fallbacks: raw companion
    if not gp.get("video"):
        cv_path = run / "companion_video.json"
        if cv_path.is_file():
            end = (json.loads(cv_path.read_text()).get("end") or {}).get("sum") or {}
            gp = {"video": {
                "lost_packets": end.get("lost_packets"),
                "lost_percent": end.get("lost_percent"),
            }}

    lost = (gp.get("video") or {}).get("lost_packets")
    rejects = rlc.get("sdu_rejected_sum")
    qdelta = iface.get("qdisc_dropped_delta")

    ratio = None
    residual = None
    if lost is not None and rejects is not None:
        residual = int(lost) - int(rejects)
        ratio = rejects / lost if lost else None

    verdict = "indeterminate"
    if lost is None or rejects is None:
        verdict = "missing_inputs"
    elif ratio is not None and 0.85 <= ratio <= 1.15:
        verdict = "rlc_admission_explains_loss_order"
    elif qdelta is not None and qdelta > 0 and residual is not None and residual > 0.15 * lost:
        verdict = "qdisc_and_or_other_contribute"
    elif residual is not None and residual > 0.15 * (lost or 1) and (qdelta is None or qdelta == 0):
        verdict = "residual_not_qdisc_investigate_pre_rlc_or_accounting"
    elif residual is not None and residual < 0:
        verdict = "rejects_exceed_iperf_lost_accounting_mismatch"

    report = {
        "schema": "flyblind_loss_locus_v1",
        "lost_packets_iperf": lost,
        "sdu_rejected_sum": rejects,
        "rejects_over_lost": round(ratio, 4) if ratio is not None else None,
        "residual_lost_minus_rejects": residual,
        "qdisc_dropped_delta": qdelta,
        "verdict": verdict,
        "note": (
            "SDU rejected counts all DRB SDUs hitting full buffer; iperf counts video "
            "datagrams only. Order-of-magnitude match is the claim; exact equality not required."
        ),
    }
    out = Path(args.out) if args.out else run / "flyblind_loss_locus.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

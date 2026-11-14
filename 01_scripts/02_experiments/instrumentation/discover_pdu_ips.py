#!/usr/bin/env python3
##################################################################################################################################
# Module: discover_pdu_ips.py (Discover GCS/UAV PDU IPs on oaitun_ue1 (read-only kubectl))                                       #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Prints JSON to stdout; optional --out file. Used by run_sidecars / ACK / UL OWD so operators need not hard-code   #
#   12.1.1.x after restart.                                                                                                      #
##################################################################################################################################
"""Discover GCS/UAV PDU IPs on oaitun_ue1 (read-only kubectl).

Prints JSON to stdout; optional --out file. Used by run_sidecars / ACK / UL OWD
so operators need not hard-code 12.1.1.x after restart.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


def _addr(ctx: str, ns: str, deploy: str) -> str | None:
    r = subprocess.run(
        [
            "kubectl", "--context", ctx, "exec", "-n", ns,
            f"deploy/{deploy}", "-c", "nr-ue", "--",
            "ip", "-4", "-o", "addr", "show", "dev", "oaitun_ue1",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", r.stdout or "")
    return m.group(1) if m else None


def discover(ctx: str = "kind-tm-oranslice", ns: str = "ran") -> dict:
    gcs = _addr(ctx, ns, "oai-ue-gcs")
    uav = _addr(ctx, ns, "oai-ue-uav")
    out = {
        "schema": "flyblind_pdu_ips_v1",
        "context": ctx,
        "namespace": ns,
        "gcs_ip": gcs,
        "uav_ip": uav,
        "ok": bool(gcs and uav),
    }
    if not out["ok"]:
        out["error"] = "missing oaitun_ue1 PDU IP on gcs and/or uav"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover GCS/UAV PDU IPs")
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    data = discover(args.context, args.namespace)
    text = json.dumps(data, indent=2) + "\n"
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
    sys.stdout.write(text)
    if not data["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
##################################################################################################################################
# Module: smoke_kpi.py (Offline smoke test: synthetic JSONL + two instances (M3) on the bench)                                   #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
##################################################################################################################################
"""Offline smoke test: synthetic JSONL + two instances (M3) on the bench."""
from __future__ import annotations

import json
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "probes"))
sys.path.insert(0, str(ROOT / "instrumentation"))

from kpi_leg import assign_phase_half_open, compute_owd_kpis  # noqa: E402
from leg_owd_probe import run_bench  # noqa: E402
from phase_windows import assign_phase, build_windows  # noqa: E402


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    out = {
        "schema": "smoke_kpi_v1",
        "checks": {},
    }
    # Phase windows convention
    markers = {"mission_start": 0.0, "attack_onset": 10.0, "attack_end": 20.0, "run_end": 30.0}
    doc = build_windows(markers)
    assert assign_phase(10.0, doc["windows"]) == "ATTACK"
    assert assign_phase(9.999, doc["windows"]) == "PRE"
    assert assign_phase_half_open(20.0, doc["windows"]) == "DRAIN"
    out["checks"]["phase_half_open"] = {"pass": True}

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # M3: two instances on return-leg-like ports with different size/rate
        p1 = _free_port()
        p2 = _free_port()
        r1 = run_bench(
            leg="return", duration_s=0.35, rate_hz=25.0, payload_bytes=140,
            port=p1, out_dir=td_path / "a", warmup_s=0.0,
        )
        r2 = run_bench(
            leg="return", duration_s=0.35, rate_hz=10.0, payload_bytes=64,
            port=p2, out_dir=td_path / "b", warmup_s=0.0,
        )
        out["checks"]["m3_two_instances"] = {
            "pass": r1.get("loss_by_seq", {}).get("sent", 0) > 0
            and r2.get("loss_by_seq", {}).get("sent", 0) > 0,
            "a_sent": r1.get("loss_by_seq", {}).get("sent"),
            "b_sent": r2.get("loss_by_seq", {}).get("sent"),
        }
        # Synthetic JSONL → KPI
        samples = [
            {"seq": i, "owd_s": 0.02 + (0.5 if i % 17 == 0 else 0.0),
             "t_rx_mono_ns": 1_000_000_000 + i * 40_000_000}
            for i in range(300)
        ]
        kpi = compute_owd_kpis(
            leg="command",
            samples_rx=samples,
            tx_seqs=list(range(300)),
            rate_hz=25.0,
            duration_s=12.0,
            warmup_s=1.0,
            payload_bytes=24,
            port=48002,
            windows=doc["windows"],
            t0_mono_ns=1_000_000_000,
            denominator_where="smoke_tx",
        )
        out["checks"]["synthetic_kpi"] = {
            "pass": "delivery_rate_post_warmup" in kpi and "phases" in kpi,
            "has_le_1s_source": any(
                f.get("source", "").startswith("Honeywell")
                for f in kpi["phases"]["ALL"]["budget_fractions"]
            ),
            "availability_renamed": "availability" not in kpi,
            "rate_key": "delivery_rate_post_warmup" in kpi,
        }

    out["ok"] = all(
        (c is True) or (isinstance(c, dict) and c.get("pass"))
        for c in out["checks"].values()
    )
    print(json.dumps(out, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

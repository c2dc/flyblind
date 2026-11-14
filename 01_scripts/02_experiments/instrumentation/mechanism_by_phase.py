#!/usr/bin/env python3
##################################################################################################################################
# Module: mechanism_by_phase.py (Mechanism metrics by PRE/ATTACK/DRAIN)                                                          #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Reuses existing collectors; slices by phase_windows when timestamps allow.                                        #
#                                                                                                                                #
# Already existed (report, do not reinvent): * mac_prb.py → prb.json per_role (GCS/UAV/rogue via CU-UE-ID; RX=UL TX=DL) *        #
#   rlc_reject_scraper.py → sdu_rejected_sum (sum N, not line count) * ue_iface_sampler.py → qdisc_dropped_delta *               #
#   goodput_mac_report.py → video goodput beside MAC UAV UL * loss_locus.py → residual loss vs RLC + qdisc                       #
#                                                                                                                                #
# This module: load those artifacts + phase windows, emit per-phase view where clock alignment is possible; otherwise ALL + NOT  #
#   VERIFIED.                                                                                                                    #
##################################################################################################################################
"""Mechanism metrics by PRE/ATTACK/DRAIN.

Reuses existing collectors; slices by phase_windows when timestamps allow.

Already existed (report, do not reinvent):
  * mac_prb.py → prb.json per_role (GCS/UAV/rogue via CU-UE-ID; RX=UL TX=DL)
  * rlc_reject_scraper.py → sdu_rejected_sum (sum N, not line count)
  * ue_iface_sampler.py → qdisc_dropped_delta
  * goodput_mac_report.py → video goodput beside MAC UAV UL
  * loss_locus.py → residual loss vs RLC + qdisc

This module: load those artifacts + phase windows, emit per-phase view where
clock alignment is possible; otherwise ALL + NOT VERIFIED.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from phase_windows import (  # noqa: E402
    assign_phase,
    build_windows,
    load_markers,
    load_t0_unix_ns,
)

# Cellular polarity in prb.json (fixed parser): RX = UL (air), TX = DL (air).
# Aviation legs for C2: UAV UL ≈ return leg; GCS→UAV DL ≈ command leg.
POLARITY_NOTE = (
    "prb.json: RX=UL (cellular uplink), TX=DL (cellular downlink); "
    "roles via CU-UE-ID (rogue… → gcs → uav attach order, with UL-heuristic repair)."
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _slice_by_unix(
    rows: list[dict[str, Any]],
    *,
    t_key: str,
    t0_unix_ns: Optional[float],
    windows: dict,
    phase: str,
) -> list[dict[str, Any]]:
    if t0_unix_ns is None or phase not in windows:
        return []
    out = []
    for r in rows:
        t_unix = r.get(t_key)
        if t_unix is None:
            continue
        t_rel = (float(t_unix) - float(t0_unix_ns)) / 1e9
        if assign_phase(t_rel, windows) == phase:
            out.append({**r, "t_rel_s": t_rel})
    return out


def mac_block(prb: dict[str, Any]) -> dict[str, Any]:
    per_role = prb.get("per_role") or {}
    roles_out = {}
    for role, entry in per_role.items():
        if not isinstance(entry, dict):
            continue
        roles_out[role] = {
            "rnti": entry.get("rnti"),
            "cu_ue_id": entry.get("cu_ue_id") or entry.get("cu_ue"),
            "ul_mbps": entry.get("ul_mbps"),  # RX = UL
            "dl_mbps": entry.get("dl_mbps"),  # TX = DL
            "ul_rounds_delta": entry.get("ul_rounds_delta"),
            "note_polarity": "ul_mbps from RX, dl_mbps from TX",
        }
    return {
        "source": "prb.json / mac_prb.py",
        "polarity": POLARITY_NOTE,
        "per_role": roles_out,
        "phase_slice": "ALL_run_or_capture_window",
        "NOT_VERIFIED": (
            []
            if roles_out
            else ["prb.json missing or empty per_role"]
        ),
    }


def rlc_block(
    rlc_summary: dict[str, Any],
    rlc_events: list[dict[str, Any]],
    *,
    windows: dict,
    t0_unix_ns: Optional[float],
) -> dict[str, Any]:
    by_phase: dict[str, Any] = {"ALL": {
        "sdu_rejected_sum": rlc_summary.get("sdu_rejected_sum"),
        "log_lines": rlc_summary.get("log_lines"),
        "source": "flyblind_rlc_rejects.json (sum N, not line count)",
    }}
    not_verified_items = []
    # Events carry softmodem t_s (not unix) — phase slice only if we invent mapping.
    if rlc_events and any(e.get("t_unix_ns") for e in rlc_events):
        for ph in ("PRE", "ATTACK", "DRAIN"):
            if ph not in windows:
                continue
            sliced = _slice_by_unix(
                rlc_events, t_key="t_unix_ns", t0_unix_ns=t0_unix_ns, windows=windows, phase=ph
            )
            by_phase[ph] = {
                "sdu_rejected_sum": int(sum(int(e.get("n") or 0) for e in sliced)),
                "log_lines": len(sliced),
            }
    else:
        not_verified_items.append(
            "RLC events lack t_unix_ns aligned to T0; per-phase sum NOT VERIFIED "
            "(ALL uses scraper summary)"
        )
    return {"by_phase": by_phase, "NOT_VERIFIED": not_verified_items}


def qdisc_block(
    iface_summary: dict[str, Any],
    iface_rows: list[dict[str, Any]],
    *,
    windows: dict,
    t0_unix_ns: Optional[float],
) -> dict[str, Any]:
    by_phase: dict[str, Any] = {
        "ALL": {
            "qdisc_dropped_delta": iface_summary.get("qdisc_dropped_delta"),
            "qdisc_dropped_first": iface_summary.get("qdisc_dropped_first"),
            "qdisc_dropped_last": iface_summary.get("qdisc_dropped_last"),
            "source": "flyblind_ue_iface_summary.json",
        }
    }
    not_verified_items = []
    if iface_rows and t0_unix_ns is not None:
        for ph in ("PRE", "ATTACK", "DRAIN"):
            if ph not in windows:
                continue
            sliced = _slice_by_unix(
                iface_rows, t_key="t_unix_ns", t0_unix_ns=t0_unix_ns, windows=windows, phase=ph
            )
            drops = [s["qdisc_dropped"] for s in sliced if s.get("qdisc_dropped") is not None]
            by_phase[ph] = {
                "n_samples": len(sliced),
                "qdisc_dropped_first": drops[0] if drops else None,
                "qdisc_dropped_last": drops[-1] if drops else None,
                "qdisc_dropped_delta": (drops[-1] - drops[0]) if len(drops) >= 2 else None,
            }
    else:
        not_verified_items.append(
            "qdisc per-phase requires flyblind_ue_iface.jsonl + T0_unix_ns; "
            "ALL delta from summary only"
        )
    return {"by_phase": by_phase, "NOT_VERIFIED": not_verified_items}


def goodput_block(gp: dict[str, Any], prb: dict[str, Any]) -> dict[str, Any]:
    vid = gp.get("video") or {}
    if not vid and gp:
        # goodput_mac_report shape
        pass
    mac_ul = gp.get("mac_uav_ul_mbps")
    if mac_ul is None:
        mac_ul = ((prb.get("per_role") or {}).get("uav") or {}).get("ul_mbps")
    return {
        "source": "flyblind_goodput_mac.json / companion_video.json",
        "video_goodput_mbps": vid.get("goodput_mbps") or gp.get("video", {}).get("goodput_mbps"),
        "video_offered_mbps": vid.get("offered_mbps"),
        "mac_uav_ul_mbps": mac_ul,
        "mac_minus_goodput_mbps": gp.get("mac_minus_goodput_mbps"),
        "phase_slice": "ALL_run (iperf end.sum is whole-session)",
        "NOT_VERIFIED": (
            ["video goodput not phase-sliced (companion end.sum is aggregate)"]
        ),
    }


def analyze_run(run_dir: Path) -> dict[str, Any]:
    markers = load_markers(run_dir)
    pw_path = run_dir / "flyblind_phase_windows.json"
    if pw_path.is_file():
        windows = (_load_json(pw_path).get("windows") or {})
    else:
        windows = (build_windows(markers).get("windows") or {})

    t0_raw = load_t0_unix_ns(run_dir)
    t0_unix = float(t0_raw) if t0_raw is not None else None

    prb = _load_json(run_dir / "prb.json")
    rlc = _load_json(run_dir / "flyblind_rlc_rejects.json")
    rlc_events = _load_jsonl(run_dir / "flyblind_rlc_rejects.jsonl")
    iface = _load_json(run_dir / "flyblind_ue_iface_summary.json")
    iface_rows = _load_jsonl(run_dir / "flyblind_ue_iface.jsonl")
    gp = _load_json(run_dir / "flyblind_goodput_mac.json")
    if not gp and (run_dir / "companion_video.json").is_file():
        # Lightweight inline goodput without re-running CLI
        from goodput_mac_report import goodput_from_companion  # type: ignore
        cv = _load_json(run_dir / "companion_video.json")
        gp = {
            "video": goodput_from_companion(cv),
            "mac_uav_ul_mbps": ((prb.get("per_role") or {}).get("uav") or {}).get("ul_mbps"),
        }

    mac = mac_block(prb)
    rlc_b = rlc_block(rlc, rlc_events, windows=windows, t0_unix_ns=t0_unix)
    qdisc = qdisc_block(iface, iface_rows, windows=windows, t0_unix_ns=t0_unix)
    video = goodput_block(gp, prb)

    return {
        "schema": "flyblind_mechanism_by_phase_v1",
        "run_dir": str(run_dir),
        "windows": windows,
        "t0_unix_ns": t0_unix,
        "mac_by_role": mac,
        "rlc_rejections": rlc_b,
        "qdisc_drops": qdisc,
        "goodput_video": video,
        "existing_not_reimplemented": [
            "mac_prb.py / prb.json",
            "rlc_reject_scraper.py",
            "ue_iface_sampler.py",
            "goodput_mac_report.py",
            "loss_locus.py",
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Mechanism metrics by phase window")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    run = Path(args.run_dir)
    data = analyze_run(run)
    out = Path(args.out) if args.out else run / "flyblind_mechanism_by_phase.json"
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: data[k] for k in data if k != "windows"}, indent=2))


if __name__ == "__main__":
    main()

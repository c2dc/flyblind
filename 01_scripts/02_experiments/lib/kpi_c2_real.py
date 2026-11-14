#!/usr/bin/env python3
##################################################################################################################################
# Module: kpi_c2_real.py (Applies kpi_leg to real C2 traffic, with metric nature owd and M9)                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Command leg: network_arrival_delay of the setpoints (denominator = application send). Return leg:                 #
#   telemetry_arrival_delay / delta (denominator = egress at the forwarder/UAV CSV).                                             #
##################################################################################################################################
"""Applies kpi_leg to real C2 traffic, with metric nature owd and M9.

Command leg: network_arrival_delay of the setpoints (denominator = application send).
Return leg: telemetry_arrival_delay / delta (denominator = egress at the forwarder/UAV CSV).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any, Optional

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))

from kpi_leg import (  # noqa: E402
    NATURE_OWD,
    LEG_COMMAND,
    LEG_RETURN,
    compute_owd_kpis,
    setpoint_counts_m1,
)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load_setpoint_owd_samples(setpoints_csv: Path) -> tuple[list[dict], list[int], float]:
    """OWD = network_arrival_delay_ms. Seq = logical_setpoint_id."""
    rows = []
    tx_ids = []
    max_owd_s = 0.0
    with setpoints_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sid = _i(row.get("logical_setpoint_id"))
            t_send = _i(row.get("t_send_gcs_ns"))
            if sid is None or t_send is None:
                continue
            tx_ids.append(sid)
            delay_ms = _f(row.get("network_arrival_delay_ms"))
            t_arr = _i(row.get("t_arrival_uav_proxy_ns"))
            if delay_ms is None or t_arr is None:
                continue
            owd_s = delay_ms / 1e3
            max_owd_s = max(max_owd_s, owd_s)
            rows.append({
                "seq": sid,
                "owd_s": owd_s,
                "t_tx_mono_ns": t_send,
                "t_rx_mono_ns": t_arr,
                "leg": LEG_COMMAND,
                "metric_nature": NATURE_OWD,
            })
    return rows, tx_ids, max_owd_s


def load_telemetry_owd_samples(
    gcs_csv: Path,
    uav_csv: Path,
) -> tuple[list[dict], list[int], dict[str, Any]]:
    """Match GCS arrivals to UAV egress by (system_id, time_boot_ms)."""
    uav_by_key: dict[tuple[int, int], dict] = {}
    uav_ids: list[int] = []
    with uav_csv.open(encoding="utf-8") as f:
        for idx, row in enumerate(csv.DictReader(f)):
            boot = _i(row.get("time_boot_ms"))
            sysid = _i(row.get("mavlink_system_id")) or 1
            t_uav = _i(row.get("t_mono_ns"))
            if boot is None or t_uav is None:
                continue
            key = (sysid, boot)
            uav_by_key[key] = row
            uav_ids.append(idx)

    samples = []
    tx_seqs = []
    max_owd = 0.0
    matched = 0
    with gcs_csv.open(encoding="utf-8") as f:
        for idx, row in enumerate(csv.DictReader(f)):
            boot = _i(row.get("time_boot_ms"))
            sysid = _i(row.get("mavlink_system_id")) or 1
            t_gcs = _i(row.get("t_mono_ns"))
            if boot is None or t_gcs is None:
                continue
            key = (sysid, boot)
            uav = uav_by_key.get(key)
            if uav is None:
                continue
            t_uav = _i(uav.get("t_mono_ns"))
            if t_uav is None:
                continue
            owd_s = (t_gcs - t_uav) / 1e9
            max_owd = max(max_owd, owd_s)
            seq = boot  # stable id within system
            tx_seqs.append(seq)
            samples.append({
                "seq": seq,
                "owd_s": owd_s,
                "t_tx_mono_ns": t_uav,
                "t_rx_mono_ns": t_gcs,
                "leg": LEG_RETURN,
                "metric_nature": NATURE_OWD,
            })
            matched += 1

    meta = {
        "ned_uav_egress_rows": len(uav_ids),
        "ned_matched_gcs": matched,
        "denominator_where_measured": (
            "UAV egress in c2_telemetry_uav.csv (post-SITL / forwarder), "
            "not the SITL internal generation"
        ),
    }
    return samples, tx_seqs, meta


def build_c2_kpi_pair(
    run_dir: Path,
    windows: Optional[dict] = None,
    t0_mono_ns: Optional[int] = None,
    rate_hz_command: float = 10.0,
    duration_s: float = 0.0,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    out: dict[str, Any] = {
        "schema": "kpi_c2_real_v1",
        "note_analogous_not_identical": (
            "network_arrival_delay (command) and telemetry_arrival_delay/delta (return) "
            "are analogous (OWD at delivery), not identical."
        ),
    }
    sp_path = run_dir / "setpoints.csv"
    if sp_path.is_file():
        samples, tx_ids, max_owd = load_setpoint_owd_samples(sp_path)
        # duration estimate
        if duration_s <= 0 and samples:
            duration_s = max(1.0, (samples[-1]["t_rx_mono_ns"] - samples[0]["t_tx_mono_ns"]) / 1e9)
        kpi_cmd = compute_owd_kpis(
            leg=LEG_COMMAND,
            samples_rx=samples,
            tx_seqs=tx_ids,
            rate_hz=rate_hz_command,
            duration_s=duration_s or 1.0,
            packets_sent=len(tx_ids),
            metric_nature=NATURE_OWD,
            windows=windows,
            t0_mono_ns=t0_mono_ns,
            denominator_where="GCS application setpoints.csv t_send_gcs_ns",
        )
        kpi_cmd["max_owd_s_observed"] = max_owd
        out["command"] = kpi_cmd

        # 1.3c M1 counts
        class _SP:
            pass

        sps = []
        arrivals = {}
        with sp_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sid = _i(row.get("logical_setpoint_id"))
                t_send = _i(row.get("t_send_gcs_ns"))
                t_arr = _i(row.get("t_arrival_uav_proxy_ns"))
                if sid is None or t_send is None:
                    continue
                sp = _SP()
                sp.logical_setpoint_id = sid
                sp.t_send_gcs_ns = t_send
                sp.t_arrival_uav_proxy_ns = t_arr
                sps.append(sp)
                if t_arr is not None:
                    arrivals[sid] = t_arr
        if sps and windows and t0_mono_ns is not None:
            t_last = max(sp.t_send_gcs_ns for sp in sps)
            out["setpoint_counts_1_3c"] = setpoint_counts_m1(
                sps,
                arrivals,
                t_last_successful_send_ns=t_last,
                max_owd_s_observed=max_owd,
                windows=windows,
                t0_mono_ns=t0_mono_ns,
                get_send_ns=lambda s: s.t_send_gcs_ns,
                get_id=lambda s: s.logical_setpoint_id,
                get_arrival_ns=lambda s: s.t_arrival_uav_proxy_ns,
            )

    gcs = run_dir / "telemetry_downlink_gcs.csv"
    uav = run_dir / "c2_telemetry_uav.csv"
    if gcs.is_file() and uav.is_file():
        samples, tx_seqs, tel_meta = load_telemetry_owd_samples(gcs, uav)
        dur = duration_s
        if dur <= 0 and samples:
            dur = max(1.0, (samples[-1]["t_rx_mono_ns"] - samples[0]["t_tx_mono_ns"]) / 1e9)
        kpi_ret = compute_owd_kpis(
            leg=LEG_RETURN,
            samples_rx=samples,
            tx_seqs=tx_seqs,
            rate_hz=20.0,
            duration_s=dur or 1.0,
            packets_sent=tel_meta["ned_uav_egress_rows"],
            metric_nature=NATURE_OWD,
            windows=windows,
            t0_mono_ns=t0_mono_ns,
            denominator_where=tel_meta["denominator_where_measured"],
        )
        kpi_ret["m9_meta"] = tel_meta
        out["return"] = kpi_ret
    return out


def verify_m9_sitl_vs_egress(run_dir: Path) -> dict[str, Any]:
    """M9: does the SITL generate more LOCAL_POSITION_NED than the egress CSV?

    Looks for evidence in the run artifacts. Without an internal SITL log of
    generated NED messages, the typical result is NOT VERIFIED.
    """
    run_dir = Path(run_dir)
    uav = run_dir / "c2_telemetry_uav.csv"
    gcs = run_dir / "telemetry_downlink_gcs.csv"
    out: dict[str, Any] = {
        "run_dir": str(run_dir),
        "question": "Does the SITL generate more LOCAL_POSITION_NED than c2_telemetry_uav.csv records?",
        "result": "NOT VERIFIED",
        "evidence": [],
    }
    if not uav.is_file():
        out["evidence"].append("c2_telemetry_uav.csv missing")
        return out
    n_uav = sum(1 for _ in uav.open(encoding="utf-8")) - 1
    out["evidence"].append(f"c2_telemetry_uav.csv rows≈{n_uav}")
    # Look for a SITL / mission log with a count of emitted NED messages
    candidates = list(run_dir.glob("*.log")) + list(run_dir.glob("*.txt"))
    found_sitl_count = None
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key in ("LOCAL_POSITION_NED sent", "ned_generated", "SITL NED count"):
            if key.lower() in text.lower():
                out["evidence"].append(f"mention '{key}' in {path.name}")
                found_sitl_count = "mention_without_parse"
    if found_sitl_count is None:
        out["evidence"].append(
            "No counter of SITL-generated NED messages in the run logs; "
            "only the egress captured at the forwarder/proxy is available."
        )
        out["result"] = "NOT VERIFIED"
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--check-m9-only", action="store_true")
    args = ap.parse_args()
    run = Path(args.run_dir)
    if args.check_m9_only:
        data = verify_m9_sitl_vs_egress(run)
    else:
        windows = None
        pw = run / "flyblind_phase_windows.json"
        t0 = None
        if pw.is_file():
            doc = json.loads(pw.read_text(encoding="utf-8"))
            windows = doc.get("windows")
        markers = run / "event_markers.csv"
        # T0 not always available as absolute mono; leave None → ALL only
        data = build_c2_kpi_pair(run, windows=windows, t0_mono_ns=t0)
        data["m9_sitl_vs_egress"] = verify_m9_sitl_vs_egress(run)
    Path(args.out).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "out": args.out}, indent=2))


if __name__ == "__main__":
    main()

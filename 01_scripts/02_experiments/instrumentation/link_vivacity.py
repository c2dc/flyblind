#!/usr/bin/env python3
##################################################################################################################################
# Module: link_vivacity.py (K7 (FS_GCS / command-leg vivacity) and K8 (QGC-style return-leg vivacity))                           #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: K7 (command leg): * FS_GCS PRIMARY: SITL msgs on UAV side of proxy (sitl_to_gcs HEARTBEAT mode + STATUSTEXT in    #
#   proxy_link.csv). Local TCP, not delayed by 5G return. * FS_GCS SECONDARY: GCS mission_summary failsafe + statustext_gcs.csv  #
#   * Max interarrival of HEARTBEAT / MANUAL_CONTROL / RC_CHANNELS_OVERRIDE arriving at the UAV (proxy_link.csv, direction       #
#   gcs_to_sitl)                                                                                                                 #
#                                                                                                                                #
# K8 (return leg, QGC VehicleLinkManager style): * Interarrival of ALL UAV→GCS MAVLink messages except RADIO_STATUS * Tick every #
#   1 s; declare loss when > 3.5 s without any message * Report max gap, loss declarations, % time with gap > 3.5 s              #
#                                                                                                                                #
# Windows: always via phase_windows / assign_phase ([lo, hi)). Clock: event_markers and proxy_link share CLOCK_MONOTONIC (t_rel  #
#   = (t_arrival_ns - T0_mono_ns)/1e9 when time_namespace_match).                                                                #
##################################################################################################################################
"""K7 (FS_GCS / command-leg vivacity) and K8 (QGC-style return-leg vivacity).

K7 (command leg):
  * FS_GCS PRIMARY: SITL msgs on UAV side of proxy (sitl_to_gcs HEARTBEAT
    mode + STATUSTEXT in proxy_link.csv). Local TCP, not delayed by 5G return.
  * FS_GCS SECONDARY: GCS mission_summary failsafe + statustext_gcs.csv
  * Max interarrival of HEARTBEAT / MANUAL_CONTROL / RC_CHANNELS_OVERRIDE
    arriving at the UAV (proxy_link.csv, direction gcs_to_sitl)

K8 (return leg, QGC VehicleLinkManager style):
  * Interarrival of ALL UAV→GCS MAVLink messages except RADIO_STATUS
  * Tick every 1 s; declare loss when > 3.5 s without any message
  * Report max gap, loss declarations, % time with gap > 3.5 s

Windows: always via phase_windows / assign_phase ([lo, hi)).
Clock: event_markers and proxy_link share CLOCK_MONOTONIC
(t_rel = (t_arrival_ns - T0_mono_ns)/1e9 when time_namespace_match).
"""
from __future__ import annotations

import argparse
import csv
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
    load_t0_mono_ns,
    load_t0_unix_ns,
)

QGC_LOSS_S = 3.5
QGC_TICK_S = 1.0
COMMAND_LINK_TYPES = frozenset({
    "HEARTBEAT",
    "MANUAL_CONTROL",
    "RC_CHANNELS_OVERRIDE",
})
# ArduCopter custom_mode numbers commonly used as FS_GCS reaction.
FS_GCS_CUSTOM_MODES = {
    6: "RTL",
    9: "LAND",
    21: "SMART_RTL",
}
FS_GCS_MODE_NAMES = frozenset({"RTL", "LAND", "SMART_RTL", "AUTO_RTL"})


def _load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def max_gap_s(t_rel: list[float]) -> Optional[float]:
    if len(t_rel) < 2:
        return None
    s = sorted(t_rel)
    return max(s[i] - s[i - 1] for i in range(1, len(s)))


def gaps_list(t_rel: list[float]) -> list[float]:
    if len(t_rel) < 2:
        return []
    s = sorted(t_rel)
    return [s[i] - s[i - 1] for i in range(1, len(s))]


def qgc_style_link_loss(
    arrivals_t_rel: list[float],
    *,
    t_lo: float,
    t_hi: float,
    loss_s: float = QGC_LOSS_S,
    tick_s: float = QGC_TICK_S,
) -> dict[str, Any]:
    """Emulate VehicleLinkManager: check every tick_s; loss if idle > loss_s.

    arrivals_t_rel must be sorted ascending. Window is [t_lo, t_hi).
    """
    if t_hi <= t_lo:
        return {
            "ok": False,
            "error": "empty_window",
            "max_gap_s": None,
            "loss_declarations": 0,
            "pct_time_gap_gt_loss": None,
            "window_duration_s": 0.0,
            "pass": None,
        }
    all_sorted = sorted(arrivals_t_rel)
    arr = [t for t in all_sorted if t_lo <= t < t_hi]
    pre = [t for t in all_sorted if t < t_lo]
    last_seen = pre[-1] if pre else t_lo

    loss_decls = 0
    in_loss = False
    loss_time_s = 0.0
    tick = t_lo
    i = 0
    n = len(arr)
    while tick < t_hi:
        while i < n and arr[i] <= tick:
            last_seen = arr[i]
            i += 1
            in_loss = False
        idle = tick - last_seen
        if idle > loss_s:
            if not in_loss:
                loss_decls += 1
                in_loss = True
            next_tick = min(tick + tick_s, t_hi)
            loss_time_s += next_tick - tick
        tick += tick_s

    gap_candidates: list[float] = []
    if len(arr) >= 2:
        gap_candidates.extend(gaps_list(arr))
    if arr:
        start_anchor = pre[-1] if pre else t_lo
        gap_candidates.append(arr[0] - start_anchor)
        gap_candidates.append(t_hi - arr[-1])
    else:
        gap_candidates.append(t_hi - t_lo)

    max_gap = max(gap_candidates) if gap_candidates else None
    dur = t_hi - t_lo
    pct = (100.0 * loss_time_s / dur) if dur > 0 else None
    pass_k8 = (max_gap is not None and max_gap <= loss_s and loss_decls == 0)

    return {
        "ok": True,
        "schema": "k8_qgc_style_v1",
        "loss_threshold_s": loss_s,
        "tick_s": tick_s,
        "n_arrivals": len(arr),
        "max_gap_s": round(max_gap, 6) if max_gap is not None else None,
        "loss_declarations": loss_decls,
        "loss_time_s": round(loss_time_s, 6),
        "pct_time_gap_gt_loss": round(pct, 6) if pct is not None else None,
        "window_duration_s": round(dur, 6),
        "pass": pass_k8,
        "note": (
            "Emulates QGC VehicleLinkManager: 1 s tick, link loss when "
            "> 3.5 s without any vehicle MAVLink (RADIO_STATUS excluded)."
        ),
    }


def _row_t_rel(
    row: dict[str, str],
    *,
    t0_mono_ns: Optional[float] = None,
    t0_unix_ns: Optional[float] = None,
) -> Optional[float]:
    t_rel = _f(row, "t_rel_s")
    if t_rel is not None:
        return t_rel
    mono = _f(row, "t_arrival_ns")
    if mono is not None and t0_mono_ns is not None:
        return (float(mono) - float(t0_mono_ns)) / 1e9
    unix = _f(row, "t_unix_ns")
    if unix is not None and t0_unix_ns is not None:
        return (float(unix) - float(t0_unix_ns)) / 1e9
    return None


def _command_link_events(
    proxy_link_rows: list[dict[str, str]],
    *,
    t0_mono_ns: Optional[float] = None,
    t0_unix_ns: Optional[float] = None,
) -> list[dict[str, Any]]:
    """GCS→UAV link messages that reset ArduPilot FS_GCS."""
    out: list[dict[str, Any]] = []
    for r in proxy_link_rows:
        if (r.get("direction") or "") != "gcs_to_sitl":
            continue
        mtype = r.get("mavlink_msg_type") or ""
        if mtype not in COMMAND_LINK_TYPES:
            continue
        t_rel = _row_t_rel(r, t0_mono_ns=t0_mono_ns, t0_unix_ns=t0_unix_ns)
        mono = _f(r, "t_arrival_ns")
        unix = _f(r, "t_unix_ns")
        if t_rel is None and mono is None and unix is None:
            continue
        out.append({
            "mavlink_msg_type": mtype,
            "t_arrival_ns": mono,
            "t_unix_ns": unix,
            "t_rel_s": t_rel,
        })
    return out


def _fs_gcs_from_uav_proxy(
    proxy_link_rows: list[dict[str, str]],
    *,
    windows: dict[str, dict],
    phase: Optional[str],
    t0_mono_ns: Optional[float] = None,
    t0_unix_ns: Optional[float] = None,
) -> dict[str, Any]:
    """Primary FS_GCS: SITL→proxy (sitl_to_gcs) HEARTBEAT mode + STATUSTEXT."""
    mode_transitions: list[dict[str, Any]] = []
    status_hits: list[dict[str, Any]] = []
    last_mode: Optional[str] = None
    triggered = False
    trigger_mode: Optional[str] = None
    trigger_t: Optional[float] = None
    reason: Optional[str] = None

    for r in proxy_link_rows:
        if (r.get("direction") or "") != "sitl_to_gcs":
            continue
        t_rel = _row_t_rel(r, t0_mono_ns=t0_mono_ns, t0_unix_ns=t0_unix_ns)
        if phase is not None and windows and t_rel is not None:
            if assign_phase(t_rel, windows) != phase:
                continue
        elif phase is not None and windows and t_rel is None:
            continue

        mtype = r.get("mavlink_msg_type") or ""
        if mtype == "STATUSTEXT":
            text = (r.get("text") or "")
            low = text.lower()
            if "failsafe" in low or "fs_gcs" in low or "gcs failsafe" in low:
                status_hits.append({"t_rel_s": t_rel, "text": text})
                if not triggered:
                    triggered = True
                    trigger_t = t_rel
                    reason = "statustext"
        elif mtype == "HEARTBEAT":
            mode = None
            cm = r.get("custom_mode")
            try:
                cm_i = int(float(cm)) if cm not in (None, "") else None
            except (TypeError, ValueError):
                cm_i = None
            if cm_i in FS_GCS_CUSTOM_MODES:
                mode = FS_GCS_CUSTOM_MODES[cm_i]
            if mode is None:
                continue
            if last_mode is not None and mode != last_mode:
                mode_transitions.append({
                    "t_rel_s": t_rel,
                    "from": last_mode,
                    "to": mode,
                })
            if mode in FS_GCS_MODE_NAMES and mode != "LAND":
                # RTL / SMART_RTL: strong FS_GCS signal at UAV.
                if not triggered:
                    triggered = True
                    trigger_mode = mode
                    trigger_t = t_rel
                    reason = "heartbeat_mode"
            elif mode == "LAND":
                # LAND alone is ambiguous (mission end). Require STATUSTEXT.
                if status_hits and not triggered:
                    triggered = True
                    trigger_mode = mode
                    trigger_t = t_rel
                    reason = "land_plus_statustext"
            last_mode = mode

    return {
        "source": "proxy_link sitl_to_gcs (UAV-local TCP SITL↔proxy)",
        "triggered": triggered,
        "mode": trigger_mode,
        "time_s": trigger_t,
        "reason": reason,
        "statustext_failsafe_hits": len(status_hits),
        "statustext_samples": status_hits[:10],
        "mode_transitions": mode_transitions[:20],
        "mode_transitions_n": len(mode_transitions),
        "choice": (
            "proxy SITL msgs over DataFlash/stdout: already on hot path, "
            "CLOCK_MONOTONIC stamped, no radio delay, no .bin parse"
        ),
    }


def _fs_gcs_from_gcs(
    mission_summary: dict[str, Any],
    status_rows: list[dict[str, str]],
    *,
    windows: dict[str, dict],
    phase: Optional[str],
) -> dict[str, Any]:
    """Secondary FS_GCS: GCS view (may be delayed by return leg under attack)."""
    fs = ((mission_summary.get("c2_metrics") or {}).get("failsafe") or {})
    mode_tx = (
        ((mission_summary.get("physics_metrics") or {}).get("flight_state") or {}).get(
            "mode_transitions"
        )
        or (mission_summary.get("physics_metrics") or {}).get("mode_transitions")
        or []
    )
    fs_texts = []
    for r in status_rows:
        text = (r.get("text") or "").lower()
        if "failsafe" in text or "fs_gcs" in text or "gcs failsafe" in text:
            t_rel = _f(r, "t_rel_s")
            if phase is not None and t_rel is not None and windows:
                if assign_phase(t_rel, windows) != phase:
                    continue
            fs_texts.append(r)
    triggered = bool(fs.get("triggered")) if fs else None
    return {
        "source": "mission_summary c2_metrics.failsafe + statustext_gcs (delayed under attack)",
        "triggered": triggered,
        "mode": fs.get("mode"),
        "time_s": fs.get("time_s"),
        "statustext_failsafe_hits": len(fs_texts),
        "statustext_samples": [
            {"t_rel_s": r.get("t_rel_s"), "text": r.get("text")} for r in fs_texts[:10]
        ],
        "mode_transitions_n": len(mode_tx) if isinstance(mode_tx, list) else None,
    }


def compute_k7_command_leg(
    *,
    proxy_link_rows: list[dict[str, str]],
    mission_summary: dict[str, Any],
    status_rows: list[dict[str, str]],
    windows: dict[str, dict],
    phase: Optional[str] = None,
    t0_mono_ns: Optional[float] = None,
    t0_unix_ns: Optional[float] = None,
) -> dict[str, Any]:
    """K7 for one phase (or ALL if phase is None). Primary FS_GCS = UAV proxy."""
    events = _command_link_events(
        proxy_link_rows, t0_mono_ns=t0_mono_ns, t0_unix_ns=t0_unix_ns,
    )
    by_type: dict[str, int] = {}
    t_cmd: list[float] = []
    timeline = "none"
    phase_ok = True
    not_verified: list[str] = []

    resolved = [e for e in events if e.get("t_rel_s") is not None]
    if resolved:
        timeline = "t_rel_s or (t_arrival_ns - T0_mono_ns)"
        for e in resolved:
            t_rel = float(e["t_rel_s"])
            if phase is not None and windows:
                if assign_phase(t_rel, windows) != phase:
                    continue
            t_cmd.append(t_rel)
            by_type[e["mavlink_msg_type"]] = by_type.get(e["mavlink_msg_type"], 0) + 1
    else:
        timeline = "mono_delta_ALL_only"
        if phase is not None:
            phase_ok = False
            not_verified.append(
                "K7 phase slice: proxy_link lacks t_rel_s and T0_mono_ns"
            )
        else:
            monos = sorted(
                float(e["t_arrival_ns"])
                for e in events
                if e.get("t_arrival_ns") is not None
            )
            if monos:
                t0 = monos[0]
                for e in sorted(
                    (e for e in events if e.get("t_arrival_ns") is not None),
                    key=lambda x: float(x["t_arrival_ns"]),
                ):
                    t_cmd.append((float(e["t_arrival_ns"]) - t0) / 1e9)
                    by_type[e["mavlink_msg_type"]] = by_type.get(e["mavlink_msg_type"], 0) + 1

    max_g = max_gap_s(t_cmd) if t_cmd else None
    if max_g is None and events:
        monos = sorted(
            float(e["t_arrival_ns"])
            for e in events
            if e.get("t_arrival_ns") is not None
        )
        if len(monos) >= 2:
            max_g = max((monos[i] - monos[i - 1]) / 1e9 for i in range(1, len(monos)))
            if phase is not None:
                not_verified.append("max_gap_s from ALL mono deltas, not phase")

    primary = _fs_gcs_from_uav_proxy(
        proxy_link_rows,
        windows=windows,
        phase=phase,
        t0_mono_ns=t0_mono_ns,
        t0_unix_ns=t0_unix_ns,
    )
    secondary = _fs_gcs_from_gcs(
        mission_summary,
        status_rows,
        windows=windows,
        phase=phase,
    )

    # C4 / pass uses PRIMARY (UAV). GCS kept for discrepancy reporting.
    pass_k7 = (not primary["triggered"]) if primary else None
    if primary.get("triggered") is False and not any(
        (r.get("mavlink_msg_type") == "HEARTBEAT" and r.get("direction") == "sitl_to_gcs")
        for r in proxy_link_rows
    ):
        not_verified.append(
            "UAV primary FS_GCS: no sitl_to_gcs HEARTBEAT in proxy_link; "
            "triggered=false may be vacuous"
        )

    return {
        "schema": "k7_fs_gcs_v2",
        "phase": phase or "ALL",
        "leg": "command",
        "phase_slice_ok": phase_ok if phase is not None else True,
        "NOT_VERIFIED": not_verified,
        "FS_GCS": {
            "triggered": primary.get("triggered"),
            "mode": primary.get("mode"),
            "time_s": primary.get("time_s"),
            "primary_source": "uav_proxy_sitl_to_gcs",
            "primary": primary,
            "secondary_gcs": secondary,
            "gcs_discrepancy": (
                None
                if secondary.get("triggered") is None
                else bool(secondary.get("triggered")) != bool(primary.get("triggered"))
            ),
        },
        "link_arrivals": {
            "n": len(t_cmd),
            "by_type": by_type,
            "max_gap_s": round(max_g, 6) if max_g is not None else None,
            "timeline": timeline,
        },
        "pass": pass_k7,
        "c4_field": "FS_GCS",
        "note": (
            "pass uses the primary FS_GCS (proxy sitl_to_gcs). "
            "The GCS is secondary (return-leg delay under attack). "
            "Max gap: HEARTBEAT/MANUAL_CONTROL/RC no UAV (gcs_to_sitl)."
        ),
    }


def rebase_gcs_rows_to_t0(
    rows: list[dict[str, str]],
    t0_mono_ns: Optional[float],
) -> list[dict[str, str]]:
    """GCS csv t_rel_s is the mission clock; re-express it as s since T0 via t_mono_ns."""
    if t0_mono_ns is None:
        return rows
    out = []
    for r in rows:
        mono = _f(r, "t_mono_ns")
        if mono is None:
            out.append(r)
            continue
        row = dict(r)
        row["t_rel_mission_s"] = r.get("t_rel_s")
        row["t_rel_s"] = (mono - float(t0_mono_ns)) / 1e9
        out.append(row)
    return out


def compute_k8_return_leg(
    *,
    mavlink_rx_rows: list[dict[str, str]],
    windows: dict[str, dict],
    phase: Optional[str] = None,
    t_lo: Optional[float] = None,
    t_hi: Optional[float] = None,
) -> dict[str, Any]:
    if not mavlink_rx_rows:
        return {
            "ok": False,
            "schema": "k8_qgc_style_v1",
            "error": "no_source",
            "n_arrivals": 0,
            "max_gap_s": None,
            "loss_declarations": None,
            "pct_time_gap_gt_loss": None,
            "pass": None,
            "NOT_VERIFIED": "mavlink_rx_gcs.csv missing or empty; K8 not evaluated",
            "phase": phase or "ALL",
            "leg": "return",
            "c4_field": "QGC_style_gap_3_5s",
        }
    arrivals = []
    for r in mavlink_rx_rows:
        mtype = r.get("mavlink_msg_type") or ""
        if mtype in ("RADIO_STATUS", "BAD_DATA"):
            continue
        t_rel = _f(r, "t_rel_s")
        if t_rel is None:
            # Fallback from mono ns relative
            continue
        arrivals.append(t_rel)

    if t_lo is None or t_hi is None:
        if phase and phase in windows:
            t_lo = float(windows[phase]["t_rel_lo_s"])
            t_hi = float(windows[phase]["t_rel_hi_s"])
        elif arrivals:
            t_lo = min(arrivals)
            t_hi = max(arrivals) + 1e-9
        else:
            t_lo, t_hi = 0.0, 0.0

    result = qgc_style_link_loss(arrivals, t_lo=float(t_lo), t_hi=float(t_hi))
    result["phase"] = phase or "ALL"
    result["leg"] = "return"
    result["c4_field"] = "QGC_style_gap_3_5s"
    return result


def fill_c4_checks(c4: dict[str, Any], k7: dict[str, Any], k8: dict[str, Any]) -> dict[str, Any]:
    """Merge K7/K8 into a criterion_C4 checks dict."""
    out = dict(c4) if c4 else {"criterion": "C4", "checks": {}}
    checks = dict(out.get("checks") or {})
    checks["FS_GCS"] = {
        "pass": k7.get("pass"),
        "triggered": (k7.get("FS_GCS") or {}).get("triggered"),
        "max_gap_command_s": (k7.get("link_arrivals") or {}).get("max_gap_s"),
        "primary_source": ((k7.get("FS_GCS") or {}).get("primary_source")
                          or "uav_proxy_sitl_to_gcs"),
        "source": "K7 link_vivacity (UAV primary, GCS secondary)",
    }
    checks["QGC_style_gap_3_5s"] = {
        "pass": k8.get("pass"),
        "max_gap_s": k8.get("max_gap_s"),
        "loss_declarations": k8.get("loss_declarations"),
        "pct_time_gap_gt_3_5s": k8.get("pct_time_gap_gt_loss"),
        "source": "K8 link_vivacity (QGC VehicleLinkManager style)",
    }
    out["checks"] = checks
    out["scope"] = "kpi_owd_plus_FS_GCS_plus_QGC"
    scored = [v.get("pass") for v in checks.values() if v.get("pass") is not None]
    out["pass_aggregate"] = all(scored) if scored else None
    return out


def analyze_run(run_dir: Path) -> dict[str, Any]:
    markers = load_markers(run_dir)
    pw_path = run_dir / "flyblind_phase_windows.json"
    if pw_path.is_file():
        pw = json.loads(pw_path.read_text(encoding="utf-8"))
        windows = pw.get("windows") or {}
    else:
        windows = (build_windows(markers).get("windows") or {})

    proxy_link = _load_csv(run_dir / "proxy_link.csv")
    if not proxy_link:
        for cand in run_dir.glob("**/proxy_link.csv"):
            proxy_link = _load_csv(cand)
            break

    t0_mono_raw = load_t0_mono_ns(run_dir)
    t0_unix_raw = load_t0_unix_ns(run_dir)
    t0_mono = float(t0_mono_raw) if t0_mono_raw is not None else None
    t0_unix = float(t0_unix_raw) if t0_unix_raw is not None else None
    mav_rx = rebase_gcs_rows_to_t0(_load_csv(run_dir / "mavlink_rx_gcs.csv"), t0_mono)
    status = rebase_gcs_rows_to_t0(_load_csv(run_dir / "statustext_gcs.csv"), t0_mono)
    summary = {}
    summary_src = None
    for name in ("mission_summary.json", "c2.json"):
        sp = run_dir / name
        if sp.is_file():
            summary = json.loads(sp.read_text(encoding="utf-8"))
            summary_src = name
            break

    phases = ["ALL"] + [p for p in ("PRE", "ATTACK", "DRAIN") if p in windows]
    k7_by: dict[str, Any] = {}
    k8_by: dict[str, Any] = {}
    for ph in phases:
        phase_arg = None if ph == "ALL" else ph
        k7_by[ph] = compute_k7_command_leg(
            proxy_link_rows=proxy_link,
            mission_summary=summary,
            status_rows=status,
            windows=windows,
            phase=phase_arg,
            t0_mono_ns=t0_mono,
            t0_unix_ns=t0_unix,
        )
        if ph == "ALL":
            k8_by[ph] = compute_k8_return_leg(
                mavlink_rx_rows=mav_rx, windows=windows, phase=None
            )
        else:
            k8_by[ph] = compute_k8_return_leg(
                mavlink_rx_rows=mav_rx, windows=windows, phase=ph
            )

    k8_preflight = None
    arrivals_rel = [t for t in (_f(r, "t_rel_s") for r in mav_rx) if t is not None]
    if windows and arrivals_rel:
        flight_lo = min(float(w["t_rel_lo_s"]) for w in windows.values())
        first_rx = min(arrivals_rel)
        if first_rx < flight_lo:
            k8_preflight = compute_k8_return_leg(
                mavlink_rx_rows=mav_rx, windows=windows, phase=None,
                t_lo=first_rx, t_hi=flight_lo,
            )
            k8_preflight["phase"] = "PREFLIGHT"
            k8_preflight["excluded_from_analysis"] = True
            k8_preflight["note_preflight"] = (
                "From the first MAVLink received at the GCS to the start of the first flight window "
                "(mission bootstrap, takeoff). Not included in C4 nor in the flight phases."
            )

    focus = "ATTACK" if "ATTACK" in k7_by else "ALL"
    c4 = fill_c4_checks({}, k7_by[focus], k8_by[focus])

    return {
        "schema": "flyblind_link_vivacity_v1",
        "run_dir": str(run_dir),
        "windows": windows,
        "t0_mono_ns": t0_mono,
        "t0_unix_ns": t0_unix,
        "k7_command": k7_by,
        "k8_return": k8_by,
        "k8_preflight": k8_preflight,
        "criterion_C4_vivacity": c4,
        "inputs": {
            "proxy_link_rows": len(proxy_link),
            "mavlink_rx_gcs_rows": len(mav_rx),
            "statustext_rows": len(status),
            "mission_summary": bool(summary),
            "mission_summary_source": summary_src,
            "gcs_time_base": (
                "t_mono_ns - T0_mono_ns" if t0_mono is not None else "t_rel_s (mission clock)"
            ),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="K7/K8 link vivacity by phase")
    ap.add_argument("--run-dir", default="", help="Required unless --bench-synthetic")
    ap.add_argument("--out", default="")
    ap.add_argument(
        "--bench-synthetic",
        action="store_true",
        help="Run synthetic K8 bench (known gaps) and exit",
    )
    args = ap.parse_args()
    if args.bench_synthetic:
        # Arrivals at 0,1,2, then gap 5 s, then 8,9  → loss at tick ~5.5
        arrivals = [0.0, 1.0, 2.0, 8.0, 9.0]
        r = qgc_style_link_loss(arrivals, t_lo=0.0, t_hi=10.0)
        print(json.dumps({"bench": "k8_synthetic", "result": r}, indent=2))
        # Expect max_gap >= 5, loss_declarations >= 1
        ok = (
            r.get("max_gap_s") is not None
            and r["max_gap_s"] >= 5.0 - 1e-9
            and r["loss_declarations"] >= 1
            and r.get("pass") is False
        )
        raise SystemExit(0 if ok else 1)

    if not args.run_dir:
        ap.error("--run-dir is required unless --bench-synthetic")
    run = Path(args.run_dir)
    data = analyze_run(run)
    out = Path(args.out) if args.out else run / "flyblind_link_vivacity.json"
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: data[k] for k in data if k not in ("windows",)}, indent=2))


if __name__ == "__main__":
    main()

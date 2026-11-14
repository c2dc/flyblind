##################################################################################################################################
# Module: paper_artifacts.py (per-run metadata block recorded with every result)                                                 #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Mitigation-stack variant of paper_artifacts.py.                                                                   #
##################################################################################################################################

"""Paper-facing capture metadata — additive JSON/CSV only; never feeds VALID/DAMAGE.

Writes analysis constants and event markers so post-processing can build SA/HMI
metrics offline without changing runtime gates or on-screen reports.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

# Defaults for offline SA / HMI analysis (not used by orchestrate gates).
ANALYSIS_PARAMS_DEFAULTS: dict[str, Any] = {
    "schema_version": "analysis_params_v1",
    "T_safe_ms": 500.0,
    "E_safe_m": 5.0,
    "geofence_radius_m": 50.0,
    "landing_radius_m": 5.0,
    "notes": (
        "Constants for post-processing only. "
        "T_safe_ms / E_safe_m feed Semantic Availability offline; "
        "geofence/landing radii feed HMI detectors offline."
    ),
}

ARTIFACT_BASENAMES: dict[str, str] = {
    "telemetry_downlink_gcs_csv": "telemetry_downlink_gcs.csv",
    "c2_telemetry_uav_csv": "c2_telemetry_uav.csv",
    "c2_arrival_csv": "c2_arrival.csv",
    "prb_csv": "prb.csv",
    "prb_raw_log": "mac-prb-raw.log",
    "prb_json": "prb.json",
    "event_markers_csv": "event_markers.csv",
    "analysis_params_json": "analysis_params.json",
    "host_telemetry_csv": "host_timeseries.csv",
    "run_meta_json": "run_meta.json",
}

EVENT_MARKER_FIELDS = ["event", "t_rel_s", "note"]


def build_analysis_params(args: Any | None = None) -> dict[str, Any]:
    """Return analysis constants; optional CLI overrides if present on args."""
    out = dict(ANALYSIS_PARAMS_DEFAULTS)
    if args is None:
        return out
    for key, attr in (
        ("T_safe_ms", "analysis_t_safe_ms"),
        ("E_safe_m", "analysis_e_safe_m"),
        ("geofence_radius_m", "analysis_geofence_radius_m"),
        ("landing_radius_m", "analysis_landing_radius_m"),
    ):
        val = getattr(args, attr, None)
        if val is not None:
            out[key] = float(val)
    return out


def write_analysis_params(path: Path, params: dict[str, Any] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = params if params is not None else build_analysis_params()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def artifact_paths_relative() -> dict[str, str]:
    return dict(ARTIFACT_BASENAMES)


def compute_offered_attack_load(args: Any, *, is_baseline: bool) -> dict[str, Any] | None:
    """Nominal offered attack load from CLI (not measured RAN goodput)."""
    if is_baseline:
        return None
    attack = getattr(args, "attack", None)
    mean_mbps = float(getattr(args, "mean_rate_mbps", 0.0) or 0.0)
    period_ms = float(getattr(args, "period_ms", 0.0) or 0.0)
    burst_bytes = int(getattr(args, "burst_size_bytes", 0) or 0)
    out: dict[str, Any] = {
        "attack": attack,
        "mean_rate_mbps_per_rogue": mean_mbps if mean_mbps > 0 else None,
        "period_ms": period_ms if period_ms > 0 else None,
        "burst_size_bytes": burst_bytes if burst_bytes > 0 else None,
    }
    if attack in ("multi-rogue", "multi-rogue-dl"):
        n = max(1, int(getattr(args, "multi_rogue_count", 1) or 1))
        out["rogue_count"] = n
        out["direction"] = "dl" if attack == "multi-rogue-dl" else "ul"
        out["offered_aggregate_mbps"] = round(n * mean_mbps, 3) if mean_mbps > 0 else None
        if period_ms > 0:
            out["offered_bursts_per_s_per_rogue"] = round(1000.0 / period_ms, 3)
            out["offered_bursts_per_s_aggregate"] = round(n * 1000.0 / period_ms, 3)
    elif attack in ("multi-rogue-sync", "multi-rogue-dl-sync"):
        n = max(1, int(getattr(args, "multi_rogue_count", 1) or 1))
        burst_ms = float(getattr(args, "multi_sync_burst_ms", 0.0) or 0.0)
        pps = int(getattr(args, "multi_sync_pps", 0) or 0)
        size = int(getattr(args, "multi_sync_size", 0) or 0)
        out.update({
            "rogue_count": n,
            "direction": "dl" if attack == "multi-rogue-dl-sync" else "ul",
            "sync": True,
            "mean_rate_mbps_per_rogue": None,
            "period_ms": None,
            "burst_size_bytes": None,
            "burst_ms": burst_ms,
            "peak_pps_per_rogue": pps,
            "payload_bytes": size,
            "peak_mbps_per_rogue": round(pps * size * 8 / 1e6, 3),
            "peak_aggregate_mbps": round(n * pps * size * 8 / 1e6, 3),
        })
    elif attack in ("microburst", "dual-pulse"):
        out["rogue_count"] = 1
        out["offered_aggregate_mbps"] = mean_mbps if mean_mbps > 0 else None
        if period_ms > 0:
            out["offered_bursts_per_s_per_rogue"] = round(1000.0 / period_ms, 3)
            out["offered_bursts_per_s_aggregate"] = out["offered_bursts_per_s_per_rogue"]
    else:
        out["rogue_count"] = None
        out["offered_aggregate_mbps"] = mean_mbps if mean_mbps > 0 else None
    return out


def build_attack_onset_meta(
    args: Any,
    *,
    is_baseline: bool,
    envelope_s: float,
    resolved_delay_s: float,
    burst_duration_s: float,
    mission_start_s: float,
    attack_start_s_abs: float | None,
    attack_end_s_abs: float | None,
) -> dict[str, Any] | None:
    if is_baseline:
        return None
    asked = float(getattr(args, "attack_start_after_s", 0.0) or 0.0)
    return {
        "requested_start_after_s": asked,
        "resolved_delay_s": float(resolved_delay_s),
        "burst_duration_s": float(burst_duration_s),
        "mission_start_s": float(mission_start_s),
        "envelope_s": float(envelope_s),
        "adjusted_for_takeoff": bool(
            asked > 0 and abs(float(resolved_delay_s) - asked) >= 0.5
        ),
        "attack_start_s_abs": (
            round(float(attack_start_s_abs), 3) if attack_start_s_abs is not None else None
        ),
        "attack_end_s_abs": (
            round(float(attack_end_s_abs), 3) if attack_end_s_abs is not None else None
        ),
    }


def build_capture_flags(args: Any) -> dict[str, Any]:
    c2 = bool(getattr(args, "c2_arrival_capture", False))
    tel = bool(getattr(args, "telemetry_arrival_capture", False))
    return {
        "c2_arrival_capture": c2,
        "telemetry_arrival_capture": tel,
        "delivery_aoi_ready": bool(c2 and tel),
        "note": (
            "delivery AoI (UAV egress → GCS arrival) needs both flags; "
            "gap-only needs --telemetry-arrival-capture"
        ),
    }


def build_event_markers(
    *,
    envelope_s: float,
    mission_start_s: float,
    attack_onset_s: float | None,
    attack_end_s: float | None,
    wall_runtime_s: float | None,
    video_stop_s: float | None = None,
) -> list[dict[str, Any]]:
    """Append-only timeline markers (t_rel_s from T0 / envelope clock)."""
    rows: list[dict[str, Any]] = [
        {"event": "run_start", "t_rel_s": 0.0, "note": "T0"},
        {
            "event": "mission_start",
            "t_rel_s": round(float(mission_start_s), 3),
            "note": "centralization margin / mission launch",
        },
    ]
    if attack_onset_s is not None:
        rows.append({
            "event": "attack_onset",
            "t_rel_s": round(float(attack_onset_s), 3),
            "note": "resolved radio-contention attack start",
        })
    if attack_end_s is not None:
        rows.append({
            "event": "attack_end",
            "t_rel_s": round(float(attack_end_s), 3),
            "note": "resolved radio-contention attack end",
        })
    if video_stop_s is not None:
        rows.append({
            "event": "video_stop",
            "t_rel_s": round(float(video_stop_s), 3),
            "note": "DRAIN (b): companion video stopped at attack_end",
        })
    end_t = wall_runtime_s if wall_runtime_s is not None else envelope_s
    rows.append({
        "event": "run_end",
        "t_rel_s": round(float(end_t), 3),
        "note": "wall runtime" if wall_runtime_s is not None else "envelope",
    })
    return rows


def write_event_markers_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_MARKER_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in EVENT_MARKER_FIELDS})
    return path


def build_paper_artifacts_block(
    args: Any,
    *,
    is_baseline: bool,
    envelope_s: float,
    resolved_delay_s: float,
    burst_duration_s: float,
    mission_start_s: float,
    attack_start_s_abs: float | None,
    attack_end_s_abs: float | None,
    wall_runtime_s: float | None,
    analysis_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Block embedded in run_meta.json (additive; ignored by gates)."""
    params = analysis_params if analysis_params is not None else build_analysis_params(args)
    video_stop_s = getattr(args, "_video_stop_t_rel_s", None)
    markers = build_event_markers(
        envelope_s=envelope_s,
        mission_start_s=mission_start_s,
        attack_onset_s=attack_start_s_abs if not is_baseline else None,
        attack_end_s=attack_end_s_abs if not is_baseline else None,
        wall_runtime_s=wall_runtime_s,
        video_stop_s=float(video_stop_s) if video_stop_s is not None else None,
    )
    return {
        "schema_version": "paper_artifacts_v1",
        "multi_rogue_count": (
            int(getattr(args, "multi_rogue_count", 0) or 0)
            if (
                not is_baseline
                and getattr(args, "attack", None)
                in (
                    "multi-rogue", "multi-rogue-dl",
                    "multi-rogue-sync", "multi-rogue-dl-sync",
                )
            )
            else None
        ),
        "companion_video_mbps": float(getattr(args, "companion_video_mbps", 0) or 0) or None,
        "capture": build_capture_flags(args),
        "attack_onset": build_attack_onset_meta(
            args,
            is_baseline=is_baseline,
            envelope_s=envelope_s,
            resolved_delay_s=resolved_delay_s,
            burst_duration_s=burst_duration_s,
            mission_start_s=mission_start_s,
            attack_start_s_abs=attack_start_s_abs,
            attack_end_s_abs=attack_end_s_abs,
        ),
        "offered_attack_load": compute_offered_attack_load(args, is_baseline=is_baseline),
        "artifact_paths": artifact_paths_relative(),
        "analysis_params": params,
        "event_markers": markers,
    }


def persist_run_paper_artifacts(
    run_dir: Path,
    block: dict[str, Any],
) -> dict[str, str]:
    """Write analysis_params.json + event_markers.csv under the run directory."""
    run_dir = Path(run_dir)
    params_path = write_analysis_params(
        run_dir / ARTIFACT_BASENAMES["analysis_params_json"],
        block.get("analysis_params"),
    )
    markers_path = write_event_markers_csv(
        run_dir / ARTIFACT_BASENAMES["event_markers_csv"],
        list(block.get("event_markers") or []),
    )
    return {
        "analysis_params_json": str(params_path.name),
        "event_markers_csv": str(markers_path.name),
    }


def warn_paper_capture_flags(args: Any) -> list[str]:
    """Non-fatal tips when paper-style runs omit delivery-AoI captures."""
    tips: list[str] = []
    if getattr(args, "workload", None) != "setpoint-stream":
        return tips
    c2 = bool(getattr(args, "c2_arrival_capture", False))
    tel = bool(getattr(args, "telemetry_arrival_capture", False))
    dur = int(getattr(args, "duration", 0) or 0)
    if dur >= 180 and (not c2 or not tel):
        missing = []
        if not c2:
            missing.append("--c2-arrival-capture")
        if not tel:
            missing.append("--telemetry-arrival-capture")
        tips.append(
            "paper tip: missing %s — telemetry delivery AoI/delay will be incomplete"
            % (", ".join(missing))
        )
    return tips

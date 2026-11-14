#!/usr/bin/env python3
##################################################################################################################################
# Module: freshness_metrics.py (AoI, deadline misses, trk_truth, belief validation)                                              #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Post-run merge of setpoints + proxy CSVs; metric definitions for Gate A and report-only diagnostics.              #
##################################################################################################################################

"""Age of Information, deadline misses, and stale-interval metrics (freshness layer).

Consumed by ``setpoint_stream.py`` during the run and by ``orchestrate.py`` post-run
when merging ``proxy_forward.csv`` / ``proxy_reverse.csv`` into ``freshness_summary.json``.

Key concepts
------------
* ``network_arrival_delay_ms`` — per-setpoint one-way delay (GCS send → UAV proxy)
* ``aoi_network_ms`` — continuous 100 Hz AoI sampled between arrivals
* ``compute_trk_truth`` — SITL-egress NED vs plan (report-only; not VALID/DAMAGE)
* ``belief_vs_truth_m`` — frozen GCS belief vs moving SITL truth (diagnostic)

Correlation strategies (best match wins): setpoint_id → stream_order → send_time → ordinal.
"""
from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_D_NET_MS = (20, 50, 100, 150, 250)
DEFAULT_D_ACCEPT_MS = (100, 150, 250, 500, 1000)
DEFAULT_D_MOTION_MS = (250, 500, 1000, 1500)
TARGET_MATCH_TOL_M = 1.0

METRIC_DEFINITIONS: dict[str, str] = {
    "network_arrival_delay_ms": (
        "Per-setpoint one-way delay: t_arrival_uav_proxy − t_send_gcs (ms). "
        "Used for miss_ratio_network / owd_miss@D ms."
    ),
    "aoi_network_ms": (
        "Continuous Age of Information at 100 Hz: t_sample − t_generation of the latest "
        "setpoint whose proxy arrival ≤ t_sample. Grows between arrivals; "
        "used for miss_ratio_aoi_network / aoi_miss@D ms."
    ),
    "aoi_generation_ms": (
        "AoI vs setpoint generation time (legacy direct-UDP path without arrival capture): "
        "t_sample − t_generation; structural ~10 Hz sawtooth, blind to network delay."
    ),
    "aoi_arrival_ms": (
        "Continuous AoI (100 Hz) using generation time of the latest setpoint that has "
        "already arrived at the UAV proxy. Series cut at min(active_end, last_arrival) "
        "Not an alias of aoi_network_ms."
    ),
    "delivery_ratio": (
        "correlated_setpoints / generated_setpoints (active window)."
    ),
    "capture_ratio": (
        "unique_setpoints_seen_by_proxy / successful_send_calls."
    ),
    "correlation_ratio": (
        "correlated_setpoints / unique_setpoints_seen_by_proxy."
    ),
}


def percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = int(round((p / 100.0) * (len(s) - 1)))
    return float(s[idx])


@dataclass
class SetpointRecord:
    logical_setpoint_id: int
    generation_index: int
    trajectory_id: str
    t_generation_gcs_ns: int
    t_send_gcs_ns: int
    t_arrival_uav_proxy_ns: Optional[int] = None
    t_forward_sitl_ns: Optional[int] = None
    t_target_observed_ns: Optional[int] = None
    t_motion_onset_ns: Optional[int] = None
    x_ref: float = 0.0
    y_ref: float = 0.0
    z_ref: float = 0.0
    x_actual: Optional[float] = None
    y_actual: Optional[float] = None
    z_actual: Optional[float] = None
    tracking_error_m: Optional[float] = None
    cross_track_error_m: Optional[float] = None
    along_track_error_m: Optional[float] = None
    trajectory_window: Optional[str] = None  # acquisition | steady
    phase_lag_ms: Optional[float] = None
    superseded_network: bool = False
    superseded_acceptance: bool = False
    correlation_confidence: Optional[str] = None

    def network_arrival_delay_ms(self) -> Optional[float]:
        if self.t_arrival_uav_proxy_ns is None:
            return None
        return (self.t_arrival_uav_proxy_ns - self.t_send_gcs_ns) / 1e6

    def proxy_delay_ms(self) -> Optional[float]:
        if self.t_arrival_uav_proxy_ns is None or self.t_forward_sitl_ns is None:
            return None
        return (self.t_forward_sitl_ns - self.t_arrival_uav_proxy_ns) / 1e6

    def target_acceptance_delay_ms(self) -> Optional[float]:
        if self.t_target_observed_ns is None:
            return None
        return (self.t_target_observed_ns - self.t_generation_gcs_ns) / 1e6

    def motion_onset_delay_ms(self) -> Optional[float]:
        if self.t_motion_onset_ns is None:
            return None
        return (self.t_motion_onset_ns - self.t_generation_gcs_ns) / 1e6

    def inter_arrival_gap_ms(self, prev: Optional["SetpointRecord"]) -> Optional[float]:
        if prev is None or self.t_arrival_uav_proxy_ns is None or prev.t_arrival_uav_proxy_ns is None:
            return None
        return (self.t_arrival_uav_proxy_ns - prev.t_arrival_uav_proxy_ns) / 1e6

    def to_csv_row(self, prev: Optional["SetpointRecord"]) -> dict[str, Any]:
        return {
            "logical_setpoint_id": self.logical_setpoint_id,
            "generation_index": self.generation_index,
            "trajectory_id": self.trajectory_id,
            "t_generation_gcs_ns": self.t_generation_gcs_ns,
            "t_send_gcs_ns": self.t_send_gcs_ns,
            "t_arrival_uav_proxy_ns": self.t_arrival_uav_proxy_ns,
            "t_forward_sitl_ns": self.t_forward_sitl_ns,
            "t_target_observed_ns": self.t_target_observed_ns,
            "t_motion_onset_ns": self.t_motion_onset_ns,
            "network_arrival_delay_ms": self.network_arrival_delay_ms(),
            "proxy_delay_ms": self.proxy_delay_ms(),
            "target_acceptance_delay_ms": self.target_acceptance_delay_ms(),
            "motion_onset_delay_ms": self.motion_onset_delay_ms(),
            "inter_arrival_gap_ms": self.inter_arrival_gap_ms(prev),
            "x_ref": self.x_ref,
            "y_ref": self.y_ref,
            "z_ref": self.z_ref,
            "x_actual": self.x_actual,
            "y_actual": self.y_actual,
            "z_actual": self.z_actual,
            "tracking_error_m": self.tracking_error_m,
            "cross_track_error_m": self.cross_track_error_m,
            "along_track_error_m": self.along_track_error_m,
            "trajectory_window": self.trajectory_window,
            "phase_lag_ms": self.phase_lag_ms,
            "superseded_network": int(self.superseded_network),
            "superseded_acceptance": int(self.superseded_acceptance),
            "correlation_confidence": self.correlation_confidence,
        }


@dataclass
class AoISample:
    t_mono_ns: int
    t_rel_s: float
    latest_setpoint_id: Optional[int]
    latest_generation_ns: Optional[int]
    aoi_network_ms: Optional[float]
    aoi_acceptance_ms: Optional[float]
    aoi_motion_ms: Optional[float]
    current_deadline_state: str = "ok"


def compute_stale_intervals(
    times_ns: list[int],
    aoi_ms: list[float],
    deadline_ms: float,
) -> dict[str, Any]:
    """Longest continuous interval where AoI(t) > deadline."""
    if not times_ns or len(times_ns) != len(aoi_ms):
        return {"longest_continuous_stale_interval_ms": 0.0, "number_of_stale_intervals": 0}
    longest = 0.0
    count = 0
    start = None
    for i, aoi in enumerate(aoi_ms):
        if aoi is None:
            continue
        if aoi > deadline_ms:
            if start is None:
                start = times_ns[i]
                count += 1
        elif start is not None:
            longest = max(longest, (times_ns[i] - start) / 1e6)
            start = None
    if start is not None and times_ns:
        longest = max(longest, (times_ns[-1] - start) / 1e6)
    return {
        "longest_continuous_stale_interval_ms": round(longest, 3),
        "number_of_stale_intervals": count,
    }


def miss_ratio(values_ms: list[Optional[float]], deadline_ms: float) -> Optional[float]:
    obs = [v for v in values_ms if v is not None]
    if not obs:
        return None
    return sum(1 for v in obs if v > deadline_ms) / len(obs)


def build_freshness_summary(
    setpoints: list[SetpointRecord],
    aoi_samples: list[AoISample],
    proxy_forward_delays_ms: list[float],
    nominal_period_ms: float,
    d_net: tuple[int, ...] = DEFAULT_D_NET_MS,
    d_accept: tuple[int, ...] = DEFAULT_D_ACCEPT_MS,
    d_motion: tuple[int, ...] = DEFAULT_D_MOTION_MS,
) -> dict[str, Any]:
    net = [sp.network_arrival_delay_ms() for sp in setpoints]
    acc = [sp.target_acceptance_delay_ms() for sp in setpoints]
    mot = [sp.motion_onset_delay_ms() for sp in setpoints]
    gaps = []
    for i, sp in enumerate(setpoints):
        gaps.append(sp.inter_arrival_gap_ms(setpoints[i - 1] if i else None))

    aoi_net = [s.aoi_network_ms for s in aoi_samples if s.aoi_network_ms is not None]
    times_ns = [s.t_mono_ns for s in aoi_samples if s.aoi_network_ms is not None]
    aoi_vals = [s.aoi_network_ms for s in aoi_samples if s.aoi_network_ms is not None]

    summary: dict[str, Any] = {
        "schema_version": "freshness_v1",
        "n_setpoints": len(setpoints),
        "n_aoi_samples": len(aoi_samples),
        "nominal_period_ms": nominal_period_ms,
        "network_arrival_delay_ms": {
            "p50": percentile([v for v in net if v is not None] or [], 50),
            "p95": percentile([v for v in net if v is not None] or [], 95),
            "p99": percentile([v for v in net if v is not None] or [], 99),
            "max": max((v for v in net if v is not None), default=None),
        },
        "inter_arrival_gap_ms": {
            "p50": percentile([v for v in gaps if v is not None] or [], 50),
            "p95": percentile([v for v in gaps if v is not None] or [], 95),
            "p99": percentile([v for v in gaps if v is not None] or [], 99),
            "p99_9": percentile([v for v in gaps if v is not None] or [], 99.9),
            "max": max((v for v in gaps if v is not None), default=None),
            "above_1_5x_nominal": sum(
                1 for v in gaps if v is not None and nominal_period_ms > 0 and v > 1.5 * nominal_period_ms
            ),
            "above_2x_nominal": sum(
                1 for v in gaps if v is not None and nominal_period_ms > 0 and v > 2.0 * nominal_period_ms
            ),
            "above_5x_nominal": sum(
                1 for v in gaps if v is not None and nominal_period_ms > 0 and v > 5.0 * nominal_period_ms
            ),
        },
        "aoi_network_ms": {
            "p50": percentile(aoi_net, 50),
            "p95": percentile(aoi_net, 95),
            "p99": percentile(aoi_net, 99),
            "p99_9": percentile(aoi_net, 99.9),
            "max": max(aoi_net) if aoi_net else None,
            "mean": sum(aoi_net) / len(aoi_net) if aoi_net else None,
        },
        "proxy_delay_ms": {
            "p50": percentile(proxy_forward_delays_ms, 50),
            "p95": percentile(proxy_forward_delays_ms, 95),
            "p99": percentile(proxy_forward_delays_ms, 99),
            "p99_9": percentile(proxy_forward_delays_ms, 99.9),
            "max": max(proxy_forward_delays_ms) if proxy_forward_delays_ms else None,
            "n": len(proxy_forward_delays_ms),
        },
        "miss_ratio_network": {str(d): miss_ratio(net, d) for d in d_net},
        "miss_ratio_aoi_network": {str(d): miss_ratio(aoi_net, d) for d in d_net},
        "miss_ratio_acceptance": {str(d): miss_ratio(acc, d) for d in d_accept},
        "miss_ratio_motion": {str(d): miss_ratio(mot, d) for d in d_motion},
        "metric_definitions": {
            "network_arrival_delay_ms": METRIC_DEFINITIONS["network_arrival_delay_ms"],
            "aoi_network_ms": METRIC_DEFINITIONS["aoi_network_ms"],
            "miss_ratio_network": "Share of setpoints with network_arrival_delay_ms > D.",
            "miss_ratio_aoi_network": "Share of 100 Hz AoI samples with aoi_network_ms > D.",
        },
        "stale_intervals_ms": {},
        "target_acceptance_delay_ms": {
            "p50": percentile([v for v in acc if v is not None] or [], 50),
            "p99": percentile([v for v in acc if v is not None] or [], 99),
            "n_observed": sum(1 for v in acc if v is not None),
            "n_null": sum(1 for v in acc if v is None),
            "note": "proxy via POSITION_TARGET_LOCAL_NED when available",
        },
        "motion_onset_delay_ms": {
            "p50": percentile([v for v in mot if v is not None] or [], 50),
            "p99": percentile([v for v in mot if v is not None] or [], 99),
            "n_observed": sum(1 for v in mot if v is not None),
        },
    }
    for d in d_net:
        stale = compute_stale_intervals(times_ns, aoi_vals, float(d))
        summary["stale_intervals_ms"][str(d)] = stale
    return summary


def write_json(path: str, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def correlate_proxy_by_xyz(
    setpoints: list[SetpointRecord],
    proxy_forward_rows: list[dict[str, Any]],
    match_tol: float = TARGET_MATCH_TOL_M,
) -> tuple[int, int]:
    """Match setpoints to proxy arrivals by rounded xyz (time-ordered)."""
    arrivals = [
        r for r in proxy_forward_rows
        if r.get("mavlink_msg_type") == "SET_POSITION_TARGET_LOCAL_NED"
        and r.get("t_arrival_ns") is not None
    ]
    arrivals.sort(key=lambda r: int(r["t_arrival_ns"]))
    matched = 0
    used: set[int] = set()
    for sp in setpoints:
        key = (round(sp.x_ref, 2), round(sp.y_ref, 2), round(sp.z_ref, 2))
        for j, row in enumerate(arrivals):
            if j in used:
                continue
            try:
                ok = (
                    round(float(row["x"]), 2),
                    round(float(row["y"]), 2),
                    round(float(row["z"]), 2),
                ) == key
            except (KeyError, TypeError, ValueError):
                ok = False
            if ok:
                sp.t_arrival_uav_proxy_ns = int(row["t_arrival_ns"])
                sp.t_forward_sitl_ns = int(row.get("t_forward_ns") or row["t_arrival_ns"])
                sp.correlation_confidence = "xyz"
                used.add(j)
                matched += 1
                break
    return matched, len(arrivals)


def _clear_proxy_correlation(setpoints: list[SetpointRecord]) -> None:
    for sp in setpoints:
        sp.t_arrival_uav_proxy_ns = None
        sp.t_forward_sitl_ns = None
        sp.correlation_confidence = None


def _setpoint_arrivals(
    proxy_forward_rows: list[dict[str, Any]],
    t_min_ns: Optional[int] = None,
    t_max_ns: Optional[int] = None,
) -> list[dict[str, Any]]:
    arrivals = [
        r for r in proxy_forward_rows
        if r.get("mavlink_msg_type") == "SET_POSITION_TARGET_LOCAL_NED"
        and r.get("t_arrival_ns") is not None
    ]
    if t_min_ns is not None:
        arrivals = [r for r in arrivals if int(r["t_arrival_ns"]) >= t_min_ns]
    if t_max_ns is not None:
        arrivals = [r for r in arrivals if int(r["t_arrival_ns"]) <= t_max_ns]
    arrivals.sort(key=lambda r: int(r["t_arrival_ns"]))
    return arrivals


def _frame_setpoint_id(row: dict[str, Any]) -> int:
    try:
        return int(row.get("time_boot_ms"))
    except (TypeError, ValueError):
        return 0


def logical_ids_embedded(
    setpoints: list[SetpointRecord],
    proxy_forward_rows: list[dict[str, Any]],
    min_fraction: float = 0.9,
) -> bool:
    """True when SET_POSITION_TARGET frames carry logical_setpoint_id in time_boot_ms."""
    known = {sp.logical_setpoint_id for sp in setpoints if sp.logical_setpoint_id > 0}
    positive = [
        sid for sid in (_frame_setpoint_id(r) for r in _setpoint_arrivals(proxy_forward_rows))
        if sid > 0
    ]
    if not known or not positive:
        return False
    return sum(1 for sid in positive if sid in known) >= min_fraction * len(positive)


def drop_preposition_frames(proxy_forward_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop SET_POSITION_TARGET frames without a logical id (preposition, time_boot_ms <= 0)."""
    return [
        r for r in proxy_forward_rows
        if r.get("mavlink_msg_type") != "SET_POSITION_TARGET_LOCAL_NED"
        or _frame_setpoint_id(r) > 0
    ]


def correlate_proxy_by_stream_order(
    setpoints: list[SetpointRecord],
    proxy_forward_rows: list[dict[str, Any]],
    time_margin_ns: int = int(1.5 * 1e9),
) -> tuple[int, int]:
    """Match 1:1 by monotonic arrival order within the setpoint send window."""
    ordered_sps = sorted(setpoints, key=lambda sp: sp.t_send_gcs_ns)
    if not ordered_sps:
        return 0, 0
    t_min = ordered_sps[0].t_send_gcs_ns - time_margin_ns
    t_max = ordered_sps[-1].t_send_gcs_ns + time_margin_ns
    arrivals = _setpoint_arrivals(proxy_forward_rows, t_min, t_max)
    matched = 0
    for i, sp in enumerate(ordered_sps):
        if i >= len(arrivals):
            break
        row = arrivals[i]
        sp.t_arrival_uav_proxy_ns = int(row["t_arrival_ns"])
        sp.t_forward_sitl_ns = int(row.get("t_forward_ns") or row["t_arrival_ns"])
        sp.correlation_confidence = "stream_order"
        matched += 1
    return matched, len(arrivals)


def correlate_proxy_by_send_time(
    setpoints: list[SetpointRecord],
    proxy_forward_rows: list[dict[str, Any]],
    max_late_ns: int = int(0.5 * 1e9),
    max_owd_ns: int = int(200 * 1e6),
) -> tuple[int, int]:
    """Match each setpoint to the nearest later proxy arrival (handles circle retraces)."""
    arrivals = [
        r for r in proxy_forward_rows
        if r.get("mavlink_msg_type") == "SET_POSITION_TARGET_LOCAL_NED"
        and r.get("t_arrival_ns") is not None
    ]
    arrivals.sort(key=lambda r: int(r["t_arrival_ns"]))
    matched = 0
    used: set[int] = set()
    for sp in setpoints:
        best_j: int | None = None
        best_skew: int | None = None
        for j, row in enumerate(arrivals):
            if j in used:
                continue
            arr = int(row["t_arrival_ns"])
            if arr + max_late_ns < sp.t_send_gcs_ns:
                continue
            skew = arr - sp.t_send_gcs_ns
            if skew < 0 or skew > max_owd_ns:
                continue
            if best_skew is None or skew < best_skew:
                best_skew = skew
                best_j = j
        if best_j is None:
            continue
        row = arrivals[best_j]
        sp.t_arrival_uav_proxy_ns = int(row["t_arrival_ns"])
        sp.t_forward_sitl_ns = int(row.get("t_forward_ns") or row["t_arrival_ns"])
        sp.correlation_confidence = "send_time"
        used.add(best_j)
        matched += 1
    return matched, len(arrivals)


def correlate_proxy_by_setpoint_id(
    setpoints: list[SetpointRecord],
    proxy_forward_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    """Match setpoints to arrivals by logical_setpoint_id embedded in time_boot_ms."""
    arrivals = [
        r for r in proxy_forward_rows
        if r.get("mavlink_msg_type") == "SET_POSITION_TARGET_LOCAL_NED"
        and r.get("t_arrival_ns") is not None
    ]
    by_id: dict[int, dict[str, Any]] = {}
    for row in arrivals:
        try:
            sid = int(row.get("time_boot_ms"))
        except (TypeError, ValueError):
            continue
        if sid <= 0 or sid in by_id:
            continue
        by_id[sid] = row
    matched = 0
    for sp in setpoints:
        row = by_id.get(sp.logical_setpoint_id)
        if not row:
            continue
        sp.t_arrival_uav_proxy_ns = int(row["t_arrival_ns"])
        sp.t_forward_sitl_ns = int(row.get("t_forward_ns") or row["t_arrival_ns"])
        sp.correlation_confidence = "setpoint_id"
        matched += 1
    return matched, len(arrivals)


def correlate_proxy_by_order(
    setpoints: list[SetpointRecord],
    proxy_forward_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    """Match setpoints to proxy arrivals by message order (best-effort)."""
    arrivals = [
        r for r in proxy_forward_rows
        if r.get("mavlink_msg_type") == "SET_POSITION_TARGET_LOCAL_NED"
        and r.get("t_arrival_ns") is not None
    ]
    arrivals.sort(key=lambda r: int(r["t_arrival_ns"]))
    matched = 0
    for i, sp in enumerate(setpoints):
        if i >= len(arrivals):
            break
        row = arrivals[i]
        sp.t_arrival_uav_proxy_ns = int(row["t_arrival_ns"])
        sp.t_forward_sitl_ns = int(row.get("t_forward_ns") or row["t_arrival_ns"])
        sp.correlation_confidence = "ordinal"
        matched += 1
    return matched, len(arrivals)


def correlate_acceptance_from_reverse(
    setpoints: list[SetpointRecord],
    proxy_reverse_rows: list[dict[str, Any]],
    match_tol_m: float = TARGET_MATCH_TOL_M,
) -> int:
    """Match SITL POSITION_TARGET to setpoints by rounded xyz (proxy observation)."""
    observed = [
        r for r in proxy_reverse_rows
        if r.get("mavlink_msg_type") == "POSITION_TARGET_LOCAL_NED"
        and r.get("t_arrival_ns") is not None
    ]
    observed.sort(key=lambda r: int(r["t_arrival_ns"]))
    matched = 0
    oi = 0
    for sp in setpoints:
        if sp.t_target_observed_ns is not None:
            continue
        key = (round(sp.x_ref, 2), round(sp.y_ref, 2), round(sp.z_ref, 2))
        while oi < len(observed):
            row = observed[oi]
            oi += 1
            try:
                ok = (
                    round(float(row["x"]), 2),
                    round(float(row["y"]), 2),
                    round(float(row["z"]), 2),
                ) == key
            except (KeyError, TypeError, ValueError):
                ok = False
            if ok:
                sp.t_target_observed_ns = int(row["t_arrival_ns"])
                sp.correlation_confidence = (sp.correlation_confidence or "proxy") + "+accept_xyz"
                matched += 1
                break
    return matched


def build_aoi_generation_timeseries(
    setpoints: list[SetpointRecord],
    t_end_ns: int,
    t_start_ns: int,
    sample_hz: float = 100.0,
    nominal_period_ms: float = 100.0,
) -> list[AoISample]:
    """AoI from latest setpoint *generation* time (direct-UDP default; no arrival stamp)."""
    if sample_hz <= 0:
        raise ValueError("sample_hz must be > 0")
    ordered = sorted(setpoints, key=lambda sp: sp.t_generation_gcs_ns)
    period_ns = int(1e9 / sample_hz)
    samples: list[AoISample] = []
    t = t_start_ns
    idx = 0
    latest: Optional[SetpointRecord] = None
    while t <= t_end_ns:
        while idx < len(ordered) and ordered[idx].t_generation_gcs_ns <= t:
            latest = ordered[idx]
            idx += 1
        aoi_gen = None
        state = "ok"
        if latest is not None:
            aoi_gen = (t - latest.t_generation_gcs_ns) / 1e6
            if aoi_gen > nominal_period_ms * 2:
                state = "stale"
        samples.append(AoISample(
            t_mono_ns=t,
            t_rel_s=round((t - t_start_ns) / 1e9, 4),
            latest_setpoint_id=latest.logical_setpoint_id if latest else None,
            latest_generation_ns=latest.t_generation_gcs_ns if latest else None,
            aoi_network_ms=aoi_gen,
            aoi_acceptance_ms=None,
            aoi_motion_ms=None,
            current_deadline_state=state,
        ))
        t += period_ns
    return samples


def build_aoi_timeseries(
    setpoints: list[SetpointRecord],
    t_end_ns: int,
    t_start_ns: int,
    sample_hz: float = 100.0,
    nominal_period_ms: float = 100.0,
) -> list[AoISample]:
    """AoI from latest setpoint received at UAV proxy (generation time of last arrival)."""
    if sample_hz <= 0:
        raise ValueError("sample_hz must be > 0")
    received = [
        sp for sp in setpoints
        if sp.t_arrival_uav_proxy_ns is not None and sp.t_generation_gcs_ns is not None
    ]
    if not received:
        received = [sp for sp in setpoints if sp.t_send_gcs_ns]
    period_ns = int(1e9 / sample_hz)
    samples: list[AoISample] = []
    t = t_start_ns
    idx = 0
    latest: Optional[SetpointRecord] = None
    while t <= t_end_ns:
        while idx < len(received):
            arr_ns = received[idx].t_arrival_uav_proxy_ns or received[idx].t_send_gcs_ns
            if arr_ns <= t:
                latest = received[idx]
                idx += 1
            else:
                break
        aoi_net = None
        state = "ok"
        if latest is not None:
            aoi_net = (t - latest.t_generation_gcs_ns) / 1e6
            if aoi_net > nominal_period_ms * 2:
                state = "stale"
        samples.append(AoISample(
            t_mono_ns=t,
            t_rel_s=round((t - t_start_ns) / 1e9, 4),
            latest_setpoint_id=latest.logical_setpoint_id if latest else None,
            latest_generation_ns=latest.t_generation_gcs_ns if latest else None,
            aoi_network_ms=aoi_net,
            aoi_acceptance_ms=(
                (t - latest.t_target_observed_ns) / 1e6
                if latest and latest.t_target_observed_ns else None
            ),
            aoi_motion_ms=(
                (t - latest.t_motion_onset_ns) / 1e6
                if latest and latest.t_motion_onset_ns else None
            ),
            current_deadline_state=state,
        ))
        t += period_ns
    return samples


def setpoint_from_csv_row(row: dict[str, Any]) -> SetpointRecord:
    def _int_or_none(k: str) -> Optional[int]:
        v = row.get(k)
        if v in (None, "", "None"):
            return None
        return int(float(v))

    def _float_or_none(k: str) -> Optional[float]:
        v = row.get(k)
        if v in (None, "", "None"):
            return None
        return float(v)

    return SetpointRecord(
        logical_setpoint_id=int(row["logical_setpoint_id"]),
        generation_index=int(row["generation_index"]),
        trajectory_id=str(row.get("trajectory_id", "")),
        t_generation_gcs_ns=int(row["t_generation_gcs_ns"]),
        t_send_gcs_ns=int(row["t_send_gcs_ns"]),
        t_arrival_uav_proxy_ns=_int_or_none("t_arrival_uav_proxy_ns"),
        t_forward_sitl_ns=_int_or_none("t_forward_sitl_ns"),
        t_target_observed_ns=_int_or_none("t_target_observed_ns"),
        t_motion_onset_ns=_int_or_none("t_motion_onset_ns"),
        x_ref=float(row.get("x_ref", 0)),
        y_ref=float(row.get("y_ref", 0)),
        z_ref=float(row.get("z_ref", 0)),
        x_actual=_float_or_none("x_actual"),
        y_actual=_float_or_none("y_actual"),
        z_actual=_float_or_none("z_actual"),
        tracking_error_m=_float_or_none("tracking_error_m"),
        cross_track_error_m=_float_or_none("cross_track_error_m"),
        along_track_error_m=_float_or_none("along_track_error_m"),
        phase_lag_ms=_float_or_none("phase_lag_ms"),
        superseded_network=bool(int(row.get("superseded_network", 0) or 0)),
        superseded_acceptance=bool(int(row.get("superseded_acceptance", 0) or 0)),
        correlation_confidence=row.get("correlation_confidence") or None,
    )


def _pick_proxy_correlation(
    setpoints: list[SetpointRecord],
    proxy_forward_rows: list[dict[str, Any]],
    logical_only: Optional[bool] = None,
) -> tuple[int, int, str, dict[str, Any]]:
    """Exact setpoint_id when logical ids are embedded; else the strategy with most matches.

    With embedded ids, preposition frames are dropped before every strategy and
    order-based strategies are not allowed to win on count (they shift on loss).
    """
    if logical_only is None:
        logical_only = logical_ids_embedded(setpoints, proxy_forward_rows)
    rows = drop_preposition_frames(proxy_forward_rows) if logical_only else proxy_forward_rows
    candidates: list[tuple[str, Any]] = [
        ("setpoint_id", correlate_proxy_by_setpoint_id),
        ("stream_order", correlate_proxy_by_stream_order),
        ("send_time", correlate_proxy_by_send_time),
        ("ordinal", correlate_proxy_by_order),
    ]
    matches_by_method: dict[str, int] = {}
    best_matched = -1
    best_method = "ordinal"
    best_fn = correlate_proxy_by_order
    for name, fn in candidates:
        _clear_proxy_correlation(setpoints)
        matched, _total = fn(setpoints, rows)
        matches_by_method[name] = matched
        if matched > best_matched:
            best_matched = matched
            best_method = name
            best_fn = fn
    selection = "max_matched"
    if logical_only and matches_by_method["setpoint_id"] > 0:
        best_method = "setpoint_id"
        best_fn = correlate_proxy_by_setpoint_id
        selection = "logical_setpoint_id_exact"
    _clear_proxy_correlation(setpoints)
    matched, total = best_fn(setpoints, rows)
    info = {
        "logical_ids_embedded": bool(logical_only),
        "selection": selection,
        "matches_by_method": matches_by_method,
    }
    return matched, total, best_method, info


def merge_proxy_artifacts(
    setpoints: list[SetpointRecord],
    proxy_forward_rows: list[dict[str, Any]],
    proxy_reverse_rows: list[dict[str, Any]],
    nominal_period_ms: float,
    t_start_ns: Optional[int] = None,
    t_active_end_ns: Optional[int] = None,
    post_stream_s: float = 30.0,
    t_end_ns: Optional[int] = None,  # legacy alias for t_active_end_ns
    run_dir: Optional[Any] = None,
    successful_send_calls: Optional[int] = None,
) -> tuple[list[SetpointRecord], list[AoISample], dict[str, Any]]:
    """Post-run merge: proxy CSV + setpoints -> correlated records + AoI series."""
    if t_start_ns is None and setpoints:
        t_start_ns = setpoints[0].t_generation_gcs_ns
    if t_active_end_ns is None:
        t_active_end_ns = t_end_ns
    if t_active_end_ns is None and setpoints:
        t_active_end_ns = setpoints[-1].t_generation_gcs_ns + int(nominal_period_ms * 1e6)
    if t_start_ns is None or t_active_end_ns is None:
        return setpoints, [], {"reason": "missing measurement window"}

    active_sps = [
        sp for sp in setpoints
        if t_start_ns <= sp.t_generation_gcs_ns <= t_active_end_ns
    ]
    send_margin_ns = int(1.5 * 1e9)
    stream_t_min = min(sp.t_send_gcs_ns for sp in active_sps) - send_margin_ns if active_sps else t_start_ns
    stream_t_max = max(sp.t_send_gcs_ns for sp in active_sps) + send_margin_ns if active_sps else t_active_end_ns

    logical_only = logical_ids_embedded(setpoints, proxy_forward_rows)
    logical_rows = drop_preposition_frames(proxy_forward_rows) if logical_only else proxy_forward_rows
    matched, n_proxy, corr_method, corr_info = _pick_proxy_correlation(
        active_sps, proxy_forward_rows, logical_only=logical_only,
    )
    meta: dict[str, Any] = {
        "correlation_method": "%s+accept_xyz" % corr_method,
        "correlation_selection": corr_info,
        "proxy_forward_matched": matched,
        "proxy_forward_total": n_proxy,
    }
    meta["acceptance_matched"] = correlate_acceptance_from_reverse(setpoints, proxy_reverse_rows)

    active_arrivals = _setpoint_arrivals(logical_rows, stream_t_min, stream_t_max)
    all_forward_sp = _setpoint_arrivals(proxy_forward_rows)
    preposition_frames = len(all_forward_sp) - len(_setpoint_arrivals(logical_rows))
    generated_setpoints = len(active_sps)
    if successful_send_calls is None:
        successful_send_calls = generated_setpoints
    unique_setpoints_seen_by_proxy = len(active_arrivals)
    proxy_forward_setpoint_frames = len(all_forward_sp)
    correlated_setpoints = sum(
        1 for sp in active_sps
        if sp.t_arrival_uav_proxy_ns is not None and sp.correlation_confidence
    )
    uncorrelated_setpoints = max(0, generated_setpoints - correlated_setpoints)
    accepted_setpoints = sum(1 for sp in active_sps if sp.t_target_observed_ns is not None)

    seen_ids: set[int] = set()
    duplicate_ids: list[int] = []
    missing_ids = [
        sp.logical_setpoint_id for sp in active_sps if sp.t_arrival_uav_proxy_ns is None
    ]
    for sp in active_sps:
        if sp.t_arrival_uav_proxy_ns is None:
            continue
        if sp.logical_setpoint_id in seen_ids:
            duplicate_ids.append(sp.logical_setpoint_id)
        seen_ids.add(sp.logical_setpoint_id)

    proxy_duplicate_setpoint_frames = max(0, unique_setpoints_seen_by_proxy - correlated_setpoints)

    delivery_ratio = (
        (correlated_setpoints / generated_setpoints) if generated_setpoints else None
    )
    capture_ratio = (
        (unique_setpoints_seen_by_proxy / successful_send_calls)
        if successful_send_calls else None
    )
    correlation_ratio = (
        (correlated_setpoints / unique_setpoints_seen_by_proxy)
        if unique_setpoints_seen_by_proxy else None
    )

    accounting = {
        "generated_setpoints": generated_setpoints,
        "successful_send_calls": successful_send_calls,
        "failed_send_calls": None,
        "unique_setpoints_seen_by_proxy": unique_setpoints_seen_by_proxy,
        "proxy_forward_setpoint_frames": proxy_forward_setpoint_frames,
        "proxy_forward_preposition_frames_excluded": preposition_frames,
        "proxy_duplicate_setpoint_frames": proxy_duplicate_setpoint_frames,
        "correlated_setpoints": correlated_setpoints,
        "uncorrelated_setpoints": uncorrelated_setpoints,
        "accepted_setpoints": accepted_setpoints,
        "delivery_ratio_denominator": "generated_setpoints",
        "capture_ratio_denominator": "successful_send_calls",
        "correlation_ratio_denominator": "unique_setpoints_seen_by_proxy",
    }

    if run_dir is not None and missing_ids:
        miss_path = Path(run_dir) / "missing_setpoint_ids.csv"
        with miss_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["logical_setpoint_id"])
            for sid in missing_ids:
                w.writerow([sid])
    if run_dir is not None and duplicate_ids:
        dup_path = Path(run_dir) / "duplicate_setpoint_ids.csv"
        with dup_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["logical_setpoint_id"])
            for sid in duplicate_ids:
                w.writerow([sid])

    meta["delivery_ratio"] = delivery_ratio
    meta["capture_ratio"] = capture_ratio
    meta["correlation_ratio"] = correlation_ratio
    meta["setpoint_accounting"] = accounting
    meta["n_setpoints_generated_active"] = generated_setpoints
    meta["n_setpoints_arrived_active"] = unique_setpoints_seen_by_proxy
    meta["n_setpoints_delivered_active"] = correlated_setpoints
    meta["metric_definitions"] = METRIC_DEFINITIONS

    # 1.3b: cut continuous AoI at last send / last arrival (no post-stream tail in primary)
    t_last_send = max((int(sp.t_send_gcs_ns) for sp in active_sps), default=int(t_active_end_ns))
    arr_ns = [
        int(sp.t_arrival_uav_proxy_ns)
        for sp in active_sps
        if sp.t_arrival_uav_proxy_ns is not None
    ]
    t_last_arr = max(arr_ns) if arr_ns else None
    # 1.3b: end at last arrival of an active setpoint (no post-arrival tail).
    # If nothing arrived, fall back to last send. Never extend past active_end.
    if t_last_arr is not None:
        t_aoi_end = min(int(t_active_end_ns), t_last_arr)
    else:
        t_aoi_end = min(int(t_active_end_ns), t_last_send)
    aoi_cut_meta = {
        "t_aoi_end_ns": t_aoi_end,
        "t_active_end_ns_original": int(t_active_end_ns),
        "t_last_send_ns": t_last_send,
        "t_last_arrival_ns": t_last_arr,
        "cut_reason": (
            "min(active_end, last_arrival) if there is an arrival; "
            "otherwise min(active_end, last_send)"
        ),
    }

    aoi_active = build_aoi_timeseries(
        setpoints, t_aoi_end, t_start_ns, 100.0, nominal_period_ms
    )
    proxy_delays = []
    for row in proxy_forward_rows:
        if row.get("mavlink_msg_type") != "SET_POSITION_TARGET_LOCAL_NED":
            continue
        try:
            proxy_delays.append((int(row["t_forward_ns"]) - int(row["t_arrival_ns"])) / 1e6)
        except (KeyError, TypeError, ValueError):
            pass

    active_summary = build_freshness_summary(
        active_sps, aoi_active, proxy_delays, nominal_period_ms
    )
    # Arrival-path continuous AoI (cut). Keep aoi_network_ms key inside active_summary
    # for august readers of that nested object; also expose aoi_arrival_ms explicitly.
    aoi_arrival_stats = active_summary.get("aoi_network_ms")
    miss_aoi_arrival = active_summary.get("miss_ratio_aoi_network")

    post_end_ns = t_active_end_ns + int(max(0.0, post_stream_s) * 1e9)
    aoi_post = build_aoi_timeseries(
        setpoints, post_end_ns, t_active_end_ns, 100.0, nominal_period_ms
    )
    post_summary = build_freshness_summary(
        setpoints, aoi_post, proxy_delays, nominal_period_ms
    )

    combined: dict[str, Any] = {
        "schema_version": "freshness_v2",
        "active_window_start_ns": t_start_ns,
        "active_window_end_ns": t_active_end_ns,
        "active_window_duration_s": round((t_active_end_ns - t_start_ns) / 1e9, 3),
        "active_samples": len(aoi_active),
        "post_stream_samples": len(aoi_post),
        "post_stream_included_in_primary_stats": False,
        "aoi_arrival_cut": aoi_cut_meta,
        "measurement_window_ns": {
            "t_start_ns": t_start_ns,
            "t_active_end_ns": t_active_end_ns,
            "t_aoi_end_ns": t_aoi_end,
            "post_stream_end_ns": post_end_ns,
            "trajectory_duration_s": round((t_active_end_ns - t_start_ns) / 1e9, 3),
        },
        "n_setpoints": len(setpoints),
        "delivery_ratio": delivery_ratio,
        "capture_ratio": capture_ratio,
        "correlation_ratio": correlation_ratio,
        "proxy_match_ratio": correlation_ratio,
        "correlation_method": meta["correlation_method"],
        "setpoint_accounting": accounting,
        "n_setpoints_generated_active": generated_setpoints,
        "n_setpoints_arrived_active": unique_setpoints_seen_by_proxy,
        "n_setpoints_delivered_active": correlated_setpoints,
        "active_stream": active_summary,
        "post_stream": {
            "duration_s": post_stream_s,
            "behavior": {
                "aoi_network_ms": post_summary.get("aoi_network_ms"),
                "miss_ratio_network": post_summary.get("miss_ratio_network"),
                "stale_intervals_ms": post_summary.get("stale_intervals_ms"),
                "n_aoi_samples": post_summary.get("n_aoi_samples"),
                "note": "observation only — not included in primary Gate A metrics",
            },
        },
        # Continuous arrival AoI (cut). Not an alias of generation-based aoi_network_ms.
        "aoi_arrival_ms": aoi_arrival_stats,
        "miss_ratio_aoi_arrival": miss_aoi_arrival,
        # Legacy key kept for gates that still read aoi_network_ms from this merge path:
        # after arrival capture this nested value is arrival-based+cut (same as aoi_arrival_ms).
        # Orchestrate overwrites top-level aoi_network_ms with generation-based stats (M8).
        "aoi_network_ms": aoi_arrival_stats,
        "miss_ratio_network": active_summary.get("miss_ratio_network"),
        "miss_ratio_aoi_network": miss_aoi_arrival,
        "metric_definitions": METRIC_DEFINITIONS,
        "stale_intervals_ms": active_summary.get("stale_intervals_ms"),
        "network_arrival_delay_ms": active_summary.get("network_arrival_delay_ms"),
        "inter_arrival_gap_ms": active_summary.get("inter_arrival_gap_ms"),
        "proxy_delay_ms": active_summary.get("proxy_delay_ms"),
        "metric_nature_network_arrival_delay": "owd",
        "errata_aoi_alias": (
            "Plan item M8: aoi_arrival_ms is the cut arrival-path continuous AoI; "
            "aoi_network_ms at top-level after orchestrate merge is generation-based; "
            "do not treat aoi_arrival_ms as a silent alias of aoi_network_ms."
        ),
    }
    meta["freshness_summary"] = combined
    meta["aoi_arrival_cut"] = aoi_cut_meta
    return setpoints, aoi_active, meta


def validate_freshness_integrity(
    fresh: dict[str, Any],
    merge_meta: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Sanity gate: active window, non-null ratios, no impossible AoI samples."""
    info: dict[str, Any] = {}
    merge_meta = merge_meta or {}
    fs = fresh if fresh.get("schema_version") else fresh.get("freshness_summary") or fresh
    t0 = fs.get("active_window_start_ns") or fs.get("measurement_window_ns", {}).get("t_start_ns")
    t1 = fs.get("active_window_end_ns") or fs.get("measurement_window_ns", {}).get("t_active_end_ns")
    info["active_window_start_ns"] = t0
    info["active_window_end_ns"] = t1
    if t0 is None or t1 is None or int(t1) <= int(t0):
        info["reason"] = "invalid active measurement window"
        return False, info
    info["active_window_duration_s"] = round((int(t1) - int(t0)) / 1e9, 3)

    for key in ("delivery_ratio", "capture_ratio", "correlation_ratio"):
        val = merge_meta.get(key) if merge_meta.get(key) is not None else fs.get(key)
        info[key] = val
        if val is None:
            info["reason"] = "%s is null (denominator invalid)" % key
            return False, info

    aoi = fs.get("aoi_network_ms") or {}
    for p in ("p50", "p99", "p99_9", "max"):
        v = aoi.get(p)
        if v is not None and float(v) < 0:
            info["reason"] = "negative AoI %s=%s" % (p, v)
            return False, info
        if v is not None and float(v) > 60_000:
            info["reason"] = "impossible AoI %s=%s ms" % (p, v)
            return False, info

    info["ok"] = True
    return True, info


def _summary_stats(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "p99_9": None, "max": None, "mean": None, "n": 0}
    s = sorted(values)
    n = len(s)
    return {
        "p50": round(percentile(s, 50), 3),
        "p95": round(percentile(s, 95), 3),
        "p99": round(percentile(s, 99), 3),
        "p99_9": round(percentile(s, 99.9), 3),
        "max": round(float(s[-1]), 3),
        "mean": round(sum(s) / n, 3),
        "n": n,
    }


def _telemetry_row_int(row: dict[str, str], key: str) -> Optional[int]:
    val = row.get(key)
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _telemetry_uav_lookup_key(row: dict[str, str]) -> Optional[tuple[int, int]]:
    sys_id = _telemetry_row_int(row, "mavlink_system_id")
    boot = _telemetry_row_int(row, "time_boot_ms")
    if sys_id is not None and boot is not None:
        return (sys_id, boot)
    seq = _telemetry_row_int(row, "mavlink_sequence")
    if sys_id is not None and seq is not None:
        return (sys_id, seq)
    return None


def _telemetry_row_xyz(row: dict[str, str]) -> Optional[tuple[float, float, float]]:
    try:
        x = float(row.get("x") or "")
        y = float(row.get("y") or "")
        z = float(row.get("z") or "")
    except (TypeError, ValueError):
        return None
    return (x, y, z)


def _xyz_dist_m(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _latest_uav_xyz_at_or_before(
    uav_timeline: list[tuple[int, tuple[float, float, float]]],
    t_mono_ns: int,
) -> Optional[tuple[float, float, float]]:
    """Latest SITL-egress NED with t_uav_mono <= t_mono_ns (binary search)."""
    if not uav_timeline:
        return None
    lo, hi = 0, len(uav_timeline) - 1
    best: Optional[tuple[float, float, float]] = None
    while lo <= hi:
        mid = (lo + hi) // 2
        t_u, xyz = uav_timeline[mid]
        if t_u <= t_mono_ns:
            best = xyz
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def compute_trk_truth(
    run_dir: Path,
    *,
    clock_offset: Optional[dict[str, Any]] = None,
    max_join_gap_ms: float = 500.0,
) -> dict[str, Any]:
    """SITL ground-truth tracking error vs planned setpoint (report-only).

    For each row in setpoints.csv, take (x_ref,y_ref,z_ref) at t_generation_gcs_ns
    and the latest c2_telemetry_uav.csv NED with t_uav_mono ≤ t_gen (same host
    CLOCK_MONOTONIC assumption as belief_vs_truth). Distance is 3D Euclidean —
    same definition as tracking_error_m, but position comes from UAV egress
    (truth) instead of GCS-received LOCAL_POSITION_NED (trk_err / hallucination).

    Never feeds VALID/DAMAGE gates.
    """
    out: dict[str, Any] = {
        "schema_version": "trk_truth_v1",
        "ok": False,
        "note": (
            "REPORT ONLY — SITL egress NED vs planned setpoint ref; "
            "complements trk_err (GCS belief vs plan). Not scored by VALID/DAMAGE."
        ),
    }
    run_dir = Path(run_dir)
    sp_path = run_dir / "setpoints.csv"
    uav_path = run_dir / "c2_telemetry_uav.csv"
    if not sp_path.is_file():
        out["reason"] = "setpoints.csv missing"
        return out
    if not uav_path.is_file():
        out["reason"] = "c2_telemetry_uav.csv missing (need --telemetry-arrival-capture)"
        return out

    uav_timeline: list[tuple[int, tuple[float, float, float]]] = []
    with uav_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t_uav = _telemetry_row_int(row, "t_mono_ns")
            xyz = _telemetry_row_xyz(row)
            if t_uav is None or xyz is None:
                continue
            uav_timeline.append((t_uav, xyz))
    uav_timeline.sort(key=lambda p: p[0])
    if not uav_timeline:
        out["reason"] = "c2_telemetry_uav.csv has no usable NED rows"
        return out

    # Optional: convert GCS mono → UAV mono using measured uav−gcs offset.
    # Cross block is named uav_minus_gcs but equals (host−uav)−(host−gcs)=gcs−uav.
    offset_ns = 0
    if isinstance(clock_offset, dict) and clock_offset.get("ok"):
        cross = clock_offset.get("uav_minus_gcs_ms") or {}
        med = cross.get("median_ms")
        if med is not None:
            # t_uav ≈ t_gcs − (gcs−uav) = t_gcs − med_ms
            offset_ns = int(float(med) * 1e6)
            out["clock_offset_applied_ns"] = offset_ns
            out["clock_offset_median_ms"] = med

    all_err: list[float] = []
    steady_err: list[float] = []
    xy_err: list[float] = []
    joined = 0
    skipped_gap = 0
    skipped_ref = 0
    max_gap_ns = int(max_join_gap_ms * 1e6)

    with sp_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                x_ref = float(row["x_ref"])
                y_ref = float(row["y_ref"])
                z_ref = float(row.get("z_ref") or 0.0)
            except (KeyError, TypeError, ValueError):
                skipped_ref += 1
                continue
            t_gen = _telemetry_row_int(row, "t_generation_gcs_ns")
            if t_gen is None:
                t_gen = _telemetry_row_int(row, "t_send_gcs_ns")
            if t_gen is None:
                skipped_ref += 1
                continue
            t_query = t_gen - offset_ns
            truth = _latest_uav_xyz_at_or_before(uav_timeline, t_query)
            if truth is None:
                skipped_gap += 1
                continue
            # Bound join freshness: find exact t of chosen sample via search
            # (re-scan nearest for gap check — timeline is sorted).
            lo, hi = 0, len(uav_timeline) - 1
            best_t = None
            while lo <= hi:
                mid = (lo + hi) // 2
                t_u, _ = uav_timeline[mid]
                if t_u <= t_query:
                    best_t = t_u
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best_t is None or (t_query - best_t) > max_gap_ns:
                skipped_gap += 1
                continue
            err3 = _xyz_dist_m(truth, (x_ref, y_ref, z_ref))
            err_xy = math.hypot(truth[0] - x_ref, truth[1] - y_ref)
            all_err.append(err3)
            xy_err.append(err_xy)
            joined += 1
            if (row.get("trajectory_window") or "").strip() == "steady":
                steady_err.append(err3)

    out["ned_samples_uav"] = len(uav_timeline)
    out["setpoints_joined"] = joined
    out["setpoints_skipped_ref"] = skipped_ref
    out["setpoints_skipped_gap"] = skipped_gap
    out["max_join_gap_ms"] = max_join_gap_ms
    out["tracking_error_truth_m"] = _summary_stats(all_err)
    # Align key names with flight_state tracking_error_m (median alias).
    if out["tracking_error_truth_m"].get("p50") is not None:
        out["tracking_error_truth_m"]["median"] = out["tracking_error_truth_m"]["p50"]
    out["tracking_error_truth_xy_m"] = _summary_stats(xy_err)
    if out["tracking_error_truth_xy_m"].get("p50") is not None:
        out["tracking_error_truth_xy_m"]["median"] = out["tracking_error_truth_xy_m"]["p50"]
    out["steady_tracking_error_truth_m"] = _summary_stats(steady_err)
    if out["steady_tracking_error_truth_m"].get("p50") is not None:
        out["steady_tracking_error_truth_m"]["median"] = out["steady_tracking_error_truth_m"]["p50"]
    out["ok"] = joined > 0
    if not out["ok"]:
        out["reason"] = "no setpoint↔UAV joins within max_join_gap_ms"
    out["metric_definitions"] = {
        "tracking_error_truth_m": (
            "||pos_SITL_egress − pos_planned|| (m) at each setpoint generation; "
            "truth = latest c2_telemetry_uav NED with t_uav ≤ t_generation_gcs. "
            "Compare to flight_state.tracking_error_m (trk_err = GCS belief vs plan)."
        ),
        "steady_tracking_error_truth_m": (
            "Same as tracking_error_truth_m restricted to trajectory_window=steady."
        ),
    }
    return out


def merge_telemetry_downlink(
    run_dir: Path,
    *,
    nominal_hz: float = 20.0,
    active_duration_s: float = 45.0,
) -> dict[str, Any]:
    """Merge UAV egress + GCS arrival LOCAL_POSITION_NED captures (downlink timeliness)."""
    meta: dict[str, Any] = {"ok": False}
    gcs_path = run_dir / "telemetry_downlink_gcs.csv"
    uav_path = run_dir / "c2_telemetry_uav.csv"
    if not gcs_path.is_file():
        meta["reason"] = "telemetry_downlink_gcs.csv missing"
        return meta

    with gcs_path.open(encoding="utf-8") as f:
        gcs_rows = list(csv.DictReader(f))
    if not gcs_rows:
        meta["reason"] = "telemetry_downlink_gcs.csv empty"
        return meta

    # Same-message match (delay diagnostic) + SITL-egress timeline (belief truth).
    uav_by_key: dict[tuple[int, int], tuple[int, Optional[tuple[float, float, float]]]] = {}
    uav_timeline: list[tuple[int, tuple[float, float, float]]] = []
    n_uav_rows = 0
    if uav_path.is_file():
        with uav_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = _telemetry_uav_lookup_key(row)
                t_uav = _telemetry_row_int(row, "t_mono_ns")
                xyz = _telemetry_row_xyz(row)
                if key is None or t_uav is None:
                    continue
                uav_by_key[key] = (t_uav, xyz)
                n_uav_rows += 1
                if xyz is not None:
                    uav_timeline.append((t_uav, xyz))
    uav_timeline.sort(key=lambda p: p[0])

    delays_ms: list[float] = []
    gaps_ms: list[float] = []
    belief_m: list[float] = []
    prev_t: Optional[int] = None
    matched = 0
    gcs_parsed: list[tuple[int, tuple[float, float, float]]] = []
    for row in gcs_rows:
        t_gcs = _telemetry_row_int(row, "t_mono_ns")
        if t_gcs is None:
            continue
        if prev_t is not None:
            gaps_ms.append((t_gcs - prev_t) / 1e6)
        prev_t = t_gcs
        key = _telemetry_uav_lookup_key(row)
        if key is not None:
            uav_hit = uav_by_key.get(key)
            if uav_hit is not None:
                t_uav, _uav_xyz = uav_hit
                delays_ms.append((t_gcs - t_uav) / 1e6)
                matched += 1
        gcs_xyz = _telemetry_row_xyz(row)
        if gcs_xyz is not None:
            gcs_parsed.append((t_gcs, gcs_xyz))

    # Belief = last NED GCS still holds; truth = latest SITL-egress NED with
    # t_uav_mono <= t_gcs_arrival (vehicle has moved while belief was frozen).
    # Post-hoc / report only — not used by VALID or DAMAGE gates.
    for i in range(1, len(gcs_parsed)):
        t_arrive, _ = gcs_parsed[i]
        _t_prev, belief_xyz = gcs_parsed[i - 1]
        truth_xyz = _latest_uav_xyz_at_or_before(uav_timeline, t_arrive)
        if truth_xyz is None:
            continue
        belief_m.append(_xyz_dist_m(belief_xyz, truth_xyz))

    expected = max(1, int(active_duration_s * nominal_hz))
    capture_ratio = len(gcs_rows) / expected if expected else None
    uav_egress_ratio = len(gcs_rows) / n_uav_rows if n_uav_rows else None
    summary = {
        "schema_version": "telemetry_downlink_v2",
        "message": "LOCAL_POSITION_NED",
        "nominal_hz": nominal_hz,
        "ned_samples_gcs": len(gcs_rows),
        "ned_samples_uav": n_uav_rows,
        "ned_samples_expected_approx": expected,
        "capture_ratio": round(capture_ratio, 4) if capture_ratio is not None else None,
        "uav_to_gcs_delivery_ratio": round(uav_egress_ratio, 4) if uav_egress_ratio is not None else None,
        "matched_uav_gcs": matched,
        "belief_matched": len(belief_m),
        "belief_vs_truth_m": _summary_stats(belief_m),
        "telemetry_arrival_delay_ms": _summary_stats(delays_ms),
        "inter_arrival_gap_ms": _summary_stats(gaps_ms),
        "miss_ratio_delay": {
            str(d): round(miss_ratio(delays_ms, float(d)), 4) for d in (50, 100, 150)
        } if delays_ms else {},
        "miss_ratio_gap": {
            str(d): round(miss_ratio(gaps_ms, float(d)), 4) for d in (50, 100, 150)
        } if gaps_ms else {},
        "metric_definitions": {
            "telemetry_arrival_delay_ms": "One-way downlink delay: t_arrival_gcs − t_egress_uav (ms), matched by (system_id, time_boot_ms) or sequence. Diagnostic only.",
            "inter_arrival_gap_ms": "Gap between consecutive LOCAL_POSITION_NED arrivals at GCS (ms).",
            "capture_ratio": "ned_samples_gcs / (nominal_hz × active_duration_s).",
            "uav_to_gcs_delivery_ratio": "ned_samples_gcs / ned_samples_uav (5G downlink delivery of NED).",
            "belief_vs_truth_m": (
                "At each GCS NED arrival i: ||pos_belief − pos_truth|| (m), where belief is "
                "the previous GCS sample (frozen GCS state) and truth is the latest "
                "SITL-egress NED in c2_telemetry_uav.csv with t_uav_mono ≤ t_gcs_arrival. "
                "Report-only; not scored by VALID/DAMAGE. Requires comparable pod CLOCK_MONOTONIC."
            ),
        },
    }
    summary["belief_validation"] = validate_belief_vs_truth(
        summary,
        speed_mps=estimate_horizontal_speed_mps(uav_timeline),
        clock_offset=None,
        trajectory_diameter_m=estimate_trajectory_diameter_m(run_dir),
    )
    out_path = run_dir / "telemetry_downlink_summary.json"
    out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    meta["ok"] = True
    meta["telemetry_downlink_summary"] = summary
    return meta


# --- belief_vs_truth integrity (report-only; never feeds VALID/DAMAGE) ---

# Systematic |UAV−GCS| median above this → clock check fails.
BELIEF_CLOCK_SKEW_MAX_MS = 100.0
# kubectl-exec jitter (p95−p5) above this → clock check inconclusive/fail.
BELIEF_CLOCK_JITTER_MAX_MS = 250.0
# Legacy gap-anchored path: belief_p50 > max(floor, factor × gap_p50_s × speed).
BELIEF_PHYS_FACTOR = 3.0
BELIEF_PHYS_ABS_FLOOR_M = 2.0
# AoI-anchored path: belief must be ≤ physical max displacement over the belief age
# (min(speed × age, trajectory diameter)). Slack for measurement/jitter noise.
BELIEF_GEOM_TOL = 1.15


def estimate_trajectory_diameter_m(run_dir: Path) -> Optional[float]:
    """Geometric extent (max pairwise distance) of the commanded reference path (m).

    Upper-bounds the Euclidean belief_vs_truth on any bounded trajectory, since both
    belief and truth are points on that path. Read from setpoints.csv (x_ref, y_ref).
    """
    path = Path(run_dir) / "setpoints.csv"
    if not path.is_file():
        return None
    pts: list[tuple[float, float]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    x = float(row.get("x_ref") or "")
                    y = float(row.get("y_ref") or "")
                except (TypeError, ValueError):
                    continue
                pts.append((x, y))
    except OSError:
        return None
    if len(pts) < 2:
        return None
    if len(pts) > 400:
        step = len(pts) // 400
        pts = pts[::step]
    diameter = 0.0
    for i in range(len(pts)):
        xi, yi = pts[i]
        for j in range(i + 1, len(pts)):
            d = math.hypot(xi - pts[j][0], yi - pts[j][1])
            if d > diameter:
                diameter = d
    return diameter if diameter > 0.0 else None


def estimate_horizontal_speed_mps(
    timeline: list[tuple[int, tuple[float, float, float]]],
) -> Optional[float]:
    """Median horizontal speed (m/s) while moving (ignores hover/near-zero samples)."""
    speeds: list[float] = []
    for (t0, p0), (t1, p1) in zip(timeline, timeline[1:]):
        dt = (t1 - t0) / 1e9
        if dt <= 1e-3 or dt > 2.0:
            continue
        dist = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        speeds.append(dist / dt)
    moving = [s for s in speeds if s >= 0.5]
    if len(moving) >= 5:
        moving.sort()
        return float(moving[len(moving) // 2])
    if len(speeds) < 5:
        return None
    speeds.sort()
    return float(speeds[len(speeds) // 2])


def estimate_speed_mps_from_setpoints(run_dir: Path) -> Optional[float]:
    """Fallback speed from setpoints.csv actual NED (GCS-side)."""
    path = Path(run_dir) / "setpoints.csv"
    if not path.is_file():
        return None
    rows: list[tuple[int, float, float]] = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                t = int(float(row.get("t_send_gcs_ns") or row.get("t_generation_gcs_ns") or 0))
                x = float(row.get("x_actual") or "")
                y = float(row.get("y_actual") or "")
            except (TypeError, ValueError):
                continue
            if t <= 0:
                continue
            rows.append((t, x, y))
    rows.sort(key=lambda r: r[0])
    timeline = [(t, (x, y, 0.0)) for t, x, y in rows]
    return estimate_horizontal_speed_mps(timeline)


def validate_belief_vs_truth(
    tel_summary: dict[str, Any],
    *,
    speed_mps: Optional[float],
    clock_offset: Optional[dict[str, Any]] = None,
    trajectory_diameter_m: Optional[float] = None,
) -> dict[str, Any]:
    """Cross-check belief_vs_truth_m against its physical staleness and clock offset.

    The belief is the *previous* GCS sample, so its underlying reality is
    ``delivery AoI + one inter-arrival gap`` old. The Euclidean displacement it can
    accrue over that age is bounded by ``min(speed × age, trajectory diameter)``.
    When the delivery AoI is available we anchor on it (correct staleness); otherwise
    we fall back to the legacy inter-arrival-gap anchor.

    Report-only. Does not affect VALID/DAMAGE.
    """
    belief = tel_summary.get("belief_vs_truth_m") or {}
    gap = tel_summary.get("inter_arrival_gap_ms") or {}
    aoi = tel_summary.get("telemetry_arrival_delay_ms") or {}
    belief_p50 = belief.get("p50")
    belief_p99 = belief.get("p99")
    gap_p50 = gap.get("p50")
    gap_p99 = gap.get("p99")
    aoi_p50 = aoi.get("p50")
    aoi_p99 = aoi.get("p99")
    n_belief = int(tel_summary.get("belief_matched") or 0)
    has_diameter = isinstance(trajectory_diameter_m, (int, float)) and trajectory_diameter_m > 0

    out: dict[str, Any] = {
        "status": "unavailable",
        "physical_consistent": None,
        "clock_ok": None,
        "speed_mps": round(float(speed_mps), 3) if isinstance(speed_mps, (int, float)) else None,
        "belief_p50_m": belief_p50,
        "belief_p99_m": belief_p99,
        "gap_p50_ms": gap_p50,
        "gap_p99_ms": gap_p99,
        "aoi_p50_ms": aoi_p50,
        "aoi_p99_ms": aoi_p99,
        "belief_age_p50_s": None,
        "belief_age_p99_s": None,
        "trajectory_diameter_m": round(float(trajectory_diameter_m), 3) if has_diameter else None,
        "anchor": None,
        "belief_expected_p50_m": None,
        "belief_expected_p99_m": None,
        "clock_skew_abs_median_ms": None,
        "clock_jitter_spread_ms": None,
        "thresholds": {
            "clock_skew_max_ms": BELIEF_CLOCK_SKEW_MAX_MS,
            "clock_jitter_max_ms": BELIEF_CLOCK_JITTER_MAX_MS,
            "phys_factor": BELIEF_PHYS_FACTOR,
            "phys_abs_floor_m": BELIEF_PHYS_ABS_FLOOR_M,
            "geom_tol": BELIEF_GEOM_TOL,
        },
        "reason": "",
        "appendix_line": None,
    }

    if n_belief < 2 or not isinstance(belief_p50, (int, float)):
        out["reason"] = "belief_vs_truth_m unavailable (need GCS+UAV NED series)"
        return out

    # --- Part 2: physical consistency (decisive). ---
    # Belief age ≈ delivery AoI + one inter-arrival gap (belief is the previous
    # GCS sample). Max displacement over that age = min(speed × age, diameter).
    phys_ok: Optional[bool] = None
    phys_reason = ""
    speed_ok = isinstance(speed_mps, (int, float)) and speed_mps > 0.1
    if speed_ok and isinstance(aoi_p50, (int, float)):
        out["anchor"] = "delivery_AoI+gap"
        g50 = float(gap_p50) if isinstance(gap_p50, (int, float)) else 0.0
        g99 = float(gap_p99) if isinstance(gap_p99, (int, float)) else g50
        age50 = (float(aoi_p50) + g50) / 1000.0
        arc50 = age50 * float(speed_mps)
        ceil50 = min(arc50, float(trajectory_diameter_m)) if has_diameter else arc50
        out["belief_age_p50_s"] = round(age50, 3)
        out["belief_expected_p50_m"] = round(ceil50, 3)
        limit50 = max(BELIEF_PHYS_ABS_FLOOR_M, BELIEF_GEOM_TOL * ceil50)
        phys_ok = float(belief_p50) <= limit50
        if isinstance(aoi_p99, (int, float)):
            age99 = (float(aoi_p99) + g99) / 1000.0
            arc99 = age99 * float(speed_mps)
            ceil99 = min(arc99, float(trajectory_diameter_m)) if has_diameter else arc99
            out["belief_age_p99_s"] = round(age99, 3)
            out["belief_expected_p99_m"] = round(ceil99, 3)
            if phys_ok and isinstance(belief_p99, (int, float)):
                limit99 = max(BELIEF_PHYS_ABS_FLOOR_M, BELIEF_GEOM_TOL * ceil99)
                if float(belief_p99) > limit99:
                    phys_ok = False
        cap = (
            f"capped by trajectory Ø {float(trajectory_diameter_m):.1f} m"
            if has_diameter else "no geometric cap"
        )
        if phys_ok:
            phys_reason = (
                f"belief p50={belief_p50}m ≤ physical max {ceil50:.2f}m "
                f"(belief age {age50:.2f}s × {speed_mps:.2f} m/s, {cap})"
            )
        else:
            phys_reason = (
                f"FLAG: belief p50={belief_p50}m exceeds physical max {ceil50:.2f}m "
                f"(belief age {age50:.2f}s × speed, {cap}) — likely skew/artifact"
            )
    elif speed_ok and isinstance(gap_p50, (int, float)):
        # Legacy fallback: no delivery AoI → approximate age by inter-arrival gap.
        out["anchor"] = "inter_arrival_gap"
        exp50 = (float(gap_p50) / 1000.0) * float(speed_mps)
        exp99 = (
            (float(gap_p99) / 1000.0) * float(speed_mps)
            if isinstance(gap_p99, (int, float)) else None
        )
        out["belief_expected_p50_m"] = round(exp50, 3)
        if exp99 is not None:
            out["belief_expected_p99_m"] = round(exp99, 3)
        limit50 = max(BELIEF_PHYS_ABS_FLOOR_M, BELIEF_PHYS_FACTOR * exp50)
        phys_ok = float(belief_p50) <= limit50
        if (
            phys_ok
            and exp99 is not None
            and isinstance(belief_p99, (int, float))
        ):
            limit99 = max(BELIEF_PHYS_ABS_FLOOR_M, BELIEF_PHYS_FACTOR * exp99)
            if float(belief_p99) > limit99:
                phys_ok = False
        if phys_ok:
            phys_reason = (
                f"belief p50={belief_p50}m ≈ gap×speed expected {exp50:.2f}m "
                f"(limit {limit50:.2f}m @ {speed_mps:.2f} m/s)"
            )
        else:
            phys_reason = (
                f"FLAG: belief p50={belief_p50}m >> gap×speed expected {exp50:.2f}m "
                f"(limit {limit50:.2f}m) — likely skew/artifact"
            )
    else:
        phys_reason = "physical check skipped (need speed_mps and delivery AoI or gap p50)"
    out["physical_consistent"] = phys_ok

    # --- Part 1: clock offset (optional; best-effort from probe). ---
    clock_ok: Optional[bool] = None
    clock_reason = ""
    if isinstance(clock_offset, dict) and clock_offset.get("ok"):
        skew = clock_offset.get("skew_abs_median_ms")
        jitter = clock_offset.get("jitter_spread_ms")
        out["clock_skew_abs_median_ms"] = skew
        out["clock_jitter_spread_ms"] = jitter
        out["clock_appendix_line"] = clock_offset.get("appendix_line")
        if isinstance(skew, (int, float)) and isinstance(jitter, (int, float)):
            clock_ok = (
                float(skew) <= BELIEF_CLOCK_SKEW_MAX_MS
                and float(jitter) <= BELIEF_CLOCK_JITTER_MAX_MS
            )
            if clock_ok:
                clock_reason = (
                    f"clock offset |median|={skew} ms, spread={jitter} ms "
                    f"(≤ {BELIEF_CLOCK_SKEW_MAX_MS}/{BELIEF_CLOCK_JITTER_MAX_MS} ms)"
                )
            else:
                clock_reason = (
                    f"FLAG: clock offset |median|={skew} ms or spread={jitter} ms "
                    f"exceeds thresholds"
                )
        else:
            clock_reason = "clock probe incomplete stats"
    else:
        clock_reason = "clock offset not measured"
    out["clock_ok"] = clock_ok

    # --- Part 3: status ---
    if phys_ok is False or clock_ok is False:
        out["status"] = "diagnostic_only"
        parts = [p for p in (phys_reason, clock_reason) if p]
        out["reason"] = "; ".join(parts)
        out["appendix_line"] = (
            "belief_vs_truth_m: DIAGNOSTIC ONLY — failed integrity check "
            f"({out['reason']})"
        )
    elif phys_ok is True and clock_ok is True:
        out["status"] = "validated"
        out["reason"] = f"validated ({clock_reason}; {phys_reason})"
        if out.get("anchor") == "delivery_AoI+gap":
            out["appendix_line"] = (
                f"belief_vs_truth_m validated (clock offset {out.get('clock_skew_abs_median_ms')} ms; "
                f"belief age ≈ {out.get('belief_age_p50_s')} s from delivery AoI, physically "
                f"consistent — p50 {belief_p50} m ≤ geometric max {out.get('belief_expected_p50_m')} m)"
            )
        else:
            out["appendix_line"] = (
                f"belief_vs_truth_m validated (clock offset {out.get('clock_skew_abs_median_ms')} ms; "
                f"physically consistent with telemetry gap × speed ≈ "
                f"{out.get('belief_expected_p50_m')} m)"
            )
    elif phys_ok is True and clock_ok is None:
        out["status"] = "validated_physical_only"
        out["reason"] = (
            f"physical OK ({phys_reason}); clock offset not measured — "
            "treat as provisional"
        )
        anchor_desc = (
            "delivery AoI × speed capped by trajectory diameter"
            if out.get("anchor") == "delivery_AoI+gap" else "gap×speed"
        )
        out["appendix_line"] = (
            f"belief_vs_truth_m: physically consistent with {anchor_desc}; "
            "inter-pod CLOCK_MONOTONIC offset not measured this run"
        )
    else:
        out["status"] = "unavailable"
        out["reason"] = "; ".join(p for p in (phys_reason, clock_reason) if p)

    return out


def annotate_belief_validation(
    tel_summary: dict[str, Any],
    run_dir: Path,
    clock_offset: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Attach/replace belief_validation using setpoints speed fallback + clock probe."""
    speed = None
    prev = tel_summary.get("belief_validation") or {}
    cand = prev.get("speed_mps")
    if isinstance(cand, (int, float)) and float(cand) >= 0.5:
        speed = float(cand)
    if speed is None:
        speed = estimate_speed_mps_from_setpoints(run_dir)
    if speed is None or speed < 0.5:
        # Last resort: commanded trajectory speed from mission/stream summary.
        for name in ("mission_summary.json", "four_layer_summary.json", "run_meta.json"):
            path = Path(run_dir) / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for key_path in (
                ("physics_metrics", "trajectory", "speed_mps"),
                ("physics_metrics", "speed_mps"),
                ("trajectory", "speed_mps"),
                ("setpoint_stream", "speed_mps"),
            ):
                cur: Any = data
                ok = True
                for k in key_path:
                    if not isinstance(cur, dict) or k not in cur:
                        ok = False
                        break
                    cur = cur[k]
                if ok and isinstance(cur, (int, float)) and float(cur) >= 0.5:
                    speed = float(cur)
                    break
            if speed is not None:
                break
    validation = validate_belief_vs_truth(
        tel_summary, speed_mps=speed, clock_offset=clock_offset,
        trajectory_diameter_m=estimate_trajectory_diameter_m(run_dir),
    )
    tel_summary["belief_validation"] = validation
    # Keep the metric definition idempotent: strip any prior downgrade suffix
    # before (optionally) re-adding one, so re-annotation never stacks notes or
    # leaves a stale downgrade when a run flips back to validated.
    defs = tel_summary.setdefault("metric_definitions", {})
    base_def = defs.get("belief_vs_truth_m")
    if isinstance(base_def, str):
        base_def = re.sub(r"\s*\[DOWNGRADED to diagnostic:.*$", "", base_def).rstrip()
        defs["belief_vs_truth_m"] = base_def
    # Soften report framing when not validated.
    if validation.get("status") in ("diagnostic_only", "unavailable"):
        defs["belief_vs_truth_m"] = (
            (base_def or "")
            + " [DOWNGRADED to diagnostic: " + str(validation.get("reason") or "") + "]"
        )
    return validation

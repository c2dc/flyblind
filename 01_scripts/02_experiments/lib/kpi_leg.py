#!/usr/bin/env python3
##################################################################################################################################
# Module: kpi_leg.py (Symmetric per-leg KPIs (command | return))                                                                 #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Primary percentile: round_index_on_sorted (same as the August campaign). Secondary percentile: linear             #
#   interpolation (numpy), cross-check only.                                                                                     #
#                                                                                                                                #
# IPDV: samples sorted by seq, |owd[i]-owd[i-1]|. Post-warmup UDP availability is renamed: delivery_rate_post_warmup_pct.        #
##################################################################################################################################
"""Symmetric per-leg KPIs (command | return).

Primary percentile: round_index_on_sorted (same as the August campaign).
Secondary percentile: linear interpolation (numpy), cross-check only.

IPDV: samples sorted by seq, |owd[i]-owd[i-1]|.
Post-warmup UDP availability is renamed: delivery_rate_post_warmup_pct.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

try:
    from kpi_budgets import (
        GAP_THRESHOLDS_S,
        RELIABILITY_TARGET_STICK,
        RELIABILITY_TARGET_VIDEO,
        budgets_for_leg,
    )
except ImportError:  # pragma: no cover
    from lib.kpi_budgets import (  # type: ignore
        GAP_THRESHOLDS_S,
        RELIABILITY_TARGET_STICK,
        RELIABILITY_TARGET_VIDEO,
        budgets_for_leg,
    )

PERCENTILE_METHOD_PRIMARY = "round_index_on_sorted"
PERCENTILE_METHOD_SECONDARY = "numpy_linear_interpolation"

# Minimum n to report p99 / p99.9 without flagging them as insufficient
MIN_N_P99 = 100
MIN_N_P99_9 = 1000

NATURE_OWD = "owd"
NATURE_AOI = "aoi"
NATURE_RTT = "rtt"

LEG_COMMAND = "command"
LEG_RETURN = "return"


def percentile_round_index(values: list[float], p: float) -> Optional[float]:
    """Same rule as owd_probe / freshness_metrics (August campaign)."""
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return float(s[idx])


def percentile_numpy_linear(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    return float(np.percentile(values, p, method="linear"))


def _percentile_block(values: list[float], ps: Iterable[float]) -> dict[str, Any]:
    n = len(values)
    primary: dict[str, Any] = {"n": n, "method": PERCENTILE_METHOD_PRIMARY}
    secondary: dict[str, Any] = {"n": n, "method": PERCENTILE_METHOD_SECONDARY}
    flags: dict[str, bool] = {}
    for p in ps:
        key = _p_key(p)
        insuf = False
        if p >= 99.9 and n < MIN_N_P99_9:
            insuf = True
        elif p >= 99.0 and p < 99.9 and n < MIN_N_P99:
            insuf = True
        flags[key] = insuf
        if insuf:
            primary[key] = None
            secondary[key] = None
        else:
            primary[key] = percentile_round_index(values, p)
            secondary[key] = percentile_numpy_linear(values, p)
    if values:
        primary["max"] = float(max(values))
        primary["min"] = float(min(values))
        secondary["max"] = primary["max"]
        secondary["min"] = primary["min"]
    else:
        primary["max"] = primary["min"] = None
        secondary["max"] = secondary["min"] = None
    return {
        "primary": primary,
        "secondary_check": secondary,
        "percentile_insufficient": flags,
        "any_percentile_insufficient": any(flags.values()),
    }


def _p_key(p: float) -> str:
    if abs(p - 50) < 1e-9:
        return "p50"
    if abs(p - 95) < 1e-9:
        return "p95"
    if abs(p - 99) < 1e-9:
        return "p99"
    if abs(p - 99.9) < 1e-9:
        return "p99_9"
    return "p" + str(p).replace(".", "_")


def compute_ipdv_by_seq(
    samples: list[dict[str, Any]],
    owd_key: str = "owd_s",
) -> dict[str, Any]:
    """Single IPDV: sort by seq, |consecutive owd|. Counts out-of-order arrivals."""
    with_seq = []
    for s in samples:
        if s.get("seq") is None or s.get(owd_key) is None:
            continue
        with_seq.append(s)
    if not with_seq:
        return {
            "ipdv_s": [],
            "out_of_order_count": 0,
            "n_pairs": 0,
        }
    # Arrival order vs seq order
    arrival_order = list(with_seq)  # assumes RX order already if t_rx is present
    if all(s.get("t_rx_mono_ns") is not None for s in with_seq):
        arrival_order = sorted(with_seq, key=lambda s: int(s["t_rx_mono_ns"]))
    seq_along_arrival = [int(s["seq"]) for s in arrival_order]
    fora = 0
    for i in range(1, len(seq_along_arrival)):
        if seq_along_arrival[i] < seq_along_arrival[i - 1]:
            fora += 1

    ordered = sorted(with_seq, key=lambda s: int(s["seq"]))
    ipdv = []
    for i in range(1, len(ordered)):
        ipdv.append(abs(float(ordered[i][owd_key]) - float(ordered[i - 1][owd_key])))
    return {
        "ipdv_s": ipdv,
        "out_of_order_count": fora,
        "n_pairs": len(ipdv),
    }


def loss_by_seq(tx_seqs: Iterable[int], rx_seqs: Iterable[int]) -> dict[str, Any]:
    tx = set(int(x) for x in tx_seqs)
    rx = set(int(x) for x in rx_seqs)
    missing = sorted(tx - rx)
    extra = sorted(rx - tx)
    return {
        "sent": len(tx),
        "unique_received": len(rx & tx),
        "lost": len(missing),
        "loss_frac": (len(missing) / len(tx)) if tx else None,
        "lost_seqs_sample": missing[:50],
        "seqs_extras_rx": extra[:20],
    }


def delivery_rate_post_warmup(
    n_received_post: int,
    rate_hz: float,
    duration_s: float,
    warmup_s: float,
    packets_sent: Optional[int] = None,
) -> dict[str, Any]:
    """Replaces availability. No hard-coded rate: expected = rate * (dur - warm)."""
    useful = max(0.0, float(duration_s) - float(warmup_s))
    expected_from_rate = max(1, int(round(rate_hz * useful))) if rate_hz > 0 else 1
    if packets_sent is not None and packets_sent > 0 and rate_hz > 0:
        # Post-warmup fraction of the packets actually sent
        expected = max(1, int(round(packets_sent * (useful / duration_s)))) if duration_s > 0 else packets_sent
    else:
        expected = expected_from_rate
    pct = 100.0 * n_received_post / expected if expected else None
    return {
        "delivery_rate_post_warmup_pct": round(pct, 6) if pct is not None else None,
        "n_received_post_warmup": n_received_post,
        "expected_post_warmup": expected,
        "expected_derivation": (
            "packets_sent * (duration-warmup)/duration"
            if packets_sent
            else "rate_hz * (duration_s - warmup_s)"
        ),
        "rate_hz": rate_hz,
        "duration_s": duration_s,
        "warmup_s": warmup_s,
    }


def fraction_within_budget(
    owd_s: list[float],
    threshold_s: float,
) -> Optional[float]:
    if not owd_s:
        return None
    return sum(1 for v in owd_s if v <= threshold_s) / len(owd_s)


def reliability_22125(
    owd_by_seq: dict[int, float],
    tx_seqs: Iterable[int],
    threshold_s: float,
) -> dict[str, Any]:
    """Delivered within budget / sent (denominator = sent)."""
    tx = list(tx_seqs)
    n_tx = len(tx)
    if n_tx == 0:
        return {"reliability": None, "delivered_within_budget": 0, "sent": 0}
    ok = 0
    for seq in tx:
        owd = owd_by_seq.get(int(seq))
        if owd is not None and owd <= threshold_s:
            ok += 1
    return {
        "reliability": ok / n_tx,
        "delivered_within_budget": ok,
        "sent": n_tx,
        "threshold_s": threshold_s,
    }


def interruption_gaps(
    t_rx_mono_ns: list[int],
    phase_of_t,
    thresholds_s: Iterable[dict[str, Any]] = GAP_THRESHOLDS_S,
) -> dict[str, Any]:
    """Gaps between consecutive arrivals. Assigns the phase where the gap starts."""
    if len(t_rx_mono_ns) < 2:
        empty = {
            "n_gaps": 0,
            "largest_gap_s": None,
            "by_threshold": {},
        }
        return empty
    ts = sorted(int(t) for t in t_rx_mono_ns)
    gaps = []
    for i in range(1, len(ts)):
        dur_s = (ts[i] - ts[i - 1]) / 1e9
        t_start = ts[i - 1]
        phase = phase_of_t(t_start)
        # Crosses if the gap ends in another phase
        phase_end = phase_of_t(ts[i])
        gaps.append({
            "t_start_mono_ns": t_start,
            "t_end_mono_ns": ts[i],
            "gap_duration_s": dur_s,
            "phase": phase,
            "gap_crosses_phase": phase != phase_end,
        })
    per_phase = {}
    for th in thresholds_s:
        thr = float(th["threshold_s"])
        sel = [g for g in gaps if g["gap_duration_s"] > thr]
        per_phase[f"gt_{thr:g}s"] = {
            "threshold_s": thr,
            "source": th.get("source"),
            "gap_count": len(sel),
            "total_duration_s": round(sum(g["gap_duration_s"] for g in sel), 6),
            "largest_gap_s": max((g["gap_duration_s"] for g in sel), default=None),
        }
    return {
        "n_gaps": len(gaps),
        "largest_gap_s": max((g["gap_duration_s"] for g in gaps), default=None),
        "by_threshold": per_phase,
        "gaps_gt_1s_sample": [g for g in gaps if g["gap_duration_s"] > 1.0][:20],
        "convention": (
            "gap = consecutive arrivals at the receiver; "
            "phase = phase where the gap starts; gap_crosses_phase if it ends in another phase"
        ),
    }


def assign_phase_half_open(
    t_rel_s: float,
    windows: dict[str, dict[str, float]],
) -> Optional[str]:
    """[lo, hi): a sample at attack_onset → ATTACK."""
    for name in ("PRE", "ATTACK", "DRAIN"):
        w = windows.get(name)
        if not w:
            continue
        lo = float(w["t_rel_lo_s"])
        hi = float(w["t_rel_hi_s"])
        if lo <= t_rel_s < hi:
            return name
    return None


# Histogram edges (seconds) aligned to plan budgets + coarse tail.
HIST_EDGES_S = (0.0, 0.01, 0.04, 0.1, 0.14, 0.155, 0.5, 1.0, 5.0, float("inf"))


def owd_histogram(owds: list[float], edges: tuple[float, ...] = HIST_EDGES_S) -> dict[str, Any]:
    counts = [0] * (len(edges) - 1)
    for v in owds:
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i + 1]
            if lo <= v < hi or (hi == float("inf") and v >= lo):
                counts[i] += 1
                break
    bins = []
    for i, c in enumerate(counts):
        lo, hi = edges[i], edges[i + 1]
        bins.append({
            "lo_s": lo,
            "hi_s": None if hi == float("inf") else hi,
            "count": c,
        })
    return {
        "unit": "s",
        "edges_s": [None if e == float("inf") else e for e in edges],
        "bins": bins,
        "n": len(owds),
    }


def owd_timeseries(
    rows: list[dict[str, Any]],
    *,
    max_points: Optional[int] = None,
) -> dict[str, Any]:
    """OWD time series (arrival order)."""
    ordered = sorted(
        (r for r in rows if r.get("t_rx_mono_ns") is not None and r.get("owd_s") is not None),
        key=lambda r: int(r["t_rx_mono_ns"]),
    )
    truncated = False
    if max_points is not None and len(ordered) > max_points:
        # Keep evenly spaced samples for machine-readable plot without huge files
        step = max(1, len(ordered) // max_points)
        ordered = ordered[::step][:max_points]
        truncated = True
    points = [
        {
            "t_rx_mono_ns": int(r["t_rx_mono_ns"]),
            "seq": int(r["seq"]) if r.get("seq") is not None else None,
            "owd_s": float(r["owd_s"]),
        }
        for r in ordered
    ]
    return {
        "n_points": len(points),
        "truncated": truncated,
        "points": points,
    }


def evaluate_c1_leg(owd_pct_primary: dict[str, Any]) -> dict[str, Any]:
    """C1 'effect present' on this leg's OWD (plan criterion C1).

    primary: p50 >= 1.0 s OR p99 >= 150 ms
    tail: p99 >= 150 ms and p50 < 100 ms
    belief: not derived from these probe/OWD KPIs (separate field).
    """
    p50 = owd_pct_primary.get("p50")
    p99 = owd_pct_primary.get("p99")
    insuf = bool(
        owd_pct_primary.get("percentile_insufficient_p50")
        or owd_pct_primary.get("percentile_insufficient_p99")
    )
    # percentil block stores flags outside primary; callers pass enriched dict
    effect = False
    tail_only = False
    if isinstance(p50, (int, float)) and isinstance(p99, (int, float)):
        effect = (p50 >= 1.0) or (p99 >= 0.150)
        tail_only = (p99 >= 0.150) and (p50 < 0.100)
    elif isinstance(p99, (int, float)) and p99 >= 0.150:
        effect = True
    return {
        "criterion": "C1",
        "definition": (
            "effect present if owd p50 >= 1.0 s OR owd p99 >= 150 ms; "
            "effect only in the tail if p99 >= 150 ms and p50 < 100 ms"
        ),
        "owd_p50_s": p50,
        "owd_p99_s": p99,
        "effect_present": effect,
        "effect_tail_only": tail_only,
        "belief_p50_m": None,
        "belief_note": (
            "the belief p50 >= 5 m confirmation is not computed in this kpi_leg "
            "(requires telemetry_downlink / belief_vs_truth)"
        ),
        "pass_effect": effect,
        "percentiles_insufficient": insuf,
    }


def evaluate_c4_kpi_slice(
    *,
    leg: str,
    fractions: list[dict[str, Any]],
    reliabilities: list[dict[str, Any]],
    interruptions: dict[str, Any],
    k7_fs_gcs: Optional[dict[str, Any]] = None,
    k8_qgc_style: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """C4: OWD KPIs + optionally FS_GCS (K7) and QGC style (K8)."""
    by_id_frac = {f["id"]: f for f in fractions}
    by_id_rel = {r["id"]: r for r in reliabilities}

    honey = by_id_frac.get("le_1s_honeywell") or {}
    frac_1s = honey.get("fraccao_le_threshold")
    pass_1s = honey.get("pass")

    rel_40 = by_id_rel.get("le_40ms_stick") or {}
    rel_1s = by_id_rel.get("le_1s_honeywell") or {}
    rel_140 = by_id_rel.get("le_140ms_video") or {}

    gaps = interruptions.get("by_threshold") or {}
    g3 = gaps.get("gt_3s") or {}
    g5 = gaps.get("gt_5s") or {}
    pass_gap3 = (g3.get("gap_count") == 0) if g3 else None
    pass_gap5 = (g5.get("gap_count") == 0) if g5 else None

    if k7_fs_gcs is not None:
        fs_check = {
            "pass": k7_fs_gcs.get("pass"),
            "triggered": (k7_fs_gcs.get("FS_GCS") or k7_fs_gcs).get("triggered"),
            "max_gap_command_s": (k7_fs_gcs.get("link_arrivals") or {}).get("max_gap_s"),
            "source": "K7 link_vivacity",
            "role": "pass_fail" if leg == LEG_COMMAND else "informative_on_return_leg",
        }
    else:
        fs_check = {
            "pass": None,
            "note": "provide k7_fs_gcs (K7) via link_vivacity",
        }

    if k8_qgc_style is not None:
        qgc_check = {
            "pass": k8_qgc_style.get("pass"),
            "max_gap_s": k8_qgc_style.get("max_gap_s"),
            "loss_declarations": k8_qgc_style.get("loss_declarations"),
            "pct_time_gap_gt_3_5s": k8_qgc_style.get("pct_time_gap_gt_loss"),
            "source": "K8 link_vivacity (QGC VehicleLinkManager style)",
            "role": "pass_fail" if leg == LEG_RETURN else "informative_on_command_leg",
        }
    else:
        qgc_check = {
            "pass": None,
            "note": "provide k8_qgc_style (K8) via link_vivacity",
        }

    checks = {
        "owd_le_1s_frac_ge_95pct": {
            "pass": pass_1s,
            "fraccao": frac_1s,
            "source": "Honeywell TestReport-263 citing DO-377A",
        },
        "reliability_le_40ms_ge_99_9pct": {
            "pass": rel_40.get("pass"),
            "reliability": rel_40.get("reliability"),
            "target": RELIABILITY_TARGET_STICK,
            "source": "TS 22.125 stick steering",
        },
        "reliability_le_1s_ge_99_9pct": {
            "pass": rel_1s.get("pass"),
            "reliability": rel_1s.get("reliability"),
            "target": RELIABILITY_TARGET_STICK,
            "source": "TS 22.125 (1 s budget / waypoints class)",
        },
        "reliability_video_le_140ms_ge_99_99pct": {
            "pass": rel_140.get("pass") if leg == LEG_RETURN else None,
            "reliability": rel_140.get("reliability") if leg == LEG_RETURN else None,
            "target": RELIABILITY_TARGET_VIDEO if leg == LEG_RETURN else None,
            "role": "pass_fail" if leg == LEG_RETURN else "informative_or_not_applicable",
            "source": "TS 22.125 Tab. 7.2-2 video Non-VLOS",
        },
        "no_gap_gt_3s": {
            "pass": pass_gap3,
            "gap_count": g3.get("gap_count"),
            "source": "AURA / TET 3 s (secondary citation)",
        },
        "no_gap_gt_5s": {
            "pass": pass_gap5,
            "gap_count": g5.get("gap_count"),
            "source": "Honeywell / TET cruise 5 s (secondary citation)",
        },
        "FS_GCS": fs_check,
        "QGC_style_gap_3_5s": qgc_check,
    }
    # Score: OWD checks always; FS_GCS only on command leg; QGC only on return leg.
    scored = []
    for k, v in checks.items():
        if v.get("pass") is None:
            continue
        if k == "FS_GCS" and leg != LEG_COMMAND:
            continue
        if k == "QGC_style_gap_3_5s" and leg != LEG_RETURN:
            continue
        scored.append(v["pass"])
    has_vivacity = k7_fs_gcs is not None or k8_qgc_style is not None
    return {
        "criterion": "C4",
        "scope": (
            "kpi_owd_plus_vivacity" if has_vivacity else "partial_kpi_owd_without_FS_GCS_without_QGC"
        ),
        "checks": checks,
        "pass_aggregate_partial": all(scored) if scored else None,
    }


def compute_owd_kpis(
    *,
    leg: str,
    samples_rx: list[dict[str, Any]],
    tx_seqs: list[int],
    rate_hz: float,
    duration_s: float,
    warmup_s: float = 0.0,
    packets_sent: Optional[int] = None,
    payload_bytes: Optional[int] = None,
    port: Optional[int] = None,
    clock_source: str = "CLOCK_MONOTONIC",
    metric_nature: str = NATURE_OWD,
    windows: Optional[dict[str, dict[str, float]]] = None,
    t0_mono_ns: Optional[int] = None,
    rejected_header: int = 0,
    denominator_where: Optional[str] = None,
) -> dict[str, Any]:
    """KPIs per PRE/ATTACK/DRAIN window (or ALL if there are no windows)."""
    if leg not in (LEG_COMMAND, LEG_RETURN):
        raise ValueError("leg must be 'command' or 'return'")

    def _owd_s(s: dict[str, Any]) -> Optional[float]:
        if s.get("owd_s") is not None:
            return float(s["owd_s"])
        if s.get("owd_us") is not None:
            return float(s["owd_us"]) / 1e6
        if s.get("owd_ms") is not None:
            return float(s["owd_ms"]) / 1e3
        return None

    # Filter warmup by t_rx if T0 is known, otherwise by order
    rx_valid = []
    for s in samples_rx:
        owd = _owd_s(s)
        if owd is None or s.get("seq") is None:
            continue
        row = dict(s)
        row["owd_s"] = owd
        rx_valid.append(row)

    phases: dict[str, list[dict[str, Any]]] = {"ALL": list(rx_valid)}
    if windows and t0_mono_ns is not None:
        for name in ("PRE", "ATTACK", "DRAIN"):
            phases[name] = []
        for s in rx_valid:
            t_rx = s.get("t_rx_mono_ns")
            if t_rx is None:
                continue
            t_rel = (int(t_rx) - int(t0_mono_ns)) / 1e9
            ph = assign_phase_half_open(t_rel, windows)
            if ph:
                phases[ph].append(s)

    # Warmup: drop the first warmup_s of ALL for delivery_rate
    if warmup_s > 0 and t0_mono_ns is not None:
        t_warm_end = int(t0_mono_ns) + int(warmup_s * 1e9)
        post_warm = [s for s in rx_valid if int(s.get("t_rx_mono_ns") or 0) >= t_warm_end]
    elif warmup_s > 0 and rate_hz > 0:
        drop = int(round(warmup_s * rate_hz))
        post_warm = rx_valid[drop:] if len(rx_valid) > drop else []
    else:
        post_warm = list(rx_valid)

    delivery = delivery_rate_post_warmup(
        n_received_post=len(post_warm),
        rate_hz=rate_hz,
        duration_s=duration_s,
        warmup_s=warmup_s,
        packets_sent=packets_sent if packets_sent is not None else len(tx_seqs),
    )

    loss = loss_by_seq(tx_seqs, [int(s["seq"]) for s in rx_valid])
    owd_by_seq = {int(s["seq"]): float(s["owd_s"]) for s in rx_valid}

    def _phase_kpi(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        owds = [float(s["owd_s"]) for s in rows]
        ipdv_info = compute_ipdv_by_seq(rows, "owd_s")
        pct = _percentile_block(owds, (50, 95, 99, 99.9))
        ipdv_pct = _percentile_block(ipdv_info["ipdv_s"], (50, 95, 99, 99.9))

        def phase_of_t_mono(t_ns: int) -> Optional[str]:
            if not windows or t0_mono_ns is None:
                return name if name != "ALL" else None
            t_rel = (int(t_ns) - int(t0_mono_ns)) / 1e9
            return assign_phase_half_open(t_rel, windows)

        gaps = interruption_gaps(
            [int(s["t_rx_mono_ns"]) for s in rows if s.get("t_rx_mono_ns") is not None],
            phase_of_t_mono,
        )

        fractions = []
        reliabilities = []
        for b in budgets_for_leg(leg):
            thr = float(b["threshold_s"])
            frac = fraction_within_budget(owds, thr)
            role = b["effective_role"]
            passed = None
            if frac is not None and role == "pass_fail":
                need = float(b.get("pass_if_frac_ge") or 0.0)
                if need > 0:
                    passed = frac >= need
                else:
                    passed = True  # informative fraction; pass/fail without an extra threshold = report only
                    # stick steering: pass/fail is on reliability, not on the fraction alone
            fractions.append({
                "id": b["id"],
                "threshold_s": thr,
                "source": b["source"],
                "role": role,
                "fraccao_le_threshold": frac,
                "pass": passed,
                "note": b.get("note"),
            })
            # Honeywell: pass na fraccao
            if b["id"] == "le_1s_honeywell" and frac is not None and role == "pass_fail":
                fractions[-1]["pass"] = frac >= float(b.get("pass_if_frac_ge", 0.95))

            rel = reliability_22125(owd_by_seq, tx_seqs, thr)
            target = RELIABILITY_TARGET_STICK
            if b["id"] == "le_140ms_video" and leg == LEG_RETURN:
                target = RELIABILITY_TARGET_VIDEO
            reliabilities.append({
                "id": b["id"],
                "threshold_s": thr,
                "source": b["source"],
                "role": role,
                "reliability": rel["reliability"],
                "delivered_within_budget": rel["delivered_within_budget"],
                "sent": rel["sent"],
                "target": target,
                "pass": (
                    None
                    if rel["reliability"] is None or role == "informative"
                    else rel["reliability"] >= target
                ),
            })

        hist = owd_histogram(owds)
        series = owd_timeseries(rows)
        c1_in = dict(pct["primary"])
        flags = pct.get("percentile_insufficient") or {}
        c1_in["percentile_insufficient_p50"] = bool(flags.get("p50"))
        c1_in["percentile_insufficient_p99"] = bool(flags.get("p99"))
        c1 = evaluate_c1_leg(c1_in)
        c4 = evaluate_c4_kpi_slice(
            leg=leg,
            fractions=fractions,
            reliabilities=reliabilities,
            interruptions=gaps,
        )

        return {
            "phase": name,
            "n": len(owds),
            "owd_s": pct,
            "ipdv_s": {
                **ipdv_pct,
                "out_of_order_count": ipdv_info["out_of_order_count"],
                "definition": "abs(owd[i]-owd[i-1]) with samples sorted by seq",
            },
            "owd_histogram": hist,
            "owd_time_series": series,
            "budget_fractions": fractions,
            "reliability_22125": reliabilities,
            "interruptions": gaps,
            "criterion_C1": c1,
            "criterion_C4": c4,
        }

    phases_out = {name: _phase_kpi(name, rows) for name, rows in phases.items() if name == "ALL" or rows or (windows and name in ("PRE", "ATTACK", "DRAIN"))}

    return {
        "schema": "kpi_leg_v1",
        "leg": leg,
        "metric_nature": metric_nature,
        "clock_source": clock_source,
        "payload_bytes": payload_bytes,
        "rate_hz": rate_hz,
        "port": port,
        "percentile_method_primary": PERCENTILE_METHOD_PRIMARY,
        "percentile_method_secondary": PERCENTILE_METHOD_SECONDARY,
        "delivery_rate_post_warmup": delivery,
        "loss_by_seq": loss,
        "rejected_header": rejected_header,
        "run_flagged_header_rejections": rejected_header > 0,
        "denominator_where_measured": denominator_where,
        "phases": phases_out,
        "note_analogous_not_identical": (
            "When applied to real C2: the telemetry delta and the network_arrival_delay "
            "of the setpoints are analogous (OWD at delivery), not identical."
        ),
    }


def cut_aoi_end_ns(
    active_setpoints: list[Any],
    t_active_end_ns: int,
) -> dict[str, Any]:
    """End of the AoI series = min(active_end, last_arrival) (no tail)."""
    if not active_setpoints:
        return {
            "t_aoi_end_ns": t_active_end_ns,
            "cut_reason": "no_active_setpoints",
            "t_last_send_ns": None,
            "t_last_arrival_ns": None,
        }
    t_last_send = max(int(sp.t_send_gcs_ns) for sp in active_setpoints)
    arrivals = [
        int(sp.t_arrival_uav_proxy_ns)
        for sp in active_setpoints
        if getattr(sp, "t_arrival_uav_proxy_ns", None) is not None
    ]
    t_last_arr = max(arrivals) if arrivals else None
    if t_last_arr is not None:
        t_end = min(int(t_active_end_ns), t_last_arr)
        reason = "min(active_end, last_arrival)"
    else:
        t_end = min(int(t_active_end_ns), t_last_send)
        reason = "min(active_end, last_send) without arrivals"
    return {
        "t_aoi_end_ns": t_end,
        "cut_reason": reason,
        "t_last_send_ns": t_last_send,
        "t_last_arrival_ns": t_last_arr,
        "t_active_end_ns_original": int(t_active_end_ns),
    }


def setpoint_counts_m1(
    setpoints: list[Any],
    arrivals_by_id: dict[int, Any],
    *,
    t_last_successful_send_ns: int,
    max_owd_s_observed: float,
    windows: dict[str, dict[str, float]],
    t0_mono_ns: int,
    get_send_ns,
    get_id,
    get_arrival_ns=None,
) -> dict[str, Any]:
    """Plan items 1.3c / M1: do not drop arrivals during the proxy drain.

    Sent universe = sends up to t_last_successful_send - W,
    W >= largest observed OWD. An arrival counts at any point of the capture
    if the send is in the universe (match by logical_setpoint_id).
    """
    w_s = max(0.0, float(max_owd_s_observed))
    w_ns = int(math.ceil(w_s * 1e9))
    cutoff_ns = int(t_last_successful_send_ns) - w_ns

    sent_items = []
    excluded = []
    for sp in setpoints:
        sid = int(get_id(sp))
        t_send = int(get_send_ns(sp))
        if t_send <= cutoff_ns:
            sent_items.append(sp)
        else:
            excluded.append(sid)

    def phase_for_send(sp) -> Optional[str]:
        t_rel = (int(get_send_ns(sp)) - int(t0_mono_ns)) / 1e9
        return assign_phase_half_open(t_rel, windows)

    counts = {ph: {"n_sent": 0, "n_received": 0} for ph in ("PRE", "ATTACK", "DRAIN")}
    for sp in sent_items:
        ph = phase_for_send(sp)
        if ph is None:
            continue
        counts[ph]["n_sent"] += 1
        sid = int(get_id(sp))
        if sid in arrivals_by_id:
            counts[ph]["n_received"] += 1
        elif get_arrival_ns is not None and get_arrival_ns(sp) is not None:
            counts[ph]["n_received"] += 1

    return {
        "schema": "setpoint_counts_m1_v1",
        "rule": (
            "sent universe = sends with t_send <= t_last_successful_send - W; "
            "W >= largest OWD observed in the run; an arrival counts at any point "
            "of the capture (drain included) if the id is in the universe"
        ),
        "W_s": w_s,
        "W_ns": w_ns,
        "W_derivation": "max(0, largest_owd_s_observed_in_run)",
        "t_last_successful_send_ns": int(t_last_successful_send_ns),
        "cutoff_send_ns": cutoff_ns,
        "n_sent_universe": len(sent_items),
        "n_sends_excluded_by_W": len(excluded),
        "excluded_ids_sample": excluded[:40],
        "by_phase": counts,
    }

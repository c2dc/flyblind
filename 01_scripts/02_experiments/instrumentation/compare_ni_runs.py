#!/usr/bin/env python3
##################################################################################################################################
# Module: compare_ni_runs.py (Compare non-interference runs (probes OFF vs ON))                                                  #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Compares what would reveal C2 theft / probe interference on :5760: * mission telemetry rate (ned Hz),             #
#   ned_samples_*, δ (aoi_p50), trk_err * delivery overlap loss * video goodput + MAC UL * optional host load                    #
#                                                                                                                                #
# Works for baseline OR attack pairs. Does not modify frozen originals.                                                          #
##################################################################################################################################
"""Compare non-interference runs (probes OFF vs ON).

Compares what would reveal C2 theft / probe interference on :5760:
  * mission telemetry rate (ned Hz), ned_samples_*, δ (aoi_p50), trk_err
  * delivery overlap loss
  * video goodput + MAC UL
  * optional host load

Works for baseline OR attack pairs. Does not modify frozen originals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(p: Path):
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def _telemetry(run: Path, post: Path | None = None) -> dict:
    fl = _load(run / "four_layer_summary.json") or {}
    tel = (
        ((fl.get("physics") or {}).get("freshness") or {}).get("telemetry_downlink")
        or (fl.get("freshness") or {}).get("telemetry_downlink")
        or {}
    )
    # also flat telemetry_downlink_summary.json
    tds = _load(run / "telemetry_downlink_summary.json") or {}
    bv = tel.get("belief_validation") or tds.get("belief_validation") or {}
    ov = _load(run / "flyblind_delivery_ratio_overlap.json") or {}
    if not ov and post is not None:
        ov = _load(post / "flyblind_delivery_ratio_overlap.json") or {}

    ned_uav = tel.get("ned_samples_uav") or tds.get("ned_samples_uav")
    ned_gcs = tel.get("ned_samples_gcs") or tds.get("ned_samples_gcs")
    # Hz from summary if present
    hz = tds.get("nominal_hz") or tel.get("nominal_hz")
    # empirical Hz from capture_ratio * nominal, or matched/span
    span = None
    if isinstance(tds.get("ned_samples_gcs"), int) and tds.get("ned_samples_gcs"):
        # try four_layer active window
        aw = (fl.get("freshness") or {}).get("active_window_duration_s")
        if aw and ned_gcs:
            span = float(aw)
    empiric_hz_gcs = (ned_gcs / span) if (ned_gcs and span and span > 0) else None

    phys = fl.get("physics") or {}
    trk_p50 = None
    # Prefer GCS-side tracking / truth used in paper gates
    for cand in (
        ((phys.get("trk_truth") or {}).get("tracking_error_truth_m") or {}),
        ((phys.get("flight_state") or {}).get("tracking_error_m") or {}),
        ((phys.get("steady_state_window") or {}).get("tracking_error_m") or {}),
        ((phys.get("trk_err") or {})),
    ):
        if isinstance(cand, dict):
            trk_p50 = cand.get("p50") or cand.get("median") or cand.get("p50_m")
            if trk_p50 is not None:
                break
    if trk_p50 is None:
        c2 = _load(run / "c2.json") or {}
        te = c2.get("trk_err") or c2.get("tracking_error") or {}
        if isinstance(te, dict):
            trk_p50 = te.get("p50_m") or te.get("p50") or te.get("median")

    return {
        "ned_samples_uav": ned_uav,
        "ned_samples_gcs": ned_gcs,
        "nominal_hz": hz,
        "empiric_hz_gcs": empiric_hz_gcs,
        "uav_to_gcs_delivery_ratio_file": tel.get("uav_to_gcs_delivery_ratio") or tds.get("uav_to_gcs_delivery_ratio"),
        "delivery_ratio_overlap": ov.get("uav_to_gcs_delivery_ratio_overlap"),
        "loss_percent_overlap": ov.get("loss_percent_overlap"),
        "delta_p50_s": (bv.get("aoi_p50_ms") / 1000.0) if bv.get("aoi_p50_ms") is not None else bv.get("belief_age_p50_s"),
        "delta_p99_s": (bv.get("aoi_p99_ms") / 1000.0) if bv.get("aoi_p99_ms") is not None else bv.get("belief_age_p99_s"),
        "belief_p50_m": bv.get("belief_p50_m"),
        "trk_err_p50_m": trk_p50,
    }


def _video(run: Path, post: Path | None = None) -> dict:
    g = _load(run / "flyblind_goodput_mac.json")
    if g is None and post is not None:
        g = _load(post / "flyblind_goodput_mac.json")
    if g:
        return {
            "mac_uav_ul_mbps": g.get("mac_uav_ul_mbps"),
            "goodput_mbps": (g.get("video") or {}).get("goodput_mbps"),
            "lost_percent": (g.get("video") or {}).get("lost_percent"),
        }
    cv = _load(run / "companion_video.json") or {}
    s = (cv.get("end") or {}).get("sum") or {}
    gp = None
    if s.get("bytes") and s.get("lost_percent") is not None and s.get("seconds"):
        gp = s["bytes"] * (1 - s["lost_percent"] / 100) * 8 / s["seconds"] / 1e6
    prb = _load(run / "prb.json") or {}
    return {
        "mac_uav_ul_mbps": ((prb.get("per_role") or {}).get("uav") or {}).get("ul_mbps"),
        "goodput_mbps": gp,
        "lost_percent": s.get("lost_percent"),
    }


def _phases(run: Path, post: Path | None = None) -> dict | None:
    for base in (post, run):
        if base is None:
            continue
        p = _load(base / "flyblind_phase_windows.json")
        if p:
            return p
    return None


def _delta(a, b):
    if a is None or b is None:
        return None
    return b - a


def _rel(a, b):
    """Relative change (b-a)/|a| if a else None."""
    if a is None or b is None or a == 0:
        return None
    return (b - a) / abs(a)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare NI probes OFF vs ON")
    ap.add_argument("--off-run", required=True, help="WITHOUT sidecars / without ACK:5763 traffic")
    ap.add_argument("--on-run", required=True, help="WITH sidecars (incl. ACK via :5763)")
    ap.add_argument("--label", default="", help="e.g. baseline or attack")
    ap.add_argument("--off-sidecar-dir", default="")
    ap.add_argument("--on-sidecar-dir", default="")
    ap.add_argument("--off-post-dir", default="")
    ap.add_argument("--on-post-dir", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    off_r, on_r = Path(args.off_run), Path(args.on_run)
    off_p = Path(args.off_post_dir) if args.off_post_dir else None
    on_p = Path(args.on_post_dir) if args.on_post_dir else None
    off_t, on_t = _telemetry(off_r, off_p), _telemetry(on_r, on_p)
    off_v, on_v = _video(off_r, off_p), _video(on_r, on_p)

    load_off = _load(Path(args.off_sidecar_dir) / "flyblind_host_load_summary.json") if args.off_sidecar_dir else None
    load_on = _load(Path(args.on_sidecar_dir) / "flyblind_host_load_summary.json") if args.on_sidecar_dir else None
    if load_off is None:
        load_off = _load(off_r / "flyblind_host_load_summary.json")
    if load_on is None:
        load_on = _load(on_r / "flyblind_host_load_summary.json")

    ack_on = None
    if args.on_sidecar_dir:
        ack_on = _load(Path(args.on_sidecar_dir) / "flyblind_command_ack_rtt.json")
    if ack_on is None:
        ack_on = _load(on_r / "flyblind_command_ack_rtt.json")

    d_ned_g = _delta(off_t.get("ned_samples_gcs"), on_t.get("ned_samples_gcs"))
    d_delta = _delta(off_t.get("delta_p50_s"), on_t.get("delta_p50_s"))
    d_trk = _delta(off_t.get("trk_err_p50_m"), on_t.get("trk_err_p50_m"))

    report = {
        "schema": "flyblind_ni_compare_v2",
        "label": args.label or None,
        "off_run": str(off_r),
        "on_run": str(on_r),
        "telemetry": {
            "off": off_t,
            "on": on_t,
            "delta_on_minus_off": {
                "ned_samples_uav": _delta(off_t.get("ned_samples_uav"), on_t.get("ned_samples_uav")),
                "ned_samples_gcs": d_ned_g,
                "ned_samples_gcs_rel": _rel(off_t.get("ned_samples_gcs"), on_t.get("ned_samples_gcs")),
                "delta_p50_s": d_delta,
                "trk_err_p50_m": d_trk,
                "loss_percent_overlap": _delta(off_t.get("loss_percent_overlap"), on_t.get("loss_percent_overlap")),
            },
            "flag_possible_c2_theft": None,
            "flag_5763_affects_5760": None,
        },
        "video": {
            "off": off_v,
            "on": on_v,
            "delta_on_minus_off": {
                "goodput_mbps": _delta(off_v.get("goodput_mbps"), on_v.get("goodput_mbps")),
                "mac_uav_ul_mbps": _delta(off_v.get("mac_uav_ul_mbps"), on_v.get("mac_uav_ul_mbps")),
                "lost_percent": _delta(off_v.get("lost_percent"), on_v.get("lost_percent")),
            },
        },
        "ack_on": {
            "mode": (ack_on or {}).get("mode"),
            "crosses_5g": (ack_on or {}).get("crosses_5g"),
            "rtt_ms_p50": ((ack_on or {}).get("rtt_ms") or {}).get("p50"),
            "n_ok": (ack_on or {}).get("n_ok"),
        } if ack_on else None,
        "phases_off": _phases(off_r, off_p),
        "phases_on": _phases(on_r, on_p),
        "host_load": {"off": load_off, "on": load_on},
        "pass_criteria_note": (
            "5763 must not disturb mission :5760: |Δ ned_samples_gcs|/off < 10%, "
            "|Δ δ p50| small on baseline (<0.05s) or comparable under attack, "
            "|Δ trk_err p50| small. flag_possible_c2_theft if GCS NED collapses."
        ),
    }

    flag_theft = False
    flag_5763 = False
    reason = []
    reason_5763 = []

    dg = d_ned_g
    if dg is not None and off_t.get("ned_samples_gcs") and dg < -0.1 * off_t["ned_samples_gcs"]:
        flag_theft = True
        reason.append("ned_samples_gcs dropped >10% with probes on")
        flag_5763 = True
        reason_5763.append("telemetry count drop suggests :5763 interference with :5760")

    du = report["telemetry"]["delta_on_minus_off"]["ned_samples_uav"]
    if du is not None and off_t.get("ned_samples_uav") and du < -0.1 * off_t["ned_samples_uav"]:
        flag_theft = True
        reason.append("ned_samples_uav dropped >10% with probes on")

    dloss = report["telemetry"]["delta_on_minus_off"]["loss_percent_overlap"]
    if dloss is not None and dloss > 2.0:
        flag_theft = True
        reason.append("overlap loss rose >2pp")
        flag_5763 = True
        reason_5763.append("overlap loss rose")

    # δ / trk: baseline-sensitive thresholds; attack pairs only flag huge jumps
    is_attack = "attack" in (args.label or "").lower() or "atk" in str(off_r).lower()
    if d_delta is not None:
        lim = 2.0 if is_attack else 0.05
        if abs(d_delta) > lim:
            flag_5763 = True
            reason_5763.append(f"|Δ δ p50|={d_delta:.4f}s > {lim}s")
    if d_trk is not None and off_t.get("trk_err_p50_m") is not None:
        # relative 20% or absolute 0.5 m
        if abs(d_trk) > max(0.5, 0.2 * abs(off_t["trk_err_p50_m"])):
            flag_5763 = True
            reason_5763.append(f"|Δ trk_err p50|={d_trk:.3f}m")

    if ack_on and not ack_on.get("crosses_5g", True) and (ack_on.get("mode") or "").startswith("local"):
        reason.append("ACK mode is local (not 5G) — NI ON run invalid for ACK metric")

    report["telemetry"]["flag_possible_c2_theft"] = flag_theft
    report["telemetry"]["flag_reasons"] = reason
    report["telemetry"]["flag_5763_affects_5760"] = flag_5763
    report["telemetry"]["flag_5763_reasons"] = reason_5763
    report["ni_pass"] = (not flag_theft) and (not flag_5763)

    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

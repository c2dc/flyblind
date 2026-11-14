#!/usr/bin/env python3
##################################################################################################################################
# Module: phase_windows.py (Resolve PRE / ATTACK / DRAIN windows from event_markers.csv)                                         #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Never use attack_start_after_s alone as the time axis — that is relative to mission_start, not T0 or GCS capture  #
#   start. Canonical boundaries:                                                                                                 #
#                                                                                                                                #
# attack_onset, attack_end  (required for ATTACK/DRAIN) mission_start             (optional; PRE often [mission_start,           #
#   attack_onset))                                                                                                               #
#                                                                                                                                #
# Half-open intervals [lo, hi). A sample with t_rel == attack_onset belongs to ATTACK. Written to flyblind_phase_windows.json.   #
##################################################################################################################################
"""Resolve PRE / ATTACK / DRAIN windows from event_markers.csv.

Never use attack_start_after_s alone as the time axis — that is relative to
mission_start, not T0 or GCS capture start. Canonical boundaries:

  attack_onset, attack_end  (required for ATTACK/DRAIN)
  mission_start             (optional; PRE often [mission_start, attack_onset))

Half-open intervals [lo, hi). A sample with t_rel == attack_onset
belongs to ATTACK. Written to flyblind_phase_windows.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any, Optional


def write_clock_anchor(
    run_dir: Path,
    *,
    t0_mono_ns: int,
    t0_unix_ns: int,
) -> Path:
    """Persist T0. Phase slice of proxy_link uses T0_mono_ns (shared monotonic)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "schema": "flyblind_clock_anchor_v2",
        "T0_mono_ns": int(t0_mono_ns),
        "T0_unix_ns": int(t0_unix_ns),
        "written_unix_ns": time.time_ns(),
        "event_markers_clock": "host monotonic; t_rel_s = (t_mono - T0_mono_ns)/1e9",
        "proxy_link_clock": (
            "pod monotonic == host when time_namespace_match; "
            "t_rel_s = (t_arrival_ns - T0_mono_ns)/1e9"
        ),
        "t0_unix_ns_role": "metadata / fallback for samplers that only stamp unix",
    }
    path = run_dir / "clock_anchor.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def load_t0_mono_ns(run_dir: Path) -> Optional[int]:
    """Resolve T0_mono_ns from clock_anchor / run_meta."""
    run_dir = Path(run_dir)
    for name in ("clock_anchor.json", "run_meta.json"):
        p = run_dir / name
        if not p.is_file():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for key in ("T0_mono_ns", "t0_mono_ns"):
            if doc.get(key) is not None:
                return int(doc[key])
    return None


def load_t0_unix_ns(run_dir: Path) -> Optional[int]:
    """Resolve T0_unix_ns (metadata / ue_iface fallback)."""
    run_dir = Path(run_dir)
    for name in ("clock_anchor.json", "run_meta.json", "analysis_params.json", "flyblind_sidecars_meta.json"):
        p = run_dir / name
        if not p.is_file():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for key in ("T0_unix_ns", "t0_unix_ns"):
            if doc.get(key) is not None:
                return int(doc[key])
    return None


def load_markers(run_dir: Path) -> dict[str, float]:
    path = run_dir / "event_markers.csv"
    if not path.is_file():
        return {}
    out: dict[str, float] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ev = (row.get("event") or "").strip()
            if not ev:
                continue
            try:
                out[ev] = float(row["t_rel_s"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def derive_mission_markers(run_dir: Path) -> dict[str, dict[str, Any]]:
    """orbit_acquired / land_command (s since T0) from setpoints.csv + c2.json.

    The mission clock is mapped to T0 through the first logical setpoint:
    abs(t_mission) = first_generation_abs + (t_mission - trajectory_t0_rel_s).
    """
    run_dir = Path(run_dir)
    t0 = load_t0_mono_ns(run_dir)
    sp_path = run_dir / "setpoints.csv"
    c2_path = run_dir / "c2.json"
    if t0 is None or not sp_path.is_file() or not c2_path.is_file():
        return {}
    gens: list[int] = []
    with sp_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                gens.append(int(row["t_generation_gcs_ns"]))
            except (KeyError, TypeError, ValueError):
                continue
    try:
        c2 = json.loads(c2_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    traj_t0_rel = (c2.get("trajectory") or {}).get("trajectory_t0_rel_s")
    pm = c2.get("physics_metrics") or {}
    orbit_rel = (pm.get("trajectory_acquisition") or {}).get("orbit_stream_started_at_rel_s")
    if not gens or traj_t0_rel is None:
        return {}
    first_abs = (min(gens) - t0) / 1e9
    out: dict[str, dict[str, Any]] = {}
    if orbit_rel is not None:
        out["orbit_acquired"] = {
            "t_rel_s": round(first_abs + float(orbit_rel) - float(traj_t0_rel), 3),
            "note": "c2.json orbit_stream_started_at_rel_s mapped via first logical setpoint",
        }
    out["land_command"] = {
        "t_rel_s": round((max(gens) - t0) / 1e9, 3),
        "note": "last logical setpoint generated; LAND is sent right after the stream",
    }
    for tr in (pm.get("flight_state") or {}).get("mode_transitions") or []:
        if tr.get("from") == "GUIDED" and tr.get("to") == "LAND" and tr.get("t_rel_s") is not None:
            out["guided_to_land_observed"] = {
                "t_rel_s": round(first_abs + float(tr["t_rel_s"]) - float(traj_t0_rel), 3),
                "note": "observed mode transition (informational)",
            }
            break
    return out


def virtual_attack_markers(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Baseline: virtual onset / attack_end from run_meta.schedule_optc (option C)."""
    p = Path(run_dir) / "run_meta.json"
    if not p.is_file():
        return {}
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    sched = meta.get("schedule_optc") or {}
    if meta.get("condition") != "baseline" or sched.get("virtual_attack_onset_s") is None:
        return {}
    return {
        "virtual_attack_onset": {
            "t_rel_s": float(sched["virtual_attack_onset_s"]),
            "note": "option C virtual onset (baseline, same schedule as attack)",
        },
        "virtual_attack_end": {
            "t_rel_s": float(sched["virtual_attack_end_s"]),
            "note": "option C virtual attack_end (onset + burst)",
        },
    }


def append_markers(run_dir: Path, extra: dict[str, dict[str, Any]]) -> list[str]:
    """Append missing events to event_markers.csv (kept before run_end)."""
    path = Path(run_dir) / "event_markers.csv"
    if not path.is_file() or not extra:
        return []
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    present = {(r.get("event") or "").strip() for r in rows}
    added = [name for name in extra if name not in present]
    if not added:
        return []
    tail = [r for r in rows if r.get("event") == "run_end"]
    body = [r for r in rows if r.get("event") != "run_end"]
    for name in added:
        body.append({"event": name, "t_rel_s": extra[name]["t_rel_s"], "note": extra[name].get("note", "")})
    body.sort(key=lambda r: float(r.get("t_rel_s") or 0.0))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["event", "t_rel_s", "note"])
        w.writeheader()
        for r in body + tail:
            w.writerow({"event": r.get("event"), "t_rel_s": r.get("t_rel_s"), "note": r.get("note", "")})
    return added


def write_run_phase_windows(run_dir: Path) -> dict:
    """Post-run: add orbit/land (+ virtual onset on baseline) markers, write flyblind_phase_windows.json."""
    run_dir = Path(run_dir)
    extra = {**derive_mission_markers(run_dir), **virtual_attack_markers(run_dir)}
    added = append_markers(run_dir, extra)
    data = build_windows(load_markers(run_dir))
    data["run_dir"] = str(run_dir)
    data["markers_added"] = added
    (run_dir / "flyblind_phase_windows.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return data


def load_run_windows(run_dir: Path) -> Optional[dict]:
    """windows dict from flyblind_phase_windows.json, else built from event_markers.csv."""
    run_dir = Path(run_dir)
    p = run_dir / "flyblind_phase_windows.json"
    if p.is_file():
        try:
            return (json.loads(p.read_text(encoding="utf-8")).get("windows")) or None
        except json.JSONDecodeError:
            pass
    return build_windows(load_markers(run_dir)).get("windows") or None


def assign_phase(t_rel_s: float, windows: dict) -> Optional[str]:
    """Convention [lo, hi): t == attack_onset → ATTACK."""
    for name in ("PRE", "ATTACK", "DRAIN"):
        w = windows.get(name)
        if not w:
            continue
        lo = float(w["t_rel_lo_s"])
        hi = float(w["t_rel_hi_s"])
        if lo <= float(t_rel_s) < hi:
            return name
    return None


def build_windows(markers: dict[str, float]) -> dict:
    """PRE from orbit_acquired (else mission_start), DRAIN until land_command (else run_end).

    Baseline runs without attack markers use virtual_attack_onset / virtual_attack_end
    (option C schedule) so baseline and attack are comparable window by window.
    """
    onset = markers.get("attack_onset")
    end = markers.get("attack_end")
    virtual = False
    if onset is None and markers.get("virtual_attack_onset") is not None:
        onset = markers.get("virtual_attack_onset")
        end = markers.get("virtual_attack_end")
        virtual = True
    mission = markers.get("mission_start")
    orbit = markers.get("orbit_acquired")
    land = markers.get("land_command")
    run_start = markers.get("run_start", 0.0)
    run_end = markers.get("run_end")
    onset_name = "virtual_attack_onset" if virtual else "attack_onset"
    end_name = "virtual_attack_end" if virtual else "attack_end"

    windows = {}
    if onset is not None:
        if orbit is not None and orbit < onset:
            pre_lo, pre_anchor = orbit, f"orbit_acquired → {onset_name}"
        elif mission is not None:
            pre_lo, pre_anchor = mission, f"mission_start → {onset_name}"
        else:
            pre_lo, pre_anchor = run_start, f"run_start → {onset_name}"
        windows["PRE"] = {
            "t_rel_lo_s": pre_lo,
            "t_rel_hi_s": onset,
            "anchor": pre_anchor,
            "interval": "[lo, hi)",
        }
    if onset is not None and end is not None:
        windows["ATTACK"] = {
            "t_rel_lo_s": onset,
            "t_rel_hi_s": end,
            "anchor": f"{onset_name} → {end_name}",
            "interval": "[lo, hi)",
        }
    drain_hi, drain_anchor = None, None
    if end is not None and land is not None and land > end:
        drain_hi, drain_anchor = land, f"{end_name} → land_command"
    elif end is not None and run_end is not None:
        drain_hi, drain_anchor = run_end, f"{end_name} → run_end"
    if drain_hi is not None:
        windows["DRAIN"] = {
            "t_rel_lo_s": end,
            "t_rel_hi_s": drain_hi,
            "anchor": drain_anchor,
            "interval": "[lo, hi)",
        }

    schedule_check: dict[str, Any] = {"virtual_attack": virtual}
    if onset is not None and orbit is not None:
        schedule_check["pre_stable_orbit_s"] = round(onset - orbit, 3)
    if onset is not None and end is not None and orbit is not None and land is not None:
        schedule_check["attack_within_orbit_and_trajectory"] = orbit <= onset and end <= land

    return {
        "schema": "flyblind_phase_windows_v2",
        "markers": markers,
        "windows": windows,
        "virtual_attack": virtual,
        "schedule_check": schedule_check,
        "ok": "ATTACK" in windows,
        "boundary_convention": "[lo, hi); sample at attack_onset → ATTACK",
        "note": (
            "t_rel_s is relative to T0 in event_markers (four_layer T0_mono_ns). "
            "Do not treat attack_start_after_s as absolute time from T0. "
            "Half-open intervals [lo, hi)."
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase windows from event_markers")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    run = Path(args.run_dir)
    markers = load_markers(run)
    data = build_windows(markers)
    data["run_dir"] = str(run)
    if not markers:
        data["ok"] = False
        data["error"] = "event_markers.csv missing or empty"
    Path(args.out).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: data[k] for k in data if k != "markers"}, indent=2))
    # Baseline runs have no attack_onset — still a successful write (ok=false).
    if not markers:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
##################################################################################################################################
# Module: harness_hooks.py (hooks the orchestrator shares across runs: manifest, phases, drain, digests)                         #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Importable offline. Does not start the testbed.                                                                   #
##################################################################################################################################
"""Hooks shared by the orchestrates (plan items 1.8, 1.10, 1.11, 1.15).

Importable offline. Does not start the testbed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
RUN_SIDECARS = HERE / "run_sidecars.py"


def normalize_sidecar_profile(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("", "off", "0", "false", "no", "none"):
        return None
    if s in ("minimal", "min", "minimum"):
        return "MINIMAL"
    if s in ("full", "complete"):
        return "FULL"
    if s.upper() in ("MINIMAL", "FULL"):
        return s.upper()
    raise ValueError(f"invalid sidecar profile: {raw!r} (use off|MINIMAL|FULL)")


def sidecar_duration_s(envelope_s: float, override: Optional[float] = None) -> float:
    if override is not None and override > 0:
        return float(override)
    # Sidecars must cover the full orchestrate envelope.
    return max(float(envelope_s), 1.0)


def start_sidecars(
    *,
    run_dir: Path,
    profile: str,
    duration_s: float,
    context: str,
    namespace: str,
    gcs_ip: str = "",
    uav_ip: str = "",
) -> tuple[Optional[subprocess.Popen], Path]:
    out = Path(run_dir) / "sidecars"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(RUN_SIDECARS),
        "--out-dir", str(out),
        "--duration", str(duration_s),
        "--profile", profile,
        "--context", context,
        "--namespace", namespace,
    ]
    if gcs_ip:
        cmd += ["--gcs-ip", gcs_ip]
    if uav_ip:
        cmd += ["--uav-ip", uav_ip]
    logf = open(out / "supervisor.live.log", "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    meta = {
        "schema": "flyblind_orchestrate_sidecars_v1",
        "profile": profile,
        "duration_s": duration_s,
        "pid": proc.pid,
        "cmd": cmd,
    }
    (out / "orchestrate_sidecars.json").write_text(json.dumps(meta, indent=2) + "\n")
    return proc, out


def stop_sidecars(proc: Optional[subprocess.Popen], grace_s: float = 30.0) -> Optional[int]:
    if proc is None:
        return None
    if proc.poll() is not None:
        return proc.returncode
    try:
        return proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait(timeout=5)


def schedule_video_stop_at_attack_end(
    *,
    T0_mono_ns: int,
    attack_end_t_rel_s: float,
    stop_fn,
    on_mark,
) -> threading.Thread:
    """DRAIN (b): calls stop_fn at the attack_end instant and writes the marker via on_mark."""

    def _worker() -> None:
        now_rel = (time.monotonic_ns() - T0_mono_ns) / 1e9
        wait = attack_end_t_rel_s - now_rel
        if wait > 0:
            time.sleep(wait)
        t_rel = (time.monotonic_ns() - T0_mono_ns) / 1e9
        try:
            stop_fn()
        finally:
            on_mark(t_rel)

    th = threading.Thread(target=_worker, name="video_stop_at_attack_end", daemon=True)
    th.start()
    return th


def append_event_marker_rows(
    rows: list[dict[str, Any]],
    *,
    video_stop_t_rel_s: Optional[float] = None,
) -> list[dict[str, Any]]:
    out = list(rows)
    if video_stop_t_rel_s is not None:
        # Insert before run_end if present
        marker = {
            "event": "video_stop",
            "t_rel_s": round(float(video_stop_t_rel_s), 3),
            "note": "DRAIN (b): companion video stopped at attack_end",
        }
        inserted = False
        for i, r in enumerate(out):
            if r.get("event") == "run_end":
                out.insert(i, marker)
                inserted = True
                break
        if not inserted:
            out.append(marker)
    return out


def write_post_run_harness(
    run_dir: Path,
    *,
    sidecar_profile: Optional[str],
    drain_policy: Optional[dict[str, Any]] = None,
    video_stop_t_rel_s: Optional[float] = None,
) -> dict[str, Any]:
    """Writes the manifest + drain_policy; tries the run report (best-effort)."""
    written: dict[str, Any] = {}
    try:
        from run_manifest import build_manifest, write_manifest  # type: ignore
        man = build_manifest(run_dir, sidecar_profile=sidecar_profile or "off")
        if video_stop_t_rel_s is not None:
            man["video_stop_t_rel_s"] = video_stop_t_rel_s
        write_manifest(run_dir, man)
        written["run_manifest.json"] = True
    except Exception as exc:
        written["run_manifest.json"] = f"error: {exc}"

    if drain_policy is not None:
        try:
            from drain_policy import write_drain_policy  # type: ignore
            write_drain_policy(run_dir, drain_policy)
            written["drain_policy.json"] = True
        except Exception as exc:
            written["drain_policy.json"] = f"error: {exc}"

    try:
        try:
            from phase_windows import write_run_phase_windows  # type: ignore
        except ImportError:
            from instrumentation.phase_windows import write_run_phase_windows  # type: ignore
        pw = write_run_phase_windows(run_dir)
        written["flyblind_phase_windows.json"] = {
            "windows": sorted((pw.get("windows") or {}).keys()),
            "markers_added": pw.get("markers_added"),
            "virtual_attack": pw.get("virtual_attack"),
        }
    except Exception as exc:
        written["flyblind_phase_windows.json"] = f"error: {exc}"

    try:
        from run_report import build_report, render_md  # type: ignore
        report = build_report(run_dir)
        (run_dir / "run_kpi_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (run_dir / "run_kpi_report.md").write_text(render_md(report), encoding="utf-8")
        written["run_kpi_report"] = True
    except Exception as exc:
        written["run_kpi_report"] = f"error: {exc}"

    return written

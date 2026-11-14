##################################################################################################################################
# Module: common.py (Shared helpers for the campaign analysis)                                                                   #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Read-only over the campaign results: nothing under 02_results/ is modified. KPI percentiles, phase windows and    #
#   belief are computed with the harness's own functions (kpi_leg, phase_windows, freshness_metrics), so the analysis uses       #
#   exactly the same methodology as the per-run reports.                                                                         #
##################################################################################################################################
"""Shared helpers for the campaign analysis.

Read-only over the campaign results: nothing under 02_results/ is modified. KPI percentiles,
phase windows and belief are computed with the harness's own functions (kpi_leg, phase_windows,
freshness_metrics), so the analysis uses exactly the same methodology as the per-run reports.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

ANALYSIS_DIR = Path(__file__).resolve().parent
ARTIFACTS = ANALYSIS_DIR.parent
SCRIPTS = ARTIFACTS / "01_scripts"
CAMPAIGN = ARTIFACTS / "02_results" / "campaign"
OUT = ANALYSIS_DIR / "out"
MANIFEST = OUT / "campaign_manifest.json"

for _p in (SCRIPTS / "02_experiments" / "lib",
           SCRIPTS / "02_experiments" / "instrumentation",
           SCRIPTS / "03_sitl" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

LEGS = ("command", "return")
PHASES = ("PRE", "ATTACK", "DRAIN", "ALL")

# Condition metadata (labels only; every number comes from the run artifacts). The keys are the
# condition ids emitted by 01_scripts/04_campaign/campaign.sh, so this dict and the campaign driver
# must be kept in step; build_manifest.py warns about any condition missing from here.
# series, testbed, short English label
CONDITIONS: dict[str, tuple[str, str, str]] = {
    # --- S1_core: 106 PRB, victim co-located in the attacker's slice, UL patch armed but inert ---
    "a_baseline": ("S1", "default-106", "Baseline, video 12 Mb/s"),
    "a_atk_n1": ("S1", "default-106", "Attack N=1, video 12 Mb/s"),
    "a_atk_n2": ("S1", "default-106", "Attack N=2, video 12 Mb/s"),
    "a_atk_n3": ("S1", "default-106", "Attack N=3, video 12 Mb/s"),
    "a_atk_n4": ("S1", "default-106", "Attack N=4, video 12 Mb/s (headline)"),
    # Onset: the attack needs the victim's demand above its share C/(N+1) ~= 4.8..6.4 Mb/s at N=4.
    "c_atk_n4_novideo": ("S1", "default-106", "Attack N=4, video 0 (victim not the fat flow)"),
    "b_atk_n4_vid4": ("S1", "default-106", "Attack N=4, video 4 Mb/s (below the share)"),
    "b_atk_n4_vid6": ("S1", "default-106", "Attack N=4, video 6 Mb/s (on the predicted share)"),
    "b_atk_n4_vid8": ("S1", "default-106", "Attack N=4, video 8 Mb/s (above the share)"),
    # Plateau: above the share the age is buffer/share and must not grow with the offered rate.
    "d_atk_n4_vid16": ("S1", "default-106", "Attack N=4, video 16 Mb/s (plateau)"),
    "d_atk_n4_vid24": ("S1", "default-106", "Attack N=4, video 24 Mb/s (plateau)"),
    # Capacity probe: no adversary, victim video raised until it self-congests (VALID=NO expected).
    "cap_base_vid24": ("S1", "default-106", "Capacity probe, video 24 Mb/s"),
    "cap_base_vid32": ("S1", "default-106", "Capacity probe, video 32 Mb/s"),
    "cap_base_vid40": ("S1", "default-106", "Capacity probe, video 40 Mb/s"),
    "g_drain_video_on": ("S1", "default-106", "DRAIN, video continues"),
    "g_drain_video_off": ("S1", "default-106", "DRAIN, video stops at attack end"),
    # --- S3_prb51: bandwidth generality, 20 MHz ---
    "e_base51": ("S3", "default-51", "Baseline, video 8 Mb/s, 51 PRB"),
    "e_atk51_n4": ("S3", "default-51", "Attack N=4, video 8 Mb/s, 51 PRB"),
    "cap51_base_vid16": ("S3", "default-51", "Capacity probe, video 16 Mb/s, 51 PRB"),
    "cap51_base_vid24": ("S3", "default-51", "Capacity probe, video 24 Mb/s, 51 PRB"),
    # --- S4 / S5: the two defences ---
    "f1_slicing_n4": ("S4", "slicing-106", "Defence: UL slice enforcement (victim on its own slice, B0)"),
    "f2_aqm_n4": ("S5", "aqm-106", "Defence: host AQM (fq_codel on the victim uplink)"),
    # --- S6: positive control (expected invalid: the tripped failsafe fails the mission gates) ---
    "pos_control_route": ("S6", "default-106", "Positive control: command-leg blackhole > FS_GCS_TIMEOUT"),
}


# Conditions pooled for aggregation (a rerun counts as an extra replicate of the same condition).
POOL: dict[str, str] = {}


def pooled(cid: str) -> str:
    return POOL.get(cid, cid)


def condition_order(cid: str) -> tuple[int, str]:
    keys = list(CONDITIONS)
    return (keys.index(cid) if cid in keys else len(keys), cid)


def open_maybe_gz(path: Path):
    """Open a result file, transparently accepting a gzipped copy beside it.

    The two bulkiest per-run captures (``run_kpi_report.json`` and ``mac-prb-raw.log``) are released
    gzipped: together they are 417 MB raw and 39 MB compressed, which is the difference between an
    artifact repository that can be hosted and one that cannot. Everything reads through here, so the
    analysis is identical whether a run directory holds the plain file or the ``.gz``.
    """
    if path.exists():
        return open(path, encoding="utf-8")
    gz = path.with_name(path.name + ".gz")
    if gz.exists():
        return gzip.open(gz, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")       # raises FileNotFoundError naming the plain path


def result_exists(path: Path) -> bool:
    """True if the file is present either plain or gzipped."""
    return path.exists() or path.with_name(path.name + ".gz").exists()


def load_json(path: Path) -> Any:
    with open_maybe_gz(path) as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def get(d: Any, path: str, default: Any = None) -> Any:
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def num(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x)) else None


def stats(values: Iterable[Any]) -> dict[str, Optional[float]]:
    """mean / sd / min / max over runs (sd is the sample standard deviation; None if n < 2)."""
    xs = [v for v in (num(x) for x in values) if v is not None]
    if not xs:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None}
    return {"n": len(xs), "mean": statistics.fmean(xs),
            "sd": statistics.stdev(xs) if len(xs) > 1 else None, "min": min(xs), "max": max(xs)}


def resolve_run_dir(raw: str | None) -> Path | None:
    """Locate a run directory recorded in a batch log, wherever the artifact is checked out.

    The batch logs store the path the run was written to. In the released artifact that path is the
    placeholder ``ARTIFACT_ROOT/...`` (the recording machine's home directory is not published), and
    on a reviewer's machine the checkout lives somewhere else entirely. Both cases resolve against the
    artifact root computed from this file's location, so the manifest is independent of where the
    results were produced.
    """
    if not raw:
        return None
    p = Path(raw)
    for prefix in ("ARTIFACT_ROOT/", "ARTIFACT_ROOT"):
        if raw.startswith(prefix):
            return ARTIFACTS / raw[len(prefix):].lstrip("/")
    if p.is_absolute():
        if p.is_dir():
            return p
        # An absolute path from another machine: keep the tail from 02_results/ onwards.
        parts = p.parts
        if "02_results" in parts:
            return ARTIFACTS / Path(*parts[parts.index("02_results"):])
        return p
    return ARTIFACTS / p

def rel_to_artifact(p: Path) -> str:
    """Path as a string relative to the artifact root, never absolute.

    Anything written into out/ is published, so it must not carry the directory layout of the machine
    that produced it. Paths outside the artifact fall back to the basename for the same reason.
    """
    try:
        return str(Path(p).resolve().relative_to(ARTIFACTS))
    except ValueError:
        return Path(p).name


def load_manifest() -> dict[str, Any]:
    if not MANIFEST.is_file():
        raise SystemExit(f"missing {MANIFEST}; run build_manifest.py first")
    return load_json(MANIFEST)


def included_runs(manifest: dict[str, Any], categories: tuple[str, ...] = ("campaign", "capacity_probe")) -> list[dict[str, Any]]:
    runs = [r for r in manifest["runs"] if r["category"] in categories]
    return sorted(runs, key=lambda r: (condition_order(r["condition"]), r["run_id"]))


def run_path(run: dict[str, Any]) -> Path:
    return ARTIFACTS / run["run_dir"]


def tex_escape(s: str) -> str:
    return (s.replace("\\", r"\textbackslash{}").replace("_", r"\_").replace("%", r"\%")
             .replace("&", r"\&").replace("#", r"\#"))


def fmt_mean_sd(st: dict[str, Optional[float]], scale: float = 1.0, digits: int = 1) -> str:
    if st.get("mean") is None:
        return "--"
    m = st["mean"] * scale
    if st.get("sd") is None:
        return f"{m:.{digits}f}"
    return f"{m:.{digits}f} $\\pm$ {st['sd'] * scale:.{digits}f}"


def telemetry_delivery(run_dir: Path, phase: str = "ATTACK") -> Optional[float]:
    """Share of the LOCAL_POSITION_NED messages the UAV emitted inside `phase` that ever reach the GCS.

    Messages are matched by the same (system_id, time_boot_ms) key as delivery age, and the phase is
    assigned at UAV egress against T0, so each message is counted once, in the window where it was
    generated. This replaces the file-level ``uav_to_gcs_delivery_ratio`` (GCS rows / UAV rows), whose
    two capture files cover different spans: it reads 0.84 at baseline although nothing is lost.
    """
    import freshness_metrics as fm
    from kpi_leg import assign_phase_half_open
    from phase_windows import load_t0_mono_ns
    run_dir = Path(run_dir)
    uav_csv, gcs_csv = run_dir / "c2_telemetry_uav.csv", run_dir / "telemetry_downlink_gcs.csv"
    win_p = run_dir / "flyblind_phase_windows.json"
    t0 = load_t0_mono_ns(run_dir)
    if t0 is None or not (uav_csv.is_file() and gcs_csv.is_file() and win_p.is_file()):
        return None
    windows = load_json(win_p).get("windows") or {}
    with open(gcs_csv, encoding="utf-8") as f:
        got = {fm._telemetry_uav_lookup_key(r) for r in csv.DictReader(f)}
    sent = delivered = 0
    with open(uav_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = fm._telemetry_row_int(r, "t_mono_ns")
            key = fm._telemetry_uav_lookup_key(r)
            if t is None or key is None or assign_phase_half_open((t - t0) / 1e9, windows) != phase:
                continue
            sent += 1
            delivered += key in got
    return delivered / sent if sent else None

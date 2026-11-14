##################################################################################################################################
# Module: results_layout.py (naming and layout of the 02_results/ directory tree)                                                #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Mitigation-stack variant of results_layout.py.                                                                    #
##################################################################################################################################

"""Human-readable results layout under artifacts/02_results/.

Two levels: one **campaign** folder per (config), with the shared
``analysis_params.json`` + ``matrix_*.json`` and one ``RUNxx`` subfolder per run.

Default (when ``--out`` is omitted)::

    artifacts/02_results/
      106_PRB-CROSS-4_ROGUES-BASELINE-600S-12MPBS/
        analysis_params.json
        matrix_….json
        RUN01/
        RUN02/
        RUN03/
      106_PRB-CROSS-4_ROGUES-ATK_N2-600S-8MPBS/
        …
        RUN01/
      106_PRB-CROSS-4_ROGUES-ATK_N2_AND_MITIGATION_PRB60-60S-12MPBS/
        …

Campaign pattern::

    {PRB}_PRB-{TOPO}-{N}_ROGUES-{SCENARIO}-{DUR}S-{VID}MPBS

Run subfolder pattern::

    RUN{RR}          (RR = repeat index, zero-padded; _rec for recovery retries)

- ``{N}_ROGUES`` = testbed capacity (primary + EXTRA_ROGUE_COUNT), **not** attack dose.
- Dose lives in ``ATK_N{k}``.
- Collision: if a folder exists, append ``_a``, ``_b``, … (alphabetical).

Mitigation tokens (scenario suffix ``_AND_…``), ranked for the paper:

Rank 1 (report now)
  - no companion → ``0MPBS`` (video field; not a scenario token)
  - MinRatio slice A (rogues) → ``MITIGATION_A{n}`` (A0 omitted; A60→A60)
  - MinRatio slice B / reservation → ``MITIGATION_PRB{resv}`` (B0 omitted; B60→PRB60)
  - dose n=1..4 → ``ATK_Nn`` (already in scenario)
  - rate-limit A → ``MITIGATION_RATELIMIT_A`` (``--mitigation-rate-limit-a`` on
    attack; naming-only ``--results-tag RATELIMIT_A`` on the control baseline)

Rank 2 (ablation / contrast)
  - AQM/tc UL → ``MITIGATION_AQM_UL`` (``args.mitigation_aqm_ul``)

Rank 3 (mitigation testbed only)
  - UL rrmPolicy → ``MITIGATION_UL_RRMPOLICY``
  - NSSAI-keyed UL rrmPolicyRatio → ``MITIGATION_UL_RRMPOLICY``

Out of scope on pinned ORANSlice 2024.w28: multi-PDU 5QI C2≠video split
(``MITIGATION_5QI`` / ``5qi-split``) — nrUE has no multi-PDU parser (OAI MR !3769).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

# lib/ → 02_experiments/ → 01_scripts/ → artifacts/
_LIB_DIR = Path(__file__).resolve().parent
_EXPERIMENTS_DIR = _LIB_DIR.parent
DEFAULT_RESULTS_ROOT = _EXPERIMENTS_DIR.parent.parent / "02_results"


def default_results_root() -> Path:
    return DEFAULT_RESULTS_ROOT


def video_mbps_token(mbps: float) -> str:
    v = max(0.0, float(mbps or 0.0))
    if abs(v - round(v)) < 1e-6:
        return f"{int(round(v))}MPBS"
    # 11.5 → 11p5MPBS
    body = f"{v:.1f}".rstrip("0").rstrip(".").replace(".", "p")
    return f"{body}MPBS"


def normalize_results_tag(raw: Any) -> str | None:
    """Folder token only. ``RATELIMIT_A`` → ``MITIGATION_RATELIMIT_A``."""
    if raw is None:
        return None
    tag = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
    if not tag:
        return None
    if not tag.startswith("MITIGATION_"):
        tag = f"MITIGATION_{tag}"
    return tag


def testbed_rogue_capacity(
    *,
    stamp_extra_rogues: int | None = None,
    fallback: int = 4,
) -> int:
    """Primary rogue + EXTRA_ROGUE_COUNT extras (= pods available in testbed)."""
    if stamp_extra_rogues is None:
        return max(1, int(fallback))
    return max(1, int(stamp_extra_rogues) + 1)


def build_scenario_token(args: Any) -> str:
    """BASELINE | ATK_Nn | … plus optional ``_AND_MITIGATION_*`` chain."""
    cond = getattr(args, "condition", "baseline")
    if cond == "baseline":
        base = "BASELINE"
    elif getattr(args, "attack", None) == "multi-rogue":
        base = f"ATK_N{max(1, int(getattr(args, 'multi_rogue_count', 1) or 1))}"
    elif getattr(args, "attack", None) in (
        "multi-rogue-dl", "multi-rogue-sync", "multi-rogue-dl-sync",
    ):
        n = max(1, int(getattr(args, "multi_rogue_count", 1) or 1))
        suffix = {
            "multi-rogue-dl": "DL",
            "multi-rogue-sync": "UL_SYNC",
            "multi-rogue-dl-sync": "DL_SYNC",
        }[getattr(args, "attack", None)]
        base = f"ATK_N{n}_{suffix}"
    else:
        atk = str(getattr(args, "attack", "attack") or "attack")
        base = "ATK_" + atk.upper().replace("-", "_")

    mit: list[str] = []
    min_a = int(getattr(args, "min_ratio_slice_a", 0) or 0)
    if min_a > 0:
        # Rank 1 contrast: MinRatio on attacker slice (A60 / A100).
        mit.append(f"MITIGATION_A{min_a}")
    resv = int(getattr(args, "reservation", 0) or 0)
    if resv > 0:
        # Rank 1: MinRatio B60/B100 (and any >0) as C2/UAS defense.
        mit.append(f"MITIGATION_PRB{resv}")
    if getattr(args, "mitigation_rate_limit_a", False):
        mit.append("MITIGATION_RATELIMIT_A")
    if getattr(args, "mitigation_aqm_ul", False):
        mit.append("MITIGATION_AQM_UL")
    if getattr(args, "mitigation_ul_rrmpolicy", False):
        mit.append("MITIGATION_UL_RRMPOLICY")
    extra = normalize_results_tag(getattr(args, "results_tag", None))
    if extra and extra not in mit:
        mit.append(extra)

    if not mit:
        return base
    return base + "".join(f"_AND_{m}" for m in mit)


def build_campaign_dirname(
    args: Any,
    *,
    rogue_capacity: int | None = None,
    stamp_extra_rogues: int | None = None,
) -> str:
    """Campaign folder (holds analysis_params + matrix + RUNxx subfolders)."""
    prb = int(getattr(args, "gnb_prb", 0) or 0)
    topo = str(getattr(args, "topology", "cross") or "cross").upper()
    if rogue_capacity is None:
        rogue_capacity = testbed_rogue_capacity(
            stamp_extra_rogues=stamp_extra_rogues, fallback=4,
        )
    scenario = build_scenario_token(args)
    dur = int(getattr(args, "duration", 0) or 0)
    vid = video_mbps_token(float(getattr(args, "companion_video_mbps", 0) or 0))
    return (
        f"{prb}_PRB-{topo}-{int(rogue_capacity)}_ROGUES-"
        f"{scenario}-{dur}S-{vid}"
    )


def build_run_subdirname(run_num: int, *, recovery: bool = False) -> str:
    name = f"RUN{int(run_num):02d}"
    return f"{name}_rec" if recovery else name


def allocate_unique_dirname(parent: Path, base: str) -> str:
    """If ``parent/base`` exists, try ``base_a``, ``base_b``, …"""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    candidate = base
    if not (parent / candidate).exists():
        return candidate
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    for ch in alphabet:
        candidate = f"{base}_{ch}"
        if not (parent / candidate).exists():
            return candidate
    for a in alphabet:
        for b in alphabet:
            candidate = f"{base}_{a}{b}"
            if not (parent / candidate).exists():
                return candidate
    raise RuntimeError(f"no free suffix under {parent} for base={base!r}")


def allocate_unique_filename(parent: Path, filename: str) -> str:
    """If ``parent/filename`` exists, try ``stem_a.ext``, ``stem_b.ext``, …"""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(filename)
    stem, suffix = path.stem, path.suffix
    candidate = path.name
    if not (parent / candidate).exists():
        return candidate
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    for ch in alphabet:
        candidate = f"{stem}_{ch}{suffix}"
        if not (parent / candidate).exists():
            return candidate
    for a in alphabet:
        for b in alphabet:
            candidate = f"{stem}_{a}{b}{suffix}"
            if not (parent / candidate).exists():
                return candidate
    raise RuntimeError(f"no free filename under {parent} for base={filename!r}")


def resolve_campaign_dir(
    args: Any,
    out_root: Path,
    *,
    paper_layout: bool,
    stamp_extra_rogues: int | None = None,
) -> Path:
    """Where runs + analysis_params + matrix live.

    paper_layout → ``out_root/<campaign_name>`` (reused across repeats of the
    same config). legacy → ``out_root`` unchanged.
    """
    out_root = Path(out_root)
    if not paper_layout:
        return out_root
    name = build_campaign_dirname(args, stamp_extra_rogues=stamp_extra_rogues)
    return out_root / name


def resolve_run_dirname(
    args: Any,
    run_num: int,
    campaign_dir: Path,
    *,
    paper_layout: bool,
    legacy_slug: str,
    recovery: bool = False,
    clamp_fn: Callable[[str, Path], str] | None = None,
) -> str:
    """Pick run subfolder name under ``campaign_dir``."""
    campaign_dir = Path(campaign_dir)
    if paper_layout:
        base = build_run_subdirname(run_num, recovery=recovery)
        return allocate_unique_dirname(campaign_dir, base)
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = f"_r{run_num}_rec" if recovery else f"_r{run_num}"
    name = f"{ts}_{legacy_slug}{suffix}"
    if clamp_fn is not None:
        name = clamp_fn(name, campaign_dir)
    return name

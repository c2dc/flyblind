#!/usr/bin/env python3
##################################################################################################################################
# Module: run_report.py (Automatic per-run report)                                                                               #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Input: run_dir. Output: run_dir/run_kpi_report.{json,md} Command leg x return leg table + criteria C1..C4.        #
#                                                                                                                                #
# Reuses kpi_leg / kpi_c2_real / link_vivacity. The C1 belief comes from the telemetry merge (freshness / belief_vs_truth).      #
##################################################################################################################################
"""Automatic per-run report.

Input: run_dir. Output: run_dir/run_kpi_report.{json,md}
Command leg x return leg table + criteria C1..C4.

Reuses kpi_leg / kpi_c2_real / link_vivacity.
The C1 belief comes from the telemetry merge (freshness / belief_vs_truth).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
LIB = EXP / "lib"
for p in (str(HERE), str(LIB), str(EXP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kpi_leg import evaluate_c1_leg, evaluate_c4_kpi_slice  # noqa: E402
from kpi_c2_real import build_c2_kpi_pair  # noqa: E402


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _belief_from_run(run_dir: Path) -> dict[str, Any]:
    """Pull belief_p50_m from freshness / four_layer / mission artifacts."""
    out: dict[str, Any] = {
        "belief_p50_m": None,
        "belief_p99_m": None,
        "source": None,
    }
    candidates = [
        run_dir / "freshness_summary.json",
        run_dir / "four_layer.json",
        run_dir / "run_meta.json",
    ]
    for path in candidates:
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        # freshness_summary style
        tel = (
            ((data.get("telemetry_downlink") or {}) if "telemetry_downlink" in data else None)
            or ((data.get("freshness") or {}).get("telemetry_downlink") if isinstance(data.get("freshness"), dict) else None)
            or {}
        )
        bv = tel.get("belief_vs_truth_m") or {}
        if bv.get("p50") is not None:
            out["belief_p50_m"] = bv.get("p50")
            out["belief_p99_m"] = bv.get("p99")
            out["source"] = str(path.name) + ":telemetry_downlink.belief_vs_truth_m"
            return out
        # four_layer / run_meta col style
        for key in ("belief_p50_m",):
            if data.get(key) is not None:
                out["belief_p50_m"] = data.get("belief_p50_m")
                out["belief_p99_m"] = data.get("belief_p99_m")
                out["source"] = str(path.name)
                return out
        # nested outcomes
        fr = data.get("freshness") or {}
        if isinstance(fr, dict):
            tel2 = fr.get("telemetry_downlink") or {}
            bv2 = tel2.get("belief_vs_truth_m") or {}
            if bv2.get("p50") is not None:
                out["belief_p50_m"] = bv2.get("p50")
                out["belief_p99_m"] = bv2.get("p99")
                out["source"] = str(path.name) + ":freshness.telemetry_downlink"
                return out
    return out


def _enrich_c1_with_belief(c1: dict[str, Any], belief: dict[str, Any]) -> dict[str, Any]:
    out = dict(c1)
    b50 = belief.get("belief_p50_m")
    out["belief_p50_m"] = b50
    out["belief_source"] = belief.get("source")
    # Plan C1 confirmation: belief p50 >= 5 m (or >= 5x baseline; 5x needs a reference)
    if isinstance(b50, (int, float)):
        out["belief_confirmation_ge_5m"] = float(b50) >= 5.0
        out["belief_note"] = (
            "C1 confirmation: belief_p50_m >= 5 m "
            f"(obs={b50}; source={belief.get('source')})"
        )
    else:
        out["belief_confirmation_ge_5m"] = None
        out["belief_note"] = (
            "belief missing in run_dir; primary C1 (OWD) still holds; "
            "belief confirmation = NOT VERIFIED"
        )
    return out


def _c2_ni_placeholder(run_dir: Path) -> dict[str, Any]:
    """C2 (non-interference) needs an instrumentation on/off pair. Only a marker here."""
    return {
        "criterion": "C2",
        "pass": None,
        "note": (
            "C2 compares instrumentation on vs off. "
            "A single-run report does not evaluate C2; use compare_ni_runs."
        ),
        "run_dir": str(run_dir),
    }


def _c3_paper_comparable(run_dir: Path) -> dict[str, Any]:
    meta = _load_json(run_dir / "run_meta.json") or {}
    four = _load_json(run_dir / "four_layer.json") or {}
    # Look for paper_comparable / orbit acquired flags if present
    pc = (
        meta.get("paper_comparable")
        or (meta.get("gates") or {}).get("paper_comparable")
        or four.get("paper_comparable")
        or (meta.get("outcomes") or {}).get("paper_comparable")
    )
    if isinstance(pc, dict):
        return {
            "criterion": "C3",
            "pass": pc.get("pass") if "pass" in pc else pc.get("ok"),
            "detail": pc,
            "source": "run_meta/four_layer",
        }
    # Fallback: mission orbit markers in mission_summary / c2
    c2 = _load_json(run_dir / "mission_summary.json") or {}
    orbit = c2.get("orbit") or c2.get("paper_comparable") or {}
    if orbit:
        return {
            "criterion": "C3",
            "pass": orbit.get("acquired") or orbit.get("pass"),
            "detail": orbit,
            "source": "mission_summary",
        }
    return {
        "criterion": "C3",
        "pass": None,
        "note": "paper_comparable not found in this run_dir (NOT VERIFIED)",
        "source": None,
    }


def _link_vivacity(run_dir: Path) -> dict[str, Any]:
    try:
        from link_vivacity import analyze_run  # type: ignore
    except Exception:
        try:
            from instrumentation.link_vivacity import analyze_run  # type: ignore
        except Exception as exc:
            return {"error": f"link_vivacity import failed: {exc}"}
    try:
        return analyze_run(run_dir)
    except TypeError:
        # CLI-shaped API: try module-level helpers
        pass
    except Exception as exc:
        return {"error": str(exc)}
    # Fallback: invoke as subprocess-less by reading expected outputs if present
    cached = _load_json(run_dir / "link_vivacity.json")
    if cached:
        return cached
    return {"error": "link_vivacity.analyze_run unavailable; no link_vivacity.json"}


def _side_by_side(command_leg: dict[str, Any], return_leg: dict[str, Any]) -> dict[str, Any]:
    def slice_kpi(block: Optional[dict]) -> dict[str, Any]:
        if not block:
            return {}
        # Prefer ATTACK phase if present, else ALL
        phases = block.get("phases") or {}
        phase = phases.get("ATTACK") or phases.get("ALL") or {}
        owd = (phase.get("owd_s") or {}).get("primary") or {}
        return {
            "phase": "ATTACK" if "ATTACK" in phases else "ALL",
            "n": phase.get("n"),
            "owd_p50_s": owd.get("p50"),
            "owd_p99_s": owd.get("p99"),
            "owd_max_s": owd.get("max"),
            "criterion_C1": phase.get("criterion_C1"),
            "criterion_C4": phase.get("criterion_C4"),
            "delivery_rate_post_warmup": block.get("delivery_rate_post_warmup"),
        }

    return {
        "command": slice_kpi(command_leg),
        "return": slice_kpi(return_leg),
    }


def build_report(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    belief = _belief_from_run(run_dir)
    c2_pair: dict[str, Any] = {}
    try:
        try:
            from phase_windows import load_run_windows, load_t0_mono_ns  # type: ignore
        except ImportError:
            from instrumentation.phase_windows import load_run_windows, load_t0_mono_ns  # type: ignore
        c2_pair = build_c2_kpi_pair(
            run_dir,
            windows=load_run_windows(run_dir),
            t0_mono_ns=load_t0_mono_ns(run_dir),
        )
    except Exception as exc:
        c2_pair = {"error": str(exc)}

    command_leg = (c2_pair.get("command") if isinstance(c2_pair, dict) else None) or {}
    return_leg = (c2_pair.get("return") if isinstance(c2_pair, dict) else None) or {}

    # Enrich C1 belief on both legs (confirmation is return-leg belief typically)
    for leg_name, leg in (("command", command_leg), ("return", return_leg)):
        if not isinstance(leg, dict):
            continue
        phases = leg.get("phases") or {}
        for fname, fblock in list(phases.items()):
            if not isinstance(fblock, dict):
                continue
            c1 = fblock.get("criterion_C1")
            if isinstance(c1, dict):
                fblock["criterion_C1"] = _enrich_c1_with_belief(c1, belief)

    viv = _link_vivacity(run_dir)
    # Attach K7/K8 into C4 if available (focus ATTACK else ALL)
    k7 = k8 = None
    if isinstance(viv, dict) and "error" not in viv:
        k7_by = viv.get("k7_command") or {}
        k8_by = viv.get("k8_return") or {}
        focus = "ATTACK" if "ATTACK" in k7_by else "ALL"
        k7 = k7_by.get(focus)
        k8 = k8_by.get(focus)
    if isinstance(command_leg, dict):
        for fblock in (command_leg.get("phases") or {}).values():
            if isinstance(fblock, dict) and isinstance(fblock.get("criterion_C4"), dict):
                c4 = evaluate_c4_kpi_slice(
                    leg="command",
                    fractions=fblock.get("budget_fractions") or [],
                    reliabilities=fblock.get("reliability_22125") or [],
                    interruptions=fblock.get("interruptions") or {},
                    k7_fs_gcs=k7,
                    k8_qgc_style=k8,
                )
                fblock["criterion_C4"] = c4
    if isinstance(return_leg, dict):
        for fblock in (return_leg.get("phases") or {}).values():
            if isinstance(fblock, dict) and isinstance(fblock.get("criterion_C4"), dict):
                c4 = evaluate_c4_kpi_slice(
                    leg="return",
                    fractions=fblock.get("budget_fractions") or [],
                    reliabilities=fblock.get("reliability_22125") or [],
                    interruptions=fblock.get("interruptions") or {},
                    k7_fs_gcs=k7,
                    k8_qgc_style=k8,
                )
                fblock["criterion_C4"] = c4

    table = _side_by_side(
        command_leg if isinstance(command_leg, dict) else {},
        return_leg if isinstance(return_leg, dict) else {},
    )

    # Top-level C1/C4 snapshots (ATTACK or ALL)
    def top_c(leg: dict) -> dict:
        phases = leg.get("phases") or {}
        phase = phases.get("ATTACK") or phases.get("ALL") or {}
        return {
            "C1": phase.get("criterion_C1"),
            "C4": phase.get("criterion_C4"),
        }

    report = {
        "schema": "flyblind_run_kpi_report_v1",
        "run_dir": str(run_dir),
        "run_meta_valid": (_load_json(run_dir / "run_meta.json") or {}).get("valid"),
        "belief": belief,
        "legs_table": table,
        "criteria": {
            "C1_command": top_c(command_leg if isinstance(command_leg, dict) else {}).get("C1"),
            "C1_return": top_c(return_leg if isinstance(return_leg, dict) else {}).get("C1"),
            "C2": _c2_ni_placeholder(run_dir),
            "C3": _c3_paper_comparable(run_dir),
            "C4_command": top_c(command_leg if isinstance(command_leg, dict) else {}).get("C4"),
            "C4_return": top_c(return_leg if isinstance(return_leg, dict) else {}).get("C4"),
        },
        "kpi_c2_real": c2_pair,
        "link_vivacity": viv,
    }
    return report


def render_md(report: dict[str, Any]) -> str:
    t = report.get("legs_table") or {}
    cmd = t.get("command") or {}
    ret = t.get("return") or {}
    lines = [
        "# Per-run report",
        "",
        f"run_dir: `{report.get('run_dir')}`",
        f"run_meta.valid: {report.get('run_meta_valid')}",
        "",
        "## Command leg x return leg table",
        "",
        "| metric | command | return |",
        "|---|---:|---:|",
        f"| phase | {cmd.get('phase')} | {ret.get('phase')} |",
        f"| n | {cmd.get('n')} | {ret.get('n')} |",
        f"| owd p50 (s) | {cmd.get('owd_p50_s')} | {ret.get('owd_p50_s')} |",
        f"| owd p99 (s) | {cmd.get('owd_p99_s')} | {ret.get('owd_p99_s')} |",
        f"| owd max (s) | {cmd.get('owd_max_s')} | {ret.get('owd_max_s')} |",
        "",
        "## Criteria",
        "",
    ]
    crit = report.get("criteria") or {}
    for key in ("C1_command", "C1_return", "C2", "C3", "C4_command", "C4_return"):
        block = crit.get(key) or {}
        lines.append(f"### {key}")
        lines.append("```json")
        lines.append(json.dumps(block, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
    belief = report.get("belief") or {}
    lines.append("## Belief (C1 confirmation)")
    lines.append(f"- belief_p50_m: {belief.get('belief_p50_m')}")
    lines.append(f"- source: {belief.get('source')}")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-run report")
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--out-json", type=Path, default=None)
    ap.add_argument("--out-md", type=Path, default=None)
    args = ap.parse_args()
    report = build_report(args.run_dir)
    out_json = args.out_json or (args.run_dir / "run_kpi_report.json")
    out_md = args.out_md or (args.run_dir / "run_kpi_report.md")
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(render_md(report), encoding="utf-8")
    print(f"[run_report] json → {out_json}")
    print(f"[run_report] md   → {out_md}")


if __name__ == "__main__":
    main()

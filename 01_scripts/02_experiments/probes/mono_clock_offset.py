#!/usr/bin/env python3
##################################################################################################################################
# Module: mono_clock_offset.py (inter-pod CLOCK_MONOTONIC offset probe)                                                          #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Measures GCS/UAV time namespace offset for belief_vs_truth diagnostic validation.                                 #
##################################################################################################################################

"""Measure CLOCK_MONOTONIC offset between Kind host and FlyBlind UE pods.

belief_vs_truth_m joins UAV egress vs GCS arrival on t_mono. On a single-node
Kind cluster the pods usually share the host monotonic clock; this probe
*measures* that instead of assuming it.

Usage:
  python3 mono_clock_offset.py <kube_context> <namespace> [--n 10] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional


def _pct(vals: list[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return float(s[0])
    k = (p / 100.0) * (len(s) - 1)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return float(s[lo] * (1.0 - frac) + s[hi] * frac)


def summarize_offsets_ms(
    host_uav_ms: list[float],
    host_gcs_ms: list[float],
) -> dict[str, Any]:
    """Pure summary from paired (host−pod) offset samples in milliseconds.

    Positive host_uav_ms means host monotonic is ahead of UAV's reading at the
    sample instant (includes kubectl-exec latency jitter).
    """
    uav_gcs = [a - b for a, b in zip(host_uav_ms, host_gcs_ms)]
    def block(name: str, samples: list[float]) -> dict[str, Any]:
        if not samples:
            return {"n": 0}
        med = float(statistics.median(samples))
        return {
            "n": len(samples),
            "median_ms": round(med, 3),
            "mean_ms": round(float(statistics.mean(samples)), 3),
            "p5_ms": round(_pct(samples, 5) or 0.0, 3),
            "p95_ms": round(_pct(samples, 95) or 0.0, 3),
            "spread_p95_p5_ms": round((_pct(samples, 95) or 0.0) - (_pct(samples, 5) or 0.0), 3),
            "max_abs_ms": round(max(abs(x) for x in samples), 3),
        }

    uav = block("host_minus_uav", host_uav_ms)
    gcs = block("host_minus_gcs", host_gcs_ms)
    cross = block("uav_minus_gcs", uav_gcs)
    # Systematic skew proxy: |median|; jitter proxy: spread.
    skew_ms = abs(float(cross.get("median_ms") or 0.0))
    jitter_ms = float(cross.get("spread_p95_p5_ms") or 0.0)
    return {
        "host_minus_uav_ms": uav,
        "host_minus_gcs_ms": gcs,
        "uav_minus_gcs_ms": cross,
        "skew_abs_median_ms": round(skew_ms, 3),
        "jitter_spread_ms": round(jitter_ms, 3),
        "appendix_line": (
            "measured inter-pod CLOCK_MONOTONIC offset (UAV−GCS): "
            f"median {cross.get('median_ms')} ms, "
            f"spread(p95−p5) {cross.get('spread_p95_p5_ms')} ms "
            f"(N={cross.get('n')})"
        ),
    }


def _mono_ns_host() -> int:
    return time.monotonic_ns()


def _mono_ns_pod(ctx: str, ns: str, deploy: str, container: str = "nr-ue") -> Optional[int]:
    """Best-effort: print monotonic_ns inside the pod (python3)."""
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", ns, f"deploy/{deploy}",
        "-c", container, "--",
        "python3", "-c", "import time; print(time.monotonic_ns(), flush=True)",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return None
    try:
        return int(line[-1].strip())
    except ValueError:
        return None


def measure_mono_clock_offset(
    ctx: str,
    ns: str,
    *,
    n: int = 10,
    uav_deploy: str = "oai-ue-uav",
    gcs_deploy: str = "oai-ue-gcs",
) -> dict[str, Any]:
    """N paired samples: host vs UAV, host vs GCS (near-simultaneous kubectl exec)."""
    out: dict[str, Any] = {
        "ok": False,
        "n_requested": int(n),
        "kube_context": ctx,
        "namespace": ns,
    }
    host_uav: list[float] = []
    host_gcs: list[float] = []
    failures = 0
    for _ in range(max(1, int(n))):
        with ThreadPoolExecutor(max_workers=3) as pool:
            fut_host = pool.submit(_mono_ns_host)
            fut_uav = pool.submit(_mono_ns_pod, ctx, ns, uav_deploy)
            fut_gcs = pool.submit(_mono_ns_pod, ctx, ns, gcs_deploy)
            # Host stamp taken as close as possible to pod queries.
            t_host = fut_host.result()
            t_uav = fut_uav.result()
            t_gcs = fut_gcs.result()
        # Refresh host after joins to bound exec latency (use mid of before/after).
        t_host_after = _mono_ns_host()
        t_host_mid = (t_host + t_host_after) // 2
        if t_uav is None or t_gcs is None:
            failures += 1
            continue
        host_uav.append((t_host_mid - t_uav) / 1e6)
        host_gcs.append((t_host_mid - t_gcs) / 1e6)
        time.sleep(0.05)
    out["failures"] = failures
    if len(host_uav) < 3:
        out["reason"] = "too few successful kubectl mono samples"
        return out
    out.update(summarize_offsets_ms(host_uav, host_gcs))
    out["ok"] = True
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kube_context")
    ap.add_argument("namespace")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args(argv)
    result = measure_mono_clock_offset(args.kube_context, args.namespace, n=args.n)
    text = json.dumps(result, indent=2) + "\n"
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text)
    print(text, end="")
    if result.get("ok") and result.get("appendix_line"):
        print(result["appendix_line"], file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
##################################################################################################################################
# Module: host_uav_time_ns.py (Host vs UAV pod check: shared CLOCK_MONOTONIC)                                                    #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Context: owd_probe.py L261-264 compares the GCS pod ns/time with the UAV pod's. K7 uses the HOST T0_mono_ns       #
#   (orchestrate, time.monotonic_ns) with timestamps from the UAV pod (proxy/forwarder). This check confirms that the host and   #
#   the UAV pod read the same CLOCK_MONOTONIC; otherwise the run must fail.                                                      #
#                                                                                                                                #
# Criterion (passes if either holds): 1. same /proc/self/ns/time inode on the host and in the pod; 2. different inode, but in    #
#   the pod: a. ns/time == ns/time_for_children (the kernel shows in /proc/self/timens_offsets the offsets of time_for_children: #
#   kernel/time/namespace.c proc_timens_show_offsets -> timens_for_children_get; only with this equality are they those of the   #
#   namespace in use); b. offsets monotonic 0 0 and boottime 0 0; c. the pod's time.monotonic_ns() within the host's [t1, t2],   #
#   read before and after the kubectl exec.                                                                                      #
#                                                                                                                                #
# Kind/Docker may give the node (and the pods) their own time namespace with zero offsets; the inode differs, but the clock is   #
#   the host's.                                                                                                                  #
##################################################################################################################################
"""Host vs UAV pod check: shared CLOCK_MONOTONIC.

Context:
  owd_probe.py L261-264 compares the GCS pod ns/time with the UAV pod's.
  K7 uses the HOST T0_mono_ns (orchestrate, time.monotonic_ns) with timestamps
  from the UAV pod (proxy/forwarder). This check confirms that the host and the UAV pod
  read the same CLOCK_MONOTONIC; otherwise the run must fail.

Criterion (passes if either holds):
  1. same /proc/self/ns/time inode on the host and in the pod;
  2. different inode, but in the pod:
     a. ns/time == ns/time_for_children (the kernel shows in
        /proc/self/timens_offsets the offsets of time_for_children:
        kernel/time/namespace.c proc_timens_show_offsets ->
        timens_for_children_get; only with this equality are they those of the
        namespace in use);
     b. offsets monotonic 0 0 and boottime 0 0;
     c. the pod's time.monotonic_ns() within the host's [t1, t2], read before
        and after the kubectl exec.

Kind/Docker may give the node (and the pods) their own time namespace with
zero offsets; the inode differs, but the clock is the host's.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from typing import Any, Optional

SCHEMA = "flyblind_host_uav_time_ns_v2"

POD_CLOCK_PROBE_PY = (
    "import json,os,time;"
    "print(json.dumps({"
    "'ns':os.readlink('/proc/self/ns/time'),"
    "'ns_for_children':os.readlink('/proc/self/ns/time_for_children'),"
    "'timens_offsets':open('/proc/self/timens_offsets').read(),"
    "'mono_ns':time.monotonic_ns()}))"
)


def read_host_time_ns() -> str:
    return os.readlink("/proc/self/ns/time")


def kubectl_read_pod_clock_cmd(
    ctx: str, namespace: str, deploy: str = "oai-ue-uav", container: str = "nr-ue",
) -> list[str]:
    return [
        "kubectl", "--context", ctx, "exec", "-n", namespace,
        f"deploy/{deploy}", "-c", container, "--",
        "python3", "-c", POD_CLOCK_PROBE_PY,
    ]


def parse_timens_offsets(text: Optional[str]) -> Optional[dict[str, list[int]]]:
    """'monotonic 0 0\\nboottime 0 0' -> {'monotonic': [0, 0], 'boottime': [0, 0]}."""
    if not text:
        return None
    out: dict[str, list[int]] = {}
    for line in text.strip().splitlines():
        parts = line.split()
        if len(parts) != 3:
            return None
        try:
            out[parts[0]] = [int(parts[1]), int(parts[2])]
        except ValueError:
            return None
    if "monotonic" not in out or "boottime" not in out:
        return None
    return out


def read_pod_clock(
    ctx: str,
    namespace: str,
    deploy: str = "oai-ue-uav",
    container: str = "nr-ue",
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    cmd = kubectl_read_pod_clock_cmd(ctx, namespace, deploy, container)
    out: dict[str, Any] = {
        "deploy": deploy,
        "container": container,
        "cmd_str": " ".join(cmd),
        "ns": None,
        "ns_for_children": None,
        "timens_offsets_raw": None,
        "mono_ns": None,
        "host_t1_ns": None,
        "host_t2_ns": None,
        "ok": False,
        "error": None,
        "dry_run": bool(dry_run),
    }
    if dry_run:
        out["error"] = "dry-run: command listed; pod clock not read"
        return out
    t1 = time.monotonic_ns()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["error"] = str(exc)
        return out
    t2 = time.monotonic_ns()
    out["host_t1_ns"] = t1
    out["host_t2_ns"] = t2
    if r.returncode != 0:
        out["error"] = (r.stderr or r.stdout or f"rc={r.returncode}").strip()[:400]
        return out
    lines = (r.stdout or "").strip().splitlines()
    try:
        data = json.loads(lines[-1])
        out["ns"] = str(data["ns"])
        out["ns_for_children"] = str(data["ns_for_children"])
        out["timens_offsets_raw"] = str(data["timens_offsets"])
        out["mono_ns"] = int(data["mono_ns"])
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        out["error"] = f"unexpected pod response ({exc}): {(r.stdout or '')[:300]!r}"
        return out
    out["ok"] = True
    return out


def evaluate_host_uav_clock(
    host_ns: str,
    pod_ns: Optional[str],
    pod_ns_for_children: Optional[str],
    timens_offsets_raw: Optional[str],
    pod_mono_ns: Optional[int],
    host_t1_ns: Optional[int],
    host_t2_ns: Optional[int],
) -> dict[str, Any]:
    """Pure: decides whether the host and the UAV pod share CLOCK_MONOTONIC."""
    offsets = parse_timens_offsets(timens_offsets_raw)
    inode_match = pod_ns is not None and host_ns == pod_ns
    pod_ns_is_for_children = pod_ns is not None and pod_ns == pod_ns_for_children
    offsets_zero = (
        offsets is not None
        and offsets["monotonic"] == [0, 0]
        and offsets["boottime"] == [0, 0]
    )
    have_bracket = None not in (pod_mono_ns, host_t1_ns, host_t2_ns)
    in_window = bool(have_bracket and host_t1_ns <= pod_mono_ns <= host_t2_ns)
    window_ms = (
        round((host_t2_ns - host_t1_ns) / 1e6, 1)
        if host_t1_ns is not None and host_t2_ns is not None else None
    )

    if inode_match:
        ok, basis, error = True, "inode_match", None
    else:
        failed = []
        if not pod_ns_is_for_children:
            failed.append(
                f"pod ns/time ({pod_ns!r}) != ns/time_for_children "
                f"({pod_ns_for_children!r}); timens_offsets does not describe the ns in use"
            )
        if not offsets_zero:
            failed.append(f"non-zero or unreadable offsets ({timens_offsets_raw!r})")
        if not in_window:
            failed.append(
                f"pod monotonic ({pod_mono_ns}) outside the host [t1, t2] "
                f"([{host_t1_ns}, {host_t2_ns}])"
            )
        ok = not failed
        basis = "zero_offsets_and_bracket" if ok else None
        error = None if ok else (
            f"host ns/time={host_ns!r} uav ns/time={pod_ns!r}; "
            + "; ".join(failed)
            + "; K7/T0_mono_ns is not comparable, the run must fail"
        )

    return {
        "schema": SCHEMA,
        "host_ns": host_ns,
        "uav_ns": pod_ns,
        "uav_ns_for_children": pod_ns_for_children,
        "inode_match": bool(inode_match),
        "uav_ns_is_for_children": bool(pod_ns_is_for_children),
        "timens_offsets": offsets,
        "offsets_zero": bool(offsets_zero),
        "host_t1_ns": host_t1_ns,
        "uav_mono_ns": pod_mono_ns,
        "host_t2_ns": host_t2_ns,
        "bracket_window_ms": window_ms,
        "uav_mono_in_host_window": in_window,
        "basis": basis,
        "ok": bool(ok),
        "error": error,
    }


def check_host_uav_time_namespace(
    ctx: str,
    namespace: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    host_ns = read_host_time_ns()
    pod = read_pod_clock(ctx, namespace, dry_run=dry_run)
    if dry_run:
        return {
            "schema": SCHEMA,
            "host_ns": host_ns,
            "uav": pod,
            "ok": False,
            "dry_run": True,
            "error": "dry-run: commands listed; comparison not done",
            "commands": [pod.get("cmd_str")],
        }
    if not pod.get("ok"):
        return {
            "schema": SCHEMA,
            "host_ns": host_ns,
            "uav": pod,
            "ok": False,
            "error": f"failed to read the UAV clock/ns: {pod.get('error')}",
        }
    res = evaluate_host_uav_clock(
        host_ns,
        pod["ns"],
        pod["ns_for_children"],
        pod["timens_offsets_raw"],
        pod["mono_ns"],
        pod["host_t1_ns"],
        pod["host_t2_ns"],
    )
    res["uav"] = {k: pod[k] for k in ("deploy", "container", "cmd_str")}
    return res


ZERO_OFFSETS = "monotonic           0         0\nboottime            0         0\n"


def bench_cases() -> list[dict[str, Any]]:
    """Synthetic cases without a cluster: (name, expected, result)."""
    host = "time:[4026531834]"
    pod = "time:[4026532902]"
    t1, t2 = 1_000_000_000, 1_127_800_000
    cases = [
        ("inode_equal", True,
         evaluate_host_uav_clock(host, host, host, ZERO_OFFSETS, 1_050_000_000, t1, t2)),
        ("inode_differs_offset_zero_within_window", True,
         evaluate_host_uav_clock(host, pod, pod, ZERO_OFFSETS, 1_050_000_000, t1, t2)),
        ("offset_nonzero", False,
         evaluate_host_uav_clock(host, pod, pod,
                                 "monotonic      172800         0\nboottime            0         0\n",
                                 1_050_000_000, t1, t2)),
        ("reading_outside_window", False,
         evaluate_host_uav_clock(host, pod, pod, ZERO_OFFSETS, 2_000_000_000, t1, t2)),
        ("ns_differs_from_time_for_children", False,
         evaluate_host_uav_clock(host, pod, "time:[4026532999]", ZERO_OFFSETS,
                                 1_050_000_000, t1, t2)),
    ]
    return [
        {"case": name, "expected_ok": exp, "got_ok": res["ok"],
         "pass": res["ok"] is exp, "error": res["error"]}
        for name, exp, res in cases
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description="Host vs UAV shared CLOCK_MONOTONIC")
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--bench-compare", action="store_true",
                    help="Synthetic cases without a cluster")
    args = ap.parse_args()
    if args.bench_compare:
        rows = bench_cases()
        print(json.dumps(rows, indent=2))
        n_pass = sum(1 for r in rows if r["pass"])
        print(f"bench: {n_pass}/{len(rows)} cases as expected")
        if n_pass != len(rows):
            raise SystemExit(1)
        return
    res = check_host_uav_time_namespace(args.context, args.namespace, dry_run=args.dry_run)
    print(json.dumps(res, indent=2))
    if not args.dry_run and not res.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

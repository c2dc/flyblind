#!/usr/bin/env python3                                                                                                           #
##################################################################################################################################
# Module: orchestrate.py (host experiment orchestrator: one run, four time-aligned measurement layers)                           #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Run driver for one experiment against an ALREADY-RUNNING testbed (it does not provision the cluster). Starts and  #
#   time-aligns the four measurement layers against a single run T0 -- NETWORK (one-way delay probe), MECHANISM (per-slice PRB   #
#   from the gNB MAC dumps), C2 (the MAVLink mission and the setpoint stream) and PHYSICS (trajectory deviation, failsafe) --    #
#   then scores the run against the pre-registered validity gates and writes a self-describing result directory. The parameters  #
#   that every campaign run shares are baked in as defaults (106 PRB, cross topology, 600 s, route trajectory at 4 m/s,          #
#   12 Mb/s AVIATOR-shaped companion video, FULL sidecars), so a condition states only what varies.                              #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   python3 orchestrate.py --condition baseline --open-qgc                                                                       #
#   python3 orchestrate.py --condition attack --attack multi-rogue --attack-start-after-s 45 --open-qgc                          #
#   python3 orchestrate.py --condition attack --attack multi-rogue --attack-start-after-s 45 --open-qgc-mitigation               #
#   See --help for the full matrix of flags; 04_campaign/campaign.sh drives the whole campaign.                                  #
##################################################################################################################################

"""FlyBlind experiment orchestrator — 4-layer synchronized capture (external).

Coordinates the four measurement layers of the IEEE S&P 2027 FlyBlind testbed against
an already-running Kind/OAI 5G cluster (does NOT provision it):

  NETWORK   OWD/IPDV probe (probes/owd_probe.py) — the temporal envelope.
  MECHANISM per-slice PRB proxy (probes/mac_prb.py) — gNB MAC dumps.
  C2        MAVLink mission (mission_runner.py in the GCS pod) — commands.csv.
  PHYSICS   trajectory deviation / failsafe (mission_summary physics_metrics).

Every layer is stamped with a host CLOCK_MONOTONIC offset relative to a single
run T0. Because GCS/UAV share one time namespace on a single Kind host (verified
by the probe's clock_sanity), the in-pod ``t_mono_ns`` in commands.csv and the
probe's tx/rx ns live on the same clock — so the layers are alignable.

First deliverable: an impeccable BASELINE (no attack) as the experimental
control, with the same measurement structure as the attack runs.

Usage (baseline, 180s, 3 repeats; defaults: cross, 106 PRB, setpoint-stream):
  python3 orchestrate.py --condition baseline --duration 180 --repeat 3

See --help for the full matrix flags.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
PROBES_DIR = SCRIPT_DIR / "probes"
SITL_LIB = SCRIPT_DIR.parent / "03_sitl" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SITL_LIB) not in sys.path:
    sys.path.insert(0, str(SITL_LIB))
INSTRUMENTATION_DIR = SCRIPT_DIR / "instrumentation"
if str(INSTRUMENTATION_DIR) not in sys.path:
    sys.path.insert(0, str(INSTRUMENTATION_DIR))

from paper_artifacts import (  # noqa: E402
    build_paper_artifacts_block,
    persist_run_paper_artifacts,
    warn_paper_capture_flags,
    write_analysis_params,
    build_analysis_params,
)
from results_layout import (  # noqa: E402
    allocate_unique_filename,
    default_results_root,
    resolve_campaign_dir,
    resolve_run_dirname,
    testbed_rogue_capacity,
)
from instrumentation.harness_hooks import (  # noqa: E402
    normalize_sidecar_profile,
    sidecar_duration_s,
    start_sidecars,
    stop_sidecars,
    schedule_video_stop_at_attack_end,
    write_post_run_harness,
)
from instrumentation.drain_policy import default_policy as drain_default_policy  # noqa: E402
from instrumentation.schedule_optc import (  # noqa: E402
    optc_applies as _optc_applies_for,
    plan_schedule as optc_plan_schedule,
)
from instrumentation.collect_image_digests import (  # noqa: E402
    collect as collect_image_digests,
    write_image_digests,
)
from instrumentation.host_uav_time_ns import check_host_uav_time_namespace  # noqa: E402
from terminal_report import (  # noqa: E402
    print_aggregate,
    print_campaign_banner,
    print_campaign_result,
    print_run_header,
    print_run_report as print_terminal_run_report,
    start_campaign_transcript,
)
# Python modules pushed to UE pods each run (host repo wins over stale ConfigMap symlinks in /tmp).
C2_RUN_TOOLS = "/tmp/tm-run"
C2_SYNC_PY = (
    "mission_runner.py",
    "setpoint_stream.py",
    "trajectory.py",
    "freshness_metrics.py",
    "mavlink_proxy.py",
    "co_tenant_load.py",
    "sitl_warmup.py",
    "co_tenant_burst_sender.py",
    "aviator_video_send.py",
)
# Staged to ROGUE /tmp (ConfigMap may be stale until next start_testbed_mitigation).
# rogue_pulse.py is optional (pulse-* only); missing it must not warn on
# multi-rogue / sync-prb / microburst (those use co_tenant_load / c2_sync_flood).
ROGUE_ATTACK_PY_REQUIRED = (
    "c2_sync_flood.py",
    "udp_flood.py",
)
ROGUE_ATTACK_PY_OPTIONAL = (
    "rogue_pulse.py",
)
OWD_PROBE = PROBES_DIR / "owd_probe.py"
C2_ARRIVAL_FORWARDER = PROBES_DIR / "c2_arrival_forwarder.py"
MAC_PRB = PROBES_DIR / "mac_prb.py"
HOST_TELEMETRY = PROBES_DIR / "host_telemetry.py"
UPF_TELEMETRY = PROBES_DIR / "upf_telemetry.py"
SITL_BACKEND_PORT = 5761  # ArduPilot localhost TCP backend (role B); NOT c2-proxy
CURRENT_RUN: dict[str, Any] = {}
CORE_NAMESPACE_DEFAULT = "core"
UPF_CPU_SATURATE_PCT_DEFAULT = 85.0

# duration_s -> (mission_len_s, n_nav_commands). Margin = (duration - mission_len)/2.
MISSION_BY_DURATION = {180: (120, 25), 600: (420, 72)}
MISSION_OVERHEAD_S = 30.0   # arm + takeoff + 8s climb + land + 10s settle (excluded from steady)
# Grace after mission start before UL contention is allowed: arm + takeoff + climb.
ATTACK_TAKEOFF_GUARD_S = 30.0
MISSION_JITTER_S = 1.5      # +/- jitter around uniform mean spacing (decorrelate from ~60ms pulse)

# Waypoint deltas at SITL home lat ~-35°. 1° lat ≈ 111 km; 1° lon ≈ 91 km (cos 35°).
# 120s: 100 m square (perimeter ~400 m @ 5 m/s ≈ 80 s in 90 s useful window).
# 420s: two 200 m rectangles NE + SW (perimeter ~1600 m in ~380 s useful window).
D_LAT_100 = 0.0009    # ~100 m N/S
D_LON_100 = 0.0011    # ~100 m E/W
D_LAT_200 = 0.0018    # ~200 m N/S
D_LON_200 = 0.0022    # ~200 m E/W
WAYPOINTS_SHORT = [
    {"lat": 0.0, "lon": 0.0},
    {"lat": 0.0, "lon": D_LON_100},
    {"lat": D_LAT_100, "lon": D_LON_100},
    {"lat": D_LAT_100, "lon": 0.0},
    {"lat": 0.0, "lon": 0.0},
]
WAYPOINTS_LONG = [
    {"lat": 0.0, "lon": 0.0},
    {"lat": 0.0, "lon": D_LON_200},
    {"lat": D_LAT_200, "lon": D_LON_200},
    {"lat": D_LAT_200, "lon": 0.0},
    {"lat": 0.0, "lon": 0.0},
    {"lat": 0.0, "lon": -D_LON_200},
    {"lat": -D_LAT_200, "lon": -D_LON_200},
    {"lat": -D_LAT_200, "lon": 0.0},
    {"lat": 0.0, "lon": 0.0},
]

ACCEPT_RADIUS_M = 1.5

# Flyaway gate: peak distance to commanded waypoint. Peaks near leg length in steady
# transit — NOT spatial damage. Baseline spatial quality: cross_track_error_m p99.
DEVIATION_MAX_M = 300.0
CROSS_TRACK_BASELINE_MAX_P99_M = 50.0
# Baseline quality gate: post-warmup UDP availability must be >= this (%).
AVAILABILITY_MIN_PCT = 99.9
SITL_PORT = 5760
TESTBED_RUNTIME = Path(
    os.environ.get(
        "TM_TESTBED_RUNTIME",
        os.environ.get("TM1_TESTBED_RUNTIME", str(SCRIPT_DIR.parent / "01_testbeds" / "runtime-mitigation")),  # TM1_* = legacy env alias
    )
)
TOPOLOGY_STAMP = TESTBED_RUNTIME / "topology.env"
FRESHNESS_MATCH_MIN_RATIO = 0.90  # legacy alias
CORRELATION_MIN_RATIO = 0.999
DELIVERY_MIN_RATIO = 0.99
CAPTURE_MIN_RATIO = 0.99
CO_TENANT_PORT = 5202
EXT_DN_IPERF_PORT_ATTACK = 5201  # ROGUE CBR / PRB contention (GCS co-tenant uses CO_TENANT_PORT)
EXT_DN_IPERF_PORTS = (EXT_DN_IPERF_PORT_ATTACK, CO_TENANT_PORT)
# Cross L3 pools (must match start_testbed.sh). Pure-contention attacks must not target B.
SUBNET_SLICE_A_CIDR = "12.1.1.128/25"   # ROGUE / DNN oai
SUBNET_SLICE_B_CIDR = "12.1.1.64/26"    # GCS+UAV C2 / DNN oai.urllc
# Path/endpoint attacks (target UAV/GCS :5760) — outside the pure-contention thesis after L3.
PATH_ENDPOINT_ATTACKS = frozenset({"sync-c2", "pulse-c2", "flood-c2", "combined-c2"})
MULTI_ROGUE_ATTACKS = frozenset({
    "multi-rogue", "multi-rogue-dl", "multi-rogue-sync", "multi-rogue-dl-sync",
})
SYNC_STYLE_ATTACKS = frozenset({
    "sync-c2", "sync-prb", "multi-rogue-sync", "multi-rogue-dl-sync",
})
PURE_CONTENTION_ATTACKS = frozenset({
    "multi-rogue", "multi-rogue-dl", "multi-rogue-sync", "multi-rogue-dl-sync",
    "sync-prb",
})
DL_FAMILY_ATTACKS = frozenset({"multi-rogue-dl", "multi-rogue-dl-sync"})
IPERF_CLIENT_FAIL_PATTERNS = (
    "control socket has closed",
    "unable to send control message",
    "server is busy",
    "connection refused",
)
PROXY_DELAY_P99_MAX_MS = 5.0
PROXY_DRAIN_S = 2.0
PROXY_SHUTDOWN_TIMEOUT_S = 15.0
PRB_CAPTURE_GRACE_S = 15.0
PRB_CAPTURE_MIN_SPAN_RATIO = 0.90
# Companion video seen on the role the parser called UAV/GCS, as a fraction of
# the offered rate — detects RNTI→role misattribution in the PRB evidence.
PRB_ROLE_IDENTITY_MIN_RATIO = 0.20
PROXY_SIGTERM_GRACE_S = 5.0
PROXY_SUMMARY_REQUIRED_FIELDS = frozenset({
    "forward_frames",
    "reverse_frames",
    "parse_errors",
    "reconnects",
    "frames_discarded",
    "proxy_delay_ms",
})
SETPOINT_RATE_TOL = 0.10
COOLDOWN_S = 15
COOLDOWN_STABLE_S = 15
COOLDOWN_LOADAVG_MAX = 10.0
COOLDOWN_MAX_WAIT_S = 120
COOLDOWN_ATTACK_LONG_MAX_WAIT_S = 180  # attack duration_s >= this uses longer inter-run wait
ATTACK_LONG_DURATION_S = 180
SITL_STRESS_HEARTBEAT_GAP_S = 60.0  # mark SITL dirty after attack if HB gap exceeds this
# Formal host gate: default loadavg1 max = max(nproc − 2, floor). On 16 cores → 14
# (same as the old fixed default). Override with --host-loadavg-max.
HOST_LOADAVG_MAX_FLOOR = 2.0
HOST_CPU_BUSY_MAX_PCT = 90.0
SITL_DIRTY_FLAG = "/tmp/tm_sitl_dirty"
EXT_DN_BURST_SENDER = SITL_LIB / "co_tenant_burst_sender.py"
EXT_DN_CO_TENANT_SINK = SITL_LIB / "co_tenant_sink.py"
# --open-qgc (operator view only): one-way MAVLink mirror + QGroundControl on the host.
# Read-only — QGC displays telemetry but has no path back to the vehicle; never affects a run.
QGC_MIRROR_SCRIPT = SITL_LIB / "qgc_mavlink_mirror.py"
# Host-side shim that answers QGC's param/fence/rally requests so the read-only
# mirror does not trigger QGC's "did not respond" dialogs. When present, the
# mirror targets the shim (QGC_SHIM_POD_PORT) which forwards to QGC; otherwise
# the mirror targets QGC directly (old behaviour, with the warnings).
QGC_RESPONDER_SCRIPT = SITL_LIB / "qgc_responder.py"
QGC_SHIM_POD_PORT = 14560
# Bundled with the artifact so it survives reboots and needs no download (/tmp is
# wiped on reboot). Override with QGC_APPIMAGE=/path. Missing file → graceful skip.
QGC_APPIMAGE = os.environ.get(
    "QGC_APPIMAGE", str(SCRIPT_DIR.parent / "03_sitl" / "bin" / "QGroundControl.AppImage")
)
# Custom QGC build carrying the FlyBlind "STALE STATE" C2-freshness detector (second
# contribution). Opened by --open-qgc-mitigation; identical mirror/responder plumbing,
# only the binary differs. Missing file → graceful skip (same as stock).
QGC_APPIMAGE_MITIGATION = os.environ.get(
    "QGC_APPIMAGE_MITIGATION",
    str(SCRIPT_DIR.parent / "03_sitl" / "bin" / "QGroundControl-mitigation.AppImage"),
)
QGC_HOST = os.environ.get("QGC_HOST", "172.18.0.1")
QGC_PORT = 14550
ATTACK_RATE_TOL = 0.05
ATTACK_DURATION_MIN_RATIO = 0.95
ATTACK_RECEIVER_ACTIVE_MIN_RATIO = 0.90
ATTACK_RECEIVER_GAP_MAX_S = 5.0
ATTACK_RECEIVER_MIN_BYTES = 1_000_000
ATTACK_RECEIVER_THIRD_MIN_BYTES = 10_000
ATTACK_DELIVERY_MIN_RATIO_NON_SAT = 0.95
ATTACK_RAN_ENGAGEMENT_MIN_SAMPLES = 10
ATTACK_RAN_ENGAGEMENT_MIN_WINDOW_RATIO = 0.90
MIN_RATIO_ROLLOUT_TIMEOUT_S = 180
HOST_TELEMETRY_MIN_COVERAGE_RATIO = 0.90
CBR_PROFILE_SATURATING = "saturating"
CBR_PROFILE_NON_SATURATING = "non_saturating"
C2_MIN_POSITION_SAMPLES = 10
C2_MIN_SETPOINT_TRACK_SAMPLES = 10
DEFAULT_BURST_SIZE_BYTES = 100000  # 20 Mbit/s @ 40 ms (burst_bytes*8/period)

# PRB strict gate: what the code checks vs audit checklist (formal campaign).
PRB_GATE_AUDIT = {
    "run_scoped_artifacts": "yes — mac-prb-raw.log + capture_meta.run_id per run",
    "experimental_window_overlap": "yes — span >= 90% envelope when --require-prb",
    "parser_no_error": "partial — dump_samples>0 and per_slice populated",
    "rnti_role_map": "yes — rogue/gcs/uav with RNTI when --require-prb",
    "rnti_slice_map": "partial — per_slice shares; reservation via preflight",
    "topology_reservation_match": "preflight + stamp on run_meta",
}
PRB_PILOT_COMPLETED = {
    "intra_B0_gnb106_180s_N1": True,
}

GATE_LABELS = {
    "clock": "clock",
    "host": "host",
    "host_instrumentation_integrity": "host_instr",
    "mission_integrity": "mission_int",
    "mission_success": "mission_base",
    "prb": "prb",
    "proxy": "proxy",
    "freshness": "fresh",
    "stream": "stream",
    "c2_health": "c2_health",
    "availability": "disp",
    "drone_integrity": "drone_int",
    "baseline_drone_quality": "drone_base",
    "attack_integrity": "attack_int",
    "mitigation_integrity": "mitigation_int",
}

GATE_PRINT_ORDER = [
    "clock", "host", "host_instrumentation_integrity", "mission_integrity", "mission_success",
    "prb", "proxy", "freshness", "stream", "c2_health", "availability",
    "drone_integrity", "baseline_drone_quality", "attack_integrity", "mitigation_integrity",
]


# --------------------------------------------------------------------------- #
# kubectl helpers
# --------------------------------------------------------------------------- #
def kctl(ctx, *args, timeout=None, check_input=None):
    cmd = ["kubectl", "--context", ctx, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=check_input)


def pod_running(ctx, ns, deploy):
    r = kctl(ctx, "get", "deploy", "-n", ns, deploy,
             "-o", "jsonpath={.status.availableReplicas}")
    return r.returncode == 0 and (r.stdout or "").strip() not in ("", "0")


def pdu_ip_dev(ctx, ns, deploy, dev: str = "oaitun_ue1"):
    r = kctl(ctx, "exec", "-n", ns, f"deploy/{deploy}", "-c", "nr-ue", "--",
             "ip", "-4", "-o", "addr", "show", "dev", dev)
    if r.returncode != 0:
        return None
    for tok in (r.stdout or "").split():
        if "/" in tok and tok.count(".") == 3:
            return tok.split("/")[0]
    return None


def pdu_ip(ctx, ns, deploy):
    return pdu_ip_dev(ctx, ns, deploy, "oaitun_ue1")


def node_of(ctx, ns, app):
    r = kctl(ctx, "get", "pod", "-n", ns, "-l", f"app={app}",
             "-o", "jsonpath={.items[0].spec.nodeName}")
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def pod_cni_ip(ctx, ns, deploy):
    """Pod IP on the CNI network (eth0). Used by sync-c2: mission_runner (GCS) sends the
    trigger to the ROGUE here, off the 5G path, without polluting the C2 measurement."""
    r = kctl(ctx, "get", "pod", "-n", ns, "-l", f"app={deploy}",
             "-o", "jsonpath={.items[0].status.podIP}")
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def ext_dn_target(ctx, ns, path: str) -> tuple[str, str]:
    """Return (target_host, bind_host) for co-tenant load: pdu or cni-bypass."""
    if path == "cni-bypass":
        ext_cni = pod_cni_ip(ctx, "core", "oai-ext-dn")
        rogue_cni = pod_cni_ip(ctx, ns, "oai-ue-rogue")
        if not ext_cni:
            raise SystemExit("cni-bypass: cannot resolve oai-ext-dn pod CNI IP")
        if not rogue_cni:
            raise SystemExit("cni-bypass: cannot resolve ROGUE pod CNI IP")
        return ext_cni, rogue_cni
    return '"$EXT"', '"$ROGUE"'


def read_topology_stamp() -> dict[str, str]:
    out: dict[str, str] = {}
    if not TOPOLOGY_STAMP.is_file():
        return out
    for line in TOPOLOGY_STAMP.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_topology_stamp_min_ratio(min_a: int, min_b: int) -> None:
    """Update MIN_RATIO_SLICE_* lines in topology.env after live policy apply."""
    if not TOPOLOGY_STAMP.is_file():
        return
    out_lines: list[str] = []
    for line in TOPOLOGY_STAMP.read_text(encoding="utf-8").splitlines():
        if line.startswith("MIN_RATIO_SLICE_A="):
            out_lines.append(f"MIN_RATIO_SLICE_A={min_a}")
        elif line.startswith("MIN_RATIO_SLICE_B="):
            out_lines.append(f"MIN_RATIO_SLICE_B={min_b}")
        else:
            out_lines.append(line)
    TOPOLOGY_STAMP.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def desired_min_ratio_values(args) -> tuple[int, int]:
    """Return (MIN_RATIO_SLICE_A, MIN_RATIO_SLICE_B) implied by args + topology."""
    if args.topology == "intra":
        return int(args.reservation), 0
    min_a = int(getattr(args, "min_ratio_slice_a", 0) or 0)
    # Preserve cluster A* if stamp sync was skipped (never wipe A60 → A0 by accident).
    if min_a == 0:
        stamp = read_topology_stamp()
        min_a = int(stamp.get("MIN_RATIO_SLICE_A", "0") or 0)
        if min_a > 0:
            args.min_ratio_slice_a = min_a
    return min_a, int(args.reservation)


def rollout_restart_deploy(ctx: str, ns: str, deploy: str, timeout_s: int = MIN_RATIO_ROLLOUT_TIMEOUT_S) -> tuple[bool, str]:
    r = kctl(ctx, "rollout", "restart", "-n", ns, f"deploy/{deploy}", timeout=60)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or f"rollout restart {deploy} failed").strip()
    w = kctl(
        ctx, "rollout", "status", "-n", ns, f"deploy/{deploy}",
        f"--timeout={timeout_s}s", timeout=timeout_s + 15,
    )
    if w.returncode != 0:
        return False, (w.stderr or w.stdout or f"rollout status {deploy} failed").strip()
    return True, "ok"


def wait_all_ues_pdu_ready(
    ctx: str,
    ns: str,
    *,
    need_rogue: bool,
    timeout_s: float = 400.0,
) -> tuple[bool, str]:
    deploys = ["oai-ue-gcs", "oai-ue-uav"] + (["oai-ue-rogue"] if need_rogue else [])
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        ok = True
        for d in deploys:
            ip = pdu_ip(ctx, ns, d)
            if not ip or not ip.startswith("12.1.1."):
                ok = False
                break
        if ok:
            gcs_ip = pdu_ip(ctx, ns, "oai-ue-gcs") or ""
            uav_ip = pdu_ip(ctx, ns, "oai-ue-uav") or ""
            if wait_pdu_reachable(ctx, ns, gcs_ip, uav_ip, timeout_s=30.0):
                return True, f"ues_ready attempt={attempt}"
        if attempt % 6 == 0:
            print(f"[min-ratio] waiting for UE PDU attach… attempt={attempt}", flush=True)
        time.sleep(5)
    return False, "UE PDU attach timeout after min-ratio rollout"


def verify_gnb_min_ratio(ctx: str, ns: str, min_a: int, min_b: int, topology: str) -> tuple[bool, str]:
    r = kctl(ctx, "exec", "-n", ns, "deploy/oai-gnb", "-c", "gnb", "--",
             "cat", "/opt/oai-gnb/etc/rrmPolicy.json")
    if r.returncode != 0:
        return False, "rrmPolicy.json unreadable"
    policy = r.stdout or ""
    if topology == "intra":
        pat = rf'"min_ratio"\s*:\s*{min_a}\b'
        if not re.search(pat, policy):
            return False, f"intra min_ratio={min_a} not found in rrmPolicy.json"
        return True, "ok"
    if not re.search(rf'"min_ratio"\s*:\s*{min_a}\b', policy):
        return False, f"slice-A min_ratio={min_a} not found in rrmPolicy.json"
    if not re.search(rf'"min_ratio"\s*:\s*{min_b}\b', policy):
        return False, f"slice-B min_ratio={min_b} not found in rrmPolicy.json"
    return True, "ok"


def apply_rrm_min_ratio_if_needed(ctx: str, ns: str, args) -> tuple[bool, str | None]:
    """Patch gNB MIN_RATIO env + rollout when CLI reservation differs from cluster stamp."""
    min_a, min_b = desired_min_ratio_values(args)
    stamp = read_topology_stamp()
    saved_a = int(stamp.get("MIN_RATIO_SLICE_A", "0") or 0)
    saved_b = int(stamp.get("MIN_RATIO_SLICE_B", "0") or 0)
    if args.topology == "intra":
        if saved_a == min_a and saved_b == 0:
            return True, None
    elif saved_a == min_a and saved_b == min_b:
        return True, None

    print(
        f"[min-ratio] applying rRMPolicyMinRatio A={min_a} B={min_b} "
        f"(stamp was A={saved_a} B={saved_b})…",
        flush=True,
    )
    r = kctl(
        ctx, "set", "env", "-n", ns, "deployment/oai-gnb", "-c", "gnb",
        f"MIN_RATIO_SLICE_A={min_a}", f"MIN_RATIO_SLICE_B={min_b}",
        timeout=60,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "kubectl set env failed").strip()

    ok, msg = rollout_restart_deploy(ctx, ns, "oai-gnb")
    if not ok:
        return False, msg

    need_rogue = args.condition != "baseline"
    for deploy in (["oai-ue-gcs", "oai-ue-uav"] + (["oai-ue-rogue"] if need_rogue else [])):
        ok, msg = rollout_restart_deploy(ctx, ns, deploy)
        if not ok:
            return False, f"{deploy}: {msg}"

    ok, msg = wait_all_ues_pdu_ready(ctx, ns, need_rogue=need_rogue)
    if not ok:
        return False, msg

    time.sleep(15)
    ok, msg = verify_gnb_min_ratio(ctx, ns, min_a, min_b, args.topology)
    if not ok:
        return False, msg

    write_topology_stamp_min_ratio(min_a, min_b)
    print(f"[min-ratio] OK — cluster now MinRatio A={min_a} B={min_b}", flush=True)
    return True, f"applied A={min_a} B={min_b}"


def stamp_c2_transport() -> str:
    # Legacy stamps may still carry C2_TRANSPORT; C2 over 5G is always UDP.
    t = (read_topology_stamp().get("C2_TRANSPORT") or "udp").lower()
    if t == "tcp":
        raise SystemExit(
            "Legacy stamp has TCP C2 — redeploy testbed "
            "(./stop_testbed.sh && ./start_testbed.sh)."
        )
    if t != "udp":
        raise SystemExit(
            f"Legacy stamp C2_TRANSPORT={t} unsupported — redeploy testbed "
            "(./stop_testbed.sh && ./start_testbed.sh)."
        )
    return "udp"


def gcs_c2_port(c2_proxy: bool = False) -> int:
    return SITL_PORT


def build_gcs_connection(uav_ip: str, c2_transport: str = "udp", c2_proxy: bool = False) -> str:
    if (c2_transport or "udp").lower() == "tcp" or c2_proxy:
        raise SystemExit(
            "TCP C2 / --c2-proxy removed — use UDP over 5G (udp:UAV:5760)."
        )
    return f"udp:{uav_ip}:{SITL_PORT}"


def sitl_listen_port(c2_proxy: bool = False) -> int:
    # Public UDP port facing 5G (socat listens here).
    return SITL_PORT


def build_sitl_local_url(c2_proxy: bool = False, c2_transport: str = "udp") -> str:
    # Warm-up / direct SITL attach: localhost TCP backend behind UDP relay.
    return f"tcp:127.0.0.1:{SITL_BACKEND_PORT}"


def sitl_ss_proto(c2_transport: str = "udp") -> str:
    return "uln"


def detect_cluster_topology(ctx, ns) -> str:
    r = kctl(ctx, "exec", "-n", ns, "deploy/oai-gnb", "-c", "gnb", "--",
             "grep", "-cE", "^[[:space:]]+- sst:", "/opt/oai-gnb/etc/gnb.yaml")
    if r.returncode != 0:
        return ""
    try:
        count = int((r.stdout or "0").strip())
    except ValueError:
        return ""
    return {1: "intra", 2: "cross"}.get(count, "")


def detect_cluster_gnb_prb(ctx, ns) -> int | None:
    r = kctl(ctx, "exec", "-n", ns, "deploy/oai-gnb", "-c", "gnb", "--",
             "grep", "-m1", "dl_carrierBandwidth", "/opt/oai-gnb/etc/gnb.yaml")
    if r.returncode != 0:
        return None
    line = (r.stdout or "").strip()
    for tok in line.replace(":", " ").split():
        if tok.isdigit():
            return int(tok)
    return None


def attack_core_pin(ctx, ns) -> str:
    r = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-rogue", "-c", "nr-ue", "--",
             "sh", "-c", 'echo "${ATTACK_CORE_PIN:-}"')
    return (r.stdout or "").strip()


def attack_taskset(ctx, ns) -> str:
    pin = attack_core_pin(ctx, ns)
    if not pin:
        stamp = read_topology_stamp()
        # fallback when pod env not yet redeployed — match start_testbed defaults
        pin = stamp.get("CORES_ATTACK", "") if stamp.get("PIN_CORES") == "1" else ""
    return f"taskset -c {pin} " if pin else ""


def implied_burst_mbps(burst_bytes: int, period_ms: int) -> float:
    if period_ms <= 0:
        return 0.0
    return (burst_bytes * 8.0) / (period_ms / 1000.0) / 1e6


def validate_co_tenant_params(args) -> None:
    if args.attack not in ("microburst", "dual-pulse", "multi-rogue-dl"):
        return
    impl = implied_burst_mbps(args.burst_size_bytes, args.period_ms)
    tol = 0.05
    if args.mean_rate_mbps > 0 and abs(impl - args.mean_rate_mbps) / args.mean_rate_mbps > tol:
        raise SystemExit(
            f"burst/period inconsistent with --mean-rate-mbps: "
            f"implied={impl:.2f} Mbit/s requested={args.mean_rate_mbps} "
            f"(burst={args.burst_size_bytes}B period={args.period_ms}ms). "
            f"For 20 Mbit/s @ 40 ms use --burst-size-bytes 100000"
        )


def _rate_slug(hz: float) -> str:
    return f"sp{str(hz).replace('.', 'p')}hz"


def fs_name_max(parent: Path) -> int:
    """Per-directory NAME_MAX (this host's runs/ is 143, not the usual 255)."""
    try:
        return int(os.pathconf(parent, "PC_NAME_MAX"))
    except (OSError, ValueError, AttributeError):
        return 255


def clamp_run_dirname(name: str, parent: Path) -> str:
    """Truncate run dir basename to filesystem NAME_MAX (keep unique hash suffix)."""
    limit = fs_name_max(parent)
    if len(name) <= limit:
        return name
    import hashlib
    h = hashlib.sha1(name.encode()).hexdigest()[:8]
    keep = max(16, limit - 9)  # "_" + 8 hex
    clipped = f"{name[:keep]}_{h}"
    if len(clipped) > limit:
        clipped = clipped[:limit]
    print(
        f"[warn] run dirname truncated to NAME_MAX={limit}: {len(name)} → {len(clipped)}",
        flush=True,
    )
    return clipped


def build_run_slug(args) -> str:
    parts = [
        args.condition,
        args.topology,
        f"prb{args.gnb_prb}",
        f"B{args.reservation}",
        args.workload,
    ]
    if args.workload == "setpoint-stream":
        parts += [
            f"traj-{args.trajectory}",
            _rate_slug(args.setpoint_rate_hz),
            f"seed{args.setpoint_seed}",
        ]
        if args.c2_proxy:
            parts.append("proxy")
        elif getattr(args, "c2_transport", "udp") == "udp":
            parts.append("udp")
        else:
            parts.append("tcpdirect")
    if args.condition != "baseline":
        parts.append(args.attack)
        if args.attack in ("microburst", "dual-pulse"):
            parts += [
                args.attack_path,
                f"{int(args.mean_rate_mbps)}mbps",
                f"T{args.period_ms}",
                f"B{args.burst_size_bytes}",
                f"aseed{args.attack_seed}",
            ]
        if args.attack == "dual-pulse":
            parts.append(f"ph{args.dual_phase_ms}")
        elif args.attack == "cbr":
            parts += [
                f"dir-{args.cbr_dir}",
                f"{args.cbr_mbps}mbps-per-stream",
                f"P{args.cbr_parallel}",
            ]
        elif args.attack in ("pulse", "pulse-c2"):
            bolus, period, pkt, flows = pulse_params(args)
            parts += [
                f"pulse-{args.pulse_profile}",
                f"B{bolus}KB",
                f"T{period}ms",
                f"pkt{pkt}",
                f"F{flows}",
            ]
        elif args.attack == "flood-c2":
            fp = flood_params(args)
            # Keep short: runs/ NAME_MAX can be 143 on this host.
            parts += [
                f"fld-{args.flood_profile[:3]}",
                f"{fp['pps']}p",
                f"{fp['size']}B",
            ]
        elif args.attack == "combined":
            bolus, period, pkt, flows = pulse_params(args)
            parts += [
                f"comb-pulse-B{bolus}KB-T{period}ms",
                f"cbr{args.cbr_mbps}M-P{args.cbr_parallel}",
            ]
        elif args.attack == "combined-c2":
            bolus, period, pkt, flows = pulse_params(args)
            parts += [
                f"cC2-B{bolus}",
                f"c{args.carrier_pps}x{args.carrier_size}",
            ]
        elif args.attack in ("sync-c2", "sync-prb"):
            parts += [
                f"sync-l{args.sync_listen_port}",
                f"b{int(args.sync_burst_ms)}ms",
                f"{args.sync_pps}pps",
            ]
            if args.attack == "sync-prb":
                parts.append("prb")
            if sync_open_loop_enabled(args):
                parts.append(f"ol-ph{int(args.sync_phase_ms)}ms")
        elif args.attack in MULTI_ROGUE_ATTACKS:
            parts.append(f"n{int(args.multi_rogue_count)}")
            if args.attack == "multi-rogue":
                parts.append(f"ph{int(args.multi_rogue_phase_ms)}ms")
            if args.attack == "multi-rogue-dl":
                parts.append("dl")
            elif args.attack == "multi-rogue-dl-sync":
                parts += [
                    "dl-sync",
                    f"b{int(args.multi_sync_burst_ms)}ms",
                    f"{args.multi_sync_pps}pps",
                ]
                if sync_open_loop_enabled(args):
                    parts.append(f"ol-ph{int(args.sync_phase_ms)}ms")
            elif args.attack == "multi-rogue-sync":
                parts += [
                    f"sync-b{int(args.multi_sync_burst_ms)}ms",
                    f"{args.multi_sync_pps}pps",
                ]
                if sync_open_loop_enabled(args):
                    parts.append(f"ol-ph{int(args.sync_phase_ms)}ms")
    vid = companion_video_mbps(args)
    if vid > 0:
        parts.append(f"vid{str(vid).replace('.', 'p')}mbps")
    if args.stream_cut_ms is not None:
        parts.append(f"cut{int(args.stream_cut_ms)}ms")
    return "_".join(parts)


def wait_run_cooldown(ctx, ns, c2_proxy=False, max_wait_s: int = 120) -> None:
    """Wait until host/ROGUE/proxy are stable for COOLDOWN_STABLE_S seconds."""
    deadline = time.monotonic() + max_wait_s
    stable_since: float | None = None
    while time.monotonic() < deadline:
        la = read_loadavg1()
        r = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-rogue", "-c", "nr-ue", "--",
                 "sh", "-c",
                 "pgrep -f '[c]o_tenant_load|[i]perf3|[r]ogue_pulse|[u]dp_flood|[c]2_sync_flood' "
                 ">/dev/null && echo attack_busy || echo attack_free")
        attack_free = "attack_free" in (r.stdout or "")
        proxy_free = True
        if c2_proxy:
            pr = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
                      "sh", "-c", f"ss -ltn 2>/dev/null | grep -q ':{SITL_PORT} ' && echo busy || echo free")
            proxy_free = "free" in (pr.stdout or "")
        mr = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
                  "sh", "-c",
                  "pgrep -f '[s]etpoint_stream|[m]ission_runner' >/dev/null "
                  "&& echo gcs_busy || echo gcs_free")
        gcs_free = "gcs_free" in (mr.stdout or "")
        clean = (
            la is not None
            and la <= COOLDOWN_LOADAVG_MAX
            and attack_free
            and proxy_free
            and gcs_free
        )
        if clean:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= COOLDOWN_STABLE_S:
                return
        else:
            stable_since = None
        time.sleep(1)
    raise RuntimeError(
        f"Cooldown failed: host/processes did not stabilize within {max_wait_s}s "
        f"(loadavg<={COOLDOWN_LOADAVG_MAX}, no attack/gcs/proxy busy, stable>={COOLDOWN_STABLE_S}s)"
    )


def wait_for_probe_ready(ready_path: Path, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if ready_path.is_file():
            try:
                data = json.loads(ready_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            if data.get("status") == "ready":
                return
        time.sleep(0.2)
    raise RuntimeError(f"Probe clock handshake did not become ready within {timeout_s}s ({ready_path})")


def wait_for_sink_ready(ctx: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    port = CO_TENANT_PORT
    while time.monotonic() < deadline:
        result = kctl(
            ctx, "exec", "-n", "core", "deploy/oai-ext-dn", "-c", "ext-dn", "--",
            "sh", "-c", f"ss -lun | grep -q ':{port} '",
        )
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError(f"Co-tenant UDP sink did not become ready on port {port}")


def wait_for_proxy_ready(ctx: str, ns: str, timeout_s: float = 15.0) -> None:
    """Wait until proxy listens on :5760 and SITL backend :5761 is up in the UAV pod."""
    deadline = time.monotonic() + timeout_s
    check = (
        f"ss -ltn 2>/dev/null | grep -q ':{SITL_PORT} ' && "
        f"ss -ltn 2>/dev/null | grep -q ':{SITL_BACKEND_PORT} '"
    )
    while time.monotonic() < deadline:
        result = kctl(
            ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
            "sh", "-c", check,
        )
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError(
        f"MAVLink proxy/SITL not ready in UAV pod "
        f"(need TCP :{SITL_PORT} proxy listener and :{SITL_BACKEND_PORT} SITL backend)"
    )


def effective_traj_duration_s(args, mission_len: float, envelope_s: float) -> float:
    """Setpoint-stream trajectory (and NED telemetry) duration.

    Option C (setpoint-stream, >= 600 s): tied to the attack schedule, ending at
    attack_end + 10 s (or + drain ceiling with --drain-until-baseline). It is never
    derived from the capture envelope, so extending the capture cannot stretch the
    stream past the capture again. Short runs keep the legacy mission_len - overhead.
    An explicit --trajectory-duration-s overrides.
    """
    explicit = getattr(args, "trajectory_duration_s", None)
    if explicit:
        return float(explicit)
    if optc_applies(args):
        return float(optc_schedule(args)["trajectory_duration_s"])
    return max(30.0, float(mission_len) - MISSION_OVERHEAD_S)


def drain_applies(args) -> bool:
    return (
        bool(getattr(args, "drain_until_baseline", False))
        and getattr(args, "condition", None) != "baseline"
    )


def optc_applies(args) -> bool:
    return _optc_applies_for(getattr(args, "workload", None), getattr(args, "duration", 0) or 0)


def optc_schedule(args) -> dict[str, Any]:
    """Option C schedule (s since T0); identical for baseline and attack without drain."""
    duration = int(getattr(args, "duration", 0) or 0)
    mission_len, _n_cmds, _margin = mission_plan(duration)
    return optc_plan_schedule(
        duration_s=duration,
        warmup_s=float(getattr(args, "warmup", 0) or 0),
        mission_len_s=mission_len,
        requested_delay_s=float(getattr(args, "attack_start_after_s", 0.0) or 0.0),
        takeoff_guard_s=ATTACK_TAKEOFF_GUARD_S,
        drain_ceiling_s=(
            float(getattr(args, "drain_ceiling_s", 180.0) or 180.0)
            if drain_applies(args) else 0.0
        ),
    )


def proxy_watchdog_s(args, envelope_s: float, mission_len: float) -> int:
    """Emergency upper bound only — normal shutdown is via stop-file after mission."""
    if not args.c2_proxy:
        return int(envelope_s)
    if args.workload == "setpoint-stream":
        traj = effective_traj_duration_s(args, mission_len, envelope_s)
        return int(envelope_s + traj + 180)
    return int(envelope_s + mission_len + 120)


def proxy_duration_s(args, envelope_s: float, mission_len: float) -> int:
    """Deprecated alias — use proxy_watchdog_s."""
    return proxy_watchdog_s(args, envelope_s, mission_len)


def mission_wait_timeout_s(args, mission_len: float, envelope_s: float) -> float:
    if args.workload == "setpoint-stream":
        traj = effective_traj_duration_s(args, mission_len, envelope_s)
        return traj + 150.0
    return mission_len + 300.0


def _iperf_interval_path_bytes(
    streams: list[dict[str, Any]],
    cbr_dir: str,
    *,
    path: str = "receiver",
) -> int:
    """Bytes per interval on the attack data path (receiver-side for DL, sender-side for UL)."""
    if path == "sender":
        want_sender = True
    elif cbr_dir == "dl":
        want_sender = False
    else:
        want_sender = True
    total = 0
    for stream in streams:
        is_sender = bool(stream.get("sender", True))
        if is_sender == want_sender:
            total += int(stream.get("bytes") or 0)
    return total


def analyze_iperf3_cbr_activity(
    raw: dict[str, Any] | None,
    cbr_dir: str,
) -> dict[str, Any]:
    """Derive attack duration and sustained path activity from iperf3 -J intervals."""
    info: dict[str, Any] = {
        "expected_attack_duration_s": None,
        "observed_attack_duration_s": None,
        "duration_ratio": None,
        "receiver_active_duration_s": None,
        "receiver_active_window_ratio": None,
        "receiver_max_gap_s": None,
        "receiver_third_bytes": None,
        "receiver_thirds_active": None,
    }
    if not raw:
        return info
    start = raw.get("start") or {}
    test = start.get("test_start") or {}
    end = raw.get("end") or {}
    recv_sum = end.get("sum_received") or {}
    sent_sum = end.get("sum_sent") or {}
    expected_s = float(test.get("duration") or 0)
    observed_s = float(recv_sum.get("seconds") or sent_sum.get("seconds") or 0)
    if expected_s <= 0:
        expected_s = observed_s
    info["expected_attack_duration_s"] = round(expected_s, 3) if expected_s else None
    info["observed_attack_duration_s"] = round(observed_s, 3) if observed_s else None
    if expected_s > 0 and observed_s > 0:
        info["duration_ratio"] = round(observed_s / expected_s, 4)

    intervals = raw.get("intervals") or []
    bins: list[tuple[float, float, int]] = []
    for iv in intervals:
        streams = iv.get("streams") or []
        if not streams:
            continue
        t0 = min(float(s.get("start") or 0) for s in streams)
        t1 = max(float(s.get("end") or t0) for s in streams)
        if t1 <= t0:
            t1 = t0 + float(streams[0].get("seconds") or 1.0)
        nbytes = _iperf_interval_path_bytes(streams, cbr_dir, path="receiver")
        bins.append((t0, t1, nbytes))
    if not bins:
        return info

    active_bins = [b for b in bins if b[2] > 0]
    if active_bins:
        active_span = max(b[1] for b in active_bins) - min(b[0] for b in active_bins)
        info["receiver_active_duration_s"] = round(active_span, 3)
    else:
        info["receiver_active_duration_s"] = 0.0

    total_span = max(b[1] for b in bins) - min(b[0] for b in bins)
    if total_span > 0:
        active_time = sum(b[1] - b[0] for b in active_bins)
        info["receiver_active_window_ratio"] = round(active_time / total_span, 4)

    max_gap = 0.0
    gap_start: float | None = None
    for t0, t1, nbytes in bins:
        if nbytes > 0:
            if gap_start is not None:
                max_gap = max(max_gap, t0 - gap_start)
                gap_start = None
        elif gap_start is None:
            gap_start = t0
    info["receiver_max_gap_s"] = round(max_gap, 3)

    t_min = min(b[0] for b in bins)
    t_max = max(b[1] for b in bins)
    span = t_max - t_min
    third_bytes = [0, 0, 0]
    if span > 0:
        for t0, t1, nbytes in bins:
            mid = (t0 + t1) / 2.0
            idx = min(2, int((mid - t_min) / (span / 3.0)))
            third_bytes[idx] += nbytes
    info["receiver_third_bytes"] = third_bytes
    info["receiver_thirds_active"] = all(b >= ATTACK_RECEIVER_THIRD_MIN_BYTES for b in third_bytes)
    return info


def parse_iperf3_attack_summary(
    raw: dict[str, Any],
    run_id: str,
    cbr_mbps_per_stream: float,
    cbr_parallel: int,
    cbr_dir: str = "ul",
    cbr_profile: str = CBR_PROFILE_SATURATING,
    cbr_protocol: str = "udp",
) -> dict[str, Any]:
    """Normalize iperf3 -J output into attack_summary schema (single direction only)."""
    if cbr_dir == "both":
        raise ValueError("cbr_dir=both requires a bidirectional parser (not implemented)")
    end = raw.get("end") or {}
    sent = end.get("sum_sent") or end.get("sum") or {}
    recv = end.get("sum_received") or {}
    sender_bps = float(sent.get("bits_per_second") or 0)
    receiver_bps = float(recv.get("bits_per_second") or 0)
    requested_aggregate = float(cbr_mbps_per_stream) * max(1, int(cbr_parallel))
    bytes_sent = int(sent.get("bytes") or 0)
    bytes_received = int(recv.get("bytes") or 0)
    delivery_ratio = (bytes_received / bytes_sent) if bytes_sent > 0 else None
    undelivered_ratio = (1.0 - delivery_ratio) if delivery_ratio is not None else None
    packets_received = int(recv.get("packets") or 0)
    packets_lost = int(recv.get("lost_packets") or 0)
    udp_packet_loss_ratio = None
    if recv.get("lost_percent") is not None:
        udp_packet_loss_ratio = round(float(recv["lost_percent"]) / 100.0, 4)
    elif packets_received + packets_lost > 0:
        udp_packet_loss_ratio = round(packets_lost / (packets_received + packets_lost), 4)
    activity = analyze_iperf3_cbr_activity(raw, cbr_dir)
    return {
        "mode": "cbr",
        "run_id": run_id,
        "path": "pdu",
        "cbr_dir": cbr_dir,
        "cbr_protocol": cbr_protocol,
        "cbr_profile": cbr_profile,
        "cbr_mbps_per_stream": cbr_mbps_per_stream,
        "cbr_parallel": cbr_parallel,
        "requested_aggregate_mbps": requested_aggregate,
        "requested_mean_mbps": requested_aggregate,
        "offered_mbps": requested_aggregate,
        "achieved_sender_mbps": round(sender_bps / 1e6, 3),
        "achieved_mean_mbps_sender": round(sender_bps / 1e6, 3),
        "achieved_receiver_mbps": round(receiver_bps / 1e6, 3),
        "delivered_mbps": round(receiver_bps / 1e6, 3),
        "bytes_sent": bytes_sent,
        "bytes_received": bytes_received,
        "packets_sent": sent.get("packets", 0),
        "packets_received": packets_received,
        "packets_lost": packets_lost,
        "lost_percent": recv.get("lost_percent"),
        "delivery_ratio": round(delivery_ratio, 4) if delivery_ratio is not None else None,
        "undelivered_ratio": round(undelivered_ratio, 4) if undelivered_ratio is not None else None,
        "udp_packet_loss_ratio": udp_packet_loss_ratio,
        **activity,
    }


def evaluate_proxy_gate(run_dir: Path, proxy_rc: int | None) -> tuple[bool, dict[str, Any]]:
    """Validate proxy using proxy_summary.json fields from mavlink_proxy.py."""
    info: dict[str, Any] = {"proxy_rc": proxy_rc}
    fwd_path = run_dir / "proxy_forward.csv"
    if proxy_rc != 0:
        info["reason"] = "proxy process non-zero exit"
        return False, info
    if not fwd_path.is_file() or fwd_path.stat().st_size == 0:
        info["reason"] = "proxy_forward.csv missing or empty"
        return False, info
    rev_path = run_dir / "proxy_reverse.csv"
    if not rev_path.is_file() or rev_path.stat().st_size == 0:
        info["reason"] = "proxy_reverse.csv missing or empty"
        return False, info
    ps_path = run_dir / "proxy_summary.json"
    if not ps_path.is_file():
        info["reason"] = "proxy_summary.json missing"
        return False, info
    try:
        ps = json.loads(ps_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        info["reason"] = "proxy_summary.json invalid"
        return False, info
    missing = PROXY_SUMMARY_REQUIRED_FIELDS - ps.keys()
    if missing:
        info["reason"] = f"proxy_summary fields missing: {sorted(missing)}"
        return False, info
    forward = int(ps["forward_frames"])
    reverse = int(ps["reverse_frames"])
    parse_errors = int(ps["parse_errors"])
    reconnects = int(ps["reconnects"])
    discarded = int(ps["frames_discarded"])
    delay_p99 = dget(ps, "proxy_delay_ms", "p99")
    info.update({
        "forward_frames": forward,
        "reverse_frames": reverse,
        "parse_errors": parse_errors,
        "reconnects": reconnects,
        "frames_discarded": discarded,
        "proxy_delay_p99_ms": delay_p99,
        "proxy_delay_max_ms": PROXY_DELAY_P99_MAX_MS,
    })
    if delay_p99 is None:
        if forward >= 20:
            info["reason"] = "proxy_delay_ms.p99 missing"
            return False, info
        delay_p99 = 0.0
        info["proxy_delay_p99_ms"] = None
        info["proxy_delay_p99_note"] = "too few forward frames for p99 (stream-cut / short run)"
    ok = (
        forward > 0
        and parse_errors == 0
        and reconnects == 0
        and discarded == 0
        and int(ps.get("parse_queue_drops") or 0) == 0
        and float(delay_p99) <= PROXY_DELAY_P99_MAX_MS
    )
    if reverse == 0:
        info["reverse_frames_note"] = (
            "no POSITION_TARGET reverse rows (expected for SET_POSITION stream workloads)"
        )
    if not ok and "reason" not in info:
        info["reason"] = "proxy_summary quality gate failed"
    return ok, info


def evaluate_freshness_integrity_gate(
    fresh: dict[str, Any],
    merge_meta: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    try:
        from freshness_metrics import validate_freshness_integrity  # noqa: WPS433
    except ImportError as exc:
        return False, {"reason": f"import failed: {exc}"}
    return validate_freshness_integrity(fresh, merge_meta)


def evaluate_stream_cut_gate(
    run_dir: Path,
    stream_cut_ms: float,
    setpoint_rate_hz: float,
    traj_dur_s: float,
    *,
    n_setpoints: int | None = None,
) -> tuple[bool, dict[str, Any]]:
    """I-0A: temporary pause must resume with quantitatively correct AoI spike."""
    path = run_dir / "stream_cut_summary.json"
    info: dict[str, Any] = {"path": str(path)}
    if not path.is_file():
        return False, {**info, "reason": "stream_cut_summary.json missing"}
    try:
        cut = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, {**info, "reason": f"invalid JSON: {exc}"}
    info["cut"] = cut
    nominal_period_ms = 1000.0 / setpoint_rate_hz if setpoint_rate_hz > 0 else 100.0
    req_ms = float(cut.get("cut_requested_ms") or stream_cut_ms)
    obs_ms = cut.get("cut_observed_ms")
    if not cut.get("stream_resumed"):
        return False, {**info, "reason": "stream did not resume after pause"}
    if obs_ms is None:
        return False, {**info, "reason": "cut_observed_ms missing"}
    obs_ms_f = float(obs_ms)
    tol_ms = max(50.0, req_ms * 0.25)
    if abs(obs_ms_f - req_ms) > tol_ms:
        return False, {
            **info,
            "reason": f"cut_observed_ms {obs_ms_f:.1f} outside tolerance of {req_ms:.1f} ms",
        }
    suppressed = int(cut.get("setpoints_suppressed") or 0)
    expected_suppressed = max(1, int(req_ms / 1000.0 * setpoint_rate_hz * 0.65))
    if suppressed < expected_suppressed:
        return False, {
            **info,
            "reason": f"setpoints_suppressed {suppressed} < expected ~{expected_suppressed}",
        }
    peak_expected_ms = nominal_period_ms + req_ms
    aoi_max = cut.get("aoi_max_during_cut_ms")
    if aoi_max is None or float(aoi_max) < peak_expected_ms * 0.55:
        return False, {
            **info,
            "reason": (
                f"aoi_max_during_cut_ms {aoi_max} below expected ~{peak_expected_ms:.0f} ms "
                "(nominal_period + pause)"
            ),
        }
    if float(aoi_max) > traj_dur_s * 1000.0 * 0.5:
        return False, {
            **info,
            "reason": (
                f"aoi_max_during_cut_ms {aoi_max} suggests permanent stream stop, not temporary pause"
            ),
        }
    for label, key in (("before", "rate_before_cut_hz"), ("after", "rate_after_cut_hz")):
        rate = cut.get(key)
        if rate is None:
            return False, {**info, "reason": f"{key} missing"}
        if abs(float(rate) - setpoint_rate_hz) / setpoint_rate_hz > SETPOINT_RATE_TOL:
            return False, {
                **info,
                "reason": f"rate_{label}_cut_hz {rate} outside +/-{SETPOINT_RATE_TOL*100:.0f}%",
            }
    if n_setpoints is not None and traj_dur_s > 0:
        expected_total = max(
            1,
            int(traj_dur_s * setpoint_rate_hz - setpoint_rate_hz * req_ms / 1000.0),
        )
        if n_setpoints < expected_total * (1.0 - SETPOINT_RATE_TOL):
            return False, {
                **info,
                "reason": (
                    f"n_setpoints {n_setpoints} below expected ~{expected_total} "
                    "after accounting for pause"
                ),
            }
    info["peak_expected_ms"] = round(peak_expected_ms, 2)
    return True, info


def write_keyboard_interrupt_run_meta(
    run_dir: Path,
    run_id: str,
    args,
    *,
    wall_t0: float,
    cleanup_audit: dict[str, Any],
    port_check: dict[str, Any],
) -> None:
    """Persist partial run metadata when the operator interrupts mid-run."""
    meta = {
        "run_id": run_id,
        "valid": False,
        "termination_reason": "keyboard_interrupt",
        "cleanup_completed": bool(cleanup_audit.get("clean")),
        "processes_terminated": cleanup_audit.get("clean"),
        "port_5760_free": port_check.get("proxy_port_5760") == "free",
        "port_5761_preserved": port_check.get("sitl_port_5761") == "busy",
        "wall_runtime_s": round(time.monotonic() - wall_t0, 3),
        "cleanup_audit": cleanup_audit,
        "port_check": port_check,
        "condition": getattr(args, "condition", None),
        "topology": getattr(args, "topology", None),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "run_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def evaluate_prb_gate(
    prb: dict[str, Any],
    require: bool,
    envelope_s: float | None = None,
    *,
    run_id: str | None = None,
    companion_video_mbps: float = 0.0,
) -> tuple[bool, dict[str, Any]]:
    """PRB evidence gate — best-effort by default; strict with --require-prb."""
    dump_samples = int(prb.get("dump_samples") or 0)
    window_s = float(prb.get("window_s") or 0)
    per_slice = prb.get("per_slice") or {}
    per_role = prb.get("per_role") or {}
    capture_meta = prb.get("capture_meta") or {}
    identity = prb.get("slice_identity") or {}
    info: dict[str, Any] = {
        "required": require,
        "dump_samples": dump_samples,
        "window_s": window_s,
        "per_slice_slices": sorted(per_slice.keys()),
        "roles_mapped": sorted(per_role.keys()),
        "capture_meta": capture_meta or None,
        "run_id": run_id,
        "slice_source": identity.get("slice_source"),
        "role_identity_source": identity.get("role_identity_source"),
        "kubectl_restarts": capture_meta.get("kubectl_restarts"),
        "idle_restarts": capture_meta.get("idle_restarts"),
        "max_silence_s": capture_meta.get("max_silence_s"),
    }
    if run_id and capture_meta.get("run_id") and capture_meta.get("run_id") != run_id:
        info["run_id_match"] = False
        info["reason"] = "PRB capture run_id mismatch (stale dump?)"
        return False, info
    if run_id and capture_meta:
        capture_meta.setdefault("run_id", run_id)
        info["run_id_match"] = capture_meta.get("run_id") == run_id
    if envelope_s is not None:
        info["envelope_s"] = envelope_s
        info["min_window_s"] = round(float(envelope_s) * PRB_CAPTURE_MIN_SPAN_RATIO, 1)
    if not require:
        info["mode"] = "best-effort"
        return True, info
    roles_ok = all(
        role in per_role and bool(per_role[role].get("rnti"))
        for role in ("rogue", "gcs", "uav")
    )
    scheduler_ok = all(
        per_role.get(role, {}).get("mac_dump_samples", 0) > 0
        for role in ("rogue", "gcs", "uav")
    ) if roles_ok else False
    span_ok = True
    if envelope_s is not None and envelope_s > 0:
        span_ok = window_s >= float(envelope_s) * PRB_CAPTURE_MIN_SPAN_RATIO
        info["span_ok"] = span_ok
    # With companion video on, UAV is the UL sender and GCS the DL receiver by
    # construction; if the parsed roles disagree the slice attribution is wrong
    # and every per-slice figure would be misleading.
    identity_ok = True
    if companion_video_mbps > 0 and roles_ok:
        uav_ul = dget(per_role, "uav", "ul_mbps") or 0.0
        gcs_dl = dget(per_role, "gcs", "dl_mbps") or 0.0
        # The UAV UL counter is the direct signature of the video sender: a
        # mis-labelled UAV shows ~0.01 Mbps, while a heavily degraded but
        # correctly labelled run still carries a fifth of the offered rate.
        # gcs_dl is reported but not gated — the DL byte counter is missing from
        # some gNB builds even when the mapping is right.
        floor = PRB_ROLE_IDENTITY_MIN_RATIO * float(companion_video_mbps)
        identity_ok = float(uav_ul) >= floor
        info.update({
            "identity_ok": identity_ok,
            "uav_ul_mbps": uav_ul,
            "gcs_dl_mbps": gcs_dl,
            "identity_floor_mbps": round(floor, 3),
        })
    ok = (
        dump_samples > 0 and bool(per_slice) and roles_ok
        and scheduler_ok and span_ok and identity_ok
    )
    info.update({
        "roles_ok": roles_ok,
        "scheduler_ok": scheduler_ok,
        "rogue_rnti": dget(per_role, "rogue", "rnti"),
        "gcs_rnti": dget(per_role, "gcs", "rnti"),
        "uav_rnti": dget(per_role, "uav", "rnti"),
        "audit_checklist": PRB_GATE_AUDIT,
        "pilot_completed": PRB_PILOT_COMPLETED,
    })
    if not ok:
        if dump_samples <= 0 or not per_slice:
            info["reason"] = "no gNB MAC dump evidence (dump_samples/per_slice)"
        elif not span_ok:
            info["reason"] = (
                "PRB dump window %.1fs < %.1fs (%.0f%% of envelope)"
                % (window_s, info.get("min_window_s", 0), PRB_CAPTURE_MIN_SPAN_RATIO * 100)
            )
        elif not roles_ok:
            info["reason"] = "RNTI role map incomplete (need rogue/gcs/uav)"
        elif not identity_ok:
            info["reason"] = (
                "role/slice identity inconsistent with companion video "
                "(uav_ul=%.2f gcs_dl=%.2f < %.2f Mbps)"
                % (info.get("uav_ul_mbps") or 0.0, info.get("gcs_dl_mbps") or 0.0,
                   info.get("identity_floor_mbps") or 0.0)
            )
        else:
            info["reason"] = "scheduler grants/MCS evidence missing for one or more roles"
    return ok, info


def evaluate_clock_gate(
    *,
    probe_rc: int | None,
    ordering_violations: int | None,
    negative_owd_samples: int | None,
    temporal_trend_us: float | None,
    trend_max_us: float | None,
    time_namespace_match: bool | None,
) -> tuple[bool, str | None]:
    """Decide the clock-sanity validity gate.

    The single-host testbed shares CLOCK_MONOTONIC across GCS/UAV pods, so when the
    time namespaces match (``time_namespace_match=true``, confirmed by owd_probe) a
    clock *skew* is physically impossible. In that case a large OWD *temporal trend*
    is a genuine path/queue non-stationarity — the attack signature itself (e.g.
    sync-c2 inflating the tail) — and must NOT invalidate the run, or the gate would
    erase the very effect we measure.

    Conservative rules (do not loosen beyond this):
      * CAUSALITY always invalidates: negative OWD (rx before tx) or handshake
        ordering violations are real measurement errors, regardless of namespace.
      * TEMPORAL TREND only invalidates when the clock CAN drift, i.e. when there is
        no shared namespace (``time_namespace_match`` false/unknown) — the original
        behavior, preserved for multi-host / unshared-clock setups.
      * Any other probe-process failure (too few samples, availability, DO-377A caps,
        crash) still vetoes via ``probe_rc``.
    """
    if (negative_owd_samples or 0) > 0:
        return False, (
            "negative OWD samples=%d — causality violation (rx before tx on shared clock)"
            % int(negative_owd_samples or 0)
        )
    if (ordering_violations or 0) > 0:
        return False, (
            "handshake ordering violations=%d — causality/clock integrity failure"
            % int(ordering_violations or 0)
        )
    trend_exceeds = (
        temporal_trend_us is not None
        and trend_max_us
        and abs(temporal_trend_us) > float(trend_max_us)
    )
    if trend_exceeds and not time_namespace_match:
        return False, (
            "OWD temporal trend |%.1f| us exceeds cap %.1f us with no shared time "
            "namespace — possible clock skew" % (temporal_trend_us, float(trend_max_us))
        )
    # trend_exceeds AND time_namespace_match: shared clock => skew impossible => the
    # trend is the attack/path signal, not contamination => do not invalidate.
    if probe_rc not in (0, None):
        return False, "owd_probe exited rc=%s" % probe_rc
    return True, None


def expand_core_spec(spec: str) -> set[int]:
    out: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            out.update(range(lo, hi + 1))
        else:
            out.add(int(part))
    return out


def validate_pin_core_layout(stamp: dict[str, str], ncores: int) -> list[str]:
    """Validate CPU pinning layout when PIN_CORES=1."""
    if stamp.get("PIN_CORES") != "1":
        return []
    problems: list[str] = []
    roles = {
        "CORES_GNB": stamp.get("CORES_GNB", ""),
        "CORES_GCS": stamp.get("CORES_GCS", ""),
        "CORES_UAV": stamp.get("CORES_UAV", ""),
        "CORES_ROGUE": stamp.get("CORES_ROGUE", ""),
        "CORES_ATTACK": stamp.get("CORES_ATTACK", ""),
    }
    assigned: dict[str, set[int]] = {}
    for name, spec in roles.items():
        if not spec:
            problems.append(f"{name} empty with PIN_CORES=1")
            continue
        cpus = expand_core_spec(spec)
        if not cpus:
            problems.append(f"{name}={spec!r} parses to empty set")
            continue
        if min(cpus) < 0 or max(cpus) >= ncores:
            problems.append(f"{name}={spec} exceeds host nproc={ncores}")
        assigned[name] = cpus
    seen: dict[int, str] = {}
    for name, cpus in assigned.items():
        for cpu in cpus:
            if cpu in seen:
                problems.append(
                    f"CPU {cpu} shared by {seen[cpu]} and {name} (pin sets must be disjoint)"
                )
            else:
                seen[cpu] = name
    if ncores >= 16:
        housekeeping = set(range(14, 16))
        used = set().union(*assigned.values()) if assigned else set()
        free_hk = housekeeping - used
        if not free_hk:
            problems.append("no housekeeping cores free in 14-15 with PIN_CORES=1")
    return problems


def analyze_rogue_prb_attack_overlap(
    prb_csv: Path | None,
    attack_start_s: float,
    attack_end_s: float,
    prb_start_s: float = 0.0,
) -> dict[str, Any]:
    """Temporal overlap between run-scoped attack window and ROGUE PRB samples."""
    info: dict[str, Any] = {
        "attack_start_s": round(attack_start_s, 3),
        "attack_end_s": round(attack_end_s, 3),
        "attack_duration_s": round(max(0.0, attack_end_s - attack_start_s), 3),
        "prb_start_s": round(prb_start_s, 3),
        "rogue_samples_in_attack": 0,
        "rogue_active_span_s": None,
        "attack_overlap_ratio": None,
        "rogue_thirds_in_attack": None,
    }
    attack_dur = max(0.0, attack_end_s - attack_start_s)
    if attack_dur <= 0 or prb_csv is None or not prb_csv.is_file():
        info["reason"] = "attack/PRB timeline unavailable"
        return info

    rel_times: list[float] = []
    seen: set[float] = set()
    try:
        with prb_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("role") != "rogue":
                    continue
                t_run = prb_start_s + float(row["t_rel_s"])
                if attack_start_s <= t_run <= attack_end_s:
                    t_attack = t_run - attack_start_s
                    key = round(t_attack, 3)
                    if key not in seen:
                        seen.add(key)
                        rel_times.append(t_attack)
    except (OSError, ValueError, KeyError):
        info["reason"] = "PRB CSV parse failed"
        return info

    rel_times.sort()
    info["rogue_samples_in_attack"] = len(rel_times)
    if not rel_times:
        info["reason"] = "no ROGUE PRB samples during attack window"
        return info

    span = rel_times[-1] - rel_times[0]
    info["rogue_active_span_s"] = round(span, 3)
    info["attack_overlap_ratio"] = round(span / attack_dur, 4) if attack_dur > 0 else None
    third = attack_dur / 3.0
    thirds = [
        any(0.0 <= t < third for t in rel_times),
        any(third <= t < 2 * third for t in rel_times),
        any(t >= 2 * third for t in rel_times),
    ]
    info["rogue_thirds_in_attack"] = thirds
    return info


def evaluate_ran_engagement(
    prb: dict[str, Any],
    expected_attack_duration_s: float,
    *,
    attack_start_s: float | None = None,
    attack_end_s: float | None = None,
    prb_start_s: float = 0.0,
    prb_csv: Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Verify sustained RAN scheduler engagement by the attacker during the attack window."""
    info: dict[str, Any] = {
        "attacker_rnti": None,
        "attacker_slice": None,
        "prb_samples": 0,
        "prb_window_s": float(prb.get("window_s") or 0),
        "active_fraction": None,
        "ul_nprb_sum": 0,
        "dl_goodput_mbps_sum": 0.0,
        "evidence": None,
        "limitation": None,
        "temporal_overlap": None,
    }
    per_role = prb.get("per_role") or {}
    rogue = per_role.get("rogue") or {}
    window_s = float(prb.get("window_s") or 0)
    dump_samples = int(prb.get("dump_samples") or 0)
    expected_s = float(expected_attack_duration_s or 0)
    if expected_s > 0:
        info["active_fraction"] = round(window_s / expected_s, 4)

    temporal_ok = True
    overlap = None
    if (
        attack_start_s is not None
        and attack_end_s is not None
        and prb_csv is not None
        and prb_csv.is_file()
    ):
        overlap = analyze_rogue_prb_attack_overlap(
            prb_csv, float(attack_start_s), float(attack_end_s), prb_start_s,
        )
        info["temporal_overlap"] = overlap
        attack_dur = float(overlap.get("attack_duration_s") or 0)
        temporal_ok = (
            int(overlap.get("rogue_samples_in_attack") or 0) >= ATTACK_RAN_ENGAGEMENT_MIN_SAMPLES
            and overlap.get("attack_overlap_ratio") is not None
            and float(overlap["attack_overlap_ratio"]) >= ATTACK_RAN_ENGAGEMENT_MIN_WINDOW_RATIO
            and all(overlap.get("rogue_thirds_in_attack") or [])
        )
        if attack_dur > 0 and not temporal_ok:
            if int(overlap.get("rogue_samples_in_attack") or 0) < ATTACK_RAN_ENGAGEMENT_MIN_SAMPLES:
                info["reason"] = "attacker PRB samples below minimum during attack window"
            elif not all(overlap.get("rogue_thirds_in_attack") or []):
                info["reason"] = "attacker PRB activity absent in part of attack window"
            else:
                info["reason"] = "insufficient temporal overlap between attack and ROGUE PRB"
    else:
        info["limitation"] = "attack timeline offsets unavailable; temporal overlap not verified"
        temporal_ok = True

    if rogue.get("rnti"):
        samples = int(rogue.get("mac_dump_samples") or 0)
        nprb = int(rogue.get("ul_nprb_samples_sum") or 0)
        dl_gp = float(rogue.get("dl_goodput_mbps_sum") or 0)
        ul_gp = float(rogue.get("ul_goodput_mbps_sum") or 0)
        dl_rounds = int(rogue.get("dl_rounds_last") or 0)
        ul_rounds = int(rogue.get("ul_rounds_last") or 0)
        info.update({
            "attacker_rnti": rogue.get("rnti"),
            "attacker_slice": rogue.get("slice"),
            "prb_samples": samples,
            "ul_nprb_sum": nprb,
            "dl_goodput_mbps_sum": dl_gp,
            "ul_goodput_mbps_sum": ul_gp,
            "evidence": "rogue_rnti",
        })
        grants_ok = nprb > 0 or dl_gp > 0 or ul_gp > 0 or dl_rounds > 0 or ul_rounds > 0
        samples_ok = samples >= ATTACK_RAN_ENGAGEMENT_MIN_SAMPLES
        coverage_ok = (
            expected_s <= 0
            or window_s >= expected_s * ATTACK_RAN_ENGAGEMENT_MIN_WINDOW_RATIO
        )
        ok = grants_ok and samples_ok and coverage_ok and temporal_ok
        if not ok and not info.get("reason"):
            if not grants_ok:
                info["reason"] = "attacker NPRB/grants not observed"
            elif not samples_ok:
                info["reason"] = "attacker PRB samples below minimum"
            elif not coverage_ok:
                info["reason"] = "PRB capture window too short for attack duration"
        return ok, info

    if dump_samples >= ATTACK_RAN_ENGAGEMENT_MIN_SAMPLES and window_s > 0:
        info["evidence"] = "aggregate_only"
        info["limitation"] = "attacker RNTI/slice not mapped; using aggregate PRB activity"
        coverage_ok = (
            expected_s <= 0
            or window_s >= expected_s * ATTACK_RAN_ENGAGEMENT_MIN_WINDOW_RATIO
        )
        ok = coverage_ok and temporal_ok
        if not ok and not info.get("reason"):
            info["reason"] = "PRB capture incomplete"
        return ok, info

    info["reason"] = "RAN engagement not observed"
    return False, info


def scan_instrumentation_faults(run_dir: Path) -> list[str]:
    """Detect OOM/kill signatures in layer live logs."""
    faults: list[str] = []
    patterns = ("out of memory", "oom-kill", "killed process", "signal 9", "sigkill")
    for name in (
        "probe.live.log", "host_telemetry.live.log", "mission.live.log",
        "attack.live.log", "proxy.live.log", "prb.live.log",
    ):
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            tail = path.read_text(encoding="utf-8", errors="replace")[-12000:].lower()
        except OSError:
            continue
        for pat in patterns:
            if pat in tail:
                faults.append(f"{name}: {pat}")
                break
    return faults


def evaluate_host_instrumentation_integrity(
    *,
    run_dir: Path,
    probe_rc: int | None,
    host_rc: int | None,
    prb_rc: int | None,
    mission_rc: int | None,
    proxy_rc: int | None,
    attack_rc: int | None,
    host_summary: dict[str, Any],
    envelope_s: float,
    require_prb: bool,
    c2_proxy: bool,
    is_attack: bool,
    clock_pass: bool,
    proxy_pass: bool,
    freshness_pass: bool,
    stream_pass: bool,
    prb_pass: bool,
    prb_gate_info: dict[str, Any],
    host_proc_started: bool,
) -> tuple[bool, dict[str, Any]]:
    """Aggregate instrumentation health — distinct from host load/quality gate."""
    samples = int(host_summary.get("samples") or 0)
    min_samples = int(envelope_s * HOST_TELEMETRY_MIN_COVERAGE_RATIO)
    oom_faults = scan_instrumentation_faults(run_dir)

    def _chk(name: str, passed: bool, detail: str | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {"pass": passed}
        if detail:
            row["detail"] = detail
        return row

    subchecks: dict[str, Any] = {
        "clock": _chk("clock", clock_pass),
        "probe_rc": _chk("probe_rc", probe_rc in (0, None), f"rc={probe_rc}"),
        "host_telemetry_proc": _chk(
            "host_telemetry_proc", host_proc_started and host_rc in (0, None), f"rc={host_rc}",
        ),
        "host_telemetry_span": _chk(
            "host_telemetry_span",
            samples >= min_samples,
            f"samples={samples} min={min_samples}",
        ),
        "prb_capture_rc": _chk(
            "prb_capture_rc",
            (not require_prb) or prb_rc in (0, None),
            f"rc={prb_rc}",
        ),
        "prb_capture_gate": _chk(
            "prb_capture_gate",
            (not require_prb) or prb_pass,
            prb_gate_info.get("reason"),
        ),
        "proxy_rc": _chk(
            "proxy_rc",
            (not c2_proxy) or proxy_rc in (0, None),
            f"rc={proxy_rc}",
        ),
        "proxy_gate": _chk("proxy_gate", (not c2_proxy) or proxy_pass),
        "freshness_gate": _chk("freshness_gate", freshness_pass),
        "stream_gate": _chk("stream_gate", stream_pass),
        "attack_rc": _chk(
            "attack_rc",
            (not is_attack) or attack_rc in (0, None),
            f"rc={attack_rc}",
        ),
        "oom_or_kill": _chk("oom_or_kill", not oom_faults, ", ".join(oom_faults) or None),
    }
    failed = [name for name, row in subchecks.items() if not row["pass"]]
    ok = not failed
    info: dict[str, Any] = {
        "probe_rc": probe_rc,
        "host_rc": host_rc,
        "prb_rc": prb_rc,
        "mission_rc": mission_rc,
        "proxy_rc": proxy_rc,
        "attack_rc": attack_rc,
        "host_samples": samples,
        "host_min_samples": min_samples,
        "oom_or_kill": oom_faults,
        "subchecks": subchecks,
        "failed_subchecks": failed,
        "failures": failed,
    }
    if not ok:
        first = failed[0]
        detail = subchecks[first].get("detail")
        info["reason"] = f"{first}: {detail}" if detail else first.replace("_", " ")
    return ok, info


def evaluate_cbr_attack_checks(
    attack_summary: dict[str, Any],
    raw_iperf: dict[str, Any] | None,
    run_id: str,
    expected_attack_duration_s: float,
    cbr_profile: str,
    prb: dict[str, Any],
    *,
    attack_start_s: float | None = None,
    attack_end_s: float | None = None,
    prb_start_s: float = 0.0,
    prb_csv: Path | None = None,
) -> dict[str, Any]:
    """CBR attack integrity sub-checks — offered load vs delivered outcomes separated."""
    info: dict[str, Any] = {
        "cbr_profile": cbr_profile,
        "identity_ok": True,
        "sender_target_ok": False,
        "attack_duration_ok": False,
        "receiver_path_active": False,
        "ran_engagement_ok": False,
        "delivery_enforced": cbr_profile == CBR_PROFILE_NON_SATURATING,
        "failures": [],
    }
    if not attack_summary or attack_summary.get("run_id") != run_id:
        info["identity_ok"] = False
        info["failures"].append("attack artifacts run_id mismatch (stale process?)")
        info["reason"] = info["failures"][0]
        return info

    req = attack_summary.get("requested_aggregate_mbps") or attack_summary.get("offered_mbps")
    sender_mbps = attack_summary.get("achieved_sender_mbps") or attack_summary.get(
        "achieved_mean_mbps_sender",
    )
    sender_bytes = int(attack_summary.get("bytes_sent") or 0)
    recv_mbps = attack_summary.get("achieved_receiver_mbps") or attack_summary.get("delivered_mbps")
    recv_bytes = int(attack_summary.get("bytes_received") or 0)
    cbr_dir = attack_summary.get("cbr_dir") or "ul"
    delivery_ratio = attack_summary.get("delivery_ratio")
    if delivery_ratio is None and sender_bytes > 0:
        delivery_ratio = recv_bytes / sender_bytes

    activity = analyze_iperf3_cbr_activity(raw_iperf, cbr_dir)
    if not activity.get("duration_ratio") and attack_summary.get("duration_ratio") is not None:
        activity["duration_ratio"] = attack_summary.get("duration_ratio")
    info.update({
        "offered_mbps": req,
        "sender_mbps": sender_mbps,
        "sender_ratio": round(float(sender_mbps) / float(req), 4) if req and sender_mbps else None,
        "delivered_mbps": recv_mbps,
        "delivery_ratio": delivery_ratio,
        "undelivered_ratio": attack_summary.get("undelivered_ratio"),
        "udp_packet_loss_ratio": attack_summary.get("udp_packet_loss_ratio"),
        **activity,
    })

    if attack_summary.get("cbr_protocol") == "tcp":
        # TCP best-effort fill — no fixed offered rate; require meaningful sustained load.
        sender_ok = sender_mbps is not None and float(sender_mbps) >= 30.0
    elif req and sender_mbps is not None and float(req) > 0:
        sender_ok = abs(float(sender_mbps) - float(req)) / float(req) <= ATTACK_RATE_TOL
    else:
        sender_ok = False
    if sender_bytes <= 0:
        sender_ok = False
    info["sender_target_ok"] = sender_ok
    if not sender_ok:
        info["failures"].append("sender below requested offered load")

    dur_ratio = activity.get("duration_ratio")
    duration_ok = dur_ratio is not None and float(dur_ratio) >= ATTACK_DURATION_MIN_RATIO
    info["attack_duration_ok"] = duration_ok
    if not duration_ok:
        info["failures"].append("attack duration too short")

    active_ratio = activity.get("receiver_active_window_ratio")
    thirds_ok = activity.get("receiver_thirds_active")
    max_gap = float(activity.get("receiver_max_gap_s") or 0)
    path_ok = (
        recv_bytes >= ATTACK_RECEIVER_MIN_BYTES
        and active_ratio is not None
        and float(active_ratio) >= ATTACK_RECEIVER_ACTIVE_MIN_RATIO
        and bool(thirds_ok)
        and max_gap <= ATTACK_RECEIVER_GAP_MAX_S
    )
    if recv_bytes <= 0:
        info["failures"].append("receiver path absent (zero bytes)")
    elif active_ratio is not None and float(active_ratio) < ATTACK_RECEIVER_ACTIVE_MIN_RATIO:
        info["failures"].append("receiver path not sustained")
    elif not thirds_ok:
        third_bytes = activity.get("receiver_third_bytes") or []
        if len(third_bytes) >= 3 and third_bytes[2] < ATTACK_RECEIVER_THIRD_MIN_BYTES:
            info["failures"].append("receiver traffic absent in final third")
        else:
            info["failures"].append("receiver traffic not distributed across attack window")
    elif max_gap > ATTACK_RECEIVER_GAP_MAX_S:
        info["failures"].append("receiver path gap too large")
    if cbr_profile == CBR_PROFILE_NON_SATURATING:
        if delivery_ratio is None or float(delivery_ratio) < ATTACK_DELIVERY_MIN_RATIO_NON_SAT:
            path_ok = False
            info["failures"].append("delivery below minimum for non_saturating profile")
    info["receiver_path_active"] = path_ok

    ran_ok, ran_info = evaluate_ran_engagement(
        prb,
        expected_attack_duration_s,
        attack_start_s=attack_start_s,
        attack_end_s=attack_end_s,
        prb_start_s=prb_start_s,
        prb_csv=prb_csv,
    )
    info["ran_engagement_ok"] = ran_ok
    info["ran_engagement"] = ran_info
    if not ran_ok:
        info["failures"].append(ran_info.get("reason") or "RAN engagement not observed")

    if info["failures"]:
        info["reason"] = info["failures"][0]
    return info


def evaluate_attack_rate_gates(
    attack: str,
    attack_summary: dict[str, Any],
    receiver_summary: dict[str, Any],
    run_id: str,
    *,
    raw_iperf: dict[str, Any] | None = None,
    expected_attack_duration_s: float = 0,
    cbr_profile: str = CBR_PROFILE_SATURATING,
    prb: dict[str, Any] | None = None,
    rogue_ul_cap_mbps: float | None = None,
) -> tuple[bool, bool, bool]:
    """Legacy tuple API: (sender_ok, path_ok, identity_ok). Prefer evaluate_cbr_attack_checks."""
    if attack == "cbr":
        checks = evaluate_cbr_attack_checks(
            attack_summary,
            raw_iperf,
            run_id,
            expected_attack_duration_s,
            cbr_profile,
            prb or {},
        )
        return (
            checks.get("sender_target_ok", False),
            checks.get("receiver_path_active", False) and checks.get("attack_duration_ok", False)
            and checks.get("ran_engagement_ok", False),
            checks.get("identity_ok", False),
        )
    if attack not in ("microburst", "dual-pulse", "cbr", "multi-rogue"):
        return True, True, True
    if not attack_summary or attack_summary.get("run_id") != run_id:
        return False, False, False
    if attack in ("microburst", "dual-pulse", "multi-rogue") and receiver_summary.get("run_id") != run_id:
        return False, False, False
    if attack == "dual-pulse":
        req = float(attack_summary.get("total_mbps_requested") or 0) / 2.0
        ach = attack_summary.get("achieved_ul_mbps_sender")
    else:
        req = (
            attack_summary.get("requested_aggregate_mbps")
            or attack_summary.get("requested_mean_mbps")
        )
        ach = attack_summary.get("achieved_sender_mbps") or attack_summary.get("achieved_mean_mbps_sender")
    if not req:
        return False, False, True
    if ach is None:
        return False, False, True
    sender_ok = abs(float(ach) - float(req)) / float(req) <= ATTACK_RATE_TOL
    recv_mbps = receiver_summary.get("achieved_receiver_mbps")
    recv_bytes = receiver_summary.get("bytes_received", 0)
    sent_bytes = attack_summary.get("bytes_sent", 0)
    if attack == "multi-rogue":
        # N×mean Mbps aggregate is not deliverable end-to-end on rfsim/UDP sink;
        # integrity = all rogues sent near target AND sink saw sustained path activity.
        # Receiver matching aggregate would false-fail every multi-rogue run.
        per_req = float(
            attack_summary.get("requested_mean_mbps_per_rogue")
            or (float(req) / max(1, int(attack_summary.get("rogue_count") or 1)))
        )
        # A defender-side cap (rate-limit A) makes the offered rate unreachable by
        # design: score the path against the rate the attacker is *allowed* to send.
        if rogue_ul_cap_mbps is not None and float(rogue_ul_cap_mbps) > 0:
            per_req = min(per_req, float(rogue_ul_cap_mbps))
        min_recv_mbps = max(1.0, per_req * 0.25)  # ≥25% of one rogue's offered rate
        receiver_ok = (
            recv_mbps is not None
            and float(recv_mbps) >= min_recv_mbps
            and int(recv_bytes or 0) > 0
        )
        # Identity = every launched rogue reported back; a missing per-rogue
        # summary means the dose N is unknown, not merely under-measured.
        want = int(attack_summary.get("rogue_count_requested") or 0)
        got = int(attack_summary.get("rogue_count") or 0)
        identity_ok = got > 0 and (want == 0 or got == want)
        return sender_ok, receiver_ok, identity_ok
    if recv_mbps is not None:
        receiver_ok = abs(float(recv_mbps) - float(req)) / float(req) <= ATTACK_RATE_TOL
    else:
        receiver_ok = False
    if sent_bytes and recv_bytes < sent_bytes * ATTACK_DELIVERY_MIN_RATIO_NON_SAT:
        receiver_ok = False
    return sender_ok, receiver_ok, True


def collect_attack_artifacts(
    ctx,
    ns,
    run_dir: Path,
    attack: str,
    run_id: str,
    cbr_mbps_per_stream: float | None = None,
    cbr_parallel: int = 1,
    cbr_dir: str = "ul",
    cbr_profile: str = CBR_PROFILE_SATURATING,
    cbr_protocol: str = "udp",
    multi_rogue_count: int = 4,
    multi_rogue_phase_ms: int = 25,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch attack/receiver summaries from ROGUE and ext-DN pods when present."""
    summary: dict[str, Any] = {}
    receiver: dict[str, Any] = {}
    if attack == "cbr":
        iperf_txt = fetch_file(ctx, ns, "oai-ue-rogue", "nr-ue", "/tmp/attack_cbr.json")
        stderr_txt = fetch_file(ctx, ns, "oai-ue-rogue", "nr-ue", "/tmp/attack_cbr.stderr")
        if stderr_txt:
            (run_dir / "attack_cbr.stderr").write_text(stderr_txt, encoding="utf-8")
        if iperf_txt:
            (run_dir / "attack_cbr.json").write_text(iperf_txt, encoding="utf-8")
            try:
                summary = parse_iperf3_attack_summary(
                    json.loads(iperf_txt),
                    run_id,
                    cbr_mbps_per_stream or 0.0,
                    cbr_parallel,
                    cbr_dir,
                    cbr_profile,
                    cbr_protocol,
                )
            except json.JSONDecodeError:
                summary = {"parse_error": True, "run_id": run_id}
            (run_dir / "attack_summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8",
            )
        return summary, receiver
    if attack in DL_FAMILY_ATTACKS:
        summary_name = (
            "/tmp/multi_rogue_dl_sync_summary.json"
            if attack == "multi-rogue-dl-sync"
            else "/tmp/multi_rogue_dl_summary.json"
        )
        txt = fetch_file(ctx, "core", "oai-ext-dn", "ext-dn", summary_name)
        if txt:
            try:
                summary = json.loads(txt)
            except json.JSONDecodeError:
                summary = {"parse_error": True, "run_id": run_id}
            (run_dir / "attack_summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8",
            )
        return summary, receiver
    if attack == "multi-rogue-sync":
        logs = sorted(run_dir.glob("attack.oai-ue-rogue*.live.log"))
        active = sum(
            1 for path in logs
            if sync_c2_rogue_saw_bursts(
                path.read_text(encoding="utf-8", errors="replace")
            )
        )
        summary = {
            "mode": "multi-rogue-sync",
            "run_id": run_id,
            "rogue_count": active,
            "rogue_count_requested": multi_rogue_count,
            "burst_ms": None,
            "log_count": len(logs),
        }
        (run_dir / "attack_summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8",
        )
        return summary, receiver
    if attack not in ("microburst", "dual-pulse", "multi-rogue"):
        return summary, receiver
    if attack == "multi-rogue":
        deploys = rogue_deploy_names(multi_rogue_count)
        phases = multi_rogue_phase_offsets(len(deploys), multi_rogue_phase_ms)
        per_rogue: list[dict[str, Any]] = []
        missing: list[str] = []
        total_bytes = 0
        total_achieved = 0.0
        total_requested = 0.0
        for idx, deploy in enumerate(deploys):
            phase = phases[idx]
            txt, src = fetch_attack_summary(
                ctx, ns, deploy, [f"/tmp/attack_summary_{phase}.json"], run_id,
            )
            if not txt:
                missing.append(deploy)
                continue
            try:
                one = json.loads(txt)
            except json.JSONDecodeError:
                per_rogue.append({"deploy": deploy, "phase_ms": phase, "parse_error": True})
                continue
            per_rogue.append({"deploy": deploy, "phase_ms": phase, "source_path": src, **one})
            total_bytes += int(one.get("bytes_sent") or 0)
            total_achieved += float(one.get("achieved_sender_mbps") or 0.0)
            total_requested += float(one.get("requested_mean_mbps") or 0.0)
            (run_dir / f"attack_summary_{deploy}.json").write_text(txt, encoding="utf-8")
        if per_rogue:
            reqs = [float(p.get("requested_mean_mbps") or 0.0) for p in per_rogue]
            summary = {
                "mode": "multi-rogue",
                "run_id": run_id,
                "rogue_count": len(per_rogue),
                "rogue_count_requested": len(deploys),
                "rogue_summaries_missing": missing,
                "requested_mean_mbps_per_rogue": reqs[0] if reqs else 0.0,
                "requested_aggregate_mbps": round(total_requested, 3),
                "achieved_sender_mbps": round(total_achieved, 3),
                "bytes_sent": total_bytes,
                "per_rogue": per_rogue,
            }
            (run_dir / "attack_summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8",
            )
    else:
        txt, _src = fetch_attack_summary(
            ctx, ns, "oai-ue-rogue",
            ["/tmp/attack_summary.json", "/tmp/attack_summary_0.json"], run_id,
        )
        if txt:
            try:
                summary = json.loads(txt)
            except json.JSONDecodeError:
                summary = {"parse_error": True}
            (run_dir / "attack_summary.json").write_text(txt, encoding="utf-8")
        ts_txt = fetch_file(ctx, ns, "oai-ue-rogue", "nr-ue", "/tmp/attack_timeseries.csv")
        if ts_txt:
            (run_dir / "attack_timeseries.csv").write_text(ts_txt, encoding="utf-8")
    recv_txt = fetch_file(ctx, "core", "oai-ext-dn", "ext-dn", "/tmp/receiver_summary.json")
    if recv_txt:
        try:
            receiver = json.loads(recv_txt)
        except json.JSONDecodeError:
            receiver = {"parse_error": True}
        (run_dir / "receiver_summary.json").write_text(recv_txt, encoding="utf-8")
    recv_ts = fetch_file(ctx, "core", "oai-ext-dn", "ext-dn", "/tmp/receiver_timeseries.csv")
    if recv_ts:
        (run_dir / "receiver_timeseries.csv").write_text(recv_ts, encoding="utf-8")
    if summary and summary.get("run_id") != run_id:
        summary["run_id_mismatch"] = True
    if receiver and receiver.get("run_id") != run_id:
        receiver["run_id_mismatch"] = True
    return summary, receiver


def _popen_log(cmd: list[str], log_path: Path) -> tuple[subprocess.Popen, Any]:
    """Start a long-lived process with stdout/stderr streamed to a file (no pipe deadlock)."""
    logf = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, text=True)
    return proc, logf


def _wait_proc(proc: subprocess.Popen | None, timeout: float | None = None) -> int | None:
    if proc is None:
        return None
    deadline = (time.monotonic() + timeout) if timeout is not None else None
    while True:
        try:
            return proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                proc.kill()
                proc.wait()
                return proc.returncode


def prb_capture_wait_timeout_s(
    prb_started_mono: float | None,
    envelope_s: float,
    grace_s: float = PRB_CAPTURE_GRACE_S,
) -> float:
    """Remaining wait for PRB capture from *now* (not a fixed envelope+60 from mission end)."""
    if prb_started_mono is None:
        return envelope_s + grace_s
    deadline = prb_started_mono + float(envelope_s) + grace_s
    return max(5.0, deadline - time.monotonic())


def _wait_proc_no_kill(
    proc: subprocess.Popen | None,
    timeout: float | None = None,
) -> tuple[int | None, str]:
    """Wait without killing on timeout — returns (returncode, status)."""
    if proc is None:
        return None, "no_proc"
    deadline = (time.monotonic() + timeout) if timeout is not None else None
    while True:
        try:
            rc = proc.wait(timeout=1.0)
            return rc, "exited"
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                return None, "timeout"


def _prepare_proxy_run_dir(ctx: str, ns: str, pod_proxy_out: str) -> None:
    kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "sh", "-c",
        f"mkdir -p {pod_proxy_out} && rm -f {pod_proxy_out}/STOP {pod_proxy_out}/proxy.pid "
        f"{pod_proxy_out}/proxy_summary.json {pod_proxy_out}/proxy_forward.csv "
        f"{pod_proxy_out}/proxy_reverse.csv {pod_proxy_out}/proxy_link.csv",
    )


def _fetch_proxy_artifacts(ctx: str, ns: str, pod_proxy_out: str, run_dir: Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for extra in ("proxy_forward.csv", "proxy_reverse.csv", "proxy_link.csv", "proxy_summary.json"):
        txt = fetch_file(ctx, ns, "oai-ue-uav", "nr-ue", f"{pod_proxy_out}/{extra}")
        if txt:
            (run_dir / extra).write_text(txt, encoding="utf-8")
            info[extra] = True
        else:
            info[extra] = False
    return info


def shutdown_uav_proxy(
    ctx: str,
    ns: str,
    pod_proxy_out: str,
    proxy_proc: subprocess.Popen | None,
    run_dir: Path,
) -> tuple[int | None, dict[str, Any]]:
    """Graceful proxy shutdown: drain, stop-file, wait, SIGTERM/SIGKILL fallback."""
    info: dict[str, Any] = {
        "stop_file": f"{pod_proxy_out}/STOP",
        "graceful_shutdown": False,
        "shutdown_path": [],
    }
    if proxy_proc is None:
        info["reason"] = "no_proxy_proc"
        return None, info

    stop_file = f"{pod_proxy_out}/STOP"
    time.sleep(PROXY_DRAIN_S)
    info["shutdown_path"].append("drain_slept")
    touch = kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "touch", stop_file,
    )
    if touch.returncode != 0:
        info["stop_file_error"] = (touch.stderr or touch.stdout or "").strip()
    else:
        info["shutdown_path"].append("stop_file_created")

    rc, status = _wait_proc_no_kill(proxy_proc, PROXY_SHUTDOWN_TIMEOUT_S)
    info["first_wait_status"] = status
    info["first_wait_rc"] = rc

    if status == "exited" and rc == 0:
        info["graceful_shutdown"] = True
        info["shutdown_path"].append("exited_after_stop_file")
    elif status == "timeout":
        pid_txt = fetch_file(ctx, ns, "oai-ue-uav", "nr-ue", f"{pod_proxy_out}/proxy.pid")
        pid = (pid_txt or "").strip()
        info["proxy_pid"] = pid or None
        if pid.isdigit():
            kctl(
                ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
                "kill", "-TERM", pid,
            )
            info["shutdown_path"].append("sigterm_in_pod")
        rc, status = _wait_proc_no_kill(proxy_proc, PROXY_SIGTERM_GRACE_S)
        info["sigterm_wait_status"] = status
        if status == "exited":
            info["graceful_shutdown"] = bool(rc == 0)
            info["shutdown_path"].append("exited_after_sigterm")
        elif status == "timeout":
            if pid.isdigit():
                kctl(
                    ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
                    "kill", "-KILL", pid,
                )
                info["shutdown_path"].append("sigkill_in_pod")
            rc, status = _wait_proc_no_kill(proxy_proc, 5.0)
            info["graceful_shutdown"] = False
            info["shutdown_path"].append("killed")

    fetch_info = _fetch_proxy_artifacts(ctx, ns, pod_proxy_out, run_dir)
    info["artifacts"] = fetch_info
    if proxy_proc.poll() is None:
        proxy_proc.kill()
        proxy_proc.wait()
        info["shutdown_path"].append("local_kubectl_killed")
    return rc if rc is not None else proxy_proc.returncode, info


def check_uav_mavlink_ports(ctx: str, ns: str, c2_proxy: bool) -> dict[str, str]:
    """Post-run port sanity: 5760 free, 5761 SITL up when proxy mode."""
    out: dict[str, str] = {}
    for port, key in ((SITL_PORT, "proxy_port_5760"), (SITL_BACKEND_PORT, "sitl_port_5761")):
        r = kctl(
            ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
            "sh", "-c", f"ss -ltn 2>/dev/null | grep -q ':{port} ' && echo busy || echo free",
        )
        out[key] = (r.stdout or "").strip() or "unknown"
    if c2_proxy:
        out["proxy_port_ok"] = "yes" if out.get("proxy_port_5760") == "free" else "no"
        out["sitl_port_ok"] = "yes" if out.get("sitl_port_5761") == "busy" else "no"
    return out


def start_co_tenant_sink(
    ctx, run_id: str, duration_s: float, log_path: Path,
) -> tuple[subprocess.Popen | None, Any]:
    """Start UDP sink on ext-DN for co-tenant load validation."""
    prep = (
        "pkill -f '[c]o_tenant_sink.py' 2>/dev/null || true; "
        "rm -f /tmp/receiver_summary.json /tmp/receiver_timeseries.csv; "
    )
    kctl(ctx, "exec", "-n", "core", "deploy/oai-ext-dn", "-c", "ext-dn", "--", "sh", "-c", prep)
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", "core", "deploy/oai-ext-dn", "-c", "ext-dn", "--",
        "python3", "-u", "/tools/co_tenant_sink.py",
        "--listen", "0.0.0.0", "--port", str(CO_TENANT_PORT),
        "--duration", str(int(duration_s) + 5),
        "--run-id", run_id,
        "--out-summary", "/tmp/receiver_summary.json",
        "--out-timeseries", "/tmp/receiver_timeseries.csv",
    ]
    return _popen_log(cmd, log_path)


def ensure_companion_video_server(ctx: str, ns: str, shape: str = "iperf") -> None:
    """GCS :5202 sink for UAV→GCS companion video (BVLOS camera).

    iperf shape: iperf3 server. aviator shape: a plain UDP drain (the sender is not
    iperf3-protocol), so the DL packets are consumed at the GCS and still cross the
    radio (counted by the gNB MAC for role identity / GCS DL).

    Use killall (process name), not ``pkill -f iperf3``: the latter matches this
    shell's ``-c`` argv (which contains ``iperf3 -s``) and SIGTERMs itself → rc 143.
    """
    port = CO_TENANT_PORT
    if shape == "aviator":
        # UDP drain on :5202 for the aviator companion sender (not iperf3-protocol). Run it as a
        # HOST-side background process (like the QGC mirror): kubectl exec stays attached while the
        # in-pod drain runs, which sidesteps in-pod daemonize fragility (kubectl exec waits on the
        # command's stdio, so a plain `&`/setsid/double-fork left the exec hanging → kctl → rc=143).
        kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--", "sh", "-c",
             "killall -q iperf3 2>/dev/null || true; pkill -f companion_drain 2>/dev/null || true; "
             "rm -f /tmp/companion_video.json /tmp/companion_drain.ready")
        drain = (  # "companion_drain" appears in argv so `pkill -f companion_drain` finds it
            "import socket  # companion_drain\n"
            "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1<<20)\n"
            f"s.bind(('0.0.0.0',{port}))\n"
            "open('/tmp/companion_drain.ready','w').close()\n"
            "while True:\n try: s.recvfrom(65535)\n except Exception: break\n"
        )
        proc = subprocess.Popen(
            ["kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue",
             "--", "python3", "-c", drain],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        CURRENT_RUN["companion_drain_proc"] = proc
        for _ in range(40):
            r = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--", "sh", "-c",
                     "[ -f /tmp/companion_drain.ready ] && echo ok")
            if "ok" in (r.stdout or ""):
                return
            time.sleep(0.1)
        raise RuntimeError(f"companion-video: aviator UDP drain not ready on GCS :{port}")

    script = (
        f"killall -q iperf3 2>/dev/null || true; "
        f"sleep 0.2; "
        f"rm -f /tmp/companion_video.json; "
        f"iperf3 -s -D -p {port}; "
        f"sleep 0.2; "
        f"ss -ltn | grep -q ':{port} ' && echo ok"
    )
    r = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
             "sh", "-c", script)
    if "ok" not in (r.stdout or ""):
        raise RuntimeError(
            f"companion-video: failed to start iperf sink on GCS :{port} "
            f"(rc={r.returncode} out={(r.stdout or '').strip()!r})"
        )


def stop_companion_video_server(ctx: str, ns: str) -> None:
    """Tear down the GCS :5202 sink and the UAV sender left over from a run.

    Without this the ``iperf3 -s -D`` daemon survives the run and keeps a UDP
    socket (and a scheduling share) alive into the next one; only the *next*
    soft_reset would clear it, so the last run of a campaign always leaked one.
    """
    proc = CURRENT_RUN.pop("companion_drain_proc", None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    kill = ("killall -q iperf3 2>/dev/null || true; "
            "pkill -f companion_drain 2>/dev/null || true; "
            "pkill -f aviator_video_send 2>/dev/null || true; "
            "rm -f /tmp/companion_drain.ready 2>/dev/null || true")
    for deploy in ("oai-ue-gcs", "oai-ue-uav"):
        kctl(ctx, "exec", "-n", ns, f"deploy/{deploy}", "-c", "nr-ue", "--", "sh", "-c", kill)


def companion_video_command(
    ctx: str,
    ns: str,
    mbps: float,
    duration_s: float,
    run_id: str,
    gcs_ip: str,
    uav_ip: str | None = None,
    shape: str = "iperf",
) -> list[str]:
    """UAV → GCS UDP companion stream; caller selects the PDU session.

    Radio: UL at UAV + DL at GCS (competes with telemetry; NOT with setpoint DL at the UAV).
    shape=iperf: iperf3 CBR. shape=aviator: realistic UAV packet-size distribution at the
    same mean (aviator_video_send.py), writing an iperf3-compatible companion_video.json.
    """
    dur = max(1, int(duration_s))
    gcs = (gcs_ip or "").strip()
    if not gcs:
        raise RuntimeError("companion-video: GCS PDU IP missing")
    bind_expr = shlex.quote((uav_ip or "").strip()) if uav_ip else '"$(tm_pdu_ip)"'
    if shape == "aviator":
        gen = (
            'python3 "%(tools)s/aviator_video_send.py" "%(gcs)s" %(port)s %(mbps)s %(dur)s "$UAV" '
            "/tmp/companion_video.json"
        ) % {"tools": C2_RUN_TOOLS, "gcs": gcs, "port": CO_TENANT_PORT, "mbps": f"{mbps:g}", "dur": dur}
    else:
        gen = (
            'iperf3 -c "%(gcs)s" -p %(port)s -u -b %(mbps)sM -t %(dur)s -B "$UAV" -J '
            "> /tmp/companion_video.json"
        ) % {"gcs": gcs, "port": CO_TENANT_PORT, "mbps": f"{mbps:g}", "dur": dur}
    script = (
        "set -euo pipefail; . /tmp/tm_common.sh; "
        f"UAV={bind_expr}; "
        'if [ -z "$UAV" ]; then echo "[COMPANION-VIDEO] ERROR: UAV PDU IP missing"; exit 1; fi; '
        'echo "[COMPANION-VIDEO] run=%(rid)s shape=%(shape)s UAV=$UAV -> GCS=%(gcs)s:%(port)s UDP %(mbps)sM for %(dur)ss (uav→gcs)"; '
        "rm -f /tmp/companion_video.json; "
        f"{gen}; "
        'echo "[COMPANION-VIDEO] done rc=$? bytes=$(wc -c </tmp/companion_video.json 2>/dev/null || echo 0)"'
    ) % {
        "rid": run_id,
        "shape": shape,
        "gcs": gcs,
        "port": CO_TENANT_PORT,
        "mbps": f"{mbps:g}",
        "dur": dur,
    }
    return [
        "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "bash", "-o", "pipefail", "-c", script,
    ]


def parse_companion_video_summary(raw: str, *, offered_mbps: float) -> dict[str, Any]:
    """Best-effort parse of iperf3 -J JSON from companion video (victim demand)."""
    out: dict[str, Any] = {
        "offered_mbps": offered_mbps,
        "port": CO_TENANT_PORT,
        "direction": "uav_to_gcs",
        "path": "uav_ul_gcs_dl",
        "protocol": "udp",
        "ok": False,
    }
    if not raw or not raw.strip():
        out["error"] = "empty companion_video.json"
        return out
    try:
        j = json.loads(raw)
    except json.JSONDecodeError as exc:
        out["error"] = f"json decode: {exc}"
        return out
    end = j.get("end") or {}
    sum_b = end.get("sum") or end.get("sum_received") or end.get("sum_sent") or {}
    bps = sum_b.get("bits_per_second")
    if bps is not None:
        out["achieved_mbps"] = round(float(bps) / 1e6, 3)
    lost = sum_b.get("lost_percent")
    if lost is not None:
        out["lost_percent"] = float(lost)
    out["ok"] = bps is not None and float(bps) > 0
    out["iperf_end_keys"] = sorted(end.keys())
    return out


def _terminate_layer_procs(*procs: subprocess.Popen | None) -> None:
    for proc in procs:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def evaluate_c2_health(c2: dict[str, Any], is_baseline: bool) -> tuple[bool, dict[str, Any]]:
    cm = c2.get("c2_metrics") or {}
    phys = c2.get("physics_metrics") or {}
    landing = c2.get("landing") or {}
    fs = cm.get("failsafe") or {}
    hb_gaps = cm.get("heartbeat_gaps_over_threshold")
    pos_samples = phys.get("position_samples")
    mode_trans = dget(phys, "flight_state", "mode_transitions") or []
    unexpected = [
        m for m in mode_trans
        if m.get("to") in ("RTL", "SMART_RTL", "AUTO_RTL")
        or (m.get("to") == "LAND" and not landing.get("requested"))
    ]
    normal_land = landing.get("normal")
    if normal_land is None:
        normal_land = bool(landing.get("requested")) and not fs.get("triggered")
    info = {
        "heartbeat_gaps_over_threshold": hb_gaps,
        "unexpected_failsafe": bool(fs.get("triggered")),
        "unexpected_mode_transitions": unexpected,
        "position_samples": pos_samples,
        "normal_land": normal_land,
        "min_position_samples": C2_MIN_POSITION_SAMPLES,
    }
    if not is_baseline:
        return True, info
    ok = (
        hb_gaps == 0
        and not fs.get("triggered")
        and len(unexpected) == 0
        and pos_samples is not None
        and pos_samples >= C2_MIN_POSITION_SAMPLES
        and normal_land
    )
    return ok, info


def _mission_artifact_names(workload: str) -> list[str]:
    if workload == "setpoint-stream":
        return ["setpoints.csv", "freshness_summary.json"]
    return ["commands.csv"]


def _count_setpoint_track_samples(run_dir: Path) -> int:
    sp_path = run_dir / "setpoints.csv"
    if not sp_path.is_file():
        return 0
    import csv as _csv

    n = 0
    with sp_path.open(encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if row.get("cross_track_error_m") not in (None, "", "None"):
                try:
                    if math.isfinite(float(row["cross_track_error_m"])):
                        n += 1
                except (TypeError, ValueError):
                    pass
    return n


def _mode_transition_seen(mode_trans: list[dict], frm: str, to: str) -> bool:
    return any(m.get("from") == frm and m.get("to") == to for m in mode_trans)


def evaluate_mission_integrity(
    run_dir: Path,
    mission_proc,
    mission_rc: int | None,
    c2: dict[str, Any],
    mission_log: str,
    workload: str,
) -> tuple[bool, dict[str, Any]]:
    """Instrumentation trust — independent of flight success (attack may fail mission)."""
    summary_valid = bool(c2) and bool(c2.get("workload") or c2.get("schema_version"))
    required = _mission_artifact_names(workload)
    missing = [name for name in required if not (run_dir / name).is_file()]
    started = mission_proc is not None
    finished = mission_rc is not None
    silent_crash = started and not finished
    info: dict[str, Any] = {
        "mission_started": started,
        "mission_rc": mission_rc,
        "summary_valid": summary_valid,
        "artifacts_required": required,
        "artifacts_missing": missing,
        "silent_crash": silent_crash,
    }
    ok = started and finished and summary_valid and not missing and not silent_crash
    if not ok:
        if silent_crash:
            info["reason"] = "mission_runner did not return (timeout/kill?)"
        elif missing:
            info["reason"] = "missing artifacts: %s" % ", ".join(missing)
        elif not summary_valid:
            info["reason"] = "mission_summary.json missing or invalid"
        elif not started:
            info["reason"] = "mission process never started"
        else:
            info["reason"] = "mission integrity check failed"
    elif mission_rc != 0:
        info["mission_failed"] = True
    return ok, info


def evaluate_mission_success(
    mission_rc: int | None,
    mission_log: str = "",
) -> tuple[bool, dict[str, Any]]:
    ok = mission_rc == 0
    reason = None if ok else meaningful_error(mission_log, "mission_rc=%s" % mission_rc)
    return ok, {"mission_rc": mission_rc, "reason": reason}


def evaluate_drone_integrity(
    run_dir: Path,
    c2: dict[str, Any],
    physics: dict[str, Any],
    workload: str,
) -> tuple[bool, dict[str, Any]]:
    """Physical data trustworthy — no acquisition/flight-quality requirements."""
    phys_m = c2.get("physics_metrics") or {}
    cm = c2.get("c2_metrics") or {}
    pos_samples = phys_m.get("position_samples")
    track_samples = _count_setpoint_track_samples(run_dir)
    fs_path = run_dir / "flight_state.csv"
    sp_path = run_dir / "setpoints.csv"
    has_telemetry = (
        fs_path.is_file()
        or (sp_path.is_file() and track_samples >= C2_MIN_SETPOINT_TRACK_SAMPLES)
        or (pos_samples is not None and pos_samples >= C2_MIN_POSITION_SAMPLES)
    )
    bootstrap_ok = phys_m.get("bootstrap_land_then_guided")
    if bootstrap_ok is None:
        bootstrap_ok = True  # legacy summaries
    physics_complete = phys_m.get("physics_metrics_complete")
    if physics_complete is None:
        physics_complete = True
    hb_seen = cm.get("heartbeats_seen")
    hb_ok = hb_seen is None or hb_seen >= 1
    steady_xte = dget(phys_m, "steady_state_window", "cross_track_error_m", "p99")
    all_xte = dget(phys_m, "flight_state", "cross_track_error_m", "p99")
    metrics_finite = all(
        x is None or (isinstance(x, (int, float)) and math.isfinite(float(x)))
        for x in (steady_xte, all_xte)
    )
    info: dict[str, Any] = {
        "position_samples": pos_samples,
        "setpoint_track_samples": track_samples,
        "flight_state_csv": fs_path.is_file(),
        "setpoints_csv": sp_path.is_file(),
        "heartbeats_seen": hb_seen,
        "max_heartbeat_gap_s": cm.get("max_heartbeat_gap_s"),
        "bootstrap_land_then_guided": bootstrap_ok,
        "physics_metrics_complete": physics_complete,
    }
    ok = (
        bool(c2)
        and has_telemetry
        and hb_ok
        and metrics_finite
        and bootstrap_ok is not False
        and physics_complete is not False
    )
    if not ok:
        if not has_telemetry:
            info["reason"] = "insufficient physical telemetry samples"
        elif not metrics_finite:
            info["reason"] = "non-finite cross-track metrics"
        elif bootstrap_ok is False or physics_complete is False:
            info["reason"] = "physics instrumentation incomplete (bootstrap/mode logger)"
        elif not hb_ok:
            info["reason"] = "no heartbeats observed in measurement window"
        else:
            info["reason"] = "mission_summary/physics invalid"
    return ok, info


def evaluate_baseline_drone_quality(
    mission_rc: int | None,
    c2: dict[str, Any],
    physics: dict[str, Any],
    *,
    workload: str,
    cross_track_max_p99_m: float,
    deviation_max_m: float,
) -> tuple[bool, dict[str, Any]]:
    """Normal-flight requirements — baseline only; never enforced under attack."""
    cm = c2.get("c2_metrics") or {}
    phys_m = c2.get("physics_metrics") or {}
    landing = c2.get("landing") or {}
    fs = cm.get("failsafe") or {}
    hb_gaps = cm.get("heartbeat_gaps_over_threshold")
    cmds_lost = cm.get("commands_lost")
    acq_completed = dget(phys_m, "trajectory_acquisition", "completed")
    xtrack_steady = dget(phys_m, "steady_state_window", "cross_track_error_m", "p99")
    xtrack_all = dget(phys_m, "flight_state", "cross_track_error_m", "p99")
    max_dev = phys_m.get("max_deviation_m")
    mode_integrity = dget(phys_m, "flight_state", "mode_transition_integrity")
    mode_trans = dget(phys_m, "flight_state", "mode_transitions") or []
    land_guided = _mode_transition_seen(mode_trans, "LAND", "GUIDED")
    guided_land = _mode_transition_seen(mode_trans, "GUIDED", "LAND")
    normal_land = landing.get("normal")
    if normal_land is None:
        normal_land = bool(landing.get("requested")) and not fs.get("triggered")
    xtrack_gate = xtrack_steady if workload == "setpoint-stream" else xtrack_all
    if workload == "setpoint-stream":
        spatial_ok = (
            xtrack_steady is not None
            and float(xtrack_steady) <= cross_track_max_p99_m
            and acq_completed is True
        )
    else:
        spatial_ok = max_dev is not None and float(max_dev) <= deviation_max_m
        if xtrack_all is not None:
            spatial_ok = spatial_ok and float(xtrack_all) <= cross_track_max_p99_m
    ok = (
        mission_rc == 0
        and hb_gaps == 0
        and cmds_lost == 0
        and not fs.get("triggered")
        and spatial_ok
        and mode_integrity is not False
        and land_guided
        and guided_land
        and normal_land
    )
    info: dict[str, Any] = {
        "mission_rc": mission_rc,
        "heartbeat_gaps_over_threshold": hb_gaps,
        "commands_lost": cmds_lost,
        "failsafe": fs.get("triggered"),
        "trajectory_acquisition_completed": acq_completed,
        "cross_track_p99_steady_m": xtrack_steady,
        "cross_track_p99_m": xtrack_all,
        "cross_track_gate_m": xtrack_gate,
        "max_deviation_m": max_dev,
        "mode_transition_integrity": mode_integrity,
        "land_to_guided": land_guided,
        "guided_to_land": guided_land,
        "normal_land": normal_land,
    }
    if not ok:
        reasons = []
        if mission_rc != 0:
            reasons.append("mission_rc!=0")
        if hb_gaps:
            reasons.append("heartbeat_gaps")
        if cmds_lost:
            reasons.append("commands_lost")
        if fs.get("triggered"):
            reasons.append("failsafe")
        if not spatial_ok:
            reasons.append("spatial/acquisition")
        if mode_integrity is False:
            reasons.append("mode_transition_integrity")
        if not land_guided:
            reasons.append("missing LAND->GUIDED")
        if not guided_land:
            reasons.append("missing GUIDED->LAND")
        if not normal_land:
            reasons.append("abnormal_land")
        info["reason"] = "; ".join(reasons) or "baseline drone quality failed"
    return ok, info


def resolve_final_armed(
    fstate: dict[str, Any],
    landing: dict[str, Any],
    final_mode: str | None,
) -> bool | None:
    """Last HEARTBEAT armed flag, with conservative inference after normal LAND."""
    for key in ("final_armed", "armed"):
        val = fstate.get(key)
        if val is not None:
            return bool(val)
    if landing.get("normal") and final_mode == "LAND":
        return False
    return None


def build_drone_outcome(
    mission_rc: int | None,
    c2: dict[str, Any],
    physics: dict[str, Any],
) -> dict[str, Any]:
    """Recorded outcome — never a validity gate under attack."""
    phys_m = c2.get("physics_metrics") or physics
    cm = c2.get("c2_metrics") or {}
    landing = c2.get("landing") or {}
    fs = cm.get("failsafe") or {}
    acq = phys_m.get("trajectory_acquisition") or {}
    steady = phys_m.get("steady_state_window") or {}
    fstate = phys_m.get("flight_state") or {}
    final_mode = fstate.get("final_mode")
    if final_mode is None and fstate.get("mode_transitions"):
        final_mode = fstate["mode_transitions"][-1].get("to")
    return {
        "mission_success": mission_rc == 0,
        "mission_rc": mission_rc,
        "acquisition_completed": acq.get("completed"),
        "acquisition_time_s": acq.get("duration_s"),
        "acquisition_cross_track_max_m": acq.get("cross_track_max_m"),
        "paper_comparable": acq.get("paper_comparable"),
        "paper_comparable_reasons": acq.get("paper_comparable_reasons"),
        "stream_start_radius_m": acq.get("stream_start_radius_m"),
        "cross_track_p99_steady_m": dget(steady, "cross_track_error_m", "p99"),
        "cross_track_p99_m": dget(fstate, "cross_track_error_m", "p99"),
        "tracking_error_p99_m": dget(fstate, "tracking_error_m", "p99"),
        "failsafe": bool(fs.get("triggered")),
        "failsafe_mode": fs.get("mode"),
        "landing_requested": landing.get("requested"),
        "landing_normal": landing.get("normal"),
        "final_mode": final_mode,
        "final_armed": resolve_final_armed(fstate, landing, final_mode),
        "max_deviation_m": phys_m.get("max_deviation_m"),
        "landing_completed": landing.get("normal"),
        "mode_transitions": fstate.get("mode_transitions"),
    }


def build_attack_outcomes(
    attack: str,
    attack_summary: dict[str, Any],
    cbr_checks: dict[str, Any] | None,
) -> dict[str, Any]:
    """Experimental attack outcomes — never enforced as integrity gates (saturating profile)."""
    if attack != "cbr" or not attack_summary:
        return {}
    checks = cbr_checks or {}
    return {
        "attack": attack,
        "cbr_dir": attack_summary.get("cbr_dir"),
        "cbr_profile": attack_summary.get("cbr_profile") or checks.get("cbr_profile"),
        "offered_mbps": attack_summary.get("offered_mbps") or attack_summary.get("requested_aggregate_mbps"),
        "sender_mbps": attack_summary.get("achieved_sender_mbps"),
        "delivered_mbps": attack_summary.get("delivered_mbps") or attack_summary.get("achieved_receiver_mbps"),
        "delivery_ratio": attack_summary.get("delivery_ratio") or checks.get("delivery_ratio"),
        "undelivered_ratio": attack_summary.get("undelivered_ratio") or checks.get("undelivered_ratio"),
        "udp_packet_loss_ratio": attack_summary.get("udp_packet_loss_ratio")
        or checks.get("udp_packet_loss_ratio"),
        "duration_ratio": checks.get("duration_ratio") or attack_summary.get("duration_ratio"),
        "receiver_active_window_ratio": checks.get("receiver_active_window_ratio")
        or attack_summary.get("receiver_active_window_ratio"),
        "ran_engagement": checks.get("ran_engagement"),
    }


def enforced_validity_gates(args: Any, *, is_baseline: bool) -> list[str]:
    """Return only gates that may invalidate this run."""
    enforced = [
        "clock",
        "host_instrumentation_integrity",
        "mission_integrity",
        "drone_integrity",
    ]
    if is_baseline:
        enforced.insert(1, "host")
    if args.c2_proxy:
        enforced.append("proxy")
    if args.workload == "setpoint-stream":
        enforced += ["freshness", "stream"]
        if is_baseline:
            enforced.append("c2_health")
    if args.require_prb:
        enforced.append("prb")
    if (
        args.mitigation_rate_limit_a
        or args.mitigation_aqm_ul
        or args.mitigation_ul_rrmpolicy
    ):
        enforced.append("mitigation_integrity")
    if is_baseline:
        enforced += ["availability", "mission_success", "baseline_drone_quality"]
    else:
        enforced.append("attack_integrity")
    return enforced


def evaluate_attack_integrity(
    attack: str,
    attack_proc,
    attack_rc: int | None,
    sink_rc: int | None,
    attack_summary: dict[str, Any],
    receiver_summary: dict[str, Any],
    run_id: str,
    attack_log: str,
    *,
    attack_procs: list | None = None,
    raw_iperf: dict[str, Any] | None = None,
    attack_stderr: str = "",
    expected_attack_duration_s: float = 0,
    cbr_profile: str = CBR_PROFILE_SATURATING,
    prb: dict[str, Any] | None = None,
    attack_start_s: float | None = None,
    attack_end_s: float | None = None,
    prb_start_s: float = 0.0,
    prb_csv: Path | None = None,
    triggers_fired: int | None = None,
    rogue_ul_cap_mbps: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Attack instrumentation — generator/path/RAN engagement; not physical outcome."""
    started = attack_proc is not None or bool(attack_procs)
    finished = attack_rc is not None
    sink_ok = (sink_rc == 0) if attack in ("microburst", "dual-pulse", "multi-rogue") else True

    if attack in DL_FAMILY_ATTACKS:
        targets = int(attack_summary.get("target_count") or 0)
        target_list = attack_summary.get("targets") or []
        identity_ok = (
            targets > 0
            and targets == len(target_list)
            and all(ip_in_cidr(str(ip), SUBNET_SLICE_A_CIDR) for ip in target_list)
            and not any(ip_in_cidr(str(ip), SUBNET_SLICE_B_CIDR) for ip in target_list)
        )
        run_id_ok = attack_summary.get("run_id") == run_id
        if attack == "multi-rogue-dl-sync":
            open_loop = bool(attack_summary.get("open_loop")) or attack_log_is_open_loop(
                attack_log
            )
            fired = int(triggers_fired or 0)
            triggers_ok = True if open_loop else (fired > 0)
            bursts_ok = int(attack_summary.get("bursts_completed") or 0) > 0
            if not bursts_ok:
                bursts_ok = "[EXT-DN-SYNC] burst #" in (attack_log or "")
            sender_ok = bursts_ok
            ok = (
                started and finished and attack_rc == 0
                and identity_ok and triggers_ok and sender_ok and run_id_ok
            )
            info = {
                "attack": attack,
                "attack_started": started,
                "attack_rc": attack_rc,
                "open_loop": open_loop,
                "triggers_fired": fired,
                "triggers_gate_pass": triggers_ok,
                "identity_gate_pass": identity_ok,
                "rate_gate_sender_pass": sender_ok,
                "run_id_match": run_id_ok,
                "threat_model": "pure_dl_prb_contention_sync",
                "summary": attack_summary or None,
            }
            if not ok:
                info["reason"] = (
                    "multi-rogue-dl-sync process did not complete"
                    if not started or not finished else
                    meaningful_error(attack_log, f"attack_rc={attack_rc}")
                    if attack_rc != 0 else
                    "multi-rogue-dl-sync trigger/burst/identity gate failed"
                )
            return ok, info

        requested = float(attack_summary.get("requested_aggregate_mbps") or 0.0)
        achieved = float(attack_summary.get("achieved_sender_mbps") or 0.0)
        sender_ok = requested > 0 and achieved >= requested * 0.80
        ok = (
            started and finished and attack_rc == 0
            and identity_ok and sender_ok and run_id_ok
        )
        info = {
            "attack": attack,
            "attack_started": started,
            "attack_rc": attack_rc,
            "identity_gate_pass": identity_ok,
            "rate_gate_sender_pass": sender_ok,
            "run_id_match": run_id_ok,
            "threat_model": "pure_dl_prb_contention",
            "summary": attack_summary or None,
        }
        if not ok:
            info["reason"] = (
                "multi-rogue-dl process did not complete"
                if not started or not finished else
                meaningful_error(attack_log, f"attack_rc={attack_rc}")
                if attack_rc != 0 else
                "multi-rogue-dl target identity/rate/run_id gate failed"
            )
        return ok, info

    if attack in SYNC_STYLE_ATTACKS and attack != "multi-rogue-dl-sync":
        # Evidence = GCS fired the trigger AND the ROGUE emitted bursts. Just
        # triggers_fired (UDP one-way) is not enough: the flood may have died
        # at start (pkill/SIGTERM) while GCS keeps sending into the void.
        # Open-loop: no oracle — ROGUE bursts only.
        open_loop = attack_log_is_open_loop(attack_log)
        fired = int(triggers_fired or 0)
        triggers_ok = True if open_loop else (fired > 0)
        bursts_ok = sync_c2_rogue_saw_bursts(attack_log)
        identity_ok = True
        if attack == "multi-rogue-sync":
            identity_ok = (
                int(attack_summary.get("rogue_count") or 0) > 0
                and int(attack_summary.get("rogue_count") or 0)
                == int(attack_summary.get("rogue_count_requested") or 0)
            )
            bursts_ok = bursts_ok and identity_ok
        ok = started and finished and attack_rc == 0 and triggers_ok and bursts_ok
        info: dict[str, Any] = {
            "attack": attack,
            "attack_started": started,
            "attack_rc": attack_rc,
            "open_loop": open_loop,
            "triggers_fired": fired,
            "triggers_gate_pass": triggers_ok,
            "rogue_bursts_ok": bursts_ok,
            "identity_gate_pass": identity_ok,
            "threat_model": (
                "pure_ul_prb_contention_sync"
                if attack in ("sync-prb", "multi-rogue-sync")
                else "path_endpoint"
            ),
            "summary": attack_summary or None,
        }
        if not ok:
            if not started or not finished:
                info["reason"] = "attack process did not complete"
            elif attack_rc not in (0, None):
                info["reason"] = meaningful_error(attack_log, "attack_rc=%s" % attack_rc)
            elif not open_loop and not triggers_ok:
                info["reason"] = (
                    f"{attack} trigger never fired (triggers_fired=0) - null attack"
                )
            elif not bursts_ok:
                info["reason"] = f"{attack}: triggers ok but ROGUE emitted no bursts"
            else:
                info["reason"] = "attack integrity failed"
        return ok, info

    if attack == "cbr":
        checks = evaluate_cbr_attack_checks(
            attack_summary,
            raw_iperf,
            run_id,
            expected_attack_duration_s,
            cbr_profile,
            prb or {},
            attack_start_s=attack_start_s,
            attack_end_s=attack_end_s,
            prb_start_s=prb_start_s,
            prb_csv=prb_csv,
        )
        iperf_fail = iperf_client_failure_hint(attack_stderr, attack_log)
        if iperf_fail:
            checks["failures"].insert(0, f"iperf client failed ({iperf_fail})")
            checks["receiver_path_active"] = False
            checks["attack_duration_ok"] = False
            checks["reason"] = checks["failures"][0]
        integrity_ok = all(
            checks.get(k)
            for k in (
                "identity_ok",
                "sender_target_ok",
                "attack_duration_ok",
                "receiver_path_active",
                "ran_engagement_ok",
            )
        )
        ok = started and finished and attack_rc == 0 and integrity_ok and sink_ok
        info: dict[str, Any] = {
            "attack": attack,
            "attack_started": started,
            "attack_rc": attack_rc,
            "sink_rc": sink_rc,
            "sink_gate_pass": sink_ok,
            "summary": attack_summary or None,
            "receiver_summary": receiver_summary or None,
            **checks,
            "rate_gate_sender_pass": checks.get("sender_target_ok"),
            "rate_gate_receiver_pass": checks.get("receiver_path_active"),
            "identity_gate_pass": checks.get("identity_ok"),
        }
        if not ok and not info.get("reason"):
            if iperf_fail:
                info["reason"] = checks["reason"]
            elif not started or not finished:
                info["reason"] = "attack process did not complete"
            elif attack_rc not in (0, None):
                hint = iperf_client_failure_hint(attack_stderr, attack_log)
                info["reason"] = meaningful_error(
                    attack_log,
                    attack_stderr or ("attack_rc=%s" % attack_rc),
                )
                if hint:
                    info["reason"] = f"iperf client failed ({hint})"
            elif not sink_ok:
                info["reason"] = "co-tenant sink failed"
            elif checks.get("failures"):
                info["reason"] = checks["failures"][0]
            else:
                info["reason"] = "attack integrity failed"
        return ok, info

    rate_ok, recv_ok, identity_ok = evaluate_attack_rate_gates(
        attack, attack_summary, receiver_summary, run_id,
        rogue_ul_cap_mbps=rogue_ul_cap_mbps,
    )
    run_id_ok = (
        not attack_summary
        or attack_summary.get("run_id") == run_id
        or attack not in ("microburst", "cbr", "multi-rogue")
    )
    if attack in ("microburst", "cbr", "multi-rogue") and not attack_summary:
        rate_ok = recv_ok = identity_ok = False
    ok = (
        started
        and finished
        and attack_rc == 0
        and rate_ok
        and recv_ok
        and identity_ok
        and sink_ok
        and run_id_ok
    )
    info = {
        "attack": attack,
        "attack_started": started,
        "attack_rc": attack_rc,
        "sink_rc": sink_rc,
        "rate_gate_sender_pass": rate_ok,
        "rate_gate_receiver_pass": recv_ok,
        "identity_gate_pass": identity_ok,
        "sink_gate_pass": sink_ok,
        "run_id_match": run_id_ok,
        "summary": attack_summary or None,
        "receiver_summary": receiver_summary or None,
    }
    if not ok:
        if not started or not finished:
            info["reason"] = "attack process did not complete"
        elif attack_rc not in (0, None):
            info["reason"] = meaningful_error(attack_log, "attack_rc=%s" % attack_rc)
        elif not run_id_ok:
            info["reason"] = "attack artifacts run_id mismatch (stale process?)"
        elif not rate_ok:
            info["reason"] = "sender below requested offered load"
        elif not recv_ok:
            info["reason"] = "receiver path not sustained"
        elif not sink_ok:
            info["reason"] = "co-tenant sink failed"
        else:
            info["reason"] = "attack integrity failed"
    return ok, info


def put_file(ctx, ns, deploy, container, path, content):
    r = kctl(ctx, "exec", "-i", "-n", ns, f"deploy/{deploy}", "-c", container, "--",
             "sh", "-c", f"cat > {path}", check_input=content)
    return r.returncode == 0


def rogue_deploy_exists(ctx: str, ns: str, deploy: str) -> bool:
    r = kctl(ctx, "get", "deploy", "-n", ns, deploy, "--ignore-not-found")
    return r.returncode == 0 and bool((r.stdout or "").strip())


def sync_c2_tools_from_host(
    ctx, ns, need_rogue: bool = False, rogue_count: int = 1,
) -> list[str]:
    """Push current sitl/lib/*.py into UE pods (real files, not ConfigMap symlinks under /tmp)."""
    problems: list[str] = []
    targets = [("oai-ue-gcs", "nr-ue"), ("oai-ue-uav", "nr-ue")]
    if need_rogue:
        for deploy in rogue_deploy_names(max(1, rogue_count)):
            if rogue_deploy_exists(ctx, ns, deploy):
                targets.append((deploy, "nr-ue"))
    for deploy, container in targets:
        kctl(
            ctx, "exec", "-n", ns, f"deploy/{deploy}", "-c", container, "--",
            "sh", "-c", f"mkdir -p {C2_RUN_TOOLS} && rm -rf {C2_RUN_TOOLS}/*",
        )
        for fname in C2_SYNC_PY:
            src = SITL_LIB / fname
            if not src.is_file():
                problems.append(f"missing host tool {src}")
                continue
            dest = f"{C2_RUN_TOOLS}/{fname}"
            if not put_file(ctx, ns, deploy, container, dest, src.read_text(encoding="utf-8")):
                problems.append(f"failed to stage {dest} on {deploy}")
        # ROGUE attack generators live under /tmp (scripts call python3 /tmp/c2_sync_flood.py).
        if need_rogue and deploy.startswith("oai-ue-rogue"):
            for fname in ROGUE_ATTACK_PY_REQUIRED:
                src = SITL_LIB / fname
                if not src.is_file():
                    problems.append(f"missing host tool {src}")
                    continue
                dest = f"/tmp/{fname}"
                if not put_file(ctx, ns, deploy, container, dest, src.read_text(encoding="utf-8")):
                    problems.append(f"failed to stage {dest} on {deploy}")
            for fname in ROGUE_ATTACK_PY_OPTIONAL:
                src = SITL_LIB / fname
                if not src.is_file():
                    continue  # optional; pulse-* paths check at use time
                dest = f"/tmp/{fname}"
                put_file(ctx, ns, deploy, container, dest, src.read_text(encoding="utf-8"))
    return problems


def merge_setpoint_freshness(
    run_dir: Path,
    rate_hz: float,
    trajectory_duration_s: float,
    post_stream_s: float = 30.0,
    successful_send_calls: int | None = None,
) -> dict[str, Any]:
    """Post-run: correlate proxy CSVs with setpoints and rewrite freshness artifacts."""
    meta: dict[str, Any] = {"ok": False}
    sp_path = run_dir / "setpoints.csv"
    if not sp_path.is_file():
        meta["reason"] = "setpoints.csv missing"
        return meta
    try:
        from freshness_metrics import merge_proxy_artifacts, setpoint_from_csv_row  # noqa: WPS433
    except ImportError as exc:
        meta["reason"] = f"import failed: {exc}"
        return meta

    setpoints = []
    with sp_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            setpoints.append(setpoint_from_csv_row(row))
    if not setpoints:
        meta["reason"] = "setpoints.csv empty"
        return meta

    def _read_csv(name: str) -> list[dict[str, Any]]:
        p = run_dir / name
        if not p.is_file():
            return []
        with p.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    fwd = _read_csv("proxy_forward.csv")
    rev = _read_csv("proxy_reverse.csv")
    if not fwd:
        meta["reason"] = "proxy_forward.csv missing or empty"
        return meta

    nominal_ms = 1000.0 / rate_hz if rate_hz > 0 else 100.0
    t0 = setpoints[0].t_generation_gcs_ns
    t_active_end = t0 + int(max(1.0, trajectory_duration_s) * 1e9)
    setpoints, aoi_samples, merge_meta = merge_proxy_artifacts(
        setpoints,
        fwd,
        rev,
        nominal_period_ms=nominal_ms,
        t_start_ns=t0,
        t_active_end_ns=t_active_end,
        post_stream_s=post_stream_s,
        run_dir=run_dir,
        successful_send_calls=successful_send_calls,
    )

    rows = [sp.to_csv_row(setpoints[i - 1] if i else None) for i, sp in enumerate(setpoints)]
    with sp_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    aoi_fields = [
        "t_mono_ns", "t_rel_s", "latest_setpoint_id", "latest_generation_ns",
        "aoi_network_ms", "aoi_acceptance_ms", "aoi_motion_ms", "current_deadline_state",
    ]
    with (run_dir / "aoi_timeseries.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=aoi_fields)
        w.writeheader()
        for s in aoi_samples:
            w.writerow({
                "t_mono_ns": s.t_mono_ns,
                "t_rel_s": s.t_rel_s,
                "latest_setpoint_id": s.latest_setpoint_id,
                "latest_generation_ns": s.latest_generation_ns,
                "aoi_network_ms": s.aoi_network_ms,
                "aoi_acceptance_ms": s.aoi_acceptance_ms,
                "aoi_motion_ms": s.aoi_motion_ms,
                "current_deadline_state": s.current_deadline_state,
            })

    fresh = merge_meta.get("freshness_summary") or {}
    matched = merge_meta.get("proxy_forward_matched", 0)
    total = merge_meta.get("proxy_forward_total", 0)
    fresh["merge_meta"] = {k: v for k, v in merge_meta.items() if k != "freshness_summary"}
    fresh["proxy_match_ratio"] = fresh.get("correlation_ratio") or ((matched / total) if total else None)
    (run_dir / "freshness_summary.json").write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")
    print(
        "  [freshness] proxy merge: matched %s/%s forward, delivery=%s capture=%s correlation=%s"
        % (matched, total, merge_meta.get("delivery_ratio"), merge_meta.get("capture_ratio"),
           merge_meta.get("correlation_ratio"))
    )
    meta.update({
        "ok": True,
        "n_setpoints": len(setpoints),
        "proxy_forward_matched": matched,
        "proxy_forward_total": total,
        "proxy_match_ratio": fresh.get("proxy_match_ratio"),
        "correlation_ratio": merge_meta.get("correlation_ratio"),
        "delivery_ratio": merge_meta.get("delivery_ratio"),
        "capture_ratio": merge_meta.get("capture_ratio"),
        "setpoint_accounting": merge_meta.get("setpoint_accounting"),
        "freshness_summary": fresh,
    })
    return meta


def stop_socat_udp5760(ctx: str, ns: str) -> None:
    """Stop sitl sidecar socat UDP:5760 so arrival forwarder can bind (capture runs only)."""
    kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "sitl", "--",
        "sh", "-c", "pkill -f 'socat UDP4-LISTEN:5760' || pkill -f 'socat UDP.*5760' || true",
    )


def restore_socat_udp5760(ctx: str, ns: str) -> None:
    """Restore certified UDP:5760->TCP:5761 socat after capture forwarder teardown."""
    stop_socat_udp5760(ctx, ns)
    kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "sitl", "--",
        "sh", "-c",
        "command -v socat >/dev/null 2>&1 && "
        "nohup socat UDP4-LISTEN:5760,reuseaddr,fork TCP:127.0.0.1:5761 >/dev/null 2>&1 & "
        "sleep 0.5 || true",
    )


def stage_arrival_forwarder(ctx: str, ns: str) -> bool:
    if not C2_ARRIVAL_FORWARDER.is_file():
        return False
    return put_file(
        ctx, ns, "oai-ue-uav", "nr-ue",
        f"{C2_RUN_TOOLS}/c2_arrival_forwarder.py",
        C2_ARRIVAL_FORWARDER.read_text(encoding="utf-8"),
    )


def build_arrival_forwarder_cmd(
    ctx: str, ns: str, run_id: str, envelope_s: float, pod_out: str,
    pod_telemetry_out: str | None = None,
    t0_mono_ns: int | None = None,
    t0_unix_ns: int | None = None,
) -> list[str]:
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "python3", "-u", f"{C2_RUN_TOOLS}/c2_arrival_forwarder.py",
        "--listen-port", str(SITL_PORT),
        "--sitl-port", str(SITL_BACKEND_PORT),
        "--out", pod_out,
        "--duration", str(int(envelope_s) + 15),
    ]
    if pod_telemetry_out:
        cmd += ["--telemetry-out", pod_telemetry_out]
    if t0_mono_ns is not None:
        cmd += ["--t0-mono-ns", str(t0_mono_ns)]
    if t0_unix_ns is not None:
        cmd += ["--t0-unix-ns", str(t0_unix_ns)]
    return cmd


def merge_telemetry_downlink_freshness(
    run_dir: Path,
    trajectory_duration_s: float,
    nominal_hz: float = 20.0,
    clock_offset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Post-run: downlink LOCAL_POSITION_NED timeliness (GCS + optional UAV egress)."""
    meta: dict[str, Any] = {"ok": False}
    try:
        from freshness_metrics import (  # noqa: WPS433
            annotate_belief_validation,
            merge_telemetry_downlink,
        )
    except ImportError as exc:
        meta["reason"] = f"import failed: {exc}"
        return meta
    meta = merge_telemetry_downlink(
        run_dir,
        nominal_hz=nominal_hz,
        active_duration_s=trajectory_duration_s,
    )
    summary = meta.get("telemetry_downlink_summary")
    if isinstance(summary, dict):
        annotate_belief_validation(summary, run_dir, clock_offset=clock_offset)
        out_path = Path(run_dir) / "telemetry_downlink_summary.json"
        out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        meta["telemetry_downlink_summary"] = summary
    return meta


def merge_setpoint_arrival_freshness(
    run_dir: Path,
    rate_hz: float,
    trajectory_duration_s: float,
    post_stream_s: float = 30.0,
    successful_send_calls: int | None = None,
) -> dict[str, Any]:
    """Merge C2 arrival capture CSV with setpoints; dual AoI (generation + arrival)."""
    meta: dict[str, Any] = {"ok": False}
    sp_path = run_dir / "setpoints.csv"
    arr_path = run_dir / "c2_arrival.csv"
    if not sp_path.is_file():
        meta["reason"] = "setpoints.csv missing"
        return meta
    if not arr_path.is_file():
        meta["reason"] = "c2_arrival.csv missing"
        return meta
    try:
        from freshness_metrics import merge_proxy_artifacts, setpoint_from_csv_row  # noqa: WPS433
    except ImportError as exc:
        meta["reason"] = f"import failed: {exc}"
        return meta

    gen_fresh: dict[str, Any] = {}
    gen_path = run_dir / "freshness_summary.json"
    if gen_path.is_file():
        try:
            gen_fresh = json.loads(gen_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            gen_fresh = {}
    gen_aoi_path = run_dir / "aoi_timeseries.csv"
    if gen_aoi_path.is_file():
        (run_dir / "aoi_generation_timeseries.csv").write_text(
            gen_aoi_path.read_text(encoding="utf-8"), encoding="utf-8",
        )

    setpoints = []
    with sp_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            setpoints.append(setpoint_from_csv_row(row))
    if not setpoints:
        meta["reason"] = "setpoints.csv empty"
        return meta

    with arr_path.open(encoding="utf-8") as f:
        fwd = list(csv.DictReader(f))
    if not fwd:
        meta["reason"] = "c2_arrival.csv empty"
        return meta

    nominal_ms = 1000.0 / rate_hz if rate_hz > 0 else 100.0
    t0 = setpoints[0].t_generation_gcs_ns
    t_active_end = t0 + int(max(1.0, trajectory_duration_s) * 1e9)
    setpoints, aoi_samples, merge_meta = merge_proxy_artifacts(
        setpoints,
        fwd,
        [],
        nominal_period_ms=nominal_ms,
        t_start_ns=t0,
        t_active_end_ns=t_active_end,
        post_stream_s=post_stream_s,
        run_dir=run_dir,
        successful_send_calls=successful_send_calls,
    )

    rows = [sp.to_csv_row(setpoints[i - 1] if i else None) for i, sp in enumerate(setpoints)]
    with sp_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    aoi_fields = [
        "t_mono_ns", "t_rel_s", "latest_setpoint_id", "latest_generation_ns",
        "aoi_network_ms", "aoi_acceptance_ms", "aoi_motion_ms", "current_deadline_state",
    ]
    with (run_dir / "aoi_timeseries.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=aoi_fields)
        w.writeheader()
        for s in aoi_samples:
            w.writerow({
                "t_mono_ns": s.t_mono_ns,
                "t_rel_s": s.t_rel_s,
                "latest_setpoint_id": s.latest_setpoint_id,
                "latest_generation_ns": s.latest_generation_ns,
                "aoi_network_ms": s.aoi_network_ms,
                "aoi_acceptance_ms": s.aoi_acceptance_ms,
                "aoi_motion_ms": s.aoi_motion_ms,
                "current_deadline_state": s.current_deadline_state,
            })

    fresh = dict(merge_meta.get("freshness_summary") or {})
    matched = merge_meta.get("proxy_forward_matched", 0)
    total = merge_meta.get("proxy_forward_total", 0)
    # M8: see orchestrate.py — aoi_arrival_ms is cut arrival-path AoI, not a silent alias.
    if not fresh.get("aoi_arrival_ms"):
        fresh["aoi_arrival_ms"] = fresh.get("active_stream", {}).get("aoi_network_ms") or {}
    if not fresh.get("miss_ratio_aoi_arrival"):
        fresh["miss_ratio_aoi_arrival"] = fresh.get("active_stream", {}).get("miss_ratio_aoi_network") or {}
    fresh["aoi_generation_ms"] = gen_fresh.get("aoi_network_ms")
    fresh["miss_ratio_aoi_generation"] = gen_fresh.get("miss_ratio_aoi_network")
    fresh["aoi_network_ms"] = gen_fresh.get("aoi_network_ms")
    fresh["miss_ratio_aoi_network"] = gen_fresh.get("miss_ratio_aoi_network")
    fresh["arrival_capture"] = True
    fresh["aoi_alias_removed"] = True
    fresh["errata_aoi_alias"] = (
        "Plan item M8: removed silent alias aoi_arrival_ms := aoi_network_ms. "
        "aoi_arrival_ms = cut arrival-path continuous AoI; "
        "aoi_network_ms / aoi_generation_ms = generation-based."
    )
    fresh["merge_meta"] = {k: v for k, v in merge_meta.items() if k != "freshness_summary"}
    fresh["proxy_match_ratio"] = fresh.get("correlation_ratio") or ((matched / total) if total else None)
    (run_dir / "freshness_summary.json").write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")
    meta.update({
        "ok": True,
        "n_setpoints": len(setpoints),
        "proxy_forward_matched": matched,
        "proxy_forward_total": total,
        "correlation_ratio": merge_meta.get("correlation_ratio"),
        "freshness_summary": fresh,
    })
    return meta


def fetch_file(ctx, ns, deploy, container, path):
    r = kctl(ctx, "exec", "-n", ns, f"deploy/{deploy}", "-c", container, "--", "cat", path)
    return r.stdout if r.returncode == 0 else None


def fetch_attack_summary(
    ctx, ns, deploy, candidates: list[str], run_id: str = "",
) -> tuple[str | None, str | None]:
    """Read an attack summary from a rogue pod, tolerating the phase suffix.

    co_tenant_load names the file after the burst phase, so a launcher/collector
    mismatch silently drops attackers from the aggregate.  Falls back to the
    newest ``/tmp/attack_summary*.json`` on the pod, rejecting files stamped with
    a different run_id (stale leftovers).
    """
    def _fresh(txt: str | None) -> bool:
        if not txt or not txt.strip():
            return False
        if not run_id:
            return True
        try:
            got = json.loads(txt).get("run_id")
        except json.JSONDecodeError:
            return True
        return not got or got == run_id

    for path in candidates:
        txt = fetch_file(ctx, ns, deploy, "nr-ue", path)
        if _fresh(txt):
            return txt, path
    r = kctl(ctx, "exec", "-n", ns, f"deploy/{deploy}", "-c", "nr-ue", "--", "sh", "-c",
             "ls -1t /tmp/attack_summary*.json 2>/dev/null | head -1")
    newest = (r.stdout or "").strip().splitlines()
    if not newest or not newest[0]:
        return None, None
    txt = fetch_file(ctx, ns, deploy, "nr-ue", newest[0])
    return (txt, newest[0]) if _fresh(txt) else (None, None)


# --------------------------------------------------------------------------- #
# host helpers
# --------------------------------------------------------------------------- #
def read_loadavg1():
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def nproc():
    return os.cpu_count() or 1


def default_host_loadavg_max(ncores: int | None = None) -> float:
    """Auto loadavg1 gate: leave ~2 cores of headroom; floor avoids tiny hosts."""
    n = int(ncores if ncores is not None else nproc())
    return float(max(n - 2, int(HOST_LOADAVG_MAX_FLOOR)))


def dget(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def meaningful_error(out, err):
    """Extract the real failure line from a kubectl-exec'd process, skipping
    kubectl's generic 'command terminated with exit code N' wrapper."""
    lines = []
    for blob in (err, out):
        for ln in (blob or "").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("command terminated with exit code"):
                lines.append(ln)
    return lines[-1] if lines else None


def iperf_client_failure_hint(*texts: str | None) -> str | None:
    """Detect common iperf3 client failures (zombie server, mid-run drop, port busy)."""
    blob = "\n".join(t or "" for t in texts).lower()
    for pat in IPERF_CLIENT_FAIL_PATTERNS:
        if pat in blob:
            return pat
    return None


def reset_ext_dn_iperf_servers(ctx: str) -> list[str]:
    """Kill zombie iperf3 on ext-DN and restart listeners on ROGUE/co-tenant ports."""
    r = kctl(
        ctx, "exec", "-n", "core", "deploy/oai-ext-dn", "-c", "ext-dn", "--",
        "sh", "-c",
        "killall iperf3 2>/dev/null || true; sleep 0.3; "
        f"iperf3 -s -D -p {EXT_DN_IPERF_PORT_ATTACK}; iperf3 -s -D -p {CO_TENANT_PORT}; "
        f"ss -ltn | grep -q ':{EXT_DN_IPERF_PORT_ATTACK} ' && "
        f"ss -ltn | grep -q ':{CO_TENANT_PORT} ' && echo ok",
    )
    if "ok" not in (r.stdout or ""):
        return [f"failed to reset ext-DN iperf3 :{EXT_DN_IPERF_PORT_ATTACK}/:{CO_TENANT_PORT}"]
    return []


def healthcheck_ext_dn_iperf_servers(ctx: str) -> list[str]:
    """Quick 1s loopback client probe per ext-DN iperf3 port."""
    problems: list[str] = []
    for port in EXT_DN_IPERF_PORTS:
        r = kctl(
            ctx, "exec", "-n", "core", "deploy/oai-ext-dn", "-c", "ext-dn", "--",
            "sh", "-c", f"iperf3 -c 127.0.0.1 -p {port} -t 1 >/dev/null 2>&1 && echo ok",
        )
        if "ok" not in (r.stdout or ""):
            problems.append(f"ext-DN iperf3 :{port} healthcheck failed")
    return problems


def ensure_ext_dn_iperf_ready(ctx: str, *, restart_on_fail: bool = True) -> list[str]:
    """Reset ext-DN iperf3 servers and verify they accept clients before a run."""
    problems = reset_ext_dn_iperf_servers(ctx)
    if problems:
        return problems
    problems = healthcheck_ext_dn_iperf_servers(ctx)
    if not problems or not restart_on_fail:
        return problems
    print("[preflight] ext-DN iperf healthcheck failed — restarting oai-ext-dn pod", flush=True)
    kctl(ctx, "rollout", "restart", "-n", "core", "deploy/oai-ext-dn")
    kctl(ctx, "rollout", "status", "-n", "core", "deploy/oai-ext-dn", timeout=120)
    time.sleep(2)
    problems = reset_ext_dn_iperf_servers(ctx)
    if problems:
        return problems
    return healthcheck_ext_dn_iperf_servers(ctx)


def live_log_tail(run_dir: Path, filename: str, max_lines: int = 30) -> str:
    path = run_dir / filename
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


# --------------------------------------------------------------------------- #
# mission plan
# --------------------------------------------------------------------------- #
def mission_plan(duration_s):
    if duration_s in MISSION_BY_DURATION:
        mission_len, n = MISSION_BY_DURATION[duration_s]
    else:
        mission_len = int(round(duration_s * 2 / 3))
        n = max(8, int(round((mission_len - MISSION_OVERHEAD_S) / 5.0)))
    margin = (duration_s - mission_len) / 2.0
    return mission_len, n, margin


def build_mission_json(name, mission_len_s, n_cmds, seed, alt_m=10.0, interval_s=None):
    steady = max(20.0, mission_len_s - MISSION_OVERHEAD_S)
    if interval_s:
        lo, hi = interval_s
    else:
        mean = steady / max(1, n_cmds)
        lo = round(max(0.5, mean - MISSION_JITTER_S), 3)
        hi = round(mean + MISSION_JITTER_S, 3)
    wps = WAYPOINTS_LONG if n_cmds > 40 else WAYPOINTS_SHORT
    return {
        "name": name,
        "takeoff_alt_m": alt_m,
        "accept_radius_m": ACCEPT_RADIUS_M,
        "steady": {"commands": n_cmds, "interval_s": [lo, hi], "alt_m": alt_m, "seed": seed},
        "waypoints": wps,
        "land_after": True,
    }


def mission_params(args, duration):
    """Mission length/command count; --stress-c2 tightens spacing only (25/72 cmds fixed)."""
    mission_len, n_cmds, margin = mission_plan(duration)
    interval_s = None
    if getattr(args, "stress_c2", False):
        interval_s = (0.8, 1.5)
    return mission_len, n_cmds, margin, interval_s


# --------------------------------------------------------------------------- #
# preflight + soft reset
# --------------------------------------------------------------------------- #
def run_sitl_warmup(
    ctx: str,
    ns: str,
    timeout_s: float = 120.0,
    *,
    c2_proxy: bool = False,
    c2_transport: str = "udp",
) -> tuple[bool, str]:
    """Arm/disarm once via direct SITL so the first C2 run is not cold."""
    sitl_url = build_sitl_local_url(c2_proxy, c2_transport)
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "env", f"PYTHONPATH={C2_RUN_TOOLS}",
        "python3", "-u", f"{C2_RUN_TOOLS}/sitl_warmup.py",
        "--sitl", sitl_url,
        "--timeout", str(int(timeout_s)),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-12:])
        return False, tail or f"exit {r.returncode}"
    return True, out.strip().splitlines()[-1] if out.strip() else "ok"


def apply_mitigation_stamp_defaults(args, stamp: dict[str, str]) -> None:
    """Honor start_testbed.sh tokens without re-typing orchestrate flags.

    - MITIGATION_UL_RRMPOLICY=1 (auto from A*/B* > 0) → --mitigation-ul-rrmpolicy
    - MITIGATION_AQM=1 → --mitigation-aqm-ul
    - MIN_RATIO_SLICE_A from A* → sync --min-ratio-slice-a when still 0
    - MIN_RATIO_SLICE_B from B* → sync --reservation when still 0
    Explicit True CLI flags remain True.

    Call this *before* resolve_campaign_dir so folder tokens (A60, AQM, …) match
    the live testbed stamp.
    """
    if str(stamp.get("MITIGATION_UL_RRMPOLICY", "0")) == "1":
        args.mitigation_ul_rrmpolicy = True
    if str(stamp.get("MITIGATION_AQM", "0")) == "1":
        args.mitigation_aqm_ul = True
    try:
        stamp_b = int(stamp.get("MIN_RATIO_SLICE_B", "0") or 0)
        stamp_a = int(stamp.get("MIN_RATIO_SLICE_A", "0") or 0)
    except ValueError:
        stamp_b, stamp_a = 0, 0
    if getattr(args, "topology", "cross") == "cross":
        if int(getattr(args, "min_ratio_slice_a", 0) or 0) == 0 and stamp_a > 0:
            args.min_ratio_slice_a = stamp_a
        if int(getattr(args, "reservation", 0) or 0) == 0 and stamp_b > 0:
            args.reservation = stamp_b
    if getattr(args, "topology", "cross") == "intra" and int(getattr(args, "reservation", 0) or 0) == 0 and stamp_a > 0:
        args.reservation = stamp_a


def preflight(ctx, ns, args, need_rogue):
    problems = []
    mitigation_audit: dict[str, Any] = {
        "enabled": bool(
            args.mitigation_ul_rrmpolicy
            or args.mitigation_rate_limit_a
            or args.mitigation_aqm_ul
        ),
        "pass": True,
    }
    c2_proxy = bool(getattr(args, "c2_proxy", False))
    c2_transport = getattr(args, "c2_transport", "udp")
    if kctl(ctx, "cluster-info", timeout=20).returncode != 0:
        return [f"cluster unreachable via context {ctx}"], {}
    deploys = ["oai-gnb", "oai-ue-uav", "oai-ue-gcs"] + (["oai-ue-rogue"] if need_rogue else [])
    for d in deploys:
        if not pod_running(ctx, ns, d):
            problems.append(f"{d} not Running/available")
    gcs_ip = pdu_ip(ctx, ns, "oai-ue-gcs")
    uav_ip = pdu_ip(ctx, ns, "oai-ue-uav")
    if not gcs_ip:
        problems.append("GCS PDU IP (oaitun_ue1) missing")
    if not uav_ip:
        problems.append("UAV PDU IP (oaitun_ue1) missing")
    gnode, unode = node_of(ctx, ns, "oai-ue-gcs"), node_of(ctx, ns, "oai-ue-uav")
    if gnode and unode and gnode != unode:
        problems.append(f"GCS ({gnode}) and UAV ({unode}) not co-located — CLOCK_MONOTONIC OWD invalid")

    # Host vs UAV /proc/self/ns/time (K7 uses the host T0_mono).
    # owd_probe only compares the GCS↔UAV pods; this check is host↔UAV.
    _tns = check_host_uav_time_namespace(ctx, ns, dry_run=False)
    if not _tns.get("ok"):
        problems.append(
            "HOST/UAV time namespace mismatch — "
            + str(_tns.get("error") or _tns)
        )

    stamp = read_topology_stamp()
    if stamp:
        apply_mitigation_stamp_defaults(args, stamp)
        # Refresh enabled flag after stamp may have turned on AQM / UL rrmPolicy.
        mitigation_audit["enabled"] = bool(
            args.mitigation_ul_rrmpolicy
            or args.mitigation_rate_limit_a
            or args.mitigation_aqm_ul
        )
        saved_topo = stamp.get("TOPOLOGY")
        saved_prb = stamp.get("GNB_PRB")
        if saved_topo and saved_topo != args.topology:
            problems.append(
                f"topology mismatch: cluster stamp={saved_topo} args={args.topology} "
                f"(run ./stop_testbed.sh && ./start_testbed.sh {args.topology})"
            )
        if saved_prb and str(saved_prb) != str(args.gnb_prb):
            problems.append(
                f"GNB_PRB mismatch: cluster stamp={saved_prb} args={args.gnb_prb} "
                f"(redeploy testbed with GNB_PRB={args.gnb_prb})"
            )
        saved_res_a = int(stamp.get("MIN_RATIO_SLICE_A", "0") or 0)
        saved_res_b = int(stamp.get("MIN_RATIO_SLICE_B", "0") or 0)
        want_res = args.reservation
        want_a = int(getattr(args, "min_ratio_slice_a", 0) or 0)
        if args.topology == "intra" and saved_res_a != want_res:
            problems.append(f"reservation mismatch: stamp MinRatio A={saved_res_a} args={want_res}")
        if args.topology == "cross" and saved_res_b != want_res:
            problems.append(f"reservation mismatch: stamp MinRatio B={saved_res_b} args={want_res}")
        if args.topology == "cross" and saved_res_a != want_a:
            problems.append(
                f"min-ratio-slice-a mismatch: stamp MinRatio A={saved_res_a} args={want_a}"
            )
        saved_transport = (stamp.get("C2_TRANSPORT") or "udp").lower()
        if saved_transport == "tcp":
            problems.append(
                "legacy stamp has TCP C2 — redeploy "
                "(./stop_testbed.sh && ./start_testbed.sh)"
            )
        elif saved_transport != "udp":
            problems.append(
                f"legacy stamp C2_TRANSPORT={saved_transport} — redeploy "
                "(./stop_testbed.sh && ./start_testbed.sh)"
            )
        stamp_proxy = stamp.get("TM_C2_PROXY") or stamp.get("TM1_C2_PROXY", "0")
        if stamp_proxy in ("1", "true", "True"):
            problems.append(
                "legacy stamp has C2 proxy — redeploy "
                "(./stop_testbed.sh && ./start_testbed.sh)"
            )

        if args.mitigation_ul_rrmpolicy:
            stamp_rrm = stamp.get("MITIGATION_UL_RRMPOLICY", "0")
            mitigation_audit["ul_rrmpolicy_stamp"] = {"enabled": stamp_rrm}
            if stamp_rrm != "1":
                problems.append(
                    "--mitigation-ul-rrmpolicy requires start_testbed.sh with A*/B*>0 (or ul-rrmpolicy)"
                )
        if args.mitigation_aqm_ul:
            stamp_aqm = stamp.get("MITIGATION_AQM", "0")
            mitigation_audit["aqm_stamp"] = {"enabled": stamp_aqm}
            # AQM may also be requested only via orchestrate (no stamp) — that is OK.
    else:
        problems.append(f"topology stamp missing at {TOPOLOGY_STAMP} — run start_testbed.sh first")

    deployed_topo = detect_cluster_topology(ctx, ns)
    if deployed_topo and deployed_topo != args.topology:
        problems.append(f"gNB deployed topology={deployed_topo} but args={args.topology}")
    deployed_prb = detect_cluster_gnb_prb(ctx, ns)
    if deployed_prb is not None and deployed_prb != args.gnb_prb:
        problems.append(f"gNB dl_carrierBandwidth={deployed_prb} but args.gnb_prb={args.gnb_prb}")

    sitl_port = sitl_listen_port(c2_proxy)
    ss_proto = sitl_ss_proto(c2_transport)
    sitl = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
                "sh", "-c", f"ss -{ss_proto} 2>/dev/null | grep -q ':{sitl_port} ' && echo up || echo down")
    if "up" not in (sitl.stdout or ""):
        hint = " (redeploy: ./stop_testbed.sh && ./start_testbed.sh)"
        problems.append(f"SITL not listening (udp) on UAV :{sitl_port}{hint}")
    if not HOST_TELEMETRY.is_file():
        problems.append(f"host telemetry missing: {HOST_TELEMETRY}")

    ext_dn = kctl(
        ctx, "exec", "-n", "core", "deploy/oai-ext-dn", "-c", "ext-dn", "--",
        "sh", "-c",
        "command -v python3 >/dev/null && command -v iperf3 >/dev/null "
        "&& test -r /tools/co_tenant_sink.py && echo ok",
    )
    if "ok" not in (ext_dn.stdout or ""):
        problems.append(
            "ext-DN pod missing python3, iperf3, or /tools/co_tenant_sink.py "
            "(re-run start_testbed.sh to refresh tm-ext-dn-tools ConfigMap)"
        )

    if stamp.get("PIN_CORES") == "1":
        problems.extend(validate_pin_core_layout(stamp, nproc()))

    if gcs_ip and uav_ip and gcs_ip == uav_ip:
        problems.append(
            f"GCS and UAV share the same PDU IP ({gcs_ip}) — RAN attach broken; "
            "run stop_testbed.sh && start_testbed.sh"
        )
    elif gcs_ip and uav_ip and c2_transport == "udp" and not problems:
        print(f"[preflight] PDU reachability {gcs_ip} -> {uav_ip}…", flush=True)
        if not wait_pdu_reachable(ctx, ns, gcs_ip, uav_ip, timeout_s=120.0):
            problems.append(
                f"PDU path unreachable: ping {gcs_ip} -> {uav_ip} failed after 120s "
                "(restart testbed or UE pods; do not ping your own oaitun IP)"
            )

    if need_rogue and args.attack == "cbr" and args.cbr_dir == "both":
        problems.append(
            "--cbr-dir both is blocked for formal validation; use separate ul/dl runs"
        )

    # Multi-rogue family: need EXTRA_ROGUE_COUNT + live pods (primary + extras).
    if need_rogue and getattr(args, "attack", None) in MULTI_ROGUE_ATTACKS:
        want = max(1, int(getattr(args, "multi_rogue_count", 1) or 1))
        extras = stamp_extra_rogue_count(stamp)
        have = 1 + extras
        if have < want:
            problems.append(
                f"multi-rogue needs {want} rogue pods; stamp EXTRA_ROGUE_COUNT={extras} "
                f"(have {have}). Redeploy: EXTRA_ROGUE_COUNT={want - 1} ./start_testbed.sh"
            )
        else:
            for deploy in rogue_deploy_names(want):
                if not rogue_deploy_exists(ctx, ns, deploy):
                    problems.append(
                        f"multi-rogue missing deploy/{deploy} "
                        f"(EXTRA_ROGUE_COUNT={extras}; redeploy with EXTRA_ROGUE_COUNT={want - 1})"
                    )

    vid = companion_video_mbps(args)
    if vid > 0 and companion_conflicts_with_attack(getattr(args, "attack", None) if need_rogue else None):
        problems.append(
            f"--companion-video-mbps conflicts with --attack {args.attack} "
            f"(legacy gate; UAV→GCS video should not conflict)"
        )

    mitigation_audit["pass"] = not any(
        "mitigation" in p.lower()
        or "5qi" in p.lower()
        or "isolation" in p.lower()
        for p in problems
    )
    args._testbed_mitigation_audit = mitigation_audit
    return problems, {"gcs_ip": gcs_ip, "uav_ip": uav_ip, "node": gnode or unode, "topology_stamp": stamp}


def rogue_count_for_args(args) -> int:
    if getattr(args, "attack", None) in MULTI_ROGUE_ATTACKS:
        return max(1, int(getattr(args, "multi_rogue_count", 1) or 1))
    return 1


def companion_video_mbps(args) -> float:
    """BVLOS demand: UAV→GCS UDP :5202 (camera on the drone). 0 = off (certified default)."""
    try:
        return max(0.0, float(getattr(args, "companion_video_mbps", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def companion_conflicts_with_attack(attack: str | None) -> bool:
    """Legacy gate: old video used ext-dn :5202 (same sink as microburst/multi-rogue).

    Video is now UAV→GCS :5202 (distinct pods from the ext-dn sink) — no port conflict.
    Kept always False so useful combinations (e.g. multi-rogue + video) are not blocked.
    """
    return False


def stamp_extra_rogue_count(stamp: dict[str, str] | None = None) -> int:
    st = stamp if stamp is not None else read_topology_stamp()
    try:
        return max(0, int(st.get("EXTRA_ROGUE_COUNT", "0") or 0))
    except (TypeError, ValueError):
        return 0


def _tc_exec(ctx: str, ns: str, deploy: str, script: str):
    return kctl(
        ctx,
        "exec",
        "-n",
        ns,
        f"deploy/{deploy}",
        "-c",
        "nr-ue",
        "--",
        "bash",
        "-o",
        "pipefail",
        "-c",
        script,
    )


def _detect_pdu_iface(ctx: str, ns: str, deploy: str, preferred: str = "") -> str | None:
    """Resolve an OAI PDU TUN deterministically; never shape eth0/CNI by accident."""
    if preferred:
        check = _tc_exec(ctx, ns, deploy, f"ip link show dev {shlex.quote(preferred)} >/dev/null")
        return preferred if check.returncode == 0 else None
    r = _tc_exec(
        ctx,
        ns,
        deploy,
        "for d in /sys/class/net/oaitun_ue* /sys/class/net/oai; do "
        "[ -e \"$d\" ] && basename \"$d\"; done | sort -V",
    )
    if r.returncode != 0:
        return None
    interfaces = [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]
    return interfaces[0] if interfaces else None


def _tc_qdisc_entries(payload: str) -> list[dict[str, Any]]:
    """Parse ``tc -s -j qdisc show`` (array or single object)."""
    text = (payload or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def extract_aqm_qdisc_sample(payload: str) -> dict[str, Any] | None:
    """Pull HTB/fq_codel counters used to claim local-queue health."""
    entries = _tc_qdisc_entries(payload)
    if not entries:
        return None
    htb = next((e for e in entries if e.get("kind") == "htb"), None)
    fq = next((e for e in entries if e.get("kind") == "fq_codel"), None)
    if fq is None and htb is None:
        return None

    def _num(obj: dict[str, Any] | None, *keys: str) -> float | None:
        if not obj:
            return None
        for key in keys:
            if key in obj and obj[key] is not None:
                try:
                    return float(obj[key])
                except (TypeError, ValueError):
                    continue
        return None

    # Prefer leaf fq_codel; fall back to HTB root if the leaf is missing.
    leaf = fq or htb or {}
    sample = {
        "htb_present": htb is not None,
        "fq_codel_present": fq is not None,
        "backlog_bytes": int(_num(leaf, "backlog") or 0),
        "qlen": int(_num(leaf, "qlen") or 0),
        "drops": int(_num(leaf, "drops") or 0),
        "overlimits": int(_num(leaf, "overlimits") or 0),
        "requeues": int(_num(leaf, "requeues") or 0),
        "bytes": int(_num(leaf, "bytes") or 0),
        "packets": int(_num(leaf, "packets") or 0),
        "ecn_mark": int(_num(fq, "ecn_mark") or 0) if fq else 0,
        "ce_mark": int(_num(fq, "ce_mark") or 0) if fq else 0,
        # Some iproute2 builds expose maxpacket; sojourn itself is rarely in -j.
        "maxpacket": int(_num(fq, "maxpacket") or 0) if fq else 0,
    }
    return sample


def summarize_aqm_qdisc_timeseries(
    samples: list[dict[str, Any]],
    *,
    healthy_max_backlog_kb: float,
    healthy_max_qlen: int,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Aggregate tc samples into audit fields + local_queue_ok ablation flag."""
    n = len(samples)
    if n == 0:
        return {
            "n_samples": 0,
            "local_queue_ok": False,
            "local_queue_ok_reason": "no_tc_samples",
            "pass": False,
        }

    backlog = [int(s.get("backlog_bytes") or 0) for s in samples]
    qlen = [int(s.get("qlen") or 0) for s in samples]
    drops = [int(s.get("drops") or 0) for s in samples]
    ecn = [int(s.get("ecn_mark") or 0) for s in samples]
    max_backlog = max(backlog)
    max_qlen = max(qlen)
    p95_idx = max(0, int(math.ceil(0.95 * n)) - 1)
    backlog_sorted = sorted(backlog)
    qlen_sorted = sorted(qlen)
    backlog_p95 = backlog_sorted[p95_idx]
    qlen_p95 = qlen_sorted[p95_idx]
    drops_delta = max(0, drops[-1] - drops[0])
    ecn_delta = max(0, ecn[-1] - ecn[0])
    max_backlog_kb = max_backlog / 1024.0
    backlog_p95_kb = backlog_p95 / 1024.0
    max_allowed_bytes = max(0.0, float(healthy_max_backlog_kb)) * 1024.0

    reasons: list[str] = []
    if n < min_samples:
        reasons.append(f"too_few_samples({n}<{min_samples})")
    if max_backlog > max_allowed_bytes:
        reasons.append(
            f"max_backlog_bytes={max_backlog}>{int(max_allowed_bytes)}"
        )
    if max_qlen > int(healthy_max_qlen):
        reasons.append(f"max_qlen={max_qlen}>{int(healthy_max_qlen)}")

    local_ok = not reasons
    return {
        "n_samples": n,
        "max_backlog_bytes": max_backlog,
        "max_backlog_kb": round(max_backlog_kb, 3),
        "backlog_p95_bytes": backlog_p95,
        "backlog_p95_kb": round(backlog_p95_kb, 3),
        "max_qlen": max_qlen,
        "qlen_p95": qlen_p95,
        "drops_delta": drops_delta,
        "ecn_mark_delta": ecn_delta,
        "healthy_max_backlog_kb": float(healthy_max_backlog_kb),
        "healthy_max_qlen": int(healthy_max_qlen),
        "local_queue_ok": local_ok,
        "local_queue_ok_reason": "ok" if local_ok else ",".join(reasons),
        "pass": True,  # sampling integrity (not the ablation verdict)
    }


class AqmQdiscSampler:
    """Background ``tc -s -j`` sampler on the UAV PDU interface during a run."""

    def __init__(
        self,
        ctx: str,
        ns: str,
        iface: str,
        out_jsonl: Path,
        *,
        interval_s: float = 1.0,
        deploy: str = "oai-ue-uav",
    ):
        self.ctx = ctx
        self.ns = ns
        self.iface = iface
        self.deploy = deploy
        self.out_jsonl = out_jsonl
        self.interval_s = max(0.2, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[dict[str, Any]] = []
        self.errors = 0
        self.t0_mono = time.monotonic()

    def start(self) -> None:
        self.out_jsonl.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._loop, name="aqm-qdisc-sampler", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
        return {
            "interface": self.iface,
            "deploy": self.deploy,
            "interval_s": self.interval_s,
            "timeseries_path": str(self.out_jsonl.name),
            "n_samples": len(self.samples),
            "n_errors": self.errors,
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            if self._stop.wait(self.interval_s):
                break

    def _sample_once(self) -> None:
        script = (
            f"command -v tc >/dev/null || exit 91; "
            f"tc -s -j qdisc show dev {shlex.quote(self.iface)}"
        )
        r = _tc_exec(self.ctx, self.ns, self.deploy, script)
        mono_s = time.monotonic() - self.t0_mono
        row: dict[str, Any] = {
            "t_mono_s": round(mono_s, 3),
            "wall_utc": datetime.now(timezone.utc).isoformat(),
            "rc": r.returncode,
        }
        if r.returncode != 0:
            self.errors += 1
            row["error"] = (r.stderr or r.stdout or f"rc={r.returncode}").strip()[:240]
        else:
            parsed = extract_aqm_qdisc_sample(r.stdout or "")
            if parsed is None:
                self.errors += 1
                row["error"] = "unparseable_tc_json"
            else:
                row.update(parsed)
                self.samples.append(row)
        try:
            with self.out_jsonl.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError:
            self.errors += 1


def remove_runtime_mitigation_qdiscs(
    ctx: str,
    ns: str,
    *,
    rogue_count: int = 4,
) -> dict[str, Any]:
    """Best-effort cleanup so a mitigation can never leak into a later campaign."""
    audit: dict[str, Any] = {"ok": True, "deployments": {}}
    deploys = rogue_deploy_names(max(1, rogue_count)) + ["oai-ue-uav"]
    for deploy in deploys:
        if not rogue_deploy_exists(ctx, ns, deploy) and deploy != "oai-ue-uav":
            continue
        r = _tc_exec(
            ctx,
            ns,
            deploy,
            "for dev in oaitun_ue1 oaitun_ue2 oaitun_ue3 oai; do "
            "ip link show dev \"$dev\" >/dev/null 2>&1 || continue; "
            "q=\"$(tc qdisc show dev \"$dev\" 2>/dev/null || true)\"; "
            "case \"$q\" in "
            "*'qdisc tbf 10:'*|*'qdisc htb 20:'*) "
            "tc qdisc replace dev \"$dev\" root fq_codel 2>/dev/null || "
            "tc qdisc del dev \"$dev\" root 2>/dev/null || true ;; "
            "esac; "
            "done; true",
        )
        audit["deployments"][deploy] = {"rc": r.returncode}
        audit["ok"] = bool(audit["ok"] and r.returncode == 0)
    return audit


def per_rogue_ul_cap_mbps(args) -> float | None:
    """Per-rogue UL ceiling imposed by rate-limit A, or None when uncapped."""
    if not getattr(args, "mitigation_rate_limit_a", False):
        return None
    aggregate = float(getattr(args, "rate_limit_a_mbps", 0) or 0)
    if aggregate <= 0:
        return None
    return aggregate / max(1, rogue_count_for_args(args))


def apply_runtime_mitigations(args, ctx: str, ns: str) -> dict[str, Any]:
    """Apply and verify defender-controlled tc mitigations before each run."""
    enabled = bool(args.mitigation_rate_limit_a or args.mitigation_aqm_ul)
    audit: dict[str, Any] = {
        "schema_version": "runtime_mitigation_v1",
        "enabled": enabled,
        "pass": True,
        "rate_limit_a": None,
        "aqm_ul": None,
    }
    if not enabled:
        return audit

    if args.attack_path != "pdu":
        raise RuntimeError("runtime tc mitigations require --attack-path pdu")

    if args.mitigation_rate_limit_a:
        count = rogue_count_for_args(args)
        aggregate = float(args.rate_limit_a_mbps)
        per_rogue = aggregate / count
        entries: list[dict[str, Any]] = []
        for deploy in rogue_deploy_names(count):
            iface = _detect_pdu_iface(ctx, ns, deploy, "oaitun_ue1")
            if not iface:
                raise RuntimeError(f"rate-limit A: {deploy} has no oaitun_ue1")
            burst_kbit = max(64, int(args.rate_limit_a_burst_kb * 8))
            script = (
                "command -v tc >/dev/null || exit 91; "
                f"tc qdisc replace dev {shlex.quote(iface)} root handle 10: tbf "
                f"rate {per_rogue:.6f}mbit burst {burst_kbit}kbit "
                f"latency {int(args.rate_limit_a_latency_ms)}ms; "
                f"tc -j qdisc show dev {shlex.quote(iface)}"
            )
            r = _tc_exec(ctx, ns, deploy, script)
            verified = r.returncode == 0 and '"kind":"tbf"' in (r.stdout or "").replace(" ", "")
            entry = {
                "deploy": deploy,
                "interface": iface,
                "aggregate_cap_mbps": aggregate,
                "per_rogue_cap_mbps": round(per_rogue, 6),
                "burst_kb": int(args.rate_limit_a_burst_kb),
                "latency_ms": int(args.rate_limit_a_latency_ms),
                "rc": r.returncode,
                "verified": verified,
                "tc_json": (r.stdout or "").strip(),
                "stderr": (r.stderr or "").strip(),
            }
            entries.append(entry)
            if not verified:
                raise RuntimeError(
                    f"rate-limit A failed on {deploy}/{iface}: rc={r.returncode} "
                    f"{(r.stderr or r.stdout or '').strip()}"
                )
        audit["rate_limit_a"] = {
            "pass": all(e["verified"] for e in entries),
            "aggregate_cap_mbps": aggregate,
            "per_rogue_cap_mbps": round(per_rogue, 6),
            "entries": entries,
        }

    if args.mitigation_aqm_ul:
        preferred = args.aqm_ul_interface
        if preferred == "auto":
            preferred = "oaitun_ue1"
        iface = _detect_pdu_iface(ctx, ns, "oai-ue-uav", preferred)
        if not iface:
            raise RuntimeError(f"AQM UL: UAV has no interface {preferred}")
        rate = float(args.aqm_ul_rate_mbps)
        script = (
            "command -v tc >/dev/null || exit 91; "
            f"tc qdisc replace dev {shlex.quote(iface)} root handle 20: htb default 10; "
            f"tc class replace dev {shlex.quote(iface)} parent 20: classid 20:10 htb "
            f"rate {rate:.6f}mbit ceil {rate:.6f}mbit burst {int(args.aqm_ul_burst_kb)}kb; "
            f"tc qdisc replace dev {shlex.quote(iface)} parent 20:10 handle 30: fq_codel "
            f"limit {int(args.aqm_ul_limit_packets)} target {float(args.aqm_ul_target_ms):g}ms "
            f"interval {float(args.aqm_ul_interval_ms):g}ms "
            + ("ecn; " if args.aqm_ul_ecn else "noecn; ")
            + f"tc -j qdisc show dev {shlex.quote(iface)}"
        )
        r = _tc_exec(ctx, ns, "oai-ue-uav", script)
        compact = (r.stdout or "").replace(" ", "")
        verified = (
            r.returncode == 0
            and '"kind":"htb"' in compact
            and '"kind":"fq_codel"' in compact
        )
        shared_with_c2 = iface == "oaitun_ue1"
        audit["aqm_ul"] = {
            "pass": verified,
            "deploy": "oai-ue-uav",
            "interface": iface,
            "rate_mbps": rate,
            "target_ms": float(args.aqm_ul_target_ms),
            "interval_ms": float(args.aqm_ul_interval_ms),
            "limit_packets": int(args.aqm_ul_limit_packets),
            "ecn": bool(args.aqm_ul_ecn),
            "sample_hz": float(getattr(args, "aqm_ul_sample_hz", 1.0) or 1.0),
            "healthy_max_backlog_kb": float(
                getattr(args, "aqm_ul_healthy_max_backlog_kb", 64.0) or 64.0
            ),
            "healthy_max_qlen": int(getattr(args, "aqm_ul_healthy_max_qlen", 64) or 64),
            "rc": r.returncode,
            "verified": verified,
            "tc_json": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
            # Honest claim: without multi-PDU/5QI, HTB+fq_codel shapes the
            # shared UAV UL TUN (C2 + companion video), not video-only.
            "shared_ue_ul_with_c2": shared_with_c2,
            "claim": (
                "shared_ue_ul_host_aqm"
                if shared_with_c2
                else "video_pdu_host_aqm"
            ),
            "role": "end_host_queue_control_ablation",
        }
        if not verified:
            raise RuntimeError(
                f"AQM UL failed on UAV/{iface}: rc={r.returncode} "
                f"{(r.stderr or r.stdout or '').strip()}"
            )

    audit["pass"] = bool(
        (not args.mitigation_rate_limit_a or dget(audit, "rate_limit_a", "pass"))
        and (not args.mitigation_aqm_ul or dget(audit, "aqm_ul", "pass"))
    )
    return audit





def disable_ul_rrmpolicy(ctx: str, ns: str) -> None:
    kctl(
        ctx,
        "exec",
        "-n",
        ns,
        "deploy/oai-gnb",
        "-c",
        "gnb",
        "--",
        "rm",
        "-f",
        "/tmp/tm_ul_rrmpolicy.conf",
        "/tmp/tm1_ul_rrmpolicy.conf",
    )


def configure_ul_rrmpolicy(args, ctx: str, ns: str) -> dict[str, Any]:
    """Activate the NSSAI-keyed UL rrmPolicyRatio scheduler (TM_UL_RRMPOLICY),
    the 3GPP-faithful UL counterpart of the ORANSlice DL two-level scheduler.

    Unlike the RNTI-based UL isolation, there is nothing to resolve at runtime:
    slices are keyed by S-NSSAI and the Min/Max/Dedicated ratios come from the
    same rrmPolicy.json the DL already enforces (set via testbed B-label /
    MIN_RATIO_SLICE_B). This function flips the runtime switch inside the gNB,
    waits for the scheduler acknowledgment, and audits the policy in force."""
    if not args.mitigation_ul_rrmpolicy:
        disable_ul_rrmpolicy(ctx, ns)
        return {"enabled": False, "pass": True}

    rrm_json = kctl(
        ctx, "exec", "-n", ns, "deploy/oai-gnb", "-c", "gnb", "--",
        "cat", "/opt/oai-gnb/etc/rrmPolicy.json",
    )
    if rrm_json.returncode != 0 or not (rrm_json.stdout or "").strip():
        raise RuntimeError("UL rrmpolicy: cannot read gNB rrmPolicy.json (ratios source)")
    try:
        rrm_policy = json.loads(rrm_json.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"UL rrmpolicy: invalid rrmPolicy.json in gNB pod: {exc}") from exc

    policy = "enabled=1\n"
    write = kctl(
        ctx,
        "exec",
        "-i",
        "-n",
        ns,
        "deploy/oai-gnb",
        "-c",
        "gnb",
        "--",
        "sh",
        "-c",
        "umask 077; tmp=/tmp/tm_ul_rrmpolicy.conf.new; "
        "cat > \"$tmp\" && cp \"$tmp\" /tmp/tm_ul_rrmpolicy.conf "
        "&& cp \"$tmp\" /tmp/tm1_ul_rrmpolicy.conf && rm -f \"$tmp\"",
        check_input=policy,
    )
    if write.returncode != 0:
        raise RuntimeError(
            "UL rrmpolicy: cannot install gNB policy switch: "
            + (write.stderr or write.stdout or f"rc={write.returncode}").strip()
        )

    # Policy reload runs every 128 NR frames (~1.28 s). Poll for the ack.
    marker = "[TM_UL_RRMPOLICY] active"
    legacy_marker = "[TM1_UL_RRMPOLICY] active"  # legacy gNB log tag; keep for old clusters
    verified = False
    for _ in range(20):
        time.sleep(1.0)
        logs = kctl(
            ctx,
            "logs",
            "-n",
            ns,
            "deploy/oai-gnb",
            "-c",
            "gnb",
            "--since=30s",
        )
        out = logs.stdout or ""
        if logs.returncode == 0 and (marker in out or legacy_marker in out):
            verified = True
            break
    if not verified:
        disable_ul_rrmpolicy(ctx, ns)
        raise RuntimeError(
            "UL rrmpolicy: gNB did not acknowledge the policy within ~20s; "
            "confirm mitigation image carries TM_UL_RRMPOLICY_V1"
        )

    audit = {
        "enabled": True,
        "pass": True,
        "policy_path": "/tmp/tm_ul_rrmpolicy.conf",
        "keying": "s_nssai",
        "ratios_source": "/opt/oai-gnb/etc/rrmPolicy.json",
        "rrm_policy_ratio": rrm_policy.get("rrmPolicyRatio"),
        "scheduler_ack": marker,
        "semantics": "3gpp_ts28541_dedicated_prioritized_shared_two_level_pf",
        "policy": policy,
    }
    testbed_audit = getattr(args, "_testbed_mitigation_audit", None) or {}
    testbed_audit["ul_rrmpolicy_runtime"] = audit
    testbed_audit["pass"] = True
    args._testbed_mitigation_audit = testbed_audit
    return audit


def soft_reset(ctx, ns, need_rogue=False, rogue_count: int = 1, *, companion_video: bool = False):
    """Clean per-run leftovers without re-provisioning: restore the C2 tools from
    the mounted ConfigMap (self-heal) and drop ONLY timestamped run outputs — never
    the tool files (mission_runner.py, mission_120.json, ...). SITL persists;
    ArduCopter auto-disarms after LAND.

    NOTE: run-output dirs are /tmp/mission_<YYYYMMDD>_<...>; the glob
    'mission_????????_*' (8 digits + underscore) matches those and the per-run
    mission JSON, but NOT mission_runner.py / mission_120.json / mission_420.json.
    """
    restore = (
        "test -d /config/tm-tools && "
        "for f in /config/tm-tools/*.sh /config/tm-tools/*.json; do "
        "[ -f \"$f\" ] && cp -a \"$f\" /tmp/; done; "
        "chmod +x /tmp/*.sh 2>/dev/null; true"
    )
    clean = (
        "pkill -f '[i]perf3' 2>/dev/null || true; "
        "pkill -f '[s]etpoint_stream|[m]ission_runner' 2>/dev/null || true; "
        "rm -rf /tmp/mission_????????_* /tmp/c2_probe_* 2>/dev/null || true; "
        "rm -f /tmp/companion_video.json 2>/dev/null || true"
    )
    kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
         "sh", "-c", restore + "; " + clean)
    if companion_video:
        # Client iperf + JSON live on the UAV (UAV→GCS); does not reuse ext-dn.
        kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
             "sh", "-c",
             "pkill -f '[i]perf3' 2>/dev/null || true; "
             "rm -f /tmp/companion_video.json 2>/dev/null || true")
    if need_rogue:
        ext_problems = ensure_ext_dn_iperf_ready(ctx)
        if ext_problems:
            print("[warn] ext-DN iperf pre-run:", "; ".join(ext_problems), file=sys.stderr)
    if need_rogue:
        rogue_clean = (
            restore + "; pkill -f '[r]ogue_pulse.py|[i]perf3|[u]dp_flood.py|[c]2_sync_flood.py|"
            "[c]o_tenant_load.py|[m]avlink_proxy.py' 2>/dev/null || true; "
            "rm -f /tmp/attack_summary*.json /tmp/attack_timeseries*.csv /tmp/attack_cbr.json /tmp/attack_*.log"
        )
        for deploy in rogue_deploy_names(max(1, rogue_count)):
            if rogue_deploy_exists(ctx, ns, deploy):
                kctl(ctx, "exec", "-n", ns, f"deploy/{deploy}", "-c", "nr-ue", "--",
                     "sh", "-c", rogue_clean)
    kctl(ctx, "exec", "-n", "core", "deploy/oai-ext-dn", "-c", "ext-dn", "--",
         "sh", "-c",
         "pkill -f '[c]o_tenant_sink.py|[c]o_tenant_burst_sender.py' 2>/dev/null || true; "
         "rm -f /tmp/receiver_summary.json /tmp/receiver_timeseries.csv "
         "/tmp/dl_burst_summary.json /tmp/multi_rogue_dl_summary.json "
         "/tmp/multi_rogue_dl_sync_summary.json "
         "/tmp/dl_burst_summary_*.json 2>/dev/null || true")
    kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
         "sh", "-c",
         restore + "; pkill -f '[m]avlink_proxy.py' 2>/dev/null || true; "
         "rm -rf /tmp/proxy_* /tmp/mission_????????_* 2>/dev/null; rm -f /tmp/owd_recv.json")
    tool_problems = sync_c2_tools_from_host(ctx, ns, need_rogue=need_rogue, rogue_count=rogue_count)
    if tool_problems:
        print("[warn] C2 tool sync:", "; ".join(tool_problems), file=sys.stderr)
    remove_runtime_mitigation_qdiscs(ctx, ns, rogue_count=max(1, rogue_count))
    disable_ul_rrmpolicy(ctx, ns)
    time.sleep(3)


def sync_ext_dn_tools(ctx: str) -> list[str]:
    """Refresh ext-DN /tools/ from host sitl/lib (ConfigMap is read-only mount)."""
    problems: list[str] = []
    for label, path in (
        ("co_tenant_sink.py", EXT_DN_CO_TENANT_SINK),
        ("co_tenant_burst_sender.py", EXT_DN_BURST_SENDER),
    ):
        if not path.is_file():
            problems.append(f"missing {path}")
    if problems:
        return problems
    cmd = [
        "kubectl", "--context", ctx, "create", "configmap", "tm-ext-dn-tools",
        "-n", "core",
        f"--from-file=co_tenant_sink.py={EXT_DN_CO_TENANT_SINK}",
        f"--from-file=co_tenant_burst_sender.py={EXT_DN_BURST_SENDER}",
        "--dry-run=client", "-o", "yaml",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        problems.append(f"configmap dry-run failed: {r.stderr.strip()}")
        return problems
    apply = subprocess.run(
        ["kubectl", "--context", ctx, "apply", "-f", "-"],
        input=r.stdout, capture_output=True, text=True,
    )
    if apply.returncode != 0:
        problems.append(f"configmap apply failed: {apply.stderr.strip()}")
        return problems
    restart = kctl(ctx, "rollout", "restart", "-n", "core", "deploy/oai-ext-dn")
    if restart.returncode != 0:
        problems.append(f"ext-DN restart failed: {restart.stderr.strip()}")
        return problems
    wait = kctl(ctx, "rollout", "status", "-n", "core", "deploy/oai-ext-dn", timeout=120)
    if wait.returncode != 0:
        problems.append(f"ext-DN rollout wait failed: {wait.stderr.strip()}")
    return problems


def sync_ext_dn_burst_sender(ctx: str) -> list[str]:
    """Stage DL burst sender on ext-DN (/tools/) for dual-pulse attacks."""
    return sync_ext_dn_tools(ctx)


def mark_sitl_dirty(ctx: str, ns: str, reason: str = "") -> None:
    payload = reason.replace("'", "")[:200]
    kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "sh", "-c", f"echo '{payload}' > {SITL_DIRTY_FLAG}",
    )


def clear_sitl_dirty(ctx: str, ns: str) -> None:
    kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "sh", "-c", f"rm -f {SITL_DIRTY_FLAG}",
    )


def is_sitl_dirty(ctx: str, ns: str) -> bool:
    r = kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "sh", "-c", f"test -f {SITL_DIRTY_FLAG} && echo dirty || echo clean",
    )
    return "dirty" in (r.stdout or "")


def restart_uav_deployment(
    ctx: str, ns: str, timeout_s: float = 180.0, *, sitl_port: int | None = None,
    c2_transport: str = "udp",
) -> tuple[bool, str]:
    """Delete UAV pod (single-IMSI clean re-attach) and wait for SITL + PDU attach.

    NOT `rollout restart`: RollingUpdate surges a second pod sharing the same IMSI,
    which cannot attach (deadlock) and churns the UAV PDU IP, breaking UPF inter-UE
    routing. Deleting the pod recreates exactly one, re-attaching cleanly.
    """
    port = sitl_port if sitl_port is not None else SITL_BACKEND_PORT
    ss_proto = sitl_ss_proto(c2_transport)
    print("[recovery] delete pod -l app=oai-ue-uav (clean single-IMSI re-attach)…", flush=True)
    r = kctl(ctx, "delete", "pod", "-n", ns, "-l", "app=oai-ue-uav",
             "--grace-period=5", timeout=90)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "delete pod failed").strip()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pod_running(ctx, ns, "oai-ue-uav"):
            ip = pdu_ip(ctx, ns, "oai-ue-uav")
            sitl = kctl(
                ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
                "sh", "-c",
                f"ss -{ss_proto} 2>/dev/null | grep -q ':{port} ' && echo up || echo down",
            )
            if ip and "up" in (sitl.stdout or ""):
                print(
                    "[recovery] UAV ready ip=%s SITL :%d %s up — settling for arducopter boot"
                    % (ip, port, c2_transport),
                    flush=True,
                )
                time.sleep(30)
                return True, ip
        time.sleep(3)
    return False, "UAV pod/SITL not ready within %ss" % int(timeout_s)


def audit_run_environment(
    ctx: str,
    ns: str,
    args,
    need_rogue: bool,
) -> tuple[bool, dict[str, Any]]:
    """Pre-run isolation audit (bootstrap checklist)."""
    audit: dict[str, Any] = {"ok": True, "checks": {}}
    c2_transport = getattr(args, "c2_transport", "udp")
    sitl_port = sitl_listen_port(args.c2_proxy)
    ss_flag = sitl_ss_proto(c2_transport)
    p5760 = kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "sh", "-c", f"ss -ltn 2>/dev/null | grep ':{SITL_PORT} ' && echo busy || echo free",
    )
    p_sitl = kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "sh", "-c", f"ss -{ss_flag} 2>/dev/null | grep ':{sitl_port} ' && echo busy || echo free",
    )
    audit["checks"]["proxy_port_5760"] = (p5760.stdout or "").strip()
    audit["checks"]["sitl_port"] = (p_sitl.stdout or "").strip()
    if args.c2_proxy and "busy" in (p5760.stdout or ""):
        audit["ok"] = False
        audit["reason"] = "stale proxy on :5760"
    if "busy" not in (p_sitl.stdout or ""):
        audit["ok"] = False
        audit["reason"] = "SITL not listening (%s) on :%d" % (c2_transport, sitl_port)
    mr = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
              "sh", "-c",
              "pgrep -f '[s]etpoint_stream|[m]ission_runner' >/dev/null && echo busy || echo free")
    audit["checks"]["gcs_mission"] = (mr.stdout or "").strip()
    if "busy" in (mr.stdout or ""):
        audit["ok"] = False
        audit["reason"] = "stale mission_runner/setpoint_stream on GCS"
    if need_rogue:
        multi = rogue_count_for_args(args)
        if args.attack in MULTI_ROGUE_ATTACKS or multi > 1:
            ok_r, reason_r = audit_rogue_attack_idle(ctx, ns, multi)
            audit["checks"]["rogue_attack"] = "free" if ok_r else "busy"
            if not ok_r:
                audit["ok"] = False
                audit["reason"] = reason_r
        else:
            ar = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-rogue", "-c", "nr-ue", "--",
                       "sh", "-c",
                       "pgrep -f '[c]o_tenant_load|[i]perf3|[r]ogue_pulse' >/dev/null && echo busy || echo free")
            audit["checks"]["rogue_attack"] = (ar.stdout or "").strip()
            if "busy" in (ar.stdout or ""):
                audit["ok"] = False
                audit["reason"] = "stale attack process on ROGUE"
    audit["checks"]["sitl_dirty"] = is_sitl_dirty(ctx, ns)
    return audit["ok"], audit


def audit_post_run_cleanup(
    ctx: str,
    ns: str,
    args,
    need_rogue: bool,
) -> dict[str, Any]:
    """Post-run process/port audit for run_meta.json."""
    audit: dict[str, Any] = {}
    audit["port_check"] = check_uav_mavlink_ports(ctx, ns, args.c2_proxy)
    mr = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
              "sh", "-c",
              "pgrep -f '[s]etpoint_stream|[m]ission_runner' >/dev/null && echo alive || echo dead")
    audit["gcs_mission_alive"] = (mr.stdout or "").strip()
    if companion_video_mbps(args) > 0:
        # Server on GCS; client on UAV.
        ir_gcs = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
                      "sh", "-c",
                      "pgrep -f '[i]perf3' >/dev/null && echo alive || echo dead")
        ir_uav = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
                      "sh", "-c",
                      "pgrep -f '[i]perf3' >/dev/null && echo alive || echo dead")
        audit["companion_iperf_gcs_alive"] = (ir_gcs.stdout or "").strip()
        audit["companion_iperf_uav_alive"] = (ir_uav.stdout or "").strip()
        audit["companion_iperf_alive"] = audit["companion_iperf_uav_alive"]
    pr = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
              "sh", "-c",
              "pgrep -f '[m]avlink_proxy.py' >/dev/null && echo alive || echo dead")
    audit["proxy_alive"] = (pr.stdout or "").strip()
    if need_rogue:
        ar = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-rogue", "-c", "nr-ue", "--",
                   "sh", "-c",
                   "pgrep -f '[c]o_tenant_load|[i]perf3' >/dev/null && echo alive || echo dead")
        audit["rogue_attack_alive"] = (ar.stdout or "").strip()
    audit["clean"] = (
        cleanup_ports_ok(audit.get("port_check") or {})
        and audit["gcs_mission_alive"] == "dead"
        and audit["proxy_alive"] == "dead"
    )
    return audit


def cleanup_ports_ok(port_check: dict[str, str]) -> bool:
    """Expected post-run MAVLink ports in direct UDP and proxy-capable layouts."""
    return (
        port_check.get("proxy_port_5760") == "free"
        and port_check.get("sitl_port_5761") == "busy"
    )


def should_run_sitl_warmup_preflight(args, run_num: int) -> bool:
    """SITL warm-up before a repeat run (attack + long paper campaigns)."""
    if args.workload != "setpoint-stream" or args.skip_sitl_warmup:
        return False
    if run_num == 1:
        return True
    if int(getattr(args, "duration", 0) or 0) >= ATTACK_LONG_DURATION_S:
        return True
    return args.condition != "baseline"


def cooldown_max_wait_s(args) -> int:
    if int(getattr(args, "duration", 0) or 0) >= ATTACK_LONG_DURATION_S:
        return COOLDOWN_ATTACK_LONG_MAX_WAIT_S
    return COOLDOWN_MAX_WAIT_S


def sitl_recovery_timeout_s(args) -> float:
    base = float(getattr(args, "sitl_warmup_timeout", 120) or 120)
    if int(getattr(args, "duration", 0) or 0) >= ATTACK_LONG_DURATION_S:
        return max(base, 240.0)
    return max(base, 180.0)


def attack_run_stressed_sitl(run_meta: dict[str, Any]) -> tuple[bool, str]:
    """True when an attack run likely left SITL/MAVLink unhealthy for the next repeat."""
    if run_meta.get("condition") == "baseline":
        return False, ""
    di = (run_meta.get("gates") or {}).get("drone_integrity") or {}
    hb = di.get("heartbeats_seen")
    if hb is not None and int(hb) < 1:
        return True, "attack run: zero heartbeats in measurement window"
    gap = di.get("max_heartbeat_gap_s")
    if gap is not None and float(gap) >= SITL_STRESS_HEARTBEAT_GAP_S:
        return True, "attack run: heartbeat gap %.1fs (>=%ss)" % (float(gap), SITL_STRESS_HEARTBEAT_GAP_S)
    return False, ""


def mission_failure_needs_sitl_recovery(run_meta: dict[str, Any]) -> tuple[bool, str]:
    gates = run_meta.get("gates") or {}
    for key in ("mission_success", "mission_integrity"):
        g = gates.get(key) or {}
        reason = str(g.get("reason") or "")
        low = reason.lower()
        if any(k in low for k in ("arm failed", "takeoff", "climb", "pre-arm")):
            return True, reason
    di = gates.get("drone_integrity") or {}
    if di.get("pass") is False:
        reason = str(di.get("reason") or "drone_integrity failed")
        hb = di.get("heartbeats_seen")
        if hb is not None and int(hb) < 1:
            return True, reason
        if "heartbeat" in reason.lower():
            return True, reason
    outcomes = run_meta.get("outcomes") or {}
    if outcomes.get("mission_success") is False:
        return True, "mission_success=false"
    stressed, stress_reason = attack_run_stressed_sitl(run_meta)
    if stressed and not run_meta.get("valid"):
        return True, stress_reason
    return False, ""


def refresh_run_ips(ctx: str, ns: str, ips: dict[str, Any]) -> dict[str, Any]:
    """Refresh PDU IPs after UAV restart/re-attach."""
    gcs_ip = pdu_ip(ctx, ns, "oai-ue-gcs")
    uav_ip = pdu_ip(ctx, ns, "oai-ue-uav")
    if gcs_ip:
        ips["gcs_ip"] = gcs_ip
    if uav_ip:
        ips["uav_ip"] = uav_ip
    return ips


def wait_sitl_listen(
    ctx: str, ns: str, port: int, c2_transport: str, timeout_s: float = 90.0,
) -> bool:
    """Wait until SITL port is ready (TCP accept or UDP bind)."""
    if c2_transport == "udp":
        ss_proto = sitl_ss_proto(c2_transport)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            r = kctl(
                ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
                "sh", "-c", f"ss -{ss_proto} 2>/dev/null | grep -q ':{port} ' && echo up || echo down",
            )
            if "up" in (r.stdout or ""):
                return True
            time.sleep(2)
        return False
    return wait_sitl_tcp_accept(ctx, ns, port, timeout_s=timeout_s)


def wait_sitl_tcp_accept(ctx: str, ns: str, port: int, timeout_s: float = 90.0) -> bool:
    """Wait until SITL TCP port accepts connections (MAVLink ready)."""
    deadline = time.monotonic() + timeout_s
    script = (
        f"python3 -c \"import socket,sys; s=socket.socket(); s.settimeout(2); "
        f"s.connect(('127.0.0.1',{port})); s.close(); print('ok')\""
    )
    while time.monotonic() < deadline:
        r = kctl(
            ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
            "sh", "-c", script,
        )
        if r.returncode == 0 and "ok" in (r.stdout or ""):
            return True
        time.sleep(2)
    return False


def wait_pdu_reachable(
    ctx: str,
    ns: str,
    gcs_ip: str,
    uav_ip: str,
    timeout_s: float = 120.0,
) -> bool:
    """Wait until GCS pod can ping UAV PDU IP (5G attach ready after pod restart)."""
    if not gcs_ip or not uav_ip:
        return False
    script = f"ping -c 1 -W 2 {uav_ip} >/dev/null 2>&1 && echo ok"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = kctl(ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--", "sh", "-c", script)
        if r.returncode == 0 and "ok" in (r.stdout or ""):
            return True
        time.sleep(3)
    return False


def recover_sitl_resilient(ctx: str, ns: str, args, reason: str) -> tuple[bool, str]:
    """One automatic retry after a failed UAV re-attach (5G attach can exceed 120s)."""
    ok, info = recover_sitl_after_mission_failure(ctx, ns, args, reason)
    if ok:
        return ok, info
    print(
        "[recovery] first attempt failed (%s) — wait 20s and retry…"
        % str(info)[:100],
        flush=True,
    )
    time.sleep(20)
    return recover_sitl_after_mission_failure(ctx, ns, args, reason + " (retry)")


def recover_sitl_after_mission_failure(
    ctx: str,
    ns: str,
    args,
    reason: str,
) -> tuple[bool, str]:
    """Full SITL recovery: dirty flag → UAV restart → warm-up → clean."""
    print("[recovery] SITL dirty — %s" % reason[:120], flush=True)
    mark_sitl_dirty(ctx, ns, reason)
    soft_reset(ctx, ns, need_rogue=(args.condition != "baseline"),
               rogue_count=rogue_count_for_args(args),
               companion_video=companion_video_mbps(args) > 0)
    sitl_port = sitl_listen_port(args.c2_proxy)
    c2_transport = getattr(args, "c2_transport", "udp")
    ok, info = restart_uav_deployment(
        ctx, ns, timeout_s=sitl_recovery_timeout_s(args), sitl_port=sitl_port,
        c2_transport=c2_transport,
    )
    if not ok:
        return False, info
    need_rogue = args.condition != "baseline"
    rogue_count = rogue_count_for_args(args)
    tool_problems = sync_c2_tools_from_host(ctx, ns, need_rogue=need_rogue, rogue_count=rogue_count)
    if tool_problems:
        return False, "C2 tool sync after UAV restart: " + "; ".join(tool_problems)
    gcs_ip = pdu_ip(ctx, ns, "oai-ue-gcs") or ""
    uav_ip = pdu_ip(ctx, ns, "oai-ue-uav") or ""
    if not wait_pdu_reachable(ctx, ns, gcs_ip, uav_ip, timeout_s=120.0):
        return False, (
            "PDU path GCS→UAV not reachable after UAV restart "
            f"({gcs_ip}→{uav_ip}); run stop_testbed && start_testbed"
        )
    if not wait_sitl_listen(ctx, ns, sitl_port, c2_transport, timeout_s=90.0):
        return False, "SITL (%s) :%d not ready after UAV restart" % (c2_transport, sitl_port)
    if args.workload == "setpoint-stream" and not args.skip_sitl_warmup:
        warm_ok, warm_info = run_sitl_warmup(
            ctx, ns, timeout_s=float(args.sitl_warmup_timeout),
            c2_proxy=args.c2_proxy, c2_transport=c2_transport,
        )
        if not warm_ok:
            return False, warm_info
    clear_sitl_dirty(ctx, ns)
    return True, "uav_restarted_and_warmed"


def start_ext_dn_dl_burst(
    ctx: str,
    rogue_ip: str,
    args,
    duration_s: float,
    run_id: str,
    log_path: Path,
) -> tuple[subprocess.Popen | None, Any]:
    """Start DL burst sender on ext-DN (dual-pulse DL leg)."""
    half = args.mean_rate_mbps / 2.0
    ul_bytes = max(1000, args.burst_size_bytes // 2)
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", "core", "deploy/oai-ext-dn", "-c", "ext-dn", "--",
        "python3", "-u", "/tools/co_tenant_burst_sender.py",
        "--target", rogue_ip,
        "--port", "5203",
        "--duration", str(int(duration_s) + 5),
        "--period-ms", str(args.dual_period_ms),
        "--phase-ms", str(args.dual_phase_ms),
        "--burst-bytes", str(ul_bytes),
        "--total-mbps", str(args.mean_rate_mbps),
        "--run-id", run_id,
        "--out-summary", "/tmp/dl_burst_summary.json",
    ]
    return _popen_log(cmd, log_path)


# --------------------------------------------------------------------------- #
# attack launch
# --------------------------------------------------------------------------- #
# Pulse presets: (bolus_kb, period_ms, pkt_bytes, flows). The bolus is dumped as
# fast as possible each period, then the sender sleeps the remainder — so shorter
# period + bigger bolus + more flows = heavier, burstier PRB contention.
#   moderate  ~= run_attack_pulse.sh default (~26 Mbit/s avg, single flow)
#   aggressive = short period, big bolus, multi-flow (saturates the radio + fills buffers)
PULSE_PROFILES = {
    "moderate":   (192, 60, 1200, 1),
    "aggressive": (384, 30, 1400, 3),
}

# UE-to-UE UDP flood profiles for flood-c2 (pps, payload bytes).
FLOOD_PROFILES = {
    "gentle":     {"pps": 25,  "size": 32},
    "moderate":   {"pps": 100, "size": 32},
    "aggressive": {"pps": 200, "size": 64},
}


def flood_params(args):
    prof = dict(FLOOD_PROFILES[args.flood_profile])
    if args.flood_pps is not None:
        prof["pps"] = args.flood_pps
    if args.flood_size is not None:
        prof["size"] = args.flood_size
    return prof


def pulse_params(args):
    bolus, period, pkt, flows = PULSE_PROFILES[args.pulse_profile]
    if args.pulse_bolus_kb is not None:
        bolus = args.pulse_bolus_kb
    if args.pulse_period_ms is not None:
        period = args.pulse_period_ms
    if args.pulse_pkt is not None:
        pkt = args.pulse_pkt
    if args.pulse_flows is not None:
        flows = args.pulse_flows
    return bolus, period, pkt, flows


def _rogue_bash(ctx, ns, script):
    return ["kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-rogue",
            "-c", "nr-ue", "--", "bash", "-o", "pipefail", "-c", script]


def sync_c2_rogue_saw_bursts(attack_log: str) -> bool:
    """True if the ROGUE flood actually emitted bursts (not just GCS triggers)."""
    text = attack_log or ""
    if "[ROGUE-SYNC] burst #" in text:
        return True
    return bool(re.search(r"\[ROGUE-SYNC\] done: bursts=([1-9]\d*)", text))


def is_sync_style_attack(attack: str | None) -> bool:
    return (attack or "") in SYNC_STYLE_ATTACKS


def sync_open_loop_enabled(args) -> bool:
    return bool(getattr(args, "sync_open_loop", False)) and is_sync_style_attack(
        getattr(args, "attack", None)
    )


def attack_log_is_open_loop(attack_log: str) -> bool:
    return "mode=open-loop" in (attack_log or "")


def summarize_sync_skew(run_dir: Path) -> dict[str, Any]:
    """Merge GCS + ROGUE skew CSVs (join on t0_gcs_ns) for oracle latency evidence."""
    info: dict[str, Any] = {"ok": False}
    gcs_path = run_dir / "sync_skew_gcs.csv"
    rogue_path = run_dir / "sync_skew_rogue.csv"
    info["gcs_csv"] = gcs_path.is_file()
    info["rogue_csv"] = rogue_path.is_file()
    if not rogue_path.is_file():
        info["reason"] = "sync_skew_rogue.csv missing"
        return info
    try:
        with rogue_path.open(encoding="utf-8") as f:
            rogue_rows = list(csv.DictReader(f))
    except OSError as exc:
        info["reason"] = str(exc)
        return info
    gcs_by_t0: dict[str, dict[str, str]] = {}
    if gcs_path.is_file():
        try:
            with gcs_path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    t0 = (row.get("t0_gcs_ns") or "").strip()
                    if t0:
                        gcs_by_t0[t0] = row
        except OSError:
            pass
    total_us: list[float] = []
    cni_us: list[float] = []
    setup_us: list[float] = []
    joined = 0
    for row in rogue_rows:
        try:
            setup_us.append(float(row.get("d_burst_minus_recv_us") or 0))
        except ValueError:
            pass
        t0 = (row.get("t0_gcs_ns") or "").strip()
        if t0 and t0 in gcs_by_t0:
            joined += 1
            try:
                t3 = int(row["t3_burst_start_ns"])
                t0i = int(t0)
                t1 = int(gcs_by_t0[t0]["t1_trigger_sent_ns"])
                t2 = int(row["t2_trigger_recv_ns"])
                total_us.append((t3 - t0i) / 1000.0)
                cni_us.append((t2 - t1) / 1000.0)
            except (KeyError, ValueError, TypeError):
                pass
        elif row.get("d_burst_minus_t0_us"):
            try:
                total_us.append(float(row["d_burst_minus_t0_us"]))
            except ValueError:
                pass

    def _stat(vals: list[float]) -> dict[str, float | None]:
        if not vals:
            return {"n": 0, "p50": None, "p99": None, "max": None}
        s = sorted(vals)
        return {
            "n": len(s),
            "p50": s[len(s) // 2],
            "p99": s[max(0, int(len(s) * 0.99) - 1)],
            "max": s[-1],
        }

    info.update({
        "ok": True,
        "rogue_bursts": len(rogue_rows),
        "joined_on_t0": joined,
        "burst_minus_t0_us": _stat(total_us),
        "cni_t2_minus_t1_us": _stat(cni_us),
        "setup_t3_minus_t2_us": _stat(setup_us),
        "note": (
            "t3-t0 = oracle→burst (CLOCK_MONOTONIC; Kind same-node ≈ aligned). "
            "Compare to C2 OWD (~10–20 ms)."
        ),
    })
    return info


def ip_in_cidr(ip: str, cidr: str) -> bool:
    """True if dotted-quad *ip* falls in *cidr* (IPv4 only)."""
    try:
        a, b, c, d = (int(x) for x in ip.strip().split("."))
        host = (a << 24) | (b << 16) | (c << 8) | d
        net_s, pref_s = cidr.split("/", 1)
        na, nb, nc, nd = (int(x) for x in net_s.split("."))
        net = (na << 24) | (nb << 16) | (nc << 8) | nd
        pref = int(pref_s)
        mask = (0xFFFFFFFF << (32 - pref)) & 0xFFFFFFFF if pref else 0
        return (host & mask) == (net & mask)
    except (TypeError, ValueError):
        return False


def assert_attack_avoids_victim_endpoint(
    attack: str,
    *,
    target_ip: str,
    target_port: int | None = None,
    uav_ip: str = "",
    gcs_ip: str = "",
) -> None:
    """Guard-rail: pure contention MUST NOT target victim C2 IP/port (slice B)."""
    tip = (target_ip or "").strip()
    if not tip:
        raise SystemExit(
            "CRITICAL ERROR: attack has no destination — cannot validate pure-contention threat model"
        )
    hits_victim = False
    if uav_ip and tip == uav_ip.strip():
        hits_victim = True
    if gcs_ip and tip == gcs_ip.strip():
        hits_victim = True
    if ip_in_cidr(tip, SUBNET_SLICE_B_CIDR):
        hits_victim = True
    if hits_victim:
        raise SystemExit(
            "CRITICAL ERROR: attack targets the victim endpoint "
            f"({tip}:{target_port if target_port is not None else '?'}) — "
            "violates the pure-contention threat model. "
            "Attacks must go to the attacker's own destination (ext-dn), not GCS/UAV C2."
        )


def parse_sync_c2_pod_status(line: str) -> tuple[str, int | None]:
    """Parse ROGUE query: DONE <rc> | RUNNING | DEAD | unknown."""
    tok = (line or "").split()
    if not tok:
        return "unknown", None
    kind = tok[0].upper()
    if kind == "DONE":
        if len(tok) > 1:
            try:
                return "done", int(tok[1])
            except ValueError:
                return "done", None
        return "done", None
    if kind == "RUNNING":
        return "running", None
    if kind == "DEAD":
        return "dead", None
    return "unknown", None


def query_sync_c2_pod(ctx: str, ns: str) -> str:
    r = kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-rogue", "-c", "nr-ue", "--",
        "sh", "-c",
        "if [ -f /tmp/c2_sync_flood.rc ]; then echo DONE $(cat /tmp/c2_sync_flood.rc); "
        "elif [ -f /tmp/c2_sync_flood.pid ] && kill -0 $(cat /tmp/c2_sync_flood.pid) 2>/dev/null; "
        "then echo RUNNING; else echo DEAD; fi",
    )
    return (r.stdout or "").strip()


def fetch_sync_c2_pod_log(ctx: str, ns: str, dest: Path) -> None:
    r = kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-rogue", "-c", "nr-ue", "--",
        "sh", "-c",
        'f=$(ls -1t /tmp/attack_sync_prb_*.log /tmp/attack_sync_c2_*.log 2>/dev/null | head -1); '
        '[ -n "$f" ] && cat "$f"',
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return
    try:
        dest.write_text(r.stdout, encoding="utf-8")
    except OSError:
        pass


def fetch_sync_skew_rogue(ctx: str, ns: str, dest: Path) -> None:
    r = kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-rogue", "-c", "nr-ue", "--",
        "sh", "-c", "test -s /tmp/sync_skew_rogue.csv && cat /tmp/sync_skew_rogue.csv",
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return
    try:
        dest.write_text(r.stdout, encoding="utf-8")
    except OSError:
        pass


def finalize_sync_c2(
    ctx: str, ns: str, kubectl_rc: int | None, timeout_s: float, dest_log: Path,
) -> int:
    """If kubectl exec died (137), keep the detached flood's real rc from the ROGUE pod."""
    deadline = time.monotonic() + max(5.0, float(timeout_s))
    while time.monotonic() < deadline:
        state, flood_rc = parse_sync_c2_pod_status(query_sync_c2_pod(ctx, ns))
        if state == "done":
            fetch_sync_c2_pod_log(ctx, ns, dest_log)
            if kubectl_rc not in (0, None) and flood_rc == 0:
                print(
                    "  [warn] kubectl exec rc=%s but c2_sync_flood on ROGUE finished OK"
                    % kubectl_rc,
                    flush=True,
                )
            return int(flood_rc if flood_rc is not None else 1)
        if state == "dead":
            fetch_sync_c2_pod_log(ctx, ns, dest_log)
            return int(kubectl_rc) if kubectl_rc not in (0, None) else 137
        time.sleep(2)
    kctl(
        ctx, "exec", "-n", ns, "deploy/oai-ue-rogue", "-c", "nr-ue", "--",
        "sh", "-c", "pkill -f '[c]2_sync_flood.py' 2>/dev/null || true",
    )
    fetch_sync_c2_pod_log(ctx, ns, dest_log)
    return int(kubectl_rc) if kubectl_rc not in (0, None) else 124


def rogue_deploy_names(count: int = 4) -> list[str]:
    """Return oai-ue-rogue deployment names for multi-rogue fairness attack."""
    names = ["oai-ue-rogue"]
    for i in range(2, max(2, count) + 1):
        names.append(f"oai-ue-rogue{i}")
    return names[:count]


def multi_rogue_phase_offsets(count: int, phase_ms: int) -> list[int]:
    """Per-rogue burst phase in ms — the summary filename key on each pod.

    ``phase_ms=0`` still needs distinct offsets (1 ms apart) so the rogues do not
    all write ``/tmp/attack_summary_0.json``; launcher and collector must derive
    the offsets the same way or the aggregate under-counts attackers.
    """
    step = max(1, int(phase_ms))
    return [idx * step for idx in range(max(1, int(count)))]


def _rogue_bash_deploy(ctx, ns, deploy: str, script: str):
    return ["kubectl", "--context", ctx, "exec", "-n", ns, f"deploy/{deploy}",
            "-c", "nr-ue", "--", "bash", "-o", "pipefail", "-c", script]


def _microburst_script_fragment(
    args, dur: str, ts: str, run_id: str, phase_ms: int = 0, seed_offset: int = 0,
) -> str:
    mbps = args.mean_rate_mbps
    bb = args.burst_size_bytes
    pm = args.period_ms
    return (
        'echo "[CO-TENANT] microburst path=%(path)s mean=%(mbps)sM burst=%(bb)sB period=%(pm)sms phase=%(phase)sms"; '
        % {"path": args.attack_path, "mbps": mbps, "bb": bb, "pm": pm, "phase": phase_ms}
        + ts
        + f'python3 {C2_RUN_TOOLS}/co_tenant_load.py microburst --target "$EXT" --port %(port)s --bind "$ROGUE" '
        % {"port": CO_TENANT_PORT}
        + '--duration %(dur)s --mean-mbps %(mbps)s --burst-bytes %(bb)s --period-ms %(pm)s '
        % {"dur": dur, "mbps": mbps, "bb": bb, "pm": pm}
        + '--phase-ms %(phase)s --path %(path)s --seed %(seed)s --run-id %(run_id)s '
        % {"phase": phase_ms, "path": args.attack_path,
           "seed": args.attack_seed + seed_offset, "run_id": run_id}
        + '--out-summary /tmp/attack_summary_%(phase)s.json --out-timeseries /tmp/attack_timeseries_%(phase)s.csv '
        % {"phase": phase_ms}
    )


def _pulse_script(dst_expr, port, dur, bolus, period, pkt, flows, label, taskset_prefix=""):
    """Shell fragment: rogue_pulse.py to *dst_expr* (eval'd in pod)."""
    return (
        'echo "[ROGUE] pulse %(dur)ss bolus=%(bolus)sKB period=%(period)sms pkt=%(pkt)s '
        'flows=%(flows)s rogue=$ROGUE -> %(label)s"; '
        '%(ts)s python3 /tmp/rogue_pulse.py "$ROGUE" %(dst)s %(port)s %(dur)s '
        '%(bolus)s %(period)s %(pkt)s %(flows)s'
    ) % {
        "dur": dur, "bolus": bolus, "period": period, "pkt": pkt, "flows": flows,
        "label": label, "dst": dst_expr, "port": port, "ts": taskset_prefix,
    }


def attack_command(ctx, ns, args, envelope_s, uav_ip, gcs_ip, run_id: str = ""):
    attack = args.attack
    onset_delay, burst_s = resolve_attack_onset(args, envelope_s)
    # Onset delay only applies to microburst-family loaders; other attacks keep full envelope.
    if attack == "microburst":
        dur = str(burst_s)
    else:
        dur = str(int(envelope_s))
        onset_delay = 0.0
    bolus, period, pkt, flows = pulse_params(args)
    ts = attack_taskset(ctx, ns)
    preamble = (
        'set -euo pipefail; . /tmp/tm_common.sh; '
        'ROGUE="$(tm_pdu_ip)"; EXT="$(tm_ext_dn_ip)"; '
        'if [ -z "$EXT" ]; then echo "[ROGUE] ERROR: cannot resolve oai-ext-dn.core.svc.cluster.local"; exit 1; fi; '
    )

    if attack == "pulse":
        script = preamble + _pulse_script(
            '"$EXT"', "5201", dur, bolus, period, pkt, flows,
            f"ext-dn $EXT:5201 (indirect, UAV={uav_ip})", ts,
        )
        script += ' | tee /tmp/attack_pulse_extdn_%(dur)ss.log' % {"dur": dur}
        return _rogue_bash(ctx, ns, script)

    if attack == "pulse-c2":
        script = preamble + _pulse_script(
            '"%s"' % uav_ip, str(SITL_PORT), dur, bolus, period, pkt, flows,
            f"UAV {uav_ip}:{SITL_PORT} (C2 port)", ts,
        )
        script += ' | tee /tmp/attack_pulse_c2_%(dur)ss.log' % {"dur": dur}
        return _rogue_bash(ctx, ns, script)

    if attack == "cbr":
        if args.cbr_dir == "both":
            raise SystemExit(
                "--cbr-dir both is blocked for formal validation; run separate ul and dl experiments"
            )
        mbps, parallel = args.cbr_mbps, args.cbr_parallel
        dir_flag = {"ul": "", "dl": "-R", "both": "--bidir"}[args.cbr_dir]
        proto = getattr(args, "cbr_protocol", "udp")
        if proto == "tcp":
            rate_part = f"-P {parallel} "
            proto_tag = "tcp"
        else:
            rate_part = f"-u -b {mbps}M -P {parallel} "
            proto_tag = "udp"
        script = (
            preamble +
            'echo "[ROGUE] cbr %(dur)ss proto=%(proto)s dir=%(dir)s rogue=$ROGUE -> ext-dn $EXT:5201"; '
            '%(ts)s iperf3 -c "$EXT" %(rate_part)s-t %(dur)s -B "$ROGUE" %(dir_flag)s '
            '-J --get-server-output > /tmp/attack_cbr.json 2>/tmp/attack_cbr.stderr; '
            'RC=$?; (test -s /tmp/attack_cbr.json && cat /tmp/attack_cbr.json) '
            '| tee /tmp/attack_cbr_%(dir)s_%(dur)ss.log; exit $RC'
        ) % {"dur": dur, "dir": args.cbr_dir, "rate_part": rate_part, "proto": proto_tag,
             "dir_flag": dir_flag, "ts": ts}
        return _rogue_bash(ctx, ns, script)

    if attack == "combined":
        mbps, parallel = args.cbr_mbps, args.cbr_parallel
        pulse = _pulse_script(
            '"$EXT"', "5201", dur, bolus, period, pkt, flows,
            f"ext-dn $EXT:5201 (UL)", ts,
        )
        cbr = (
            'echo "[ROGUE] cbr %(dur)ss dir=dl -b %(mbps)sM -P %(par)s rogue=$ROGUE -> ext-dn $EXT:5201 (DL)"; '
            '%(ts)s iperf3 -c "$EXT" -u -b %(mbps)sM -P %(par)s -t %(dur)s -B "$ROGUE" -R '
            '| tee /tmp/attack_cbr_dl_%(dur)ss.log'
        ) % {"dur": dur, "mbps": mbps, "par": parallel, "ts": ts}
        script = (
            preamble +
            pulse + ' > /tmp/attack_pulse_extdn_%(dur)ss.log 2>&1 & PID_P=$!; ' % {"dur": dur} +
            cbr + ' & PID_C=$!; wait $PID_P; RC_P=$?; wait $PID_C; RC_C=$?; '
            'echo "combined done pulse_rc=$RC_P cbr_dl_rc=$RC_C"; test $RC_P -eq 0 -a $RC_C -eq 0'
        )
        return _rogue_bash(ctx, ns, script)

    if attack == "microburst":
        tgt, bind = ext_dn_target(ctx, ns, args.attack_path)
        bb = args.burst_size_bytes
        pm = args.period_ms
        mbps = args.mean_rate_mbps
        if args.attack_path == "pdu":
            pre = preamble
            bind_expr = '"$ROGUE"'
        else:
            pre = (
                'set -euo pipefail; '
                f'ROGUE="{bind}"; EXT="{tgt}"; '
            )
            bind_expr = '"$ROGUE"'
            tgt = '"$EXT"'
        script = (
            pre
            + _attack_onset_sleep_fragment(onset_delay)
            + _microburst_script_fragment(args, dur, ts, run_id)
            + '| tee /tmp/attack_microburst_%(dur)ss.log' % {"dur": dur}
        )
        return _rogue_bash(ctx, ns, script)

    if attack in MULTI_ROGUE_ATTACKS:
        raise SystemExit(
            f"{attack} uses a dedicated multi-rogue launcher; not attack_command()"
        )

    if attack == "dual-pulse":
        mbps = args.mean_rate_mbps
        pm = args.dual_period_ms
        ph = args.dual_phase_ms
        bb = max(1000, args.burst_size_bytes // 2)
        script = (
            preamble
            + 'echo "[CO-TENANT] dual-pulse UL dur=%(dur)ss T=%(pm)sms phase=%(ph)sms total=%(mbps)sM"; '
            % {"dur": dur, "pm": pm, "ph": ph, "mbps": mbps}
            + ts
            + f'python3 {C2_RUN_TOOLS}/co_tenant_load.py dual-pulse --target "$EXT" --port %(port)s --bind "$ROGUE" '
            % {"port": CO_TENANT_PORT}
            + '--duration %(dur)s --period-ms %(pm)s --phase-ms %(ph)s '
            % {"dur": dur, "pm": pm, "ph": ph}
            + '--ul-burst-bytes %(bb)s --dl-burst-bytes %(bb)s --total-mbps %(mbps)s '
            % {"bb": bb, "mbps": mbps}
            + '--path %(path)s --run-id %(run_id)s '
            % {"path": args.attack_path, "run_id": run_id}
            + '--out-summary /tmp/attack_summary.json --out-timeseries /tmp/attack_timeseries.csv '
            + '| tee /tmp/attack_dual_pulse_%(dur)ss.log' % {"dur": dur}
        )
        return _rogue_bash(ctx, ns, script)

    if attack == "flood-c2":
        # UE-to-UE UDP flood → UAV C2 port (path/endpoint ablation; shared bearer + stack stress).
        fp = flood_params(args)
        port = args.flood_port if args.flood_port is not None else SITL_PORT
        script = (
            preamble +
            'echo "[ROGUE] flood-c2 %(dur)ss profile=%(profile)s pps=%(pps)s size=%(size)sB '
            'rogue=$ROGUE -> %(uav)s:%(port)s"; '
            '%(ts)s python3 /tmp/udp_flood.py %(profile)s --target %(uav)s --port %(port)s '
            '--bind "$ROGUE" --duration %(dur)s --pps %(pps)s --size %(size)s '
            '| tee /tmp/attack_flood_c2_%(dur)ss.log'
        ) % {
            "dur": dur, "profile": args.flood_profile, "pps": fp["pps"], "size": fp["size"],
            "uav": uav_ip, "port": port, "ts": ts,
        }
        return _rogue_bash(ctx, ns, script)

    if attack == "sync-c2":
        # PATH/endpoint (legacy / ablation): targets UAV:5760. With L3 DROP the path dies.
        # Pure contention: --attack sync-prb (same timing, ext-dn sink).
        # --sync-open-loop: no GCS trigger (own phase).
        open_loop = sync_open_loop_enabled(args)
        if open_loop and args.sync_ack_target:
            raise SystemExit("sync open-loop + --sync-ack-target not supported")
        ack = (' --ack-target %s --ack-port %d' % (gcs_ip, SITL_PORT)) if args.sync_ack_target else ''
        acktag = " +ack" if args.sync_ack_target else ""
        ol_flags = (
            ' --open-loop --rate-hz %(rate)s --phase-ms %(phase)s'
            % {"rate": args.setpoint_rate_hz, "phase": args.sync_phase_ms}
        ) if open_loop else ''
        listen_flags = '' if open_loop else (' --listen-port %d' % args.sync_listen_port)
        mode_tag = "open-loop" if open_loop else "trigger-oracle"
        logf = "/tmp/attack_sync_c2_%ss.log" % dur
        script = (
            preamble +
            'echo "[ROGUE] sync-c2 %(dur)ss mode=%(mode)s burst=%(bms)sms pps=%(pps)s '
            'size=%(size)sB rogue=$ROGUE -> %(uav)s:%(port)s%(acktag)s [PATH/endpoint]"; '
            'if [ -f /tmp/c2_sync_flood.pid ]; then '
            '_old=$(cat /tmp/c2_sync_flood.pid); '
            'if [ -n "$_old" ]; then kill "$_old" 2>/dev/null || true; sleep 0.2; '
            'kill -9 "$_old" 2>/dev/null || true; fi; fi; '
            'rm -f /tmp/c2_sync_flood.rc /tmp/c2_sync_flood.pid /tmp/sync_skew_rogue.csv; '
            'nohup sh -c "%(ts)s setsid python3 -u /tmp/c2_sync_flood.py%(listen)s '
            '--target %(uav)s --port %(port)s --bind $ROGUE '
            '--burst-ms %(bms)s --pps %(pps)s --size %(size)s --duration %(dur)s'
            '%(ack)s%(ol)s --skew-log /tmp/sync_skew_rogue.csv; '
            'echo \\$? > /tmp/c2_sync_flood.rc" '
            '</dev/null >%(logf)s 2>&1 & '
            'echo $! > /tmp/c2_sync_flood.pid; '
            'sleep 0.4; '
            'kill -0 "$(cat /tmp/c2_sync_flood.pid)" 2>/dev/null '
            '|| { echo "[ROGUE-SYNC] ERROR: flood did not start"; exit 1; }; '
            'echo "[ROGUE-SYNC] detached pid=$(cat /tmp/c2_sync_flood.pid) log=%(logf)s"; '
            'tail -n +1 -F %(logf)s & TPID=$!; '
            'while [ ! -f /tmp/c2_sync_flood.rc ]; do sleep 1; done; '
            'sleep 0.2; kill $TPID 2>/dev/null || true; '
            'exit "$(cat /tmp/c2_sync_flood.rc)"'
        ) % {
            "dur": dur, "bms": args.sync_burst_ms,
            "pps": args.sync_pps, "size": args.sync_size, "uav": uav_ip, "port": SITL_PORT,
            "ack": ack, "acktag": acktag, "ts": ts, "logf": logf,
            "ol": ol_flags, "listen": listen_flags, "mode": mode_tag,
        }
        return _rogue_bash(ctx, ns, script)

    if attack == "sync-prb":
        # Same timing as sync-c2 (CNI trigger @ setpoint), destination = ext-dn :5201.
        # PURE radio/PRB contention — does NOT touch victim IP:port (L3-safe).
        if args.sync_ack_target:
            raise SystemExit(
                "sync-prb: --sync-ack-target targets GCS (victim) — forbidden in the "
                "pure-contention threat model; remove the flag"
            )
        open_loop = sync_open_loop_enabled(args)
        ext_ip = (
            kctl(
                ctx, "get", "pod", "-n", "core", "-l", "app=oai-ext-dn",
                "-o", "jsonpath={.items[0].status.podIP}",
            ).stdout or ""
        ).strip()
        if not ext_ip:
            raise SystemExit("sync-prb: cannot resolve oai-ext-dn IP (sink for pure PRB contention)")
        assert_attack_avoids_victim_endpoint(
            "sync-prb",
            target_ip=ext_ip,
            target_port=EXT_DN_IPERF_PORT_ATTACK,
            uav_ip=uav_ip,
            gcs_ip=gcs_ip,
        )
        port = EXT_DN_IPERF_PORT_ATTACK
        ol_flags = (
            ' --open-loop --rate-hz %(rate)s --phase-ms %(phase)s'
            % {"rate": args.setpoint_rate_hz, "phase": args.sync_phase_ms}
        ) if open_loop else ''
        listen_flags = '' if open_loop else (' --listen-port %d' % args.sync_listen_port)
        mode_tag = "open-loop" if open_loop else "trigger-oracle"
        logf = "/tmp/attack_sync_prb_%ss.log" % dur
        script = (
            preamble +
            'echo "[ROGUE] sync-prb %(dur)ss mode=%(mode)s burst=%(bms)sms pps=%(pps)s '
            'size=%(size)sB rogue=$ROGUE -> ext-dn $EXT:%(port)s (PURE PRB; NOT victim C2)"; '
            'if [ -f /tmp/c2_sync_flood.pid ]; then '
            '_old=$(cat /tmp/c2_sync_flood.pid); '
            'if [ -n "$_old" ]; then kill "$_old" 2>/dev/null || true; sleep 0.2; '
            'kill -9 "$_old" 2>/dev/null || true; fi; fi; '
            'rm -f /tmp/c2_sync_flood.rc /tmp/c2_sync_flood.pid /tmp/sync_skew_rogue.csv; '
            'nohup sh -c "%(ts)s setsid python3 -u /tmp/c2_sync_flood.py%(listen)s '
            '--target "$EXT" --port %(port)s --bind $ROGUE '
            '--burst-ms %(bms)s --pps %(pps)s --size %(size)s --duration %(dur)s'
            '%(ol)s --skew-log /tmp/sync_skew_rogue.csv; '
            'echo \\$? > /tmp/c2_sync_flood.rc" '
            '</dev/null >%(logf)s 2>&1 & '
            'echo $! > /tmp/c2_sync_flood.pid; '
            'sleep 0.4; '
            'kill -0 "$(cat /tmp/c2_sync_flood.pid)" 2>/dev/null '
            '|| { echo "[ROGUE-SYNC] ERROR: flood did not start"; exit 1; }; '
            'echo "[ROGUE-SYNC] detached pid=$(cat /tmp/c2_sync_flood.pid) log=%(logf)s '
            'mode=sync-prb sink=$EXT:%(port)s"; '
            'tail -n +1 -F %(logf)s & TPID=$!; '
            'while [ ! -f /tmp/c2_sync_flood.rc ]; do sleep 1; done; '
            'sleep 0.2; kill $TPID 2>/dev/null || true; '
            'exit "$(cat /tmp/c2_sync_flood.rc)"'
        ) % {
            "dur": dur, "bms": args.sync_burst_ms,
            "pps": args.sync_pps, "size": args.sync_size, "port": port,
            "ts": ts, "logf": logf, "ol": ol_flags, "listen": listen_flags, "mode": mode_tag,
        }
        return _rogue_bash(ctx, ns, script)

    if attack == "combined-c2":
        # Pulse (bursty) + sustained UDP carrier, BOTH on the C2 path (UAV:5760).
        # Unlike 'combined' (which only contends ext-dn), here the carrier
        # keeps the C2-path queue partly full between boluses, so pulse
        # transients start from a deeper backlog -> more HOL/RTO on the
        # MAVLink TCP stream.
        #   aggressive (high cmd_lat/adopt): --carrier-pps 8000+  (avail typically <99%)
        #   gentle (avail>=99%):             --carrier-pps 1500-2500
        cpps, csize = args.carrier_pps, args.carrier_size
        mbps_s = "%.1f" % ((cpps * csize * 8) / 1e6)
        carrier = (
            'echo "[ROGUE] carrier %(dur)ss pps=%(cpps)s size=%(csize)sB '
            '(~%(mbps)s Mbit/s) rogue=$ROGUE -> %(uav)s:%(port)s (C2 sustained)"; '
            '%(ts)s python3 /tmp/udp_flood.py moderate --target %(uav)s --port %(port)s '
            '--bind "$ROGUE" --duration %(dur)s --pps %(cpps)s --size %(csize)s '
            '| tee /tmp/attack_carrier_c2_%(dur)ss.log'
        ) % {"dur": dur, "cpps": cpps, "csize": csize, "mbps": mbps_s,
             "uav": uav_ip, "port": SITL_PORT, "ts": ts}
        pulse = _pulse_script('"%s"' % uav_ip, str(SITL_PORT), dur, bolus, period, pkt, flows,
                              f"UAV {uav_ip}:{SITL_PORT} (C2 pulse)", ts)
        script = (
            preamble +
            'echo "[ROGUE] combined-c2: pulse+carrier both on UAV:%(port)s '
            '(NOT ext-dn; unlike --attack combined)"; ' % {"port": SITL_PORT} +
            pulse + ' > /tmp/attack_pulse_c2_%(dur)ss.log 2>&1 & PID_P=$!; ' % {"dur": dur} +
            carrier + ' & PID_C=$!; ' +
            'wait $PID_P; RC_P=$?; wait $PID_C; RC_C=$?; '
            'echo "combined-c2 done pulse_rc=$RC_P carrier_rc=$RC_C"; '
            'test $RC_P -eq 0 -a $RC_C -eq 0'
        )
        return _rogue_bash(ctx, ns, script)

    raise SystemExit(f"attack mode {attack!r} not wired "
                     "(use pulse|pulse-c2|flood-c2|cbr|combined|combined-c2|sync-c2|sync-prb|"
                     "microburst|dual-pulse|multi-rogue|multi-rogue-dl|multi-rogue-sync|"
                     "multi-rogue-dl-sync)")


def resolve_attack_onset(args, envelope_s: float) -> tuple[float, int]:
    """Return (onset_delay_s, burst_duration_s).

    ``--attack-start-after-s 0`` (default) keeps legacy behaviour: burst for the
    full envelope from process start.  delay>0 sleeps inside the rogue script
    before the first burst and shortens duration so delay+burst fits the envelope.

    The delay is counted from the rogue process start, but the mission only
    starts at the centralization margin ``(duration - mission_len)/2`` — 90 s for
    a 600 s run.  A delay shorter than that margin puts full UL contention on the
    cell *before* the UAV arms, which is how the n=4 600 s run lost SITL during
    takeoff.  When that happens the onset is pushed to ``margin + takeoff guard``;
    configurations that already start after the mission (60 s/180 s) are
    untouched.

    Option C (setpoint-stream, >= 600 s): the burst keeps the legacy value above
    (3.2a, from the base envelope), but the sleep is moved later so the onset
    lands P_PRE after the orbit bound (instrumentation.schedule_optc). Under option C a
    delay of 0 is not a full-envelope burst: it goes through the takeoff guard
    like any delay shorter than the margin.
    """
    delay = float(getattr(args, "attack_start_after_s", 0.0) or 0.0)
    if delay < 0:
        raise SystemExit("--attack-start-after-s must be >= 0")
    env = max(1, int(envelope_s))
    if delay <= 0 and not optc_applies(args):
        return 0.0, env
    mission_start_s = attack_mission_start_s(args)
    if mission_start_s and delay < mission_start_s:
        delay = mission_start_s + ATTACK_TAKEOFF_GUARD_S
    delay_ceil = int(math.ceil(delay))
    if delay_ceil >= env:
        raise SystemExit(
            f"--attack-start-after-s ({delay:g}s) must be < envelope ({env}s)"
        )
    burst = max(1, env - delay_ceil)
    if optc_applies(args):
        delay = max(delay, float(optc_schedule(args)["onset_delay_s"]))
    return delay, burst


def optc_attack_onset_mismatch(args) -> str | None:
    """Option C attack runs must launch the rogues with the planned (sleep, burst).

    Returns a message when resolve_attack_onset() diverges from schedule_optc;
    None when consistent or when option C / rogue timing does not apply.
    """
    if (
        getattr(args, "condition", None) == "baseline"
        or not optc_applies(args)
        or getattr(args, "attack", None) not in (*MULTI_ROGUE_ATTACKS, "microburst")
    ):
        return None
    sched = optc_schedule(args)
    delay, burst = resolve_attack_onset(args, float(sched["base_envelope_s"]))
    if (abs(float(delay) - float(sched["onset_delay_s"])) > 1e-6
            or int(burst) != int(sched["burst_s"])):
        return (
            f"option C attack timing mismatch: effective sleep={float(delay):g}s "
            f"burst={int(burst)}s vs planned sleep={float(sched['onset_delay_s']):g}s "
            f"burst={int(sched['burst_s'])}s"
        )
    return None


def attack_mission_start_s(args) -> float:
    """Seconds from T0 until mission_runner is launched (centralization margin)."""
    try:
        return float(mission_plan(int(getattr(args, "duration", 0) or 0))[2])
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _attack_onset_sleep_fragment(delay_s: float) -> str:
    if delay_s <= 0:
        return ""
    return (
        f'echo "[ATTACK] onset delay {delay_s:g}s (post-takeoff window)"; '
        f"sleep {delay_s:g}; "
    )


def multi_rogue_attack_commands(
    ctx, ns, args, envelope_s, run_id: str = "",
) -> list[tuple[str, list[str]]]:
    """One microburst command per rogue UE pod, staggered phase for PF fairness fragmentation."""
    count = max(1, int(args.multi_rogue_count))
    deploys = rogue_deploy_names(count)
    delay_s, burst_s = resolve_attack_onset(args, envelope_s)
    dur = str(burst_s)
    ts = attack_taskset(ctx, ns)
    preamble = (
        'set -euo pipefail; . /tmp/tm_common.sh; '
        'ROGUE="$(tm_pdu_ip)"; EXT="$(tm_ext_dn_ip)"; '
        'if [ -z "$EXT" ]; then echo "[ROGUE] ERROR: cannot resolve oai-ext-dn.core.svc.cluster.local"; exit 1; fi; '
    )
    onset = _attack_onset_sleep_fragment(delay_s)
    phases = multi_rogue_phase_offsets(count, args.multi_rogue_phase_ms)
    out: list[tuple[str, list[str]]] = []
    for idx, deploy in enumerate(deploys):
        phase_ms = phases[idx]
        script = (
            preamble
            + onset
            + _microburst_script_fragment(args, dur, ts, run_id, phase_ms=phase_ms, seed_offset=idx)
            + f' | tee /tmp/attack_multirogue_{idx}_{dur}s.log'
        )
        out.append((deploy, _rogue_bash_deploy(ctx, ns, deploy, script)))
    return out


def multi_rogue_sync_attack_commands(
    ctx, ns, args, envelope_s, uav_ip: str, gcs_ip: str,
) -> list[tuple[str, list[str]]]:
    """N aggressive synchronized UL emitters; every PDU flow terminates at ext-DN."""
    ext_ip = (
        kctl(
            ctx, "get", "pod", "-n", "core", "-l", "app=oai-ext-dn",
            "-o", "jsonpath={.items[0].status.podIP}",
        ).stdout or ""
    ).strip()
    assert_attack_avoids_victim_endpoint(
        "multi-rogue-sync", target_ip=ext_ip,
        target_port=EXT_DN_IPERF_PORT_ATTACK,
        uav_ip=uav_ip, gcs_ip=gcs_ip,
    )
    count = max(1, int(args.multi_rogue_count))
    delay_s, burst_s = resolve_attack_onset(args, envelope_s)
    ts = attack_taskset(ctx, ns)
    open_loop = sync_open_loop_enabled(args)
    mode_tag = "open-loop" if open_loop else "trigger-oracle"
    out: list[tuple[str, list[str]]] = []
    for idx, deploy in enumerate(rogue_deploy_names(count)):
        # All rogues align to the same predictable C2 phase; unlike the legacy
        # fairness-fragmentation attack, this mode intentionally has no stagger.
        phase_ms = float(args.sync_phase_ms)
        ol_flags = (
            f" --open-loop --rate-hz {args.setpoint_rate_hz} --phase-ms {phase_ms:g}"
            if open_loop else ""
        )
        listen_flags = "" if open_loop else f" --listen-port {args.sync_listen_port}"
        script = (
            'set -euo pipefail; . /tmp/tm_common.sh; '
            'ROGUE="$(tm_pdu_ip)"; EXT="$(tm_ext_dn_ip)"; '
            'if [ -z "$EXT" ]; then echo "[ROGUE] ERROR: ext-dn missing"; exit 1; fi; '
            + _attack_onset_sleep_fragment(delay_s)
            + f'echo "[ROGUE] multi-rogue-sync idx={idx} mode={mode_tag} '
              f'burst={args.multi_sync_burst_ms:g}ms pps={args.multi_sync_pps} '
              f'size={args.multi_sync_size}B '
              'rogue=$ROGUE -> ext-dn $EXT:5201 (PURE UL PRB; NOT victim C2)"; '
            + f'{ts}python3 -u /tmp/c2_sync_flood.py{listen_flags} '
              f'--target "$EXT" --port {EXT_DN_IPERF_PORT_ATTACK} --bind "$ROGUE" '
              f'--burst-ms {args.multi_sync_burst_ms:g} --pps {args.multi_sync_pps} '
              f'--size {args.multi_sync_size} --duration {burst_s}{ol_flags} '
              f'--skew-log /tmp/sync_skew_multirogue_{idx}.csv'
        )
        out.append((deploy, _rogue_bash_deploy(ctx, ns, deploy, script)))
    return out


def _multi_rogue_dl_targets(
    ctx, ns, args, uav_ip: str, gcs_ip: str, attack_label: str,
) -> list[str]:
    """Resolve rogue PDU IPs and reject victim / non-slice-A destinations."""
    targets: list[str] = []
    for deploy in rogue_deploy_names(max(1, int(args.multi_rogue_count))):
        target = pdu_ip(ctx, ns, deploy) or ""
        assert_attack_avoids_victim_endpoint(
            attack_label, target_ip=target, target_port=5203,
            uav_ip=uav_ip, gcs_ip=gcs_ip,
        )
        if not ip_in_cidr(target, SUBNET_SLICE_A_CIDR):
            raise SystemExit(
                f"{attack_label}: destination {target!r} of {deploy} is not in the "
                f"attacker pool {SUBNET_SLICE_A_CIDR}"
            )
        targets.append(target)
    return targets


def multi_rogue_dl_command(
    ctx, ns, args, envelope_s, uav_ip: str, gcs_ip: str, run_id: str,
) -> tuple[list[str], list[str]]:
    """One ext-DN sender fans DL bursts out to all rogue PDU addresses."""
    targets = _multi_rogue_dl_targets(
        ctx, ns, args, uav_ip, gcs_ip, "multi-rogue-dl",
    )
    delay_s, burst_s = resolve_attack_onset(args, envelope_s)
    target_flags = " ".join(f'--target "{target}"' for target in targets)
    script = (
        "set -euo pipefail; "
        + _attack_onset_sleep_fragment(delay_s)
        + f'echo "[EXT-DN] multi-rogue-dl N={len(targets)} '
          f'{args.mean_rate_mbps:g}Mbps/target -> rogue PDU pool '
          '(PURE DL PRB; NOT victim C2)"; '
        + f'python3 -u /tools/co_tenant_burst_sender.py {target_flags} '
          f'--port 5203 --duration {burst_s} --period-ms {args.period_ms} '
          f'--burst-bytes {args.burst_size_bytes} '
          f'--mbps-per-target {args.mean_rate_mbps:g} --mode multi-rogue-dl '
          f'--run-id "{run_id}" --out-summary /tmp/multi_rogue_dl_summary.json'
    )
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", "core",
        "deploy/oai-ext-dn", "-c", "ext-dn", "--",
        "bash", "-o", "pipefail", "-c", script,
    ]
    return cmd, targets


def multi_rogue_dl_sync_command(
    ctx, ns, args, envelope_s, uav_ip: str, gcs_ip: str, run_id: str,
) -> tuple[list[str], list[str]]:
    """Ext-DN DL peaks to rogue PDU pool, timed to the setpoint rhythm."""
    targets = _multi_rogue_dl_targets(
        ctx, ns, args, uav_ip, gcs_ip, "multi-rogue-dl-sync",
    )
    delay_s, burst_s = resolve_attack_onset(args, envelope_s)
    open_loop = sync_open_loop_enabled(args)
    target_flags = " ".join(f'--target "{target}"' for target in targets)
    ol_flags = (
        f" --open-loop --rate-hz {args.setpoint_rate_hz} --phase-ms {args.sync_phase_ms:g}"
        if open_loop else ""
    )
    listen_flags = "" if open_loop else f" --listen-port {args.sync_listen_port}"
    mode_tag = "open-loop" if open_loop else "trigger-oracle"
    script = (
        "set -euo pipefail; "
        + _attack_onset_sleep_fragment(delay_s)
        + f'echo "[EXT-DN] multi-rogue-dl-sync N={len(targets)} mode={mode_tag} '
          f'burst={args.multi_sync_burst_ms:g}ms pps={args.multi_sync_pps} '
          f'size={args.multi_sync_size}B -> rogue PDU pool '
          '(PURE DL PRB sync; NOT victim C2)"; '
        + f'python3 -u /tools/co_tenant_burst_sender.py {target_flags} '
          f'--port 5203 --duration {burst_s} --mode multi-rogue-dl-sync '
          f'--burst-ms {args.multi_sync_burst_ms:g} --pps {args.multi_sync_pps} '
          f'--size {args.multi_sync_size}{ol_flags}{listen_flags} '
          f'--run-id "{run_id}" --out-summary /tmp/multi_rogue_dl_sync_summary.json'
    )
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", "core",
        "deploy/oai-ext-dn", "-c", "ext-dn", "--",
        "bash", "-o", "pipefail", "-c", script,
    ]
    return cmd, targets


def fetch_multi_rogue_sync_skew(
    ctx: str, ns: str, count: int, run_dir: Path,
) -> None:
    """Collect all rogue skew traces and create the standard merged CSV."""
    merged: list[str] = []
    for idx, deploy in enumerate(rogue_deploy_names(count)):
        txt = fetch_file(
            ctx, ns, deploy, "nr-ue", f"/tmp/sync_skew_multirogue_{idx}.csv",
        )
        if not txt:
            continue
        (run_dir / f"sync_skew_{deploy}.csv").write_text(txt, encoding="utf-8")
        lines = txt.splitlines()
        if not lines:
            continue
        if not merged:
            merged.extend(lines)
        else:
            merged.extend(lines[1:])
    if merged:
        (run_dir / "sync_skew_rogue.csv").write_text(
            "\n".join(merged) + "\n", encoding="utf-8",
        )


def audit_rogue_attack_idle(ctx: str, ns: str, multi_count: int = 1) -> tuple[bool, str]:
    """Return (ok, reason) checking all rogue pods for stale attack processes."""
    pgrep_pat = (
        "pgrep -f '[c]o_tenant_load|[i]perf3|[r]ogue_pulse|[c]2_sync_flood' "
        ">/dev/null && echo busy || echo free"
    )
    for deploy in rogue_deploy_names(multi_count):
        r = kctl(ctx, "exec", "-n", ns, f"deploy/{deploy}", "-c", "nr-ue", "--", "sh", "-c", pgrep_pat)
        state = (r.stdout or "").strip()
        if "busy" in state:
            return False, f"stale attack process on {deploy}"
    return True, ""


# --------------------------------------------------------------------------- #
# single run
# --------------------------------------------------------------------------- #
def run_single(args, ctx, ns, ips, run_id, seed):
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    run_id = clamp_run_dirname(run_id, out_root)
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # Image digests via kubectl at run start (always records the reason on failure).
    try:
        _img = collect_image_digests(
            context=ctx, namespace=ns,
            core_namespace=getattr(args, "core_namespace", CORE_NAMESPACE_DEFAULT),
            dry_run=False,
        )
        write_image_digests(run_dir, _img)
        if _img.get("ok"):
            print(
                f"  [images] {_img['digests']['n_unique_imageIDs']} imageIDs → "
                f"{run_dir.name}/image_digests.json",
                flush=True,
            )
        else:
            print(f"  [images] WARN: {_img.get('error')}", flush=True)
    except Exception as _img_exc:
        write_image_digests(run_dir, {
            "schema": "flyblind_image_digests_v2",
            "ok": False,
            "error": f"exception while collecting: {_img_exc}",
            "digests": None,
        })
        print(f"  [images] ERROR: {_img_exc}", flush=True)
    wall_t0 = time.monotonic()
    CURRENT_RUN.update({"run_id": run_id, "run_dir": run_dir, "wall_t0": wall_t0, "args": args})
    gcs_ip, uav_ip = ips["gcs_ip"], ips["uav_ip"]
    duration = args.duration
    warmup = args.warmup
    mission_len, n_cmds, margin, interval_s = mission_params(args, duration)
    is_baseline = args.condition == "baseline"
    # base_envelope_s drives attack timing (never extended by drain).
    # envelope_s is the capture envelope (all capture layers and watchdogs). It is
    # extended by the drain ceiling and, under option C, up to the planned capture
    # end. The trajectory is not derived from it (effective_traj_duration_s).
    base_envelope_s = float(duration + warmup + 20)
    envelope_s = base_envelope_s
    drain_ceiling_applied = 0.0
    if getattr(args, "drain_until_baseline", False) and not is_baseline:
        drain_ceiling_applied = float(getattr(args, "drain_ceiling_s", 180.0) or 180.0)
        envelope_s = base_envelope_s + drain_ceiling_applied
        print(
            f"  [drain] envelope extended by {drain_ceiling_applied:g}s "
            f"(ceiling; criterion in drain_policy.json)",
            flush=True,
        )
    if optc_applies(args):
        _optc = optc_schedule(args)
        envelope_s = max(envelope_s, float(_optc["capture_envelope_s"]))
        if not is_baseline and args.attack in (*MULTI_ROGUE_ATTACKS, "microburst"):
            _eff_delay, _eff_burst = resolve_attack_onset(args, base_envelope_s)
            _optc_eff = f"effective sleep={_eff_delay:g}s burst={_eff_burst}s"
        else:
            _optc_eff = "effective=n/a (baseline, virtual onset)"
        print(
            f"  [optc] plan onset>={_optc['planned_onset_s'][0]:g}s "
            f"sleep={_optc['onset_delay_s']:g}s burst={_optc['burst_s']}s | {_optc_eff} | "
            f"traj_end={_optc['trajectory_end_s']:g}s capture={int(envelope_s)}s (s since T0)",
            flush=True,
        )
    proxy_watchdog = proxy_watchdog_s(args, envelope_s, mission_len)
    mission_timeout_s = mission_wait_timeout_s(args, mission_len, envelope_s)
    traj_active_s = (
        effective_traj_duration_s(args, mission_len, envelope_s)
        if args.workload == "setpoint-stream" else None
    )

    # Per-run mission JSON pushed into the GCS pod.
    mission_name = f"tm_{args.condition}_{duration}s"
    mission_json = build_mission_json(mission_name, mission_len, n_cmds, seed, interval_s=interval_s)
    pod_mission_path = f"/tmp/mission_{run_id}.json"
    pod_mission_out = f"/tmp/mission_{run_id}"
    if not put_file(ctx, ns, "oai-ue-gcs", "nr-ue", pod_mission_path,
                    json.dumps(mission_json) + "\n"):
        raise SystemExit("failed to stage mission JSON into GCS pod")

    attack_delay = None
    attack_burst = None
    mission_start = attack_mission_start_s(args)
    if (
        not is_baseline
        and (float(getattr(args, "attack_start_after_s", 0.0) or 0.0) > 0 or optc_applies(args))
        and args.attack in (*MULTI_ROGUE_ATTACKS, "microburst")
    ):
        # 3.2a: burst must be identical with and without --drain-until-baseline.
        attack_delay, attack_burst = resolve_attack_onset(args, base_envelope_s)
    print_run_header(
        args,
        run_id,
        run_dir,
        duration=duration,
        warmup=warmup,
        envelope_s=envelope_s,
        mission_len=mission_len,
        trajectory_active_s=traj_active_s,
        proxy_watchdog_s=proxy_watchdog,
        attack_delay_s=attack_delay,
        attack_burst_s=attack_burst,
        mission_start_s=mission_start,
    )
    if args.c2_proxy:
        print(f"  [c2-proxy] GCS -> UAV:{SITL_PORT} -> SITL:{SITL_BACKEND_PORT}")
        print(f"           watchdog={proxy_watchdog}s (max only; shutdown after mission)")

    try:
        return _run_single_body(
            args, ctx, ns, ips, run_id, seed, run_dir, wall_t0, is_baseline,
            duration, warmup, mission_len, n_cmds, margin, interval_s, envelope_s,
            proxy_watchdog, mission_timeout_s, traj_active_s,
            gcs_ip, uav_ip, pod_mission_path, pod_mission_out,
            base_envelope_s,
        )
    except KeyboardInterrupt:
        audit = audit_post_run_cleanup(ctx, ns, args, need_rogue=not is_baseline)
        ports = check_uav_mavlink_ports(ctx, ns, args.c2_proxy)
        write_keyboard_interrupt_run_meta(
            run_dir, run_id, args, wall_t0=wall_t0,
            cleanup_audit=audit, port_check=ports,
        )
        raise


def _run_single_body(
    args, ctx, ns, ips, run_id, seed, run_dir, wall_t0, is_baseline,
    duration, warmup, mission_len, n_cmds, margin, interval_s, envelope_s,
    proxy_watchdog, mission_timeout_s, traj_active_s,
    gcs_ip, uav_ip, pod_mission_path, pod_mission_out,
    base_envelope_s,
):
    # 3.2a: attack timing/burst derives from the BASE envelope (never extended by the
    # drain ceiling), computed once in run_single(). envelope_s stays extended for
    # telemetry/trajectory/watchdogs.
    attack_envelope_s = float(base_envelope_s)
    pod_proxy_out = f"/tmp/proxy_{run_id}"
    prb_raw = run_dir / "mac-prb-raw.log"
    log_handles: list[Any] = []
    proxy_proc = host_proc = upf_proc = probe_proc = prb_proc = attack_proc = sink_proc = mission_proc = None
    companion_proc = None
    arrival_forwarder_proc = None
    aqm_sampler: AqmQdiscSampler | None = None
    aqm_queue_audit: dict[str, Any] | None = None
    pod_arrival_csv = f"/tmp/c2_arrival_{run_id}.csv"
    pod_telemetry_uav_csv = f"/tmp/c2_telemetry_uav_{run_id}.csv"
    attack_procs: list[subprocess.Popen] = []
    proxy_rc = host_rc = upf_rc = probe_rc = prb_rc = attack_rc = sink_rc = mission_rc = None
    companion_rc = None
    companion_summary: dict[str, Any] | None = None
    proxy_shutdown_info: dict[str, Any] = {}
    proxy_started_mono: float | None = None
    proxy_stopped_mono: float | None = None
    prb_started_mono: float | None = None
    prb_wait_timeout_s: float | None = None
    prb_wait_status: str | None = None
    mission_cmd: list[str] = []
    T0 = time.monotonic_ns()
    T0_unix = time.time_ns()
    (run_dir / "clock_anchor.json").write_text(
        json.dumps({
            "schema": "flyblind_clock_anchor_v2",
            "T0_mono_ns": T0,
            "T0_unix_ns": T0_unix,
            "written_unix_ns": time.time_ns(),
            "event_markers_clock": "host monotonic; t_rel_s = (t_mono - T0_mono_ns)/1e9",
            "proxy_link_clock": (
                "pod monotonic == host when time_namespace_match; "
                "t_rel_s = (t_arrival_ns - T0_mono_ns)/1e9"
            ),
            "t0_unix_ns_role": "metadata only (not used for phase slice of proxy_link)",
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    offsets = {}
    loadavg_before = read_loadavg1()
    companion_mbps = companion_video_mbps(args)
    sidecar_proc = None
    sidecar_profile = None
    try:
        sidecar_profile = normalize_sidecar_profile(getattr(args, "sidecars", "off"))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    video_stop_t_rel_s = None
    video_stop_thread = None
    args._video_stop_t_rel_s = None  # noqa: SLF001 — paper_artifacts marker

    def _layer_procs():
        extra = attack_procs if attack_procs else ([attack_proc] if attack_proc is not None else [])
        return (
            mission_proc, *extra, companion_proc, sink_proc, prb_proc, probe_proc,
            host_proc, upf_proc, proxy_proc, arrival_forwarder_proc,
        )

    def _start(cmd: list[str], log_name: str) -> subprocess.Popen:
        proc, logf = _popen_log(cmd, run_dir / log_name)
        log_handles.append(logf)
        return proc

    try:
        if sidecar_profile:
            sc_dur = sidecar_duration_s(
                envelope_s, getattr(args, "sidecars_duration_s", None),
            )
            offsets["sidecars_ns"] = time.monotonic_ns() - T0
            sidecar_proc, _sc_out = start_sidecars(
                run_dir=run_dir,
                profile=sidecar_profile,
                duration_s=sc_dur,
                context=ctx,
                namespace=ns,
                gcs_ip=gcs_ip or "",
                uav_ip=uav_ip or "",
            )
            print(
                f"  [sidecars] profile={sidecar_profile} duration={sc_dur:g}s "
                f"out={run_dir / 'sidecars'}",
                flush=True,
            )
        if args.c2_proxy:
            _prepare_proxy_run_dir(ctx, ns, pod_proxy_out)
            stop_file = f"{pod_proxy_out}/STOP"
            proxy_cmd = [
                "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
                "env", f"PYTHONPATH={C2_RUN_TOOLS}",
                "python3", "-u", f"{C2_RUN_TOOLS}/mavlink_proxy.py",
                "--listen-host", uav_ip,
                "--listen-port", str(SITL_PORT),
                "--sitl-host", "127.0.0.1",
                "--sitl-port", str(SITL_BACKEND_PORT),
                "--watchdog-s", str(proxy_watchdog),
                "--stop-file", stop_file,
                "--out", pod_proxy_out,
                "--t0-mono-ns", str(T0),
                "--t0-unix-ns", str(T0_unix),
            ]
            offsets["proxy_ns"] = time.monotonic_ns() - T0
            proxy_started_mono = time.monotonic()
            proxy_proc = _start(proxy_cmd, "proxy.live.log")
            if proxy_proc.poll() is not None:
                raise RuntimeError("MAVLink proxy exited immediately after start")
            wait_for_proxy_ready(ctx, ns)
            # Let the proxy finish its backend TCP handshake before GCS connects.
            time.sleep(2.0)

        if HOST_TELEMETRY.is_file():
            host_cmd = [
                sys.executable, str(HOST_TELEMETRY),
                "--duration", str(int(envelope_s)),
                "--interval", "1.0",
                "--out-csv", str(run_dir / "host_timeseries.csv"),
                "--out-summary", str(run_dir / "host_summary.json"),
            ]
            offsets["host_telemetry_ns"] = time.monotonic_ns() - T0
            host_proc = _start(host_cmd, "host_telemetry.live.log")

        # Shared-UPF attribution: sample oai-upf CPU for BL and attack (soft; does not invalidate).
        if (
            not getattr(args, "skip_upf_telemetry", False)
            and UPF_TELEMETRY.is_file()
        ):
            upf_cmd = [
                sys.executable, str(UPF_TELEMETRY),
                "--duration", str(int(envelope_s)),
                "--interval", "1.0",
                "--context", ctx,
                "--core-namespace", getattr(args, "core_namespace", CORE_NAMESPACE_DEFAULT),
                "--saturate-pct", str(getattr(args, "upf_cpu_saturate_pct", UPF_CPU_SATURATE_PCT_DEFAULT)),
                "--out-csv", str(run_dir / "upf_timeseries.csv"),
                "--out-summary", str(run_dir / "upf_summary.json"),
            ]
            offsets["upf_telemetry_ns"] = time.monotonic_ns() - T0
            upf_proc = _start(upf_cmd, "upf_telemetry.live.log")

        # AQM ablation: sample UAV PDU qdisc stats for the full envelope so we
        # can claim local-queue health independently of C2 staleness.
        if getattr(args, "mitigation_aqm_ul", False):
            runtime_aqm = (
                (getattr(args, "_runtime_mitigation_audit", None) or {}).get("aqm_ul") or {}
            )
            aqm_iface = runtime_aqm.get("interface") or (
                "oaitun_ue1"
            )
            sample_hz = float(getattr(args, "aqm_ul_sample_hz", 1.0) or 1.0)
            aqm_sampler = AqmQdiscSampler(
                ctx,
                ns,
                str(aqm_iface),
                run_dir / "aqm_qdisc_timeseries.jsonl",
                interval_s=(1.0 / sample_hz) if sample_hz > 0 else 1.0,
            )
            offsets["aqm_qdisc_sampler_ns"] = time.monotonic_ns() - T0
            aqm_sampler.start()
            print(
                f"  [aqm] sampling tc -s on {aqm_iface} @ {sample_hz:g} Hz → "
                f"{run_dir.name}/aqm_qdisc_timeseries.jsonl",
                flush=True,
            )

        # 1) NETWORK probe — clock handshake first; blocks until probe_ready.json exists.
        ready_path = run_dir / "probe_ready.json"
        probe_cmd = [
            sys.executable, str(OWD_PROBE),
            "--gcs-ip", gcs_ip, "--uav-ip", uav_ip,
            "--duration", str(duration), "--warmup", str(warmup),
            "--min-samples", str(args.min_samples),
            "--context", ctx, "--namespace", ns,
            "--out", str(run_dir / "network.json"),
            "--ready-out", str(ready_path),
        ]
        offsets["network_ns"] = time.monotonic_ns() - T0
        probe_proc = _start(probe_cmd, "probe.live.log")
        wait_for_probe_ready(ready_path, timeout_s=30.0)
        offsets["probe_ready_ns"] = time.monotonic_ns() - T0

        if getattr(args, "c2_arrival_capture", False):
            if not stage_arrival_forwarder(ctx, ns):
                raise RuntimeError("failed to stage c2_arrival_forwarder.py on oai-ue-uav")
            stop_socat_udp5760(ctx, ns)
            time.sleep(0.5)
            offsets["c2_arrival_capture_ns"] = time.monotonic_ns() - T0
            arrival_forwarder_proc = _start(
                build_arrival_forwarder_cmd(
                    ctx, ns, run_id, envelope_s, pod_arrival_csv,
                    pod_telemetry_uav_csv if getattr(args, "telemetry_arrival_capture", False) else None,
                    t0_mono_ns=T0,
                    t0_unix_ns=T0_unix,
                ),
                "c2_arrival.live.log",
            )
            if arrival_forwarder_proc.poll() is not None:
                restore_socat_udp5760(ctx, ns)
                raise RuntimeError("C2 arrival forwarder exited immediately after start")

        # 2) MECHANISM — PRB capture (after clock handshake confirmed).
        prb_cmd = [sys.executable, str(MAC_PRB), "capture", ctx, ns, str(int(envelope_s)), str(prb_raw)]
        offsets["prb_ns"] = time.monotonic_ns() - T0
        prb_started_mono = time.monotonic()
        prb_proc = _start(prb_cmd, "prb.live.log")

        # 3) ATTACK — after probe ready; microburst also starts ext-DN UDP sink on :5202.
        #    Companion video = UAV→GCS :5202 (BVLOS camera); independent of the ext-dn sink.
        if companion_mbps > 0:
            offsets["companion_video_ns"] = time.monotonic_ns() - T0
            _video_shape = getattr(args, "companion_video_shape", "iperf")
            ensure_companion_video_server(ctx, ns, _video_shape)
            companion_gcs_ip = gcs_ip
            companion_uav_ip = None
            companion_proc = _start(
                companion_video_command(
                    ctx,
                    ns,
                    companion_mbps,
                    envelope_s,
                    run_id,
                    companion_gcs_ip,
                    companion_uav_ip,
                    _video_shape,
                ),
                "companion_video.live.log",
            )
            # DRAIN (b) video_stop is scheduled AFTER the attack actually starts, using
            # the real offsets["attack_ns"]. See the block below.
        if not is_baseline:
            if args.attack in ("microburst", "dual-pulse", "multi-rogue"):
                sink_proc, sink_logf = start_co_tenant_sink(ctx, run_id, envelope_s, run_dir / "sink.live.log")
                log_handles.append(sink_logf)
                wait_for_sink_ready(ctx)
            dl_burst_proc = None
            if args.attack == "dual-pulse":
                rogue_ip = pdu_ip(ctx, ns, "oai-ue-rogue") or ""
                if not rogue_ip:
                    raise RuntimeError("dual-pulse: ROGUE PDU IP missing")
                dl_burst_proc, dl_logf = start_ext_dn_dl_burst(
                    ctx, rogue_ip, args, attack_envelope_s, run_id, run_dir / "dl_burst.live.log",
                )
                log_handles.append(dl_logf)
            offsets["attack_ns"] = time.monotonic_ns() - T0
            if args.attack == "multi-rogue":
                for deploy, cmd in multi_rogue_attack_commands(ctx, ns, args, attack_envelope_s, run_id):
                    attack_procs.append(_start(cmd, f"attack.{deploy}.live.log"))
            elif args.attack == "multi-rogue-sync":
                for deploy, cmd in multi_rogue_sync_attack_commands(
                    ctx, ns, args, attack_envelope_s, uav_ip, gcs_ip,
                ):
                    attack_procs.append(_start(cmd, f"attack.{deploy}.live.log"))
            elif args.attack == "multi-rogue-dl":
                cmd, _targets = multi_rogue_dl_command(
                    ctx, ns, args, attack_envelope_s, uav_ip, gcs_ip, run_id,
                )
                attack_proc = _start(cmd, "attack.live.log")
            elif args.attack == "multi-rogue-dl-sync":
                cmd, _targets = multi_rogue_dl_sync_command(
                    ctx, ns, args, attack_envelope_s, uav_ip, gcs_ip, run_id,
                )
                attack_proc = _start(cmd, "attack.live.log")
            else:
                attack_proc = _start(
                    attack_command(ctx, ns, args, attack_envelope_s, uav_ip, gcs_ip, run_id),
                    "attack.live.log",
                )
            if dl_burst_proc is not None:
                _wait_proc(dl_burst_proc, timeout=envelope_s + 30)
        else:
            offsets["attack_ns"] = None

        # DRAIN (b): stop the companion video at the REAL attack_end
        # (offsets["attack_ns"] + onset + burst), matching the attack_end marker,
        # not an estimate made before the attack process was launched.
        if (
            getattr(args, "video_stop_at_attack_end", False)
            and not is_baseline
            and companion_proc is not None
            and offsets.get("attack_ns") is not None
        ):
            _onset, _burst = resolve_attack_onset(args, attack_envelope_s)
            real_attack_end = offsets["attack_ns"] / 1e9 + float(_onset) + float(_burst)

            def _stop_video():
                try:
                    if companion_proc is not None and companion_proc.poll() is None:
                        companion_proc.terminate()
                except Exception:
                    pass
                try:
                    stop_companion_video_server(ctx, ns)
                except Exception:
                    pass

            def _mark_video_stop(t_rel):
                nonlocal video_stop_t_rel_s
                video_stop_t_rel_s = float(t_rel)
                args._video_stop_t_rel_s = video_stop_t_rel_s  # noqa: SLF001

            video_stop_thread = schedule_video_stop_at_attack_end(
                T0_mono_ns=T0,
                attack_end_t_rel_s=real_attack_end,
                stop_fn=_stop_video,
                on_mark=_mark_video_stop,
            )
            print(
                f"  [drain-b] video_stop scheduled at real attack_end "
                f"t_rel={real_attack_end:.1f}s",
                flush=True,
            )

        # 4) C2 mission — launched at the centralization offset, inside the envelope.
        elapsed_s = (time.monotonic_ns() - T0) / 1e9
        if margin - elapsed_s > 0:
            time.sleep(margin - elapsed_s)
        _mission_env = [f"PYTHONPATH={C2_RUN_TOOLS}"]
        for _ek in ("PREPOSITION_ORBIT_ENTRY",):
            if _ek in os.environ and str(os.environ.get(_ek, "")).strip() != "":
                _mission_env.append(f"{_ek}={os.environ[_ek]}")
        mission_cmd = [
            "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
            "env", *_mission_env,
            "python3", "-u", f"{C2_RUN_TOOLS}/mission_runner.py",
            "--connection", build_gcs_connection(uav_ip, args.c2_transport, args.c2_proxy), "--bind-ip", gcs_ip,
            "--mission", pod_mission_path, "--thresholds", "/tmp/thresholds.json",
            "--out", pod_mission_out,
            "--workload", args.workload,
        ]
        if args.workload == "setpoint-stream":
            traj_dur = effective_traj_duration_s(args, mission_len, envelope_s)
            mission_cmd += [
                "--setpoint-rate-hz", str(args.setpoint_rate_hz),
                "--trajectory", args.trajectory,
                "--trajectory-duration-s", str(traj_dur),
                "--trajectory-speed-mps", str(args.trajectory_speed_mps),
                "--trajectory-radius-m", str(args.trajectory_radius_m),
                "--trajectory-amplitude-m", str(args.trajectory_amplitude_m),
                "--trajectory-period-s", str(args.trajectory_period_s),
                "--setpoint-seed", str(args.setpoint_seed),
                "--setpoint-yaw-mode", args.setpoint_yaw_mode,
            ]
            if args.stream_cut_ms is not None:
                mission_cmd += ["--stream-cut-ms", str(args.stream_cut_ms)]
                mission_cmd += ["--stream-cut-start-s", str(args.stream_cut_start_s)]
        if getattr(args, "telemetry_arrival_capture", False):
            mission_cmd += ["--telemetry-arrival-capture"]
        if not is_baseline and is_sync_style_attack(args.attack) and not sync_open_loop_enabled(args):
            if args.attack == "multi-rogue-dl-sync":
                # Trigger arrives on ext-DN CNI (sender listens); DL goes to rogue PDUs.
                ext_cni = pod_cni_ip(ctx, "core", "oai-ext-dn")
                if ext_cni:
                    mission_cmd += [
                        "--attack-trigger", f"{ext_cni}:{args.sync_listen_port}",
                    ]
                else:
                    print(
                        f"  [warn] {args.attack}: could not resolve oai-ext-dn CNI IP — "
                        "trigger disabled (burst will not fire)"
                    )
            else:
                trigger_deploys = (
                    rogue_deploy_names(args.multi_rogue_count)
                    if args.attack == "multi-rogue-sync"
                    else ["oai-ue-rogue"]
                )
                trigger_endpoints = [
                    f"{ip}:{args.sync_listen_port}"
                    for deploy in trigger_deploys
                    if (ip := pod_cni_ip(ctx, ns, deploy))
                ]
                if len(trigger_endpoints) == len(trigger_deploys):
                    mission_cmd += ["--attack-trigger", ",".join(trigger_endpoints)]
                else:
                    print(
                        f"  [warn] {args.attack}: could not resolve all ROGUE CNI IPs — "
                        "trigger disabled (burst will not fire)"
                    )
        elif not is_baseline and sync_open_loop_enabled(args):
            print(
                f"  [sync] {args.attack} open-loop: no --attack-trigger "
                f"(rate={args.setpoint_rate_hz}Hz phase={args.sync_phase_ms}ms)"
            )
        offsets["mission_ns"] = time.monotonic_ns() - T0
        mission_proc = _start(mission_cmd, "mission.live.log")

        # --- wait for completions ---
        mission_rc = _wait_proc(mission_proc, timeout=mission_timeout_s)

        if proxy_proc is not None:
            proxy_rc, proxy_shutdown_info = shutdown_uav_proxy(
                ctx, ns, pod_proxy_out, proxy_proc, run_dir,
            )
            proxy_stopped_mono = time.monotonic()
            proxy_proc = None

        if attack_procs:
            attack_rc = 0
            for proc in attack_procs:
                rc = _wait_proc(proc, timeout=envelope_s + 60)
                if rc not in (0, None):
                    attack_rc = rc
                    break
            if args.attack == "multi-rogue-sync":
                fetch_multi_rogue_sync_skew(
                    ctx, ns, args.multi_rogue_count, run_dir,
                )
        elif attack_proc is not None:
            attack_rc = _wait_proc(attack_proc, timeout=envelope_s + 60)
            # Detached c2_sync_flood lives on ROGUE; DL-sync runs on ext-DN in-process.
            if is_sync_style_attack(args.attack) and args.attack != "multi-rogue-dl-sync":
                attack_rc = finalize_sync_c2(
                    ctx, ns, attack_rc, timeout_s=180.0,
                    dest_log=run_dir / "attack.live.log",
                )
                fetch_sync_skew_rogue(ctx, ns, run_dir / "sync_skew_rogue.csv")

        if companion_proc is not None:
            companion_rc = _wait_proc(companion_proc, timeout=envelope_s + 60)

        if sink_proc is not None:
            sink_rc = _wait_proc(sink_proc, timeout=envelope_s + 30)

        prb_wait_timeout_s = prb_capture_wait_timeout_s(prb_started_mono, envelope_s)
        prb_rc, prb_wait_status = _wait_proc_no_kill(prb_proc, timeout=prb_wait_timeout_s)
        if prb_wait_status == "timeout":
            print(
                "  [warn] PRB capture did not finish in %.0fs "
                "(window=%ds + grace=%ds from start) — terminating"
                % (prb_wait_timeout_s, int(envelope_s), int(PRB_CAPTURE_GRACE_S))
            )
            _terminate_layer_procs(prb_proc)
            prb_rc = prb_proc.returncode if prb_proc is not None else None
        elif prb_wait_status == "exited" and prb_rc == 0:
            prb_wait_status = "completed"

        probe_rc = _wait_proc(probe_proc, timeout=duration + 300)

        if arrival_forwarder_proc is not None:
            _wait_proc(arrival_forwarder_proc, timeout=envelope_s + 45)
            arrival_forwarder_proc = None

        if host_proc is not None:
            host_rc = _wait_proc(host_proc, timeout=30)
        if upf_proc is not None:
            upf_rc = _wait_proc(upf_proc, timeout=45)
    finally:
        if aqm_sampler is not None:
            try:
                sampler_meta = aqm_sampler.stop()
                aqm_queue_audit = summarize_aqm_qdisc_timeseries(
                    aqm_sampler.samples,
                    healthy_max_backlog_kb=float(
                        getattr(args, "aqm_ul_healthy_max_backlog_kb", 64.0) or 64.0
                    ),
                    healthy_max_qlen=int(getattr(args, "aqm_ul_healthy_max_qlen", 64) or 64),
                )
                aqm_queue_audit.update(sampler_meta)
                (run_dir / "aqm_qdisc_summary.json").write_text(
                    json.dumps(aqm_queue_audit, indent=2) + "\n", encoding="utf-8",
                )
            except Exception as exc:  # best-effort; never fail the run on sampler stop
                aqm_queue_audit = {
                    "pass": False,
                    "local_queue_ok": False,
                    "local_queue_ok_reason": f"sampler_stop_failed:{exc}",
                }
            aqm_sampler = None
        if getattr(args, "c2_arrival_capture", False):
            restore_socat_udp5760(ctx, ns)
        if proxy_proc is not None and proxy_proc.poll() is None:
            try:
                proxy_rc, extra_shutdown = shutdown_uav_proxy(
                    ctx, ns, pod_proxy_out, proxy_proc, run_dir,
                )
                proxy_shutdown_info.update(extra_shutdown)
                proxy_shutdown_info["shutdown_error"] = "proxy still running in finally"
            except Exception as exc:
                proxy_shutdown_info["shutdown_error"] = str(exc)
            proxy_stopped_mono = proxy_stopped_mono or time.monotonic()
            proxy_proc = None
        _terminate_layer_procs(*_layer_procs())
        if companion_mbps > 0:
            stop_companion_video_server(ctx, ns)
        stop_sidecars(sidecar_proc, grace_s=45.0)
        for logf in log_handles:
            try:
                logf.close()
            except OSError:
                pass

    loadavg_after = read_loadavg1()
    mission_log_path = run_dir / "mission.live.log"
    mission_log = mission_log_path.read_text(encoding="utf-8") if mission_log_path.is_file() else ""
    (run_dir / "mission.log").write_text(
        "$ %s\n\n--- LIVE LOG ---\n%s\n"
        % (" ".join(mission_cmd), mission_log),
        encoding="utf-8",
    )

    # --- collect C2 / physics ---
    c2 = {}
    summary_txt = fetch_file(ctx, ns, "oai-ue-gcs", "nr-ue", f"{pod_mission_out}/mission_summary.json")
    if summary_txt:
        try:
            c2 = json.loads(summary_txt)
        except json.JSONDecodeError:
            c2 = {}
    for extra in ("flight_state.csv", "response.csv", "setpoints.csv", "aoi_timeseries.csv",
                  "freshness_summary.json", "stream_cut_summary.json", "telemetry_downlink_gcs.csv",
                  "sync_skew_gcs.csv", "mavlink_rx_gcs.csv", "statustext_gcs.csv"):
        extra_txt = fetch_file(ctx, ns, "oai-ue-gcs", "nr-ue", f"{pod_mission_out}/{extra}")
        if extra_txt:
            (run_dir / extra).write_text(extra_txt, encoding="utf-8")
    merge_meta: dict[str, Any] = {}
    if args.workload == "setpoint-stream" and args.c2_proxy:
        traj_dur = effective_traj_duration_s(args, mission_len, envelope_s)
        merge_meta = merge_setpoint_freshness(
            run_dir,
            args.setpoint_rate_hz,
            traj_dur,
            successful_send_calls=dget(c2, "setpoint_accounting", "successful_send_calls"),
        )
        if merge_meta.get("freshness_summary"):
            c2["freshness_metrics"] = merge_meta["freshness_summary"]
    elif args.workload == "setpoint-stream" and getattr(args, "c2_arrival_capture", False):
        arr_txt = fetch_file(ctx, ns, "oai-ue-uav", "nr-ue", pod_arrival_csv)
        if arr_txt:
            (run_dir / "c2_arrival.csv").write_text(arr_txt, encoding="utf-8")
        traj_dur = effective_traj_duration_s(args, mission_len, envelope_s)
        merge_meta = merge_setpoint_arrival_freshness(
            run_dir,
            args.setpoint_rate_hz,
            traj_dur,
            successful_send_calls=dget(c2, "setpoint_accounting", "successful_send_calls"),
        )
        if merge_meta.get("freshness_summary"):
            c2["freshness_metrics"] = merge_meta["freshness_summary"]

    if getattr(args, "telemetry_arrival_capture", False):
        if getattr(args, "c2_arrival_capture", False):
            tel_uav_txt = fetch_file(ctx, ns, "oai-ue-uav", "nr-ue", pod_telemetry_uav_csv)
            if tel_uav_txt:
                (run_dir / "c2_telemetry_uav.csv").write_text(tel_uav_txt, encoding="utf-8")
        traj_dur = effective_traj_duration_s(args, mission_len, envelope_s)
        clock_offset: dict[str, Any] | None = None
        try:
            import importlib.util
            _spec = importlib.util.spec_from_file_location(
                "mono_clock_offset", PROBES_DIR / "mono_clock_offset.py",
            )
            if _spec is None or _spec.loader is None:
                raise ImportError("mono_clock_offset spec failed")
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            clock_offset = _mod.measure_mono_clock_offset(ctx, ns, n=10)
            (run_dir / "mono_clock_offset.json").write_text(
                json.dumps(clock_offset, indent=2) + "\n", encoding="utf-8",
            )
        except Exception as exc:  # best-effort; never fail the run
            clock_offset = {"ok": False, "reason": f"mono_clock_offset failed: {exc}"}
            try:
                (run_dir / "mono_clock_offset.json").write_text(
                    json.dumps(clock_offset, indent=2) + "\n", encoding="utf-8",
                )
            except OSError:
                pass
        tel_meta = merge_telemetry_downlink_freshness(
            run_dir, traj_dur, clock_offset=clock_offset,
        )
        if tel_meta.get("telemetry_downlink_summary"):
            fm = c2.get("freshness_metrics") or {}
            fm["telemetry_downlink"] = tel_meta["telemetry_downlink_summary"]
            c2["freshness_metrics"] = fm
            c2["telemetry_downlink"] = tel_meta["telemetry_downlink_summary"]
        # trk_truth: SITL egress vs plan (report-only; complements GCS trk_err).
        try:
            from freshness_metrics import compute_trk_truth  # noqa: WPS433
            trk_truth = compute_trk_truth(run_dir, clock_offset=clock_offset)
            (run_dir / "trk_truth.json").write_text(
                json.dumps(trk_truth, indent=2) + "\n", encoding="utf-8",
            )
            c2["trk_truth"] = trk_truth
        except Exception as exc:  # never fail the run
            c2["trk_truth"] = {"ok": False, "reason": f"compute_trk_truth failed: {exc}"}
            try:
                (run_dir / "trk_truth.json").write_text(
                    json.dumps(c2["trk_truth"], indent=2) + "\n", encoding="utf-8",
                )
            except OSError:
                pass
    elif (run_dir / "setpoints.csv").is_file() and (run_dir / "c2_telemetry_uav.csv").is_file():
        # Artifacts already on disk (e.g. re-entry) — still compute trk_truth.
        try:
            from freshness_metrics import compute_trk_truth  # noqa: WPS433
            clock_offset = None
            cop = run_dir / "mono_clock_offset.json"
            if cop.is_file():
                try:
                    clock_offset = json.loads(cop.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    clock_offset = None
            trk_truth = compute_trk_truth(run_dir, clock_offset=clock_offset)
            (run_dir / "trk_truth.json").write_text(
                json.dumps(trk_truth, indent=2) + "\n", encoding="utf-8",
            )
            c2["trk_truth"] = trk_truth
        except Exception as exc:
            c2["trk_truth"] = {"ok": False, "reason": f"compute_trk_truth failed: {exc}"}

    attack_summary: dict[str, Any] = {}
    receiver_summary: dict[str, Any] = {}
    if not is_baseline and (attack_proc is not None or attack_procs):
        attack_summary, receiver_summary = collect_attack_artifacts(
            ctx, ns, run_dir, args.attack, run_id,
            cbr_mbps_per_stream=args.cbr_mbps if args.attack == "cbr" else None,
            cbr_parallel=args.cbr_parallel,
            cbr_dir=args.cbr_dir,
            cbr_profile=getattr(args, "cbr_profile", CBR_PROFILE_SATURATING),
            cbr_protocol=getattr(args, "cbr_protocol", "udp"),
            multi_rogue_count=getattr(args, "multi_rogue_count", 4),
            multi_rogue_phase_ms=getattr(args, "multi_rogue_phase_ms", 25),
        )
    if companion_mbps > 0:
        # Client JSON stays on the UAV (video source); server on the GCS.
        vid_txt = fetch_file(ctx, ns, "oai-ue-uav", "nr-ue", "/tmp/companion_video.json")
        if vid_txt:
            (run_dir / "companion_video.json").write_text(vid_txt, encoding="utf-8")
        companion_summary = parse_companion_video_summary(
            vid_txt or "", offered_mbps=companion_mbps,
        )
        companion_summary["returncode"] = companion_rc
        companion_summary["gcs_ip"] = gcs_ip
        companion_summary["uav_ip"] = uav_ip
        (run_dir / "companion_video_summary.json").write_text(
            json.dumps(companion_summary, indent=2) + "\n", encoding="utf-8",
        )
        if not companion_summary.get("ok"):
            print(
                "  [warn] companion-video: iperf JSON incomplete/zeroed "
                f"(rc={companion_rc}) — UAV→GCS demand may not have started",
                flush=True,
            )
    (run_dir / "c2.json").write_text(json.dumps(c2, indent=2) + "\n", encoding="utf-8")
    cmds_csv = fetch_file(ctx, ns, "oai-ue-gcs", "nr-ue", f"{pod_mission_out}/commands.csv")
    if cmds_csv:
        (run_dir / "commands.csv").write_text(cmds_csv, encoding="utf-8")
    physics = {
        "max_deviation_m": dget(c2, "physics_metrics", "max_deviation_m"),
        "position_samples": dget(c2, "physics_metrics", "position_samples"),
        "failsafe": dget(c2, "c2_metrics", "failsafe"),
        "measurement_window": c2.get("measurement_window"),
        "trajectory_acquisition": dget(c2, "physics_metrics", "trajectory_acquisition"),
        "steady_state_window": dget(c2, "physics_metrics", "steady_state_window"),
        "flight_state": dget(c2, "physics_metrics", "flight_state"),
        "freshness": c2.get("freshness_metrics") or c2.get("freshness"),
        "trk_truth": c2.get("trk_truth"),
    }
    (run_dir / "physics.json").write_text(json.dumps(physics, indent=2) + "\n", encoding="utf-8")

    prb_meta_path = Path(str(prb_raw) + ".meta.json")
    if prb_meta_path.is_file():
        try:
            prb_meta = json.loads(prb_meta_path.read_text(encoding="utf-8"))
            prb_meta["run_id"] = run_id
            prb_meta_path.write_text(json.dumps(prb_meta, indent=2) + "\n", encoding="utf-8")
        except json.JSONDecodeError:
            pass

    # --- PRB parse (best-effort) ---
    prb = parse_prb(ctx, ns, args.topology, prb_raw, run_dir)

    # --- network ---
    network = {}
    net_path = run_dir / "network.json"
    if net_path.exists():
        try:
            network = json.loads(net_path.read_text())
        except json.JSONDecodeError:
            network = {}

    # --- gates ---
    ncores = nproc()
    ordering_viol = dget(network, "clock_sanity", "handshake_4ts", "ordering_violations")
    neg_owd = dget(network, "clock_sanity", "negative_owd_samples", default=0)
    hb_gaps = dget(c2, "c2_metrics", "heartbeat_gaps_over_threshold")
    cmds_lost = dget(c2, "c2_metrics", "commands_lost")
    max_dev = dget(c2, "physics_metrics", "max_deviation_m")
    xtrack_p99 = dget(c2, "physics_metrics", "flight_state", "cross_track_error_m", "p99")
    xtrack_p99_steady = dget(
        c2, "physics_metrics", "steady_state_window", "cross_track_error_m", "p99",
    )
    acq_completed = dget(c2, "physics_metrics", "trajectory_acquisition", "completed")
    mode_integrity = dget(c2, "physics_metrics", "flight_state", "mode_transition_integrity")
    physics_complete = dget(c2, "physics_metrics", "physics_metrics_complete")
    avail_pct = dget(network, "availability", "availability_post_warmup_pct")
    fresh = c2.get("freshness_metrics") or physics.get("freshness") or {}
    correlation_ratio = merge_meta.get("correlation_ratio") or fresh.get("correlation_ratio")
    delivery_ratio = merge_meta.get("delivery_ratio") or fresh.get("delivery_ratio")
    capture_ratio = merge_meta.get("capture_ratio") or fresh.get("capture_ratio")
    match_ratio = correlation_ratio or merge_meta.get("proxy_match_ratio") or fresh.get("proxy_match_ratio")
    n_setpoints = int(
        merge_meta.get("n_setpoints")
        or fresh.get("n_setpoints")
        or c2.get("n_setpoints")
        or 0
    )

    host_summary: dict[str, Any] = {}
    hs_path = run_dir / "host_summary.json"
    if hs_path.is_file():
        try:
            host_summary = json.loads(hs_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            host_summary = {}
    host_max_la = host_summary.get("loadavg1_max")
    host_max_cpu = host_summary.get("cpu_busy_pct_max")

    upf_summary: dict[str, Any] = {}
    us_path = run_dir / "upf_summary.json"
    if us_path.is_file():
        try:
            upf_summary = json.loads(us_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            upf_summary = {}
    upf_saturated = bool(upf_summary.get("saturated"))
    # Soft attribution flag only — never invalidates the run by itself.
    gate_upf_headroom = (not upf_saturated) if upf_summary.get("samples_ok") else None

    temporal_trend_us = dget(network, "owd_temporal_trend_us", "delta_us")
    trend_max_us = dget(network, "owd_temporal_trend_us", "max_abs_delta_us")
    time_ns_match = dget(network, "clock_sanity", "time_namespace_match")
    gate_clock, clock_reason = evaluate_clock_gate(
        probe_rc=probe_rc,
        ordering_violations=ordering_viol,
        negative_owd_samples=neg_owd,
        temporal_trend_us=temporal_trend_us,
        trend_max_us=trend_max_us,
        time_namespace_match=time_ns_match,
    )
    gate_host = (
        HOST_TELEMETRY.is_file()
        and host_proc is not None
        and host_rc == 0
        and hs_path.is_file()
        and host_max_la is not None
        and host_max_cpu is not None
        and host_max_la <= getattr(args, "host_loadavg_max", default_host_loadavg_max())
        and host_max_cpu <= getattr(args, "host_cpu_busy_max_pct", HOST_CPU_BUSY_MAX_PCT)
    )
    gate_proxy = True
    proxy_gate_info: dict[str, Any] = {}
    if args.c2_proxy:
        gate_proxy, proxy_gate_info = evaluate_proxy_gate(run_dir, proxy_rc)
    gate_freshness = True
    fresh_sanity_info: dict[str, Any] = {}
    if args.workload == "setpoint-stream":
        gate_freshness = (
            (run_dir / "setpoints.csv").is_file()
            and (n_setpoints or 0) > 0
        )
        if args.c2_proxy:
            gate_freshness = gate_freshness and (run_dir / "freshness_summary.json").is_file()
            gate_freshness = gate_freshness and merge_meta.get("ok") and (
                delivery_ratio is not None and delivery_ratio >= DELIVERY_MIN_RATIO
                and capture_ratio is not None and capture_ratio >= CAPTURE_MIN_RATIO
                and correlation_ratio is not None and correlation_ratio >= CORRELATION_MIN_RATIO
            )
            fresh_sanity_ok, fresh_sanity_info = evaluate_freshness_integrity_gate(fresh, merge_meta)
            gate_freshness = gate_freshness and fresh_sanity_ok
    gate_stream = True
    stream_cut_info: dict[str, Any] = {}
    if args.workload == "setpoint-stream":
        traj_dur = effective_traj_duration_s(args, mission_len, envelope_s)
        expected_sp = max(1, int(traj_dur * args.setpoint_rate_hz))
        achieved_hz = (n_setpoints / traj_dur) if (n_setpoints and traj_dur > 0) else 0.0
        rate_ok = (
            n_setpoints >= expected_sp * (1.0 - SETPOINT_RATE_TOL)
            and args.setpoint_rate_hz > 0
            and abs(achieved_hz - args.setpoint_rate_hz) / args.setpoint_rate_hz <= SETPOINT_RATE_TOL
        )
        if args.stream_cut_ms is not None and args.stream_cut_ms > 0:
            cut_ok, stream_cut_info = evaluate_stream_cut_gate(
                run_dir,
                float(args.stream_cut_ms),
                args.setpoint_rate_hz,
                traj_dur,
                n_setpoints=n_setpoints,
            )
            gate_stream = cut_ok
        else:
            gate_stream = rate_ok
            stream_cut_info = {}
    gate_avail = avail_pct is not None and avail_pct >= args.avail_min_pct
    gate_c2_health, c2_health_info = evaluate_c2_health(
        c2, is_baseline and args.workload == "setpoint-stream",
    )
    gate_prb, prb_gate_info = evaluate_prb_gate(
        prb, args.require_prb, envelope_s=envelope_s, run_id=run_id,
        companion_video_mbps=companion_mbps,
    )
    probe_log = live_log_tail(run_dir, "probe.live.log")
    mission_log = live_log_tail(run_dir, "mission.live.log")
    attack_log = live_log_tail(run_dir, "attack.live.log")
    if args.attack == "multi-rogue-sync":
        attack_log = "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(run_dir.glob("attack.oai-ue-rogue*.live.log"))
            if p.is_file()
        )
    attack_stderr = ""
    stderr_path = run_dir / "attack_cbr.stderr"
    if stderr_path.is_file():
        attack_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")

    gate_mission_integrity, mission_integrity_info = evaluate_mission_integrity(
        run_dir, mission_proc, mission_rc, c2, mission_log, args.workload,
    )
    gate_mission_success, mission_success_info = evaluate_mission_success(mission_rc, mission_log)
    gate_drone_integrity, drone_integrity_info = evaluate_drone_integrity(
        run_dir, c2, physics, args.workload,
    )
    gate_baseline_drone_quality, baseline_drone_info = evaluate_baseline_drone_quality(
        mission_rc,
        c2,
        physics,
        workload=args.workload,
        cross_track_max_p99_m=args.cross_track_max_p99_m,
        deviation_max_m=args.deviation_max_m,
    )
    drone_outcome = build_drone_outcome(mission_rc, c2, physics)
    raw_iperf: dict[str, Any] | None = None
    cbr_json_path = run_dir / "attack_cbr.json"
    if cbr_json_path.is_file():
        try:
            raw_iperf = json.loads(cbr_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw_iperf = None
    attack_start_s = (
        offsets.get("attack_ns") / 1e9 if offsets.get("attack_ns") is not None else None
    )
    if (not is_baseline
            and args.attack in (*MULTI_ROGUE_ATTACKS, "microburst")):
        # 3.2a: markers must reflect the real (base-envelope) attack window.
        onset_delay_s, attack_burst_s = resolve_attack_onset(args, attack_envelope_s)
    else:
        onset_delay_s, attack_burst_s = 0.0, int(attack_envelope_s)
    if attack_start_s is not None and onset_delay_s > 0:
        attack_start_s += onset_delay_s
    prb_start_s = offsets.get("prb_ns") / 1e9 if offsets.get("prb_ns") is not None else 0.0
    attack_dur_s = float(attack_burst_s)
    if raw_iperf and args.attack == "cbr":
        iperf_act = analyze_iperf3_cbr_activity(raw_iperf, args.cbr_dir)
        attack_dur_s = float(iperf_act.get("observed_attack_duration_s") or attack_envelope_s)
    attack_end_s = (attack_start_s + attack_dur_s) if attack_start_s is not None else None
    gate_attack_integrity, attack_integrity_info = evaluate_attack_integrity(
        args.attack if not is_baseline else "",
        attack_proc,
        attack_rc,
        sink_rc,
        attack_summary,
        receiver_summary,
        run_id,
        attack_log,
        attack_procs=attack_procs if attack_procs else None,
        raw_iperf=raw_iperf,
        attack_stderr=attack_stderr,
        expected_attack_duration_s=float(attack_dur_s),
        cbr_profile=getattr(args, "cbr_profile", CBR_PROFILE_SATURATING),
        prb=prb,
        attack_start_s=attack_start_s,
        attack_end_s=attack_end_s,
        prb_start_s=prb_start_s,
        prb_csv=run_dir / "prb.csv",
        triggers_fired=dget(c2, "attack_sync", "triggers_fired"),
        rogue_ul_cap_mbps=per_rogue_ul_cap_mbps(args),
    )
    attack_outcomes = build_attack_outcomes(
        args.attack if not is_baseline else "",
        attack_summary,
        attack_integrity_info if not is_baseline else None,
    )
    gate_host_instr, host_instr_info = evaluate_host_instrumentation_integrity(
        run_dir=run_dir,
        probe_rc=probe_rc,
        host_rc=host_rc,
        prb_rc=prb_rc,
        mission_rc=mission_rc,
        proxy_rc=proxy_rc,
        attack_rc=attack_rc,
        host_summary=host_summary,
        envelope_s=float(envelope_s),
        require_prb=bool(args.require_prb),
        c2_proxy=bool(args.c2_proxy),
        is_attack=not is_baseline,
        clock_pass=bool(gate_clock),
        proxy_pass=bool(gate_proxy),
        freshness_pass=bool(gate_freshness),
        stream_pass=bool(gate_stream),
        prb_pass=bool(gate_prb),
        prb_gate_info=prb_gate_info,
        host_proc_started=host_proc is not None,
    )

    gates: dict[str, Any] = {
        "clock": {"pass": bool(gate_clock), "probe_rc": probe_rc,
                  "ordering_violations": ordering_viol, "negative_owd_samples": neg_owd,
                  "temporal_trend_us": temporal_trend_us,
                  "temporal_trend_cap_us": trend_max_us,
                  "time_namespace_match": time_ns_match,
                  "temporal_trend_note": (
                      "shared time namespace: OWD trend is path/attack signal, not skew"
                      if time_ns_match else
                      "no shared namespace: OWD trend may be clock skew"),
                  "reason": clock_reason},
        "host": {
            "pass": bool(gate_host),
            "gate_type": "host_quality",
            "enforced": is_baseline,
            "host_telemetry_rc": host_rc,
            "loadavg1_max": host_max_la,
            "cpu_busy_pct_max": host_max_cpu,
            "cpu_busy_max_pct_threshold": getattr(
                args, "host_cpu_busy_max_pct", HOST_CPU_BUSY_MAX_PCT
            ),
            "loadavg1_before": loadavg_before,
            "loadavg1_after": loadavg_after,
            "nproc": ncores,
        },
        "upf_cpu": {
            "pass": gate_upf_headroom,  # None = unsampled; True/False = soft attribution
            "enforced": False,
            "upf_telemetry_rc": upf_rc,
            "cpu_pct_max": upf_summary.get("cpu_pct_max"),
            "cpu_pct_p50": upf_summary.get("cpu_pct_p50"),
            "cpu_pct_p99": upf_summary.get("cpu_pct_p99"),
            "saturated": upf_saturated if upf_summary else None,
            "saturate_pct_threshold": upf_summary.get("saturate_pct_threshold"),
            "mechanism_hint": upf_summary.get("mechanism_hint"),
            "samples_ok": upf_summary.get("samples_ok"),
            "note": (
                "soft: UPF busy ⇒ do not claim pure PRB/MAC; "
                "UPF headroom + PRB correlation supports RAN attribution"
            ),
        },
        "host_instrumentation_integrity": {
            "pass": bool(gate_host_instr),
            "enforced": True,
            **host_instr_info,
        },
        "mission_integrity": {"pass": bool(gate_mission_integrity), **mission_integrity_info},
        "mission_success": {"pass": bool(gate_mission_success), **mission_success_info},
        "prb": {"pass": bool(gate_prb), **prb_gate_info},
        "availability": {"pass": bool(gate_avail), "availability_post_warmup_pct": avail_pct,
                         "min_pct": args.avail_min_pct, "enforced_on_baseline_only": True},
        "drone_integrity": {"pass": bool(gate_drone_integrity), **drone_integrity_info},
        "baseline_drone_quality": {
            "pass": bool(gate_baseline_drone_quality),
            "baseline_only": True,
            **baseline_drone_info,
        },
        "drone_outcome": drone_outcome,
        "paper_comparable_orbit": {
            "pass": (
                bool(dget(c2, "physics_metrics", "trajectory_acquisition", "paper_comparable"))
                if dget(c2, "physics_metrics", "trajectory_acquisition", "paper_comparable") is not None
                else None
            ),
            "enforced": False,
            "paper_comparable": dget(
                c2, "physics_metrics", "trajectory_acquisition", "paper_comparable"
            ),
            "reasons": dget(
                c2, "physics_metrics", "trajectory_acquisition", "paper_comparable_reasons"
            ),
            "stream_start_radius_m": dget(
                c2, "physics_metrics", "trajectory_acquisition", "stream_start_radius_m"
            ),
            "acquisition_duration_s": dget(
                c2, "physics_metrics", "trajectory_acquisition", "duration_s"
            ),
            "note": (
                "false if stream started far from circle or acq > 2.5s; "
                "enable PREPOSITION_ORBIT_ENTRY=1 for new repros"
            ),
        },
    }
    runtime_mitigation = getattr(args, "_runtime_mitigation_audit", None) or {
        "enabled": False,
        "pass": True,
    }
    if aqm_queue_audit is not None:
        aqm_block = dict(runtime_mitigation.get("aqm_ul") or {})
        aqm_block["qdisc_sampling"] = aqm_queue_audit
        aqm_block["local_queue_ok"] = bool(aqm_queue_audit.get("local_queue_ok"))
        aqm_block["local_queue_ok_reason"] = aqm_queue_audit.get("local_queue_ok_reason")
        # Sampling must succeed for ablation integrity; local_queue_ok is the
        # scientific verdict (may be False while AQM is correctly installed).
        if not aqm_queue_audit.get("pass", False) or int(aqm_queue_audit.get("n_samples") or 0) < 1:
            aqm_block["pass"] = False
            runtime_mitigation["pass"] = False
        runtime_mitigation["aqm_ul"] = aqm_block
        args._runtime_mitigation_audit = runtime_mitigation
    testbed_mitigation = getattr(args, "_testbed_mitigation_audit", None) or {
        "enabled": False,
        "pass": True,
    }
    gates["mitigation_integrity"] = {
        "pass": bool(runtime_mitigation.get("pass") and testbed_mitigation.get("pass")),
        "runtime_tc": runtime_mitigation,
        "testbed": testbed_mitigation,
    }
    if args.c2_proxy:
        gates["proxy"] = {
            "pass": bool(gate_proxy),
            "proxy_rc": proxy_rc,
            **proxy_gate_info,
            "reason": proxy_gate_info.get("reason"),
        }
    if args.workload == "setpoint-stream":
        traj_dur = effective_traj_duration_s(args, mission_len, envelope_s)
        integrity_gate = {
            "pass": bool(gate_freshness),
            "gate_type": "measurement_integrity",
            "n_setpoints": n_setpoints,
            "delivery_ratio": delivery_ratio,
            "capture_ratio": capture_ratio,
            "correlation_ratio": correlation_ratio,
            "proxy_match_ratio": match_ratio,
            "min_delivery_ratio": DELIVERY_MIN_RATIO if args.c2_proxy else None,
            "min_capture_ratio": CAPTURE_MIN_RATIO if args.c2_proxy else None,
            "min_correlation_ratio": CORRELATION_MIN_RATIO if args.c2_proxy else None,
            "merge_ok": merge_meta.get("ok"),
            "setpoint_accounting": merge_meta.get("setpoint_accounting"),
            "freshness_sanity": fresh_sanity_info,
        }
        gates["freshness_measurement_integrity"] = integrity_gate
        gates["freshness"] = integrity_gate
        gates["stream"] = {
            "pass": bool(gate_stream),
            "rate_hz_requested": args.setpoint_rate_hz,
            "rate_hz_achieved": round((n_setpoints / traj_dur), 3) if (n_setpoints and traj_dur) else None,
            "setpoints_expected": int(traj_dur * args.setpoint_rate_hz),
            "setpoints_generated": n_setpoints,
            "stream_cut_validation": stream_cut_info or None,
        }
        gates["c2_health"] = {"pass": bool(gate_c2_health), **c2_health_info}
    if not is_baseline:
        gates["attack_integrity"] = {
            "pass": bool(gate_attack_integrity),
            **attack_integrity_info,
        }
        if args.attack == "pulse":
            b, p, k, f = pulse_params(args)
            gates["attack_integrity"]["pulse"] = {
                "profile": args.pulse_profile, "bolus_kb": b,
                "period_ms": p, "pkt_bytes": k, "flows": f,
            }
        elif args.attack == "cbr":
            gates["attack_integrity"]["cbr"] = {
                "dir": args.cbr_dir, "mbps": args.cbr_mbps, "parallel": args.cbr_parallel,
            }
        elif args.attack == "combined":
            b, p, k, f = pulse_params(args)
            gates["attack_integrity"]["combined"] = {
                "pulse": {"bolus_kb": b, "period_ms": p, "pkt_bytes": k, "flows": f},
                "cbr_dl": {"mbps": args.cbr_mbps, "parallel": args.cbr_parallel},
            }
        elif args.attack == "combined-c2":
            b, p, k, f = pulse_params(args)
            gates["attack_integrity"]["combined_c2"] = {
                "pulse": {"bolus_kb": b, "period_ms": p, "pkt_bytes": k, "flows": f},
                "carrier": {"pps": args.carrier_pps, "size": args.carrier_size},
            }
        elif args.attack == "flood-c2":
            fp = flood_params(args)
            gates["attack_integrity"]["flood_c2"] = {
                "profile": args.flood_profile, "pps": fp["pps"], "size": fp["size"],
                "port": args.flood_port if args.flood_port is not None else SITL_PORT,
            }
        elif args.attack in ("sync-c2", "sync-prb", "multi-rogue-sync"):
            triggers = dget(c2, "attack_sync", "triggers_fired")
            burst_ms = (
                args.multi_sync_burst_ms
                if args.attack == "multi-rogue-sync" else args.sync_burst_ms
            )
            pps = (
                args.multi_sync_pps
                if args.attack == "multi-rogue-sync" else args.sync_pps
            )
            size = (
                args.multi_sync_size
                if args.attack == "multi-rogue-sync" else args.sync_size
            )
            gates["attack_integrity"][args.attack.replace("-", "_")] = {
                "listen_port": args.sync_listen_port, "burst_ms": burst_ms,
                "pps": pps, "size": size,
                "ack_target": bool(args.sync_ack_target),
                "triggers_fired": triggers,
                "rogue_count": (
                    args.multi_rogue_count if args.attack == "multi-rogue-sync" else 1
                ),
                "sink": ("ext-dn:%d" % EXT_DN_IPERF_PORT_ATTACK)
                        if args.attack in ("sync-prb", "multi-rogue-sync")
                        else ("uav:%d" % SITL_PORT),
                "threat_model": (
                    "pure_prb_contention"
                    if args.attack in ("sync-prb", "multi-rogue-sync")
                    else "path_endpoint"
                ),
            }
        elif args.attack == "multi-rogue-dl-sync":
            triggers = dget(c2, "attack_sync", "triggers_fired")
            gates["attack_integrity"]["multi_rogue_dl_sync"] = {
                "rogue_count": args.multi_rogue_count,
                "listen_port": args.sync_listen_port,
                "burst_ms": args.multi_sync_burst_ms,
                "pps": args.multi_sync_pps,
                "size": args.multi_sync_size,
                "triggers_fired": triggers,
                "open_loop": sync_open_loop_enabled(args),
                "sink": "rogue-pdu-pool:5203",
                "threat_model": "pure_dl_prb_contention_sync",
            }
        elif args.attack == "multi-rogue-dl":
            gates["attack_integrity"]["multi_rogue_dl"] = {
                "rogue_count": args.multi_rogue_count,
                "mbps_per_target": args.mean_rate_mbps,
                "period_ms": args.period_ms,
                "burst_size_bytes": args.burst_size_bytes,
                "sink": "rogue-pdu-pool:5203",
                "threat_model": "pure_dl_prb_contention",
            }

    enforced = enforced_validity_gates(args, is_baseline=is_baseline)
    valid = all(gates[g]["pass"] for g in enforced)
    gates["enforced"] = enforced
    gates["valid"] = valid

    traj_dur_meta = (
        effective_traj_duration_s(args, mission_len, envelope_s)
        if args.workload == "setpoint-stream" else None
    )
    wall_runtime_s = round(time.monotonic() - wall_t0, 3)
    proxy_runtime_s = (
        round(proxy_stopped_mono - proxy_started_mono, 3)
        if proxy_started_mono is not None and proxy_stopped_mono is not None
        else None
    )
    port_check = check_uav_mavlink_ports(ctx, ns, args.c2_proxy)
    cleanup_audit = audit_post_run_cleanup(ctx, ns, args, need_rogue=(not is_baseline))
    run_meta = {
        "run_id": run_id,
        "run_slug": build_run_slug(args),
        "run_index": seed,
        "attack_seed": args.attack_seed if not is_baseline else None,
        "setpoint_seed": args.setpoint_seed if args.workload == "setpoint-stream" else None,
        "condition": args.condition, "topology": args.topology, "gnb_prb": args.gnb_prb,
        "attack": None if is_baseline else args.attack, "reservation": args.reservation,
        "min_ratio_slice_a": int(getattr(args, "min_ratio_slice_a", 0) or 0),
        "mitigations": {
            "rate_limit_a": bool(args.mitigation_rate_limit_a),
            "aqm_ul": bool(args.mitigation_aqm_ul),
            "ul_rrmpolicy": bool(args.mitigation_ul_rrmpolicy),
            "runtime_tc_audit": getattr(args, "_runtime_mitigation_audit", None),
        },
        "workload": args.workload,
        "c2_proxy": bool(args.c2_proxy),
        "c2_transport": getattr(args, "c2_transport", "udp"),
        "require_prb": bool(args.require_prb),
        "companion_video_mbps": companion_mbps if companion_mbps > 0 else None,
        "attack_path": args.attack_path if not is_baseline else None,
        "mean_rate_mbps": args.mean_rate_mbps if not is_baseline else None,
        "burst_size_bytes": args.burst_size_bytes if not is_baseline else None,
        "period_ms": args.period_ms if not is_baseline else None,
        "attack_start_after_s": (
            float(getattr(args, "attack_start_after_s", 0.0) or 0.0)
            if not is_baseline and args.attack in (*MULTI_ROGUE_ATTACKS, "microburst")
            else None
        ),
        "attack_burst_duration_s": (
            float(attack_burst_s)
            if not is_baseline and args.attack in (*MULTI_ROGUE_ATTACKS, "microburst")
            else None
        ),
        "duration_s": duration, "warmup_s": warmup,
        "timing": {
            "requested_duration_s": duration,
            "network_measurement_duration_s": duration,
            "warmup_s": warmup,
            "envelope_s": envelope_s,
            "mission_len_s": mission_len,
            "trajectory_active_duration_s": traj_dur_meta,
            "proxy_watchdog_s": proxy_watchdog if args.c2_proxy else None,
            "wall_runtime_s": wall_runtime_s,
            "proxy_runtime_s": proxy_runtime_s,
            "proxy_shutdown": proxy_shutdown_info or None,
            "prb_capture_grace_s": PRB_CAPTURE_GRACE_S,
            "prb_wait_timeout_s": round(prb_wait_timeout_s, 3) if prb_wait_timeout_s is not None else None,
            "prb_wait_status": prb_wait_status,
        },
        "port_check": port_check,
        "cleanup_audit": cleanup_audit,
        "mission_len_s": mission_len if args.workload == "waypoint" else None,
        "mission_commands": n_cmds if args.workload == "waypoint" else None,
        "setpoint_rate_hz": args.setpoint_rate_hz if args.workload == "setpoint-stream" else None,
        "trajectory_duration_s": traj_dur_meta,
        "setpoints_generated": n_setpoints if args.workload == "setpoint-stream" else None,
        "proxy_match_ratio": match_ratio if args.workload == "setpoint-stream" else None,
        "centralization_margin_s": margin,
        "T0_mono_ns": T0,
        "T0_unix_ns": T0_unix,
        "layer_offsets_ns": offsets,
        "layer_offsets_s": {k: (v / 1e9 if v is not None else None) for k, v in offsets.items()},
        "returncodes": {
            "probe": probe_rc,
            "prb": prb_rc,
            "mission": mission_rc,
            "proxy": proxy_rc,
            "host_telemetry": host_rc,
            "upf_telemetry": upf_rc,
            "attack": attack_rc,
            "sink": sink_rc,
            "companion_video": companion_rc,
        },
        "host_loadavg1": {"before": loadavg_before, "after": loadavg_after, "nproc": ncores},
        "upf_telemetry": upf_summary or None,
        "sync_open_loop": sync_open_loop_enabled(args) if not is_baseline else None,
        "sync_phase_ms": (
            float(args.sync_phase_ms)
            if (not is_baseline and sync_open_loop_enabled(args)) else None
        ),
        "sync_skew": (
            summarize_sync_skew(run_dir)
            if (not is_baseline and is_sync_style_attack(args.attack)) else None
        ),
        "gates": gates,
        "valid": valid,
        "testbed": testbed_version(),
        "topology_stamp": read_topology_stamp(),
        "pinning": read_topology_stamp(),
        "attack_summary": attack_summary or None,
        "receiver_summary": receiver_summary or None,
        "companion_video_summary": companion_summary,
        "outcomes": {
            **drone_outcome,
            **attack_outcomes,
            **({"companion_video": companion_summary} if companion_summary else {}),
        },
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    # Additive paper capture block (JSON/CSV only — never feeds VALID).
    paper_block = build_paper_artifacts_block(
        args,
        is_baseline=is_baseline,
        envelope_s=float(envelope_s),
        resolved_delay_s=float(onset_delay_s),
        burst_duration_s=float(attack_burst_s),
        mission_start_s=float(attack_mission_start_s(args)),
        attack_start_s_abs=attack_start_s,
        attack_end_s_abs=attack_end_s,
        wall_runtime_s=wall_runtime_s,
        analysis_params=build_analysis_params(args),
    )
    written = persist_run_paper_artifacts(run_dir, paper_block)
    paper_block["written"] = written
    run_meta["paper_artifacts"] = paper_block
    run_meta["multi_rogue_count"] = paper_block.get("multi_rogue_count")
    run_meta["schedule_optc"] = optc_schedule(args) if optc_applies(args) else None
    run_meta["c2_arrival_capture"] = bool(getattr(args, "c2_arrival_capture", False))
    run_meta["telemetry_arrival_capture"] = bool(
        getattr(args, "telemetry_arrival_capture", False)
    )
    (run_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2) + "\n", encoding="utf-8")

    _video_mode = (
        "b_stop_at_attack_end"
        if getattr(args, "video_stop_at_attack_end", False)
        else "a_continue"
    )
    _drain_baseline_ref = getattr(args, "drain_baseline_run", None)
    # drain_ceiling_applied is computed in run_single(); recompute the same value
    # here (same scope as this post-run block) to avoid a NameError.
    drain_ceiling_applied = (
        float(getattr(args, "drain_ceiling_s", 180.0) or 180.0)
        if (getattr(args, "drain_until_baseline", False) and not is_baseline)
        else 0.0
    )
    _drain_pol = drain_default_policy(
        enabled=bool(getattr(args, "drain_until_baseline", False)),
        drain_ceiling_s=float(getattr(args, "drain_ceiling_s", 180.0) or 180.0),
        video_mode=_video_mode,
        baseline_ref_run=_drain_baseline_ref,
        baseline_source=(
            "separate_baseline_run" if _drain_baseline_ref
            else ("pre_window_same_run" if getattr(args, "drain_until_baseline", False) else "unset")
        ),
    )
    _drain_pol["drain_ceiling_applied_s"] = float(drain_ceiling_applied)
    run_meta["sidecars"] = {
        "profile": sidecar_profile or "off",
        "duration_s": (
            sidecar_duration_s(envelope_s, getattr(args, "sidecars_duration_s", None))
            if sidecar_profile else None
        ),
    }
    run_meta["drain_policy"] = _drain_pol
    run_meta["video_stop_t_rel_s"] = video_stop_t_rel_s
    (run_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2) + "\n", encoding="utf-8")
    harness_written = write_post_run_harness(
        run_dir,
        sidecar_profile=sidecar_profile,
        drain_policy=_drain_pol,
        video_stop_t_rel_s=video_stop_t_rel_s,
    )
    run_meta["harness_written"] = harness_written

    four = build_four_layer(run_meta, network, c2, physics, prb, offsets, gates)
    (run_dir / "four_layer_summary.json").write_text(json.dumps(four, indent=2) + "\n", encoding="utf-8")

    print_terminal_run_report(run_meta, network, c2, physics, prb)
    CURRENT_RUN.clear()
    return run_meta, four


def parse_prb(ctx, ns, topology, prb_raw, run_dir):
    prb_json = run_dir / "prb.json"
    prb_csv = run_dir / "prb.csv"
    empty = {"dump_samples": 0, "per_slice": {},
             "note": "no gNB MAC dumps (needs MAC_log_level=info); PRB best-effort, gate skipped"}
    if not prb_raw.exists() or prb_raw.stat().st_size == 0:
        prb_json.write_text(json.dumps(empty, indent=2) + "\n", encoding="utf-8")
        return empty
    rnti_map = run_dir / "rnti.map"
    subprocess.run([sys.executable, str(MAC_PRB), "rntimap", ctx, ns, str(rnti_map)],
                   capture_output=True, text=True)
    roleslice = (
        '{"rogue":"A","rogue2":"A","rogue3":"A","rogue4":"A","gcs":"A","uav":"A"}'
        if topology == "intra"
        else '{"rogue":"A","rogue2":"A","rogue3":"A","rogue4":"A","gcs":"B","uav":"B"}'
    )
    r = subprocess.run([sys.executable, str(MAC_PRB), "parse", str(prb_raw), str(rnti_map),
                        roleslice, str(prb_csv), str(prb_json)], capture_output=True, text=True)
    if r.returncode != 0 or not prb_json.exists():
        prb_json.write_text(json.dumps(empty, indent=2) + "\n", encoding="utf-8")
        return empty
    try:
        parsed = json.loads(prb_json.read_text())
    except json.JSONDecodeError:
        return empty
    meta_path = Path(str(prb_raw) + ".meta.json")
    if meta_path.is_file():
        try:
            parsed["capture_meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return parsed


def build_four_layer(run_meta, network, c2, physics, prb, offsets, gates=None):
    gates = gates or {}
    return {
        "run_id": run_meta["run_id"],
        "T0_mono_ns": run_meta["T0_mono_ns"],
        "condition": run_meta["condition"], "topology": run_meta["topology"],
        "gnb_prb": run_meta["gnb_prb"], "attack": run_meta["attack"],
        "reservation": run_meta["reservation"], "duration_s": run_meta["duration_s"],
        "layer_offsets_s": run_meta["layer_offsets_s"],
        "network": {
            "owd_us": network.get("owd_us"), "ipdv_us": network.get("ipdv_us"),
            "availability": network.get("availability"),
            "samples": network.get("samples"),
            "clock_sanity": {
                "time_namespace_match": dget(network, "clock_sanity", "time_namespace_match"),
                "ordering_violations": dget(network, "clock_sanity", "handshake_4ts", "ordering_violations"),
                "negative_owd_samples": dget(network, "clock_sanity", "negative_owd_samples"),
                "temporal_trend_delta_us": dget(network, "owd_temporal_trend_us", "delta_us"),
            },
        },
        "c2": c2.get("c2_metrics", {}),
        "physics": physics,
        "freshness": physics.get("freshness") or {},
        "stream": gates.get("stream") or {},
        "prb": {"per_slice": prb.get("per_slice"), "dump_samples": prb.get("dump_samples"),
                "dl_delta_share_pct": prb.get("dl_delta_share_pct"),
                "dl_delta_bytes_total": prb.get("dl_delta_bytes_total"),
                "ul_delta_share_pct": prb.get("ul_delta_share_pct"),
                "ul_delta_bytes_total": prb.get("ul_delta_bytes_total"),
                "cell_ul_activity": prb.get("cell_ul_activity"),
                "note": prb.get("note") or prb.get("prb_proxy_note")},
        "gates": run_meta["gates"],
        "host": run_meta["host_loadavg1"],
        "valid": run_meta["valid"],
    }


def testbed_version():
    try:
        r = subprocess.run(["git", "-C", str(SCRIPT_DIR), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return {"git": r.stdout.strip()}
    except OSError:
        pass
    return {"git": None}


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _f(x, unit="", nd=1):
    return f"{x:.{nd}f}{unit}" if isinstance(x, (int, float)) else "n/a"


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def _median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _spread(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    if not xs:
        return None
    return max(xs) - min(xs)


def aggregate(valids, args):
    try:
        import numpy as np  # noqa: F401
        have_np = True
    except ImportError:
        have_np = False

    def col(path):
        out = []
        for four in valids:
            cur = four
            for k in path:
                cur = cur.get(k) if isinstance(cur, dict) else None
                if cur is None:
                    break
            out.append(cur)
        return out

    metrics = {
        "owd_p50_us": col(["network", "owd_us", "median"]),
        "owd_p99_us": col(["network", "owd_us", "p99"]),
        "owd_p99_9_us": col(["network", "owd_us", "p99_9"]),
        "ipdv_p50_us": col(["network", "ipdv_us", "median"]),
        "ipdv_p99_us": col(["network", "ipdv_us", "p99"]),
        "availability_pct": col(["network", "availability", "availability_post_warmup_pct"]),
        "max_deviation_m": col(["physics", "max_deviation_m"]),
        "track_err_p99_m": col(["physics", "flight_state", "tracking_error_m", "p99"]),
        "track_truth_p99_m": col(["physics", "trk_truth", "tracking_error_truth_m", "p99"]),
        "track_truth_med_m": col(["physics", "trk_truth", "tracking_error_truth_m", "median"]),
        "cross_track_p99_steady_m": col(["physics", "steady_state_window", "cross_track_error_m", "p99"]),
        "acquisition_time_s": col(["physics", "trajectory_acquisition", "duration_s"]),
        "tel_delay_p50_ms": col(
            ["freshness", "telemetry_downlink", "telemetry_arrival_delay_ms", "p50"]
        ),
        "tel_delay_p99_ms": col(
            ["freshness", "telemetry_downlink", "telemetry_arrival_delay_ms", "p99"]
        ),
        "tel_delay_p99_9_ms": col(
            ["freshness", "telemetry_downlink", "telemetry_arrival_delay_ms", "p99_9"]
        ),
        "tel_gap_p50_ms": col(
            ["freshness", "telemetry_downlink", "inter_arrival_gap_ms", "p50"]
        ),
        "tel_gap_p99_ms": col(
            ["freshness", "telemetry_downlink", "inter_arrival_gap_ms", "p99"]
        ),
        "tel_gap_p99_9_ms": col(
            ["freshness", "telemetry_downlink", "inter_arrival_gap_ms", "p99_9"]
        ),
        "belief_p50_m": col(
            ["freshness", "telemetry_downlink", "belief_vs_truth_m", "p50"]
        ),
        "belief_p99_m": col(
            ["freshness", "telemetry_downlink", "belief_vs_truth_m", "p99"]
        ),
        "prb_share_A_pct": col(["prb", "per_slice", "A", "share_ul_nprb_pct"]),
        "prb_share_B_pct": col(["prb", "per_slice", "B", "share_ul_nprb_pct"]),
        "uav_ul_mbps": col(["prb", "cell_ul_activity", "per_role", "uav", "ul_mbps"]),
        "gcs_ul_mbps": col(["prb", "cell_ul_activity", "per_role", "gcs", "ul_mbps"]),
        "host_loadavg_max": col(["gates", "host", "loadavg1_max"]),
        "host_cpu_busy_max_pct": col(["gates", "host", "cpu_busy_pct_max"]),
        "upf_cpu_max_pct": col(["gates", "upf_cpu", "cpu_pct_max"]),
        "rogue_count": col(["gates", "attack_integrity", "summary", "rogue_count"]),
        "offered_aggregate_mbps": col(
            ["gates", "attack_integrity", "summary", "requested_aggregate_mbps"]
        ),
        "delivery_ratio": col(["freshness", "delivery_ratio"]),
        "correlation_ratio": col(["freshness", "correlation_ratio"]),
        "setpoint_rate_hz": col(["stream", "rate_hz_achieved"]),
    }
    agg = {}
    for name, xs in metrics.items():
        entry = {"values": xs, "median": _median(xs), "spread": _spread(xs)}
        if have_np:
            entry["bootstrap_ci95"] = _bootstrap_ci(xs)
        agg[name] = entry
    flags = {
        "upf_saturated": {
            "values": col(["gates", "upf_cpu", "saturated"]),
        },
    }

    owd_med = agg["owd_p99_us"]["median"]
    owd_spr = agg["owd_p99_us"]["spread"]
    rel = (owd_spr / owd_med) if (owd_med and owd_spr is not None and owd_med > 0) else None
    if len(valids) < 3:
        stable = None
        stability_evaluated = False
    else:
        stable = bool(rel is not None and rel <= 0.30)
        stability_evaluated = True

    result = {
        "n_valid": len(valids),
        "metrics": agg,
        "flags": flags,
        "stable": stable,
        "stability_evaluated": stability_evaluated,
        "owd_p99_relative_spread": rel,
        "numpy": have_np,
    }
    print_aggregate(
        result,
        condition=args.condition,
        stability_evaluated=stability_evaluated,
        stable=stable,
        relative_spread=rel,
    )
    return result


def _bootstrap_ci(xs, iters=2000, p=95):
    import numpy as np
    xs = [x for x in xs if isinstance(x, (int, float))]
    if len(xs) < 2:
        return None
    arr = np.array(xs, dtype=float)
    rng = np.random.default_rng(2027)
    boot = [float(np.median(rng.choice(arr, size=len(arr), replace=True))) for _ in range(iters)]
    lo = float(np.percentile(boot, (100 - p) / 2))
    hi = float(np.percentile(boot, 100 - (100 - p) / 2))
    return {"lo": lo, "hi": hi, "level": p}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv):
    ap = argparse.ArgumentParser(description="FlyBlind 4-layer experiment orchestrator")
    ap.add_argument("--condition", choices=["baseline", "attack", "mitigation"], default="baseline")
    ap.add_argument("--duration", type=int, default=600, help="probe envelope seconds (180|600)")
    ap.add_argument("--topology", choices=["intra", "cross"], default="cross",
                    help="default cross (C2 slice B vs ROGUE slice A); intra=legacy")
    ap.add_argument("--gnb-prb", type=int, choices=[106, 51], default=106,
                    help="NRB (TS 38.104 Table 5.3.2-1, SCS 30 kHz): 106=40 MHz, 51=20 MHz")
    ap.add_argument("--attack",
                    choices=["multi-rogue", "multi-rogue-dl",
                             "multi-rogue-sync", "multi-rogue-dl-sync"],
                    default="multi-rogue",
                    help="multi-rogue=N UEs x microburst UL (playbook: phase 0); "
                         "multi-rogue-dl=ext-DN to PDU of N rogues; "
                         "multi-rogue-sync=N rogues UL synced to ext-DN (PRB, not C2); "
                         "multi-rogue-dl-sync=ext-DN DL sync (peaks at setpoint cadence)")
    ap.add_argument("--pulse-profile", choices=list(PULSE_PROFILES), default="moderate",
                    help="pulse intensity preset (bolus/period/pkt/flows)")
    ap.add_argument("--flood-profile", choices=list(FLOOD_PROFILES), default="moderate",
                    help="flood-c2 intensity: gentle|moderate|aggressive (pps/size)")
    ap.add_argument("--flood-pps", type=int, default=None, help="override flood-c2 packets/sec")
    ap.add_argument("--flood-size", type=int, default=None, help="override flood-c2 payload bytes")
    ap.add_argument("--flood-port", type=int, default=None,
                    help="override flood-c2 target port (default: 5760 SITL)")
    ap.add_argument("--pulse-bolus-kb", type=int, default=None, help="override: KB dumped per pulse")
    ap.add_argument("--pulse-period-ms", type=int, default=None, help="override: pulse period (ms)")
    ap.add_argument("--pulse-pkt", type=int, default=None, help="override: UDP payload bytes")
    ap.add_argument("--pulse-flows", type=int, default=None, help="override: parallel sender flows")
    ap.add_argument("--sync-listen-port", type=int, default=47555,
                    help="sync-c2/sync-prb: CNI port where ROGUE listens for mission_runner triggers")
    ap.add_argument("--sync-burst-ms", type=float, default=250.0,
                    help="sync-c2/sync-prb: burst duration per command (ms)")
    ap.add_argument("--sync-pps", type=int, default=20000,
                    help="sync-c2/sync-prb: packets/s in the burst")
    ap.add_argument("--sync-size", type=int, default=1400,
                    help="sync-c2/sync-prb: payload bytes per packet")
    ap.add_argument("--sync-ack-target", action="store_true",
                    help="sync-c2 only: also contend ACK->GCS (FORBIDDEN on sync-prb)")
    ap.add_argument("--sync-open-loop", action="store_true",
                    help="sync-c2/sync-prb: no GCS trigger — bursts at --setpoint-rate-hz "
                         "with phase --sync-phase-ms (realistic threat / no oracle)")
    ap.add_argument("--sync-phase-ms", type=float, default=0.0,
                    help="sync open-loop: initial delay of the 1st burst (ms)")
    ap.add_argument("--multi-sync-burst-ms", type=float, default=25.0,
                    help="multi-rogue-sync/dl-sync: short window per setpoint (default 25ms)")
    ap.add_argument("--multi-sync-pps", type=int, default=20000,
                    help="multi-rogue-sync/dl-sync: peak (default 20000 pps ~=224 Mbps)")
    ap.add_argument("--multi-sync-size", type=int, default=1400,
                    help="multi-rogue-sync/dl-sync: UDP payload per packet")
    ap.add_argument("--cbr-dir", choices=["ul", "dl", "both"], default="dl",
                    help="cbr flood direction: dl starves ACK/heartbeat return path")
    ap.add_argument("--cbr-mbps", type=int, default=20, help="cbr target bitrate per stream (Mbit/s)")
    ap.add_argument("--cbr-parallel", type=int, default=1, help="cbr iperf3 -P parallel streams")
    ap.add_argument(
        "--cbr-protocol",
        choices=["udp", "tcp"],
        default="udp",
        help="cbr iperf3 transport: udp (CBR -b) or tcp (best-effort fill, paper Sec 4.2/4.3 style)",
    )
    ap.add_argument(
        "--cbr-profile",
        choices=[CBR_PROFILE_SATURATING, CBR_PROFILE_NON_SATURATING],
        default=CBR_PROFILE_SATURATING,
        help="CBR integrity profile: saturating keeps delivery/loss as outcomes; "
             "non_saturating enforces minimum delivery ratio",
    )
    ap.add_argument("--carrier-pps", type=int, default=6000,
                    help="combined-c2: pps of the sustained UDP carrier on the C2 path (UAV:5760); "
                         "low (~1500-2500) keeps avail>=99%%, high (~6000+) pushes cmd_lat/adopt")
    ap.add_argument("--carrier-size", type=int, default=1400,
                    help="combined-c2: payload bytes per carrier packet")
    ap.add_argument("--stress-c2", action="store_true",
                    help="tighter command spacing (0.8-1.5s); keeps 25/72 cmds for paired runs")
    ap.add_argument("--workload", choices=["waypoint", "setpoint-stream"], default="setpoint-stream",
                    help="C2 workload (default setpoint-stream). Legacy: --workload waypoint (DO_REPOSITION).")
    ap.add_argument("--setpoint-rate-hz", type=float, default=10.0)
    ap.add_argument("--trajectory", choices=["circle", "figure8", "sine", "line", "route"], default="route")
    ap.add_argument("--trajectory-duration-s", type=float, default=None)
    ap.add_argument("--trajectory-speed-mps", type=float, default=4.0)
    ap.add_argument("--trajectory-radius-m", type=float, default=30.0)
    ap.add_argument("--trajectory-amplitude-m", type=float, default=20.0)
    ap.add_argument("--trajectory-period-s", type=float, default=44.0)
    ap.add_argument("--setpoint-seed", type=int, default=1)
    ap.add_argument("--setpoint-yaw-mode", choices=["fixed", "tangent", "none"], default="tangent")
    ap.add_argument("--stream-cut-ms", type=float, default=None,
                    help="Gate A: pause setpoint stream for N ms then resume (I-0A)")
    ap.add_argument("--stream-cut-start-s", type=float, default=15.0,
                    help="Gate A: seconds after trajectory_t0 when pause begins (default 15)")
    ap.add_argument("--c2-proxy", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--c2-arrival-capture", action=argparse.BooleanOptionalAction, default=True,
                    help="UDP-direct: log per-setpoint network arrival on UAV (default on; --no-c2-arrival-capture)")
    ap.add_argument("--telemetry-arrival-capture", action=argparse.BooleanOptionalAction, default=True,
                    help="log LOCAL_POSITION_NED downlink timeliness (default on; --no-telemetry-arrival-capture)")
    ap.add_argument("--c2-transport", default="udp", help=argparse.SUPPRESS)
    ap.add_argument("--allow-uninstrumented-setpoint-stream", action="store_true",
                    help="allow diagnostic setpoint-stream without formal instrumentation gates")
    ap.add_argument("--require-prb", action=argparse.BooleanOptionalAction, default=True,
                    help="require gNB MAC dump evidence (default on; --no-require-prb)")
    ap.add_argument("--attack-path", choices=["pdu", "cni-bypass"], default="pdu",
                    help="Co-tenant traffic path: 5G PDU (default) or CNI bypass control")
    ap.add_argument("--mean-rate-mbps", type=float, default=20.0,
                    help="Target mean rate for microburst/dual-pulse (Mbit/s)")
    ap.add_argument("--burst-size-bytes", type=int, default=DEFAULT_BURST_SIZE_BYTES,
                    help="Microburst payload bytes per burst (default 100000 = 20 Mbit/s @ 40 ms)")
    ap.add_argument("--period-ms", type=int, default=40,
                    help="Microburst inter-burst period (ms)")
    ap.add_argument("--dual-period-ms", type=int, default=80)
    ap.add_argument("--dual-phase-ms", type=int, default=0)
    ap.add_argument("--multi-rogue-count", type=int, default=4,
                    help="multi-rogue: number of rogue UE pods (requires EXTRA_ROGUE_COUNT>=count-1)")
    ap.add_argument("--multi-rogue-phase-ms", type=int, default=25,
                    help="multi-rogue: phase stagger between rogue UEs (ms)")
    ap.add_argument("--attack-start-after-s", type=float, default=0.0,
                    help="delay (s) before microburst/multi-rogue traffic starts (sleep in rogue "
                         "script). Default 0=legacy (burst from process start). Use ~20 so the "
                         "UAV can arm/takeoff before UL contention; burst duration is shortened "
                         "so delay+burst fits the envelope.")
    ap.add_argument("--companion-video-mbps", type=float, default=12.0,
                    help="BVLOS companion demand: UAV→GCS UDP :5202 (camera on drone; "
                         "UL at UAV + DL at GCS). Default 0=off. Does NOT touch C2 :5760. "
                         "No longer conflicts with multi-rogue/microburst (those use ext-dn).")
    ap.add_argument("--companion-video-shape", choices=["iperf", "aviator"], default="aviator",
                    help="companion video micro-shape: iperf=CBR fixed-size (default); "
                         "aviator=realistic UAV packet-size distribution (Baltaci INFOCOM 2021) "
                         "paced to the same mean (AoI preserved; answers 'iperf is not real video').")
    ap.add_argument("--open-qgc", action="store_true",
                    help="operator view: launch the one-way MAVLink mirror + stock QGroundControl "
                         "(03_sitl/bin/QGroundControl.AppImage) on the host after preflight, so the "
                         "flight is visible live. Read-only (QGC cannot command the vehicle); "
                         "best-effort and never affects the run. Off by default.")
    ap.add_argument("--open-qgc-mitigation", action="store_true",
                    help="operator view with the FlyBlind detector: same as --open-qgc but opens the "
                         "custom QGC build (QGroundControl-mitigation.AppImage) that shows the central "
                         "'STALE STATE' C2-freshness alarm (second contribution). Mutually exclusive "
                         "with --open-qgc; same mirror/responder; best-effort; off by default.")
    ap.add_argument("--analysis-t-safe-ms", type=float, default=None,
                    help="post-processing only: T_safe_ms written to analysis_params.json")
    ap.add_argument("--analysis-e-safe-m", type=float, default=None,
                    help="post-processing only: E_safe_m written to analysis_params.json")
    ap.add_argument("--analysis-geofence-radius-m", type=float, default=None,
                    help="post-processing only: geofence radius (m) in analysis_params.json")
    ap.add_argument("--analysis-landing-radius-m", type=float, default=None,
                    help="post-processing only: landing zone radius (m) in analysis_params.json")
    ap.add_argument("--attack-seed", type=int, default=1)
    ap.add_argument("--reservation", type=int, default=0,
                    help="rRMPolicyMinRatio %% on C2 slice (cross) or sole slice (intra); 0=B0 baseline "
                         "(alias: use --min-ratio-slice-b for cross mitigation sweeps)")
    ap.add_argument("--mitigation-rate-limit-a", action="store_true",
                    help="apply an aggregate tc/TBF cap across active rogue PDU interfaces")
    ap.add_argument("--rate-limit-a-mbps", type=float, default=8.0, metavar="MBPS",
                    help="aggregate slice-A/rogue UL cap split equally across active rogues (default: 8)")
    ap.add_argument("--rate-limit-a-burst-kb", type=int, default=64, metavar="KB",
                    help="TBF burst allowance per rogue (default: 64 KB)")
    ap.add_argument("--rate-limit-a-latency-ms", type=int, default=50, metavar="MS",
                    help="maximum TBF queue latency (default: 50 ms)")
    ap.add_argument("--mitigation-aqm-ul", action="store_true",
                    help="apply HTB+fq_codel to the UAV companion-video UL PDU interface")
    ap.add_argument("--aqm-ul-interface", default="auto",
                    help="UAV PDU interface for AQM; auto selects oaitun_ue1")
    ap.add_argument("--aqm-ul-rate-mbps", type=float, default=14.0, metavar="MBPS",
                    help="HTB bottleneck rate feeding fq_codel (default: 14)")
    ap.add_argument("--aqm-ul-burst-kb", type=int, default=32, metavar="KB",
                    help="HTB burst allowance (default: 32 KB)")
    ap.add_argument("--aqm-ul-target-ms", type=float, default=5.0, metavar="MS",
                    help="fq_codel target sojourn time (default: 5 ms)")
    ap.add_argument("--aqm-ul-interval-ms", type=float, default=100.0, metavar="MS",
                    help="fq_codel interval (default: 100 ms)")
    ap.add_argument("--aqm-ul-limit-packets", type=int, default=1024, metavar="N",
                    help="fq_codel hard queue limit (default: 1024 packets)")
    ap.add_argument("--aqm-ul-ecn", action=argparse.BooleanOptionalAction, default=True,
                    help="enable ECN marking in fq_codel (default: enabled)")
    ap.add_argument("--aqm-ul-sample-hz", type=float, default=1.0, metavar="HZ",
                    help="during-run tc -s sampling rate on the UAV AQM interface (default: 1 Hz)")
    ap.add_argument("--aqm-ul-healthy-max-backlog-kb", type=float, default=64.0, metavar="KB",
                    help="ablation threshold: max observed fq_codel/HTB backlog for "
                         "local_queue_ok (default: 64 KB)")
    ap.add_argument("--aqm-ul-healthy-max-qlen", type=int, default=64, metavar="N",
                    help="ablation threshold: max observed qlen for local_queue_ok (default: 64)")
    ap.add_argument("--mitigation-ul-rrmpolicy", action="store_true",
                    help="activate NSSAI-keyed UL rrmPolicyRatio (3GPP-faithful UL two-level "
                         "scheduler; ratios from gNB rrmPolicy.json / A*|B* reservation; "
                         "auto-enabled when testbed stamp has MITIGATION_UL_RRMPOLICY=1)")
    ap.add_argument("--min-ratio-slice-b", type=int, default=None, metavar="PCT",
                    help="cross topology: MinRatio on slice B (C2 defense); applies live to gNB if "
                         "differs from cluster stamp (sets --reservation)")
    ap.add_argument("--min-ratio-slice-a", type=int, default=None, metavar="PCT",
                    help="cross topology: MinRatio on slice A (rogues / contrast); applies live to "
                         "gNB if differs from cluster stamp; stamp A* syncs when still unset/0")
    ap.add_argument("--no-apply-min-ratio", action="store_true",
                    help="do not patch gNB MIN_RATIO env; require stamp match (legacy preflight-only)")
    ap.add_argument("--results-tag", default=None, metavar="TOKEN",
                    help="naming-only folder token (does not change dataplane). "
                         "Example: RATELIMIT_A → MITIGATION_RATELIMIT_A so a rate-limit "
                         "control baseline does not share the paper B0 folder. Deduped if "
                         "the same token already comes from a mitigation flag.")
    ap.add_argument("--repeat", type=int, default=1, help="number of runs to execute")
    ap.add_argument("--min-valid-runs", type=int, default=None,
                    help="minimum valid runs required (default: same as --repeat)")
    ap.add_argument("--out", type=Path, default=None,
                    help="campaign/output directory. Default: artifacts/02_results/ "
                         "with campaign folders and RUNxx subfolders "
                         "(e.g. 106_PRB-CROSS-4_ROGUES-BASELINE-60S-12MPBS/RUN01). "
                         "If set explicitly, keeps legacy timestamped run dirs inside that path.")
    ap.add_argument("--skip-sitl-warmup", action="store_true",
                    help="skip direct SITL arm/disarm warm-up before setpoint-stream runs")
    ap.add_argument("--sitl-warmup-timeout", type=int, default=120,
                    help="seconds for pre-campaign SITL warm-up (setpoint-stream)")
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--min-samples", type=int, default=10000)
    ap.add_argument("--avail-min-pct", type=float, default=AVAILABILITY_MIN_PCT,
                    help="min post-warmup UDP availability gate (%%)")
    ap.add_argument("--host-loadavg-max", type=float, default=None,
                    help="max host loadavg1 during run (default: max(nproc-2, 2))")
    ap.add_argument("--host-cpu-busy-max-pct", type=float, default=HOST_CPU_BUSY_MAX_PCT,
                    help="max host CPU busy %% during run")
    ap.add_argument("--deviation-max-m", type=float, default=DEVIATION_MAX_M,
                    help="flyaway gate (m); aggressive missions transit ~340m to far setpoints")
    ap.add_argument("--cross-track-max-p99-m", type=float, default=CROSS_TRACK_BASELINE_MAX_P99_M,
                    help="baseline only: max cross-track p99 (m) for clean spatial control")
    ap.add_argument("--kube-context", default="kind-tm-oranslice-mitigation")
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--core-namespace", default=CORE_NAMESPACE_DEFAULT,
                    help="namespace of oai-upf (UPF CPU telemetry)")
    ap.add_argument("--upf-cpu-saturate-pct", type=float, default=UPF_CPU_SATURATE_PCT_DEFAULT,
                    help="soft threshold for gates.upf_cpu.saturated (%% of one core; default 85)")
    ap.add_argument("--skip-upf-telemetry", action="store_true",
                    help="disable oai-upf CPU sampling (shared-UPF attribution)")
    if any(a == "--mitigation-5qi" or a.startswith("--mitigation-5qi=") for a in (argv or [])):
        raise SystemExit(
            "--mitigation-5qi was removed: pinned ORANSlice 2024.w28 nrUE has no multi-PDU "
            "(OAI MR !3769). C2≠video QoS split is out of scope on the pinned nrUE stack."
        )

    ap.add_argument(
        "--sidecars",
        choices=["off", "MINIMAL", "FULL", "minimal", "full"],
        default="FULL",
        help="Sidecar profile (default FULL: per-leg OWD command+return, host load, ACK RTT, RLC, UE iface). "
             "FULL needs ACK_PROBE_SOCAT=1 at testbed bring-up (it is the default). Output under run_dir/sidecars/",
    )
    ap.add_argument(
        "--sidecars-duration-s",
        type=float,
        default=None,
        help="Override sidecar wall duration (default = envelope_s)",
    )
    ap.add_argument(
        "--video-stop-at-attack-end",
        action="store_true",
        help="DRAIN (b): stop companion video at attack_end + marker video_stop. "
             "Default (absent) is DRAIN (a): video continues for full envelope.",
    )
    ap.add_argument(
        "--drain-until-baseline",
        action="store_true",
        help="DRAIN 1.11: extend envelope by --drain-ceiling-s after attack; "
             "record recovery criterion in drain_policy.json",
    )
    ap.add_argument(
        "--drain-ceiling-s",
        type=float,
        default=180.0,
        help="Max extra seconds after attack_end when --drain-until-baseline (default 180)",
    )

    ap.add_argument(
        "--drain-baseline-run",
        default=None,
        help="1.11: path to a separate baseline run_dir for baseline_delta/belief; "
             "default = PRE window of the same run (baseline_source=pre_window_same_run)",
    )

    args = ap.parse_args(argv)
    if getattr(args, "c2_proxy", False):
        raise SystemExit(
            "--c2-proxy removed — C2 over 5G is UDP-only (no TCP MAVLink proxy). "
            "Use default UDP path (socat/c2_arrival_forwarder → SITL :5761)."
        )
    _tr = (getattr(args, "c2_transport", None) or "udp").lower()
    if _tr == "tcp":
        raise SystemExit(
            "--c2-transport tcp removed — UAV C2/telemetry MAVLink is UDP-only."
        )
    if _tr != "udp":
        raise SystemExit(f"--c2-transport {_tr} unsupported — UDP-only.")
    args.c2_proxy = False
    args.c2_transport = "udp"
    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    min_valid = args.min_valid_runs if args.min_valid_runs is not None else args.repeat
    if not 1 <= min_valid <= args.repeat:
        raise SystemExit("--min-valid-runs must be between 1 and --repeat")
    args._min_valid_runs = min_valid  # noqa: SLF001 — validated campaign threshold
    if args.host_loadavg_max is None:
        args.host_loadavg_max = default_host_loadavg_max()
    # Default results: artifacts/02_results + human per-run folders.
    # Explicit --out keeps legacy timestamped dirs under that campaign folder.
    if args.out is None:
        args.out = default_results_root()
        args._paper_results_layout = True  # noqa: SLF001
    else:
        args.out = Path(args.out).expanduser()
        args._paper_results_layout = False  # noqa: SLF001
    validate_numeric_args(args)
    return args


def validate_numeric_args(args) -> None:
    if args.host_loadavg_max is not None and args.host_loadavg_max <= 0:
        raise SystemExit("--host-loadavg-max must be > 0")
    if args.duration <= 0:
        raise SystemExit("--duration must be > 0")
    if args.warmup < 0 or args.warmup >= args.duration:
        raise SystemExit("--warmup must be >= 0 and < --duration")
    if (
        args.workload == "setpoint-stream"
        and args.skip_sitl_warmup
        and not args.allow_uninstrumented_setpoint_stream
    ):
        raise SystemExit(
            "--skip-sitl-warmup is blocked for formal setpoint-stream runs "
            "(cold SITL breaks pre-arm); omit the flag or pass "
            "--allow-uninstrumented-setpoint-stream for diagnostics only"
        )
    if args.min_samples <= 0:
        raise SystemExit("--min-samples must be > 0")
    if args.workload == "setpoint-stream":
        if args.c2_arrival_capture and args.c2_transport != "udp":
            raise SystemExit("--c2-arrival-capture requires --c2-transport udp")
        # UDP-direct (role B) is the only C2 path; allow uninstrumented setpoint-stream by default.
        if not args.allow_uninstrumented_setpoint_stream:
            args.allow_uninstrumented_setpoint_stream = True
        if args.setpoint_rate_hz <= 0:
            raise SystemExit("--setpoint-rate-hz must be > 0")
        if args.trajectory_duration_s is not None and args.trajectory_duration_s <= 0:
            raise SystemExit("--trajectory-duration-s must be > 0")
    if args.condition != "baseline" and args.attack == "cbr":
        if args.cbr_mbps <= 0:
            raise SystemExit("--cbr-mbps must be > 0")
        if args.cbr_parallel <= 0:
            raise SystemExit("--cbr-parallel must be > 0")
    if args.condition != "baseline" and args.attack == "microburst":
        if args.mean_rate_mbps <= 0:
            raise SystemExit("--mean-rate-mbps must be > 0")
        if args.burst_size_bytes <= 0:
            raise SystemExit("--burst-size-bytes must be > 0")
        if args.period_ms <= 0:
            raise SystemExit("--period-ms must be > 0")
    if args.min_ratio_slice_b is not None:
        if not 0 <= args.min_ratio_slice_b <= 100:
            raise SystemExit("--min-ratio-slice-b must be 0..100")
        if args.reservation not in (0, args.min_ratio_slice_b):
            raise SystemExit(
                f"conflicting --reservation={args.reservation} and "
                f"--min-ratio-slice-b={args.min_ratio_slice_b}"
            )
        args.reservation = args.min_ratio_slice_b
    if args.min_ratio_slice_a is None:
        args.min_ratio_slice_a = 0
    elif not 0 <= args.min_ratio_slice_a <= 100:
        raise SystemExit("--min-ratio-slice-a must be 0..100")
    if not 0 <= args.reservation <= 100:
        raise SystemExit("--reservation / --min-ratio-slice-b must be 0..100")
    if args.mitigation_rate_limit_a:
        if args.condition == "baseline":
            raise SystemExit("--mitigation-rate-limit-a requires attack/mitigation condition")
        if args.rate_limit_a_mbps <= 0:
            raise SystemExit("--rate-limit-a-mbps must be > 0")
        if args.rate_limit_a_burst_kb <= 0 or args.rate_limit_a_latency_ms <= 0:
            raise SystemExit("--rate-limit-a-burst-kb and --rate-limit-a-latency-ms must be > 0")
    if args.mitigation_aqm_ul:
        if args.aqm_ul_rate_mbps <= 0:
            raise SystemExit("--aqm-ul-rate-mbps must be > 0")
        if args.aqm_ul_burst_kb <= 0 or args.aqm_ul_limit_packets <= 0:
            raise SystemExit("--aqm-ul-burst-kb and --aqm-ul-limit-packets must be > 0")
        if args.aqm_ul_target_ms <= 0 or args.aqm_ul_interval_ms <= args.aqm_ul_target_ms:
            raise SystemExit("--aqm-ul-interval-ms must be greater than positive --aqm-ul-target-ms")
        if float(getattr(args, "aqm_ul_sample_hz", 1.0) or 0) <= 0:
            raise SystemExit("--aqm-ul-sample-hz must be > 0")
        if float(getattr(args, "aqm_ul_healthy_max_backlog_kb", 64.0) or 0) <= 0:
            raise SystemExit("--aqm-ul-healthy-max-backlog-kb must be > 0")
        if int(getattr(args, "aqm_ul_healthy_max_qlen", 64) or 0) <= 0:
            raise SystemExit("--aqm-ul-healthy-max-qlen must be > 0")
    if args.mitigation_ul_rrmpolicy and args.topology != "cross":
        raise SystemExit("--mitigation-ul-rrmpolicy requires --topology cross")
    if args.topology == "intra" and args.min_ratio_slice_b is not None:
        print("[warn] --min-ratio-slice-b ignored for intra; use --reservation", file=sys.stderr)
    if args.topology == "intra" and int(args.min_ratio_slice_a or 0) > 0:
        print("[warn] --min-ratio-slice-a ignored for intra; use --reservation", file=sys.stderr)
    if companion_video_mbps(args) < 0:
        raise SystemExit("--companion-video-mbps must be >= 0")
    if float(getattr(args, "attack_start_after_s", 0.0) or 0.0) < 0:
        raise SystemExit("--attack-start-after-s must be >= 0")
    if args.attack in MULTI_ROGUE_ATTACKS:
        if not 1 <= args.multi_rogue_count <= 4:
            raise SystemExit("--multi-rogue-count must be between 1 and 4")
        if args.multi_rogue_phase_ms < 0:
            raise SystemExit("--multi-rogue-phase-ms must be >= 0")
        # Sync peaks use --multi-sync-*; continuous modes need mean rate.
        if (
            args.attack not in ("multi-rogue-sync", "multi-rogue-dl-sync")
            and args.mean_rate_mbps <= 0
        ):
            raise SystemExit("--mean-rate-mbps must be > 0")
    if (
        companion_video_mbps(args) > 0
        and args.condition != "baseline"
        and companion_conflicts_with_attack(args.attack)
    ):
        raise SystemExit(
            f"--companion-video-mbps incompatible with --attack {args.attack} "
            f"(legacy gate; UAV→GCS should not conflict)"
        )
    if args.attack == "sync-prb" and args.sync_ack_target:
        raise SystemExit(
            "sync-prb + --sync-ack-target forbidden (targets GCS/victim); "
            "pure contention uses ext-dn only"
        )
    if args.sync_open_loop and args.attack not in SYNC_STYLE_ATTACKS:
        raise SystemExit(
            "--sync-open-loop requires --attack "
            "multi-rogue-sync|multi-rogue-dl-sync"
        )
    if args.sync_phase_ms < 0:
        raise SystemExit("--sync-phase-ms must be >= 0")
    if args.attack in ("multi-rogue-sync", "multi-rogue-dl-sync"):
        if args.sync_ack_target:
            raise SystemExit(
                f"{args.attack} + --sync-ack-target forbidden: pure attack (no victim endpoint)"
            )
        if args.multi_sync_burst_ms <= 0 or args.multi_sync_pps <= 0 or args.multi_sync_size <= 0:
            raise SystemExit(f"{args.attack} peak parameters must be > 0")
        period_ms = 1000.0 / args.setpoint_rate_hz
        if args.multi_sync_burst_ms >= period_ms:
            raise SystemExit(
                "--multi-sync-burst-ms must be shorter than the setpoint period "
                f"({period_ms:g}ms) for the attack to stay bursty"
            )


def _host_proc_running(pattern: str) -> bool:
    """True if a host process matching pattern is running (pgrep)."""
    try:
        return subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True
        ).returncode == 0
    except Exception:
        return False


def open_qgc_view(ctx: str, ns: str, ips: dict, appimage: str = QGC_APPIMAGE) -> None:
    """Best-effort operator view: launch a one-way MAVLink mirror in the GCS pod
    (copies post-5G telemetry to the host) and a QGroundControl AppImage on the
    host. `appimage` selects which build: the stock QGC (--open-qgc) or the custom
    FlyBlind detector build (--open-qgc-mitigation) — the mirror/responder plumbing
    is identical, only the binary differs. Read-only: QGC can display
    mode/armed/takeoff/position but has no path back to the vehicle, so it cannot
    interfere with the run. Any failure is swallowed — this must never affect
    results (headless/CI stays safe)."""
    uav_ip = (ips or {}).get("uav_ip")
    if not uav_ip:
        print("[qgc] skipped: UAV IP not resolved", flush=True)
        return
    # 0) QGC responder (host shim): answers param/fence/rally so the read-only
    #    mirror does not trigger QGC's "did not respond" dialogs. If it is up, the
    #    mirror targets it; if it cannot start, we fall back to mirror -> QGC direct
    #    (old behaviour) so the flight view is never lost.
    mirror_target_port = QGC_PORT
    try:
        if _host_proc_running("qgc_responder"):
            print("[qgc] responder already running — reusing", flush=True)
            mirror_target_port = QGC_SHIM_POD_PORT
        elif not QGC_RESPONDER_SCRIPT.exists():
            print(f"[qgc] responder script missing: {QGC_RESPONDER_SCRIPT} "
                  "(mirror -> QGC direct; param/fence warnings may appear)", flush=True)
        else:
            resp_proc = subprocess.Popen(
                ["python3", str(QGC_RESPONDER_SCRIPT),
                 str(QGC_SHIM_POD_PORT), "127.0.0.1", str(QGC_PORT)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(1.0)  # let it bind; if it crashed (e.g. no pymavlink), fall back
            if resp_proc.poll() is None and _host_proc_running("qgc_responder"):
                mirror_target_port = QGC_SHIM_POD_PORT
                print(f"[qgc] responder up: pod:{QGC_SHIM_POD_PORT} -> QGC "
                      f"127.0.0.1:{QGC_PORT} (answers params/fence/rally)", flush=True)
            else:
                print("[qgc] responder failed to start — mirror -> QGC direct "
                      "(param/fence warnings may appear)", flush=True)
    except Exception as e:
        print(f"[qgc] responder skipped: {e} (mirror -> QGC direct)", flush=True)
    # 1) telemetry mirror (post-5G copy -> responder/QGC); skip if already running.
    try:
        if _host_proc_running("qgc_mavlink_mirror"):
            print("[qgc] mirror already running — reusing", flush=True)
        elif not QGC_MIRROR_SCRIPT.exists():
            print(f"[qgc] mirror script missing: {QGC_MIRROR_SCRIPT}", flush=True)
        else:
            with QGC_MIRROR_SCRIPT.open("rb") as _mirror_src:
                subprocess.Popen(
                    ["kubectl", "--context", ctx, "exec", "-i", "-n", ns,
                     "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
                     "python3", "-", uav_ip, "5760", QGC_HOST, str(mirror_target_port)],
                    stdin=_mirror_src,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            _viachip = " (via responder)" if mirror_target_port == QGC_SHIM_POD_PORT else ""
            print(f"[qgc] mirror up: {uav_ip}:5760 -> {QGC_HOST}:{mirror_target_port}{_viachip}", flush=True)
    except Exception as e:
        print(f"[qgc] mirror launch skipped: {e}", flush=True)
    # 2) QGroundControl app on the host; skip if any QGC is already open.
    try:
        if _host_proc_running("QGroundControl"):
            print("[qgc] QGroundControl already open — reusing", flush=True)
        elif not os.path.exists(appimage):
            print(f"[qgc] AppImage not found: {appimage} "
                  "(set QGC_APPIMAGE[_MITIGATION]=/path or open QGC manually)", flush=True)
        else:
            subprocess.Popen(
                [appimage],
                env={**os.environ,
                     "DISPLAY": os.environ.get("DISPLAY", ":0"),
                     "QT_QPA_PLATFORM": "xcb"},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[qgc] QGroundControl launched ({appimage}) — "
                  "autoconnects on UDP %d" % QGC_PORT, flush=True)
    except Exception as e:
        print(f"[qgc] QGroundControl launch skipped: {e}", flush=True)


def close_qgc_view(ctx: str, ns: str) -> None:
    """Tear down the operator view opened by --open-qgc — QGroundControl, the host
    responder, and the in-pod mirror — so it closes together with the run. Best-
    effort; never raises. Bracket patterns ([x]) keep pkill from matching this
    orchestrator process."""
    closed: list[str] = []
    # QGroundControl: the .AppImage launcher execs a mounted binary
    # (/tmp/.mount_*/usr/bin/QGroundControl), so match the broad name — not just
    # ".AppImage" — to catch the real window process, then SIGKILL any survivor.
    try:
        if subprocess.run(["pkill", "-f", "QGroundControl"],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            closed.append("QGroundControl")
            time.sleep(0.5)
            subprocess.run(["pkill", "-9", "-f", "QGroundControl"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    # Host responder.
    try:
        if subprocess.run(["pkill", "-f", "[q]gc_responder"],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            closed.append("responder")
    except Exception:
        pass
    # Host-side mirror launcher (kubectl exec holding the pod-side python).
    try:
        subprocess.run(["pkill", "-f", f"5760 {QGC_HOST}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    # In-pod mirror process (root-owned; must be killed inside the pod).
    try:
        if subprocess.run(
            ["kubectl", "--context", ctx, "exec", "-n", ns,
             "deploy/oai-ue-gcs", "-c", "nr-ue", "--",
             "pkill", "-f", f"5760 {QGC_HOST}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            closed.append("mirror")
    except Exception:
        pass
    if closed:
        print(f"[qgc] operator view closed ({', '.join(sorted(set(closed)))})", flush=True)


def main(argv=None):
    campaign_t0 = time.monotonic()
    args = parse_args(argv if argv is not None else sys.argv[1:])
    ctx, ns = args.kube_context, args.namespace
    need_rogue = args.condition != "baseline"
    rogue_count = rogue_count_for_args(args)

    Path(args.out).mkdir(parents=True, exist_ok=True)
    paper_layout = bool(getattr(args, "_paper_results_layout", False))
    stamp_extras = stamp_extra_rogue_count()
    # Stamp must run before campaign naming so A60 / AQM / UL-rrm tokens land
    # in the folder (preflight also re-applies; idempotent).
    stamp_early = read_topology_stamp()
    if stamp_early:
        apply_mitigation_stamp_defaults(args, stamp_early)
    optc_mismatch = optc_attack_onset_mismatch(args)
    if optc_mismatch:
        raise SystemExit(f"[optc] ABORT before preflight: {optc_mismatch}")
    campaign_dir = resolve_campaign_dir(
        args, Path(args.out), paper_layout=paper_layout, stamp_extra_rogues=stamp_extras,
    )
    campaign_dir.mkdir(parents=True, exist_ok=True)
    args.out = campaign_dir
    terminal_transcript = start_campaign_transcript(campaign_dir)

    _campaign_cleaned = False
    campaign_exit = 1

    def finish_campaign(exit_code: int) -> int:
        nonlocal _campaign_cleaned, campaign_exit
        campaign_exit = exit_code
        if not _campaign_cleaned:
            _campaign_cleaned = True
            if args.mitigation_rate_limit_a or args.mitigation_aqm_ul:
                remove_runtime_mitigation_qdiscs(ctx, ns, rogue_count=max(1, rogue_count))
            if args.mitigation_ul_rrmpolicy:
                disable_ul_rrmpolicy(ctx, ns)
            if getattr(args, "_qgc_view_opened", False):
                close_qgc_view(ctx, ns)
            terminal_transcript.close()
        return campaign_exit

    try:
        return _main_campaign_body(
            args, ctx, ns, need_rogue, rogue_count, campaign_dir, paper_layout,
            stamp_extras, campaign_t0, finish_campaign,
        )
    except Exception:
        # Ensure qdisc/policy cleanup on unexpected failures; then re-raise.
        finish_campaign(1)
        raise
    finally:
        if not _campaign_cleaned:
            finish_campaign(campaign_exit)


def _main_campaign_body(
    args, ctx, ns, need_rogue, rogue_count, campaign_dir, paper_layout,
    stamp_extras, campaign_t0, finish_campaign,
):
    if args.condition == "mitigation" and args.reservation == 0:
        print("[warn] mitigation with reservation=0 — no PRB reservation applied", file=sys.stderr)
    if need_rogue:
        try:
            validate_co_tenant_params(args)
        except SystemExit as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return finish_campaign(2)

    analysis_name = allocate_unique_filename(Path(args.out), "analysis_params.json")
    write_analysis_params(Path(args.out) / analysis_name, build_analysis_params(args))
    print_campaign_banner(args, testbed_rogue_capacity(stamp_extra_rogues=stamp_extras))
    print(f"[output] {campaign_dir}/", flush=True)

    if not args.no_apply_min_ratio:
        ok, err = apply_rrm_min_ratio_if_needed(ctx, ns, args)
        if not ok:
            print(f"[ERROR] min-ratio apply failed: {err}", file=sys.stderr)
            return finish_campaign(2)
        if err:
            print(f"[min-ratio] {err}", flush=True)

    problems, ips = preflight(ctx, ns, args, need_rogue)
    if problems:
        print("PREFLIGHT FAILED:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        print("Start/validate the cluster: cd ../01_testbeds && ./start_testbed.sh "
              + ("cross " if args.topology == "cross" else "")
              + ("B60" if args.reservation == 60 else "B0"), file=sys.stderr)
        return finish_campaign(2)
    print(f"[preflight] OK — GCS={ips['gcs_ip']} UAV={ips['uav_ip']} node={ips['node']} nproc={nproc()}")
    if getattr(args, "open_qgc_mitigation", False):
        open_qgc_view(ctx, ns, ips, appimage=QGC_APPIMAGE_MITIGATION)
        args._qgc_view_opened = True  # noqa: SLF001 — tells finish_campaign to close it
    elif getattr(args, "open_qgc", False):
        open_qgc_view(ctx, ns, ips)
        args._qgc_view_opened = True  # noqa: SLF001 — tells finish_campaign to close it
    for tip in warn_paper_capture_flags(args):
        print(f"[paper] {tip}", flush=True)

    tool_problems = sync_c2_tools_from_host(ctx, ns, need_rogue=need_rogue, rogue_count=rogue_count)
    if tool_problems:
        print("[warn] C2 tool sync:", "; ".join(tool_problems), file=sys.stderr)
    if need_rogue and args.attack in (
        "microburst", "dual-pulse", "multi-rogue", "multi-rogue-dl",
        "multi-rogue-dl-sync",
    ):
        ext_problems = sync_ext_dn_tools(ctx)
        if ext_problems:
            print("[warn] ext-DN tool sync:", "; ".join(ext_problems), file=sys.stderr)

    slug = build_run_slug(args)
    valids, all_runs = [], []
    min_valid = args._min_valid_runs
    try:
        for run_num in range(1, args.repeat + 1):
            run_id = resolve_run_dirname(
                args,
                run_num,
                Path(args.out),
                paper_layout=paper_layout,
                legacy_slug=slug,
                recovery=False,
                clamp_fn=clamp_run_dirname,
            )
            soft_reset(
                ctx, ns, need_rogue=need_rogue, rogue_count=rogue_count,
                companion_video=companion_video_mbps(args) > 0,
            )
            ips = refresh_run_ips(ctx, ns, ips)
            env_ok, env_audit = audit_run_environment(ctx, ns, args, need_rogue=need_rogue)
            if not env_ok:
                print(f"[ERROR] pre-run audit failed: {env_audit.get('reason')}", file=sys.stderr)
                print(json.dumps(env_audit, indent=2), file=sys.stderr)
                return finish_campaign(2)
            try:
                wait_run_cooldown(
                    ctx, ns, c2_proxy=args.c2_proxy, max_wait_s=cooldown_max_wait_s(args),
                )
            except RuntimeError as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                return finish_campaign(2)
            if is_sitl_dirty(ctx, ns) and args.workload == "setpoint-stream":
                print("[recovery] SITL dirty flag set — restarting UAV before run…", flush=True)
                rec_ok, rec_info = recover_sitl_resilient(
                    ctx, ns, args, "dirty flag from prior failure",
                )
                if not rec_ok:
                    print(f"[ERROR] SITL recovery failed: {rec_info}", file=sys.stderr)
                    print(
                        "[ERROR] UAV/SITL did not recover — "
                        "cd ../01_testbeds && ./stop_testbed.sh && "
                        "./start_testbed.sh",
                        file=sys.stderr,
                    )
                    return finish_campaign(2)
            if should_run_sitl_warmup_preflight(args, run_num):
                sitl_port = sitl_listen_port(args.c2_proxy)
                print(
                    "[preflight] SITL warm-up r%d (direct :%d %s, after soft_reset)…"
                    % (run_num, sitl_port, getattr(args, "c2_transport", "udp")),
                    flush=True,
                )
                ok, info = run_sitl_warmup(
                    ctx, ns, timeout_s=float(args.sitl_warmup_timeout),
                    c2_proxy=args.c2_proxy, c2_transport=getattr(args, "c2_transport", "udp"),
                )
                if not ok:
                    print("[ERROR] SITL warm-up failed:", file=sys.stderr)
                    print(info, file=sys.stderr)
                    return finish_campaign(2)
                print("[preflight] SITL warm-up OK — %s" % info, flush=True)
            try:
                configure_ul_rrmpolicy(args, ctx, ns)
                args._runtime_mitigation_audit = apply_runtime_mitigations(args, ctx, ns)
            except RuntimeError as exc:
                print(f"[ERROR] mitigation not applied: {exc}", file=sys.stderr)
                return finish_campaign(2)
            run_ids_this_round: list[str] = []
            try:
                run_meta, four = run_single(args, ctx, ns, ips, run_id, seed=run_num)
            except RuntimeError as exc:
                print(f"[ERROR] run {run_id} aborted: {exc}", file=sys.stderr)
                mark_sitl_dirty(ctx, ns, str(exc))
                soft_reset(ctx, ns, need_rogue=need_rogue, rogue_count=rogue_count,
                        companion_video=companion_video_mbps(args) > 0)
                all_runs.append(run_id)
                continue
            needs_recovery, recovery_reason = mission_failure_needs_sitl_recovery(run_meta)
            if (
                not run_meta["valid"]
                and needs_recovery
                and args.workload == "setpoint-stream"
                and not args.skip_sitl_warmup
            ):
                print(
                    "  [retry] mission failure (%s) — full SITL recovery + retry…"
                    % recovery_reason[:80],
                    flush=True,
                )
                run_ids_this_round.append(run_id)
                rec_ok, rec_info = recover_sitl_resilient(
                    ctx, ns, args, recovery_reason,
                )
                if rec_ok:
                    ips = refresh_run_ips(ctx, ns, ips)
                    try:
                        configure_ul_rrmpolicy(args, ctx, ns)
                        args._runtime_mitigation_audit = apply_runtime_mitigations(args, ctx, ns)
                    except RuntimeError as exc:
                        print(f"[ERROR] mitigation not reapplied after recovery: {exc}", file=sys.stderr)
                        return finish_campaign(2)
                    try:
                        wait_run_cooldown(
                            ctx, ns, c2_proxy=args.c2_proxy, max_wait_s=cooldown_max_wait_s(args),
                        )
                    except RuntimeError as exc:
                        print(f"[ERROR] {exc}", file=sys.stderr)
                        return finish_campaign(2)
                    retry_id = resolve_run_dirname(
                        args,
                        run_num,
                        Path(args.out),
                        paper_layout=paper_layout,
                        legacy_slug=slug,
                        recovery=True,
                        clamp_fn=clamp_run_dirname,
                    )
                    try:
                        run_meta, four = run_single(args, ctx, ns, ips, retry_id, seed=run_num)
                    except RuntimeError as exc:
                        print(f"[ERROR] run {retry_id} aborted: {exc}", file=sys.stderr)
                        mark_sitl_dirty(ctx, ns, str(exc))
                        soft_reset(ctx, ns, need_rogue=need_rogue, rogue_count=rogue_count,
                        companion_video=companion_video_mbps(args) > 0)
                        all_runs.extend(run_ids_this_round + [retry_id])
                        continue
                    run_ids_this_round.append(retry_id)
                else:
                    print(f"[ERROR] SITL recovery failed: {rec_info}", file=sys.stderr)
            all_runs.extend(run_ids_this_round or [run_id])
            stressed, stress_reason = attack_run_stressed_sitl(run_meta)
            if stressed:
                mark_sitl_dirty(ctx, ns, stress_reason)
                print("  [recovery] SITL marked dirty — %s" % stress_reason, flush=True)
            if run_meta["valid"]:
                valids.append(four)
            else:
                print(f"  -> run {run_id} INVALID (metrics saved; continuing to r{run_num + 1})")
    except KeyboardInterrupt:
        print("\n[interrupt] cleaning residual processes…", flush=True)
        cr = CURRENT_RUN
        if cr.get("run_dir") and Path(cr["run_dir"]).is_dir():
            audit = audit_post_run_cleanup(ctx, ns, args, need_rogue=need_rogue)
            ports = check_uav_mavlink_ports(ctx, ns, args.c2_proxy)
            write_keyboard_interrupt_run_meta(
                Path(cr["run_dir"]), str(cr["run_id"]), args,
                wall_t0=float(cr["wall_t0"]),
                cleanup_audit=audit, port_check=ports,
            )
        soft_reset(ctx, ns, need_rogue=need_rogue, rogue_count=rogue_count,
                        companion_video=companion_video_mbps(args) > 0)
        CURRENT_RUN.clear()
        return finish_campaign(130)

    wall_total_s = round(time.monotonic() - campaign_t0, 1)
    summary = {
        "requested_runs": args.repeat, "executed_runs": len(all_runs),
        "valid_runs": len(valids), "min_valid_runs": min_valid,
        "all_runs": all_runs,
        "run_slug": slug,
        "condition": args.condition, "topology": args.topology, "gnb_prb": args.gnb_prb,
        "workload": args.workload,
        "attack": None if args.condition == "baseline" else args.attack,
        "duration_s": args.duration,
        "wall_total_s": wall_total_s,
    }
    if valids:
        summary["aggregate"] = aggregate(valids, args)
    matrix_name = allocate_unique_filename(
        Path(args.out), f"matrix_{slug}_{args.duration}s.json"
    )
    (Path(args.out) / matrix_name).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print_campaign_result(
        executed=len(all_runs),
        requested=args.repeat,
        valid=len(valids),
        minimum=min_valid,
        output_dir=Path(args.out),
        elapsed_s=wall_total_s,
    )
    return finish_campaign(0 if len(valids) >= min_valid else 1)


if __name__ == "__main__":
    sys.exit(main())

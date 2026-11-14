#!/usr/bin/env python3
##################################################################################################################################
# Module: run_sidecars.py (Sidecar supervisor (covers the full wall-clock of an orchestrate run))                                #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Does NOT start/stop the testbed or orchestrate. Writes into --out-dir (typically run_dir/sidecars/).              #
#                                                                                                                                #
# Profiles: MINIMAL   = leg_owd_probe (command + return legs) + host_load (the legacy 47999 probe is NOT part of the             #
#   profile: it would collide with the orchestrate NETWORK layer; use --with-legacy-owd only in standalone mode) FULL = MINIMAL  #
#   + ACK RTT + RLC scraper + UE iface sampler                                                                                   #
#                                                                                                                                #
# Legacy CLI flags (--with-ul-owd / --with-ack-rtt / …) still work; when --profile is set it selects the probe set above.        #
#                                                                                                                                #
# Duration guidance: sidecars >= orchestrate envelope (NI ~300 s, campaign ~900 s).                                              #
##################################################################################################################################
"""Sidecar supervisor (covers the full wall-clock of an orchestrate run).

Does NOT start/stop the testbed or orchestrate. Writes into --out-dir
(typically run_dir/sidecars/).

Profiles:
  MINIMAL   = leg_owd_probe (command + return legs) + host_load
             (the legacy 47999 probe is NOT part of the profile: it would collide with the
              orchestrate NETWORK layer; use --with-legacy-owd only in standalone mode)
  FULL = MINIMAL + ACK RTT + RLC scraper + UE iface sampler

Legacy CLI flags (--with-ul-owd / --with-ack-rtt / …) still work; when
--profile is set it selects the probe set above.

Duration guidance: sidecars >= orchestrate envelope (NI ~300 s, campaign ~900 s).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBES = HERE.parent / "probes"
OWD_PROBE = PROBES / "owd_probe.py"
LEG_OWD = PROBES / "leg_owd_probe.py"

PROFILES = ("MINIMAL", "FULL")

# Probe B: second instance on the return leg, same program, different format
# and different port (M3). Opt-in: not part of any profile by default, so as not to
# change the validated instrumentation load. Enabled by flag --with-return-probe-b or env
# RETURN_PROBE_B=1. Default format: 64 B at 10 Hz on port 48004 (return-leg probe A
# stays at 140 B / 25 Hz on 48003).
RETURN_PROBE_B_DEFAULT_PAYLOAD = 64
RETURN_PROBE_B_DEFAULT_RATE_HZ = 10.0
RETURN_PROBE_B_DEFAULT_PORT = 48004
RETURN_PROBE_B_TMP_TAG = "b"


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "none")


def _env_num(name: str, default, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return cast(raw)


def _discover(ctx: str, ns: str) -> tuple[str, str]:
    r = subprocess.run(
        [sys.executable, str(HERE / "discover_pdu_ips.py"), "--context", ctx, "--namespace", ns],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        sys.exit("PDU discovery failed: %s" % (r.stderr or r.stdout))
    d = json.loads(r.stdout)
    return d["gcs_ip"], d["uav_ip"]


def profile_flags(profile: str) -> dict[str, bool]:
    p = (profile or "").strip().upper()
    if p not in PROFILES:
        raise ValueError("profile must be MINIMAL or FULL, got %r" % profile)
    base = {
        "leg_owd": True,
        # Off by default: orchestrate already runs probes/owd_probe.py on UDP 47999
        # as its NETWORK layer. Starting it again from the sidecar would collide on
        # the UAV receiver port 47999 and clobber /tmp/owd_recv.json. Opt in with
        # --with-legacy-owd only when running the sidecar supervisor standalone.
        "legacy_owd_47999": False,
        "host_load": True,
        "ack_rtt": False,
        "rlc": False,
        "ue_iface": False,
    }
    if p == "FULL":
        base.update({"ack_rtt": True, "rlc": True, "ue_iface": True})
    return base


def main() -> None:
    ap = argparse.ArgumentParser(description="Sidecar supervisor")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument(
        "--duration",
        type=float,
        required=True,
        help="Wall-clock coverage in seconds (>= orchestrate envelope)",
    )
    ap.add_argument(
        "--profile",
        choices=["MINIMAL", "FULL", "minimal", "full"],
        default="",
        help="MINIMAL or FULL. Empty = legacy flag mode.",
    )
    ap.add_argument("--context", default="kind-tm-oranslice")
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--gcs-ip", default="")
    ap.add_argument("--uav-ip", default="")
    ap.add_argument("--with-ul-owd", action="store_true",
                    help="Legacy: ul_owd_probe (48001). Prefer --profile.")
    ap.add_argument("--with-legacy-owd", action="store_true",
                    help="Start probes/owd_probe.py on UDP 47999 (Gate legacy).")
    ap.add_argument("--with-leg-owd", action="store_true",
                    help="Start leg_owd_probe for the command and return legs.")
    ap.add_argument("--with-return-probe-b", action="store_true",
                    help="Second return-leg probe (M3): different format/port, in "
                         "parallel with the 140 B/25 Hz one. Also enabled by env RETURN_PROBE_B=1.")
    ap.add_argument("--return-probe-b-payload-bytes", type=int, default=None,
                    help=f"Probe B payload (default {RETURN_PROBE_B_DEFAULT_PAYLOAD}; "
                         "env RETURN_PROBE_B_PAYLOAD_BYTES).")
    ap.add_argument("--return-probe-b-rate-hz", type=float, default=None,
                    help=f"Probe B rate (default {RETURN_PROBE_B_DEFAULT_RATE_HZ}; "
                         "env RETURN_PROBE_B_RATE_HZ).")
    ap.add_argument("--return-probe-b-port", type=int, default=None,
                    help=f"Probe B port (default {RETURN_PROBE_B_DEFAULT_PORT}; "
                         "env RETURN_PROBE_B_PORT).")
    ap.add_argument("--with-ack-rtt", action="store_true")
    ap.add_argument("--with-rlc", action="store_true")
    ap.add_argument("--with-ue-iface", action="store_true")
    ap.add_argument(
        "--ack-via-c2-udp",
        action="store_true",
        help="DANGEROUS: ACK over udp:UAV:5760 (can steal mission C2). Off by default.",
    )
    ap.add_argument(
        "--ack-local-sitl-tcp",
        action="store_true",
        help="DEBUG: ACK on UAV tcp:5762 (not 5G). Off by default.",
    )
    ap.add_argument("--ack-udp-port", type=int, default=5763)
    ap.add_argument(
        "--timeout-s",
        type=float,
        default=30.0,
        help="COMMAND_ACK per-probe timeout passed to command_ack_rtt.py (plan default: 30).",
    )
    ap.add_argument("--iface-interval-s", type=float, default=2.0)
    ap.add_argument("--host-load-interval-s", type=float, default=2.0)
    ap.add_argument("--no-host-load", action="store_true")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned children and exit without starting processes.",
    )
    args = ap.parse_args()

    if args.duration < 250:
        print(
            f"[warn] --duration={args.duration}s is short vs orchestrate wall-clock; "
            "NI 120s mission needs ~300s; 600s campaign needs ~900s",
            flush=True,
        )

    profile_name = (args.profile or "").upper() or None
    if profile_name:
        flags = profile_flags(profile_name)
        if args.no_host_load:
            flags["host_load"] = False
    else:
        flags = {
            "leg_owd": bool(args.with_leg_owd),
            "legacy_owd_47999": bool(args.with_legacy_owd),
            "host_load": not args.no_host_load,
            "ack_rtt": bool(args.with_ack_rtt),
            "rlc": bool(args.with_rlc),
            # Legacy default: always ran rlc + ue_iface; keep that only if neither
            # profile nor explicit flags — preserve old behaviour when no flags.
            "ue_iface": bool(args.with_ue_iface),
        }
        # Back-compat: bare run_sidecars (no profile, no selective flags) used to
        # always start rlc + ue_iface + host_load. Preserve when nothing selected.
        selective = (
            args.with_ul_owd or args.with_legacy_owd or args.with_leg_owd
            or args.with_ack_rtt or args.with_rlc or args.with_ue_iface
        )
        if not selective:
            flags["rlc"] = True
            flags["ue_iface"] = True

    want_ul_legacy_instr = bool(args.with_ul_owd) and not profile_name

    gcs_ip, uav_ip = args.gcs_ip, args.uav_ip
    need_ips = (
        flags["leg_owd"]
        or flags["legacy_owd_47999"]
        or want_ul_legacy_instr
        or (flags["ack_rtt"] and not args.ack_local_sitl_tcp)
    )
    if need_ips and (not gcs_ip or not uav_ip) and not args.dry_run:
        dg, du = _discover(args.context, args.namespace)
        gcs_ip = gcs_ip or dg
        uav_ip = uav_ip or du
        print(f"[sidecar] PDU IPs gcs={gcs_ip} uav={uav_ip}", flush=True)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    procs: list[tuple[str, subprocess.Popen]] = []
    planned: list[dict[str, object]] = []

    def start(name: str, cmd: list[str]) -> None:
        planned.append({"name": name, "cmd": cmd})
        if args.dry_run:
            print(f"[sidecar:dry-run] {name}: {' '.join(cmd)}", flush=True)
            return
        log = open(out / f"{name}.live.log", "w", encoding="utf-8")
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        procs.append((name, p))
        print(f"[sidecar] started {name} pid={p.pid}", flush=True)

    if flags["rlc"]:
        start(
            "rlc_rejects",
            [
                sys.executable, str(HERE / "rlc_reject_scraper.py"),
                "--context", args.context, "--namespace", args.namespace,
                "--follow", "--duration", str(args.duration),
                "--out", str(out / "flyblind_rlc_rejects.json"),
                "--events-out", str(out / "flyblind_rlc_rejects.jsonl"),
            ],
        )
    if flags["ue_iface"]:
        start(
            "ue_iface",
            [
                sys.executable, str(HERE / "ue_iface_sampler.py"),
                "--context", args.context, "--namespace", args.namespace,
                "--duration", str(args.duration),
                "--interval-s", str(args.iface_interval_s),
                "--out", str(out / "flyblind_ue_iface.jsonl"),
                "--summary-out", str(out / "flyblind_ue_iface_summary.json"),
            ],
        )
    if flags["host_load"]:
        start(
            "host_load",
            [
                sys.executable, str(HERE / "host_load_sampler.py"),
                "--duration", str(args.duration),
                "--interval-s", str(args.host_load_interval_s),
                "--out", str(out / "flyblind_host_load.jsonl"),
                "--summary-out", str(out / "flyblind_host_load_summary.json"),
            ],
        )
    return_probe_b_on = bool(args.with_return_probe_b) or _env_on("RETURN_PROBE_B")
    return_probe_b_payload = (
        args.return_probe_b_payload_bytes
        if args.return_probe_b_payload_bytes is not None
        else _env_num("RETURN_PROBE_B_PAYLOAD_BYTES", RETURN_PROBE_B_DEFAULT_PAYLOAD, int)
    )
    return_probe_b_rate = (
        args.return_probe_b_rate_hz
        if args.return_probe_b_rate_hz is not None
        else _env_num("RETURN_PROBE_B_RATE_HZ", RETURN_PROBE_B_DEFAULT_RATE_HZ, float)
    )
    return_probe_b_port = (
        args.return_probe_b_port
        if args.return_probe_b_port is not None
        else _env_num("RETURN_PROBE_B_PORT", RETURN_PROBE_B_DEFAULT_PORT, int)
    )

    if flags["leg_owd"]:
        if not args.dry_run and not (gcs_ip and uav_ip):
            sys.exit("leg_owd requires PDU IPs")
        for leg in ("command", "return"):
            start(
                f"leg_owd_{leg}",
                [
                    sys.executable, str(LEG_OWD),
                    "--leg", leg,
                    "--gcs-ip", gcs_ip or "0.0.0.0",
                    "--uav-ip", uav_ip or "0.0.0.0",
                    "--duration", str(args.duration),
                    "--context", args.context, "--namespace", args.namespace,
                    "--out-dir", str(out / f"probes_{leg}"),
                ],
            )
        if return_probe_b_on:
            # Same return leg, different format and port. Own tmp-tag
            # so it does not collide in the GCS pod /tmp with probe A (140 B/25 Hz).
            start(
                "leg_owd_return_b",
                [
                    sys.executable, str(LEG_OWD),
                    "--leg", "return",
                    "--gcs-ip", gcs_ip or "0.0.0.0",
                    "--uav-ip", uav_ip or "0.0.0.0",
                    "--duration", str(args.duration),
                    "--rate-hz", str(return_probe_b_rate),
                    "--payload-bytes", str(return_probe_b_payload),
                    "--port", str(return_probe_b_port),
                    "--tmp-tag", RETURN_PROBE_B_TMP_TAG,
                    "--context", args.context, "--namespace", args.namespace,
                    "--out-dir", str(out / "probes_return_b"),
                ],
            )
    if flags["legacy_owd_47999"] and OWD_PROBE.is_file():
        if not args.dry_run and not (gcs_ip and uav_ip):
            sys.exit("legacy_owd_47999 requires PDU IPs")
        start(
            "legacy_owd_47999",
            [
                sys.executable, str(OWD_PROBE),
                "--gcs-ip", gcs_ip or "0.0.0.0",
                "--uav-ip", uav_ip or "0.0.0.0",
                "--duration", str(int(args.duration)),
                "--context", args.context, "--namespace", args.namespace,
                "--out", str(out / "flyblind_legacy_owd_47999.json"),
            ],
        )
    if want_ul_legacy_instr:
        if not args.dry_run and not (gcs_ip and uav_ip):
            sys.exit("--with-ul-owd requires PDU IPs")
        start(
            "ul_owd",
            [
                sys.executable, str(HERE / "ul_owd_probe.py"),
                "--gcs-ip", gcs_ip or "0.0.0.0", "--uav-ip", uav_ip or "0.0.0.0",
                "--duration", str(args.duration),
                "--context", args.context, "--namespace", args.namespace,
                "--out", str(out / "flyblind_ul_owd.json"),
                "--samples-out", str(out / "flyblind_ul_owd_samples.jsonl"),
            ],
        )
    if flags["ack_rtt"]:
        cmd = [
            sys.executable, str(HERE / "command_ack_rtt.py"),
            "--duration", str(args.duration),
            "--timeout-s", str(args.timeout_s),
            "--context", args.context, "--namespace", args.namespace,
            "--ack-udp-port", str(args.ack_udp_port),
            "--out", str(out / "flyblind_command_ack_rtt.json"),
        ]
        if args.ack_via_c2_udp:
            cmd += ["--via-c2-udp", "--uav-ip", uav_ip or "", "--gcs-ip", gcs_ip or ""]
            print("[warn] ACK via udp:5760 enabled — can steal mission C2", flush=True)
        elif args.ack_local_sitl_tcp:
            cmd += ["--local-sitl-tcp"]
            print("[warn] ACK local sitl tcp — NOT a 5G measurement", flush=True)
        else:
            cmd += ["--gcs-ip", gcs_ip or "", "--uav-ip", uav_ip or ""]
        start("ack_rtt", cmd)

    if args.ack_via_c2_udp:
        ack_mode = "via_c2_udp_DANGEROUS"
    elif args.ack_local_sitl_tcp:
        ack_mode = "local_sitl_tcp_NOT_5G"
    elif flags["ack_rtt"]:
        ack_mode = "gcs_udp_ack_probe_5g"
    else:
        ack_mode = "off"

    meta = {
        "schema": "flyblind_sidecars_meta_v5",
        "out_dir": str(out),
        "duration_s": args.duration,
        "profile": profile_name,
        "flags": flags,
        "gcs_ip": gcs_ip or None,
        "uav_ip": uav_ip or None,
        "ack_mode": ack_mode,
        "return_probe_b": {
            "enabled": bool(return_probe_b_on and flags["leg_owd"]),
            "payload_bytes": return_probe_b_payload if return_probe_b_on else None,
            "rate_hz": return_probe_b_rate if return_probe_b_on else None,
            "port": return_probe_b_port if return_probe_b_on else None,
            "tmp_tag": RETURN_PROBE_B_TMP_TAG if return_probe_b_on else None,
            "out_subdir": "probes_return_b" if return_probe_b_on else None,
        },
        "ack_timeout_s": args.timeout_s if flags["ack_rtt"] else None,
        "ue_iface_mode": "in_pod_single_exec" if flags["ue_iface"] else "off",
        "started_unix_ns": time.time_ns(),
        "children": [p["name"] for p in planned],
        "dry_run": bool(args.dry_run),
    }
    (out / "flyblind_sidecars_meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    if args.dry_run:
        print(json.dumps(meta, indent=2))
        return

    deadline = time.monotonic() + args.duration + 120
    rc_map = {}
    for name, p in procs:
        remaining = max(1, deadline - time.monotonic())
        try:
            rc_map[name] = p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            p.kill()
            rc_map[name] = -9
        print(f"[sidecar] {name} rc={rc_map[name]}", flush=True)

    meta["finished_unix_ns"] = time.time_ns()
    meta["returncodes"] = rc_map
    (out / "flyblind_sidecars_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()

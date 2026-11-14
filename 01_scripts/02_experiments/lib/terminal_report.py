##################################################################################################################################
# Module: terminal_report.py (human-readable terminal summary printed at the end of a run)                                       #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Mitigation-stack variant of terminal_report.py.                                                                   #
##################################################################################################################################

"""Terminal-only report formatting for FlyBlind experiments.

This module never writes experiment artefacts and never participates in gates.
It only renders already-computed values in a compact, explained layout.
"""
from __future__ import annotations

import atexit
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

REPORT_WIDTH = 81
EXPLANATION_COLUMN = 52


class _TeeStream:
    """Mirror writes to the original terminal stream and a shared text file."""

    def __init__(self, primary: TextIO, transcript: TextIO, lock: threading.Lock):
        self._primary = primary
        self._transcript = transcript
        self._lock = lock

    def write(self, text: str) -> int:
        with self._lock:
            self._primary.write(text)
            self._transcript.write(text)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            self._primary.flush()
            self._transcript.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._primary, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._primary.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._primary, "encoding", None) or "utf-8"


class CampaignTranscript:
    """Installed stdout/stderr tee for one campaign invocation."""

    def __init__(self, path: Path, handle: TextIO):
        self.path = path
        self._handle = handle
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        self._lock = threading.Lock()
        self._tee_stdout = _TeeStream(self._stdout, handle, self._lock)
        self._tee_stderr = _TeeStream(self._stderr, handle, self._lock)
        self._closed = False

    def install(self) -> "CampaignTranscript":
        sys.stdout = self._tee_stdout  # type: ignore[assignment]
        sys.stderr = self._tee_stderr  # type: ignore[assignment]
        return self

    def close(self) -> None:
        if self._closed:
            return
        if sys.stdout is self._tee_stdout:
            sys.stdout = self._stdout
        if sys.stderr is self._tee_stderr:
            sys.stderr = self._stderr
        self._handle.flush()
        self._handle.close()
        self._closed = True


def start_campaign_transcript(campaign_dir: Path) -> CampaignTranscript:
    """Start a collision-safe terminal transcript in the campaign directory."""
    campaign_dir = Path(campaign_dir)
    campaign_dir.mkdir(parents=True, exist_ok=True)
    stem = campaign_dir.name
    suffixes = [""] + [f"_{chr(code)}" for code in range(ord("a"), ord("z") + 1)]
    suffixes += [
        f"_{chr(first)}{chr(second)}"
        for first in range(ord("a"), ord("z") + 1)
        for second in range(ord("a"), ord("z") + 1)
    ]
    for suffix in suffixes:
        path = campaign_dir / f"{stem}{suffix}.txt"
        try:
            handle = path.open("x", encoding="utf-8", buffering=1)
        except FileExistsError:
            continue
        transcript = CampaignTranscript(path, handle).install()
        atexit.register(transcript.close)
        return transcript
    raise RuntimeError(f"no free transcript name in {campaign_dir}")


def _get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _num(value: Any, decimals: int = 1, suffix: str = "") -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{decimals}f}{suffix}"
    return "n/a"


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "n/a"


def section(title: str) -> None:
    tag = f"[{title.upper()}]"
    remaining = max(0, REPORT_WIDTH - len(tag))
    left = remaining // 2
    right = remaining - left
    print("-" * left + tag + "-" * right)


def item(label: str, value: str, explanation: str) -> None:
    main = f"{label} = {value}"
    print(f"{main:<{EXPLANATION_COLUMN}} ({explanation})")


def campaign_title(args: Any, rogue_capacity: int) -> str:
    condition = str(getattr(args, "condition", "baseline")).upper()
    if condition == "BASELINE":
        scenario = "BASELINE"
    elif getattr(args, "attack", None) in (
        "multi-rogue", "multi-rogue-dl", "multi-rogue-sync", "multi-rogue-dl-sync",
    ):
        attack = getattr(args, "attack", None)
        suffix = {
            "multi-rogue": "UL",
            "multi-rogue-dl": "DL",
            "multi-rogue-sync": "UL SYNC",
            "multi-rogue-dl-sync": "DL SYNC",
        }[attack]
        scenario = (
            f"ATK N{int(getattr(args, 'multi_rogue_count', 1) or 1)} {suffix}"
        )
    else:
        scenario = f"ATK {str(getattr(args, 'attack', 'ATTACK')).upper()}"

    reservation = int(getattr(args, "reservation", 0) or 0)
    min_a = int(getattr(args, "min_ratio_slice_a", 0) or 0)
    if min_a > 0:
        scenario += f" + MITIGATION A{min_a}"
    if reservation > 0:
        scenario += f" + MITIGATION PRB{reservation}"
    if getattr(args, "mitigation_rate_limit_a", False):
        scenario += " + RATE-LIMIT A"
    if getattr(args, "mitigation_aqm_ul", False):
        scenario += " + AQM UL"
    if getattr(args, "mitigation_ul_rrmpolicy", False):
        scenario += " + UL RRMPOLICY"

    video = float(getattr(args, "companion_video_mbps", 0) or 0)
    video_text = f"{video:g}MPBS"
    return (
        f"{int(getattr(args, 'gnb_prb', 0))} PRB - "
        f"{str(getattr(args, 'topology', 'cross')).upper()} - "
        f"{rogue_capacity} ROGUES - {scenario} - "
        f"{int(getattr(args, 'duration', 0))}S - {video_text}"
    )


def print_campaign_banner(args: Any, rogue_capacity: int) -> None:
    title = campaign_title(args, rogue_capacity)
    width = max(REPORT_WIDTH, len(title) + 4)
    print("=" * width)
    print(f"*{title:^{width - 2}}*")
    print("=" * width)


def print_run_header(
    args: Any,
    run_id: str,
    run_dir: Path,
    *,
    duration: int,
    warmup: int,
    envelope_s: int,
    mission_len: float,
    trajectory_active_s: float | None,
    proxy_watchdog_s: int,
    attack_delay_s: float | None,
    attack_burst_s: int | None,
    mission_start_s: float,
) -> None:
    display_run = run_id
    if run_id.upper().startswith("RUN"):
        display_run = run_id[3:].split("_", 1)[0]
    print()
    print(f"{f' RUN {display_run} ':=^{REPORT_WIDTH}}")
    print(f"[output] {run_dir}")
    print()
    item("CONDITION", str(getattr(args, "condition", "")).upper(), "experiment type")
    item("DURATION", f"{duration}s", "requested main window")
    item("TOPOLOGY", str(getattr(args, "topology", "")).upper(), "UE split across slices")
    item("PRB", str(getattr(args, "gnb_prb", "n/a")), "NRB (106=40 MHz, 51=20 MHz @ 30 kHz)")
    attack = "-" if getattr(args, "condition", "") == "baseline" else str(getattr(args, "attack", "")).upper()
    item("ATTACK", attack, "adversarial traffic type")
    item("UAS WORKLOAD", str(getattr(args, "workload", "")).upper(), "UAS control workload")
    item("TRAJECTORY", str(getattr(args, "trajectory", "")).upper(), "planned path shape")
    min_a = int(getattr(args, "min_ratio_slice_a", 0) or 0)
    min_b = int(getattr(args, "reservation", 0) or 0)
    item("MINRATIO", f"A{min_a} B{min_b}", "configured minimum reservation (slice A / slice B)")
    item(
        "SETPOINT-STREAM",
        f"{float(getattr(args, 'setpoint_rate_hz', 0)):g} Hz",
        "position commands sent per second",
    )

    section("SCHEDULE")
    item("requested", f"{duration}s", "requested main window")
    item("network", f"{duration}s", "network probe window")
    item("warmup", f"{warmup}s", "initial samples discarded")
    item("envelope", f"{envelope_s}s", "maximum window of run layers")
    item("mission_len", f"{mission_len:g}s", "planned mission duration")
    if trajectory_active_s is not None:
        item("trajectory_active", f"{trajectory_active_s:g}s", "active trajectory segment")
    item("proxy_watchdog", f"{proxy_watchdog_s}s (maximum)", "C2 process safety limit")

    if attack_delay_s is not None and attack_burst_s is not None:
        section("ATTACK START")
        item("delay", f"{attack_delay_s:g}s", "wait before starting the attack")
        item("burst", f"{attack_burst_s}s", "attack duration")
        item(
            "after mission start",
            f"{attack_delay_s - mission_start_s:g}s",
            "attack instant relative to mission start",
        )

    print(flush=True)
    print(f"Starting RUN {display_run} execution (please wait)...", flush=True)
    print(flush=True)


def print_run_report(
    run_meta: dict[str, Any],
    network: dict[str, Any],
    c2: dict[str, Any],
    physics: dict[str, Any],
    prb: dict[str, Any],
) -> None:
    gates = run_meta.get("gates") or {}
    timing = run_meta.get("timing") or {}
    owd = network.get("owd_us") or {}
    ipdv = network.get("ipdv_us") or {}
    availability = network.get("availability") or {}
    c2m = c2.get("c2_metrics") or {}
    failsafe = c2m.get("failsafe") or {}

    section("RUNTIME")
    item("wall", _num(timing.get("wall_runtime_s"), 1, "s"), "wall-clock time spent inside the run")
    if timing.get("proxy_runtime_s") is not None:
        item("proxy", _num(timing.get("proxy_runtime_s"), 1, "s"), "C2 proxy uptime")

    section("NETWORK")
    item(
        "OWD p50/p99/p99.9",
        f"{_num(owd.get('median'))} / {_num(owd.get('p99'))} / {_num(owd.get('p99_9'))} µs",
        "one-way IP delay; 50th, 99th and 99.9th percentiles",
    )
    item(
        "IPDV p50/p99/p99.9",
        f"{_num(ipdv.get('median'))} / {_num(ipdv.get('p99'))} / {_num(ipdv.get('p99_9'))} µs",
        "IP delay variation (jitter)",
    )
    item(
        "availability",
        f"{_num(availability.get('availability_post_warmup_pct'), 3)}% "
        f"({availability.get('packets_received_post_warmup')}/"
        f"{availability.get('expected_sent_post_warmup')} packets)",
        "fraction of probe packets received",
    )
    item(
        "OWD trend",
        _num(_get(network, "owd_temporal_trend_us", "delta_us"), 1, " µs"),
        "delay change from start to end",
    )

    section("HEARTBEAT")
    item(
        "seen/expected",
        f"{c2m.get('heartbeats_seen')}/{c2m.get('heartbeats_expected_approx')}",
        "MAVLink heartbeats received and expected",
    )
    item(
        "gaps above threshold",
        str(c2m.get("heartbeat_gaps_over_threshold")),
        "abnormal intervals between heartbeats",
    )
    item(
        "largest gap",
        _num(c2m.get("max_heartbeat_gap_s"), 3, "s"),
        "longest silence between heartbeats",
    )

    section("PHYSICS")
    item(
        "max deviation",
        _num(physics.get("max_deviation_m"), 2, " m"),
        "largest physical distance observed",
    )
    item("failsafe", _yes_no(failsafe.get("triggered")), "safety mode triggered by the autopilot")

    fstate = physics.get("flight_state") or {}
    steady = physics.get("steady_state_window") or {}
    acquisition = physics.get("trajectory_acquisition") or {}
    track = fstate.get("tracking_error_m") or {}
    cross = fstate.get("cross_track_error_m") or {}
    steady_cross = steady.get("cross_track_error_m") or {}
    truth = physics.get("trk_truth") or c2.get("trk_truth") or {}
    truth_track = truth.get("tracking_error_truth_m") or {}

    section("STATE")
    item(
        "trk_err med/p99",
        f"{_num(track.get('median'), 2)} / {_num(track.get('p99'), 2)} m",
        "error between GCS-believed position and the plan",
    )
    item(
        "xtrack med/p99",
        f"{_num(cross.get('median'), 2)} / {_num(cross.get('p99'), 2)} m",
        "physical lateral distance to the planned trajectory",
    )
    item(
        "xtrack steady p99",
        _num(steady_cross.get("p99"), 2, " m"),
        "lateral error in the steady segment",
    )
    item(
        "trk_truth med/p99",
        f"{_num(truth_track.get('median') or truth_track.get('p50'), 2)} / "
        f"{_num(truth_track.get('p99'), 2)} m",
        "error between SITL true position and the plan",
    )
    item(
        "trk_truth samples",
        str(truth_track.get("n") or "n/a"),
        "number of samples used for physical truth",
    )
    item(
        "acquisition",
        f"{_num(acquisition.get('duration_s'), 1)}s; "
        f"cross_max={_num(acquisition.get('cross_track_max_m'), 2)} m; "
        f"complete={_yes_no(acquisition.get('completed'))}",
        "time until the UAS reaches the trajectory",
    )
    transitions = fstate.get("mode_transitions") or []
    if transitions:
        text = ", ".join(
            f"{m.get('from')}→{m.get('to')}@{m.get('t_rel_s')}s" for m in transitions
        )
        item("mode transitions", text, "autopilot mode changes")

    section("MECHANISM")
    per_slice = prb.get("per_slice") or {}
    for slice_name in sorted(per_slice):
        entry = per_slice[slice_name] or {}
        item(
            f"share UL NPRB slice {slice_name}",
            _num(entry.get("share_ul_nprb_pct"), 1, "%"),
            "slice share of observed UL PRBs",
        )
        item(
            f"share DL bytes slice {slice_name}",
            _num(entry.get("share_dl_delta_pct"), 1, "%"),
            "slice share of observed DL MAC bytes",
        )
    item("MAC dumps", str(prb.get("dump_samples") or 0), "scheduler samples analyzed")
    cell = prb.get("cell_ul_activity") or {}
    item(
        "observed UL total",
        _num(cell.get("ul_mbps_total"), 2, " Mbps"),
        "observed aggregate UL traffic",
    )
    roles = cell.get("per_role") or {}
    for role in ("gcs", "uav", "rogue", "rogue2", "rogue3", "rogue4"):
        entry = roles.get(role)
        if not entry:
            continue
        item(
            f"{role} UL",
            f"rps={_num(entry.get('ul_rounds_per_s'), 1)}; "
            f"NPRB p50={_num(entry.get('last_nprb_p50'), 1)}; "
            f"{_num(entry.get('ul_mbps'), 2)} Mbps",
            "grants/s, median grant size and throughput",
        )

    section("RESOURCES")
    host = gates.get("host") or {}
    item(
        "host loadavg1 before/after",
        f"{_num(host.get('loadavg1_before'), 2)} / {_num(host.get('loadavg1_after'), 2)}",
        "host mean work queue",
    )
    item(
        "host loadavg1 maximum",
        _num(host.get("loadavg1_max"), 2),
        "highest 1-minute mean load",
    )
    item(
        "host CPU busy maximum",
        f"{_num(host.get('cpu_busy_pct_max'), 1)}% "
        f"(threshold {_num(host.get('cpu_busy_max_pct_threshold'), 0)}%)",
        "highest actual CPU occupancy",
    )
    upf = gates.get("upf_cpu") or {}
    item(
        "UPF CPU max/p50/p99",
        f"{_num(upf.get('cpu_pct_max'), 1)} / {_num(upf.get('cpu_pct_p50'), 1)} / "
        f"{_num(upf.get('cpu_pct_p99'), 1)}%",
        "5G user-plane CPU usage",
    )
    item("UPF saturated", _yes_no(upf.get("saturated")), "indicates a processing bottleneck on the UPF")

    fresh = physics.get("freshness") or {}
    section("SETPOINTS")
    item(
        "delivery/capture/correlation",
        f"{_num(fresh.get('delivery_ratio'), 3)} / "
        f"{_num(fresh.get('capture_ratio'), 3)} / "
        f"{_num(fresh.get('correlation_ratio'), 3)}",
        "command delivery, capture and matching",
    )
    gen = fresh.get("aoi_generation_ms") or fresh.get("aoi_network_ms") or {}
    arrival = fresh.get("aoi_arrival_ms") or {}
    item(
        "AoI setpoint gen/arrival p99",
        f"{_num(gen.get('p99'), 1)} / {_num(arrival.get('p99'), 1)} ms",
        "GCS→UAV control; context, not telemetry AoI",
    )

    tel = fresh.get("telemetry_downlink") or {}
    if tel:
        section("TELEMETRY / AoI")
        delay = tel.get("telemetry_arrival_delay_ms") or {}
        gap = tel.get("inter_arrival_gap_ms") or {}
        belief = tel.get("belief_vs_truth_m") or {}
        item(
            "GCS samples",
            str(tel.get("ned_samples_gcs") or 0),
            "LOCAL_POSITION_NED positions received",
        )
        item(
            "AoI delivery p50/p99/p99.9",
            f"{_num(delay.get('p50'), 1)} / {_num(delay.get('p99'), 1)} / "
            f"{_num(delay.get('p99_9'), 1)} ms",
            "position age when the GCS receives it",
        )
        item(
            "gap p50/p99/p99.9",
            f"{_num(gap.get('p50'), 1)} / {_num(gap.get('p99'), 1)} / "
            f"{_num(gap.get('p99_9'), 1)} ms",
            "interval between consecutive GCS positions",
        )
        item(
            "belief p50/p99",
            f"{_num(belief.get('p50'), 2)} / {_num(belief.get('p99'), 2)} m",
            "error between GCS belief and UAV truth",
        )

    attack = run_meta.get("outcomes") or {}
    attack_gate = gates.get("attack_integrity") or {}
    attack_summary = attack_gate.get("summary") or {}
    offered = attack.get("offered_mbps")
    if offered is None:
        offered = attack_summary.get("requested_aggregate_mbps")
    sender = attack.get("sender_mbps")
    if sender is None:
        sender = attack_summary.get("achieved_sender_mbps")
    if offered is not None:
        section("ATTACK")
        item(
            "offered/sender/delivered",
            f"{_num(offered, 1)} / "
            f"{_num(sender, 1)} / "
            f"{_num(attack.get('delivered_mbps'), 1)} Mbps",
            "requested, sent and received load",
        )
        item(
            "active rogues",
            f"{attack_summary.get('rogue_count')}/{attack_summary.get('rogue_count_requested')}",
            "observed and requested attackers",
        )

    mitigations = run_meta.get("mitigations") or {}
    if any(
        mitigations.get(name)
        for name in ("rate_limit_a", "aqm_ul", "ul_rrmpolicy")
    ):
        section("MITIGATIONS")
        runtime_tc = mitigations.get("runtime_tc_audit") or {}
        rate_limit = runtime_tc.get("rate_limit_a") or {}
        aqm = runtime_tc.get("aqm_ul") or {}
        if mitigations.get("rate_limit_a"):
            item(
                "Rate-limit A",
                f"{_num(rate_limit.get('aggregate_cap_mbps'), 2)} Mbps",
                "aggregate cap verified on the rogues",
            )
        if mitigations.get("aqm_ul"):
            claim = (aqm.get("claim") or "").replace("_", " ")
            qstats = aqm.get("qdisc_sampling") or {}
            if aqm.get("local_queue_ok") is True:
                health = (
                    f"queue OK (max_backlog={_num(qstats.get('max_backlog_kb'), 1)} KB, "
                    f"max_qlen={qstats.get('max_qlen')})"
                )
            elif aqm.get("local_queue_ok") is False:
                health = f"queue NOT OK ({aqm.get('local_queue_ok_reason') or 'n/a'})"
            else:
                health = claim or "AQM host on UAV UL"
            item(
                "AQM UL",
                f"HTB@{_num(aqm.get('rate_mbps'), 2)} Mbps + fq_codel ({aqm.get('interface') or 'n/a'})",
                health,
            )
        if mitigations.get("ul_rrmpolicy"):
            item(
                "UL rrmPolicy",
                "ACTIVE (S-NSSAI)",
                "3GPP-faithful two-level UL: Dedicated/Prioritized/Shared via rrmPolicy.json",
            )

    section("VALIDATION")
    enforced = gates.get("enforced") or []
    descriptions = {
        "clock": "clock and probe integrity",
        "host": "host quality on baseline",
        "host_instrumentation_integrity": "instrumentation processes and artefacts",
        "mission_integrity": "mission executed without a silent crash",
        "drone_integrity": "sufficient telemetry and physics",
        "freshness": "freshness data present and correlatable",
        "stream": "stream rate within tolerance",
        "c2_health": "C2 health; mandatory on baseline only",
        "prb": "MAC/PRB evidence intact",
        "availability": "minimum availability on baseline",
        "mission_success": "mission completed on baseline",
        "baseline_drone_quality": "baseline physical quality",
        "attack_integrity": "attack actually executed",
        "mitigation_integrity": "requested mitigations applied and verified",
        "proxy": "C2 proxy intact",
    }
    for gate_name in enforced:
        gate = gates.get(gate_name) or {}
        item(
            gate_name,
            "OK" if gate.get("pass") else "FAIL",
            descriptions.get(gate_name, "mandatory run gate"),
        )
        if not gate.get("pass") and gate.get("reason"):
            item("reason", str(gate.get("reason")), f"failure in {gate_name}")
    item(
        "VALID",
        "YES" if run_meta.get("valid") else "NO",
        "all mandatory gates passed",
    )

    outcome = gates.get("drone_outcome") or {}
    if outcome:
        section("FLIGHT OUTCOME")
        item("mission completed", _yes_no(outcome.get("mission_success")), "operational result")
        item("acquisition completed", _yes_no(outcome.get("acquisition_completed")), "trajectory reached")
        item(
            "xtrack steady p99",
            _num(outcome.get("cross_track_p99_steady_m"), 2, " m"),
            "physical lateral error in the steady segment",
        )
        item("landing completed", _yes_no(outcome.get("landing_completed")), "final landing state")
        item("final mode", str(outcome.get("final_mode") or "n/a"), "final autopilot mode")
        item("armed at end", _yes_no(outcome.get("final_armed")), "arming state at shutdown")


def print_aggregate(
    aggregate: dict[str, Any],
    *,
    condition: str,
    stability_evaluated: bool,
    stable: bool | None,
    relative_spread: float | None,
) -> None:
    n_valid = int(aggregate.get("n_valid") or 0)
    title = f" AGGREGATE ({n_valid} valid runs) "
    print()
    print(f"{title:=^{REPORT_WIDTH}}")
    for name, entry in (aggregate.get("metrics") or {}).items():
        values = entry.get("values") or []
        vals = ", ".join(_num(v, 1) for v in values) if values else "n/a"
        median = _num(entry.get("median"), 2)
        spread = _num(entry.get("spread"), 2)
        print(f"{name:<29} median={median:<12} spread={spread:<10} vals={vals}")

    flags = aggregate.get("flags") or {}
    for name, entry in flags.items():
        vals = ", ".join(_yes_no(v) for v in (entry.get("values") or [])) or "n/a"
        print(f"{name:<29} vals={vals}")

    if stability_evaluated:
        item(
            "stability",
            "YES" if stable else "NO",
            f"OWD p99 relative spread={_num((relative_spread or 0) * 100, 1, '%')}",
        )


def print_campaign_result(
    *,
    executed: int,
    requested: int,
    valid: int,
    minimum: int,
    output_dir: Path,
    elapsed_s: float,
) -> None:
    print()
    print(
        f"[RESULT]: {executed}/{requested} runs executed "
        f"({valid} valid - minimum {minimum})."
    )
    print("=" * REPORT_WIDTH)
    print(f"Results in: {output_dir}/")
    print(f"[TOTAL EXECUTION TIME]: {elapsed_s:.0f}s")
    print()

#!/usr/bin/env python3
##################################################################################################################################
# Module: mac_prb.py (gNB MAC PRB capture — MECHANISM layer)                                                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: kubectl-driven MAC log capture and per-slice PRB proxy parsing for orchestrate.                                   #
##################################################################################################################################

"""FlyBlind per-slice PRB-proxy evidence, MECHANISM layer (standalone).

Faithful lift of ``mac_prb_tool.py`` from ``01_testbeds/start_testbed.sh`` plus a
``rntimap`` helper (lift of ``build_rnti_role_map``) so the orchestrator can
capture the proof-of-mechanism without re-provisioning.

Modes:
  capture <ctx> <ns> <duration_s> <raw_out>
      Follow ``kubectl logs -f`` for the gNB, prefixing each line with a
      monotonic epoch (t=<float>) so the parser can build a time series.
  rntimap <ctx> <ns> <map_out>
      Best-effort C-RNTI -> role map from gNB / UE logs (attach order
      rogue=1, gcs=2, uav=3 via CU-UE-ID).
  parse <raw_out> <rnti_map> <roleslice_json> <csv_out> <json_out>
      roleslice_json e.g. {"rogue":"A","gcs":"B","uav":"B"} (labels only).

PRB proxy = OAI MAC dump UL NPRB per ~1s sample summed by slice (needs the gNB
running with MAC_log_level=info; otherwise the dump is empty and the caller
should treat the layer as best-effort).

dl_delta_share_pct = delta of UE downlink bytes (MAC TX at gNB, or dlsch_total_bytes
when present) per RNTI over the capture window, aggregated by slice — the DL
enforcement axis for ORANSlice (rRMPolicyRatio applies to DL scheduling; UL NPRB
share alone can mislead).  ul_delta_share_pct uses MAC RX (UE uplink received at
gNB) / ulsch_total_bytes.

cell_ul_activity (additive): absolute UL load proxies from Δulsch_rounds / ΔMAC RX
bytes — NOT cell PRB occupancy.  share_ul_nprb_pct remains a between-slice share of
last-NPRB dump samples and can exaggerate victim starvation.

OAI gNB MAC dump polarity (confirmed 2026-10): ``MAC: TX <dl> RX <ul> bytes``
(gNB transmit = DL, gNB receive = UL).
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
import select
import statistics
import subprocess
import sys
import time

# OAI band78 rfsim default (gnb.sa.band78.106prb.rfsim.conf):
# periodicity ms5, nrofUplinkSlots=2 → 2 UL slots / 5 ms = 400 UL slots/s.
# grant_activity_proxy_pct = 100 * Σ(ulsch_rounds/s) / this; may exceed 100% when
# multiple UEs share the same UL slot (FDMA) — still not per-TTI occupancy.
UL_SLOTS_PER_S_NOMINAL = 400.0


def mcs_eff(mcs):
    if mcs is None:
        return None
    tbl = [0.23, 0.31, 0.38, 0.49, 0.60, 0.74, 0.88, 1.03, 1.18, 1.33, 1.33, 1.48, 1.70, 1.91, 2.16,
           2.41, 2.57, 2.73, 3.03, 3.32, 3.61, 3.90, 4.21, 4.52, 4.82, 5.12, 5.33, 5.55, 5.89, 6.23, 7.41]
    try:
        return tbl[int(mcs)] if 0 <= int(mcs) < len(tbl) else None
    except (TypeError, ValueError):
        return None


RE_RNTI_HDR = re.compile(r'UE\s+RNTI\s+([0-9a-fA-F]{2,4})')
RE_UE_LINE = re.compile(r'UE\s+([0-9a-fA-F]{2,4})\s*:')
RE_CU_UE = re.compile(
    r'UE\s+RNTI\s+([0-9a-fA-F]{2,4})\s+CU-UE-ID\s+(\d+)',
    re.IGNORECASE,
)
RE_CU_ADD = re.compile(
    r'No CU UE ID stored for UE RNTI\s+([0-9a-fA-F]{2,4}),\s+adding CU UE ID\s+(\d+)',
    re.IGNORECASE,
)
RE_DL_BYTES = re.compile(r'dlsch_total_bytes\s+(\d+)')
RE_UL_BYTES = re.compile(r'ulsch_total_bytes\s+(\d+)')
RE_MAC_TXRX = re.compile(r'MAC:\s*TX\s+(\d+)\s+RX\s+(\d+)\s+bytes')
RE_DLSCH = re.compile(r'dlsch_rounds\s+(\d+)')
RE_ULSCH = re.compile(r'ulsch_rounds\s+(\d+)')
RE_MCS = re.compile(r'MCS\s*\(\d+\)\s*(\d+)')
RE_NPRB = re.compile(r'NPRB\s+(\d+)')
RE_GOODPUT = re.compile(r'goodput\s+([0-9.]+)\s*Mbps')
RE_TPREFIX = re.compile(r'^t=([0-9.]+)\s')
RE_UE_LOG_RNTI = re.compile(
    r"[rR][nN][tT][iI][^0-9a-fx]*(?:0x)?([0-9a-fA-F]{2,4})"
)

# Attach order in FlyBlind (EXTRA_ROGUE_COUNT up to 3): rogue… then GCS then UAV.
CANONICAL_UE_ROLES = ("rogue", "rogue2", "rogue3", "rogue4", "gcs", "uav")

# gNB-native slice evidence (authoritative, replaces attach-order guessing):
#   "Slice 2, NSSAI 1.000001, rrmPolicy..."  -> slice index to S-NSSAI
#   "Active slices for UE 8d94 = [ 2 ]"      -> C-RNTI to slice index
RE_SLICE_NSSAI = re.compile(
    r'Slice\s+(\d+),\s*NSSAI\s+(\d+)\.([0-9a-fA-F]+)', re.IGNORECASE,
)
RE_ACTIVE_SLICES = re.compile(
    r'Active slices for UE\s+([0-9a-fA-F]{2,4})\s*=\s*\[([^\]]*)\]', re.IGNORECASE,
)
# FlyBlind S-NSSAI SD → slice label (start_testbed.sh: SD=ffffff DNN oai / slice A,
# SD_B=000001 DNN oai.urllc / slice B).
SLICE_SD_LABELS = {"ffffff": "A", "000001": "B"}
# Minimum UL/DL delta asymmetry to trust the companion-video signature when
# telling UAV (video sender) from GCS (video receiver) apart.
ROLE_SIGNATURE_MIN_BYTES = 5_000_000
ROLE_SIGNATURE_MIN_RATIO = 3.0


def native_slice_map(
    text: str, slice_sd_labels: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """(C-RNTI -> slice label, slice index -> S-NSSAI) from gNB native logs."""
    labels = dict(slice_sd_labels or SLICE_SD_LABELS)
    idx2nssai: dict[str, str] = {}
    for m in RE_SLICE_NSSAI.finditer(text or ""):
        idx2nssai[m.group(1)] = "%s.%s" % (m.group(2), m.group(3).lower())
    rnti2slice: dict[str, str] = {}
    for m in RE_ACTIVE_SLICES.finditer(text or ""):
        idxs = [x for x in m.group(2).split() if x.isdigit()]
        if not idxs:
            continue
        nssai = idx2nssai.get(idxs[0])
        sd = nssai.split(".", 1)[1] if nssai and "." in nssai else None
        label = labels.get(sd or "")
        if label is None and len(idx2nssai) <= 1:
            label = "A"  # single-slice (intra) deployment
        if label:
            rnti2slice[normalize_rnti(m.group(1))] = label
    return rnti2slice, idx2nssai


def reconcile_roles_with_slices(
    rnti2role: dict[str, str],
    rnti_slice: dict[str, str],
    role2slice: dict[str, str],
    cu_order: list[str],
    ul_delta: dict[str, int],
    dl_delta: dict[str, int],
) -> tuple[dict[str, str], dict]:
    """Force role labels to agree with the gNB-native slice of each C-RNTI.

    Attach order (CU-UE-ID) races whenever a UE re-attaches, which silently
    swapped UAV/GCS with rogue UEs and moved the video traffic to the wrong
    slice.  The native ``Active slices`` line is ground truth for the slice; the
    UAV/GCS pair inside the C2 slice is then split by the companion-video
    signature (UAV is the UL sender, GCS the DL receiver).
    """
    info: dict = {
        "slice_source": "role_map_fallback",
        "role_identity_source": "attach_order",
        "native_slice_rntis": len(rnti_slice),
        "role_reassignments": [],
    }
    if not rnti_slice:
        return dict(rnti2role), info
    known_labels = set(role2slice.values())
    if known_labels and not set(rnti_slice.values()) <= known_labels:
        info["slice_source"] = "role_map_fallback_label_mismatch"
        return dict(rnti2role), info

    info["slice_source"] = "gnb_native_active_slices"
    order = [r for r in cu_order if r in rnti_slice]
    order += sorted(r for r in rnti_slice if r not in order)

    # Co-located mode: every UE shares ONE native slice, so the slice can no
    # longer separate the victim from the attackers. Identify by the companion-
    # video signature over the whole capture: the GCS is the downlink sink (video
    # receiver; rogues have ~0 DL), and the UAV is the top uplink sender — its
    # pre-attack video gives it the highest cumulative UL even after the attack
    # equalizes per-UE shares (C/(N+1)), when throughput alone no longer tells
    # the UAV from a rogue. Falls through to the per-slice logic below if the
    # video signature is too weak (e.g. no companion video on this run).
    if len(set(rnti_slice.values())) == 1:
        gcs_c = max(order, key=lambda r: dl_delta.get(r, 0)) if order else None
        rest_c = [r for r in order if r != gcs_c]
        uav_c = max(rest_c, key=lambda r: ul_delta.get(r, 0)) if rest_c else None
        strong = (
            gcs_c is not None and uav_c is not None
            and dl_delta.get(gcs_c, 0) >= ROLE_SIGNATURE_MIN_BYTES
            and ul_delta.get(uav_c, 0) >= ROLE_SIGNATURE_MIN_BYTES
        )
        if strong:
            info["role_identity_source"] = "companion_video_signature_colocated"
            out = {gcs_c: "gcs", uav_c: "uav"}
            rogue_roles = [r for r in CANONICAL_UE_ROLES if r.startswith("rogue")]
            remaining = [r for r in order if r not in (gcs_c, uav_c)]
            for rnti, role in zip(remaining, rogue_roles):
                out[rnti] = role
            taken = set(out.values())
            for rnti, role in rnti2role.items():
                if rnti in out or role in taken:
                    continue
                out[rnti] = role
                taken.add(role)
            for rnti, role in sorted(out.items()):
                was = rnti2role.get(rnti)
                if was and was != role:
                    info["role_reassignments"].append({"rnti": rnti, "from": was, "to": role})
            return out, info

    out: dict[str, str] = {}
    for label in sorted(set(rnti_slice.values())):
        group = [r for r in order if rnti_slice[r] == label]
        roles = [r for r in CANONICAL_UE_ROLES if role2slice.get(r) == label]
        assigned = dict(zip(group, roles))
        if "gcs" in roles and "uav" in roles:
            pair = [r for r, ro in assigned.items() if ro in ("gcs", "uav")]
            if len(pair) == 2:
                ul_top = max(pair, key=lambda r: ul_delta.get(r, 0))
                dl_top = max(pair, key=lambda r: dl_delta.get(r, 0))
                other = [r for r in pair if r != ul_top][0]
                strong = (
                    ul_top != dl_top
                    and ul_delta.get(ul_top, 0) >= ROLE_SIGNATURE_MIN_BYTES
                    and ul_delta.get(ul_top, 0)
                    >= ROLE_SIGNATURE_MIN_RATIO * max(1, ul_delta.get(other, 0))
                )
                if strong:
                    assigned[ul_top] = "uav"
                    assigned[other] = "gcs"
                    info["role_identity_source"] = "companion_video_signature"
        out.update(assigned)

    # RNTIs without native slice evidence keep their previous role when free.
    taken = set(out.values())
    for rnti, role in rnti2role.items():
        if rnti in out or role in taken:
            continue
        out[rnti] = role
        taken.add(role)

    for rnti, role in sorted(out.items()):
        was = rnti2role.get(rnti)
        if was and was != role:
            info["role_reassignments"].append({"rnti": rnti, "from": was, "to": role})
    return out, info


def normalize_rnti(rnti: str) -> str:
    """Canonical C-RNTI hex (lowercase, strip 0x prefix, pad to 4 nibbles)."""
    s = (rnti or "").lower().strip()
    if s.startswith("0x"):
        s = s[2:]
    # Do NOT use str.lstrip("0x") — that strips any leading 0/x chars.
    if len(s) < 4:
        s = s.zfill(4)
    return s


def latest_cu_rnti_map(text: str) -> dict[str, str]:
    """CU-UE-ID -> latest C-RNTI seen in gNB / MAC-dump text (last write wins)."""
    cu: dict[str, str] = {}
    for rx in (RE_CU_ADD, RE_CU_UE):
        for m in rx.finditer(text or ""):
            cu[m.group(2)] = normalize_rnti(m.group(1))
    return cu


def map_roles_from_cu(
    cu_latest: dict[str, str],
    roles: list[str] | tuple[str, ...],
) -> dict[str, str]:
    """Assign sorted CU-UE-IDs to roles in attach order. Returns rnti -> role."""
    out: dict[str, str] = {}
    for idx, cu in enumerate(sorted(cu_latest.keys(), key=int)):
        if idx >= len(roles):
            break
        out[cu_latest[cu]] = roles[idx]
    return out


def merge_rnti_role_maps(
    preferred: dict[str, str],
    fallback: dict[str, str],
) -> dict[str, str]:
    """Prefer live RNTIs; drop stale fallback entries whose role was remapped."""
    roles_taken = set(preferred.values())
    out = dict(preferred)
    for rnti, role in fallback.items():
        if role in roles_taken:
            continue
        if rnti in out:
            continue
        out[rnti] = role
        roles_taken.add(role)
    return out


def _kubectl_logs_cmd(ctx, ns):
    return [
        "kubectl", "--context", ctx, "logs", "-n", ns, "deploy/oai-gnb",
        "-c", "gnb", "-f", "--since=1s",
    ]


def _write_capture_meta(raw_out, meta):
    meta_path = str(raw_out) + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")


# ``kubectl logs -f`` can go permanently silent while the process stays alive
# (kubelet rotates the container log and the follower keeps the old inode /
# k8s#59902 family of bugs).  The gNB emits MAC dumps ~1/s, so this much
# silence means the stream is dead, not the cell.
PRB_STREAM_IDLE_RESTART_S = 15.0
# Guard against restart storms when the gNB is genuinely quiet (MAC log level
# below info): after this many idle restarts we stop reconnecting and just let
# the capture run out, so the gate sees the short window instead of churn.
PRB_STREAM_MAX_IDLE_RESTARTS = 60
_READ_CHUNK = 262144


def capture(ctx, ns, duration_s, raw_out, idle_restart_s=PRB_STREAM_IDLE_RESTART_S):
    logs_cmd = _kubectl_logs_cmd(ctx, ns)
    started_mono = time.monotonic()
    end = started_mono + float(duration_s)
    err_path = str(raw_out) + ".kubectl.err"
    meta = {
        "schema_version": "prb_capture_v2",
        "requested_duration_s": float(duration_s),
        "capture_process_started_mono": started_mono,
        "first_dump_mono": None,
        "last_dump_mono": None,
        "raw_lines": 0,
        "kubectl_restarts": 0,
        "idle_restarts": 0,
        "idle_restart_threshold_s": float(idle_restart_s),
        "max_silence_s": 0.0,
        "restart_events": [],
        "stderr_path": err_path,
        "termination_reason": "duration_elapsed",
    }

    def _start_kubectl(errf):
        return subprocess.Popen(
            logs_cmd, stdout=subprocess.PIPE, stderr=errf, bufsize=0,
        )

    def _stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        try:
            proc.stdout.close()
        except OSError:
            pass

    with open(err_path, "wb") as errf, open(raw_out, "w", encoding="utf-8") as f:
        p = _start_kubectl(errf)
        pending = b""
        last_activity = started_mono
        try:
            fd = p.stdout.fileno()
            while True:
                now = time.monotonic()
                if now >= end:
                    break
                remaining = end - now
                restart_reason = None
                if p.poll() is not None:
                    restart_reason = "process_exited"
                elif (now - last_activity) > idle_restart_s:
                    restart_reason = "stream_idle"
                if restart_reason == "stream_idle" and (
                    meta["idle_restarts"] >= PRB_STREAM_MAX_IDLE_RESTARTS
                ):
                    restart_reason = None
                    last_activity = now
                    meta["termination_reason"] = "idle_restart_budget_exhausted"
                if restart_reason:
                    silence = round(now - last_activity, 3)
                    meta["kubectl_restarts"] += 1
                    if restart_reason == "stream_idle":
                        meta["idle_restarts"] += 1
                    meta["restart_events"].append({
                        "at_rel_s": round(now - started_mono, 3),
                        "reason": restart_reason,
                        "silence_s": silence,
                    })
                    _stop(p)
                    pending = b""
                    p = _start_kubectl(errf)
                    fd = p.stdout.fileno()
                    last_activity = time.monotonic()
                    continue
                ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, _READ_CHUNK)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    # EOF on the pipe: the follower is gone even if poll() lags.
                    meta["kubectl_restarts"] += 1
                    meta["restart_events"].append({
                        "at_rel_s": round(time.monotonic() - started_mono, 3),
                        "reason": "stdout_eof",
                        "silence_s": round(time.monotonic() - last_activity, 3),
                    })
                    _stop(p)
                    pending = b""
                    p = _start_kubectl(errf)
                    fd = p.stdout.fileno()
                    last_activity = time.monotonic()
                    continue
                pending += chunk
                *lines, pending = pending.split(b"\n")
                if not lines:
                    continue
                now = time.monotonic()
                gap = now - last_activity
                if gap > meta["max_silence_s"]:
                    meta["max_silence_s"] = round(gap, 3)
                last_activity = now
                if meta["first_dump_mono"] is None:
                    meta["first_dump_mono"] = now
                meta["last_dump_mono"] = now
                meta["raw_lines"] += len(lines)
                for ln in lines:
                    f.write("t=%.3f %s\n" % (now, ln.decode("utf-8", "replace")))
                f.flush()
        finally:
            _stop(p)
            if meta["raw_lines"] == 0:
                meta["termination_reason"] = "kubectl_exited_no_lines"

    if meta["first_dump_mono"] is not None and meta["last_dump_mono"] is not None:
        meta["effective_capture_span_s"] = round(meta["last_dump_mono"] - meta["first_dump_mono"], 3)
    else:
        meta["effective_capture_span_s"] = 0.0
    meta["wall_capture_span_s"] = round(time.monotonic() - started_mono, 3)
    _write_capture_meta(raw_out, meta)
    return 0


def rntimap(ctx, ns, map_out):
    """Best-effort C-RNTI -> role map.

    Primary: **live** gNB ``UE RNTI … CU-UE-ID`` (last RNTI per CU wins) mapped to
    attach order ``rogue[, rogue2…] → gcs → uav`` for deployments that exist.
    Secondary: per-UE softmodem logs, but only when the candidate RNTI is still
    present in the live gNB set (avoids stale C-RNTIs after reattach).
    ``?`` in parse = RNTI still unmapped (not a third slice).
    """
    def _deploy_exists(role):
        r = subprocess.run(
            ["kubectl", "--context", ctx, "get", "deploy", "-n", ns,
             "oai-ue-%s" % role, "--ignore-not-found"],
            capture_output=True, text=True,
        )
        return bool((r.stdout or "").strip())

    roles = [r for r in CANONICAL_UE_ROLES if _deploy_exists(r)]
    if not roles:
        roles = ["rogue", "gcs", "uav"]

    res = subprocess.run(
        ["kubectl", "--context", ctx, "logs", "-n", ns, "deploy/oai-gnb",
         "-c", "gnb", "--tail=12000"],
        capture_output=True, text=True,
    )
    cu_latest = latest_cu_rnti_map(res.stdout or "")
    live_rntis = set(cu_latest.values())
    preferred = map_roles_from_cu(cu_latest, roles)

    # UE logs: only accept candidates that match a live gNB RNTI (role identity).
    ue_fallback: dict[str, str] = {}
    for role in roles:
        r = subprocess.run(
            ["kubectl", "--context", ctx, "logs", "-n", ns, "deploy/oai-ue-%s" % role,
             "-c", "nr-ue", "--tail=4000", "--since=10m"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            continue
        hits = [normalize_rnti(h) for h in RE_UE_LOG_RNTI.findall(r.stdout or "")]
        chosen = None
        for h in reversed(hits):
            if h in live_rntis:
                chosen = h
                break
        if chosen is None and hits and not live_rntis:
            chosen = hits[-1]
        if chosen is not None:
            ue_fallback[chosen] = role

    # Prefer CU-order live map; UE-log matches refine identity when they agree on live RNTIs.
    # If UE log maps live RNTI→role differently from CU order, prefer UE log for that RNTI
    # (attach-order races between gcs/uav) and drop the CU assignment for that role.
    refined = dict(preferred)
    if ue_fallback:
        for rnti, role in ue_fallback.items():
            if rnti not in live_rntis and live_rntis:
                continue
            # Remove any other rnti currently holding this role
            refined = {rr: ro for rr, ro in refined.items() if ro != role}
            refined[rnti] = role
        # Fill any role still missing from remaining live RNTIs via CU order
        missing = [ro for ro in roles if ro not in refined.values()]
        used = set(refined.keys())
        for cu in sorted(cu_latest.keys(), key=int):
            if not missing:
                break
            rnti = cu_latest[cu]
            if rnti in used:
                continue
            refined[rnti] = missing.pop(0)
            used.add(rnti)
        seen = refined
    else:
        seen = merge_rnti_role_maps(preferred, {})

    lines = ["%s %s" % (rnti, role) for rnti, role in sorted(seen.items())]
    with open(map_out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return 0 if lines else 1


def _load_rnti_map(path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) >= 2:
                    out[normalize_rnti(parts[0])] = parts[1]
    except FileNotFoundError:
        pass
    return out


def _overlay_rnti_map_from_capture(
    raw_text: str,
    rnti2role: dict[str, str],
    role2slice: dict[str, str],
) -> dict[str, str]:
    """Rebuild/repair role map from CU-UE-ID lines inside the capture window.

    Stale rntimap entries (old C-RNTI after reattach) leave GCS/UAV as ``?unmapped``.
    Capture-local CU-UE-ID → RNTI is authoritative for the dump being parsed.
    """
    cu_latest = latest_cu_rnti_map(raw_text)
    if not cu_latest:
        return rnti2role
    roles = [r for r in CANONICAL_UE_ROLES if r in role2slice] or list(CANONICAL_UE_ROLES)
    live = map_roles_from_cu(cu_latest, roles)
    # Keep file-map roles only for RNTIs that still appear in this capture.
    live_rntis = set(cu_latest.values())
    kept = {r: role for r, role in rnti2role.items() if r in live_rntis}
    # UE-file identity for live RNTIs wins over pure CU order when present.
    preferred = merge_rnti_role_maps(kept, live)
    # Ensure every live RNTI from CU map is assigned if a role slot remains.
    missing_roles = [ro for ro in roles if ro not in preferred.values()]
    for cu in sorted(cu_latest.keys(), key=int):
        rnti = cu_latest[cu]
        if rnti in preferred:
            continue
        if not missing_roles:
            break
        preferred[rnti] = missing_roles.pop(0)
    return preferred


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return float(ys[0])
    k = (len(ys) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ys) - 1)
    if f == c:
        return float(ys[f])
    return float(ys[f] + (ys[c] - ys[f]) * (k - f))


def _cell_ul_activity(
    *,
    window_s: float,
    rnti2role: dict[str, str],
    role2slice: dict[str, str],
    first_ul_r: dict[str, int],
    last_ul_r: dict[str, int],
    first_ul_b: dict[str, int],
    last_ul_b: dict[str, int],
    series: list,
    ul_slots_per_s: float = UL_SLOTS_PER_S_NOMINAL,
) -> dict:
    """Absolute UL activity proxies (not per-TTI PRB occupancy)."""
    per_role_abs: dict[str, dict] = {}
    nprb_by_role: dict[str, list[float]] = defaultdict(list)
    for (_t, rnti, direction, metric, _mcs, _gp) in series:
        if direction != "ul":
            continue
        role = rnti2role.get(rnti, "unknown")
        nprb_by_role[role].append(float(metric))

    roles = set(rnti2role.values()) | set(nprb_by_role) | {
        rnti2role.get(r, "unknown") for r in set(first_ul_r) | set(last_ul_r)
    }
    # Also include RNTIs only seen in counters.
    for rnti in set(first_ul_r) | set(last_ul_r) | set(first_ul_b) | set(last_ul_b):
        roles.add(rnti2role.get(rnti, "unknown"))

    total_rounds_delta = 0
    total_bytes_delta = 0
    for role in sorted(roles):
        rntis = [r for r, ro in rnti2role.items() if ro == role]
        if not rntis:
            # unknown / unmapped RNTIs
            rntis = [r for r in set(first_ul_r) | set(last_ul_r)
                     if rnti2role.get(r, "unknown") == role]
        rounds_delta = 0
        bytes_delta = 0
        for rnti in rntis:
            if rnti in first_ul_r and rnti in last_ul_r:
                rounds_delta += max(0, last_ul_r[rnti] - first_ul_r[rnti])
            if rnti in first_ul_b and rnti in last_ul_b:
                bytes_delta += max(0, last_ul_b[rnti] - first_ul_b[rnti])
        total_rounds_delta += rounds_delta
        total_bytes_delta += bytes_delta
        nprbs = nprb_by_role.get(role) or []
        sl = role2slice.get(role, "?")
        entry = {
            "slice": sl,
            "ul_rounds_delta": int(rounds_delta),
            "ul_rounds_per_s": round(rounds_delta / window_s, 2) if window_s > 0 else None,
            "ul_bytes_delta": int(bytes_delta),
            "ul_bytes_per_s": round(bytes_delta / window_s, 1) if window_s > 0 else None,
            "ul_mbps": round((8.0 * bytes_delta) / (window_s * 1e6), 3) if window_s > 0 else None,
            "last_nprb_samples": len(nprbs),
            "last_nprb_mean": round(statistics.mean(nprbs), 2) if nprbs else None,
            "last_nprb_p50": round(_pct(nprbs, 50), 2) if nprbs else None,
            "last_nprb_p90": round(_pct(nprbs, 90), 2) if nprbs else None,
            "last_nprb_max": int(max(nprbs)) if nprbs else None,
        }
        per_role_abs[role] = entry

    rounds_per_s = (total_rounds_delta / window_s) if window_s > 0 else None
    bytes_per_s = (total_bytes_delta / window_s) if window_s > 0 else None
    proxy_pct = None
    if rounds_per_s is not None and ul_slots_per_s > 0:
        proxy_pct = round(100.0 * rounds_per_s / ul_slots_per_s, 1)

    return {
        "window_s": round(window_s, 3),
        "ul_slots_per_s_nominal": ul_slots_per_s,
        "ul_rounds_delta_total": int(total_rounds_delta),
        "ul_rounds_per_s_total": round(rounds_per_s, 2) if rounds_per_s is not None else None,
        "ul_bytes_delta_total": int(total_bytes_delta),
        "ul_bytes_per_s_total": round(bytes_per_s, 1) if bytes_per_s is not None else None,
        "ul_mbps_total": (
            round((8.0 * total_bytes_delta) / (window_s * 1e6), 3) if window_s > 0 else None
        ),
        "grant_activity_proxy_pct": proxy_pct,
        "per_role": per_role_abs,
        "note": (
            "PROXY ONLY — not per-TTI cell PRB occupancy. "
            "ul_rounds_per_s = Δulsch_rounds / window; "
            "grant_activity_proxy_pct = 100 * total_rounds_per_s / ul_slots_per_s_nominal "
            f"({ul_slots_per_s:.0f}/s from OAI band78 rfsim 2 UL slots / 5 ms). "
            "May exceed 100% under multi-UE FDMA in the same UL slot. "
            "last_nprb_* is sticky last-grant size from MAC dumps (do not sum across UEs). "
            "share_ul_nprb_pct is between-slice share of those samples, not capacity used."
        ),
    }


def parse(raw_out, rnti_map_path, roleslice_json, csv_out, json_out):
    role2slice = json.loads(roleslice_json) if roleslice_json else {}
    with open(raw_out, encoding="utf-8") as f:
        raw_text = f.read()
    rnti2role = _overlay_rnti_map_from_capture(
        raw_text, _load_rnti_map(rnti_map_path), role2slice,
    )

    cur = {}
    series = []
    cur_rnti = None
    t0 = None
    last_t = 0.0
    first_dl_b: dict[str, int] = {}
    last_dl_b: dict[str, int] = {}
    first_ul_b: dict[str, int] = {}
    last_ul_b: dict[str, int] = {}
    first_ul_r: dict[str, int] = {}
    last_ul_r: dict[str, int] = {}
    dl_byte_source: dict[str, str] = {}

    def note_dl_bytes(rnti: str, val: int, source: str = "mac_rx_bytes") -> None:
        if rnti not in first_dl_b:
            first_dl_b[rnti] = val
        last_dl_b[rnti] = val
        dl_byte_source[rnti] = source

    def note_ul_bytes(rnti: str, val: int) -> None:
        if rnti not in first_ul_b:
            first_ul_b[rnti] = val
        last_ul_b[rnti] = val

    def note_ul_rounds(rnti: str, val: int) -> None:
        if rnti not in first_ul_r:
            first_ul_r[rnti] = val
        last_ul_r[rnti] = val

    def st_get(rnti):
        return cur.setdefault(rnti, {"dl_r": 0, "ul_r": 0, "dl_b": 0, "ul_b": 0,
                                     "ul_nprb_sum": 0, "dl_gp_sum": 0.0, "ul_gp_sum": 0.0, "samples": 0})

    for raw in raw_text.splitlines(keepends=True):
        m = RE_TPREFIX.match(raw)
        t = float(m.group(1)) if m else last_t
        last_t = t
        if t0 is None:
            t0 = t
        line = raw[m.end():] if m else raw
        h = RE_RNTI_HDR.search(line)
        if h:
            cur_rnti = normalize_rnti(h.group(1))
            st_get(cur_rnti)
            continue
        u = RE_UE_LINE.search(line)
        rnti = normalize_rnti(u.group(1)) if u else cur_rnti
        if rnti is None:
            continue
        st = st_get(rnti)
        mb = RE_MAC_TXRX.search(line)
        if mb:
            # OAI gNB MAC dump (per UE): TX = gNB→UE downlink bytes,
            # RX = UE→gNB uplink bytes.
            gnb_tx_dl, gnb_rx_ul = int(mb.group(1)), int(mb.group(2))
            st["dl_b"] = gnb_tx_dl
            st["ul_b"] = gnb_rx_ul
            note_dl_bytes(rnti, gnb_tx_dl, source="mac_tx_bytes_dl")
            note_ul_bytes(rnti, gnb_rx_ul)
        db = RE_DL_BYTES.search(line)
        if db:
            st["dl_b"] = int(db.group(1))
            note_dl_bytes(rnti, st["dl_b"], source="dlsch_total_bytes")
        ub = RE_UL_BYTES.search(line)
        if ub:
            st["ul_b"] = int(ub.group(1))
            note_ul_bytes(rnti, st["ul_b"])
        mcs_m = RE_MCS.search(line)
        mcs = int(mcs_m.group(1)) if mcs_m else None
        gp_m = RE_GOODPUT.search(line)
        gp = float(gp_m.group(1)) if gp_m else 0.0
        if 'dlsch_rounds' in line:
            rm = RE_DLSCH.search(line)
            if rm:
                st["dl_r"] = int(rm.group(1))
                st["dl_gp_sum"] += gp
                st["samples"] += 1
                series.append((round(t - t0, 3), rnti, "dl", st["dl_r"], mcs, gp))
        if 'ulsch_rounds' in line:
            rm = RE_ULSCH.search(line)
            nm = RE_NPRB.search(line)
            if rm:
                st["ul_r"] = int(rm.group(1))
                note_ul_rounds(rnti, st["ul_r"])
                if nm:
                    st["ul_nprb_sum"] += int(nm.group(1))
                st["ul_gp_sum"] += gp
                st["samples"] += 1
                nprb = int(nm.group(1)) if nm else 0
                series.append((round(t - t0, 3), rnti, "ul", nprb, mcs, gp))

    ul_delta_by_rnti = {
        r: max(0, last_ul_b.get(r, lo) - lo) for r, lo in first_ul_b.items()
    }
    dl_delta_by_rnti = {
        r: max(0, last_dl_b.get(r, lo) - lo) for r, lo in first_dl_b.items()
    }
    rnti_slice, slice_nssai = native_slice_map(raw_text)
    cu_latest = latest_cu_rnti_map(raw_text)
    cu_order = [cu_latest[k] for k in sorted(cu_latest, key=int)]
    rnti2role, identity_info = reconcile_roles_with_slices(
        rnti2role, rnti_slice, role2slice, cu_order,
        ul_delta_by_rnti, dl_delta_by_rnti,
    )
    identity_info["slice_nssai"] = slice_nssai
    identity_info["rnti_slice_native"] = rnti_slice

    # Effective role→slice from the gNB-native slice of each UE's live RNTI, so the
    # emitted role_slice_map reflects reality (co-located: victim shares slice A)
    # instead of echoing the caller's hint. Falls back to the hint when a role has
    # no native-slice evidence.
    role2slice = dict(role2slice)
    for _rnti, _role in rnti2role.items():
        _sl = rnti_slice.get(_rnti)
        if _sl:
            role2slice[_role] = _sl

    def slice_of(rnti, role):
        return rnti_slice.get(rnti) or role2slice.get(role, "?")

    with open(csv_out, "w", encoding="utf-8") as f:
        f.write("t_rel_s,rnti,role,slice,dir,metric,mcs,goodput_mbps\n")
        for (t, rnti, direction, metric, mcs, gp) in series:
            role = rnti2role.get(rnti, "unknown")
            sl = slice_of(rnti, role)
            f.write("%s,%s,%s,%s,%s,%d,%s,%.3f\n" % (t, rnti, role, sl, direction, metric,
                    mcs if mcs is not None else "", gp))

    per_role, per_slice = {}, {}
    for rnti, st in cur.items():
        role = rnti2role.get(rnti, "unknown")
        sl = slice_of(rnti, role)
        # Aggregate if multiple RNTIs collapse to same role (should not after overlay).
        prev = per_role.get(role)
        if prev is None:
            per_role[role] = {"rnti": rnti, "slice": sl,
                              "ul_nprb_samples_sum": st["ul_nprb_sum"],
                              "dl_rounds_last": st["dl_r"], "ul_rounds_last": st["ul_r"],
                              "dl_goodput_mbps_sum": round(st["dl_gp_sum"], 3),
                              "ul_goodput_mbps_sum": round(st["ul_gp_sum"], 3),
                              "mac_dump_samples": st["samples"]}
        else:
            prev["ul_nprb_samples_sum"] += st["ul_nprb_sum"]
            prev["dl_rounds_last"] = max(prev["dl_rounds_last"], st["dl_r"])
            prev["ul_rounds_last"] = max(prev["ul_rounds_last"], st["ul_r"])
            prev["dl_goodput_mbps_sum"] = round(prev["dl_goodput_mbps_sum"] + st["dl_gp_sum"], 3)
            prev["ul_goodput_mbps_sum"] = round(prev["ul_goodput_mbps_sum"] + st["ul_gp_sum"], 3)
            prev["mac_dump_samples"] += st["samples"]
        s = per_slice.setdefault(sl, {"ul_nprb_sum": 0, "dl_rounds": 0, "ul_rounds": 0,
                                      "dl_goodput_sum": 0.0, "ul_goodput_sum": 0.0})
        s["ul_nprb_sum"] += st["ul_nprb_sum"]
        s["dl_rounds"] += st["dl_r"]
        s["ul_rounds"] += st["ul_r"]
        s["dl_goodput_sum"] += st["dl_gp_sum"]
        s["ul_goodput_sum"] += st["ul_gp_sum"]

    tot_nprb = sum(s["ul_nprb_sum"] for s in per_slice.values()) or 1
    tot_ul_r = sum(s["ul_rounds"] for s in per_slice.values()) or 1
    tot_dl_r = sum(s["dl_rounds"] for s in per_slice.values()) or 1
    for sl, s in per_slice.items():
        s["share_ul_nprb_pct"] = round(100.0 * s["ul_nprb_sum"] / tot_nprb, 2)
        s["share_ul_rounds_pct"] = round(100.0 * s["ul_rounds"] / tot_ul_r, 2)
        s["share_dl_rounds_pct"] = round(100.0 * s["dl_rounds"] / tot_dl_r, 2)
        s["ul_goodput_mbps_sum"] = round(s["ul_goodput_sum"], 3)
        s["dl_goodput_mbps_sum"] = round(s["dl_goodput_sum"], 3)

    dl_delta_slice: dict[str, int] = defaultdict(int)
    for rnti, lo in first_dl_b.items():
        role = rnti2role.get(rnti, "unknown")
        sl = slice_of(rnti, role)
        hi = last_dl_b.get(rnti, lo)
        dl_delta_slice[sl] += max(0, hi - lo)
    tot_dl_delta_raw = sum(dl_delta_slice.values())
    tot_dl_delta = tot_dl_delta_raw or 1
    dl_delta_share_pct = {
        sl: round(100.0 * v / tot_dl_delta, 2) for sl, v in dl_delta_slice.items()
    }

    ul_delta_slice: dict[str, int] = defaultdict(int)
    for rnti, lo in first_ul_b.items():
        role = rnti2role.get(rnti, "unknown")
        sl = slice_of(rnti, role)
        hi = last_ul_b.get(rnti, lo)
        ul_delta_slice[sl] += max(0, hi - lo)
    tot_ul_delta_raw = sum(ul_delta_slice.values())
    tot_ul_delta = tot_ul_delta_raw or 1
    ul_delta_share_pct = {
        sl: round(100.0 * v / tot_ul_delta, 2) for sl, v in ul_delta_slice.items()
    }

    for sl, s in per_slice.items():
        s["dl_delta_bytes"] = int(dl_delta_slice.get(sl, 0))
        s["share_dl_delta_pct"] = dl_delta_share_pct.get(sl, 0.0)
        s["ul_delta_bytes"] = int(ul_delta_slice.get(sl, 0))
        s["share_ul_delta_pct"] = ul_delta_share_pct.get(sl, 0.0)

    t_rels = [s[0] for s in series]
    gaps = [b - a for a, b in zip(t_rels, t_rels[1:])] if len(t_rels) > 1 else []
    window_s = round((series[-1][0] - series[0][0]), 1) if series else 0.0
    # Prefer full capture span (t0..last_t) for rate denominators when available.
    rate_window_s = float(window_s)
    if t0 is not None and last_t is not None and (last_t - t0) > rate_window_s:
        rate_window_s = float(last_t - t0)
    cell_ul = _cell_ul_activity(
        window_s=rate_window_s,
        rnti2role=rnti2role,
        role2slice=role2slice,
        first_ul_r=first_ul_r,
        last_ul_r=last_ul_r,
        first_ul_b=first_ul_b,
        last_ul_b=last_ul_b,
        series=series,
    )
    # Enrich legacy per_role with absolute counters (additive keys only).
    for role, abs_e in (cell_ul.get("per_role") or {}).items():
        if role in per_role:
            per_role[role]["ul_rounds_delta"] = abs_e.get("ul_rounds_delta")
            per_role[role]["ul_rounds_per_s"] = abs_e.get("ul_rounds_per_s")
            per_role[role]["ul_bytes_delta"] = abs_e.get("ul_bytes_delta")
            per_role[role]["ul_mbps"] = abs_e.get("ul_mbps")
            per_role[role]["last_nprb_p50"] = abs_e.get("last_nprb_p50")
            per_role[role]["last_nprb_p90"] = abs_e.get("last_nprb_p90")
    dl_delta_role: dict[str, int] = defaultdict(int)
    for rnti, val in dl_delta_by_rnti.items():
        dl_delta_role[rnti2role.get(rnti, "unknown")] += val
    for role, entry in per_role.items():
        entry["dl_delta_bytes"] = int(dl_delta_role.get(role, 0))
        entry["dl_mbps"] = (
            round((8.0 * dl_delta_role.get(role, 0)) / (rate_window_s * 1e6), 3)
            if rate_window_s > 0 else None
        )
    out = {
        "window_s": window_s,
        "dump_samples": len(series),
        "max_inter_dump_gap_s": round(max(gaps), 3) if gaps else 0.0,
        "gaps_gt_2s": sum(1 for g in gaps if g > 2.0),
        "rnti_role_map": rnti2role,
        "role_slice_map": role2slice,
        "slice_identity": identity_info,
        "per_role": per_role,
        "per_slice": per_slice,
        "dl_delta_share_pct": dl_delta_share_pct,
        "dl_delta_bytes_total": int(tot_dl_delta_raw),
        "dl_delta_byte_source": dl_byte_source,
        "ul_delta_share_pct": ul_delta_share_pct,
        "ul_delta_bytes_total": int(tot_ul_delta_raw),
        "cell_ul_activity": cell_ul,
        "rnti_unmapped_note": (
            "'?' = C-RNTI with no role in rntimap (not a third slice); "
            "prefer share_ul_nprb_pct for UL contention between slices; "
            "use cell_ul_activity for absolute UL load proxies"
        ),
        "prb_proxy_note": (
            "OAI gNB MAC dump (mac_log_level=info): dl_delta_share_pct = delta of UE "
            "downlink bytes (MAC TX at gNB, or dlsch_total_bytes when logged); "
            "ul_delta_share_pct = delta of UE uplink bytes (MAC RX at gNB / "
            "ulsch_total_bytes); share_ul_nprb_pct = UL NPRB between-slice share of "
            "last-grant dump samples (not cell occupancy); cell_ul_activity = absolute "
            "UL rounds/bytes proxies (still not per-TTI occupancy). "
            "Polarity: OAI MAC TX=DL, RX=UL (fixed 2026-10)."
        ),
    }
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    return 0


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: mac_prb.py capture|rntimap|parse ...")
    mode = sys.argv[1]
    if mode == "capture":
        sys.exit(capture(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]))
    if mode == "rntimap":
        sys.exit(rntimap(sys.argv[2], sys.argv[3], sys.argv[4]))
    if mode == "parse":
        sys.exit(parse(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]))
    sys.exit("unknown mode %r" % mode)


if __name__ == "__main__":
    main()

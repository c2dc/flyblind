#!/usr/bin/env bash
##################################################################################################################################
# Script: start_testbed_mitigation.sh — FlyBlind mitigation testbed (two defences to evaluate)
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G
#
# Author: (anonymous; double-blind submission).
# Institution: withheld (double-blind review).
# Date: 2026-11
# License: MIT License
#
# Description: Same OAI/Kind/ORANSlice stack as start_testbed.sh, used to evaluate a DEFENCE against the
# FlyBlind uplink-contention attack. Bring it up WITH one preset:
#   --slicing : real UL slice enforcement. The victim (GCS + UAV) is moved to its OWN slice B, separate
#               from the rogues in slice A, with the slice-aware UL scheduler patch ON at floor 0 (B0).
#               The thin-first UL scheduler serves the victim's low-demand slice first, so the attack is
#               neutralised WITHOUT any PRB floor (AoI and belief return to baseline).
#   --aqm     : host AQM ablation. Victim stays co-located in eMBB; fq_codel is applied on the UAV uplink
#               at runtime by orchestrate.py (MITIGATION_AQM stamp) as an alternative defence to compare.
# With no preset it behaves like start_testbed.sh (the attack scenario). Cluster/images/runtime identical.
#
# Usage:
#   ./start_testbed_mitigation.sh --slicing   # defence B: UL slice enforcement (victim on separate slice B, patch ON, B0)
#   ./start_testbed_mitigation.sh --aqm       # defence A: host AQM (fq_codel) on the UAV uplink, victim co-located
#   ./start_testbed_mitigation.sh             # no preset -> same as the attack default (start_testbed.sh)
#
# WARNING: if the ORANSlice images are missing, this script BUILDS them first -- a full OpenAirInterface
# build, one-off, 30-90 min (clone ORANSlice, apply doc/rrmPolicyJson.patch and our
# patches/ul_rrmpolicy_mitigation.patch, then docker build). Afterwards they are cached and a
# bring-up takes ~9 min. Check with: docker image ls 'localhost/oai-*-oranslice-mitigation'
#   ./build/build_oranslice_images_mitigation.sh         # manual (re)build
#
# Clean restart (required when switching preset, co-location, A*/B*, AQM, or 5QI):
#   ./stop_testbed.sh && ./start_testbed_mitigation.sh [--slicing|--aqm]
##################################################################################################################################

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TERM="${TERM:-xterm-256color}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
YELLOW="\033[33m"
RESET="\033[0m"
log() { echo -e "${YELLOW}$*${RESET}"; }
sleep_with_progress() {
  local total="$1" label="${2:-waiting}" step=15 elapsed=0 chunk
  while (( elapsed < total )); do
    chunk=$(( total - elapsed < step ? total - elapsed : step ))
    sleep "${chunk}"
    elapsed=$((elapsed + chunk))
    log "[pause] ${label}... ${elapsed}/${total}s"
  done
}
die() {
  echo -e "${YELLOW}[ERROR] $*${RESET}" >&2
  echo -e "${YELLOW}Recovery: ./stop_testbed.sh && ./start_testbed_mitigation.sh${RESET}" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Paths and fixed versions (OAI CN v2.2.1 + RAN develop — official rfsim tutorial)
# ---------------------------------------------------------------------------
CLUSTER_NAME="tm-oranslice-mitigation"
KUBE_CONTEXT="kind-${CLUSTER_NAME}"
NS_CORE="core"
NS_RAN="ran"
NS_EXP="exp"

# Isolated from the paper path (runtime/ / tm-oranslice) to avoid stamp/gNB contamination.
ARTIFACT_ROOT="${SCRIPT_DIR}/runtime-mitigation"
CONFIG_DIR="${ARTIFACT_ROOT}/config"
REFERENCE_DIR="${SCRIPT_DIR}/reference/oai"
SCRIPTS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"   # 01_scripts/ (portable, canonical)
ARTIFACTS_ROOT="${SCRIPTS_ROOT}"
VENDOR_DIR="${SCRIPT_DIR}/vendor"
# ArduPilot SITL + pymavlink mission (FlyBlind C2 — lib in ../03_sitl/)
SITL_DIR="${SCRIPTS_ROOT}/03_sitl"
ARDUPILOT_ROOT="${ARDUPILOT_ROOT:-${VENDOR_DIR}/ardupilot}"
SITL_MISSION_PY="${SITL_DIR}/lib/mission_runner.py"
# C2 over 5G is always UDP (MAVLink). Role B: ArduPilot SITL listens TCP on
# 127.0.0.1:5761; socat/c2_arrival_forwarder bridges UDP:5760 → TCP:5761.
# Public C2 port facing the PDU/5G path:
SITL_TCP_PORT="${SITL_TCP_PORT:-5760}"
# Role B: SITL serial backend (localhost TCP); UDP relay on SITL_TCP_PORT.
SITL_INTERNAL_PORT="5761"
SITL_SERIAL_ARG="tcp:${SITL_INTERNAL_PORT}:wait"
# ACK probe relay (ON by default): required by the FULL sidecar profile.
# When ACK_PROBE_SOCAT=1: extra socat UDP:ACK_PROBE_UDP_PORT → TCP:ACK_PROBE_SITL_TCP
# (SITL secondary GCS). Does not touch :5760 / serial0. See instrumentation/command_ack_rtt.py.
ACK_PROBE_SOCAT="${ACK_PROBE_SOCAT:-1}"
ACK_PROBE_UDP_PORT="${ACK_PROBE_UDP_PORT:-5763}"
ACK_PROBE_SITL_TCP="${ACK_PROBE_SITL_TCP:-5762}"
C2_CONN_PREFIX="udp"
SITL_BOOT_WAIT_S="${SITL_BOOT_WAIT_S:-20}"
# Sidecar image for arducopter SITL (must match host GLIBC used to build vendor/ardupilot)
SITL_IMAGE="${SITL_IMAGE:-ubuntu:24.04}"
SLEEP_STEP=3
# 150 loops × 3 s = 450 s — 3 NR-UE attach over RFsim can exceed 360 s on cold start.
WAIT_PODS_MAX_LOOPS="${WAIT_PODS_MAX_LOOPS:-150}"

# Artifact sources — CN config from oai-cn5g-fed; RAN RF profile + rfsim DB from openairinterface5g.
OAI_CN5G_FED_RAW="${OAI_CN5G_FED_RAW:-https://gitlab.eurecom.fr/oai/cn5g/oai-cn5g-fed/-/raw/develop}"
OAI_RAN_RAW="${OAI_RAN_RAW:-https://raw.githubusercontent.com/OPENAIRINTERFACE/openairinterface5g/develop}"
OAI_CN_TAG="${OAI_CN_TAG:-v2.2.1}"
OAI_RAN_TAG="${OAI_RAN_TAG:-2024w28-oranslice}"
MYSQL_IMAGE="${MYSQL_IMAGE:-mysql:8.0}"
EXT_DN_IMAGE="${EXT_DN_IMAGE:-oaisoftwarealliance/trf-gen-cn5g:latest}"
BUSYBOX_IMAGE="${BUSYBOX_IMAGE:-docker.io/library/busybox:1.36}"

# ORANSlice fork (OAI 2024.w28 + nr_update_slice_policy). Auto-build via ensure_oranslice_images()
ORANSLICE_ROOT="${ORANSLICE_ROOT:-${VENDOR_DIR}/ORANSlice-mitigation}"
ORANSLICE_RAN="${ORANSLICE_RAN:-${ORANSLICE_ROOT}/oai_ran}"
ORANSLICE_IMAGE_TAG="${ORANSLICE_IMAGE_TAG:-2024w28}"
OAI_GNB_IMAGE="${OAI_GNB_IMAGE:-localhost/oai-gnb-oranslice-mitigation:${ORANSLICE_IMAGE_TAG}}"
OAI_UE_IMAGE="${OAI_UE_IMAGE:-localhost/oai-nr-ue-oranslice-mitigation:${ORANSLICE_IMAGE_TAG}}"
OAI_AMF_IMAGE="docker.io/oaisoftwarealliance/oai-amf:${OAI_CN_TAG}"
OAI_SMF_IMAGE="docker.io/oaisoftwarealliance/oai-smf:${OAI_CN_TAG}"
OAI_UPF_IMAGE="docker.io/oaisoftwarealliance/oai-upf:${OAI_CN_TAG}"

# PLMN / slice / DNN — OAI official 5g_rfsimulator defaults
MCC="208"
MNC="99"
DNN="oai"
SST=1
# Slice SD — three representations used by different OAI layers (must stay consistent):
#   SD          → AMF/SMF mini_nonrf_config.yaml and MySQL JSON ("FFFFFF")
#   SD_JSON     → same as SD, explicit alias for SessionManagement / AccessAndMobility rows
#   SD_HEX      → NR-UE libconfig + gNB profile (0xffffff)
SD="FFFFFF"
SD_JSON="${SD}"
SD_HEX="0xffffff"
PLMN="${MCC}${MNC}"

# Slice B (C2 / URLLC in topology=cross)
SST_B="${SST_B:-1}"
SD_B="${SD_B:-000001}"
SD_B_JSON="${SD_B}"
SD_B_HEX="0x$(echo "${SD_B}" | tr '[:upper:]' '[:lower:]')"
DNN_B="${DNN_B:-oai.urllc}"          # second DNN on same UPF (1 UPF serves both)
# PDU IPv4 pools per DNN (must match mini_nonrf_config dnns: — used in smoke checks)
SUBNET_SLICE_A="${SUBNET_SLICE_A:-12.1.1.128/25}"   # DNN oai (slice A / intra)
SUBNET_SLICE_B="${SUBNET_SLICE_B:-12.1.1.64/26}"    # DNN oai.urllc (slice B, cross C2)
# L3 isolation (option b): same UPF, but DROP UE↔UE between slice pools so the ROGUE
# cannot reach GCS/UAV PDU IPs. Influence across slices must be radio/PRB only.
# GCS↔UAV (both in SUBNET_SLICE_B) stays allowed. A/B → ext-dn (CNI) stays allowed.
L3_SLICE_ISOLATION="${L3_SLICE_ISOLATION:-1}"
# 5QI provisioning (MySQL + SMF local_subscription_infos). Default = separated (paper):
#   ROGUE 5QI 9 (best-effort) vs C2 5QI 82 (URLLC profile). Ablation equal-QoS:
#     EQUAL_5QI=1 ./start_testbed_mitigation.sh          # both → EQUAL_5QI_VALUE (default 9)
#     ROGUE_5QI=9 C2_5QI=9 ./start_testbed_mitigation.sh # explicit match without EQUAL_5QI
# Changing 5QI requires clean restart (DB + SMF config bake at bootstrap).
ROGUE_5QI="${ROGUE_5QI:-9}"
C2_5QI="${C2_5QI:-82}"
EQUAL_5QI="${EQUAL_5QI:-0}"
EQUAL_5QI_VALUE="${EQUAL_5QI_VALUE:-9}"
# Mitigation-only feature switches. They are explicit so the copied testbed can
# still reproduce the original control condition when both are zero.
# Host AQM ablation (UAV UL fq_codel). Runtime apply by orchestrate.py.
MITIGATION_AQM="${MITIGATION_AQM:-0}"
# NSSAI-keyed UL rrmPolicyRatio (3GPP-faithful UL counterpart of the DL
# two-level scheduler). Auto-enabled when A* or B* reservation > 0.
MITIGATION_UL_RRMPOLICY="${MITIGATION_UL_RRMPOLICY:-1}"
# Topology: cross (default — C2 on slice B, ROGUE on slice A) | intra (legacy)
TOPOLOGY="${TOPOLOGY:-cross}"
# Co-location experiment: victim (GCS/UAV) shares the eMBB slice A with the attackers, as an
# ordinary subscriber. Slice B stays provisioned (core/gNB/rrmPolicy) but idle (no UE). Requires
# TOPOLOGY=cross so slice B remains defined. Default 0 = legacy behaviour (victim on slice B).
VICTIM_IN_EMBB="${VICTIM_IN_EMBB:-1}"
# Victim 5QI when co-located: 9 = common user (Stage 1); 82 = GBR/URLLC (Stage 4), no other edit.
VICTIM_5QI="${VICTIM_5QI:-${ROGUE_5QI}}"
# gNB/UE carrier — band 78, numerology 1 (30 kHz SCS); OAI rfsim lockstep via -r on all NR-UE pods.
# NRB vs channel BW: TS 38.104 Table 5.3.2-1 SCS 30 kHz — 106 PRB = 40 MHz, 51 PRB = 20 MHz.
GNB_PRB="${GNB_PRB:-106}"
# rRMPolicyMinRatio — partitions PRBs per S-NSSAI at gNB (static, not AI-RAN):
#   intra: MIN_RATIO_SLICE_A caps the single slice (all 3 UEs share it — does NOT isolate GCS/UAV from ROGUE)
#   cross: MIN_RATIO_SLICE_B protects C2 slice B from ROGUE on slice A (Exp 2/3 slice-level defense)
MIN_RATIO_SLICE_A="${MIN_RATIO_SLICE_A:-0}"
MIN_RATIO_SLICE_B="${MIN_RATIO_SLICE_B:-0}"
# MaxRatio/DedicatedRatio — PRB ceiling / exclusive reservation per S-NSSAI (static slicing window):
#   cap C2 (slice B) via MAX_RATIO_SLICE_B<100 to force PRB scarcity for C2 (positive-control test)
#   steal PRB for the attacker (slice A) via DED_RATIO_SLICE_A>0 (exclusive reservation, not a floor)
MAX_RATIO_SLICE_A="${MAX_RATIO_SLICE_A:-100}"
MAX_RATIO_SLICE_B="${MAX_RATIO_SLICE_B:-100}"
DED_RATIO_SLICE_A="${DED_RATIO_SLICE_A:-0}"
DED_RATIO_SLICE_B="${DED_RATIO_SLICE_B:-0}"
# OWD/IPDV probe (Gate 1.8): C2 leg GCS→UAV only (ROGUE never measured); warm-up discarded
OWD_PROBE_DURATION="${OWD_PROBE_DURATION:-60}"
OWD_PROBE_WARMUP="${OWD_PROBE_WARMUP:-15}"
OWD_PROBE_MIN_SAMPLES="${OWD_PROBE_MIN_SAMPLES:-10000}"
# |temporal trend| max (µs) between 1st/last time-ordered deciles — clock contamination gate
OWD_PROBE_TEMPORAL_TREND_MAX_US="${OWD_PROBE_TEMPORAL_TREND_MAX_US:-10000}"
# GCS PDU loopback median max (µs) — CLOCK_MONOTONIC + UDP stack sanity
OWD_LOOPBACK_MEDIAN_MAX_US="${OWD_LOOPBACK_MEDIAN_MAX_US:-5000}"
# GCS↔UAV PDU handshake: max causality violations (t2 < t1); NTP theta is diagnostic only
OWD_HANDSHAKE_ORDERING_VIOLATIONS_MAX="${OWD_HANDSHAKE_ORDERING_VIOLATIONS_MAX:-0}"
OWD_HANDSHAKE_SAMPLES="${OWD_HANDSHAKE_SAMPLES:-100}"
# DO-377A gates (optional caps on post-warmup stats; set 0 to disable a check)
OWD_DO377A_MAX_US="${OWD_DO377A_MAX_US:-155000}"           # 155 ms OWD p99
IPDV_DO377A_P99_MAX_US="${IPDV_DO377A_P99_MAX_US:-20000}"  # 20 ms IPDV p99
DO377A_AVAILABILITY_MIN_PCT="${DO377A_AVAILABILITY_MIN_PCT:-99.9}"
# validate-under-attack: ROGUE UDP flood knobs (-P stresses PRB scheduler more than -b alone)
ROGUE_FLOOD_MBPS="${ROGUE_FLOOD_MBPS:-50}"
ROGUE_FLOOD_PARALLEL="${ROGUE_FLOOD_PARALLEL:-1}"
# Attack waveform: cbr (continuous iperf3) | pulse (low-rate pulsing / shrew-like).
#   pulse = single-thread bolus injector: dumps ROGUE_PULSE_BOLUS_KB instantly then idles
#   for the rest of ROGUE_PULSE_PERIOD_MS. The bolus overruns the slice-A buffer; the ~50 Mbps
#   air drains it over ~(bolus/ceiling) ms, starving C2 (slice B) in bursts → high IPDV p99.9
#   with LOW average rate and a single generator thread → far less host CPU than -P N CBR.
ROGUE_FLOOD_MODE="${ROGUE_FLOOD_MODE:-cbr}"
# Attack DIRECTION (cbr only) — which half of the C2 path (GCS-UL + UAV-DL) to contend:
#   ul   = ROGUE uplink (contends GCS uplink half)         [iperf3 default]
#   dl   = ROGUE downlink (contends UAV DOWNLINK half — the C2 delivery leg)  [iperf3 -R]
#   both = bidirectional (contends BOTH halves of the C2 path)               [iperf3 --bidir]
ROGUE_FLOOD_DIR="${ROGUE_FLOOD_DIR:-ul}"
ROGUE_PULSE_BOLUS_KB="${ROGUE_PULSE_BOLUS_KB:-192}"   # bytes injected per pulse (instantaneous burst)
ROGUE_PULSE_PERIOD_MS="${ROGUE_PULSE_PERIOD_MS:-60}"  # pulse period: bolus blast + idle remainder
ROGUE_PULSE_PKT_BYTES="${ROGUE_PULSE_PKT_BYTES:-1200}" # UDP payload size per datagram
# pulse: concurrent flows blasting simultaneously during the ON window. N distinct 5-tuples =
# N competing MAC queues = scheduler chaos (the real IPDV driver, like iperf -P N) but ONLY during
# the short duty cycle → low AVERAGE host CPU. Decouples IPDV generation from CPU cost.
ROGUE_PULSE_FLOWS="${ROGUE_PULSE_FLOWS:-1}"
# validate-under-attack: WARN if host loadavg1 exceeds this during probe (single-host confounder)
HOST_LOAD_MAX_AVG1="${HOST_LOAD_MAX_AVG1:-14}"

# CPU core affinity — single-host confounder mitigation. On rfsim all softmodems + the flood
# generator share the 16 host cores, so the attacker's CPU burn can jitter the measured C2
# path (IPDV tracks loadavg instead of PRB queueing). PIN_CORES=1 taskset-pins each
# process to a DISJOINT core set: measured path (gNB+GCS+UAV) vs attacker
# (ROGUE softmodem + iperf/pulse). Cores 14-15 are left UNPINNED on purpose so the
# kernel/k8s daemons keep guaranteed room — a starved host would itself inject timing
# jitter into the gNB↔UE sample exchange. CPU affinity reduces single-host scheduling
# interference as a confounder; it is not full physical isolation. Requires bootstrap
# (entrypoints change → pods restart). Default 0 = legacy behaviour (no pinning).
PIN_CORES="${PIN_CORES:-0}"
CORES_GNB="${CORES_GNB:-0-4}"          # gNB scheduler+PHY (measured path)
CORES_GCS="${CORES_GCS:-5}"            # GCS softmodem + OWD sender (measured path)
CORES_UAV="${CORES_UAV:-6}"            # UAV softmodem + OWD receiver (measured path)
CORES_ROGUE="${CORES_ROGUE:-7-9}"      # ROGUE softmodem (attacker)
CORES_ATTACK="${CORES_ATTACK:-10-13}"  # iperf/pulse flood generator (attacker)
# NOTE: cores 14-15 intentionally reserved for OS/kernel/k8s housekeeping (unpinned).
# Resolved per-process core string (empty = unpinned) — consumed by manifests + flood exec.
if [[ "${PIN_CORES}" == "1" ]]; then
  PIN_GNB="${CORES_GNB}"; PIN_GCS="${CORES_GCS}"; PIN_UAV="${CORES_UAV}"
  PIN_ROGUE="${CORES_ROGUE}"; PIN_ATTACK="${CORES_ATTACK}"
else
  PIN_GNB=""; PIN_GCS=""; PIN_UAV=""; PIN_ROGUE=""; PIN_ATTACK=""
fi
core_pin_for_role() {  # echo the reserved core string for a UE role (empty if unpinned)
  case "$1" in
    gcs)   echo "${PIN_GCS}" ;;
    uav)   echo "${PIN_UAV}" ;;
    rogue|rogue2|rogue3|rogue4) echo "${PIN_ROGUE}" ;;
    *)     echo "" ;;
  esac
}

is_rogue_role() {
  case "$1" in
    rogue|rogue2|rogue3|rogue4) return 0 ;;
    *) return 1 ;;
  esac
}

# FlyBlind roles — OWD/IPDV on C2 leg GCS→UAV; ROGUE generates PRB contention only (never measured)
IMSI_ROGUE="208990100001100"
IMSI_GCS="208990100001101"
IMSI_UAV="208990100001102"
IMSI_ROGUE2="208990100001103"
IMSI_ROGUE3="208990100001104"
IMSI_ROGUE4="208990100001105"
# Env default; may be overridden by the `rogues=N` CLI token (parsed below).
# UE_ROLES / EXPECTED_UE_COUNT are resolved AFTER argument parsing.
EXTRA_ROGUE_COUNT="${EXTRA_ROGUE_COUNT:-3}"  # default 4 rogues (rogue+rogue2..4); override with rogues=1..4
UE_KEY="fec86ba6eb707ed08905757b1bb44b8f"
UE_OPC="C42449363BBAD02B66D16BC975D77CC1"

# gNB / UE RF params — numerology/center filled by parse_gnb_ran_params(); GNB_PRB from env (default 106)
GNB_NUMEROLOGY=""
GNB_CENTER_HZ=""
GNB_CENTER_GRID_HZ=""
GNB_SSB_ARFCN=""
GNB_POINT_A_ARFCN=""
GNB_SSB_FIRST_SC=""
GNB_BAND="78"

# Cross-namespace DNS (core AMF NGAP headless svc; gNB RFsim service in ran)
AMF_NGAP_HOST="oai-amf-ngap.${NS_CORE}.svc.cluster.local"
GNB_RFSIM_HOST="oai-gnb.${NS_RAN}.svc.cluster.local"

usage() {
  cat <<EOF
start_testbed_mitigation.sh — IEEE S&P 2027 FlyBlind mitigation (Kind + OAI 5G + ArduPilot SITL)

Usage:
  ./start_testbed_mitigation.sh              # mitigation default: cross, B0, GNB_PRB=106, 4 rogues
  ./start_testbed_mitigation.sh cross B60    # cross + 60% PRB reservation on C2 slice
  ./start_testbed_mitigation.sh cross B100   # cross + 100% MinRatio on C2 (RAN-hard / UPF control)
  ./start_testbed_mitigation.sh intra        # 1 slice (legacy)
  ./start_testbed_mitigation.sh intra B60    # intra + 60% reservation on single slice
  ./start_testbed_mitigation.sh B60          # cross (default) + B60
  ./start_testbed_mitigation.sh cross B60 rogues=4   # UAS UL+DL MinRatio 60%
  ./start_testbed_mitigation.sh cross A60 rogues=4   # attacker UL+DL MinRatio 60%
  ./start_testbed_mitigation.sh cross AQM rogues=4   # host AQM ablation stamp

  Positional / token args (any order, no env vars needed):
    cross | intra          topology (default cross)
    rogues=1..4            total rogue UE fleet (default 4; rogues=1 => single rogue)
    maclog=info|debug|analysis   gNB MAC log verbosity (default info)
    B0..B100               MinRatio %% on UAS/C2 slice (DL+UL via rrmPolicy)
    A0..A100               MinRatio %% on attacker slice (DL+UL via rrmPolicy)
    AQM                    stamp host AQM ablation (UAV UL fq_codel; applied by orchestrate)
    ul-rrmpolicy           force UL rrmPolicy path even with A0/B0 (usually unnecessary)

  Env overrides (optional; CLI tokens above are preferred):
    GNB_PRB=51|106   # 106=40 MHz, 51=20 MHz (n78, SCS 30 kHz; TS 38.104)
    MIN_RATIO_SLICE_B=0..100
    EQUAL_5QI=1 | ROGUE_5QI=9 C2_5QI=82   # default separated; EQUAL_5QI matches both
    MITIGATION_AQM=0|1
    MITIGATION_UL_RRMPOLICY=0|1

  PRB reservation: B0 (default) … B100 (rRMPolicyMinRatio %).
  intra → MinRatio on sole slice; cross → MinRatio on C2 slice (GCS/UAV).

Clean restart (required when changing topology, PRB, or 5QI):
  ./stop_testbed.sh && ./start_testbed_mitigation.sh [cross|intra] [B0|B60|B100|…]

After bootstrap: SITL starts in background; mission/attack/probe via /tmp/*.sh in pods.
See --help for topology / MinRatio / rogue-fleet options.

  -h, --help   Show this message.

ArduPilot: ${VENDOR_DIR}/ardupilot (runtime tarball: 03_sitl/ardupilot-sitl-runtime.tar.xz; override: ARDUPILOT_ROOT=…)
Kind: ${CLUSTER_NAME} (${KUBE_CONTEXT})
  Defaults: TOPOLOGY=${TOPOLOGY} GNB_PRB=${GNB_PRB} B0
  intra: 1 slice ${SST}/${SD} DNN ${DNN}
  cross: ROGUE slice A / ${DNN}; C2 slice B / ${DNN_B}

EOF
}

parse_ratio_label() {
  # Accept B60 / A60 / 60 → integer 0..100. Prefix letter is informational.
  local raw="${1^^}" val
  raw="${raw#[AB]}"
  [[ "${raw}" =~ ^[0-9]+$ ]] || die "Invalid PRB reservation: ${1} (use B0..B100 or A0..A100)"
  val=$((10#${raw}))
  (( val >= 0 && val <= 100 )) || die "PRB reservation out of range 0..100: ${1}"
  echo "${val}"
}

apply_prb_reservation() {
  local ratio_b ratio_a
  ratio_b="$(parse_ratio_label "${PRB_LABEL}")"
  ratio_a="$(parse_ratio_label "${ATTACKER_LABEL}")"
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    # Intra: only one slice — B-label maps to that slice; A-label must stay 0.
    (( ratio_a == 0 )) || die "A* reservation requires topology=cross (got ${ATTACKER_LABEL})"
    MIN_RATIO_SLICE_A="${ratio_b}"
    MIN_RATIO_SLICE_B=0
  else
    # Cross: B* = MinRatio on UAS/C2 (slice B); A* = MinRatio on attackers (slice A).
    # Legacy ATTACKER_RESERVE env still overrides A-label when set explicitly.
    MIN_RATIO_SLICE_A="${ATTACKER_RESERVE:-${ratio_a}}"
    MIN_RATIO_SLICE_B="${ratio_b}"
  fi
  # Any positive UL/DL floor activates the NSSAI-keyed UL rrmPolicy path so
  # A*/B* mean reservation in BOTH directions (DL via ORANSlice native + UL via FlyBlind).
  if (( MIN_RATIO_SLICE_A > 0 || MIN_RATIO_SLICE_B > 0 )); then
    MITIGATION_UL_RRMPOLICY=1
  fi
}

# Resolve EQUAL_5QI / validate ROGUE_5QI & C2_5QI (MySQL + SMF local_subscription).
resolve_5qi_provisioning() {
  if [[ "${EQUAL_5QI}" == "1" ]]; then
    ROGUE_5QI="${EQUAL_5QI_VALUE}"
    C2_5QI="${EQUAL_5QI_VALUE}"
  fi
  [[ "${ROGUE_5QI}" =~ ^[0-9]+$ ]] || die "ROGUE_5QI must be an integer (got ${ROGUE_5QI})"
  [[ "${C2_5QI}" =~ ^[0-9]+$ ]] || die "C2_5QI must be an integer (got ${C2_5QI})"
  (( ROGUE_5QI >= 1 && ROGUE_5QI <= 255 )) || die "ROGUE_5QI out of range 1..255: ${ROGUE_5QI}"
  (( C2_5QI >= 1 && C2_5QI <= 255 )) || die "C2_5QI out of range 1..255: ${C2_5QI}"
  if [[ "${ROGUE_5QI}" == "${C2_5QI}" ]]; then
    log "[02] 5QI provisioning: EQUAL ${ROGUE_WORD_LC}=c2=${ROGUE_5QI} (ablation / EQUAL_5QI=${EQUAL_5QI})"
  else
    log "[02] 5QI provisioning: SEPARATED ${ROGUE_WORD_LC}=${ROGUE_5QI} c2=${C2_5QI}"
  fi
  case "${MITIGATION_AQM}" in 0|1) ;; *) die "MITIGATION_AQM must be 0 or 1" ;; esac
  case "${MITIGATION_UL_RRMPOLICY}" in 0|1) ;; *) die "MITIGATION_UL_RRMPOLICY must be 0 or 1" ;; esac
  if [[ "${MITIGATION_UL_RRMPOLICY}" == "1" && "${TOPOLOGY}" != "cross" ]]; then
    die "UL/DL reservation (A*/B* or ul-rrmpolicy) requires topology=cross"
  fi
  case "${VICTIM_IN_EMBB}" in 0|1) ;; *) die "VICTIM_IN_EMBB must be 0 or 1" ;; esac
  if [[ "${VICTIM_IN_EMBB}" == "1" && "${TOPOLOGY}" != "cross" ]]; then
    die "VICTIM_IN_EMBB=1 requires TOPOLOGY=cross (slice B must stay provisioned but idle)"
  fi
}

PRB_LABEL="B0"          # UAS/C2 slice MinRatio (B0..B100)
ATTACKER_LABEL="A0"     # attacker slice MinRatio (A0..A100)
ROGUES_TOTAL=""
while (($#)); do
  case "${1,,}" in
    intra|cross) TOPOLOGY="${1,,}" ;;
    aqm) MITIGATION_AQM=1 ;;  # must precede a[0-9]* (prefix match would steal "aqm")
    b[0-9]*) PRB_LABEL="${1^^}" ;;
    a[0-9]*) ATTACKER_LABEL="${1^^}" ;;
    rogues=*) ROGUES_TOTAL="${1#*=}" ;;
    maclog=*) GNB_MAC_LOG="${1#*=}"; GNB_MAC_LOG="${GNB_MAC_LOG,,}" ;;
    5qi-split)
      die "5qi-split was removed: pinned ORANSlice 2024.w28 nrUE has no multi-PDU parser \
(OAI MR !3769). C2≠video QoS split is out of scope on the pinned nrUE stack."
      ;;
    ul-rrmpolicy) MITIGATION_UL_RRMPOLICY=1 ;;  # optional; also auto from A*/B*>0
    # --- Mitigation presets (this testbed only). Bring up WITH one of these to evaluate a defence. ---
    --aqm) MITIGATION_AQM=1 ;;        # Mitigation A: host AQM (fq_codel) on the UAV uplink; victim stays co-located in eMBB.
    --slicing) VICTIM_IN_EMBB=0 ;;    # Mitigation B: real UL slice enforcement — victim moved to its own slice B (UL patch ON, B0) => attack neutralised.
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1 (use --help)" ;;
  esac
  shift
done

# Resolve rogue fleet (rogues=TOTAL overrides EXTRA_ROGUE_COUNT env). TOTAL counts the
# base rogue too, so rogues=4 => EXTRA_ROGUE_COUNT=3 (rogue + rogue2..rogue4).
if [[ -n "${ROGUES_TOTAL}" ]]; then
  [[ "${ROGUES_TOTAL}" =~ ^[1-4]$ ]] || die "rogues= must be 1..4 (got '${ROGUES_TOTAL}')"
  EXTRA_ROGUE_COUNT=$((ROGUES_TOTAL - 1))
fi
case "${EXTRA_ROGUE_COUNT}" in
  0|1|2|3) ;;
  *) die "EXTRA_ROGUE_COUNT must be 0..3 (or rogues=1..4); got '${EXTRA_ROGUE_COUNT}'" ;;
esac
UE_ROLES=(rogue)
(( EXTRA_ROGUE_COUNT >= 1 )) && UE_ROLES+=(rogue2)
(( EXTRA_ROGUE_COUNT >= 2 )) && UE_ROLES+=(rogue3)
(( EXTRA_ROGUE_COUNT >= 3 )) && UE_ROLES+=(rogue4)
UE_ROLES+=(gcs uav)
EXPECTED_UE_COUNT=$((3 + EXTRA_ROGUE_COUNT))
ROGUE_COUNT=$((1 + EXTRA_ROGUE_COUNT))
# Plural labels for console banners (1 rogue → singular; 2+ → plural).
if (( ROGUE_COUNT > 1 )); then
  ROGUE_WORD="ROGUES"
  ROGUE_WORD_LC="rogues"
  NR_UE_WORD="NR-UEs"
  RAN_FLEET_LABEL="1 gNB + ${EXPECTED_UE_COUNT} ${NR_UE_WORD} (${ROGUE_WORD}×${ROGUE_COUNT}/GCS/UAV, RFsimulator)"
  RAN_FLEET_SHORT="1 gNB + ${EXPECTED_UE_COUNT} ${NR_UE_WORD} (${ROGUE_WORD}×${ROGUE_COUNT}/GCS/UAV)"
else
  ROGUE_WORD="ROGUE"
  ROGUE_WORD_LC="rogue"
  NR_UE_WORD="NR-UE"
  RAN_FLEET_LABEL="1 gNB + ${EXPECTED_UE_COUNT} ${NR_UE_WORD} (ROGUE/GCS/UAV, RFsimulator)"
  RAN_FLEET_SHORT="1 gNB + ${EXPECTED_UE_COUNT} ${NR_UE_WORD} (ROGUE/GCS/UAV)"
fi

# gNB MAC log verbosity (info|debug|analysis); default info. `maclog=debug` enables the
# per-slot CCE/PRB scheduler trace used as UL-contention evidence.
GNB_MAC_LOG="${GNB_MAC_LOG:-info}"
case "${GNB_MAC_LOG}" in
  info|debug|analysis) ;;
  *) die "maclog must be info|debug|analysis (got '${GNB_MAC_LOG}')" ;;
esac

apply_prb_reservation
resolve_5qi_provisioning
if [[ "${MITIGATION_5QI_SPLIT:-0}" == "1" ]]; then
  die "MITIGATION_5QI_SPLIT was removed: pinned ORANSlice 2024.w28 nrUE has no multi-PDU (OAI MR !3769)."
fi

PHASE="all"
VALIDATE_UNDER_ATTACK=0
# Set when validate-under-attack self-drives the ROGUE flood (background PID).
ROGUE_ATTACK_PID=""
HOST_LOAD_BEFORE_AVG1=""
HOST_LOAD_AFTER_AVG1=""
HOST_LOAD_NPROC=""

case "${TOPOLOGY}" in
  intra|cross) ;;
  *) die "TOPOLOGY must be intra or cross (got '${TOPOLOGY}')" ;;
esac

# TS 38.104 Table 5.3.2-1, SCS 30 kHz (numerology 1): 106 PRB = 40 MHz, 51 PRB = 20 MHz.
gnb_prb_mhz_label() {
  case "$1" in
    106) printf '%s\n' "40MHz" ;;
    51)  printf '%s\n' "20MHz" ;;
    *)   printf '%s\n' "NRB=$1" ;;
  esac
}

case "${GNB_PRB}" in
  106|51) ;;
  *) die "GNB_PRB must be 106 (default, 40 MHz n78 @ 30 kHz) or 51 (20 MHz n78 @ 30 kHz); got '${GNB_PRB}'" ;;
esac

mkdir -p "${CONFIG_DIR}"

TOPOLOGY_STAMP="${ARTIFACT_ROOT}/topology.env"
ARTIFACTS_STAMP="${CONFIG_DIR}/artifacts.stamp"

# ---------------------------------------------------------------------------
# kubectl + Kind helpers
# ---------------------------------------------------------------------------
kubectl() { command kubectl --context "${KUBE_CONTEXT}" "$@"; }

kind_cluster_exists() {
  kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"
}

# Create Kind cluster while stripping Kind's progress emoji icons from the log.
kind_create_cluster() {
  local kcfg="$1" rc=0
  set +o pipefail
  kind create cluster --config "${kcfg}" 2>&1 \
    | sed -E 's/[[:space:]]*(🖼|📦|📜|🕹️|🔌|💾|😅)//g'
  rc=${PIPESTATUS[0]}
  set -o pipefail
  return "${rc}"
}

oranslice_image_present() {
  docker image inspect "$1" &>/dev/null
}

ensure_oranslice_images() {
  log "[00] ORANSlice prereqs (RAN tree + local images)..."
  need_cmd docker

  local img missing=0
  if [[ ! -d "${ORANSLICE_RAN}" ]]; then
    log "[00]   missing source: ${ORANSLICE_RAN}"
    missing=1
  fi
  for img in "${OAI_GNB_IMAGE}" "${OAI_UE_IMAGE}"; do
    if oranslice_image_present "${img}"; then
      log "[00]   present: ${img}"
    else
      log "[00]   missing: ${img}"
      missing=1
    fi
  done

  if (( missing == 0 )); then
    return 0
  fi

  if [[ "${ORANSLICE_SKIP_BUILD:-0}" == "1" ]]; then
    die "Missing mitigation image(s). Build manually: ${SCRIPT_DIR}/build/build_oranslice_images_mitigation.sh"
  fi

  log "[00] Building ORANSlice images (first run: 30–90 min; cached rebuilds are fast)..."
  ORANSLICE_ROOT="${ORANSLICE_ROOT}" \
  ORANSLICE_IMAGE_TAG="${ORANSLICE_IMAGE_TAG}" \
    "${SCRIPT_DIR}/build/build_oranslice_images_mitigation.sh"

  for img in "${OAI_GNB_IMAGE}" "${OAI_UE_IMAGE}"; do
    oranslice_image_present "${img}" \
      || die "Build finished but image still missing: ${img}"
  done
  log "[00] ORANSlice images ready: ${OAI_GNB_IMAGE}, ${OAI_UE_IMAGE}"
}

load_oranslice_images_into_kind() {
  need_cmd kind
  kind_cluster_exists || return 0
  for img in "${OAI_GNB_IMAGE}" "${OAI_UE_IMAGE}"; do
    log "[01] kind load docker-image ${img} → ${CLUSTER_NAME}"
    kind load docker-image "${img}" --name "${CLUSTER_NAME}" \
      || die "kind load failed for ${img}"
  done
}

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Command '$1' not found."; }

ue_role_imsi() {
  case "$1" in
    rogue)  echo "${IMSI_ROGUE}" ;;
    rogue2) echo "${IMSI_ROGUE2}" ;;
    rogue3) echo "${IMSI_ROGUE3}" ;;
    rogue4) echo "${IMSI_ROGUE4}" ;;
    gcs)    echo "${IMSI_GCS}" ;;
    uav)    echo "${IMSI_UAV}" ;;
    *) die "unknown UE role: $1" ;;
  esac
}

all_testbed_imsis() {
  local imsis=("${IMSI_ROGUE}")
  (( EXTRA_ROGUE_COUNT >= 1 )) && imsis+=("${IMSI_ROGUE2}")
  (( EXTRA_ROGUE_COUNT >= 2 )) && imsis+=("${IMSI_ROGUE3}")
  (( EXTRA_ROGUE_COUNT >= 3 )) && imsis+=("${IMSI_ROGUE4}")
  imsis+=("${IMSI_GCS}" "${IMSI_UAV}")
  printf '%s\n' "${imsis[@]}"
}

# stdout: sst sd_json sd_hex dnn five_qi
ue_slice_profile() {
  local role="$1"
  case "${role}" in
    rogue|rogue2|rogue3|rogue4)
      printf '%s %s %s %s %s\n' "${SST}" "${SD_JSON}" "${SD_HEX}" "${DNN}" "${ROGUE_5QI}"
      ;;
    gcs|uav)
      if [[ "${VICTIM_IN_EMBB}" == "1" ]]; then
        # Co-located: victim shares eMBB slice A with the attackers as an ordinary subscriber
        # (VICTIM_5QI; 9 by default). Slice B stays provisioned but gets no UE.
        printf '%s %s %s %s %s\n' "${SST}" "${SD_JSON}" "${SD_HEX}" "${DNN}" "${VICTIM_5QI}"
      elif [[ "${TOPOLOGY}" == "cross" ]]; then
        printf '%s %s %s %s %s\n' "${SST_B}" "${SD_B_JSON}" "${SD_B_HEX}" "${DNN_B}" "${C2_5QI}"
      else
        printf '%s %s %s %s %s\n' "${SST}" "${SD_JSON}" "${SD_HEX}" "${DNN}" "${C2_5QI}"
      fi
      ;;
    *) die "unknown UE role: ${role}" ;;
  esac
}

should_run_phase() {
  [[ "${PHASE}" == "all" || "${PHASE}" == "$1" ]]
}

write_topology_stamp() {
  cat > "${TOPOLOGY_STAMP}" <<EOF
TOPOLOGY=${TOPOLOGY}
GNB_PRB=${GNB_PRB}
MIN_RATIO_SLICE_A=${MIN_RATIO_SLICE_A}
MIN_RATIO_SLICE_B=${MIN_RATIO_SLICE_B}
PIN_CORES=${PIN_CORES}
CORES_GNB=${CORES_GNB}
CORES_GCS=${CORES_GCS}
CORES_UAV=${CORES_UAV}
CORES_ROGUE=${CORES_ROGUE}
CORES_ATTACK=${CORES_ATTACK}
SITL_TCP_PORT=${SITL_TCP_PORT}
ACK_PROBE_SOCAT=${ACK_PROBE_SOCAT}
ACK_PROBE_UDP_PORT=${ACK_PROBE_UDP_PORT}
ACK_PROBE_SITL_TCP=${ACK_PROBE_SITL_TCP}
EXTRA_ROGUE_COUNT=${EXTRA_ROGUE_COUNT}
ROGUE_5QI=${ROGUE_5QI}
C2_5QI=${C2_5QI}
VICTIM_IN_EMBB=${VICTIM_IN_EMBB}
VICTIM_5QI=${VICTIM_5QI}
EQUAL_5QI=${EQUAL_5QI}
MITIGATION_AQM=${MITIGATION_AQM}
MITIGATION_UL_RRMPOLICY=${MITIGATION_UL_RRMPOLICY}
ATTACKER_LABEL=${ATTACKER_LABEL}
PRB_LABEL=${PRB_LABEL}
EOF
  log "[stamp] topology.env → TOPOLOGY=${TOPOLOGY} GNB_PRB=${GNB_PRB} EXTRA_ROGUE_COUNT=${EXTRA_ROGUE_COUNT} A=${MIN_RATIO_SLICE_A} B=${MIN_RATIO_SLICE_B} aqm=${MITIGATION_AQM} ul_rrm=${MITIGATION_UL_RRMPOLICY} ACK_PROBE_SOCAT=${ACK_PROBE_SOCAT} 5QI=r${ROGUE_5QI}/c${C2_5QI}"
}

read_saved_gnb_prb() {
  [[ -f "${TOPOLOGY_STAMP}" ]] || return 1
  grep -q '^GNB_PRB=' "${TOPOLOGY_STAMP}" || return 1
  grep '^GNB_PRB=' "${TOPOLOGY_STAMP}" | cut -d= -f2-
}

read_saved_topology() {
  [[ -f "${TOPOLOGY_STAMP}" ]] || return 1
  grep -q '^TOPOLOGY=' "${TOPOLOGY_STAMP}" || return 1
  grep '^TOPOLOGY=' "${TOPOLOGY_STAMP}" | cut -d= -f2-
}

read_saved_victim_in_embb() {
  [[ -f "${TOPOLOGY_STAMP}" ]] || return 1
  grep -q '^VICTIM_IN_EMBB=' "${TOPOLOGY_STAMP}" || return 1
  grep '^VICTIM_IN_EMBB=' "${TOPOLOGY_STAMP}" | cut -d= -f2-
}

detect_cluster_topology() {
  local count
  if ! kubectl get deploy -n "${NS_RAN}" oai-gnb &>/dev/null; then
    echo ""
    return 0
  fi
  count="$(kubectl exec -n "${NS_RAN}" deploy/oai-gnb -c gnb -- \
    grep -cE '^[[:space:]]+- sst:' /opt/oai-gnb/etc/gnb.yaml 2>/dev/null || echo 0)"
  case "${count}" in
    1) echo "intra" ;;
    2) echo "cross" ;;
    *) echo "" ;;
  esac
}

ensure_topology_coherent() {
  local saved="" saved_prb="" deployed="" saved_vembb=""
  saved="$(read_saved_topology || true)"
  saved_prb="$(read_saved_gnb_prb || true)"
  saved_vembb="$(read_saved_victim_in_embb || true)"
  deployed="$(detect_cluster_topology)"

  if [[ -n "${saved}" && "${saved}" != "${TOPOLOGY}" ]]; then
    die "TOPOLOGY=${TOPOLOGY} conflicts with ${TOPOLOGY_STAMP} (saved=${saved}). Run: ./stop_testbed.sh && ./start_testbed_mitigation.sh ${TOPOLOGY} ${PRB_LABEL}"
  fi
  if [[ -n "${saved_vembb}" && "${saved_vembb}" != "${VICTIM_IN_EMBB}" ]]; then
    die "VICTIM_IN_EMBB=${VICTIM_IN_EMBB} conflicts with ${TOPOLOGY_STAMP} (saved=${saved_vembb}). Toggling victim co-location requires a clean restart: ./stop_testbed.sh && VICTIM_IN_EMBB=${VICTIM_IN_EMBB} ./start_testbed_mitigation.sh"
  fi
  if [[ -n "${saved_prb}" && "${saved_prb}" != "${GNB_PRB}" ]]; then
    die "GNB_PRB=${GNB_PRB} conflicts with ${TOPOLOGY_STAMP} (saved=${saved_prb}). Changing PRB requires a clean restart: ./stop_testbed.sh && GNB_PRB=${GNB_PRB} ./start_testbed_mitigation.sh"
  fi
  if [[ -n "${deployed}" && "${deployed}" != "${TOPOLOGY}" ]]; then
    die "TOPOLOGY=${TOPOLOGY} but deployed gNB has ${deployed} slice(s). Run: ./stop_testbed.sh && ./start_testbed_mitigation.sh ${TOPOLOGY} ${PRB_LABEL}"
  fi
}

artifacts_stamp_key() {
  local script_sha
  script_sha="$(sha256sum "${BASH_SOURCE[0]}" | awk '{print $1}')"
  # Script SHA, image tags, and slice/DNN bust cache when bootstrap logic or versions change.
  printf '%s:%s:%s:%s:pin=%s:%s/%s/%s/%s/%s:script=%s:ran=%s:extdn=%s:cn=%s:slice=%s/%s/%s:5qi=%s/%s:vembb=%s/%s:aqm=%s:ulrrm=%s:A=%s:B=%s' \
    "${TOPOLOGY}" "${GNB_PRB}" \
    "${MIN_RATIO_SLICE_A}" "${MIN_RATIO_SLICE_B}" \
    "${PIN_CORES}" "${PIN_GNB}" "${PIN_GCS}" "${PIN_UAV}" "${PIN_ROGUE}" "${PIN_ATTACK}" \
    "${script_sha}" "${OAI_RAN_TAG}" "${EXT_DN_IMAGE}" "${OAI_CN_TAG}" \
    "${SST}" "${SD}" "${DNN}" \
    "${ROGUE_5QI}" "${C2_5QI}" \
    "${VICTIM_IN_EMBB}" "${VICTIM_5QI}" \
    "${MITIGATION_AQM}" "${MITIGATION_UL_RRMPOLICY}" \
    "${ATTACKER_LABEL}" "${PRB_LABEL}"
}

artifacts_need_prepare() {
  local key
  key="$(artifacts_stamp_key)"
  [[ -f "${ARTIFACTS_STAMP}" ]] || return 0
  [[ "$(cat "${ARTIFACTS_STAMP}")" == "${key}" ]] || return 0
  [[ -f "${CONFIG_DIR}/oai-core.yaml" && -f "${CONFIG_DIR}/oai-ran.yaml" ]] || return 0
  return 1
}

validate_pin_core_layout() {
  [[ "${PIN_CORES}" == "1" ]] || return 0
  need_cmd python3
  local ncores problems
  ncores="$(nproc)"
  problems="$(python3 - "${ncores}" "${CORES_GNB}" "${CORES_GCS}" "${CORES_UAV}" "${CORES_ROGUE}" "${CORES_ATTACK}" <<'PY'
import sys

def expand(spec):
    out = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return out

ncores = int(sys.argv[1])
roles = {
    "CORES_GNB": sys.argv[2],
    "CORES_GCS": sys.argv[3],
    "CORES_UAV": sys.argv[4],
    "CORES_ROGUE": sys.argv[5],
    "CORES_ATTACK": sys.argv[6],
}
problems = []
assigned = {}
for name, spec in roles.items():
    if not spec:
        problems.append(f"{name} empty with PIN_CORES=1")
        continue
    cpus = expand(spec)
    if not cpus:
        problems.append(f"{name}={spec!r} parses empty")
        continue
    if min(cpus) < 0 or max(cpus) >= ncores:
        problems.append(f"{name}={spec} exceeds nproc={ncores}")
    assigned[name] = cpus
seen = {}
for name, cpus in assigned.items():
    for cpu in cpus:
        if cpu in seen:
            problems.append(f"CPU {cpu} shared by {seen[cpu]} and {name}")
        else:
            seen[cpu] = name
if ncores >= 16:
    hk = set(range(14, 16))
    used = set().union(*assigned.values()) if assigned else set()
    if not (hk - used):
        problems.append("no housekeeping cores free in 14-15")
print("\n".join(problems))
PY
)"
  if [[ -n "${problems}" ]]; then
    die "Invalid CPU pinning layout (PIN_CORES=1): ${problems//$'\n'; / | }"
  fi
  log "[pin] CPU layout OK — nproc=${ncores} gNB=${CORES_GNB} GCS=${CORES_GCS} UAV=${CORES_UAV} ROGUE=${CORES_ROGUE} ATTACK=${CORES_ATTACK}"
}

mark_artifacts_prepared() {
  artifacts_stamp_key > "${ARTIFACTS_STAMP}"
}

ip_in_subnet() {
  need_cmd python3
  python3 - "$1" "$2" <<'PY'
import ipaddress, sys
ip, net = sys.argv[1], sys.argv[2]
sys.exit(0 if ipaddress.ip_address(ip) in ipaddress.ip_network(net, strict=False) else 1)
PY
}

expected_pdu_subnet() {
  local role="$1"
  case "${role}" in
    rogue|rogue2|rogue3|rogue4) echo "${SUBNET_SLICE_A}" ;;
    gcs|uav)
      if [[ "${VICTIM_IN_EMBB}" == "1" ]]; then
        echo "${SUBNET_SLICE_A}"
      elif [[ "${TOPOLOGY}" == "cross" ]]; then
        echo "${SUBNET_SLICE_B}"
      else
        echo "${SUBNET_SLICE_A}"
      fi
      ;;
    *) die "unknown UE role: ${role}" ;;
  esac
}

count_bad_pods() {
  awk '{
    ready=$2; status=$3; split(ready,r,"/");
    if (status=="Completed" || status=="Succeeded") next;
    if (status=="Terminating") next;
    total++;
    if (status!="Running" || r[1]!=r[2]) bad++;
  } END { if (total==0) print 1; else print bad+0 }'
}

wait_pods_watch() {
  local ns="$1" selector="${2:-}" loops=0 prev_lines=0
  local max_loops="${WAIT_PODS_MAX_LOOPS}"
  echo
  log "[WAIT] Waiting for pods (${selector:-all}) in namespace ${ns} (max ${max_loops} loops)..."
  while (( loops < max_loops )); do
    ((++loops))
    if [[ "${prev_lines}" -gt 0 ]]; then
      tput cuu "${prev_lines}" 2>/dev/null || true
      tput ed 2>/dev/null || true
    fi
    local output pods_only bad header_msg
    output="$(kubectl get pods -n "${ns}" ${selector:+-l "${selector}"} --no-headers=false 2>/dev/null || true)"
    [[ -n "${output}" ]] || output="[No pods detected yet...]"
    pods_only="$(echo "${output}" | grep -v "NAME" || true)"
    if [[ -z "${pods_only}" ]]; then bad=1; else bad="$(echo "${pods_only}" | count_bad_pods)"; fi
    if echo "${pods_only}" | grep -qE 'ImagePullBackOff|ErrImagePull|CreateContainerConfigError'; then
      dump_oai_diagnostics
      die "[WAIT] Unrecoverable pod state in ${ns} (image pull / config). See kubectl get pods -n ${ns}."
    fi
    if echo "${pods_only}" | grep -q 'CrashLoopBackOff'; then
      header_msg="${YELLOW}[WAIT] Loop=${loops}/${max_loops} | CrashLoopBackOff detected — check logs (e.g. kubectl logs -n ${ns} <pod> -c gnb --tail=80).${RESET}"
    elif [[ "${bad}" -eq 0 && -n "${pods_only}" ]]; then
      header_msg="${YELLOW}[WAIT] OK — loop=${loops} | All pods are Running/Ready.${RESET}"
    else
      header_msg="${YELLOW}[WAIT] Loop=${loops}/${max_loops} | ${bad} pod(s) still not Running/Ready.${RESET}"
    fi
    echo -e "${header_msg}"
    echo ""
    echo "${output}"
    local out_lines=$((1 + 1 + $(echo "${output}" | wc -l)))
    prev_lines="${out_lines}"
    if [[ "${bad}" -eq 0 && -n "${pods_only}" ]]; then
      echo ""
      return 0
    fi
    sleep "${SLEEP_STEP}"
  done
  dump_oai_diagnostics
  die "[WAIT] Timeout after $((max_loops * SLEEP_STEP))s in namespace ${ns}."
}

dump_oai_diagnostics() {
  local stamp
  stamp="$(date +%H%M%S)"
  local dir="${CONFIG_DIR}/diag-${stamp}"
  mkdir -p "${dir}"
  log "[diag] Collecting OAI logs under ${dir}/ ..."
  local deploy ns
  for ns in "${NS_CORE}" "${NS_RAN}"; do
    for deploy in $(kubectl get deploy -n "${ns}" -o name 2>/dev/null | sed 's|deployment.apps/||'); do
      kubectl logs -n "${ns}" "deploy/${deploy}" --all-containers --tail=200 \
        > "${dir}/${ns}-${deploy}.log" 2>&1 || true
    done
    kubectl get pods,svc,endpoints -n "${ns}" -o wide > "${dir}/${ns}-resources.txt" 2>&1 || true
  done
  log "[diag] Saved under ${dir}/"
}

# =============================================================================
# 01 — Host prep: toolchain, routing, Kind cluster, namespaces
# =============================================================================
ensure_inotify_limits() {
  local min_watches=524288 min_instances=512
  local cur_watches cur_instances
  cur_watches="$(sysctl -n fs.inotify.max_user_watches 2>/dev/null || echo 0)"
  cur_instances="$(sysctl -n fs.inotify.max_user_instances 2>/dev/null || echo 0)"
  if (( cur_watches >= min_watches && cur_instances >= min_instances )); then
    return 0
  fi
  log "[01] inotify limits low (watches=${cur_watches} instances=${cur_instances}) — raising for Kind..."
  if command -v sudo >/dev/null 2>&1; then
    {
      echo "fs.inotify.max_user_watches=${min_watches}"
      echo "fs.inotify.max_user_instances=${min_instances}"
    } | sudo tee /etc/sysctl.d/99-kind-inotify.conf >/dev/null
    sudo sysctl --system >/dev/null 2>&1 || true
    cur_watches="$(sysctl -n fs.inotify.max_user_watches 2>/dev/null || echo 0)"
    cur_instances="$(sysctl -n fs.inotify.max_user_instances 2>/dev/null || echo 0)"
  fi
  if (( cur_watches < min_watches || cur_instances < min_instances )); then
    die "inotify limits too low for Kind. Run once: sudo bash ../../install_dependencies.sh"
  fi
}

write_kind_cluster_config() {
  local cfg="${CONFIG_DIR}/kind-${CLUSTER_NAME}.yaml"
  mkdir -p "${VENDOR_DIR}" "${CONFIG_DIR}"
  cat > "${cfg}" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: ${CLUSTER_NAME}
nodes:
- role: control-plane
  extraMounts:
  - hostPath: ${VENDOR_DIR}
    containerPath: ${VENDOR_DIR}
    readOnly: false
  kubeadmConfigPatches:
  - |
    kind: KubeletConfiguration
    containerLogMaxSize: "${KUBELET_LOG_MAX_SIZE}"
    containerLogMaxFiles: ${KUBELET_LOG_MAX_FILES}
EOF
  printf '%s' "${cfg}"
}

# Kubelet rotates the container log at containerLogMaxSize (10Mi by default) and
# ``kubectl logs -f`` silently keeps following the rotated-away inode, which is
# what truncated the PRB capture window in the 600 s runs.  The gNB at
# MAC_log_level=info writes ~2 MiB/min, so 10Mi rotates mid-run.  mac_prb has a
# silence watchdog now, but avoiding the rotation altogether keeps the capture
# gapless.
KUBELET_LOG_MAX_SIZE="${KUBELET_LOG_MAX_SIZE:-512Mi}"
KUBELET_LOG_MAX_FILES="${KUBELET_LOG_MAX_FILES:-3}"
KUBELET_LOG_FIX="${KUBELET_LOG_FIX:-1}"
KUBELET_CFG_PATH="/var/lib/kubelet/config.yaml"

kind_kubelet_log_cap_ok() {
  local node
  node="$(kind_node_name)" || return 1
  [[ -n "${node}" ]] || return 1
  docker exec "${node}" grep -qx "containerLogMaxSize: \"\?${KUBELET_LOG_MAX_SIZE}\"\?" \
    "${KUBELET_CFG_PATH}" 2>/dev/null
}

# Applied in-place on an existing cluster (kubeadmConfigPatches only run at
# creation).  Restarting kubelet re-reads the config without stopping running
# containers — containerd keeps them alive across the resync.
ensure_kind_kubelet_log_cap() {
  local node
  [[ "${KUBELET_LOG_FIX}" == "1" ]] || return 0
  node="$(kind_node_name)" || return 0
  [[ -n "${node}" ]] || return 0
  if kind_kubelet_log_cap_ok; then
    log "[01] Kubelet containerLogMaxSize=${KUBELET_LOG_MAX_SIZE} (no mid-run log rotation)."
    return 0
  fi
  log "[01] Patching kubelet containerLogMaxSize -> ${KUBELET_LOG_MAX_SIZE} (was default 10Mi)..."
  if ! docker exec "${node}" bash -c "
      set -e
      sed -i '/^containerLogMaxSize:/d;/^containerLogMaxFiles:/d' ${KUBELET_CFG_PATH}
      printf 'containerLogMaxSize: \"%s\"\ncontainerLogMaxFiles: %s\n' \
        '${KUBELET_LOG_MAX_SIZE}' '${KUBELET_LOG_MAX_FILES}' >> ${KUBELET_CFG_PATH}
      systemctl restart kubelet
    " 2>/dev/null; then
    log "[01] WARN: could not patch kubelet log cap — PRB capture relies on the mac_prb watchdog."
    return 0
  fi
  local i
  for i in $(seq 1 30); do
    kubectl get nodes --no-headers 2>/dev/null | grep -qw Ready && break
    sleep 2
  done
  if kind_kubelet_log_cap_ok; then
    log "[01] Kubelet log cap applied (${KUBELET_LOG_MAX_SIZE} x ${KUBELET_LOG_MAX_FILES})."
  else
    log "[01] WARN: kubelet log cap not confirmed — PRB capture relies on the mac_prb watchdog."
  fi
}

kind_node_name() {
  kind get nodes --name "${CLUSTER_NAME}" 2>/dev/null | head -1
}

verify_kind_vendor_mount() {
  local node arducopter="${VENDOR_DIR}/ardupilot/build/sitl/bin/arducopter"
  node="$(kind_node_name)" || return 1
  docker exec "${node}" test -d "${VENDOR_DIR}" 2>/dev/null \
    && docker exec "${node}" test -x "${arducopter}" 2>/dev/null
}

ue_container_pid_in_kind() {
  local deploy="$1" node pod cid pid
  node="$(kind_node_name)" || return 1
  pod="$(kubectl get pod -n "${NS_RAN}" -l "app=${deploy}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" \
    || return 1
  cid="$(kubectl get pod -n "${NS_RAN}" "${pod}" -o jsonpath='{.status.containerStatuses[?(@.name=="nr-ue")].containerID}' 2>/dev/null)" \
    || return 1
  cid="${cid#containerd://}"
  cid="${cid#docker://}"
  pid="$(docker exec "${node}" crictl inspect "${cid}" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["pid"])')" \
    || return 1
  [[ -n "${pid}" && "${pid}" != "0" ]] || return 1
  echo "${pid}"
}

# Run a command in a UE pod network namespace via the Kind node (nsenter works there).
kind_netns_run() {
  local deploy="$1" node pid
  shift
  need_cmd docker
  node="$(kind_node_name)" || die "Kind node for cluster ${CLUSTER_NAME} not found"
  pid="$(ue_container_pid_in_kind "${deploy}")" \
    || die "kind_netns_run: failed to resolve ${deploy} container PID (is the UE pod running?)"
  docker exec "${node}" nsenter -t "${pid}" -n "$@"
}

ensure_kind_cluster() {
  for c in kind kubectl docker wget; do need_cmd "$c"; done
  # Prefer kind from install_dependencies.sh (/usr/local/bin) over ad-hoc ~/.local/bin copies.
  [[ -x /usr/local/bin/kind ]] && export PATH="/usr/local/bin:${PATH}"

  log "[01] Routing and Kind cluster..."
  if [[ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)" != "1" ]]; then
    if command -v sudo >/dev/null 2>&1; then
      echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-ipforward.conf >/dev/null
      sudo sysctl --system >/dev/null 2>&1 || true
    fi
  fi
  ensure_inotify_limits

  if [[ -d /proc/net/sctp ]]; then
    log "[01] SCTP module present on host (NGAP port 38412)."
  elif command -v sudo >/dev/null 2>&1; then
    sudo modprobe sctp 2>/dev/null || true
    [[ -d /proc/net/sctp ]] && log "[01] SCTP module loaded." \
      || log "[01] WARN: SCTP unavailable — run: sudo modprobe sctp"
  fi

  if kind_cluster_exists; then
    log "[01] Kind cluster '${CLUSTER_NAME}' already exists."
    if ! verify_kind_vendor_mount; then
      log "[01] WARN: Kind node lacks SITL vendor mount (${VENDOR_DIR}) — recreating cluster..."
      kind delete cluster --name "${CLUSTER_NAME}" \
        || die "kind delete cluster failed (run: ./stop_testbed.sh)"
      local kcfg
      kcfg="$(write_kind_cluster_config)"
      log "[01] Creating Kind cluster '${CLUSTER_NAME}' (mounts ${VENDOR_DIR} for SITL)..."
      kind_create_cluster "${kcfg}" \
        || die "kind create cluster failed (run: sudo bash ../../install_dependencies.sh)"
    fi
  else
    local kcfg
    kcfg="$(write_kind_cluster_config)"
    log "[01] Creating Kind cluster '${CLUSTER_NAME}' (mounts ${VENDOR_DIR} for SITL)..."
    kind_create_cluster "${kcfg}" \
      || die "kind create cluster failed (run: sudo bash ../../install_dependencies.sh)"
  fi

  kubectl cluster-info >/dev/null 2>&1 \
    || die "kubectl cannot reach Kind cluster '${CLUSTER_NAME}' (context ${KUBE_CONTEXT})."
  log "[01] Kind cluster ready (context=${KUBE_CONTEXT})."
  ensure_kind_kubelet_log_cap
  ensure_topology_coherent
}

create_namespaces() {
  local ns
  for ns in "${NS_CORE}" "${NS_RAN}" "${NS_EXP}"; do
    kubectl create namespace "${ns}" --dry-run=client -o yaml | kubectl apply -f -
  done
  log "[01] Namespaces: ${NS_CORE}, ${NS_RAN}, ${NS_EXP}"
}

# =============================================================================
# 02 — Fetch OAI reference configs and render k8s-ready artifacts
# =============================================================================
download_artifact() {
  local dest="$1"
  shift
  local url tried=() attempt http_code ref="${REFERENCE_DIR}/$(basename "${dest}")"
  need_cmd curl

  # Prefer pinned local copy (artifact-evaluation offline / avoids GitLab 429 noise).
  if [[ -f "${ref}" && -s "${ref}" ]]; then
    cp "${ref}" "${dest}"
    log "[02] OK $(basename "${dest}") ($(wc -c < "${dest}") bytes, local reference)"
    return 0
  fi

  for url in "$@"; do
    for attempt in 1 2; do
      tried+=("${url}")
      log "[02] GET $(basename "${dest}") <= ${url} (attempt ${attempt})"
      http_code="$(curl -fsSL -m 90 -w "%{http_code}" -o "${dest}.tmp" "${url}" 2>/dev/null || true)"
      if [[ "${http_code}" == "200" ]] \
         && [[ -s "${dest}.tmp" ]] \
         && ! grep -qEi '<!(DOCTYPE|html)|404: Not Found|Too Many Requests' "${dest}.tmp" 2>/dev/null; then
        mv "${dest}.tmp" "${dest}"
        log "[02] OK $(basename "${dest}") ($(wc -c < "${dest}") bytes)"
        return 0
      fi
      rm -f "${dest}.tmp"
      # Rate-limited mirrors: skip retries and try the next URL immediately.
      if [[ "${http_code}" == "429" ]]; then
        log "[02] WARN: HTTP 429 from mirror — trying next source"
        break
      fi
      [[ "${attempt}" -lt 2 ]] && sleep 2
    done
  done
  die "Failed to download $(basename "${dest}"). Tried: ${tried[*]} (place a pinned copy in ${REFERENCE_DIR}/)"
}

fetch_oai_artifacts() {
  log "[02] Downloading OAI reference configs..."

  # Core CN YAML — local reference/oai/ first (via download_artifact); then GitHub; GitLab last.
  download_artifact "${CONFIG_DIR}/mini_nonrf_config.yaml" \
    "${OAI_RAN_RAW}/ci-scripts/yaml_files/5g_rfsimulator/mini_nonrf_config.yaml" \
    "${OAI_CN5G_FED_RAW}/docker-compose/conf/mini_nonrf_config.yaml"

  # MySQL init — cn5g schema only (SessionManagementSubscriptionData per-UE slice/5QI).
  download_artifact "${CONFIG_DIR}/oai_db.sql" \
    "${OAI_RAN_RAW}/doc/tutorial_resources/oai-cn5g/database/oai_db.sql" \
    "${OAI_RAN_RAW}/ci-scripts/yaml_files/5g_rfsimulator/oai_db.sql"

  local gnb_src="${ORANSLICE_RAN}/ci-scripts/conf_files/gnb.sa.band78.106prb.rfsim.conf"
  if [[ ! -f "${gnb_src}" ]]; then
    die "ORANSlice gNB conf missing at ${gnb_src}. Clone ORANSlice to ORANSLICE_ROOT=${ORANSLICE_ROOT} or run ./build/build_oranslice_images_mitigation.sh"
  fi
  cp "${gnb_src}" "${CONFIG_DIR}/gnb-rfsim.yaml"
  log "[02] OK gnb-rfsim.yaml from ORANSlice libconfig ($(wc -c < "${CONFIG_DIR}/gnb-rfsim.yaml") bytes)"
}

# Reshape OAI band78 rfsim profile when GNB_PRB != 106 (upstream ships 106prb only).
# Keeps absoluteFrequencySSB fixed; recalculates Point A + BWP RIV for lockstep gNB/UE -r.
patch_gnb_rfsim_prb() {
  local gnb_file="${CONFIG_DIR}/gnb-rfsim.yaml"
  [[ -f "${gnb_file}" ]] || die "Missing ${gnb_file} after fetch."
  need_cmd python3

  python3 - "${gnb_file}" "${GNB_PRB}" <<'PY'
import re, sys

path, target_s = sys.argv[1], sys.argv[2]
target = int(target_s)
if target < 1 or target > 275:
    raise SystemExit(f"GNB_PRB out of range: {target}")

text = open(path, encoding="utf-8", errors="replace").read()

def conf_int(key: str) -> int:
    m = re.search(
        rf"^[ \t]*{re.escape(key)}[ \t]*[:=][ \t]*(\d+)\s*(?:;|\s*(?:#.*)?$)",
        text,
        re.M,
    )
    if not m:
        raise SystemExit(f"missing config key {key} in {path}")
    return int(m.group(1))

current = conf_int("dl_carrierBandwidth")
if current == target:
    print(f"[patch] gnb-rfsim.yaml already dl_carrierBandwidth={target} — no PRB patch")
    sys.exit(0)

mu = conf_int("dl_subcarrierSpacing")
ssb = conf_int("absoluteFrequencySSB")

def n78_hz(nref: int) -> int:
    if not (600000 <= nref <= 653333):
        raise SystemExit(f"NR-ARFCN {nref} outside n78 FR1 range")
    return int(round((3000.0 + 0.015 * (nref - 600000)) * 1e6))

def n78_arfcn_from_hz(hz: int) -> int:
    nref = int(round(600000 + (hz / 1e6 - 3000.0) / 0.015))
    if not (600000 <= nref <= 653333):
        raise SystemExit(f"Point A {hz} Hz outside n78 FR1 after PRB={target}")
    return nref

scs_hz = 15000 * (2 ** mu)
center = n78_hz(ssb)
point_a_hz = center - (target * 12 * scs_hz) // 2
point_a = n78_arfcn_from_hz(point_a_hz)
riv = 275 * (target - 1)  # RBstart=0, L=target

grid = n78_hz(point_a) + (target * 12 * scs_hz) // 2
if abs(center - grid) > 1_000_000:
    raise SystemExit(
        f"RF mismatch after PRB patch: SSB {center} Hz vs grid {grid} Hz "
        f"(PointA={point_a}, PRB={target}, mu={mu})"
    )

def sub(key: str, val: int) -> None:
    global text
    text, n = re.subn(
        rf"^([ \t]*{re.escape(key)}[ \t]*[:=][ \t]*)(\d+)(\s*(?:;.*|(?:#.*)?)?)$",
        rf"\g<1>{val}\3",
        text,
        count=1,
        flags=re.M,
    )
    if n != 1:
        raise SystemExit(f"failed to patch {key} in {path}")

sub("dl_carrierBandwidth", target)
sub("ul_carrierBandwidth", target)
sub("dl_absoluteFrequencyPointA", point_a)
sub("initialDLBWPlocationAndBandwidth", riv)
sub("initialULBWPlocationAndBandwidth", riv)

open(path, "w", encoding="utf-8").write(text)
print(
    f"[patch] gnb-rfsim.yaml PRB {current}→{target} "
    f"PointA={point_a} RIV={riv} grid-center={grid} Hz (SSB center {center} Hz)"
)
PY

  log "[02] gNB RF profile patched for GNB_PRB=${GNB_PRB} (band 78, numerology 1)"
}

# Read SST/SD from the downloaded CN config (embb_slice1) so core / DB / UE stay aligned.
sync_slice_params_from_cn_config() {
  local cn="${CONFIG_DIR}/mini_nonrf_config.yaml"
  [[ -f "${cn}" ]] || die "Missing ${cn} after fetch."
  need_cmd python3

  eval "$(python3 - "${cn}" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
block = re.search(r"&embb_slice1\s*\n(.*?)(?=\n\s*- &embb_slice2|\n\s*- &custom_slice|\n[a-zA-Z#])", text, re.S)
if not block:
    raise SystemExit("embb_slice1 anchor not found in mini_nonrf_config.yaml")
chunk = block.group(1)
sst_m = re.search(r"sst:\s*(\d+)", chunk)
sd_m = re.search(r'sd:\s*"([0-9A-Fa-f]+)"', chunk)
if not sst_m:
    raise SystemExit("could not parse sst from embb_slice1 in mini_nonrf_config.yaml")
sst = int(sst_m.group(1))
sd = sd_m.group(1).upper() if sd_m else "FFFFFF"
sd_defaulted = 0 if sd_m else 1
print(f"SST={sst}")
print(f'SD="{sd}"')
print(f'SD_JSON="{sd}"')
print(f'SD_HEX="0x{sd.lower()}"')
print(f"SD_DEFAULTED={sd_defaulted}")
PY
)"

  if [[ "${SD_DEFAULTED:-0}" == "1" ]]; then
    log "[02] INFO: embb_slice1 has no sd in upstream CN config — using FFFFFF (rfsim tutorial)"
  fi
  log "[02] Slice from mini_nonrf_config.yaml (embb_slice1): SST=${SST} SD=${SD} (JSON/DB) SD_HEX=${SD_HEX} (UE/gNB)"
}

# Build dnnConfigurations JSON for MySQL SessionManagementSubscriptionData.
# MySQL rows document per-UE slice/DNN/5QI (Gate 1.7). SMF mini deploy (no UDM pod) uses
# local_subscription_infos in mini_nonrf_config (use_local_subscription_info=yes).
# GCS/UAV use C2_5QI (default 82); ROGUE uses ROGUE_5QI (default 9). MAC/GBR not verified here.
# EQUAL_5QI=1 forces both to EQUAL_5QI_VALUE (default 9) for QoS ablation.
build_dnn_json() {
  local dnn="$1" qi="$2" ul="$3" dl="$4" prio="${5:-1}"
  printf '{"%s":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":%s,"arp":{"priorityLevel":%s,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":%s},"sessionAmbr":{"uplink":"%s","downlink":"%s"}}}' \
    "${dnn}" "${qi}" "${prio}" "${prio}" "${ul}" "${dl}"
}

# Per-UE slice/DNN subscription rows (ROGUE → slice A; GCS+UAV → C2 slice/DNN).
# OAI AMF v2.2.1 (enable_simple_scenario) reads 5G_AKA keys from legacy `users` table.
# cn5g SessionManagementSubscriptionData rows provision slice/DNN/5QI per UE (artifact + Gate 1.7).
ensure_testbed_subscribers_in_db() {
  local sql="${CONFIG_DIR}/oai_db.sql"
  local patch="${CONFIG_DIR}/oai_db_testbed.patch.sql"
  local imsi injected=0 role
  local sst sd_json dnn qi ul dl prio nssai_am dnn_json

  [[ -f "${sql}" ]] || die "Missing ${sql} after fetch."
  : > "${patch}"

  inject_amf_legacy_users_table() {
    cat >> "${patch}" <<'EOF'
-- AMF v2.2.1 simple scenario: Milenage keys from legacy users table (cn5g dump has no users).
CREATE TABLE IF NOT EXISTS `users` (
  `imsi` varchar(15) NOT NULL,
  `msisdn` varchar(46) DEFAULT NULL,
  `imei` varchar(15) DEFAULT NULL,
  `imei_sv` varchar(2) DEFAULT NULL,
  `ms_ps_status` enum('PURGED','NOT_PURGED') DEFAULT 'PURGED',
  `rau_tau_timer` int(10) unsigned DEFAULT '120',
  `ue_ambr_ul` bigint(20) unsigned DEFAULT '50000000',
  `ue_ambr_dl` bigint(20) unsigned DEFAULT '100000000',
  `access_restriction` int(10) unsigned DEFAULT '60',
  `mme_cap` int(10) unsigned zerofill DEFAULT NULL,
  `mmeidentity_idmmeidentity` int(11) NOT NULL DEFAULT '0',
  `key` varbinary(16) NOT NULL DEFAULT '0',
  `RFSP-Index` smallint(5) unsigned NOT NULL DEFAULT '1',
  `urrp_mme` tinyint(1) NOT NULL DEFAULT '0',
  `sqn` bigint(20) unsigned zerofill NOT NULL,
  `rand` varbinary(16) NOT NULL,
  `OPc` varbinary(16) DEFAULT NULL,
  PRIMARY KEY (`imsi`,`mmeidentity_idmmeidentity`)
) ENGINE=MyISAM DEFAULT CHARSET=latin1;
EOF
    for imsi in $(all_testbed_imsis); do
      cat >> "${patch}" <<EOF
DELETE FROM \`users\` WHERE \`imsi\`='${imsi}';
INSERT INTO \`users\` VALUES ('${imsi}','1','55000000000000',NULL,'PURGED',50,40000000,100000000,47,0000000000,1,0x${UE_KEY},0,0,0x40,'ebd07771ace8677a',0x${UE_OPC});
EOF
    done
    injected=1
    log "[02]   AMF auth: legacy users rows for $(all_testbed_imsis | tr '\n' ' ')"
  }

  if grep -q 'CREATE TABLE `AuthenticationSubscription`' "${sql}"; then
    log "[02] oai_db.sql schema: cn5g (SessionManagement per-UE 5QI) + legacy users (AMF 5G_AKA)."
    inject_amf_legacy_users_table
    inject_cn5g_subscriber() {
      local imsi="$1" nssai_am="$2" sst="$3" sd_json="$4" dnn_json="$5" label="$6"
      log "[02]   ${imsi} (${label}): sst=${sst} sd=${sd_json} via SessionManagementSubscriptionData"
      cat >> "${patch}" <<EOF
DELETE FROM \`SessionManagementSubscriptionData\` WHERE \`ueid\`='${imsi}';
DELETE FROM \`AccessAndMobilitySubscriptionData\` WHERE \`ueid\`='${imsi}';
DELETE FROM \`AuthenticationSubscription\` WHERE \`ueid\`='${imsi}';
INSERT INTO \`AuthenticationSubscription\` (\`ueid\`, \`authenticationMethod\`, \`encPermanentKey\`, \`protectionParameterId\`, \`sequenceNumber\`, \`authenticationManagementField\`, \`algorithmId\`, \`encOpcKey\`, \`encTopcKey\`, \`vectorGenerationInHss\`, \`n5gcAuthMethod\`, \`rgAuthenticationInd\`, \`supi\`) VALUES
('${imsi}', '5G_AKA', '${UE_KEY}', '${UE_KEY}', '{\"sqn\": \"000000000000\", \"sqnScheme\": \"NON_TIME_BASED\", \"lastIndexes\": {\"ausf\": 0}}', '8000', 'milenage', '${UE_OPC}', NULL, NULL, NULL, NULL, '${imsi}');
INSERT INTO \`AccessAndMobilitySubscriptionData\` (\`ueid\`, \`servingPlmnid\`, \`nssai\`) VALUES
('${imsi}', '${PLMN}', '${nssai_am}');
INSERT INTO \`SessionManagementSubscriptionData\` (\`ueid\`, \`servingPlmnid\`, \`singleNssai\`, \`dnnConfigurations\`) VALUES
('${imsi}', '${PLMN}', '{\"sst\": ${sst}, \"sd\": \"${sd_json}\"}', '${dnn_json}');
EOF
      injected=1
    }

    for role in "${UE_ROLES[@]}"; do
      read -r sst sd_json _ dnn qi <<< "$(ue_slice_profile "${role}")"
      imsi="$(ue_role_imsi "${role}")"
      nssai_am='{\"defaultSingleNssais\": [{\"sst\": '"${sst}"', \"sd\": \"'"${sd_json}"'\"}]}'
      if [[ "${qi}" == "82" ]]; then
        ul="5Mbps"; dl="10Mbps"; prio="3"
      else
        ul="200Mbps"; dl="400Mbps"; prio="1"
      fi
      dnn_json="$(build_dnn_json "${dnn}" "${qi}" "${ul}" "${dl}" "${prio}")"
      inject_cn5g_subscriber "${imsi}" "${nssai_am}" "${sst}" "${sd_json}" "${dnn_json}" "${role}/${TOPOLOGY}"
    done
  else
    die "Unsupported oai_db.sql schema (expected cn5g AuthenticationSubscription table)."
  fi

  if (( injected )); then
    log "[02] Appending testbed subscriber INSERTs to oai_db.sql"
    cat "${patch}" >> "${sql}"
  fi
  log "[02] oai_db.sql ready — topology=${TOPOLOGY}"
  log "[02]   ROGUE=${IMSI_ROGUE} GCS=${IMSI_GCS} UAV=${IMSI_UAV}"
  for role in "${UE_ROLES[@]}"; do
    read -r sst sd_json _ dnn qi <<< "$(ue_slice_profile "${role}")"
    log "[02]   ${role}: sst=${sst} sd=${sd_json} dnn=${dnn} 5qi=${qi}"
  done
}

# Parse gNB libconfig (ORANSlice .conf) and derive UE -C from absoluteFrequencySSB + grid cross-check.
parse_gnb_ran_params() {
  need_cmd python3
  local gnb_file="${CONFIG_DIR}/gnb-rfsim.yaml"
  [[ -f "${gnb_file}" ]] || die "Missing ${gnb_file} after fetch."

  eval "$(python3 - "${gnb_file}" "${GNB_PRB}" <<'PY'
import re, sys

path = sys.argv[1]
requested = int(sys.argv[2])
text = open(path, encoding="utf-8", errors="replace").read()

def conf_int(key: str) -> int:
    m = re.search(
        rf"^[ \t]*{re.escape(key)}[ \t]*[:=][ \t]*(\d+)\s*(?:;|\s*(?:#.*)?$)",
        text,
        re.M,
    )
    if not m:
        raise SystemExit(f"missing config key {key} in {path}")
    return int(m.group(1))

prb = conf_int("dl_carrierBandwidth")
if prb != requested:
    raise SystemExit(f"gnb-rfsim.yaml PRB={prb} != requested GNB_PRB={requested}")
mu = conf_int("dl_subcarrierSpacing")
ssb = conf_int("absoluteFrequencySSB")
point_a = conf_int("dl_absoluteFrequencyPointA")

# 3GPP TS 38.104 — n78 FR1 (600000 <= N_REF <= 653333): F = 3000 MHz + 15 kHz * (N - 600000)
def n78_hz(nref: int) -> int:
    if not (600000 <= nref <= 653333):
        raise SystemExit(f"NR-ARFCN {nref} outside n78 FR1 range")
    return int(round((3000.0 + 0.015 * (nref - 600000)) * 1e6))

center_from_ssb = n78_hz(ssb)
point_a_hz = n78_hz(point_a)
scs_hz = 15000 * (2 ** mu)
center_from_grid = point_a_hz + (prb * 12 * scs_hz) // 2
# OAI rfsim lockstep: gNB advertises --ssb <firstSC> to UEs (20-RB SSB width in get_ssb_first_sc).
ssb_first_sc = int((center_from_ssb - point_a_hz) / scs_hz - (20 // 2 * 12))
if ssb_first_sc < 0:
    raise SystemExit(
        f"SSB first SC negative ({ssb_first_sc}) — PointA={point_a} SSB={ssb} PRB={prb} inconsistent"
    )

# Cross-check: SSB-derived carrier vs PointA+PRB grid (within 1 MHz).
if abs(center_from_ssb - center_from_grid) > 1_000_000:
    raise SystemExit(
        f"RF mismatch: SSB {center_from_ssb} Hz vs grid {center_from_grid} Hz "
        f"(SSB-NR-ARFCN={ssb}, PointA={point_a}, PRB={prb}, mu={mu})"
    )

print(f"GNB_PRB={prb}")
print(f"GNB_NUMEROLOGY={mu}")
print(f"GNB_SSB_ARFCN={ssb}")
print(f"GNB_POINT_A_ARFCN={point_a}")
print(f"GNB_CENTER_HZ={center_from_ssb}")
print(f"GNB_CENTER_GRID_HZ={center_from_grid}")
print(f"GNB_SSB_FIRST_SC={ssb_first_sc}")
PY
)"

  [[ -n "${GNB_NUMEROLOGY}" && -n "${GNB_CENTER_HZ}" && -n "${GNB_SSB_ARFCN}" && -n "${GNB_SSB_FIRST_SC}" ]] \
    || die "parse_gnb_ran_params produced empty values."

  log "[02] gNB/UE RF lockstep: PRB=${GNB_PRB} numerology=${GNB_NUMEROLOGY} -C=${GNB_CENTER_HZ} Hz --ssb ${GNB_SSB_FIRST_SC} --band ${GNB_BAND}"
  _ssb_mhz="$(awk -v hz="${GNB_CENTER_HZ}" 'BEGIN { printf "%.2f", hz / 1e6 }')"
  log "[02]   absoluteFrequencySSB=${GNB_SSB_ARFCN} (n78 -> ${_ssb_mhz} MHz)"
  log "[02]   dl_absoluteFrequencyPointA=${GNB_POINT_A_ARFCN} grid-center=${GNB_CENTER_GRID_HZ} Hz (cross-check OK)"
  log "[02] Slice lockstep: SST=${SST} SD=${SD_JSON} (core/DB) SD_HEX=${SD_HEX} (UE uicc0 / gNB)"
}

# Docker-compose uses eth1 on traffic_net for UPF N6; k8s pods have a single eth0 — patch here.
# develop mini_nonrf_config ships PLMN 208/95; rfsim tutorial + oai_db.sql + gNB/UE use 208/99.
# SMF PFCP to ClusterIP oai-upf is flaky on Kind — use headless oai-upf-hl (pod IP).
# Slice B (embb_slice2) + DNN_B only for TOPOLOGY=cross (1 UPF serves both DNNs).
patch_cn_config_for_k8s() {
  need_cmd python3
  python3 - "${CONFIG_DIR}/mini_nonrf_config.yaml" "${CONFIG_DIR}/mini_nonrf_config.k8s.yaml" \
    "${MNC}" "${DNN_B}" "${SD}" "${SD_B}" "${SST_B}" "${TOPOLOGY}" \
    "${ROGUE_5QI}" "${C2_5QI}" <<'PY'
import re, sys

src, dst, mnc, dnn_b, sd_a, sd_b, sst_b, topology = sys.argv[1:9]
rogue_5qi, c2_5qi = int(sys.argv[9]), int(sys.argv[10])
text = open(src, encoding="utf-8", errors="replace").read()

def ambr_for(qi: int) -> tuple[str, str]:
    # Match MySQL build_dnn_json policy: 82 → URLLC AMBR; else best-effort high AMBR.
    if qi == 82:
        return "5Mbps", "10Mbps"
    return "200Mbps", "400Mbps"

rogue_ul, rogue_dl = ambr_for(rogue_5qi)
c2_ul, c2_dl = ambr_for(c2_5qi)

text = text.replace("interface_name: eth1", "interface_name: eth0")
text = re.sub(r"mnc: 95\b", f"mnc: {mnc}", text)
text = text.replace("host: oai-upf", "host: oai-upf-hl")
text = text.replace("tac: 0xa000", "tac: 1")

# embb_slice1: inject sd when upstream ships sst-only anchor.
if re.search(r"&embb_slice1\n    sst: 1\n    sd:", text) is None:
    text = re.sub(
        r"(&embb_slice1\n    sst: 1)\n",
        lambda m: f"{m.group(1)}\n    sd: {sd_a}\n",
        text,
        count=1,
    )
else:
    text = re.sub(
        r"(&embb_slice1\n    sst: 1\n    sd: )[0-9A-Fa-f\"]+",
        lambda m: f"{m.group(1)}{sd_a}",
        text,
        count=1,
    )

# Mini AMF/SMF/UPF only — no oai-udm. SMF must use local_subscription_infos (yes), not UDM HTTP.

SLICE_B_DNN_UPSTREAM = "oai.ipv4"

def strip_slice_anchor(text, anchor):
    text = re.sub(
        rf"\n  - &{re.escape(anchor)}\n(?:    [^\n]+\n)+",
        "\n",
        text,
        count=1,
    )
    text = re.sub(rf"\n        - \*{re.escape(anchor)}\b", "", text)
    text = re.sub(
        rf"\n      - sNssai: \*{re.escape(anchor)}\n        dnnSmfInfoList:\n          - dnn: \"[^\"]+\"",
        "",
        text,
    )
    text = re.sub(
        rf"\n      - sNssai: \*{re.escape(anchor)}\n        dnnUpfInfoList:\n          - dnn: \"[^\"]+\"",
        "",
        text,
    )
    text = re.sub(
        rf"\n    - single_nssai: \*{re.escape(anchor)}\n      dnn: \"[^\"]+\"\n      qos_profile:\n        5qi: \d+\n        session_ambr_ul: \"[^\"]+\"\n        session_ambr_dl: \"[^\"]+\"",
        "",
        text,
    )
    return text

def strip_dnn_block(text, dnn):
    return re.sub(
        rf'\n  - dnn: "{re.escape(dnn)}"\n    pdu_session_type: "[^"]+"\n    ipv4_subnet: "[^"]+"',
        "",
        text,
    )

# custom_slice (sst 222) is tutorial baggage — not used by FlyBlind; drop in both topologies.
text = strip_slice_anchor(text, "custom_slice")
text = strip_dnn_block(text, "default")

if topology == "intra":
    text = strip_slice_anchor(text, "embb_slice2")
    for legacy_dnn in (dnn_b, SLICE_B_DNN_UPSTREAM, "openairinterface"):
        text = strip_dnn_block(text, legacy_dnn)
    if f"*embb_slice2" in text or "&embb_slice2" in text:
        raise SystemExit("intra patch: embb_slice2 references remain")
    if SLICE_B_DNN_UPSTREAM in text or dnn_b in text:
        raise SystemExit(f"intra patch: slice-B DNN still present ({dnn_b!r} / {SLICE_B_DNN_UPSTREAM!r})")
    # Single-slice intra: all UEs on dnn oai — align pool with SUBNET_SLICE_A gate.
    text = re.sub(
        r'(  - dnn: "oai"\n    pdu_session_type: "[^"]+"\n    ipv4_subnet: )"[^"]+"',
        r'\g<1>"12.1.1.128/25"',
        text,
        count=1,
    )
else:
    text = text.replace('"openairinterface"', f'"{dnn_b}"')
    text = text.replace(f'"{SLICE_B_DNN_UPSTREAM}"', f'"{dnn_b}"')
    text = re.sub(
        r"(&embb_slice2\n    sst: )\d+(\n    sd: )[0-9A-Fa-f]+",
        lambda m: f"{m.group(1)}{sst_b}{m.group(2)}{sd_b}",
        text,
        count=1,
    )
    if "&embb_slice2" not in text:
        text = re.sub(
            r"(&embb_slice1\n    sst: \d+\n    sd: [0-9A-Fa-f\"]+.*\n)",
            lambda m: (
                f"{m.group(1)}  - &embb_slice2\n    sst: {sst_b}\n    sd: {sd_b} # in hex\n"
            ),
            text,
            count=1,
        )
    if not re.search(rf'(?m)^  - dnn: "{re.escape(dnn_b)}"', text):
        text = re.sub(
            r"(^dnns:\n)",
            rf'\1  - dnn: "{dnn_b}"\n    pdu_session_type: "IPV4"\n    ipv4_subnet: "12.1.1.64/26"\n',
            text,
            count=1,
            flags=re.M,
        )
    text = re.sub(
        rf'(dnn: "{re.escape(dnn_b)}"\n\s+qos_profile:\n\s+5qi: )\d+',
        rf"\g<1>{c2_5qi}",
        text,
    )
    text = re.sub(
        rf'(dnn: "{re.escape(dnn_b)}"\n\s+qos_profile:\n\s+5qi: {c2_5qi}\n\s+session_ambr_ul: )"[^"]+"',
        rf'\g<1>"{c2_ul}"',
        text,
    )
    text = re.sub(
        rf'(dnn: "{re.escape(dnn_b)}"\n\s+qos_profile:\n\s+5qi: {c2_5qi}\n\s+session_ambr_ul: "{re.escape(c2_ul)}"\n\s+session_ambr_dl: )"[^"]+"',
        rf'\g<1>"{c2_dl}"',
        text,
    )
    if "&embb_slice2" not in text or f'"{dnn_b}"' not in text:
        raise SystemExit("cross patch: embb_slice2 or slice-B DNN missing")
    if SLICE_B_DNN_UPSTREAM in text:
        raise SystemExit("cross patch: legacy oai.ipv4 DNN still present")
    # Normalize PDU pools — upstream rfsimulator bundle uses 12.1.1.0/24 + 12.1.2.0/24;
    # FlyBlind cross expects slice-A (oai) in 12.1.1.128/25 and C2 (oai.urllc) in 12.1.1.64/26.
    text = re.sub(
        r'(  - dnn: "oai"\n    pdu_session_type: "[^"]+"\n    ipv4_subnet: )"[^"]+"',
        r'\g<1>"12.1.1.128/25"',
        text,
        count=1,
    )
    text = re.sub(
        rf'(  - dnn: "{re.escape(dnn_b)}"\n    pdu_session_type: "[^"]+"\n    ipv4_subnet: )"[^"]+"',
        r'\g<1>"12.1.1.64/26"',
        text,
        count=1,
    )

    # Wire slice B into AMF/SMF/UPF (string splits — avoid regex backtracking on YAML).
    amf_nssai_block = text.split("plmn_support_list:")[1].split("supported_integrity")[0]
    if "*embb_slice2" not in amf_nssai_block:
        old = "      nssai:\n        - *embb_slice1\n  supported_integrity_algorithms:"
        new = "      nssai:\n        - *embb_slice1\n        - *embb_slice2\n  supported_integrity_algorithms:"
        if old not in text:
            raise SystemExit("cross patch: AMF plmn_support nssai anchor pattern not found")
        text = text.replace(old, new, 1)
    smf_block = text.split("sNssaiSmfInfoList:")[1].split("local_subscription_infos:")[0]
    if "*embb_slice2" not in smf_block:
        old = (
            "    sNssaiSmfInfoList:\n"
            "      - sNssai: *embb_slice1\n"
            "        dnnSmfInfoList:\n"
            "          - dnn: \"oai\"\n"
            "  local_subscription_infos:"
        )
        new = (
            "    sNssaiSmfInfoList:\n"
            "      - sNssai: *embb_slice1\n"
            "        dnnSmfInfoList:\n"
            "          - dnn: \"oai\"\n"
            f"      - sNssai: *embb_slice2\n"
            f"        dnnSmfInfoList:\n"
            f"          - dnn: \"{dnn_b}\"\n"
            "  local_subscription_infos:"
        )
        if old not in text:
            raise SystemExit("cross patch: SMF sNssaiSmfInfoList anchor pattern not found")
        text = text.replace(old, new, 1)
    upf_block = text.split("sNssaiUpfInfoList:")[1].split("## DNN configuration")[0]
    if "*embb_slice2" not in upf_block:
        old = (
            "    sNssaiUpfInfoList:\n"
            "      - sNssai: *embb_slice1\n"
            "        dnnUpfInfoList:\n"
            "          - dnn: \"oai\"\n"
            "\n## DNN configuration"
        )
        new = (
            "    sNssaiUpfInfoList:\n"
            "      - sNssai: *embb_slice1\n"
            "        dnnUpfInfoList:\n"
            "          - dnn: \"oai\"\n"
            f"      - sNssai: *embb_slice2\n"
            f"        dnnUpfInfoList:\n"
            f"          - dnn: \"{dnn_b}\"\n"
            "\n## DNN configuration"
        )
        if old not in text:
            raise SystemExit("cross patch: UPF sNssaiUpfInfoList anchor pattern not found")
        text = text.replace(old, new, 1)
    lsub_block = text.split("local_subscription_infos:")[1].split("\nupf:")[0]
    if "*embb_slice2" not in lsub_block:
        old = (
            "  local_subscription_infos:\n"
            "    - single_nssai: *embb_slice1\n"
            "      dnn: \"oai\"\n"
            "      qos_profile:\n"
            "        5qi: 9\n"
            "        session_ambr_ul: \"200Mbps\"\n"
            "        session_ambr_dl: \"400Mbps\"\n"
            "\nupf:"
        )
        new = (
            "  local_subscription_infos:\n"
            "    - single_nssai: *embb_slice1\n"
            "      dnn: \"oai\"\n"
            "      qos_profile:\n"
            f"        5qi: {rogue_5qi}\n"
            f"        session_ambr_ul: \"{rogue_ul}\"\n"
            f"        session_ambr_dl: \"{rogue_dl}\"\n"
            f"    - single_nssai: *embb_slice2\n"
            f"      dnn: \"{dnn_b}\"\n"
            "      qos_profile:\n"
            f"        5qi: {c2_5qi}\n"
            f"        session_ambr_ul: \"{c2_ul}\"\n"
            f"        session_ambr_dl: \"{c2_dl}\"\n"
            "\nupf:"
        )
        if old not in text:
            raise SystemExit("cross patch: SMF local_subscription_infos anchor pattern not found")
        text = text.replace(old, new, 1)
    # Always force configured 5QI/AMBR (idempotent across EQUAL_5QI toggles).
    text = re.sub(
        r'(dnn: "oai"\n      qos_profile:\n        5qi: )\d+(\n        session_ambr_ul: )"[^"]+"(\n        session_ambr_dl: )"[^"]+"',
        rf'\g<1>{rogue_5qi}\g<2>"{rogue_ul}"\g<3>"{rogue_dl}"',
        text,
        count=1,
    )
    text = re.sub(
        rf'(dnn: "{re.escape(dnn_b)}"\n      qos_profile:\n        5qi: )\d+(\n        session_ambr_ul: )"[^"]+"(\n        session_ambr_dl: )"[^"]+"',
        rf'\g<1>{c2_5qi}\g<2>"{c2_ul}"\g<3>"{c2_dl}"',
        text,
        count=1,
    )
    if "*embb_slice2" not in text.split("plmn_support_list:")[1].split("supported_integrity")[0]:
        raise SystemExit("cross patch: embb_slice2 missing from AMF plmn_support nssai")
    if "*embb_slice2" not in text.split("sNssaiSmfInfoList:")[1].split("local_subscription_infos:")[0]:
        raise SystemExit("cross patch: embb_slice2 missing from SMF sNssaiSmfInfoList")
    if "*embb_slice2" not in text.split("sNssaiUpfInfoList:")[1].split("## DNN configuration")[0]:
        raise SystemExit("cross patch: embb_slice2 missing from UPF sNssaiUpfInfoList")

# Both topologies: force SMF local_subscription 5QI/AMBR for DNN oai (ROGUE / intra).
text = re.sub(
    r'(dnn: "oai"\n      qos_profile:\n        5qi: )\d+(\n        session_ambr_ul: )"[^"]+"(\n        session_ambr_dl: )"[^"]+"',
    rf'\g<1>{rogue_5qi}\g<2>"{rogue_ul}"\g<3>"{rogue_dl}"',
    text,
    count=1,
)

open(dst, "w", encoding="utf-8").write(text)
PY

  if [[ "${TOPOLOGY}" == "intra" ]]; then
    log "[02] CN config patched (intra): 1 slice sst=${SST} sd=${SD} dnn=${DNN} — no slice B / ${DNN_B}"
  else
    log "[02] CN config patched (cross): eth0, PLMN ${MCC}/${MNC}, UPF oai-upf-hl"
    log "[02]   slice A: sst=${SST} sd=${SD} dnn=${DNN} (ROGUE 5QI ${ROGUE_5QI})"
    log "[02]   slice B: sst=${SST_B} sd=${SD_B} dnn=${DNN_B} (C2 5QI ${C2_5QI})"
  fi
  log "[02]   SMF use_local_subscription_info=yes (local_subscription_infos; no UDM in mini deploy)"
}

write_entrypoints() {
  [[ -n "${GNB_PRB}" && -n "${GNB_NUMEROLOGY}" && -n "${GNB_CENTER_HZ}" && -n "${GNB_SSB_FIRST_SC}" ]] \
    || die "write_entrypoints: call parse_gnb_ran_params first."

  cat > "${CONFIG_DIR}/gnb-entrypoint.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
AMF_HOST="${AMF_HOSTNAME:?AMF_HOSTNAME required}"
MIN_RATIO_A="${MIN_RATIO_SLICE_A:-0}"
MIN_RATIO_B="${MIN_RATIO_SLICE_B:-0}"
POD_IP="$(ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1 | head -n1)"
[[ -n "${POD_IP}" ]] || { echo "eth0 has no IPv4"; exit 1; }
AMF_IP="$(getent ahostsv4 "${AMF_HOST}" | awk '{print $1; exit}')"
[[ -n "${AMF_IP}" ]] || { echo "Cannot resolve ${AMF_HOST}"; exit 1; }
mkdir -p /opt/oai-gnb/etc
sed -e "s|__AMF_IP__|${AMF_IP}|g" -e "s|__GNB_IP__|${POD_IP}|g" \
    /config/gnb-template.yaml > /opt/oai-gnb/etc/gnb.yaml
sed -e "s|__MIN_RATIO_A__|${MIN_RATIO_A}|g" -e "s|__MIN_RATIO_B__|${MIN_RATIO_B}|g" \
    /config/rrmPolicy-template.json > /opt/oai-gnb/etc/rrmPolicy.json
echo "[oai-gnb] AMF ${AMF_HOST} -> ${AMF_IP}, N2/N3 ${POD_IP}"
if [[ "${TOPOLOGY:-intra}" == "intra" ]]; then
  echo "[oai-gnb] rrmPolicyRatio (1 slice) min_ratio=${MIN_RATIO_A}"
else
  echo "[oai-gnb] rrmPolicyRatio slice-A min_ratio=${MIN_RATIO_A} slice-B min_ratio=${MIN_RATIO_B}"
fi
# Call nr-softmodem directly. Do NOT chain the image's official entrypoint: it re-injects
# "-O <config>" and the leading -O collides with bash's exec ("exec: -O: invalid option").
# MAC_log_level=info triggers OAI's periodic (~1/s) per-UE scheduler dump (dump_mac_stats):
# per-RNTI dl/ul bytes + MCS. Mapping RNTI->role->slice yields per-slice PRB-grant evidence
# (proof-of-mechanism for the C2 contention). GNB_MAC_LOG overrides (info|debug|analysis).
# CPU pinning (PIN_CORES=1): taskset-confine the gNB scheduler+PHY to reserved cores so
# the attacker's CPU burn on other cores cannot jitter PRB grant timing (confounder fix).
PIN=""
if [[ -n "${CORE_PIN:-}" ]]; then
  PIN="taskset -c ${CORE_PIN}"
  echo "[oai-gnb] CPU pinning: cores ${CORE_PIN}"
fi
exec ${PIN} /opt/oai-gnb/bin/nr-softmodem --sa -O /opt/oai-gnb/etc/gnb.yaml \
    -E --rfsim --rfsimulator.serveraddr server \
    --log_config.global_log_options level,nocolor,time \
    --log_config.mac_log_level "${GNB_MAC_LOG:-info}"
EOF

  cat > "${CONFIG_DIR}/ue-entrypoint.sh" <<EOF
#!/bin/bash
set -euo pipefail
ROLE="\${UE_ROLE:?UE_ROLE required}"
GNB_HOST="\${GNB_RFSIM_HOST:?GNB_RFSIM_HOST required}"
CONF_SRC="/config/nr-ue-\${ROLE}.conf"
GNB_IP="\$(getent ahostsv4 "\${GNB_HOST}" | awk '{print \$1; exit}')"
[[ -n "\${GNB_IP}" ]] || { echo "Cannot resolve \${GNB_HOST}"; exit 1; }
mkdir -p /opt/oai-nr-ue/etc
sed -e "s|__GNB_RFSIM_ADDR__|\${GNB_IP}|g" "\${CONF_SRC}" > /opt/oai-nr-ue/etc/nr-ue.conf
echo "[oai-ue-\${ROLE}] RFsim -> \${GNB_HOST} (\${GNB_IP})"
# FlyBlind C2 tools (GCS/UAV/ROGUE): copy to /tmp and ensure pymavlink in pod.
if [[ -d /config/tm-tools ]]; then
  cp -a /config/tm-tools/. /tmp/ 2>/dev/null || true
  chmod +x /tmp/*.sh /tmp/rogue_pulse.py /tmp/udp_flood.py /tmp/c2_sync_flood.py /tmp/owd_*.py 2>/dev/null || true
  if ! python3 -c "import pymavlink" 2>/dev/null; then
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -qq >/dev/null 2>&1 || true
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip iproute2 netcat-openbsd iperf3 >/dev/null 2>&1 || true
    fi
    pip3 install pymavlink 2>/dev/null || pip3 install --break-system-packages pymavlink 2>/dev/null || true
  fi
fi
# Call nr-uesoftmodem directly (see gNB entrypoint: chaining the official one breaks on -O).
# CPU pinning (PIN_CORES=1): confine this UE softmodem to its reserved cores so CPU
# affinity reduces single-host scheduling interference as a confounder.
PIN=""
if [[ -n "\${CORE_PIN:-}" ]]; then
  PIN="taskset -c \${CORE_PIN}"
  echo "[oai-ue-\${ROLE}] CPU pinning: cores \${CORE_PIN}"
fi
exec \${PIN} /opt/oai-nr-ue/bin/nr-uesoftmodem --sa -O /opt/oai-nr-ue/etc/nr-ue.conf \
    -E --rfsim --rfsimulator.serveraddr "\${GNB_IP}" \
    -r ${GNB_PRB} --numerology ${GNB_NUMEROLOGY} -C ${GNB_CENTER_HZ} \
    --band ${GNB_BAND} --ssb ${GNB_SSB_FIRST_SC} \
    --log_config.global_log_options level,nocolor,time
EOF

  chmod +x "${CONFIG_DIR}/gnb-entrypoint.sh" "${CONFIG_DIR}/ue-entrypoint.sh"
}

write_rrm_policy_template() {
  local sd_a_int sd_b_int
  sd_a_int="$(python3 -c "print(int('${SD}', 16))")"
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    cat > "${CONFIG_DIR}/rrmPolicy-template.json" <<EOF
{
  "rrmPolicyRatio": [
    {
      "sst": ${SST},
      "sd": ${sd_a_int},
      "min_ratio": __MIN_RATIO_A__,
      "max_ratio": 100,
      "dedicated_ratio": 0
    }
  ]
}
EOF
    log "[02] rrmPolicy-template.json: intra — 1 slice (sst=${SST} sd=${SD}) min_ratio=${MIN_RATIO_SLICE_A}"
    return 0
  fi
  sd_b_int="$(python3 -c "print(int('${SD_B}', 16))")"
  cat > "${CONFIG_DIR}/rrmPolicy-template.json" <<EOF
{
  "rrmPolicyRatio": [
    {
      "sst": ${SST},
      "sd": ${sd_a_int},
      "min_ratio": __MIN_RATIO_A__,
      "max_ratio": ${MAX_RATIO_SLICE_A},
      "dedicated_ratio": ${DED_RATIO_SLICE_A}
    },
    {
      "sst": ${SST_B},
      "sd": ${sd_b_int},
      "min_ratio": __MIN_RATIO_B__,
      "max_ratio": ${MAX_RATIO_SLICE_B},
      "dedicated_ratio": ${DED_RATIO_SLICE_B}
    }
  ]
}
EOF
  log "[02] rrmPolicy-template.json: cross — slice-A(sst=${SST} sd=${SD} max=${MAX_RATIO_SLICE_A} ded=${DED_RATIO_SLICE_A}) slice-B(sst=${SST_B} sd=${SD_B} max=${MAX_RATIO_SLICE_B} ded=${DED_RATIO_SLICE_B})"
}

patch_gnb_one_slice() {
  need_cmd python3
  python3 - "${CONFIG_DIR}/gnb-template.yaml" "${SST}" "${SD_HEX}" <<'PY'
import re, sys

path, sst_a, sd_a = sys.argv[1:4]
text = open(path, encoding="utf-8", errors="replace").read()

pat = re.compile(r"snssaiList\s*=\s*\(\s*\{[^}]+\}\s*\)")
repl = f"snssaiList = ({{ sst = {sst_a}, sd = {sd_a} }})"
if not pat.search(text):
    raise SystemExit(f"snssaiList block not found in {path}")
text = pat.sub(repl, text, count=1)

if "SliceConf" not in text:
    text = re.sub(
        r"(MACRLCs\s*=\s*\(\s*\{[^}]*pucch_TargetSNRx10\s*=\s*\d+;\s*)",
        r'\1    SliceConf = "/opt/oai-gnb/etc/rrmPolicy.json";\n',
        text,
        count=1,
        flags=re.S,
    )

open(path, "w", encoding="utf-8").write(text)
PY
  log "[02] gNB template: intra — 1 S-NSSAI (${SST}/${SD_HEX}) + MACRLCs.SliceConf"
}

patch_gnb_two_slices() {
  need_cmd python3
  python3 - "${CONFIG_DIR}/gnb-template.yaml" "${SST}" "${SD_HEX}" "${SST_B}" "${SD_B_HEX}" <<'PY'
import re, sys

path, sst_a, sd_a, sst_b, sd_b = sys.argv[1:6]
text = open(path, encoding="utf-8", errors="replace").read()

pat = re.compile(r"snssaiList\s*=\s*\(\s*\{[^}]+\}\s*\)")
repl = f"snssaiList = ({{ sst = {sst_a}, sd = {sd_a} }}, {{ sst = {sst_b}, sd = {sd_b} }})"
if not pat.search(text):
    raise SystemExit(f"snssaiList block not found in {path}")
text = pat.sub(repl, text, count=1)

if "SliceConf" not in text:
    text = re.sub(
        r"(MACRLCs\s*=\s*\(\s*\{[^}]*pucch_TargetSNRx10\s*=\s*\d+;\s*)",
        r'\1    SliceConf = "/opt/oai-gnb/etc/rrmPolicy.json";\n',
        text,
        count=1,
        flags=re.S,
    )

open(path, "w", encoding="utf-8").write(text)
PY
  log "[02] gNB template: cross — 2 S-NSSAI (A=${SST}/${SD_HEX} B=${SST_B}/${SD_B_HEX}) + MACRLCs.SliceConf"
}

patch_gnb_slices() {
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    patch_gnb_one_slice
  else
    patch_gnb_two_slices
  fi
}

write_gnb_template() {
  sed -e 's|ipv4 = "192\.168\.71\.132"|ipv4 = "__AMF_IP__"|g' \
      -e 's|GNB_IPV4_ADDRESS_FOR_NG_AMF              = "192\.168\.71\.140"|GNB_IPV4_ADDRESS_FOR_NG_AMF              = "__GNB_IP__"|g' \
      -e 's|GNB_IPV4_ADDRESS_FOR_NGU                 = "192\.168\.71\.140"|GNB_IPV4_ADDRESS_FOR_NGU                 = "__GNB_IP__"|g' \
      "${CONFIG_DIR}/gnb-rfsim.yaml" > "${CONFIG_DIR}/gnb-template.yaml"
  patch_gnb_slices
}

write_ue_uicc_conf() {
  local role="$1" imsi="$2" dnn="$3" sst="$4" sd_hex="$5"
  local sessions
  sessions="({ id = 1; type = \"IPv4\"; dnn = \"${dnn}\"; nssai_sst = ${sst}; nssai_sd = ${sd_hex}; });"
  cat > "${CONFIG_DIR}/nr-ue-${role}.conf" <<EOF
uicc0 = {
  imsi = "${imsi}";
  key = "${UE_KEY}";
  opc = "${UE_OPC}";
  dnn = "${dnn}";
  nssai_sst = ${sst};
  nssai_sd = ${sd_hex};
  pdu_sessions = ${sessions}
}

rfsimulator = (
  { serveraddr = "__GNB_RFSIM_ADDR__"; }
);

thread-pool = "-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1"
EOF
}

apply_mysql_init_configmap() {
  # oai_db.sql contains binary literals. Rendering to YAML on the client (-o yaml) fails with
  # "control characters are not allowed". Apply server-side instead so kubectl stores the
  # non-UTF8 payload as base64 binaryData. Delete-then-create keeps it idempotent on re-runs.
  log "[02] Creating ConfigMap oai-mysql-init from file (binary-safe, server-side)..."
  kubectl delete configmap oai-mysql-init -n "${NS_CORE}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl create configmap oai-mysql-init \
    --from-file=oai_db.sql="${CONFIG_DIR}/oai_db.sql" \
    -n "${NS_CORE}" \
    || die "Failed to create oai-mysql-init ConfigMap from ${CONFIG_DIR}/oai_db.sql"
}

write_oai_core_manifest() {
  cat > "${CONFIG_DIR}/oai-core.yaml" <<EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: oai-cn-config
  namespace: ${NS_CORE}
data:
  config.yaml: |
$(sed 's/^/    /' "${CONFIG_DIR}/mini_nonrf_config.k8s.yaml")
---
apiVersion: v1
kind: Service
metadata:
  name: mysql
  namespace: ${NS_CORE}
spec:
  selector:
    app: oai-mysql
  ports:
    - port: 3306
      targetPort: 3306
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oai-mysql
  namespace: ${NS_CORE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oai-mysql
  template:
    metadata:
      labels:
        app: oai-mysql
    spec:
      containers:
        - name: mysql
          image: ${MYSQL_IMAGE}
          env:
            - name: MYSQL_DATABASE
              value: oai_db
            - name: MYSQL_USER
              value: test
            - name: MYSQL_PASSWORD
              value: test
            - name: MYSQL_ROOT_PASSWORD
              value: linux
          volumeMounts:
            # Mount the whole initdb dir (not subPath): subPath does not reliably decode
            # ConfigMap binaryData, which corrupts the SQL dump. MySQL runs every *.sql here.
            - name: initdb
              mountPath: /docker-entrypoint-initdb.d
          readinessProbe:
            exec:
              command: ["mysqladmin", "ping", "-h", "127.0.0.1", "-utest", "-ptest"]
            initialDelaySeconds: 20
            periodSeconds: 5
            failureThreshold: 30
      volumes:
        - name: initdb
          configMap:
            name: oai-mysql-init
---
apiVersion: v1
kind: Service
metadata:
  name: oai-amf
  namespace: ${NS_CORE}
spec:
  selector:
    app: oai-amf
  ports:
    - name: sbi
      port: 80
      targetPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: oai-amf-ngap
  namespace: ${NS_CORE}
spec:
  clusterIP: None
  selector:
    app: oai-amf
  ports:
    - name: ngap
      port: 38412
      targetPort: 38412
      protocol: SCTP
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oai-amf
  namespace: ${NS_CORE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oai-amf
  template:
    metadata:
      labels:
        app: oai-amf
    spec:
      initContainers:
        - name: wait-mysql
          image: ${BUSYBOX_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - |
              until nc -z mysql.${NS_CORE}.svc.cluster.local 3306; do
                echo "[wait-mysql] waiting for MySQL..."
                sleep 3
              done
      containers:
        - name: amf
          image: ${OAI_AMF_IMAGE}
          imagePullPolicy: IfNotPresent
          volumeMounts:
            - name: cn-config
              mountPath: /openair-amf/etc/config.yaml
              subPath: config.yaml
      volumes:
        - name: cn-config
          configMap:
            name: oai-cn-config
---
apiVersion: v1
kind: Service
metadata:
  name: oai-smf
  namespace: ${NS_CORE}
spec:
  selector:
    app: oai-smf
  ports:
    - port: 80
      targetPort: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oai-smf
  namespace: ${NS_CORE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oai-smf
  template:
    metadata:
      labels:
        app: oai-smf
    spec:
      initContainers:
        - name: wait-mysql
          image: ${BUSYBOX_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - |
              until nc -z mysql.${NS_CORE}.svc.cluster.local 3306; do
                echo "[wait-mysql] waiting for MySQL..."
                sleep 3
              done
      containers:
        - name: smf
          image: ${OAI_SMF_IMAGE}
          imagePullPolicy: IfNotPresent
          volumeMounts:
            - name: cn-config
              mountPath: /openair-smf/etc/config.yaml
              subPath: config.yaml
      volumes:
        - name: cn-config
          configMap:
            name: oai-cn-config
---
apiVersion: v1
kind: Service
metadata:
  name: oai-upf
  namespace: ${NS_CORE}
spec:
  selector:
    app: oai-upf
  ports:
    - name: n4
      port: 8805
      targetPort: 8805
    - name: n3
      port: 2152
      targetPort: 2152
      protocol: UDP
---
# Headless — ext-DN needs the UPF *pod* IP as route nexthop (ClusterIP is not a valid gateway).
apiVersion: v1
kind: Service
metadata:
  name: oai-upf-hl
  namespace: ${NS_CORE}
spec:
  clusterIP: None
  selector:
    app: oai-upf
  ports:
    - name: n4
      port: 8805
      targetPort: 8805
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oai-upf
  namespace: ${NS_CORE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oai-upf
  template:
    metadata:
      labels:
        app: oai-upf
    spec:
      initContainers:
        - name: wait-smf
          image: ${BUSYBOX_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - |
              until nc -z oai-smf.${NS_CORE}.svc.cluster.local 80; do
                echo "[wait-smf] waiting for SMF..."
                sleep 3
              done
      containers:
        - name: upf
          image: ${OAI_UPF_IMAGE}
          imagePullPolicy: IfNotPresent
          securityContext:
            privileged: true
            capabilities:
              add: ["NET_ADMIN", "SYS_ADMIN"]
          volumeMounts:
            - name: cn-config
              mountPath: /openair-upf/etc/config.yaml
              subPath: config.yaml
      volumes:
        - name: cn-config
          configMap:
            name: oai-cn-config
---
# Headless — ROGUE attack scripts resolve ext-DN pod IP via core DNS (iperf3 :5201).
apiVersion: v1
kind: Service
metadata:
  name: oai-ext-dn
  namespace: ${NS_CORE}
spec:
  clusterIP: None
  selector:
    app: oai-ext-dn
  ports:
    - name: iperf
      port: 5201
      targetPort: 5201
    - name: cotenant
      port: 5202
      targetPort: 5202
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oai-ext-dn
  namespace: ${NS_CORE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oai-ext-dn
  template:
    metadata:
      labels:
        app: oai-ext-dn
    spec:
      initContainers:
        - name: wait-upf
          image: ${BUSYBOX_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - |
              until nc -zu -w2 oai-upf.${NS_CORE}.svc.cluster.local 8805; do
                echo "[wait-upf] waiting for UPF N4 (UDP 8805)..."
                sleep 3
              done
      containers:
        - name: ext-dn
          image: ${EXT_DN_IMAGE}
          imagePullPolicy: IfNotPresent
          securityContext:
            privileged: true
          command: ["/bin/bash", "-c"]
          args:
            - |
              set -euo pipefail
              if ! command -v python3 >/dev/null 2>&1; then
                echo "[ext-dn] installing python3 (required for co_tenant_sink on :5202)..."
                export DEBIAN_FRONTEND=noninteractive
                apt-get update -qq >/dev/null 2>&1 || true
                apt-get install -y -qq python3 >/dev/null 2>&1 \
                  || { echo "[ext-dn] ERROR: failed to install python3"; exit 1; }
              fi
              UPF_IP=""
              for _ in \$(seq 1 45); do
                UPF_IP="\$(getent ahostsv4 oai-upf-hl.${NS_CORE}.svc.cluster.local 2>/dev/null | awk '{print \$1; exit}')"
                [[ -n "\${UPF_IP}" ]] && break
                sleep 2
              done
              [[ -n "\${UPF_IP}" ]] || { echo "[ext-dn] ERROR: cannot resolve UPF pod IP (oai-upf-hl)"; exit 1; }
              iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE || true
              ip route add 12.1.1.0/24 via "\${UPF_IP}" dev eth0 onlink || true
              echo "[ext-dn] PDU subnet 12.1.1.0/24 via UPF \${UPF_IP} (onlink)"
              iperf3 -s -D -p 5201 || true
              iperf3 -s -D -p 5202 || true
              echo "[ext-dn] iperf3 servers :5201 (smoke/rogue DL) + :5202 (gcs DL compete)"
              echo "[ext-dn] co-tenant sink tool at /tools/co_tenant_sink.py (orchestrator binds :5202 per run)"
              sleep infinity
          volumeMounts:
            - name: tm-ext-dn-tools
              mountPath: /tools
              readOnly: true
          readinessProbe:
            exec:
              command:
                - /bin/bash
                - -c
                - |
                  command -v python3 >/dev/null &&
                  command -v iperf3 >/dev/null &&
                  test -r /tools/co_tenant_sink.py &&
                  getent ahostsv4 oai-upf-hl.${NS_CORE}.svc.cluster.local >/dev/null
            initialDelaySeconds: 30
            periodSeconds: 10
            failureThreshold: 12
      volumes:
        - name: tm-ext-dn-tools
          configMap:
            name: tm-ext-dn-tools
EOF
}

write_ext_dn_tools() {
  local td="${CONFIG_DIR}/ext-dn"
  mkdir -p "${td}"
  cp "${SITL_DIR}/lib/co_tenant_sink.py" "${td}/co_tenant_sink.py" \
    || die "Missing 03_sitl/lib/co_tenant_sink.py"
  cp "${SITL_DIR}/lib/co_tenant_burst_sender.py" "${td}/co_tenant_burst_sender.py" \
    || die "Missing 03_sitl/lib/co_tenant_burst_sender.py"
  kubectl create configmap tm-ext-dn-tools -n "${NS_CORE}" \
    --from-file="${td}/co_tenant_sink.py" \
    --from-file="${td}/co_tenant_burst_sender.py" \
    --dry-run=client -o yaml | kubectl apply -f -
  log "[02] ConfigMap tm-ext-dn-tools (co_tenant_sink.py) applied in ${NS_CORE}"
}

write_tm_c2_pod_tools() {
  local td="${CONFIG_DIR}/tm"
  mkdir -p "${td}"
  cp "${SITL_DIR}/lib/mission_runner.py" "${td}/mission_runner.py" \
    || die "Missing 03_sitl/lib/mission_runner.py"
  cp "${SITL_DIR}/lib/udp_flood.py" "${td}/udp_flood.py" \
    || die "Missing 03_sitl/lib/udp_flood.py"
  cp "${SITL_DIR}/lib/c2_sync_flood.py" "${td}/c2_sync_flood.py" \
    || die "Missing 03_sitl/lib/c2_sync_flood.py"
  for py in trajectory.py freshness_metrics.py setpoint_stream.py mavlink_proxy.py co_tenant_load.py co_tenant_sink.py; do
    cp "${SITL_DIR}/lib/${py}" "${td}/${py}" \
      || die "Missing 03_sitl/lib/${py}"
  done
  cat > "${td}/thresholds.json" <<'JSON'
{"heartbeat_loss_gap_s":1.5,"command_ack_timeout_s":3.0,"command_late_ack_ms":500,"warmup_discard_s":5.0}
JSON
  cat > "${td}/mission_120.json" <<'JSON'
{"name":"tm_aggressive_120s","takeoff_alt_m":10.0,"accept_radius_m":1.5,"steady":{"commands":25,"interval_s":[2.1,5.1],"alt_m":10.0},"waypoints":[{"lat":0.0,"lon":0.0},{"lat":0.0,"lon":0.0011},{"lat":0.0009,"lon":0.0011},{"lat":0.0009,"lon":0.0},{"lat":0.0,"lon":0.0}],"land_after":true}
JSON
  cat > "${td}/mission_420.json" <<'JSON'
{"name":"tm_aggressive_420s","takeoff_alt_m":10.0,"accept_radius_m":1.5,"steady":{"commands":72,"interval_s":[3.4,7.4],"alt_m":10.0},"waypoints":[{"lat":0.0,"lon":0.0},{"lat":0.0,"lon":0.0022},{"lat":0.0018,"lon":0.0022},{"lat":0.0018,"lon":0.0},{"lat":0.0,"lon":0.0},{"lat":0.0,"lon":-0.0022},{"lat":-0.0018,"lon":-0.0022},{"lat":-0.0018,"lon":0.0},{"lat":0.0,"lon":0.0}],"land_after":true}
JSON
  cat > "${td}/tm_common.sh" <<'SH'
#!/bin/bash
tm_pdu_ip() { ip -4 -o addr show 2>/dev/null | awk '/oaitun| oai/{print $4; exit}' | cut -d/ -f1; }
tm_ext_dn_ip() {
  getent ahostsv4 "oai-ext-dn.core.svc.cluster.local" 2>/dev/null | awk '{print $1; exit}'
}
tm_reject_loopback() {
  case "${1:-}" in 127.*|0.0.0.0|::1|localhost*) echo "[ERROR] C2 must use PDU IPs over 5G (UPF/GTP), not $1"; exit 1;; esac
}
tm_require_gcs_pod() {
  tm_pdu_ip >/dev/null || { echo "[ERROR] run from GCS pod (oaitun_ue1 required)"; exit 1; }
}
tm_uav_ip() {
  local ip="${1:-${UAV_TUN:-}}"
  [[ -n "$ip" ]] || { echo "Usage: $0 <UAV_PDU_IP>  (or export UAV_TUN)"; exit 1; }
  tm_reject_loopback "$ip"
  echo "$ip"
}
SH
  cat > "${td}/run_mission_120.sh" <<SH
#!/bin/bash
set -euo pipefail; source /tmp/tm_common.sh
tm_require_gcs_pod
GCS_IP="\$(tm_pdu_ip)"; UAV_IP="\$(tm_uav_ip "\${1:-}")"
tm_reject_loopback "\$GCS_IP"
OUT="/tmp/mission_120_\$(date +%Y%m%d_%H%M%S)"; mkdir -p "\$OUT"; export PYTHONUNBUFFERED=1
echo "[GCS] C2 over 5G: \${GCS_IP} -> ${C2_CONN_PREFIX}://\${UAV_IP}:${SITL_TCP_PORT} (UPF/GTP)"
python3 -u /tmp/mission_runner.py --connection "${C2_CONN_PREFIX}:\${UAV_IP}:${SITL_TCP_PORT}" --bind-ip "\${GCS_IP}" \\
  --mission /tmp/mission_120.json --thresholds /tmp/thresholds.json --out "\$OUT" 2>&1 | tee "\$OUT/mission.log"
echo "[OK] \$OUT/mission_summary.json"
SH
  cat > "${td}/run_mission_420.sh" <<SH
#!/bin/bash
set -euo pipefail; source /tmp/tm_common.sh
tm_require_gcs_pod
GCS_IP="\$(tm_pdu_ip)"; UAV_IP="\$(tm_uav_ip "\${1:-}")"
tm_reject_loopback "\$GCS_IP"
OUT="/tmp/mission_420_\$(date +%Y%m%d_%H%M%S)"; mkdir -p "\$OUT"; export PYTHONUNBUFFERED=1
echo "[GCS] C2 over 5G: \${GCS_IP} -> ${C2_CONN_PREFIX}://\${UAV_IP}:${SITL_TCP_PORT} (UPF/GTP)"
python3 -u /tmp/mission_runner.py --connection "${C2_CONN_PREFIX}:\${UAV_IP}:${SITL_TCP_PORT}" --bind-ip "\${GCS_IP}" \\
  --mission /tmp/mission_420.json --thresholds /tmp/thresholds.json --out "\$OUT" 2>&1 | tee "\$OUT/mission.log"
echo "[OK] \$OUT/mission_summary.json"
SH
  cat > "${td}/check_c2_5g.sh" <<SH
#!/bin/bash
set -euo pipefail; source /tmp/tm_common.sh
tm_require_gcs_pod
GCS="\$(tm_pdu_ip)"; UAV="\$(tm_uav_ip "\${1:-}")"; PORT="${SITL_TCP_PORT}"
# Do NOT use nc here — a connect/disconnect aborts ArduPilot SITL cold-start on SERIAL0.
echo "[GCS] C2 MAVLink probe over 5G: \${GCS} -> ${C2_CONN_PREFIX}://\${UAV}:\${PORT} (UPF/GTP)"
python3 -u /tmp/mission_runner.py --probe --probe-timeout 90 \\
  --connection "${C2_CONN_PREFIX}:\${UAV}:\${PORT}" --bind-ip "\${GCS}" --out /tmp/c2_probe_\$\$
echo "[GCS] C2 path OK over 5G: \${GCS} -> ${C2_CONN_PREFIX}://\${UAV}:\${PORT}"
SH
  cat > "${td}/check_sitl_listen.sh" <<SH
#!/bin/bash
set -euo pipefail; source /tmp/tm_common.sh
IP="\$(tm_pdu_ip)"; PORT="${SITL_TCP_PORT}"
ss -uln 2>/dev/null | grep -q ":\${PORT} " \\
  && echo "[UAV] SITL UDP listening :\${PORT} on PDU \${IP} (netns; relay -> TCP ${SITL_INTERNAL_PORT})" && exit 0
echo "[UAV] SITL not listening — wait for bootstrap or see sitl-uav.log"; exit 1
SH
  cat > "${td}/rogue_pulse.py" <<'PY'
#!/usr/bin/env python3
import socket,sys,time,threading
src,dst,port=sys.argv[1],sys.argv[2],int(sys.argv[3]); dur=float(sys.argv[4]); bolus_kb=int(sys.argv[5])
period=float(sys.argv[6])/1000.0; pkt=int(sys.argv[7]); flows=max(1,int(sys.argv[8]))
payload=b"\x00"*pkt; per_flow=max(1,(bolus_kb*1024)//max(1,pkt)//flows); end=time.monotonic()+dur
barrier=threading.Barrier(flows); stats=[0]*flows
def worker(idx):
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind((src,0)); sent=0
 while time.monotonic()<end:
  try: barrier.wait(timeout=5)
  except threading.BrokenBarrierError: break
  t0=time.monotonic()
  for _ in range(per_flow):
   try: s.sendto(payload,(dst,port)); sent+=1
   except OSError: time.sleep(0.0002)
  if idx==0:
   rem=period-(time.monotonic()-t0)
   if rem>0: time.sleep(rem)
 stats[idx]=sent
ts=[threading.Thread(target=worker,args=(i,)) for i in range(flows)]
[t.start() for t in ts]; [t.join() for t in ts]
print("pulse done packets=%d dur=%.0fs"%(sum(stats),dur))
PY
  cat > "${td}/run_attack_pulse.sh" <<'SH'
#!/bin/bash
set -euo pipefail; source /tmp/tm_common.sh
DUR="${1:-190}"; shift||true; UAV_IP="${1:-}"; ROGUE="$(tm_pdu_ip)"; EXT="$(tm_ext_dn_ip)"
if [[ -z "${EXT}" ]]; then
  echo "[ROGUE] ERROR: cannot resolve oai-ext-dn.core.svc.cluster.local (apply headless Service oai-ext-dn in core)"
  exit 1
fi
echo "[ROGUE] pulse ${DUR}s rogue=${ROGUE} -> ext-dn ${EXT}:5201 (target C2 path ${UAV_IP:-any})"
python3 /tmp/rogue_pulse.py "$ROGUE" "$EXT" 5201 "$DUR" 192 60 1200 1 | tee "/tmp/attack_pulse_${DUR}s.log"
SH
  cat > "${td}/run_attack_cbr.sh" <<'SH'
#!/bin/bash
set -euo pipefail; source /tmp/tm_common.sh
DUR="${1:-190}"; MBPS="${2:-50}"; ROGUE="$(tm_pdu_ip)"; EXT="$(tm_ext_dn_ip)"
if [[ -z "${EXT}" ]]; then
  echo "[ROGUE] ERROR: cannot resolve oai-ext-dn.core.svc.cluster.local (apply headless Service oai-ext-dn in core)"
  exit 1
fi
iperf3 -c "$EXT" -u -b "${MBPS}M" -t "$DUR" -B "$ROGUE" | tee "/tmp/attack_cbr_${DUR}s.log"
SH
  cat > "${td}/run_attack_flood_c2.sh" <<'SH'
#!/bin/bash
set -euo pipefail; source /tmp/tm_common.sh
DUR="${1:-190}"; PROFILE="${2:-moderate}"; UAV_IP="${3:-}"; PORT="${4:-5760}"
ROGUE="$(tm_pdu_ip)"
[[ -n "${UAV_IP}" ]] || { echo "Usage: $0 DUR [gentle|moderate|aggressive] UAV_PDU_IP [port]"; exit 1; }
echo "[ROGUE] flood-c2 ${DUR}s profile=${PROFILE} rogue=${ROGUE} -> ${UAV_IP}:${PORT} (UE-to-UE C2 cross-traffic)"
python3 /tmp/udp_flood.py "${PROFILE}" --target "${UAV_IP}" --port "${PORT}" --bind "${ROGUE}" \
  --duration "${DUR}" | tee "/tmp/attack_flood_c2_${DUR}s.log"
SH
  cat > "${td}/owd_send.py" <<'PY'
#!/usr/bin/env python3
import socket, struct, sys, time
uav, port, dur, bind = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind((bind, 0))
end, seq = time.monotonic() + dur, 0
while time.monotonic() < end:
    s.sendto(struct.pack("!QQ", time.monotonic_ns(), seq), (uav, port))
    seq += 1
    time.sleep(0.001)
print("owd_send done packets=%d dur=%.0fs" % (seq, dur))
PY
  cat > "${td}/owd_recv.py" <<'PY'
#!/usr/bin/env python3
import json, socket, struct, sys, time
port, dur, warmup = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
out = sys.argv[4] if len(sys.argv) > 4 else "/tmp/owd-ipdv-probe.json"
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", port))
s.settimeout(1.0)
end = time.monotonic() + dur + 3
samples = []
while time.monotonic() < end:
    try:
        data, _ = s.recvfrom(64)
        if len(data) < 16:
            continue
        tx, seq = struct.unpack("!QQ", data[:16])
        rx = time.monotonic_ns()
        samples.append({"seq": int(seq), "owd_us": (rx - tx) / 1000.0, "t_mono": time.monotonic()})
    except socket.timeout:
        pass
drop = int(warmup * 1000)
post = samples[drop:] if len(samples) > drop else []
owds = sorted(s["owd_us"] for s in post)
ipdv = sorted(abs(post[i]["owd_us"] - post[i - 1]["owd_us"]) for i in range(1, len(post)))
def pct(v, p):
    if not v: return None
    return v[min(len(v) - 1, int(round((p / 100.0) * (len(v) - 1))))]
report = {
    "clock_methodology": "CLOCK_MONOTONIC GCS tx_ns in UDP; OWD = rx_uav - tx_gcs (GCS to UAV leg via PDU/5G)",
    "duration_s": dur, "warmup_discarded_s": warmup, "samples": len(post),
    "owd_us": {"min": owds[0] if owds else None, "median": pct(owds, 50), "p99": pct(owds, 99),
               "p99_9": pct(owds, 99.9), "max": owds[-1] if owds else None},
    "ipdv_us": {"median": pct(ipdv, 50), "p99": pct(ipdv, 99), "p99_9": pct(ipdv, 99.9),
                "max": ipdv[-1] if ipdv else None},
    "availability": {"packets_received_post_warmup": len(post),
                     "expected_sent_post_warmup": max(1, int((dur - warmup) * 1000))},
}
open(out, "w", encoding="utf-8").write(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
print("Wrote", out)
PY
  cat > "${td}/run_owd_recv.sh" <<'SH'
#!/bin/bash
set -euo pipefail
DUR="${1:-60}"; WARM="${2:-15}"; OUT="/tmp/owd-ipdv-probe.json"
echo "[UAV] OWD/IPDV receiver ${DUR}s (warmup ${WARM}s) -> ${OUT}"
python3 -u /tmp/owd_recv.py 47999 "$DUR" "$WARM" "$OUT" | tee "/tmp/owd_probe_${DUR}s.log"
SH
  cat > "${td}/run_owd_send.sh" <<'SH'
#!/bin/bash
set -euo pipefail; source /tmp/tm_common.sh
tm_require_gcs_pod
UAV="$(tm_uav_ip "${1:-}")"; DUR="${2:-60}"; GCS="$(tm_pdu_ip)"
echo "[GCS] OWD sender ${DUR}s ${GCS} -> ${UAV}:47999 (via 5G)"
python3 -u /tmp/owd_send.py "$UAV" 47999 "$DUR" "$GCS"
SH
  chmod +x "${td}"/*.sh "${td}/rogue_pulse.py" "${td}/udp_flood.py" "${td}/c2_sync_flood.py" "${td}/owd_send.py" "${td}/owd_recv.py" 2>/dev/null || true
  log "[FlyBlind] C2 pod tools → ${td}/"
}

# tm-c2-tools is ~260KB — do not inline in oai-ran.yaml: kubectl apply stores
# last-applied-configuration in metadata.annotations (max 262144 bytes).
apply_tm_c2_tools_configmap() {
  local td="${CONFIG_DIR}/tm" f
  [[ -d "${td}" ]] || die "write_tm_c2_pod_tools missing"
  local -a from_args=()
  for f in mission_runner.py thresholds.json mission_120.json mission_420.json tm_common.sh \
    run_mission_120.sh run_mission_420.sh check_c2_5g.sh check_sitl_listen.sh \
    owd_send.py owd_recv.py run_owd_send.sh run_owd_recv.sh \
    rogue_pulse.py run_attack_pulse.sh run_attack_cbr.sh udp_flood.py run_attack_flood_c2.sh c2_sync_flood.py \
    trajectory.py freshness_metrics.py setpoint_stream.py mavlink_proxy.py co_tenant_load.py; do
    [[ -f "${td}/${f}" ]] || die "Missing ${td}/${f}"
    from_args+=(--from-file="${f}=${td}/${f}")
  done
  kubectl delete configmap tm-c2-tools -n "${NS_RAN}" --ignore-not-found
  kubectl create configmap tm-c2-tools -n "${NS_RAN}" "${from_args[@]}"
  log "[04] ConfigMap tm-c2-tools applied in ${NS_RAN}"
}

write_oai_ran_manifest() {
  : > "${CONFIG_DIR}/oai-ran.yaml"

  local role app_label
  for role in "${UE_ROLES[@]}"; do
    app_label="oai-ue-${role}"
    cat >> "${CONFIG_DIR}/oai-ran.yaml" <<EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${app_label}-config
  namespace: ${NS_RAN}
data:
  nr-ue-${role}.conf: |
$(sed 's/^/    /' "${CONFIG_DIR}/nr-ue-${role}.conf")
  ue-entrypoint.sh: |
$(sed 's/^/    /' "${CONFIG_DIR}/ue-entrypoint.sh")
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${app_label}
  namespace: ${NS_RAN}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${app_label}
  template:
    metadata:
      labels:
        app: ${app_label}
    spec:
      initContainers:
        - name: wait-gnb-rfsim
          image: ${BUSYBOX_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - |
              until nc -z ${GNB_RFSIM_HOST} 4043; do
                echo "[wait-gnb] waiting for gNB RFsim port 4043..."
                sleep 3
              done
      containers:
        - name: nr-ue
          image: ${OAI_UE_IMAGE}
          imagePullPolicy: IfNotPresent
          env:
            - name: UE_ROLE
              value: "${role}"
            - name: GNB_RFSIM_HOST
              value: "${GNB_RFSIM_HOST}"
            - name: CORE_PIN
              value: "$(core_pin_for_role "${role}")"
$(if is_rogue_role "${role}"; then echo "            - name: ATTACK_CORE_PIN
              value: \"${PIN_ATTACK}\""; fi)
          securityContext:
            privileged: true
            capabilities:
              add: ["NET_ADMIN", "NET_RAW", "SYS_NICE"]
          command: ["/bin/bash", "/scripts/ue-entrypoint.sh"]
          volumeMounts:
            - name: ue-config
              mountPath: /scripts/ue-entrypoint.sh
              subPath: ue-entrypoint.sh
            - name: ue-config
              mountPath: /config/nr-ue-${role}.conf
              subPath: nr-ue-${role}.conf
            - name: dev-net-tun
              mountPath: /dev/net/tun
$(if [[ "${role}" == "gcs" || "${role}" == "uav" ]] || is_rogue_role "${role}"; then echo "            - name: tm-tools
              mountPath: /config/tm-tools"; fi)
          readinessProbe:
            exec:
              command: ["/bin/bash", "-c", "ps aux | grep -v grep | grep -c nr-uesoftmodem"]
            initialDelaySeconds: 30
            periodSeconds: 10
            failureThreshold: 30
$(if [[ "${role}" == "uav" ]]; then cat <<SITLSC

        - name: sitl
          image: ${SITL_IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["/bin/bash", "-c"]
          args:
            - |
              set -euo pipefail
              AP="${VENDOR_DIR}/ardupilot"
              COPTER="\${AP}/build/sitl/bin/arducopter"
              DEFAULTS="\${AP}/Tools/autotest/default_params/copter.parm"
              for _i in \$(seq 1 180); do
                [[ -x "\${COPTER}" ]] && break
                echo "[sitl] waiting for \${COPTER} (host build via start_testbed)..."
                sleep 2
              done
              [[ -x "\${COPTER}" ]] || { echo "[sitl] FATAL: \${COPTER} missing on Kind hostPath"; exit 1; }
              for _i in \$(seq 1 180); do
                grep -q 'oaitun_ue1:' /proc/net/dev 2>/dev/null && break
                echo "[sitl] waiting for oaitun_ue1 (nr-ue attach)..."
                sleep 2
              done
              mkdir -p /tmp/tm-sitl && cd /tmp/tm-sitl
              echo "[sitl] UDP C2: socat UDP :${SITL_TCP_PORT} -> TCP 127.0.0.1:${SITL_INTERNAL_PORT} (internal SITL backend)"
              command -v socat >/dev/null 2>&1 || {
                apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq socat
              }
              socat UDP4-LISTEN:${SITL_TCP_PORT},reuseaddr,fork TCP:127.0.0.1:${SITL_INTERNAL_PORT} &
              if [[ "${ACK_PROBE_SOCAT}" == "1" ]]; then
                echo "[sitl] ACK probe relay (default ON): socat UDP :${ACK_PROBE_UDP_PORT} -> TCP 127.0.0.1:${ACK_PROBE_SITL_TCP} (SITL secondary GCS; C2 :${SITL_TCP_PORT} untouched)"
                socat UDP4-LISTEN:${ACK_PROBE_UDP_PORT},reuseaddr,fork TCP:127.0.0.1:${ACK_PROBE_SITL_TCP} &
              fi
              sleep 1
              # FlyBlind overlay: GCS failsafe ON (cyber-physical). mission_runner keeps
              # GCS HEARTBEAT ~1 Hz so warm-up/climb gaps do not spuriously RTL.
              FS_PARM="/tmp/tm_gcs_failsafe.parm"
              printf '%s\n' \
                '# FlyBlind overlay — GCS link failsafe (cyber-physical outcome metric).' \
                'FS_GCS_ENABLE      1' \
                'FS_GCS_TIMEOUT    5' \
                > "\${FS_PARM}"
              echo "[sitl] arducopter --serial0=${SITL_SERIAL_ARG} defaults=\${DEFAULTS},\${FS_PARM} (GCS failsafe ON, timeout 5s)"
              exec "\${COPTER}" --model + --speedup 1 \\
                --home 22.689730,114.023910,30,0 \\
                --serial0=${SITL_SERIAL_ARG} \\
                --defaults "\${DEFAULTS},\${FS_PARM}"
          volumeMounts:
            - name: ardupilot-vendor
              mountPath: ${VENDOR_DIR}
              readOnly: true
          securityContext:
            capabilities:
              add: ["NET_BIND_SERVICE"]
SITLSC
fi)
      volumes:
        - name: ue-config
          configMap:
            name: ${app_label}-config
            defaultMode: 0755
$(if [[ "${role}" == "gcs" || "${role}" == "uav" ]] || is_rogue_role "${role}"; then echo "        - name: tm-tools
          configMap:
            name: tm-c2-tools
            defaultMode: 0755"; fi)
$(if [[ "${role}" == "uav" ]]; then echo "        - name: ardupilot-vendor
          hostPath:
            path: ${VENDOR_DIR}
            type: Directory"; fi)
        - name: dev-net-tun
          hostPath:
            path: /dev/net/tun
            type: CharDevice
EOF
  done

  cat >> "${CONFIG_DIR}/oai-ran.yaml" <<EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: oai-gnb-config
  namespace: ${NS_RAN}
data:
  gnb-template.yaml: |
$(sed 's/^/    /' "${CONFIG_DIR}/gnb-template.yaml")
  gnb-entrypoint.sh: |
$(sed 's/^/    /' "${CONFIG_DIR}/gnb-entrypoint.sh")
  rrmPolicy-template.json: |
$(sed 's/^/    /' "${CONFIG_DIR}/rrmPolicy-template.json")
---
apiVersion: v1
kind: Service
metadata:
  name: oai-gnb
  namespace: ${NS_RAN}
spec:
  selector:
    app: oai-gnb
  ports:
    - name: rfsim
      port: 4043
      targetPort: 4043
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oai-gnb
  namespace: ${NS_RAN}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oai-gnb
  template:
    metadata:
      labels:
        app: oai-gnb
    spec:
      initContainers:
        - name: wait-amf-ngap
          image: ${BUSYBOX_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - |
              until nslookup ${AMF_NGAP_HOST} >/dev/null 2>&1; do
                echo "[wait-amf] waiting for AMF NGAP DNS..."
                sleep 3
              done
              echo "[wait-amf] AMF NGAP resolvable; settling 20s..."
              sleep 20
        - name: wait-ext-dn
          image: ${BUSYBOX_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - |
              until nc -zu -w2 oai-upf.${NS_CORE}.svc.cluster.local 8805; do
                echo "[wait-upf] waiting for UPF N4 (UDP 8805)..."
                sleep 3
              done
              echo "[wait-upf] UPF reachable; settling 10s..."
              sleep 10
      containers:
        - name: gnb
          image: ${OAI_GNB_IMAGE}
          imagePullPolicy: IfNotPresent
          env:
            - name: AMF_HOSTNAME
              value: "${AMF_NGAP_HOST}"
            - name: MIN_RATIO_SLICE_A
              value: "${MIN_RATIO_SLICE_A}"
            - name: MIN_RATIO_SLICE_B
              value: "${MIN_RATIO_SLICE_B}"
            - name: TOPOLOGY
              value: "${TOPOLOGY}"
            - name: GNB_MAC_LOG
              value: "${GNB_MAC_LOG:-info}"
            - name: CORE_PIN
              value: "${PIN_GNB}"
          securityContext:
            capabilities:
              add: ["SYS_NICE"]
          command: ["/bin/bash", "/scripts/gnb-entrypoint.sh"]
          volumeMounts:
            - name: gnb-config
              mountPath: /scripts/gnb-entrypoint.sh
              subPath: gnb-entrypoint.sh
            - name: gnb-config
              mountPath: /config/gnb-template.yaml
              subPath: gnb-template.yaml
            - name: gnb-config
              mountPath: /config/rrmPolicy-template.json
              subPath: rrmPolicy-template.json
          readinessProbe:
            exec:
              command: ["/bin/bash", "-c", "ps aux | grep -v grep | grep -c nr-softmodem"]
            initialDelaySeconds: 30
            periodSeconds: 10
            failureThreshold: 30
      volumes:
        - name: gnb-config
          configMap:
            name: oai-gnb-config
            defaultMode: 0755
EOF
}

prepare_oai_artifacts() {
  write_tm_c2_pod_tools
  write_ext_dn_tools
  validate_pin_core_layout
  if ! artifacts_need_prepare; then
    log "[02] Artifacts up-to-date — refreshing derived values, templates, and manifests."
    sync_slice_params_from_cn_config
    parse_gnb_ran_params
    patch_cn_config_for_k8s
    write_rrm_policy_template
    write_entrypoints
    write_gnb_template
    for role in "${UE_ROLES[@]}"; do
      read -r sst _ sd_hex dnn _ <<< "$(ue_slice_profile "${role}")"
      write_ue_uicc_conf "${role}" "$(ue_role_imsi "${role}")" "${dnn}" "${sst}" "${sd_hex}"
    done
    write_oai_core_manifest
    write_oai_ran_manifest
    write_topology_stamp
    return 0
  fi

  fetch_oai_artifacts
  patch_gnb_rfsim_prb
  sync_slice_params_from_cn_config
  ensure_testbed_subscribers_in_db
  parse_gnb_ran_params
  patch_cn_config_for_k8s
  write_rrm_policy_template
  write_entrypoints
  write_gnb_template
  for role in "${UE_ROLES[@]}"; do
    read -r sst _ sd_hex dnn _ <<< "$(ue_slice_profile "${role}")"
    write_ue_uicc_conf "${role}" "$(ue_role_imsi "${role}")" "${dnn}" "${sst}" "${sd_hex}"
  done
  write_oai_core_manifest
  write_oai_ran_manifest
  mark_artifacts_prepared
  write_topology_stamp
  log "[02] Rendered ${CONFIG_DIR}/oai-core.yaml and oai-ran.yaml"
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    log "[02] topology=intra | 1 slice: SST=${SST} SD=${SD} DNN=${DNN} (all UEs)"
  elif [[ "${VICTIM_IN_EMBB}" == "1" ]]; then
    log "[02] topology=cross | slice A: SST=${SST} SD=${SD} DNN=${DNN} (GCS/UAV + ${ROGUE_WORD}, co-located)"
    log "[02]              slice B: SST=${SST_B} SD=${SD_B} DNN=${DNN_B} (idle, no UE)"
  else
    log "[02] topology=cross | slice A: SST=${SST} SD=${SD} DNN=${DNN} (ROGUE)"
    log "[02]              slice B: SST=${SST_B} SD=${SD_B} DNN=${DNN_B} (C2)"
  fi
  log "[02] UEs: ROGUE=${IMSI_ROGUE} GCS=${IMSI_GCS} UAV=${IMSI_UAV}"
}

# =============================================================================
# 03 — Deploy OAI 5G SA core (MySQL + AMF + SMF + UPF + ext-DN)
# =============================================================================
deploy_oai_core() {
  log "[03] Applying OAI core manifests..."
  apply_mysql_init_configmap
  kubectl apply -f "${CONFIG_DIR}/oai-core.yaml"
  wait_pods_watch "${NS_CORE}"
  log "[pause] 30s for MySQL init + AMF/SMF/UPF startup..."
  sleep 30
}

verify_oai_core_health() {
  log "[03] Sanity check — OAI CN rollouts..."
  kubectl rollout status -n "${NS_CORE}" deploy/oai-mysql --timeout=180s \
    || { dump_oai_diagnostics; die "oai-mysql not ready."; }
  kubectl rollout status -n "${NS_CORE}" deploy/oai-amf --timeout=180s \
    || { dump_oai_diagnostics; die "oai-amf not ready."; }
  kubectl rollout status -n "${NS_CORE}" deploy/oai-smf --timeout=180s \
    || { dump_oai_diagnostics; die "oai-smf not ready."; }
  kubectl rollout status -n "${NS_CORE}" deploy/oai-upf --timeout=180s \
    || { dump_oai_diagnostics; die "oai-upf not ready."; }
  kubectl rollout status -n "${NS_CORE}" deploy/oai-ext-dn --timeout=120s \
    || { dump_oai_diagnostics; die "oai-ext-dn not ready."; }
  apply_l3_slice_isolation
  log "[OK] OAI core healthy."
  log "[03] AMF NGAP (headless): ${AMF_NGAP_HOST}:38412"
  if ! kubectl exec -n "${NS_CORE}" deploy/oai-mysql -c mysql -- \
    mysql -utest -ptest oai_db -N -e \
    "SHOW TABLES LIKE 'SessionManagementSubscriptionData';" 2>/dev/null \
    | grep -q SessionManagementSubscriptionData; then
    die "oai-mysql: cn5g SessionManagementSubscriptionData missing — check oai_db.sql init. Run: ./stop_testbed.sh && ./start_testbed_mitigation.sh"
  fi
  log "[03] MySQL cn5g schema OK (SessionManagementSubscriptionData)"
  local users_count imsi_sql="" imsi
  while IFS= read -r imsi; do
    [[ -n "${imsi}" ]] || continue
    imsi_sql+="'${imsi}',"
  done < <(all_testbed_imsis)
  imsi_sql="${imsi_sql%,}"
  users_count="$(kubectl exec -n "${NS_CORE}" deploy/oai-mysql -c mysql -- \
    mysql -utest -ptest oai_db -N -e \
    "SELECT COUNT(*) FROM users WHERE imsi IN (${imsi_sql});" \
    2>/dev/null | tr -d '\r' || echo 0)"
  [[ "${users_count}" -eq "${EXPECTED_UE_COUNT}" ]] \
    || die "oai-mysql: AMF legacy users table missing testbed IMSIs (found ${users_count}/${EXPECTED_UE_COUNT}). Run: ./stop_testbed.sh && ./start_testbed_mitigation.sh"
  log "[03] MySQL AMF auth OK (users table: ${users_count}/${EXPECTED_UE_COUNT} testbed IMSIs)"
}

ensure_core_for_ran() {
  kubectl rollout status -n "${NS_CORE}" deploy/oai-amf --timeout=60s \
    || die "OAI AMF not ready in ${NS_CORE}. Run --phase core first."
  kubectl rollout status -n "${NS_CORE}" deploy/oai-ext-dn --timeout=60s \
    || die "OAI ext-DN not ready in ${NS_CORE}. Run --phase core first."
}

# =============================================================================
# 04 — Deploy OAI RAN (1 gNB + 3 NR-UE over RFsimulator)
# =============================================================================
deploy_oai_ran() {
  log "[04] Applying OAI RAN manifests (gNB + ${EXPECTED_UE_COUNT} ${NR_UE_WORD}: ${ROGUE_WORD}×${ROGUE_COUNT} GCS UAV)..."
  apply_tm_c2_tools_configmap
  kubectl apply -f "${CONFIG_DIR}/oai-ran.yaml"
  wait_pods_watch "${NS_RAN}" "app=oai-gnb"
  log "[pause] 15s after gNB ready before UE attach..."
  sleep 15
  for role in "${UE_ROLES[@]}"; do
    wait_pods_watch "${NS_RAN}" "app=oai-ue-${role}"
    log "[04] oai-ue-${role} ready; pausing 10s before next UE..."
    sleep 10
  done
  wait_pods_watch "${NS_RAN}"
}

# =============================================================================
# 05 — Sanity check: PDU session (TUN + ping via user plane)
# =============================================================================
# OAI develop names the PDU TUN oaitun_ue1; older rfsim tutorials use "oai".
get_ue_pdu_iface() {
  local deploy="$1"
  kubectl exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- sh -c '
    for n in oai oaitun_ue1 oaitun_ue2 oaitun_ue3; do
      ip -4 addr show dev "$n" 2>/dev/null | grep -q "inet " && { echo "$n"; exit 0; }
    done
    for n in /sys/class/net/oaitun_* /sys/class/net/oai; do
      [[ -e "$n" ]] || continue
      b=$(basename "$n")
      ip -4 addr show dev "$b" 2>/dev/null | grep -q "inet " && { echo "$b"; exit 0; }
    done
    exit 1
  ' 2>/dev/null
}

get_ue_pdu_ipv4() {
  local deploy="$1" iface
  iface="$(get_ue_pdu_iface "${deploy}")" || return 1
  kubectl exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- \
    ip -o -4 addr show dev "${iface}" | awk '{print $4}' | cut -d/ -f1
}

get_ext_dn_pod_ip() {
  kubectl get pod -n "${NS_CORE}" -l app=oai-ext-dn \
    -o jsonpath='{.items[0].status.podIP}' 2>/dev/null
}

reset_ext_dn_iperf_servers() {
  kubectl exec -n "${NS_CORE}" deploy/oai-ext-dn -c ext-dn -- \
    sh -c 'killall iperf3 2>/dev/null || true; sleep 0.3; iperf3 -s -D -p 5201; iperf3 -s -D -p 5202; ss -ltn | grep -q ":5201 " && ss -ltn | grep -q ":5202 "' \
    || die "failed to reset iperf3 servers on oai-ext-dn (:5201 + :5202)"
}

ensure_ext_dn_iperf_server() {
  local ip
  ip="$(get_ext_dn_pod_ip)"
  [[ -n "${ip}" ]] || die "oai-ext-dn pod IP not found for iperf smoke"
  if [[ "${RESET_EXT_DN_IPERF:-0}" == "1" ]]; then
    reset_ext_dn_iperf_servers
  else
    kubectl exec -n "${NS_CORE}" deploy/oai-ext-dn -c ext-dn -- \
      sh -c '
        command -v iperf3 >/dev/null 2>&1 || exit 1
        ss -ltn | grep -q ":5201 " || iperf3 -s -D -p 5201
        ss -ltn | grep -q ":5202 " || iperf3 -s -D -p 5202
      ' || die "failed to ensure iperf3 servers on oai-ext-dn"
  fi
  echo "${ip}"
}

wait_for_ue_oai_iface() {
  local role="$1" timeout="${2:-360}" elapsed=0
  local deploy="oai-ue-${role}" iface="" ue_ip=""
  log "[05] Waiting for PDU TUN (oai / oaitun_ue*) on ${deploy} (timeout ${timeout}s)..."
  while (( elapsed < timeout )); do
    iface="$(get_ue_pdu_iface "${deploy}" || true)"
    if [[ -n "${iface}" ]]; then
      ue_ip="$(get_ue_pdu_ipv4 "${deploy}" || true)"
      log "[05] ${deploy}: ${iface}=${ue_ip} (${elapsed}s)"
      return 0
    fi
    if (( elapsed > 0 && elapsed % 30 == 0 )); then
      log "[05] ${deploy}: still waiting for PDU TUN (oai/oaitun_ue*)... ${elapsed}/${timeout}s"
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  dump_oai_diagnostics
  die "${deploy}: PDU TUN not up. Check: kubectl logs -n ${NS_RAN} deploy/${deploy} -c nr-ue --tail=80"
}

# Ping through oai until success; on failure roll the UE pod and retry (handles slow PDU attach).
verify_ue_internet_connectivity() {
  local role="$1"
  local deploy="oai-ue-${role}"
  local target_ip="8.8.8.8"
  local attempt=0
  local max_attempts=5

  log "--- Verifying user-plane connectivity: ${deploy} (${role}) ---"

  while (( attempt < max_attempts )); do
    ((++attempt))
    iface="$(get_ue_pdu_iface "${deploy}" || true)"
    [[ -n "${iface}" ]] || { wait_for_ue_oai_iface "${role}" 120; iface="$(get_ue_pdu_iface "${deploy}")"; }
    if kubectl exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- \
      ping -I "${iface}" -c 4 -W 3 "${target_ip}"; then
      log "[OK] ${deploy}: ping to ${target_ip} via ${iface} succeeded."
      return 0
    fi
    log "[WARN] ${deploy}: ping failed (attempt ${attempt}/${max_attempts}); restarting deployment..."
    kubectl rollout restart -n "${NS_RAN}" "deployment/${deploy}"
    log "[pause] 60s for ${deploy} restart..."
    sleep 60
    wait_for_ue_oai_iface "${role}" 240
  done

  dump_oai_diagnostics
  die "${deploy}: PDU ping to ${target_ip} failed after ${max_attempts} attempts."
}

read_ue_conf_field() {
  local role="$1" field="$2" deploy="oai-ue-${role}" val=""
  # Prefer mounted ConfigMap (immutable) over runtime nr-ue.conf (OAI may rewrite).
  val="$(kubectl exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- \
    grep -E "${field}[[:space:]]*=" /config/nr-ue-${role}.conf 2>/dev/null \
    | head -n1 | sed -E 's/.*'"${field}"'[[:space:]]*=[[:space:]]*"?([^";]+)"?.*/\1/' || true)"
  [[ -n "${val}" ]] || val="$(kubectl exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- \
    grep -E "${field}[[:space:]]*=" /opt/oai-nr-ue/etc/nr-ue.conf 2>/dev/null \
    | head -n1 | sed -E 's/.*'"${field}"'[[:space:]]*=[[:space:]]*"?([^";]+)"?.*/\1/' || true)"
  echo "${val}"
}

# Smoke: nr-ue.conf dnn/sst/sd + PDU IPv4 in expected DNN pool (config + runtime).
verify_ue_pdu_profile() {
  local role="$1"
  local deploy="oai-ue-${role}"
  local exp_sst exp_sd_hex exp_dnn exp_sd_dec got_sst got_sd got_dnn pdu_ip subnet
  read -r exp_sst _ exp_sd_hex exp_dnn _ <<< "$(ue_slice_profile "${role}")"
  exp_sd_dec="$(python3 -c "print(int('${exp_sd_hex}', 0))")"
  got_dnn="$(read_ue_conf_field "${role}" "dnn")"
  got_sst="$(read_ue_conf_field "${role}" "nssai_sst")"
  got_sd="$(read_ue_conf_field "${role}" "nssai_sd")"
  [[ -n "${got_dnn}" && -n "${got_sst}" && -n "${got_sd}" ]] \
    || die "${deploy}: nr-ue.conf missing dnn/nssai fields (topology=${TOPOLOGY})"
  if [[ "${got_dnn}" != "${exp_dnn}" ]]; then
    die "${deploy}: expected DNN=${exp_dnn}, got '${got_dnn}'"
  fi
  if [[ "${got_sst}" != "${exp_sst}" ]]; then
    die "${deploy}: expected nssai_sst=${exp_sst}, got '${got_sst}'"
  fi
  if [[ "$(python3 -c "print(int('${got_sd}', 0))")" != "${exp_sd_dec}" ]]; then
    die "${deploy}: expected nssai_sd=${exp_sd_hex} (${exp_sd_dec}), got '${got_sd}'"
  fi
  pdu_ip="$(get_ue_pdu_ipv4 "${deploy}")" \
    || die "${deploy}: PDU IPv4 missing for subnet check"
  subnet="$(expected_pdu_subnet "${role}")"
  ip_in_subnet "${pdu_ip}" "${subnet}" \
    || die "${deploy}: PDU IP ${pdu_ip} not in expected pool ${subnet} (role=${role}, topology=${TOPOLOGY})"
  log "[05] ${deploy}: DNN=${got_dnn} SST=${got_sst} SD=${got_sd} PDU=${pdu_ip} ∈ ${subnet} OK"
}

verify_ul_rrmpolicy_binary() {
  [[ "${MITIGATION_UL_RRMPOLICY}" == "1" ]] || return 0
  log "[05] Gate mitigation-UL-rrmpolicy — NSSAI-keyed UL scheduler binary"
  kubectl exec -n "${NS_RAN}" deploy/oai-gnb -c gnb -- \
    sh -c "grep -a -qE 'tm_ul_rrmpolicy\\.conf|tm1_ul_rrmpolicy\\.conf' /opt/oai-gnb/bin/nr-softmodem" \
    || die "gNB image lacks FlyBlind UL rrmPolicy scheduler patch (rebuild: ./build_oranslice_images_mitigation.sh)"
  log "[OK] gNB scheduler exposes /tmp/tm_ul_rrmpolicy.conf control (tm1 alias supported)"
}

verify_ue_iperf_smoke() {
  local role="$1"
  local deploy="oai-ue-${role}"
  local target_ip iface ue_ip
  reset_ext_dn_iperf_servers
  target_ip="$(get_ext_dn_pod_ip)"
  [[ -n "${target_ip}" ]] || die "oai-ext-dn pod IP not found for iperf smoke"
  iface="$(get_ue_pdu_iface "${deploy}")" || return 1
  ue_ip="$(get_ue_pdu_ipv4 "${deploy}")" || die "${deploy}: no PDU IPv4 for iperf smoke"
  kubectl exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- \
    sh -c 'pkill -f "[i]perf3" 2>/dev/null || true'
  if kubectl exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- \
    sh -c 'command -v iperf3 >/dev/null 2>&1'; then
    log "[05] ${deploy}: iperf3 UDP smoke → oai-ext-dn ${target_ip}:5201 (3s)..."
    if kubectl --request-timeout=20s exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- \
      iperf3 -c "${target_ip}" -p 5201 -u -b 512K -t 3 -B "${ue_ip}"; then
      log "[OK] ${deploy}: iperf3 smoke succeeded."
      return 0
    fi
    die "${deploy}: iperf3 smoke to oai-ext-dn (${target_ip}:5201) failed (or timed out after 20s)."
  fi
  log "[05] ${deploy}: iperf3 not in UE image — extended ping smoke (16 pkts)..."
  kubectl exec -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue -- \
    ping -I "${iface}" -c 16 -W 3 "${target_ip}" \
    || die "${deploy}: extended ping smoke failed."
  log "[OK] ${deploy}: ping smoke (iperf3 unavailable) succeeded."
}

# Install iptables DROP A↔B on UPF (+ ext-dn anti-hairpin). Idempotent.
# Does NOT block B↔B (GCS↔UAV) nor traffic to non-PDU destinations (ext-dn CNI).
apply_l3_slice_isolation() {
  [[ "${L3_SLICE_ISOLATION}" == "1" ]] || {
    log "[03] L3 slice isolation DISABLED (L3_SLICE_ISOLATION=${L3_SLICE_ISOLATION})"
    return 0
  }
  [[ "${TOPOLOGY}" == "cross" ]] || return 0

  local sa="${SUBNET_SLICE_A}" sb="${SUBNET_SLICE_B}"
  local gcs_ip="" uav_ip=""
  if [[ "${VICTIM_IN_EMBB}" == "1" ]]; then
    # Victim co-located in slice A: model real-network UE isolation — DROP every UE↔UE pair inside
    # the eMBB subnet, ACCEPT only the legitimate GCS↔UAV C2 pair. Attacker traffic to the ext-dn
    # sink (outside the subnet) is untouched. PDU IPs are dynamic: empty on the early pre-attach
    # call (→ DROP-only), filled on the post-attach re-apply that adds the C2 exceptions.
    gcs_ip="$(get_ue_pdu_ipv4 oai-ue-gcs 2>/dev/null || true)"
    uav_ip="$(get_ue_pdu_ipv4 oai-ue-uav 2>/dev/null || true)"
    log "[03] L3 isolation: UE↔UE DROP in ${sa}, allow GCS↔UAV (${gcs_ip:-?}↔${uav_ip:-?}) on UPF (+ ext-dn)..."
  else
    log "[03] L3 isolation: DROP ${sa} ↔ ${sb} on UPF (+ ext-dn anti-hairpin)..."
  fi

  # Remote pods often have dash as /bin/sh — no pipefail. Keep POSIX (set -eu only).
  local script
  script="$(cat <<EOF
set -eu
MODE="${VICTIM_IN_EMBB}"
SA="${sa}"
SB="${sb}"
GCS="${gcs_ip}"
UAV="${uav_ip}"
CHAIN="TM_L3_ISO"
if ! command -v iptables >/dev/null 2>&1; then
  echo "ERROR: iptables missing"
  exit 1
fi
iptables -N "\$CHAIN" 2>/dev/null || iptables -F "\$CHAIN"
# Flush then re-add (idempotent across re-applies).
iptables -F "\$CHAIN"
if [ "\$MODE" = "1" ]; then
  # Co-located: allow only the C2 pair, then isolate the rest of the eMBB subnet from itself.
  if [ -n "\$GCS" ] && [ -n "\$UAV" ]; then
    iptables -A "\$CHAIN" -s "\$GCS" -d "\$UAV" -j ACCEPT -m comment --comment "tm-c2-gcs-uav"
    iptables -A "\$CHAIN" -s "\$UAV" -d "\$GCS" -j ACCEPT -m comment --comment "tm-c2-uav-gcs"
  fi
  iptables -A "\$CHAIN" -s "\$SA" -d "\$SA" -j DROP -m comment --comment "tm-drop-ue-to-ue-embb"
else
  iptables -A "\$CHAIN" -s "\$SA" -d "\$SB" -j DROP -m comment --comment "tm-drop-A-to-B"
  iptables -A "\$CHAIN" -s "\$SB" -d "\$SA" -j DROP -m comment --comment "tm-drop-B-to-A"
fi
# Jump from filter hooks if not already present.
for hook in FORWARD INPUT OUTPUT; do
  iptables -C "\$hook" -j "\$CHAIN" 2>/dev/null \\
    || iptables -I "\$hook" 1 -j "\$CHAIN"
done
echo "OK chain=\$CHAIN mode=\$MODE"
iptables -S "\$CHAIN"
EOF
)"

  kubectl exec -n "${NS_CORE}" deploy/oai-upf -c upf -- sh -c "${script}" \
    || die "failed to apply L3 DROP on oai-upf (NET_ADMIN/iptables)"
  # Hairpin via N6: same DROP on ext-dn so A↛B even if UPF forwards to DN.
  kubectl exec -n "${NS_CORE}" deploy/oai-ext-dn -c ext-dn -- sh -c "${script}" \
    || die "failed to apply L3 DROP on oai-ext-dn (anti-hairpin)"

  log "[OK] L3 isolation applied — ${ROGUE_WORD} must NOT reach C2 PDU IPs"
}

# Cross (FlyBlind): RAN slice boundary + L3 DROP A↔B (PRB contention only; no path to victim).
verify_cross_slice_isolation() {
  [[ "${TOPOLOGY}" == "cross" ]] || return 0

  log "[05] Cross slice isolation (FlyBlind): RAN S-NSSAI + L3 DROP A↔B (path to victim must be dead)..."

  local rogue_sst rogue_sd_hex rogue_dnn gcs_sst gcs_sd_hex gcs_dnn
  local rogue_ip gcs_ip uav_ip rogue_iface gcs_iface

  read -r rogue_sst _ rogue_sd_hex rogue_dnn _ <<< "$(ue_slice_profile rogue)"
  read -r gcs_sst _ gcs_sd_hex gcs_dnn _ <<< "$(ue_slice_profile gcs)"

  rogue_ip="$(get_ue_pdu_ipv4 oai-ue-rogue)" \
    || die "cross slice isolation: ROGUE PDU IP missing"
  gcs_ip="$(get_ue_pdu_ipv4 oai-ue-gcs)" \
    || die "cross slice isolation: GCS PDU IP missing"
  uav_ip="$(get_ue_pdu_ipv4 oai-ue-uav)" \
    || die "cross slice isolation: UAV PDU IP missing"

  local gcs_pool uav_pool
  gcs_pool="$(expected_pdu_subnet gcs)"
  uav_pool="$(expected_pdu_subnet uav)"
  ip_in_subnet "${rogue_ip}" "${SUBNET_SLICE_A}" \
    || die "ROGUE PDU ${rogue_ip} not in slice-A pool ${SUBNET_SLICE_A}"
  ip_in_subnet "${gcs_ip}" "${gcs_pool}" \
    || die "GCS PDU ${gcs_ip} not in expected pool ${gcs_pool}"
  ip_in_subnet "${uav_ip}" "${uav_pool}" \
    || die "UAV PDU ${uav_ip} not in expected pool ${uav_pool}"

  if [[ "${VICTIM_IN_EMBB}" == "1" ]]; then
    # Co-located by design: attacker and victim intentionally share the eMBB slice/S-NSSAI. The
    # boundary is enforced at L3 (UE↔UE DROP, C2 pair allowed), proven below, not at the gNB slice.
    [[ "${rogue_dnn}" == "${gcs_dnn}" ]] \
      || die "co-located: ROGUE and C2 must share DNN (got ${rogue_dnn} vs ${gcs_dnn})"
  else
    [[ "${rogue_dnn}" != "${gcs_dnn}" ]] \
      || die "cross: ROGUE and C2 share DNN=${rogue_dnn} (expected ${DNN} vs ${DNN_B})"
    if [[ "${rogue_sst}" == "${gcs_sst}" && "${rogue_sd_hex}" == "${gcs_sd_hex}" ]]; then
      die "cross: ROGUE and C2 share S-NSSAI ${rogue_sst}/${rogue_sd_hex} — slice boundary missing at gNB"
    fi
  fi

  # Re-apply after UE attach (UPF restart / rule loss would otherwise reopen the path).
  apply_l3_slice_isolation

  log "[05] Cross slice OK — ${ROGUE_WORD}: ${rogue_sst}/${rogue_sd_hex} ${rogue_dnn} ${rogue_ip}; C2: ${gcs_sst}/${gcs_sd_hex} ${gcs_dnn} ${gcs_ip}/${uav_ip}"

  rogue_iface="$(get_ue_pdu_iface oai-ue-rogue)" \
    || die "cross L3: ROGUE PDU iface missing"
  gcs_iface="$(get_ue_pdu_iface oai-ue-gcs)" \
    || die "cross L3: GCS PDU iface missing"

  # Negative proof (required): ROGUE must NOT reach GCS or UAV.
  if [[ "${L3_SLICE_ISOLATION}" == "1" ]]; then
    if kubectl exec -n "${NS_RAN}" deploy/oai-ue-rogue -c nr-ue -- \
      ping -I "${rogue_iface}" -c 2 -W 2 "${gcs_ip}" >/dev/null 2>&1; then
      die "L3 ISOLATION FAILED: ROGUE→GCS ${gcs_ip} still reachable (path open — DROP missing or UPF datapath bypasses iptables)"
    fi
    if kubectl exec -n "${NS_RAN}" deploy/oai-ue-rogue -c nr-ue -- \
      ping -I "${rogue_iface}" -c 2 -W 2 "${uav_ip}" >/dev/null 2>&1; then
      die "L3 ISOLATION FAILED: ROGUE→UAV ${uav_ip} still reachable (path open — DROP missing or UPF datapath bypasses iptables)"
    fi
    log "[OK] L3 negative proof: ${ROGUE_WORD}↛GCS and ${ROGUE_WORD}↛UAV (path dead)"
  else
    log "[05] WARN: L3_SLICE_ISOLATION=0 — skipping negative path proof"
  fi

  # Positive proof: legitimate GCS↔UAV C2 (same slice B) still works.
  if ! kubectl exec -n "${NS_RAN}" deploy/oai-ue-gcs -c nr-ue -- \
    ping -I "${gcs_iface}" -c 2 -W 2 "${uav_ip}" >/dev/null 2>&1; then
    die "L3 ISOLATION BROKE C2: GCS→UAV ${uav_ip} unreachable (B↔B must work)"
  fi
  log "[OK] L3 positive proof: GCS→UAV still reachable (same-slice C2 OK)"
}

# Final gate (end of start_testbed_mitigation): prove slices are L3-separated and radio path still works.
# FAIL → die (testbed must not be considered up).
run_final_l3_slice_separation_test() {
  [[ "${TOPOLOGY}" == "cross" ]] || {
    log "[final-L3] skipped (topology=${TOPOLOGY}; only cross has A↔B isolation)"
    return 0
  }

  log "========================================"
  log " FINAL TEST — L3 slice separation"
  log " Expect: ${ROGUE_WORD}↛GCS/UAV ; GCS↔UAV OK ; ${ROGUE_WORD}→ext-dn OK"
  log "========================================"

  apply_l3_slice_isolation

  local rogue_ip gcs_ip uav_ip ext_ip rogue_iface gcs_iface
  local fail=0
  local r_gcs="FAIL" r_uav="FAIL" r_c2port="FAIL" r_b2b="FAIL" r_ext="FAIL"
  local why_gcs="ping succeeded — slices not separated"
  local why_uav="ping succeeded — slices not separated"
  local why_c2port="UAV host still reachable — C2 port :5760 not proven dead"
  local why_b2b="same-slice C2 unreachable"
  local why_ext="attack sink path unreachable"

  rogue_ip="$(get_ue_pdu_ipv4 oai-ue-rogue)" || die "final-L3: ROGUE PDU IP missing"
  gcs_ip="$(get_ue_pdu_ipv4 oai-ue-gcs)" || die "final-L3: GCS PDU IP missing"
  uav_ip="$(get_ue_pdu_ipv4 oai-ue-uav)" || die "final-L3: UAV PDU IP missing"
  rogue_iface="$(get_ue_pdu_iface oai-ue-rogue)" || die "final-L3: ROGUE iface missing"
  gcs_iface="$(get_ue_pdu_iface oai-ue-gcs)" || die "final-L3: GCS iface missing"
  ext_ip="$(kubectl get pod -n "${NS_CORE}" -l app=oai-ext-dn -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
  [[ -n "${ext_ip}" ]] || die "final-L3: ext-dn pod IP missing"

  log "[final-L3] ${ROGUE_WORD}=${rogue_ip} ($(expected_pdu_subnet rogue)) | GCS=${gcs_ip} UAV=${uav_ip} ($(expected_pdu_subnet gcs)) | ext-dn=${ext_ip}"

  if [[ "${L3_SLICE_ISOLATION}" != "1" ]]; then
    log "[final-L3] WARN: L3_SLICE_ISOLATION=0 — negative proofs skipped (isolation off)"
    r_gcs="SKIP" r_uav="SKIP" r_c2port="SKIP"
    why_gcs="L3 isolation disabled"
    why_uav="L3 isolation disabled"
    why_c2port="L3 isolation disabled"
  else
    # 1) ROGUE → GCS must FAIL
    if kubectl exec -n "${NS_RAN}" deploy/oai-ue-rogue -c nr-ue -- \
      ping -I "${rogue_iface}" -c 3 -W 2 "${gcs_ip}" >/dev/null 2>&1; then
      log "[final-L3] FAIL: ${ROGUE_WORD}→GCS ${gcs_ip} REACHABLE (slices NOT separated)"
      fail=1
    else
      r_gcs="PASS"
      why_gcs="ping blocked — cross-slice path dead"
      log "[final-L3] PASS: ${ROGUE_WORD}↛GCS (ping blocked)"
    fi

    # 2) ROGUE → UAV must FAIL (host unreachable ⇒ any port incl. :5760 is unreachable)
    if kubectl exec -n "${NS_RAN}" deploy/oai-ue-rogue -c nr-ue -- \
      ping -I "${rogue_iface}" -c 3 -W 2 "${uav_ip}" >/dev/null 2>&1; then
      log "[final-L3] FAIL: ${ROGUE_WORD}→UAV ${uav_ip} REACHABLE (slices NOT separated)"
      fail=1
    else
      r_uav="PASS"
      r_c2port="PASS"
      why_uav="ping blocked — cross-slice path dead"
      why_c2port="UAV host unreachable ⇒ C2 port :5760 unreachable"
      log "[final-L3] PASS: ${ROGUE_WORD}↛UAV (ping blocked ⇒ :5760 unreachable)"
    fi
  fi

  # 4) GCS → UAV must WORK (same slice B)
  if kubectl exec -n "${NS_RAN}" deploy/oai-ue-gcs -c nr-ue -- \
    ping -I "${gcs_iface}" -c 3 -W 2 "${uav_ip}" >/dev/null 2>&1; then
    r_b2b="PASS"
    why_b2b="same-slice C2 path alive"
    log "[final-L3] PASS: GCS→UAV (same-slice C2 alive)"
  else
    log "[final-L3] FAIL: GCS→UAV ${uav_ip} UNREACHABLE (broke legitimate C2)"
    fail=1
  fi

  # 5) ROGUE → ext-dn must WORK (pure contention still has a valid destination)
  if kubectl exec -n "${NS_RAN}" deploy/oai-ue-rogue -c nr-ue -- \
    ping -I "${rogue_iface}" -c 3 -W 2 "${ext_ip}" >/dev/null 2>&1; then
    r_ext="PASS"
    why_ext="attack sink path alive"
    log "[final-L3] PASS: ${ROGUE_WORD}→ext-dn (attack sink path alive)"
  else
    log "[final-L3] FAIL: ${ROGUE_WORD}→ext-dn ${ext_ip} UNREACHABLE (broke contention sink)"
    fail=1
  fi

  log "----------------------------------------"
  log " FINAL L3 RESULTS"
  log "   ${ROGUE_WORD}↛GCS:     ${r_gcs}      (${why_gcs})"
  log "   ${ROGUE_WORD}↛UAV:     ${r_uav}      (${why_uav})"
  log "   ${ROGUE_WORD}↛UAV:5760:${r_c2port}      (${why_c2port})"
  log "   GCS→UAV:       ${r_b2b}      (${why_b2b})"
  log "   ${ROGUE_WORD}→ext-dn:  ${r_ext}      (${why_ext})"
  log "----------------------------------------"

  if [[ "${fail}" -ne 0 ]]; then
    die "FINAL L3 slice separation TEST FAILED — testbed is NOT isolated (do not use for campaigns)"
  fi
  log "[OK] FINAL L3 slice separation TEST PASSED — slices L3-separated; C2 B↔B OK; ext-dn OK"
}

verify_gnb_rrm_policy() {
  log "[05] Verifying ORANSlice rrmPolicyRatio rendered in gNB pod..."
  local policy slice_count
  policy="$(kubectl exec -n "${NS_RAN}" deploy/oai-gnb -c gnb -- cat /opt/oai-gnb/etc/rrmPolicy.json 2>/dev/null || true)"
  [[ -n "${policy}" ]] || die "gNB rrmPolicy.json not found — check gnb-entrypoint logs."
  echo "${policy}"
  echo "${policy}" | grep -q '"rrmPolicyRatio"' \
    || die "rrmPolicy.json missing rrmPolicyRatio root key (ORANSlice schema required)"
  slice_count="$(echo "${policy}" | grep -c '"sst":' || true)"
  echo "${policy}" | grep -qE "\"min_ratio\"[[:space:]]*:[[:space:]]*${MIN_RATIO_SLICE_A}([,}]|$)" \
    || die "slice min_ratio mismatch (expected ${MIN_RATIO_SLICE_A})"
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    [[ "${slice_count}" -eq 1 ]] \
      || die "intra topology: rrmPolicyRatio must have 1 slice, found ${slice_count}"
    log "[05] gNB rrmPolicyRatio (intra): 1 slice min_ratio=${MIN_RATIO_SLICE_A}"
    return 0
  fi
  [[ "${slice_count}" -eq 2 ]] \
    || die "cross topology: rrmPolicyRatio must have 2 slices, found ${slice_count}"
  echo "${policy}" | grep -qE "\"min_ratio\"[[:space:]]*:[[:space:]]*${MIN_RATIO_SLICE_B}([,}]|$)" \
    || die "slice-B min_ratio mismatch (expected ${MIN_RATIO_SLICE_B})"
  echo "${policy}" | grep -qE "\"max_ratio\"[[:space:]]*:[[:space:]]*${MAX_RATIO_SLICE_B}([,}]|$)" \
    || die "slice-B max_ratio mismatch (expected ${MAX_RATIO_SLICE_B})"
  echo "${policy}" | grep -qE "\"max_ratio\"[[:space:]]*:[[:space:]]*${MAX_RATIO_SLICE_A}([,}]|$)" \
    || die "slice-A max_ratio mismatch (expected ${MAX_RATIO_SLICE_A})"
  log "[05] gNB rrmPolicyRatio (cross): min_ratio A=${MIN_RATIO_SLICE_A} B=${MIN_RATIO_SLICE_B} max_ratio A=${MAX_RATIO_SLICE_A} B=${MAX_RATIO_SLICE_B}"
}

verify_gnb_snssai_count() {
  local gnb_yaml count expected
  expected=1
  [[ "${TOPOLOGY}" == "cross" ]] && expected=2
  gnb_yaml="$(kubectl exec -n "${NS_RAN}" deploy/oai-gnb -c gnb -- \
    grep snssaiList /opt/oai-gnb/etc/gnb.yaml 2>/dev/null || true)"
  count="$(echo "${gnb_yaml}" | grep -oE 'sst\s*=' | wc -l | tr -d ' ')"
  [[ "${count}" -eq "${expected}" ]] \
    || die "gNB snssaiList: expected ${expected} slice(s) for topology=${TOPOLOGY}, found ${count}"
  log "[05] gNB snssaiList: ${count} slice(s) OK (topology=${TOPOLOGY})"
}

verify_oai_ran_health() {
  log "======================================"
  log " Final check: ${RAN_FLEET_SHORT}"
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    log " Topology: intra | all UEs → slice ${SST}/${SD} DNN=${DNN}"
  else
    log " Topology: cross | ${ROGUE_WORD} → ${SST}/${SD}/${DNN} | C2 → ${SST_B}/${SD_B}/${DNN_B}"
  fi
  log "======================================"
  sleep_with_progress 90 "gNB + ${EXPECTED_UE_COUNT} ${NR_UE_WORD} attach (OAI RFsim)"

  verify_gnb_rrm_policy
  verify_gnb_snssai_count
  for role in "${UE_ROLES[@]}"; do
    wait_for_ue_oai_iface "${role}"
  done
  for role in "${UE_ROLES[@]}"; do
    verify_ue_pdu_profile "${role}"
  done
  verify_ul_rrmpolicy_binary
  for role in "${UE_ROLES[@]}"; do
    verify_ue_internet_connectivity "${role}"
  done
  verify_ue_iperf_smoke gcs
  verify_ue_iperf_smoke uav
  verify_cross_slice_isolation

  write_topology_stamp
  log "[OK] RAN phase healthy — ${RAN_FLEET_SHORT}, topology=${TOPOLOGY}."
}

# =============================================================================
# 06 — Bootstrap gates: MinRatio, OWD/IPDV probe, MAC 5QI evidence, rfsim ceiling
# =============================================================================
print_clock_methodology() {
  log "[06] Clock methodology — Gate 1.8, rfsim single-host:"
  log "  C2 metric leg: GCS→UAV (forward PDU path; paper UAV↔GCS uses this leg)."
  log "  GCS+UAV co-located on Kind → shared kernel CLOCK_MONOTONIC time base."
  log "  ROGUE is never measured — PRB contention only."
  log "  Checks: co-location, /proc/self/ns/time match, GCS PDU loopback,"
  log "           GCS↔UAV PDU handshake (causality t1≤t2≤t4; NTP theta diagnostic),"
  log "           negative OWD gate, temporal OWD trend (not sorted spread)."
}

verify_shared_time_namespace() {
  local gcs_ns uav_ns
  gcs_ns="$(kubectl exec -n "${NS_RAN}" deploy/oai-ue-gcs -c nr-ue -- \
    readlink /proc/self/ns/time 2>/dev/null | tr -d '\r')"
  uav_ns="$(kubectl exec -n "${NS_RAN}" deploy/oai-ue-uav -c nr-ue -- \
    readlink /proc/self/ns/time 2>/dev/null | tr -d '\r')"
  [[ -n "${gcs_ns}" && -n "${uav_ns}" ]] \
    || die "Cannot read time namespace for GCS/UAV — OWD clock sanity failed."
  if [[ "${gcs_ns}" != "${uav_ns}" ]]; then
    die "GCS time ns (${gcs_ns}) != UAV time ns (${uav_ns}) — cross-namespace OWD invalid."
  fi
  log "[06] Clock sanity: GCS+UAV share time namespace ${gcs_ns}"
}

verify_c2_colocated_for_owd() {
  local gcs_node uav_node
  gcs_node="$(kubectl get pod -n "${NS_RAN}" -l app=oai-ue-gcs \
    -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)"
  uav_node="$(kubectl get pod -n "${NS_RAN}" -l app=oai-ue-uav \
    -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)"
  [[ -n "${gcs_node}" && -n "${uav_node}" ]] \
    || die "Cannot resolve GCS/UAV node for OWD clock sanity."
  if [[ "${gcs_node}" != "${uav_node}" ]]; then
    die "GCS (${gcs_node}) and UAV (${uav_node}) are not co-located — CLOCK_MONOTONIC OWD invalid."
  fi
  log "[06] Clock sanity: GCS+UAV co-located on node ${gcs_node}"
  verify_shared_time_namespace
}

verify_per_ue_qos_in_mysql() {
  log "[06] Gate 1.7 — per-UE slice/DNN/5QI in MySQL (provisioning; SMF mini uses local_subscription_infos)"
  if ! kubectl exec -n "${NS_CORE}" deploy/oai-mysql -c mysql -- \
    mysql -utest -ptest oai_db -N -e \
    "SHOW TABLES LIKE 'SessionManagementSubscriptionData';" 2>/dev/null \
    | grep -q SessionManagementSubscriptionData; then
    die "MySQL lacks cn5g SessionManagementSubscriptionData — per-UE slice/5QI impossible. Run: ./stop_testbed.sh && ./start_testbed_mitigation.sh"
  fi
  local role imsi row qi dnn
  for role in rogue gcs uav; do
    imsi="$(ue_role_imsi "${role}")"
    read -r _ _ _ dnn qi <<< "$(ue_slice_profile "${role}")"
    row="$(kubectl exec -n "${NS_CORE}" deploy/oai-mysql -c mysql -- \
      mysql -utest -ptest oai_db -N -e \
      "SELECT dnnConfigurations FROM SessionManagementSubscriptionData WHERE ueid='${imsi}' ORDER BY singleNssai;" \
      2>/dev/null | tr -d '\r' || true)"
    [[ -n "${row}" ]] || die "MySQL: no SessionManagement row for ${role} IMSI ${imsi}"
    if echo "${row}" | grep -q "\"${dnn}\"" \
      && echo "${row}" | grep -qE '"5qi"[[:space:]]*:[[:space:]]*'"${qi}"; then
      log "[06] ${role} (${imsi}): dnn=${dnn} 5QI=${qi} OK"
    else
      die "${role} (${imsi}): expected 5QI=${qi} in dnnConfigurations, got: ${row}"
    fi
  done
}

run_owd_ipdv_probe() {
  need_cmd python3
  verify_c2_colocated_for_owd
  if (( OWD_PROBE_DURATION < 600 )); then
    log "[06] WARN: OWD_PROBE_DURATION=${OWD_PROBE_DURATION}s < 600s — p99.9 tail may be unstable; use OWD_PROBE_DURATION=600 for paper stats."
  fi
  local gcs_ip uav_ip out probe_py
  gcs_ip="$(get_ue_pdu_ipv4 oai-ue-gcs)" || die "GCS PDU IP missing for OWD probe"
  uav_ip="$(get_ue_pdu_ipv4 oai-ue-uav)" || die "UAV PDU IP missing for OWD probe"
  out="${CONFIG_DIR}/owd-ipdv-probe.json"
  probe_py="${CONFIG_DIR}/owd_probe_runner.py"
  log "[06] Gate 1.8 — OWD/IPDV GCS to UAV, ${OWD_PROBE_DURATION}s, warm-up ${OWD_PROBE_WARMUP}s"
  log "[06]   ${gcs_ip} -> ${uav_ip} via PDU, CLOCK_MONOTONIC on both sides"
  # validate-under-attack: DO-377A caps are the measured outcome, not a gate — disable
  # them so expected C2 degradation does not die the run. Measurement-integrity gates
  # (negative OWD, min_samples, temporal-trend clock contamination) stay enforced.
  local owd_cap_eff="${OWD_DO377A_MAX_US}" ipdv_cap_eff="${IPDV_DO377A_P99_MAX_US}" avail_min_eff="${DO377A_AVAILABILITY_MIN_PCT}"
  if [[ "${VALIDATE_UNDER_ATTACK}" == "1" ]]; then
    owd_cap_eff="0"
    ipdv_cap_eff="0"
    avail_min_eff="0"
    log "[06]   validate-under-attack: DO-377A OWD/IPDV/availability caps disabled (recorded as metrics; compare B=0 vs B=60 offline)"
  fi
  cat > "${probe_py}" <<PYPROBE
import json, subprocess, sys, time

gcs_ip, uav_ip, duration_s, warmup_s, min_samples, ns, ctx = sys.argv[1:8]
trend_max_us = float(sys.argv[8])
loopback_max_us = float(sys.argv[9])
owd_cap_us = float(sys.argv[10])
ipdv_cap_us = float(sys.argv[11])
avail_min_pct = float(sys.argv[12])
ordering_violations_max = int(sys.argv[13])
handshake_samples = int(sys.argv[14])
duration_s = int(duration_s)
warmup_s = int(warmup_s)
min_samples = int(min_samples)
port = 47999
loopback_port = 47998
handshake_port = 47997

# PIN_CORES=1: pin the in-pod OWD sender/receiver to the same reserved cores as their
# softmodem so the measurement itself is isolated from the attacker's CPU burn (empty = off).
_gcs_cores = "${PIN_GCS}"
_uav_cores = "${PIN_UAV}"
def _pin(deploy):
    c = _uav_cores if "uav" in deploy else (_gcs_cores if "gcs" in deploy else "")
    return (["taskset", "-c", c] if c else [])

def kexec(deploy, script):
    target = "deploy/" + deploy
    cmd = [
        "kubectl", "--context", ctx, "exec", "-n", ns, target, "-c", "nr-ue", "--",
    ] + _pin(deploy) + ["python3", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True)

def read_time_ns(deploy):
    res = kexec(deploy, "import os; print(os.readlink('/proc/self/ns/time'))")
    if res.returncode != 0:
        sys.exit("read time ns %s failed: %s" % (deploy, res.stderr or res.stdout))
    return (res.stdout or "").strip()

gcs_time_ns = read_time_ns("oai-ue-gcs")
uav_time_ns = read_time_ns("oai-ue-uav")
if gcs_time_ns != uav_time_ns:
    sys.exit(
        "GCS/UAV time namespace mismatch: gcs=%r uav=%r" % (gcs_time_ns, uav_time_ns)
    )

def pct(values, p):
    if not values:
        return None
    m = len(values)
    idx = min(m - 1, int(round((p / 100.0) * (m - 1))))
    return values[idx]

def decile_means_time_ordered(owds):
    w = max(1, len(owds) // 10)
    first = sum(owds[:w]) / w
    last = sum(owds[-w:]) / w
    return first, last, last - first

def run_loopback_sanity():
    script = (
        "import socket, struct, time, json\n"
        "ip, port = '%s', %d\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((ip, port))\n"
        "sock.settimeout(0.05)\n"
        "end = time.monotonic() + 3\n"
        "lat = []\n"
        "while time.monotonic() < end:\n"
        "    tx = time.monotonic_ns()\n"
        "    sock.sendto(struct.pack('!Q', tx), (ip, port))\n"
        "    try:\n"
        "        data, _ = sock.recvfrom(64)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    if len(data) < 8:\n"
        "        continue\n"
        "    rx = time.monotonic_ns()\n"
        "    lat.append((rx - tx) / 1000.0)\n"
        "out = {'samples': len(lat)}\n"
        "if lat:\n"
        "    s = sorted(lat)\n"
        "    out.update({'median_us': s[len(s)//2], 'p99_us': s[int(len(s)*0.99)], 'max_us': s[-1]})\n"
        "print(json.dumps(out))\n"
    ) % (gcs_ip, loopback_port)
    res = kexec("oai-ue-gcs", script)
    if res.returncode != 0:
        sys.exit("GCS loopback clock sanity failed: %s" % (res.stderr or res.stdout))
    try:
        data = json.loads(res.stdout.strip() or "{}")
    except json.JSONDecodeError:
        sys.exit("GCS loopback clock sanity: invalid JSON: %r" % res.stdout)
    med = data.get("median_us")
    if data.get("samples", 0) < 100:
        sys.exit("GCS loopback clock sanity: too few samples (%s)" % data.get("samples"))
    if med is None or med > loopback_max_us:
        sys.exit(
            "GCS loopback median %.1f us exceeds cap %.1f us — CLOCK_MONOTONIC/UDP sanity failed"
            % (med or -1, loopback_max_us)
        )
    return data

def run_clock_handshake():
    echo_script = (
        "import socket, struct, time\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind(('%s', %d))\n"
        "sock.settimeout(0.15)\n"
        "end = time.monotonic() + 10\n"
        "while time.monotonic() < end:\n"
        "    try:\n"
        "        data, addr = sock.recvfrom(64)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    if len(data) < 8:\n"
        "        continue\n"
        "    t1, = struct.unpack('!Q', data[:8])\n"
        "    t2 = time.monotonic_ns()\n"
        "    t3 = time.monotonic_ns()\n"
        "    sock.sendto(struct.pack('!QQQ', t1, t2, t3), addr)\n"
    ) % (uav_ip, handshake_port)
    echo_proc = subprocess.Popen(
        [
            "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
            "python3", "-c", echo_script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.5)
    client_script = (
        "import socket, struct, time, json\n"
        "uav_ip, port, n, gcs_ip = '%s', %d, %d, '%s'\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "sock.bind((gcs_ip, 0))\n"
        "sock.settimeout(0.25)\n"
        "thetas = []\n"
        "delays = []\n"
        "violations = 0\n"
        "ok = 0\n"
        "for _ in range(n):\n"
        "    t1 = time.monotonic_ns()\n"
        "    try:\n"
        "        sock.sendto(struct.pack('!Q', t1), (uav_ip, port))\n"
        "        data, _ = sock.recvfrom(64)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    t4 = time.monotonic_ns()\n"
        "    if len(data) < 24:\n"
        "        continue\n"
        "    t1_r, t2, t3 = struct.unpack('!QQQ', data[:24])\n"
        "    if t1_r != t1:\n"
        "        continue\n"
        "    if t2 < t1:\n"
        "        violations += 1\n"
        "    theta_ns = ((t2 - t1) + (t3 - t4)) // 2\n"
        "    delay_ns = ((t4 - t1) - (t3 - t2)) // 2\n"
        "    thetas.append(theta_ns / 1000.0)\n"
        "    delays.append(delay_ns / 1000.0)\n"
        "    ok += 1\n"
        "    time.sleep(0.002)\n"
        "out = {'samples': ok}\n"
        "if thetas:\n"
        "    s = sorted(thetas)\n"
        "    d = sorted(delays)\n"
        "    out.update({\n"
        "        'ntp_theta_median_us': s[len(s) // 2],\n"
        "        'ntp_theta_mean_us': sum(thetas) / len(thetas),\n"
        "        'ntp_theta_min_us': s[0],\n"
        "        'ntp_theta_max_us': s[-1],\n"
        "        'rtt_median_us': d[len(d) // 2],\n"
        "        'ordering_violations': violations,\n"
        "        'note': 'NTP theta on asymmetric PDU path — path asymmetry, not clock skew',\n"
        "    })\n"
        "print(json.dumps(out))\n"
    ) % (uav_ip, handshake_port, handshake_samples, gcs_ip)
    client_res = kexec("oai-ue-gcs", client_script)
    try:
        echo_proc.wait(timeout=12)
    except subprocess.TimeoutExpired:
        echo_proc.kill()
    if client_res.returncode != 0:
        sys.exit("Clock handshake failed: %s" % (client_res.stderr or client_res.stdout))
    try:
        data = json.loads(client_res.stdout.strip() or "{}")
    except json.JSONDecodeError:
        sys.exit("Clock handshake: invalid JSON: %r" % client_res.stdout)
    min_hs = max(20, handshake_samples // 5)
    if data.get("samples", 0) < min_hs:
        sys.exit(
            "Clock handshake: too few samples (%s < %s)"
            % (data.get("samples"), min_hs)
        )
    violations = data.get("ordering_violations")
    if violations is None:
        sys.exit("Clock handshake: missing ordering_violations")
    if violations > ordering_violations_max:
        sys.exit(
            "Clock handshake: %d ordering violations (t2 < t1) exceeds cap %d — causality broken"
            % (violations, ordering_violations_max)
        )
    return data

loopback = run_loopback_sanity()
handshake = run_clock_handshake()
# Shared time namespace → zero clock skew; do not subtract NTP theta (PDU path is asymmetric).
clock_offset_us = 0.0

recv_script = (
    "import socket, struct, time, json\n"
    "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
    "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "sock.settimeout(0.1)\n"
    "sock.bind(('%s', %d))\n"
    "end = time.monotonic() + %d\n"
    "samples = []\n"
    "while time.monotonic() < end:\n"
    "    try:\n"
    "        data, _ = sock.recvfrom(64)\n"
    "    except socket.timeout:\n"
    "        continue\n"
    "    if len(data) < 8:\n"
    "        continue\n"
    "    tx_ns, = struct.unpack('!Q', data[:8])\n"
    "    rx_ns = time.monotonic_ns()\n"
    "    samples.append({'tx_ns': tx_ns, 'rx_ns': rx_ns, 'owd_us': (rx_ns - tx_ns) / 1000.0})\n"
    "open('/tmp/owd_recv.json', 'w').write(json.dumps(samples))\n"
) % (uav_ip, port, duration_s + 5)

recv_proc = subprocess.Popen(
    [
        "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
    ] + _pin("oai-ue-uav") + ["python3", "-c", recv_script],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    text=True,
)
time.sleep(2)

send_script = (
    "import socket, struct, time, json\n"
    "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
    "sock.bind(('%s', 0))\n"
    "end = time.monotonic() + %d\n"
    "sent = 0\n"
    "while time.monotonic() < end:\n"
    "    tx = time.monotonic_ns()\n"
    "    sock.sendto(struct.pack('!Q', tx), ('%s', %d))\n"
    "    sent += 1\n"
    "    time.sleep(0.001)\n"
    "print(json.dumps({'packets_sent': sent}))\n"
) % (gcs_ip, duration_s, uav_ip, port)
send_res = kexec("oai-ue-gcs", send_script)
if send_res.returncode != 0:
    recv_proc.kill()
    sys.exit("GCS OWD sender failed: %s" % (send_res.stderr or send_res.stdout))
packets_sent = 0
for line in reversed((send_res.stdout or "").strip().splitlines()):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        packets_sent = int(json.loads(line).get("packets_sent", 0))
        break
    except (json.JSONDecodeError, TypeError, ValueError):
        continue
try:
    recv_proc.wait(timeout=duration_s + 30)
except subprocess.TimeoutExpired:
    recv_proc.kill()
    sys.exit("UAV OWD receiver timed out")
if recv_proc.returncode not in (0, None):
    sys.exit("UAV OWD receiver failed: %s" % (recv_proc.stderr or ""))

raw = subprocess.run(
    [
        "kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-ue-uav", "-c", "nr-ue", "--",
        "cat", "/tmp/owd_recv.json",
    ],
    capture_output=True,
    text=True,
)
try:
    samples = json.loads(raw.stdout or "[]")
except json.JSONDecodeError:
    samples = []

post = [s for s in samples if s.get("owd_us") is not None]
packets_received_total = len(post)
drop = warmup_s * 1000
if len(post) > drop:
    post = post[drop:]
owds = [s["owd_us"] for s in post]
neg_owds = [o for o in owds if o < 0]
if neg_owds:
    sys.exit(
        "OWD samples with negative latency: %d (min %.1f us) — causality violation on shared CLOCK_MONOTONIC"
        % (len(neg_owds), min(neg_owds))
    )
owds_sorted = sorted(owds)
n = len(owds)
expected_sent_post = max(1, int((duration_s - warmup_s) * 1000))
if packets_sent > 0:
    expected_sent_post = max(1, packets_sent - drop)
avail_pct = 100.0 * n / expected_sent_post
loss_pct = max(0.0, 100.0 - avail_pct)

ipdv = []
for i in range(1, len(owds)):
    ipdv.append(abs(owds[i] - owds[i - 1]))
ipdv_sorted = sorted(ipdv)

temporal_first_us = temporal_last_us = temporal_trend_us = None
owd_spread_sorted_deciles_us = None
if n >= 200:
    temporal_first_us, temporal_last_us, temporal_trend_us = decile_means_time_ordered(owds)
    w = n // 10
    owd_spread_sorted_deciles_us = (sum(owds_sorted[-w:]) / w) - (sum(owds_sorted[:w]) / w)

report = {
    "clock_methodology": (
        "CLOCK_MONOTONIC single-host Kind (GCS+UAV co-located, shared time ns); "
        "OWD = rx_uav - tx_gcs (no NTP correction on asymmetric PDU)"
    ),
    "gcs_ip": gcs_ip,
    "uav_ip": uav_ip,
    "duration_s": duration_s,
    "warmup_discarded_s": warmup_s,
    "samples": n,
    "clock_sanity": {
        "time_namespace_gcs": gcs_time_ns,
        "time_namespace_uav": uav_time_ns,
        "time_namespace_match": gcs_time_ns == uav_time_ns,
        "loopback_gcs_pdu": loopback,
        "loopback_median_max_us": loopback_max_us,
        "handshake_4ts": handshake,
        "ordering_violations_max": ordering_violations_max,
        "clock_offset_applied_us": clock_offset_us,
        "offset_correction_note": (
            "Shared time namespace — no subtraction; "
            "handshake ntp_theta_median_us is PDU path-asymmetry diagnostic"
        ),
    },
    "owd_us": {
        "min": owds_sorted[0] if owds_sorted else None,
        "median": pct(owds_sorted, 50),
        "p99": pct(owds_sorted, 99),
        "p99_9": pct(owds_sorted, 99.9),
        "max": owds_sorted[-1] if owds_sorted else None,
    },
    "ipdv_us": {
        "median": pct(ipdv_sorted, 50),
        "p99": pct(ipdv_sorted, 99),
        "p99_9": pct(ipdv_sorted, 99.9),
        "max": ipdv_sorted[-1] if ipdv_sorted else None,
    },
    "owd_temporal_trend_us": {
        "first_decile_mean_us": temporal_first_us,
        "last_decile_mean_us": temporal_last_us,
        "delta_us": temporal_trend_us,
        "max_abs_delta_us": trend_max_us,
        "note": "Time-ordered 1st vs last decile — clock contamination if |delta| large",
    },
    "owd_spread_sorted_deciles_us": {
        "delta_us": owd_spread_sorted_deciles_us,
        "note": "Fastest vs slowest deciles after sorting — distribution spread, NOT clock drift",
    },
    "availability": {
        "packets_sent": packets_sent,
        "packets_received_total": packets_received_total,
        "packets_received_post_warmup": n,
        "expected_sent_post_warmup": expected_sent_post,
        "availability_post_warmup_pct": round(avail_pct, 4),
        "packet_loss_post_warmup_pct": round(loss_pct, 4),
        "do377a_min_availability_pct": avail_min_pct,
        "do377a_compliant": avail_pct >= avail_min_pct,
    },
    "do377a_thresholds_us": {
        "owd_p99_max": owd_cap_us if owd_cap_us > 0 else None,
        "ipdv_p99_max": ipdv_cap_us if ipdv_cap_us > 0 else None,
    },
}
print(json.dumps(report, indent=2))
if n < min_samples:
    sys.exit("too few OWD samples: %d < %d" % (n, min_samples))
if temporal_trend_us is not None and abs(temporal_trend_us) > trend_max_us:
    sys.exit(
        "OWD temporal trend |%.1f| us exceeds cap %.1f us — possible clock/path non-stationarity"
        % (temporal_trend_us, trend_max_us)
    )
if avail_pct < avail_min_pct:
    sys.exit(
        "UDP availability %.4f%% < DO-377A minimum %.4f%% post-warmup"
        % (avail_pct, avail_min_pct)
    )
owd_p99 = report["owd_us"]["p99"]
ipdv_p99 = report["ipdv_us"]["p99"]
if owd_cap_us > 0 and owd_p99 is not None and owd_p99 > owd_cap_us:
    sys.exit("OWD p99 %.1f us exceeds DO-377A cap %.1f us" % (owd_p99, owd_cap_us))
if ipdv_cap_us > 0 and ipdv_p99 is not None and ipdv_p99 > ipdv_cap_us:
    sys.exit("IPDV p99 %.1f us exceeds DO-377A cap %.1f us" % (ipdv_p99, ipdv_cap_us))
PYPROBE
  python3 "${probe_py}" "${gcs_ip}" "${uav_ip}" "${OWD_PROBE_DURATION}" "${OWD_PROBE_WARMUP}" \
    "${OWD_PROBE_MIN_SAMPLES}" "${NS_RAN}" "${KUBE_CONTEXT}" \
    "${OWD_PROBE_TEMPORAL_TREND_MAX_US}" "${OWD_LOOPBACK_MEDIAN_MAX_US}" \
    "${owd_cap_eff}" "${ipdv_cap_eff}" "${avail_min_eff}" \
    "${OWD_HANDSHAKE_ORDERING_VIOLATIONS_MAX}" "${OWD_HANDSHAKE_SAMPLES}" > "${out}" \
    || die "OWD/IPDV probe failed — see ${out}"
  cat "${out}"
  log "[06] OWD/IPDV probe saved: ${out}"
}

validate_min_ratio_prb() {
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    log "[06] Gate 1.5 — rRMPolicyMinRatio intra (single slice)=${MIN_RATIO_SLICE_A}"
    log "[06]   intra: all UEs share one slice — MinRatio partitions PRBs, not GCS/UAV vs ROGUE"
  else
    log "[06] Gate 1.5 — rRMPolicyMinRatio cross A=${MIN_RATIO_SLICE_A} B=${MIN_RATIO_SLICE_B}"
    log "[06]   cross: MIN_RATIO_SLICE_B is slice-level C2 defense vs ROGUE on slice A"
  fi
  verify_gnb_rrm_policy
  if [[ "${VALIDATE_UNDER_ATTACK}" == "1" ]]; then
    log "[06] Gate 1.5 — validate-under-attack: JSON MinRatio verified; flood auto-driven around the probe (not here)"
    return 0
  fi
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    if [[ "${MIN_RATIO_SLICE_A}" == "0" ]]; then
      log "[06] WARN: MinRatio baseline 0 — set MIN_RATIO_SLICE_A=60 or 20-30"
      log "       and redeploy (stop && start) to exercise PRB partition before Exp 2."
      return 0
    fi
  elif [[ "${MIN_RATIO_SLICE_A}" == "0" && "${MIN_RATIO_SLICE_B}" == "0" ]]; then
    log "[06] WARN: MinRatio baseline 0/0 — set MIN_RATIO_SLICE_B=60 or 20-30 for cross Exp 2/3"
    log "       and redeploy (stop && start)."
    return 0
  fi
  local ext_ip rogue_ip
  ext_ip="$(ensure_ext_dn_iperf_server)"
  rogue_ip="$(get_ue_pdu_ipv4 oai-ue-rogue)" || die "ROGUE PDU IP missing for MinRatio load"
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    log "[06] ROGUE UDP flood (intra shared slice) → ext-dn ${ext_ip}, 15s, sampling gNB MAC logs..."
  else
    log "[06] ROGUE UDP flood slice A → ext-dn ${ext_ip}, 15s, sampling gNB MAC logs..."
  fi
  kubectl logs -n "${NS_RAN}" deploy/oai-gnb -c gnb --since=5s >/dev/null 2>&1 || true
  kubectl exec -n "${NS_RAN}" deploy/oai-ue-rogue -c nr-ue -- \
    iperf3 -c "${ext_ip}" -u -b 50M -t 15 -B "${rogue_ip}" >/dev/null 2>&1 &
  local flood_pid=$!
  sleep 16
  wait "${flood_pid}" 2>/dev/null || true
  local mac_ev="${CONFIG_DIR}/gate15-mac-slice-prb.log"
  kubectl logs -n "${NS_RAN}" deploy/oai-gnb -c gnb --tail=500 2>/dev/null \
    | grep -iE 'slice|PRB|ratio|MinRatio|sched' > "${mac_ev}" || true
  if [[ ! -s "${mac_ev}" ]]; then
    die "Gate 1.5: no slice/PRB lines in gNB log with MinRatio≠0 — scheduler evidence missing. Inspect: kubectl logs -n ${NS_RAN} deploy/oai-gnb -c gnb --tail=200"
  fi
  log "[06] MAC slice/PRB evidence saved: ${mac_ev} — $(wc -l < "${mac_ev}") lines (manual review — JSON MinRatio already verified)"
}

capture_mac_5qi_evidence() {
  local out="${CONFIG_DIR}/gate19-mac-5qi-evidence.log"
  log "[06] Gate 1.9 — best-effort MAC 5QI/scheduler grep (OAI may not log GBR/5QI at MAC)..."
  kubectl logs -n "${NS_RAN}" deploy/oai-gnb -c gnb --tail=1000 2>/dev/null \
    | grep -iE '5qi|Qos|LCID|sched|slice|GBR|DRB' > "${out}" || true
  if [[ ! -s "${out}" ]]; then
    log "[06] WARN: no 5QI/sched lines in gNB log — MySQL 5QI provisioned; MAC evidence absent on this OAI build."
    log "       kubectl logs -n ${NS_RAN} deploy/oai-gnb -c gnb | grep -i slice"
  else
    log "[06] MAC 5QI evidence (best-effort): ${out} — $(wc -l < "${out}") lines"
  fi
}

measure_rfsim_throughput_ceiling() {
  local ext_ip ue_ip out
  ext_ip="$(ensure_ext_dn_iperf_server)"
  ue_ip="$(get_ue_pdu_ipv4 oai-ue-gcs)" || return 0
  out="${CONFIG_DIR}/rfsim-throughput-ceiling.txt"
  log "[06] SANITY — rfsim UDP ceiling: GCS → ext-dn, 10s at 200M"
  kubectl exec -n "${NS_RAN}" deploy/oai-ue-gcs -c nr-ue -- \
    iperf3 -c "${ext_ip}" -u -b 200M -t 10 -B "${ue_ip}" > "${out}" 2>&1 || true
  log "[06] Ceiling probe saved: ${out}"
  grep -E 'receiver|sender|Mbits' "${out}" | tail -5 || true
}

# validate-under-attack: script-driven ROGUE flood. Launches iperf3 in the
# background for the FULL probe window (+warm-up +margin) so the C2 OWD/IPDV is
# always measured with slice-A saturation active — no manual two-terminal timing.
launch_rogue_attack_flood() {
  local flood_s=$(( OWD_PROBE_DURATION + OWD_PROBE_WARMUP + 30 ))
  local ext_ip rogue_ip flog
  ext_ip="$(ensure_ext_dn_iperf_server)"
  rogue_ip="$(get_ue_pdu_ipv4 oai-ue-rogue)" || die "ROGUE PDU IP missing for under-attack flood"
  flog="${CONFIG_DIR}/under-attack-rogue-flood.log"
  # PIN_CORES=1: confine the flood generator to attacker cores so its CPU burn can't
  # jitter the measured C2 path (the softmodems are pinned via their entrypoints).
  local attack_pin=""
  [[ -n "${PIN_ATTACK}" ]] && { attack_pin="taskset -c ${PIN_ATTACK}"; log "[06]   flood CPU pinning: cores ${PIN_ATTACK}"; }
  if [[ "${ROGUE_FLOOD_MODE}" == "pulse" ]]; then
    local avg_mbps drain_ms
    avg_mbps="$(awk -v kb="${ROGUE_PULSE_BOLUS_KB}" -v p="${ROGUE_PULSE_PERIOD_MS}" 'BEGIN{printf "%.1f",(kb*1024*8)/(p*1000)}')"
    drain_ms="$(awk -v kb="${ROGUE_PULSE_BOLUS_KB}" 'BEGIN{printf "%.0f",(kb*1024*8)/50000}')"  # ms to drain bolus at ~50 Mbps air
    log "[06] validate-under-attack — ROGUE PULSE (low-rate/shrew) slice A → ext-dn ${ext_ip} for ${flood_s}s"
    log "[06]   bolus=${ROGUE_PULSE_BOLUS_KB}KB period=${ROGUE_PULSE_PERIOD_MS}ms pkt=${ROGUE_PULSE_PKT_BYTES}B flows=${ROGUE_PULSE_FLOWS} ≈ ${avg_mbps} Mbit/s avg, ~${drain_ms}ms starvation/pulse"
    kubectl exec -i -n "${NS_RAN}" deploy/oai-ue-rogue -c nr-ue -- \
      ${attack_pin} python3 - "${rogue_ip}" "${ext_ip}" 5201 "${flood_s}" \
      "${ROGUE_PULSE_BOLUS_KB}" "${ROGUE_PULSE_PERIOD_MS}" "${ROGUE_PULSE_PKT_BYTES}" "${ROGUE_PULSE_FLOWS}" > "${flog}" 2>&1 <<'PY' &
import socket, sys, time, threading
src, dst, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
dur = float(sys.argv[4]); bolus_kb = int(sys.argv[5])
period = float(sys.argv[6]) / 1000.0; pkt = int(sys.argv[7])
flows = max(1, int(sys.argv[8]))
payload = b"\x00" * pkt
total_pkts = max(1, (bolus_kb * 1024) // pkt)      # datagrams per bolus (whole burst)
per_flow = max(1, total_pkts // flows)             # each concurrent flow blasts this share
end = time.monotonic() + dur
barrier = threading.Barrier(flows)                 # all flows fire together → simultaneous queues
stats = [0] * flows

def worker(idx):
    # Each flow = own socket = distinct src port = distinct 5-tuple = distinct MAC queue.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
    except OSError:
        pass
    s.bind((src, 0))
    sent = 0
    while time.monotonic() < end:
        try:
            barrier.wait(timeout=5)                # sync burst start across all flows
        except threading.BrokenBarrierError:
            break
        t0 = time.monotonic()
        for _ in range(per_flow):
            try:
                s.sendto(payload, (dst, port))
                sent += 1
            except OSError:                        # ENOBUFS: air congested (goal); brief yield
                time.sleep(0.0002)
        if idx == 0:                               # flow 0 paces the idle period for everyone
            rem = period - (time.monotonic() - t0)
            if rem > 0:
                time.sleep(rem)
    stats[idx] = sent

threads = [threading.Thread(target=worker, args=(i,)) for i in range(flows)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("pulse-flood done flows=%d packets=%d per_flow=%d bolus_kb=%d period_ms=%.1f pkt=%d"
      % (flows, sum(stats), per_flow, bolus_kb, period * 1000.0, pkt))
PY
  else
    local dir_flag="" dir_desc="uplink (GCS-UL half)"
    case "${ROGUE_FLOOD_DIR}" in
      dl)   dir_flag="-R";      dir_desc="downlink (UAV-DL half — C2 delivery leg)" ;;
      both) dir_flag="--bidir"; dir_desc="bidirectional (both halves of C2 path)" ;;
      ul|*) dir_flag="";        dir_desc="uplink (GCS-UL half)" ;;
    esac
    log "[06] validate-under-attack — launching ROGUE flood (cbr, ${dir_desc}) slice A → ext-dn ${ext_ip} for ${flood_s}s (-b ${ROGUE_FLOOD_MBPS}M -P ${ROGUE_FLOOD_PARALLEL} ${dir_flag})"
    kubectl exec -n "${NS_RAN}" deploy/oai-ue-rogue -c nr-ue -- \
      ${attack_pin} iperf3 -c "${ext_ip}" -u -b "${ROGUE_FLOOD_MBPS}M" -P "${ROGUE_FLOOD_PARALLEL}" -t "${flood_s}" -B "${rogue_ip}" ${dir_flag} > "${flog}" 2>&1 &
  fi
  ROGUE_ATTACK_PID=$!
  trap stop_rogue_attack_flood EXIT
  sleep 3
  kill -0 "${ROGUE_ATTACK_PID}" 2>/dev/null \
    || die "ROGUE flood failed to start — see ${flog}"
  log "[06] ROGUE flood active (pid ${ROGUE_ATTACK_PID}); log: ${flog}"
}

stop_rogue_attack_flood() {
  [[ -n "${ROGUE_ATTACK_PID}" ]] || return 0
  if kill -0 "${ROGUE_ATTACK_PID}" 2>/dev/null; then
    log "[06] Stopping ROGUE flood (pid ${ROGUE_ATTACK_PID})"
    kill "${ROGUE_ATTACK_PID}" 2>/dev/null || true
    wait "${ROGUE_ATTACK_PID}" 2>/dev/null || true
  fi
  ROGUE_ATTACK_PID=""
}

# =============================================================================
# Proof-of-mechanism: per-slice PRB/scheduler evidence (roadmap 3.2 / Exp 3)
# The gNB MAC (log level info) periodically dumps per-RNTI scheduled dl/ul bytes
# + MCS. We capture that stream across the probe, map RNTI->role->slice, and
# summarise the per-slice share over time. Combined with the MinRatio B=0 vs B=60
# intervention this attributes the C2 IPDV to PRB contention (not CPU/UPF/clock).
# NOTE: stock OAI image exposes SCHEDULED BYTES + MCS, not raw PRB-per-slot; bytes
# (normalised by MCS spectral efficiency) is the PRB proxy. True per-slot PRB needs
# E2/KPM or a scheduler patch — documented as such in the artifact JSON.
# =============================================================================
MAC_PRB_CAP_PID=""

# Emit the capture+parse tool once so both the live capture and offline unit test
# run identical logic. Writes ${CONFIG_DIR}/mac_prb_tool.py.
write_mac_prb_tool() {
  cat > "${CONFIG_DIR}/mac_prb_tool.py" <<'PYTOOL'
#!/usr/bin/env python3
"""Capture/parse OAI gNB MAC scheduler dumps into per-slice PRB-proxy evidence.

Modes:
  capture <ctx> <ns> <duration_s> <raw_out>
      Follow `kubectl logs -f` for the gNB and write each line prefixed with a
      wall-clock epoch (t=<float> ) so the parser can build a time series.
  parse <raw_out> <rnti_map> <roleslice_json> <csv_out> <json_out>
      rnti_map: lines "<rnti_hex> <role>" (role in rogue/gcs/uav; unknown allowed)
      roleslice_json: {"rogue":"A","gcs":"B","uav":"B"} (labels only)
"""
import sys, re, json, subprocess, time

# MCS index -> approx spectral efficiency (bits/RE), 3GPP 38.214 Table 5.1.3.1-1 (256QAM table, coarse).
def mcs_eff(mcs):
    if mcs is None:
        return None
    tbl = [0.23,0.31,0.38,0.49,0.60,0.74,0.88,1.03,1.18,1.33,1.33,1.48,1.70,1.91,2.16,
           2.41,2.57,2.73,3.03,3.32,3.61,3.90,4.21,4.52,4.82,5.12,5.33,5.55,5.89,6.23,7.41]
    try:
        return tbl[int(mcs)] if 0 <= int(mcs) < len(tbl) else None
    except (TypeError, ValueError):
        return None

RE_RNTI_HDR = re.compile(r'UE\s+RNTI\s+([0-9a-fA-F]{2,4})')
RE_UE_LINE  = re.compile(r'UE\s+([0-9a-fA-F]{2,4})\s*:')
RE_DL_BYTES = re.compile(r'dlsch_total_bytes\s+(\d+)')
RE_UL_BYTES = re.compile(r'ulsch_total_bytes\s+(\d+)')
RE_MAC_TXRX = re.compile(r'MAC:\s*TX\s+(\d+)\s+RX\s+(\d+)\s+bytes')
RE_DLSCH    = re.compile(r'dlsch_rounds\s+(\d+)')
RE_ULSCH    = re.compile(r'ulsch_rounds\s+(\d+)')
RE_MCS      = re.compile(r'MCS\s*\(\d+\)\s*(\d+)')
RE_NPRB     = re.compile(r'NPRB\s+(\d+)')
RE_GOODPUT  = re.compile(r'goodput\s+([0-9.]+)\s*Mbps')
RE_TPREFIX  = re.compile(r'^t=([0-9.]+)\s')


def capture(ctx, ns, duration_s, raw_out):
    cmd = ["kubectl", "--context", ctx, "exec", "-n", ns, "deploy/oai-gnb", "-c", "gnb",
           "--", "true"]
    # We follow logs (not exec). Build the logs command instead.
    logs_cmd = ["kubectl", "--context", ctx, "logs", "-n", ns, "deploy/oai-gnb",
                "-c", "gnb", "-f", "--since=1s"]
    end = time.monotonic() + float(duration_s)
    with open(raw_out, "w", encoding="utf-8") as f:
        p = subprocess.Popen(logs_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, bufsize=1)
        try:
            for line in p.stdout:
                f.write("t=%.3f %s" % (time.monotonic(), line))
                if time.monotonic() >= end:
                    break
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


def parse(raw_out, rnti_map_path, roleslice_json, csv_out, json_out):
    # role<-rnti map
    rnti2role = {}
    try:
        with open(rnti_map_path, encoding="utf-8") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) >= 2:
                    rnti2role[parts[0].lower().lstrip("0x")] = parts[1]
    except FileNotFoundError:
        pass
    role2slice = json.loads(roleslice_json) if roleslice_json else {}

    # Per-RNTI running state and time-bucketed snapshots.
    cur = {}  # rnti -> counters
    series = []  # (t, rnti, dir, metric, mcs)
    cur_rnti = None
    t0 = None
    last_t = 0.0

    def norm(r):
        return r.lower().lstrip("0x")

    def st_get(rnti):
        return cur.setdefault(rnti, {"dl_r": 0, "ul_r": 0, "dl_b": 0, "ul_b": 0,
                                     "ul_nprb_sum": 0, "dl_gp_sum": 0.0, "ul_gp_sum": 0.0, "samples": 0})

    with open(raw_out, encoding="utf-8") as f:
        for raw in f:
            m = RE_TPREFIX.match(raw)
            t = float(m.group(1)) if m else last_t
            last_t = t
            if t0 is None:
                t0 = t
            line = raw[m.end():] if m else raw
            h = RE_RNTI_HDR.search(line)
            if h:
                cur_rnti = norm(h.group(1))
                st_get(cur_rnti)
                continue
            u = RE_UE_LINE.search(line)
            rnti = norm(u.group(1)) if u else cur_rnti
            if rnti is None:
                continue
            st = st_get(rnti)
            mb = RE_MAC_TXRX.search(line)
            if mb:
                st["dl_b"], st["ul_b"] = int(mb.group(1)), int(mb.group(2))
            db = RE_DL_BYTES.search(line)
            if db:
                st["dl_b"] = int(db.group(1))
            ub = RE_UL_BYTES.search(line)
            if ub:
                st["ul_b"] = int(ub.group(1))
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
                    if nm:
                        st["ul_nprb_sum"] += int(nm.group(1))
                    st["ul_gp_sum"] += gp
                    st["samples"] += 1
                    nprb = int(nm.group(1)) if nm else 0
                    series.append((round(t - t0, 3), rnti, "ul", nprb, mcs, gp))

    # CSV time series
    with open(csv_out, "w", encoding="utf-8") as f:
        f.write("t_rel_s,rnti,role,slice,dir,metric,mcs,goodput_mbps\n")
        for (t, rnti, direction, metric, mcs, gp) in series:
            role = rnti2role.get(rnti, "unknown")
            sl = role2slice.get(role, "?")
            f.write("%s,%s,%s,%s,%s,%d,%s,%.3f\n" % (t, rnti, role, sl, direction, metric,
                    mcs if mcs is not None else "", gp))

    per_role, per_slice = {}, {}
    for rnti, st in cur.items():
        role = rnti2role.get(rnti, "unknown")
        sl = role2slice.get(role, "?")
        per_role[role] = {"rnti": rnti, "slice": sl,
                          "ul_nprb_samples_sum": st["ul_nprb_sum"],
                          "dl_rounds_last": st["dl_r"], "ul_rounds_last": st["ul_r"],
                          "dl_goodput_mbps_sum": round(st["dl_gp_sum"], 3),
                          "ul_goodput_mbps_sum": round(st["ul_gp_sum"], 3),
                          "mac_dump_samples": st["samples"]}
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

    out = {
        "window_s": round((series[-1][0] - series[0][0]), 1) if series else 0,
        "dump_samples": len(series),
        "rnti_role_map": rnti2role,
        "role_slice_map": role2slice,
        "per_role": per_role,
        "per_slice": per_slice,
        "prb_proxy_note": ("OAI MAC dump (mac_log_level=info): UL NPRB per ~1s sample summed "
                           "by slice; DL/UL rounds and goodput as complementary scheduler load. "
                           "share_ul_nprb_pct = fraction of scheduled UL PRB samples."),
    }
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2); f.write("\n")
    return 0


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: mac_prb_tool.py capture|parse ...")
    mode = sys.argv[1]
    if mode == "capture":
        sys.exit(capture(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]))
    if mode == "parse":
        sys.exit(parse(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]))
    sys.exit("unknown mode %r" % mode)


if __name__ == "__main__":
    main()
PYTOOL
}

# Map each UE's C-RNTI -> role for slice attribution.
# Prefer live gNB "UE RNTI xxxx CU-UE-ID N" (last RNTI per CU); attach order:
# rogue[, rogue2, rogue3, rogue4], gcs, uav. UE logs are fallback only.
build_rnti_role_map() {
  local out="${CONFIG_DIR}/mac-rnti-role.map"
  : > "${out}"
  local role deploy rnti cu_id
  local -a roles=()
  for role in rogue rogue2 rogue3 rogue4 gcs uav; do
    if kubectl get deploy -n "${NS_RAN}" "oai-ue-${role}" --ignore-not-found -o name 2>/dev/null | grep -q .; then
      roles+=("${role}")
    fi
  done
  if [[ ${#roles[@]} -eq 0 ]]; then
    roles=(rogue gcs uav)
  fi
  # CU-UE-ID -> latest RNTI (awk keeps last)
  local tmp_cu
  tmp_cu="$(mktemp)"
  kubectl logs -n "${NS_RAN}" deploy/oai-gnb -c gnb --tail=12000 2>/dev/null \
    | grep -oE 'UE RNTI [0-9a-fA-F]{2,4} CU-UE-ID [0-9]+' \
    | awk '{r=tolower($3); gsub(/^0x/,"",r); while (length(r)<4) r="0" r; print $5, r}' \
    | awk '{cu[$1]=$2} END {for (c in cu) print c, cu[c]}' \
    | sort -n > "${tmp_cu}" || true
  local idx=0
  while read -r cu_id rnti; do
    [[ -n "${rnti}" && -n "${cu_id}" ]] || continue
    if [[ ${idx} -lt ${#roles[@]} ]]; then
      echo "${rnti} ${roles[${idx}]}" >> "${out}"
      idx=$((idx + 1))
    fi
  done < "${tmp_cu}"
  rm -f "${tmp_cu}"
  if [[ ! -s "${out}" ]]; then
    for role in "${roles[@]}"; do
      deploy="oai-ue-${role}"
      rnti="$(kubectl logs -n "${NS_RAN}" "deploy/${deploy}" -c nr-ue --tail=4000 --since=10m 2>/dev/null \
        | grep -oiE 'rnti[^0-9a-fx]*(0x)?[0-9a-f]{2,4}' | grep -oiE '[0-9a-f]{2,4}$' \
        | tail -n1 || true)"
      if [[ -n "${rnti}" ]]; then
        while [[ ${#rnti} -lt 4 ]]; do rnti="0${rnti}"; done
        echo "${rnti} ${role}" >> "${out}"
      fi
    done
  fi
  if [[ -s "${out}" ]]; then
    log "[06] RNTI→role map: $(wc -l < "${out}") UE(s) — ${out}"
  else
    log "[06] WARN: RNTI→role map empty — per-slice attribution will show 'unknown'"
  fi
}

start_mac_prb_capture() {
  need_cmd python3
  write_mac_prb_tool
  build_rnti_role_map
  local dur=$(( OWD_PROBE_DURATION + OWD_PROBE_WARMUP + 20 ))
  local raw="${CONFIG_DIR}/mac-prb-raw.log"
  log "[06] Proof-of-mechanism — capturing gNB MAC scheduler dumps for ${dur}s → ${raw}"
  python3 "${CONFIG_DIR}/mac_prb_tool.py" capture "${KUBE_CONTEXT}" "${NS_RAN}" "${dur}" "${raw}" &
  MAC_PRB_CAP_PID=$!
}

stop_mac_prb_capture() {
  [[ -n "${MAC_PRB_CAP_PID}" ]] || return 0
  if kill -0 "${MAC_PRB_CAP_PID}" 2>/dev/null; then
    wait "${MAC_PRB_CAP_PID}" 2>/dev/null || true
  fi
  MAC_PRB_CAP_PID=""
  local raw="${CONFIG_DIR}/mac-prb-raw.log"
  local csv="${CONFIG_DIR}/mac-prb-timeseries.csv"
  local json="${CONFIG_DIR}/mac-prb-evidence.json"
  local roleslice
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    roleslice='{"rogue":"A","gcs":"A","uav":"A"}'
  else
    roleslice='{"rogue":"A","gcs":"B","uav":"B"}'
  fi
  if [[ ! -s "${raw}" ]]; then
    log "[06] WARN: no MAC dump captured (raw empty) — is MAC_log_level=info on the gNB? Proof-of-mechanism skipped."
    return 0
  fi
  python3 "${CONFIG_DIR}/mac_prb_tool.py" parse "${raw}" \
    "${CONFIG_DIR}/mac-rnti-role.map" "${roleslice}" "${csv}" "${json}" || {
      log "[06] WARN: MAC PRB parse failed — inspect ${raw}"; return 0; }
  log "[06] Proof-of-mechanism saved: ${json} (per-slice share) + ${csv} (time series)"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "${json}" <<'PY' || true
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for sl, s in sorted(d.get("per_slice", {}).items()):
    print("[06]   slice %s: UL-NPRB %.1f%% | UL-rounds %.1f%% | DL-rounds %.1f%%"
          % (sl, s.get("share_ul_nprb_pct", 0), s.get("share_ul_rounds_pct", 0),
             s.get("share_dl_rounds_pct", 0)))
PY
  fi
}

# Records host CPU load so single-host Kind contention is a documented confounder,
# not a hidden one. Appends a labelled line to a per-run artifact.
capture_host_load() {
  local phase="$1" out="${CONFIG_DIR}/host-load-under-attack.log"
  local la nc avg1
  la="$(awk '{print $1" "$2" "$3}' /proc/loadavg 2>/dev/null || echo '? ? ?')"
  nc="$(nproc 2>/dev/null || echo '?')"
  avg1="${la%% *}"
  HOST_LOAD_NPROC="${nc}"
  case "${phase}" in
    before-probe) HOST_LOAD_BEFORE_AVG1="${avg1}" ;;
    after-probe)  HOST_LOAD_AFTER_AVG1="${avg1}" ;;
  esac
  echo "${phase}: loadavg(1/5/15m)=${la} nproc=${nc} ts=$(date -Is)" >> "${out}"
  log "[06] host load (${phase}): loadavg1=${avg1} nproc=${nc} → ${out}"
}

# Merge flood knobs + host load into owd-ipdv-probe.json for calibration sweeps.
augment_under_attack_probe_json() {
  local json="${CONFIG_DIR}/owd-ipdv-probe.json"
  [[ -f "${json}" ]] || return 0
  need_cmd python3
  python3 - "${json}" \
    "${ROGUE_FLOOD_MBPS}" "${ROGUE_FLOOD_PARALLEL}" \
    "${MIN_RATIO_SLICE_A}" "${MIN_RATIO_SLICE_B}" \
    "${HOST_LOAD_BEFORE_AVG1}" "${HOST_LOAD_AFTER_AVG1}" "${HOST_LOAD_NPROC}" \
    "${HOST_LOAD_MAX_AVG1}" \
    "${ROGUE_FLOOD_MODE}" "${ROGUE_PULSE_BOLUS_KB}" "${ROGUE_PULSE_PERIOD_MS}" "${ROGUE_PULSE_PKT_BYTES}" "${ROGUE_PULSE_FLOWS}" "${ROGUE_FLOOD_DIR}" \
    "${PIN_CORES}" "gnb=${PIN_GNB};gcs=${PIN_GCS};uav=${PIN_UAV};rogue=${PIN_ROGUE};attack=${PIN_ATTACK}" <<'PY'
import json, sys

(path, mbps, parallel, min_a, min_b, la_b, la_a, nproc, la_max,
 mode, bolus_kb, period_ms, pkt_bytes, pulse_flows, flood_dir) = sys.argv[1:16]
pin_cores = sys.argv[16] if len(sys.argv) > 16 else "0"
pin_layout = sys.argv[17] if len(sys.argv) > 17 else ""
with open(path, encoding="utf-8") as f:
    d = json.load(f)
ipdv_p99_9 = (d.get("ipdv_us") or {}).get("p99_9")
try:
    la_after = float(la_a)
    la_max_f = float(la_max)
    contaminated = la_after > la_max_f
except (TypeError, ValueError):
    la_after = None
    contaminated = None
attack = {"mode": mode}
if mode == "pulse":
    bolus_b = int(bolus_kb) * 1024
    period_s = float(period_ms) / 1000.0
    attack["pulse"] = {
        "bolus_kb": int(bolus_kb),
        "period_ms": float(period_ms),
        "pkt_bytes": int(pkt_bytes),
        "flows": int(pulse_flows),
        "avg_mbps": round(bolus_b * 8 / (period_s * 1e6), 2),
        "drain_ms_at_50mbps": round(bolus_b * 8 / 50e3, 1),
    }
else:
    attack["cbr"] = {"mbps": int(mbps), "parallel": int(parallel), "direction": flood_dir}
d["under_attack"] = {
    "attack": attack,
    "rogue_flood_mbps": int(mbps),
    "rogue_flood_parallel": int(parallel),
    "min_ratio_slice_a": int(min_a),
    "min_ratio_slice_b": int(min_b),
    "host_load": {
        "nproc": int(nproc) if str(nproc).isdigit() else nproc,
        "before_probe_loadavg1": la_b,
        "after_probe_loadavg1": la_a,
        "max_loadavg1_threshold": la_max,
        "cpu_contamination_suspected": contaminated,
        "note": "Single-host Kind — high loadavg1 may inflate IPDV; prefer after_probe_loadavg1 <= threshold",
    },
    "cpu_isolation": {
        "pin_cores_enabled": pin_cores == "1",
        "layout": pin_layout,
        "note": "PIN_CORES=1 taskset affinity separates measured path (gNB+GCS+UAV) from attacker cores; CPU affinity reduces single-host scheduling interference as a confounder",
    },
    "calibration": {
        "ipdv_p99_9_ms": round(ipdv_p99_9 / 1000.0, 3) if ipdv_p99_9 is not None else None,
        "do377a_ipdv_p99_9_violated": ipdv_p99_9 > 20000 if ipdv_p99_9 is not None else None,
    },
}
# Fold in per-slice PRB proof-of-mechanism if captured this run.
import os
prb_path = os.path.join(os.path.dirname(path), "mac-prb-evidence.json")
if os.path.exists(prb_path):
    try:
        with open(prb_path, encoding="utf-8") as pf:
            prb = json.load(pf)
        d["under_attack"]["prb_evidence"] = {
            "per_slice": prb.get("per_slice"),
            "per_role": prb.get("per_role"),
            "dump_samples": prb.get("dump_samples"),
            "note": prb.get("prb_proxy_note"),
        }
    except (ValueError, OSError):
        pass
with open(path, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
PY
  if [[ -n "${HOST_LOAD_AFTER_AVG1}" ]] && command -v python3 >/dev/null 2>&1; then
    if python3 - "${HOST_LOAD_AFTER_AVG1}" "${HOST_LOAD_MAX_AVG1}" <<'PY'
import sys
la, mx = float(sys.argv[1]), float(sys.argv[2])
sys.exit(0 if la <= mx else 1)
PY
    then
      log "[06] host load OK — loadavg1=${HOST_LOAD_AFTER_AVG1} <= ${HOST_LOAD_MAX_AVG1} (no CPU saturation flag)"
    else
      log "[06] WARN: host loadavg1=${HOST_LOAD_AFTER_AVG1} > ${HOST_LOAD_MAX_AVG1} — CPU confounder likely; reduce -P or -b and recalibrate"
    fi
  fi
}

# =============================================================================
# SITL — ArduPilot C2 mission (always 5G PDU path; pymavlink lib in ../03_sitl/)
# =============================================================================
SITL_PID=""

sitl_cleanup() {
  [[ -n "${SITL_PID:-}" ]] && kill "${SITL_PID}" 2>/dev/null || true
  pkill -f "sim_vehicle.py.*ArduCopter" 2>/dev/null || true
  pkill -f "build/sitl/bin/arducopter" 2>/dev/null || true
  sleep 2
}

ensure_sitl_prereqs() {
  need_cmd python3
  need_cmd ss
  [[ -f "${SITL_MISSION_PY}" ]] \
    || die "Missing ${SITL_MISSION_PY} — sitl/ artifact tree incomplete"
  mkdir -p "${VENDOR_DIR}"
  if [[ ! -x "${ARDUPILOT_ROOT}/build/sitl/bin/arducopter" ]]; then
    log "[SITL] ArduPilot not ready — install (runtime tarball or clone+build) → ${ARDUPILOT_ROOT}…"
    if ! SITL_ROOT="${ARDUPILOT_ROOT}" BUILD_LOG="${CONFIG_DIR}/ardupilot-build.log" \
         bash "${SITL_DIR}/install_ardupilot.sh"; then
      log "[SITL] BUILD FAILED — last 50 lines of ${CONFIG_DIR}/ardupilot-build.log:"
      tail -50 "${CONFIG_DIR}/ardupilot-build.log" 2>/dev/null || true
      die "ArduPilot SITL build failed — full log: ${CONFIG_DIR}/ardupilot-build.log"
    fi
    log "[SITL] ArduPilot build OK — ${ARDUPILOT_ROOT}/build/sitl/bin/arducopter"
  fi
  [[ -x "${ARDUPILOT_ROOT}/build/sitl/bin/arducopter" ]] \
    || die "ArduPilot SITL binary missing at ${ARDUPILOT_ROOT}"
  export PATH="${ARDUPILOT_ROOT}/Tools/autotest:${PATH}"
}

netns_run_deploy() {
  kind_netns_run "$@"
}

wait_sitl_listen_netns() {
  local deploy="$1" port="$2" pid="$3" logfile="$4"
  local i ready=0
  for (( i = 1; i <= 120; i++ )); do
    if netns_run_deploy "${deploy}" -- ss -ltn 2>/dev/null | grep -q ":${port} "; then
      log "[SITL] ${deploy}: TCP ${port} listening (${i}s)"
      ready=1
      break
    fi
    if [[ -n "${pid}" ]] && ! kill -0 "${pid}" 2>/dev/null; then
      log "[SITL] ${deploy} SITL died — tail ${logfile}:"
      tail -30 "${logfile}" 2>/dev/null || true
      die "SITL in ${deploy} failed (see ${logfile})"
    fi
    sleep 1
  done
  [[ "${ready}" -eq 1 ]] || die "${deploy}: SITL TCP ${port} not listening within 120s"
}

ensure_ran_for_sitl() {
  kubectl --context "${KUBE_CONTEXT}" get deploy -n "${NS_RAN}" oai-ue-gcs oai-ue-uav >/dev/null 2>&1 \
    || die "RAN UEs not deployed. Run ./start_testbed_mitigation.sh first."
  get_ue_pdu_ipv4 oai-ue-gcs >/dev/null \
    || die "GCS PDU IP missing — wait for PDU attach or rerun --phase ran"
  get_ue_pdu_ipv4 oai-ue-uav >/dev/null \
    || die "UAV PDU IP missing — wait for PDU attach or rerun --phase ran"
}

run_sitl_uav_host() {
  local uav_ip i ready=0 sitl_reason
  ensure_ran_for_sitl
  uav_ip="$(get_ue_pdu_ipv4 oai-ue-uav)" || die "UAV PDU IP missing"
  verify_kind_vendor_mount \
    || die "ArduPilot vendor dir not mounted in Kind node. Recreate cluster: ./stop_testbed.sh && ./start_testbed_mitigation.sh"
  log "[SITL] Sidecar in oai-ue-uav pod — UDP C2 :${SITL_TCP_PORT} -> internal TCP SITL :${SITL_INTERNAL_PORT} (UAV PDU ${uav_ip})"
  log "[SITL] C2 path: GCS pod → UPF/GTP → UAV TUN → UDP C2 → socat → TCP SITL backend (5G only, no loopback)"
  for (( i = 1; i <= 120; i++ )); do
    if kubectl exec -n "${NS_RAN}" deploy/oai-ue-uav -c nr-ue -- \
         ss -uln 2>/dev/null | grep -q ":${SITL_TCP_PORT} "; then
      log "[SITL] OK — UDP ${SITL_TCP_PORT} listening in UAV pod netns (relay -> TCP ${SITL_INTERNAL_PORT})"
      if [[ "${ACK_PROBE_SOCAT}" == "1" ]]; then
        if kubectl exec -n "${NS_RAN}" deploy/oai-ue-uav -c nr-ue -- \
             ss -uln 2>/dev/null | grep -q ":${ACK_PROBE_UDP_PORT} "; then
          log "[SITL] OK — ACK probe relay UDP ${ACK_PROBE_UDP_PORT} -> TCP ${ACK_PROBE_SITL_TCP} (required by FULL sidecars; C2 :${SITL_TCP_PORT} untouched)"
        else
          log "[SITL] WARN — ACK probe relay UDP ${ACK_PROBE_UDP_PORT} not listening; FULL sidecars (command_ack_rtt) would have no path"
        fi
      fi
      ready=1
      break
    fi
    sitl_reason="$(kubectl get pod -n "${NS_RAN}" -l app=oai-ue-uav \
      -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="sitl")].state.waiting.reason}' 2>/dev/null || true)"
    if [[ "${sitl_reason}" == "CrashLoopBackOff" || "${sitl_reason}" == "Error" ]]; then
      log "[SITL] sidecar failed (${sitl_reason}) — logs:"
      kubectl logs -n "${NS_RAN}" deploy/oai-ue-uav -c sitl --tail=40 2>/dev/null || true
      die "SITL sidecar failed (kubectl logs -n ${NS_RAN} deploy/oai-ue-uav -c sitl)"
    fi
    if (( i == 1 || i % 15 == 0 )); then
      log "[SITL] waiting for UDP C2 ${SITL_TCP_PORT} (internal TCP SITL ${SITL_INTERNAL_PORT})… (${i}/120s)"
    fi
    sleep 1
  done
  [[ "${ready}" -eq 1 ]] || {
    log "[SITL] sidecar did not open UDP ${SITL_TCP_PORT} in 120s — logs:"
    kubectl logs -n "${NS_RAN}" deploy/oai-ue-uav -c sitl --tail=30 2>/dev/null || true
    die "SITL sidecar failed (kubectl logs -n ${NS_RAN} deploy/oai-ue-uav -c sitl)"
  }
}

# When a recent OWD probe exists, merge network + C2 + physics into one JSON.
_sitl_merge_network_probe() {
  local art="$1"
  local probe="${CONFIG_DIR}/owd-ipdv-probe.json"
  [[ -f "${probe}" && -f "${art}/mission_summary.json" ]] || return 0
  cp "${probe}" "${art}/network_probe.json"
  python3 - "${art}" <<'PY' || true
import json, sys
from pathlib import Path
art = Path(sys.argv[1])
summary = json.loads((art / "mission_summary.json").read_text())
net = json.loads((art / "network_probe.json").read_text())
merged = {
    "run_id": art.name,
    "layers": {
        "network": {
            "owd_us": net.get("owd_us"),
            "ipdv_us": net.get("ipdv_us"),
            "availability": net.get("availability"),
            "under_attack": net.get("under_attack"),
        },
        "c2": summary.get("c2_metrics"),
        "physics": summary.get("physics_metrics"),
    },
}
(art / "three_layer_summary.json").write_text(json.dumps(merged, indent=2) + "\n")
print("Wrote", art / "three_layer_summary.json")
PY
}

print_tm_c2_cookbook() {
  log "========================================"
  log " FlyBlind cookbook — ./start_testbed_mitigation.sh [cross] [B0|B60|B100|…]"
  log " SITL sidecar: kubectl logs -n ${NS_RAN} deploy/oai-ue-uav -c sitl"
  log " 5QI default: ROGUE=${ROGUE_5QI} C2=${C2_5QI}  |  ablation: EQUAL_5QI=1 ./start_testbed_mitigation.sh"
  log " RAN-hard control (shared-UPF ablation): ./stop_testbed.sh && ./start_testbed_mitigation.sh cross B100"
  log "========================================"
  log " 0) PDU IPs:"
  log "    kubectl exec -n ${NS_RAN} deploy/oai-ue-uav -c nr-ue -- ip -4 -o addr show dev oaitun_ue1"
  log "    kubectl exec -n ${NS_RAN} deploy/oai-ue-gcs -c nr-ue -- ip -4 -o addr show dev oaitun_ue1"
  log " 1) Mission (GCS pod — C2 over 5G):"
  log "    kubectl exec -it -n ${NS_RAN} deploy/oai-ue-gcs -c nr-ue -- bash"
  log "    export UAV_TUN=<UAV_PDU_IP>"
  log "    /tmp/check_c2_5g.sh \$UAV_TUN"
  log "    /tmp/run_mission_120.sh \$UAV_TUN    # ~120s, ~25 nav cmds (commands.csv + FLIGHT LOG)"
  log "    /tmp/run_mission_420.sh \$UAV_TUN    # ~7 min, ~72 nav cmds"
  log " 2) OWD/IPDV under attack (3 terminals, same duration D):"
  log "    T1 UAV:  kubectl exec -it -n ${NS_RAN} deploy/oai-ue-uav -c nr-ue -- /tmp/run_owd_recv.sh D"
  log "    T2 GCS:  kubectl exec -it -n ${NS_RAN} deploy/oai-ue-gcs -c nr-ue -- /tmp/run_owd_send.sh \$UAV_TUN D"
  log "    T3 ROGUE: kubectl exec -it -n ${NS_RAN} deploy/oai-ue-rogue -c nr-ue -- /tmp/run_attack_pulse.sh D"
  log "    metrics: /tmp/owd-ipdv-probe.json (UAV pod) + mission_summary.json (GCS pod)"
  log " 3) Attack only (ROGUE pod):"
  log "    kubectl exec -it -n ${NS_RAN} deploy/oai-ue-rogue -c nr-ue -- /tmp/run_attack_pulse.sh 190"
  log "    kubectl exec -it -n ${NS_RAN} deploy/oai-ue-rogue -c nr-ue -- /tmp/run_attack_flood_c2.sh 190 moderate \$UAV_TUN"
  log " 4) Copy logs:"
  log "    kubectl exec -n ${NS_RAN} deploy/oai-ue-uav -c nr-ue -- cat /tmp/owd-ipdv-probe.json"
  log "    kubectl exec -n ${NS_RAN} deploy/oai-ue-gcs -c nr-ue -- tar cf - -C /tmp . | tar xf - -C ."
  log "========================================"
}

run_validate_gates() {
  log "========================================"
  if [[ "${VALIDATE_UNDER_ATTACK}" == "1" ]]; then
    log " Phase: validate-under-attack (C2 OWD under script-driven ROGUE flood)"
  else
    log " Phase: validate"
  fi
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    log " Topology: intra | 1 slice ${SST}/${SD} | MinRatio=${MIN_RATIO_SLICE_A} | GNB_PRB=${GNB_PRB}"
  else
    log " Topology: cross | slice A=${SST}/${SD} B=${SST_B}/${SD_B} | MinRatio A=${MIN_RATIO_SLICE_A} B=${MIN_RATIO_SLICE_B} | GNB_PRB=${GNB_PRB}"
  fi
  log "========================================"
  kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach cluster ${KUBE_CONTEXT}"
  ensure_topology_coherent
  verify_gnb_snssai_count
  for role in gcs uav; do
    wait_for_ue_oai_iface "${role}" 120
  done
  for role in "${UE_ROLES[@]}"; do
    verify_ue_pdu_profile "${role}"
  done
  print_clock_methodology
  validate_min_ratio_prb
  verify_per_ue_qos_in_mysql
  if [[ "${VALIDATE_UNDER_ATTACK}" == "1" ]]; then
    launch_rogue_attack_flood
    start_mac_prb_capture
    capture_host_load "before-probe"
    run_owd_ipdv_probe
    capture_host_load "after-probe"
    stop_mac_prb_capture
    augment_under_attack_probe_json
    stop_rogue_attack_flood
  else
    run_owd_ipdv_probe
  fi
  capture_mac_5qi_evidence
  if [[ "${VALIDATE_UNDER_ATTACK}" == "1" ]]; then
    log "[06] Skipping rfsim ceiling probe (validate-under-attack — flood already exercised)"
  else
    measure_rfsim_throughput_ceiling
  fi
  if [[ "${VALIDATE_UNDER_ATTACK}" == "1" ]]; then
    log "[OK] validate-under-attack complete — artifacts in ${CONFIG_DIR}/"
  else
    log "[OK] validate complete — artifacts in ${CONFIG_DIR}/"
  fi
}

# =============================================================================
# Main
# =============================================================================
main() {
  local deploy_t0 deploy_elapsed _rf_mhz
  deploy_t0="$(date +%s)"

  log "========================================"
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    log " IEEE S&P 2027 FlyBlind — intra ${PRB_LABEL} (1 slice, PRB contention)"
    log " Core: MySQL + AMF + SMF + UPF + ext-DN (1 slice, DNN ${DNN})"
    log " RAN : ${RAN_FLEET_LABEL}"
    log " Topology: intra | slice ${SST}/${SD} | min_ratio=${MIN_RATIO_SLICE_A} (ORANSlice)"
    log " Note: intra MinRatio partitions one slice — no GCS/UAV vs ROGUE slice split"
  elif [[ "${VICTIM_IN_EMBB}" == "1" ]]; then
    log " IEEE S&P 2027 FlyBlind — cross ${PRB_LABEL} + VICTIM_IN_EMBB (victim co-located in eMBB; slice B idle)"
    log " Core: MySQL + AMF + SMF + UPF + ext-DN (slice A active, slice B provisioned but idle, 1 UPF)"
    log " L3 isolation: UE↔UE DROP inside ${SUBNET_SLICE_A}; only GCS↔UAV allowed (real-network UE isolation)"
    log " RAN : ${RAN_FLEET_LABEL}"
    log " Topology: cross | slice A (eMBB): ${SST}/${SD} [GCS/UAV + ${ROGUE_WORD}] | slice B: ${SST_B}/${SD_B} [idle, no UE]"
    log " rrmPolicyRatio: min_ratio A=${MIN_RATIO_SLICE_A} B=${MIN_RATIO_SLICE_B}"
    log " 5QI: ${ROGUE_WORD}=${ROGUE_5QI} victim=${VICTIM_5QI} (co-located common user; EQUAL_5QI=${EQUAL_5QI})"
  else
    log " IEEE S&P 2027 FlyBlind — cross ${PRB_LABEL} (2 slices, PRB boundary at gNB)"
    log " Core: MySQL + AMF + SMF + UPF + ext-DN (2 slices, 1 UPF + L3 DROP A↔B)"
    log " L3 isolation: DROP between the 2 slices (${ROGUE_WORD}↛C2 PDU; GCS↔UAV OK)"
    log " RAN : ${RAN_FLEET_LABEL}"
    log " Topology: cross | slice A: ${SST}/${SD} | slice B: ${SST_B}/${SD_B}"
    log " rrmPolicyRatio: min_ratio A=${MIN_RATIO_SLICE_A} B=${MIN_RATIO_SLICE_B}"
    log " 5QI: ${ROGUE_WORD}=${ROGUE_5QI} C2=${C2_5QI} (EQUAL_5QI=${EQUAL_5QI})"
  fi
  log " Kind cluster: ${CLUSTER_NAME}"
  log " Context     : ${KUBE_CONTEXT}"
  _rf_mhz="$(gnb_prb_mhz_label "${GNB_PRB}")"
  log " RF carrier  : band 78, numerology 1, GNB_PRB=${GNB_PRB} (${_rf_mhz})"
  log "========================================"

  mkdir -p "${CONFIG_DIR}"

  ensure_oranslice_images
  ensure_kind_cluster
  load_oranslice_images_into_kind
  create_namespaces

  log "========================================"
  log " Phase: core"
  log "========================================"
  prepare_oai_artifacts
  deploy_oai_core
  verify_oai_core_health

  log "========================================"
  log " Phase: ran"
  log "========================================"
  ensure_core_for_ran
  ensure_sitl_prereqs
  prepare_oai_artifacts
  deploy_oai_ran
  verify_oai_ran_health

  log "========================================"
  log " Phase: sitl"
  log "========================================"
  run_sitl_uav_host

  log "========================================"
  log " Phase: final L3 separation test"
  log "========================================"
  run_final_l3_slice_separation_test

  log "========================================"
  log " Done — topology=${TOPOLOGY} ${PRB_LABEL} MinRatio A=${MIN_RATIO_SLICE_A} B=${MIN_RATIO_SLICE_B}"
  if [[ "${TOPOLOGY}" == "intra" ]]; then
    log " 1 slice: SST=${SST} SD=${SD} DNN=${DNN}"
  else
    log " Slice A / ROGUE: ${SST}/${SD} ${DNN}"
    log " Slice B / C2:    ${SST_B}/${SD_B} ${DNN_B}"
  fi
  log " To stop the testbed: ./stop_testbed.sh"
  log "========================================"
  deploy_elapsed="$(( $(date +%s) - deploy_t0 ))"
  log "[DEPLOYMENT DURATION]: ${deploy_elapsed}s"
}

main "$@"
exit 0

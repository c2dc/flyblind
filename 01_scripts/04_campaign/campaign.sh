#!/usr/bin/env bash
##################################################################################################################################
# Script: campaign.sh (FlyBlind full campaign driver: brings the testbed up/down and runs every condition)                       #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Single-file, unattended driver for the whole intra-slice campaign (65 runs over 5 series). For each series it     #
# tears the testbed down to a verified-clean state, waits for host load to settle, brings the required configuration up,         #
# verifies it (pods Ready, topology stamp, SITL failsafe parameters, C2 socat), and then runs that series' conditions through    #
# batch_wrapper.py with --continue-on-invalid. A series that cannot be brought up is SKIPPED, never fatal to the campaign.       #
# Condition lists are generated inline (no external files) and archived with the results for provenance.                         #
#                                                                                                                                #
# SAFETY: nothing touches the testbed unless --go is given. The default is a DRY RUN that validates the plan, the generated      #
# condition files and the wrapper invocation, writing logs under /tmp.                                                           #
#                                                                                                                                #
# Usage:                                                                                                                         #
#   ./campaign.sh                 # DRY RUN: print and validate the plan (no testbed command is executed)                        #
#   ./campaign.sh --go            # EXECUTE the full campaign (~18 h; asks the sudo password once)                               #
#   ./campaign.sh --go --only=S1_core,S4_slicing      # execute only the named series                                            #
#   ./campaign.sh --list          # list the series and their run counts                                                         #
#                                                                                                                                #
# Env knobs: SIDECARS=off|MINIMAL|FULL (default FULL: per-leg OWD + ACK RTT + RLC + UE iface), MIN_FREE_GB (default 10),         #
#            SETTLE_LOAD1 (default 6.0), SETTLE_MAX_S (default 900), PODS_READY_MAX_S (default 900), PYTHON (default python3),   #
#            POSCTL_CUT_AT_S (default 300) and POSCTL_CUT_S (default 10) for the S6 positive control.                            #
##################################################################################################################################

# Deliberately NOT -e: a failing series must not abort the campaign.
set -uo pipefail

# --- Paths -------------------------------------------------------------------------------------------------------------------
CAMPAIGN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "${CAMPAIGN_DIR}/.." && pwd)"
ARTIFACTS_DIR="$(cd "${SCRIPTS_DIR}/.." && pwd)"
TESTBEDS_DIR="${SCRIPTS_DIR}/01_testbeds"
EXP_DIR="${SCRIPTS_DIR}/02_experiments"
WRAPPER="${EXP_DIR}/instrumentation/batch_wrapper.py"
RESULTS_DIR="${ARTIFACTS_DIR}/02_results"
PY="${PYTHON:-python3}"

# --- Single stack: both start scripts share this cluster, context and stamp ---------------------------------------------------
CLUSTER_RE='^tm-oranslice'
KUBE_CTX="kind-tm-oranslice-mitigation"
STAMP="${TESTBEDS_DIR}/runtime-mitigation/topology.env"

# --- Tunables ----------------------------------------------------------------------------------------------------------------
MIN_FREE_GB="${MIN_FREE_GB:-10}"           # abort a series below this free disk space (65 runs ~ 2.8 GB + logs)
SETTLE_LOAD1="${SETTLE_LOAD1:-6.0}"        # wait for host load1 below this before start and before the runs
SETTLE_MAX_S="${SETTLE_MAX_S:-900}"        # give up waiting after this (orchestrate host gates still apply)
PODS_READY_MAX_S="${PODS_READY_MAX_S:-900}"
SIDECARS="${SIDECARS:-FULL}"            # FULL = per-leg OWD (command+return) + host load + ACK RTT + RLC + UE iface
IMAGE_TAG="${ORANSLICE_IMAGE_TAG:-2024w28}"
NS_RAN="${NS_RAN:-ran}"

# Positive control (S6): when to cut the command leg, and for how long. The cut must land in steady flight --
# the route run reaches it around t_rel 136 s and the landing command goes out around t_rel 705 s, so 300 s is
# comfortably inside with margin on both sides. The cut must also outlast FS_GCS_TIMEOUT (5 s, asserted by
# verify_stack), hence 10 s.
POSCTL_CUT_AT_S="${POSCTL_CUT_AT_S:-300}"
POSCTL_CUT_S="${POSCTL_CUT_S:-10}"
POSCTL_TEST_DST="203.0.113.1"           # RFC 5737 TEST-NET-3: used only to probe that we may add a blackhole route
REQUIRED_IMAGES=(
  "localhost/oai-gnb-oranslice-mitigation:${IMAGE_TAG}"
  "localhost/oai-nr-ue-oranslice-mitigation:${IMAGE_TAG}"
)

# Campaign-invariant orchestrate flags. Everything else (106 PRB, cross, 600 s, warmup 10, route, 4 m/s,
# 12 Mb/s aviator video, deviation 300, B0, repeat 1) is already the default inside orchestrate.py.
COMMON_ARGS=""
[[ "${SIDECARS}" != "off" ]] && COMMON_ARGS="--sidecars ${SIDECARS}"

GO=0
LIST_ONLY=0
ONLY_SERIES=""
NIGHT_ROOT=""
NIGHT_LOG=""
SUMMARY_MD=""
COND_OUT=""
PRERUN_HOOK=""            # optional function name; series() runs it in the background after verify, before the runs
declare -a SERIES_RESULTS=()

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

ts() { date '+%F %T'; }
log() { echo "[$(ts)] $*" | tee -a "${NIGHT_LOG:-/dev/null}"; }

parse_args() {
  local a
  for a in "$@"; do
    case "$a" in
      --go) GO=1 ;;
      --list) LIST_ONLY=1 ;;
      --only=*) ONLY_SERIES="${a#*=}" ;;
      --only) echo "use --only=S1_core,S4_slicing" >&2; exit 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "unknown argument: $a" >&2; usage; exit 2 ;;
    esac
  done
}

selected() {  # $1 = series id; empty --only means "all"
  [[ -z "$ONLY_SERIES" ]] && return 0
  [[ ",${ONLY_SERIES}," == *",$1,"* ]]
}

# --- Night bookkeeping -------------------------------------------------------------------------------------------------------
init_night() {
  local stamp; stamp="$(date '+%Y%m%d_%H%M%S')"
  if [[ "$GO" == "1" ]]; then
    NIGHT_ROOT="${RESULTS_DIR}/campaign/flyblind_${stamp}"
  else
    NIGHT_ROOT="/tmp/flyblind_campaign_dryrun/flyblind_${stamp}"
  fi
  COND_OUT="${NIGHT_ROOT}/conditions"
  mkdir -p "$COND_OUT"
  NIGHT_LOG="${NIGHT_ROOT}/campaign.log"
  SUMMARY_MD="${NIGHT_ROOT}/campaign_summary.md"
  echo "# FlyBlind campaign summary (${stamp})" >"$SUMMARY_MD"
  log "=== FlyBlind campaign | mode=$([[ "$GO" == "1" ]] && echo EXECUTE || echo DRY-RUN) | root=${NIGHT_ROOT}"
  log "    sidecars=${SIDECARS} | min_free=${MIN_FREE_GB}GB | settle_load1=${SETTLE_LOAD1}"
  [[ "$GO" == "1" ]] || log "DRY-RUN: no testbed command is executed. Re-run with --go to execute."
}

# Run a testbed-touching command only when GO=1. Output goes to a per-series log file.
act() {  # $1 = log file, rest = command
  local lf="$1"; shift
  if [[ "$GO" == "1" ]]; then
    log "RUN: $*  (log: ${lf})"
    "$@" >>"$lf" 2>&1
    local rc=$?
    log "rc=${rc}"
    return $rc
  fi
  log "DRY-RUN would run: $*"
  return 0
}

# --- sudo: ask once, keep the ticket alive for the whole campaign -------------------------------------------------------------
SUDO_KEEPALIVE_PID=""
sudo_init() {
  if [[ "$GO" != "1" ]]; then
    log "DRY-RUN would ask the sudo password once (sudo -v) and keep it alive in background"
    return 0
  fi
  log "sudo: asking the password once; kept alive in background for the whole campaign"
  if ! sudo -v; then log "ABORT: sudo authentication failed"; exit 1; fi
  if ! sudo -n true 2>/dev/null; then
    log "ABORT: sudo does not cache the authentication (timestamp_timeout=0?); a prompt could block the campaign"
    exit 1
  fi
  ( while kill -0 "$$" 2>/dev/null; do sudo -n -v 2>/dev/null; sleep 50; done ) &
  SUDO_KEEPALIVE_PID=$!
  trap '[[ -n "${SUDO_KEEPALIVE_PID}" ]] && kill "${SUDO_KEEPALIVE_PID}" 2>/dev/null' EXIT
  log "sudo: OK (keep-alive pid ${SUDO_KEEPALIVE_PID})"
}
sudo_check() {
  [[ "$GO" == "1" ]] || return 0
  if sudo -n true 2>/dev/null; then log "sudo: ticket still valid"
  else log "WARN: sudo ticket expired; a sudo call inside the start script would wait for a password"; fi
}

# --- Process / disk / load helpers -------------------------------------------------------------------------------------------
# Only real campaign processes count: the executable must be a Python interpreter, kubectl or iperf3. Shells or editors that
# merely mention these names in their command line are ignored (avoids false "leftover" detections between series).
_procs_matching() {  # $1 = extended regex matched against the full command line
  pgrep -af "$1" 2>/dev/null | awk '{ n = split($2, p, "/"); b = p[n];
    if (b ~ /^python[0-9.]*$/ || b == "kubectl" || b == "iperf3") print }' || true
}
campaign_procs() { _procs_matching 'orchestrate\.py|batch_wrapper\.py|run_sidecars\.py'; }
leftover_procs() {
  _procs_matching 'orchestrate\.py|run_sidecars\.py|leg_owd_probe\.py|c2_arrival_forwarder\.py|mavlink_proxy\.py|iperf3'
}

free_gb() { df -BG --output=avail "$ARTIFACTS_DIR" | tail -1 | tr -dc '0-9'; }
check_disk() {
  local f; f="$(free_gb)"
  if (( f < MIN_FREE_GB )); then log "FAIL: ${f} GB free < ${MIN_FREE_GB} GB"; return 1; fi
  log "disk free: ${f} GB"
  return 0
}

settle_load() {
  local t0=$SECONDS l1
  if [[ "$GO" != "1" ]]; then
    log "DRY-RUN would wait for load1 < ${SETTLE_LOAD1} (now $(cut -d' ' -f1 /proc/loadavg))"; return 0
  fi
  while :; do
    l1="$(cut -d' ' -f1 /proc/loadavg)"
    if awk -v a="$l1" -v b="$SETTLE_LOAD1" 'BEGIN{exit !(a<b)}'; then
      log "host load1=${l1} < ${SETTLE_LOAD1} (settled)"; return 0
    fi
    if (( SECONDS - t0 >= SETTLE_MAX_S )); then
      log "WARN: load1=${l1} still >= ${SETTLE_LOAD1} after ${SETTLE_MAX_S}s; continuing (orchestrate host gates still apply)"
      return 0
    fi
    sleep 15
  done
}

# --- Preflight ---------------------------------------------------------------------------------------------------------------
preflight() {
  local bad=0 out img
  out="$(campaign_procs)"
  if [[ -n "$out" ]]; then
    # Hard failure only when executing: two concurrent campaigns would fight over the testbed.
    # A dry run touches nothing, so it only warns (lets the plan be validated while a run is in flight).
    if [[ "$GO" == "1" ]]; then log "FAIL: another campaign process is running:"; log "$out"; bad=1
    else log "WARN (dry-run only): another campaign process is running:"; log "$out"; fi
  fi
  check_disk || bad=1
  for img in "${REQUIRED_IMAGES[@]}"; do
    if docker image inspect "$img" >/dev/null 2>&1; then log "image OK: $img"
    else log "FAIL: image missing: $img (build it once: 01_testbeds/build/build_oranslice_images_mitigation.sh)"; bad=1; fi
  done
  command -v kind >/dev/null && command -v kubectl >/dev/null && command -v docker >/dev/null \
    || { log "FAIL: kind/kubectl/docker not in PATH"; bad=1; }
  [[ -f "$WRAPPER" ]] || { log "FAIL: missing ${WRAPPER}"; bad=1; }
  [[ -x "${TESTBEDS_DIR}/start_testbed.sh" ]] || { log "FAIL: missing/not executable ${TESTBEDS_DIR}/start_testbed.sh"; bad=1; }
  [[ -x "${TESTBEDS_DIR}/start_testbed_mitigation.sh" ]] || { log "FAIL: missing/not executable start_testbed_mitigation.sh"; bad=1; }
  [[ -x "${TESTBEDS_DIR}/stop_testbed.sh" ]] || { log "FAIL: missing/not executable stop_testbed.sh"; bad=1; }
  # Every run opens QGroundControl, so a graphical session is mandatory for the whole campaign.
  # Fail here rather than at run 1 of 58, and keep the AppImages reachable.
  if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    if [[ "$GO" == "1" ]]; then
      log "FAIL: no DISPLAY/WAYLAND_DISPLAY. Every run opens QGroundControl; run the campaign from a graphical session"
      log "      (and make sure the screen lock / suspend will not kill the session during ~16 h)."
      bad=1
    else
      log "WARN (dry-run only): no DISPLAY/WAYLAND_DISPLAY; with --go this is a hard failure (every run opens QGC)"
    fi
  else
    log "display OK: ${DISPLAY:-${WAYLAND_DISPLAY}}"
  fi
  # Same rule as the display check above: a dry run validates the plan and touches nothing, so a missing
  # operator view only warns there. It is a hard failure with --go, where every run opens it.
  # The AppImage is not shipped with the artifact (187 MB); see 03_sitl/bin/README.md to obtain it.
  if [[ -x "${SCRIPTS_DIR}/03_sitl/bin/QGroundControl.AppImage" ]]; then
    log "QGC OK: QGroundControl.AppImage"
  elif [[ "$GO" == "1" ]]; then
    log "FAIL: missing/not executable ${SCRIPTS_DIR}/03_sitl/bin/QGroundControl.AppImage"
    log "      every campaign run opens it; see 03_sitl/bin/README.md to obtain it"
    bad=1
  else
    log "WARN (dry-run only): QGroundControl.AppImage absent; with --go this is a hard failure"
    log "      (see 03_sitl/bin/README.md); the orchestrator can also run without --open-qgc"
  fi
  # The detector build is not run by this campaign (validated separately); only warn if it is absent.
  [[ -x "${SCRIPTS_DIR}/03_sitl/bin/QGroundControl-mitigation.AppImage" ]] \
    || log "WARN: QGroundControl-mitigation.AppImage absent (not used by this campaign)"
  return $bad
}

# --- Testbed lifecycle -------------------------------------------------------------------------------------------------------
stop_clean() {  # $1 = series dir; two attempts, each verified
  local sd="$1" i cl lo
  for i in 1 2; do
    act "${sd}/stop.log" bash -c "cd '${TESTBEDS_DIR}' && ./stop_testbed.sh" \
      || log "WARN: stop_testbed.sh rc!=0 (attempt ${i})"
    [[ "$GO" == "1" ]] || return 0
    cl="$(kind get clusters 2>/dev/null | grep -E "$CLUSTER_RE" || true)"
    lo="$(leftover_procs)"
    if [[ -z "$cl" && -z "$lo" ]]; then log "clean: no Kind cluster, no leftover campaign processes"; return 0; fi
    log "not clean yet (attempt ${i}): clusters=[${cl//$'\n'/ }] leftovers=[${lo//$'\n'/ | }]"
  done
  log "FAIL: could not reach a clean state"
  return 1
}

start_stack() {  # $1 series dir, $2 start script, $3 extra env (string), $4 script args (string)
  local sd="$1" script="$2" envs="$3" sargs="$4"
  sudo_check
  # ORANSLICE_SKIP_BUILD=1: fail fast if an image is missing instead of starting a 30-90 min build unattended.
  act "${sd}/start.log" bash -c "cd '${TESTBEDS_DIR}' && env ORANSLICE_SKIP_BUILD=1 ${envs} ./${script} ${sargs}"
}

pods_ready() {  # $1 kube context; every pod in core + ran Running with all containers ready
  { kubectl --context "$1" get pods -n core -o json; kubectl --context "$1" get pods -n ran -o json; } 2>/dev/null \
  | "$PY" -c '
import sys, json
s = sys.stdin.read(); dec = json.JSONDecoder(); i = 0; n = 0; bad = 0
while True:
    rest = s[i:].lstrip()
    if not rest: break
    obj, end = dec.raw_decode(rest); i = len(s) - len(rest) + end
    for p in obj.get("items", []):
        n += 1
        st = p.get("status", {}); cs = st.get("containerStatuses") or []
        if p.get("metadata", {}).get("deletionTimestamp"): continue
        if st.get("phase") != "Running" or not cs or not all(c.get("ready") for c in cs): bad += 1
sys.exit(0 if n > 0 and bad == 0 else 1)'
}

verify_stack() {  # $1 series dir, rest = KEY=VALUE expected in the topology stamp
  local sd="$1"; shift
  local vf="${sd}/verify.log" ok=1 kv parm procs t0
  if [[ "$GO" != "1" ]]; then
    log "DRY-RUN would verify: ${KUBE_CTX} pods Ready (core, ran); stamp has [$*];"
    log "DRY-RUN would verify: SITL FS_GCS_ENABLE=1 + FS_GCS_TIMEOUT=5; C2 socat UDP:5760 (+ ACK relay UDP:5763 when SIDECARS=FULL) alive"
    return 0
  fi
  t0=$SECONDS
  until pods_ready "$KUBE_CTX"; do
    if (( SECONDS - t0 >= PODS_READY_MAX_S )); then
      log "FAIL verify: pods not Ready after ${PODS_READY_MAX_S}s"
      kubectl --context "$KUBE_CTX" get pods -A >>"$vf" 2>&1
      return 1
    fi
    sleep 10
  done
  log "verify: all pods Ready (core, ran)"
  for kv in "$@"; do
    if grep -qx "$kv" "$STAMP" 2>/dev/null; then log "verify: stamp ${kv} OK"
    else log "FAIL verify: stamp expects ${kv}, found '$(grep "^${kv%%=*}=" "$STAMP" 2>/dev/null)'"; ok=0; fi
  done
  parm="$(kubectl --context "$KUBE_CTX" -n ran exec deploy/oai-ue-uav -c sitl -- cat /tmp/tm_gcs_failsafe.parm 2>>"$vf")"
  echo "$parm" >>"$vf"
  if grep -Eq '^FS_GCS_ENABLE[[:space:]]+1[[:space:]]*$' <<<"$parm" \
     && grep -Eq '^FS_GCS_TIMEOUT[[:space:]]+5[[:space:]]*$' <<<"$parm"; then
    log "verify: SITL FS_GCS_ENABLE=1, FS_GCS_TIMEOUT=5 OK"
  else
    log "FAIL verify: SITL failsafe parameters not as expected (see ${vf})"; ok=0
  fi
  # C2 relay on UDP:5760; the FULL profile also needs the ACK relay on UDP:5763 (ACK_PROBE_SOCAT=1,
  # which is the testbed default). Without it, command_ack_rtt would run with no path.
  procs="$(kubectl --context "$KUBE_CTX" -n ran exec deploy/oai-ue-uav -c sitl -- sh -c \
    'for f in /proc/[0-9]*/cmdline; do tr "\000" " " < "$f" 2>/dev/null; echo; done' 2>>"$vf")"
  if grep -q "UDP4-LISTEN:5760," <<<"$procs"; then log "verify: C2 socat UDP:5760 alive"
  else log "FAIL verify: C2 socat UDP:5760 not found in the sitl container"; ok=0; fi
  if [[ "${SIDECARS^^}" == "FULL" ]]; then
    if grep -q "UDP4-LISTEN:5763," <<<"$procs"; then log "verify: ACK probe relay UDP:5763 alive (FULL sidecars)"
    else log "FAIL verify: SIDECARS=FULL but the ACK relay UDP:5763 is missing (bring the testbed up with ACK_PROBE_SOCAT=1)"; ok=0; fi
  fi
  [[ "$ok" == "1" ]]
}

# --- Positive control: blackhole the command leg so the native failsafe trips --------------------------------------------------
# The UAV's PDU address is assigned by the UPF at attach time and changes between bring-ups, so it is discovered
# at runtime (same awk as tm_pdu_ip() inside the testbed) instead of being hardcoded.
uav_pdu_ip() {
  kubectl --context "$KUBE_CTX" -n "$NS_RAN" exec deploy/oai-ue-uav -c nr-ue -- \
    sh -c "ip -4 -o addr show 2>/dev/null | awk '/oaitun| oai/{print \$4; exit}' | cut -d/ -f1" 2>/dev/null | tr -d '\r\n'
}
gcs_route() {  # $1 = add|del, $2 = destination address
  kubectl --context "$KUBE_CTX" -n "$NS_RAN" exec deploy/oai-ue-gcs -c nr-ue -- \
    ip route "$1" blackhole "${2}/32" >/dev/null 2>&1
}

# Runs in the background for the duration of the S6 series. Everything it does is recorded, and a failure to cut
# is logged loudly rather than silently producing a run that looks like a control but is not one.
poscontrol_injector() {  # $1 = series dir
  # NOTE: one `local` per dependent assignment. In bash a variable set earlier in the SAME `local` statement is
  # not yet visible to the later ones, and under `set -u` referencing it there is a fatal error.
  local sd="$1"
  local runs="${sd}/runs"
  local out="${sd}/blackhole_injection.json"
  local log_f="${sd}/poscontrol.log"
  local rd slug d t t0 t0ns uav cs ce waited i
  exec >>"$log_f" 2>&1
  echo "[$(ts)] poscontrol: injector started (cut at t_rel ${POSCTL_CUT_AT_S}s for ${POSCTL_CUT_S}s)"

  # Capability probe first: if the container may not add routes, say so now instead of after a wasted 13 min run.
  if gcs_route add "$POSCTL_TEST_DST"; then
    gcs_route del "$POSCTL_TEST_DST"
    echo "[$(ts)] poscontrol: route capability OK in the oai-ue-gcs pod"
  else
    echo "[$(ts)] poscontrol: FAIL - cannot add a blackhole route in oai-ue-gcs (missing NET_ADMIN?); NO cut will be applied"
    log "poscontrol: FAIL - cannot install the blackhole route; the S6 run will NOT be a positive control"
    return 1
  fi
  uav="$(uav_pdu_ip)"
  if [[ ! "$uav" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    echo "[$(ts)] poscontrol: FAIL - could not resolve the UAV PDU address (got '${uav}'); NO cut will be applied"
    log "poscontrol: FAIL - could not resolve the UAV PDU address; the S6 run will NOT be a positive control"
    return 1
  fi
  echo "[$(ts)] poscontrol: UAV PDU address = ${uav}"

  # Wait for the run directory. orchestrate.py creates it at run start and its name carries the start timestamp,
  # which is the anchor used below; this is more reliable than the directory mtime.
  waited=$SECONDS
  while :; do
    rd="$(find "$runs" -maxdepth 1 -type d -name '2026*' 2>/dev/null | sort | tail -1)"
    [[ -n "$rd" ]] && break
    if (( SECONDS - waited > 900 )); then
      echo "[$(ts)] poscontrol: FAIL - no run directory under ${runs} after 900s; NO cut applied"
      log "poscontrol: FAIL - the run never started; no cut applied"
      return 1
    fi
    sleep 5
  done
  slug="$(basename "$rd")"
  # Anchor on the run's OWN T0: clock_anchor.json is written at T0, early in the run, and carries both
  # T0_unix_ns and T0_mono_ns. With it, POSCTL_CUT_AT_S means t_rel in the same base as the phase markers.
  # The directory name is only a fallback: it is stamped ~80 s BEFORE T0 (preflight and SITL warm-up run
  # first), so anchoring on it would place the cut that much earlier than requested.
  t0ns=""
  for ((i = 0; i < 60; i++)); do
    [[ -s "${rd}/clock_anchor.json" ]] && break
    sleep 5
  done
  if [[ -s "${rd}/clock_anchor.json" ]]; then
    t0ns="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["T0_unix_ns"])' \
            "${rd}/clock_anchor.json" 2>/dev/null)"
  fi
  if [[ "$t0ns" =~ ^[0-9]+$ ]]; then
    t0=$(( t0ns / 1000000000 ))
    echo "[$(ts)] poscontrol: run=${slug} T0=$(date -d "@${t0}" '+%F %T') (from clock_anchor.json)"
  else
    d="${slug:0:8}"; t="${slug:9:6}"
    t0="$(date -d "${d:0:4}-${d:4:2}-${d:6:2} ${t:0:2}:${t:2:2}:${t:4:2}" +%s 2>/dev/null)" || t0=""
    if [[ -z "$t0" ]]; then
      echo "[$(ts)] poscontrol: FAIL - no clock_anchor.json and unparsable slug '${slug}'; NO cut applied"
      log "poscontrol: FAIL - could not anchor the cut in time; no cut applied"
      return 1
    fi
    echo "[$(ts)] poscontrol: WARN no clock_anchor.json; anchored on the directory name ($(date -d "@${t0}" '+%F %T')), which precedes T0"
  fi

  # Sleep until the target instant, then cut. Removal is attempted even if the sleep is interrupted.
  local target=$((t0 + POSCTL_CUT_AT_S)) now
  while :; do
    now="$(date +%s)"
    (( now >= target )) && break
    sleep $(( (target - now) > 15 ? 10 : 1 ))
  done
  trap 'gcs_route del "$uav"' RETURN
  if ! gcs_route add "$uav"; then
    echo "[$(ts)] poscontrol: FAIL - blackhole add failed at cut time; NO cut applied"
    log "poscontrol: FAIL - blackhole add failed at cut time"
    return 1
  fi
  cs="$(date +%s%N)"
  echo "[$(ts)] poscontrol: CUT ON  -> blackhole ${uav}/32 installed in oai-ue-gcs"
  sleep "$POSCTL_CUT_S"
  gcs_route del "$uav"
  ce="$(date +%s%N)"
  trap - RETURN
  echo "[$(ts)] poscontrol: CUT OFF -> blackhole removed after $(( (ce - cs) / 1000000 )) ms"
  log "poscontrol: command leg cut for $(( (ce - cs) / 1000000 )) ms at t_rel ~${POSCTL_CUT_AT_S}s (${uav})"

  # Resolve the cut against the run's OWN T0 and record it. This is the part that makes the control auditable:
  # the times written here are measured, not planned, and are expressed in the same base as the run's artifacts.
  local i
  for ((i = 0; i < 120; i++)); do
    [[ -s "${rd}/run_meta.json" ]] && break
    sleep 10
  done
  "$PY" - "$rd" "$out" "$uav" "$cs" "$ce" "$POSCTL_CUT_AT_S" <<'PY'
import csv, json, os, sys
rd, out, uav, cs, ce, want = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), float(sys.argv[6])
rec = {"control": "positive_control_fs_gcs", "leg": "command (GCS -> UAV)", "pod": "oai-ue-gcs",
       "dest": f"{uav}/32", "method": "ip route add/del blackhole (no iptables, no qdisc change)",
       "cut_start_unix_ns": cs, "cut_end_unix_ns": ce, "cut_duration_s": (ce - cs) / 1e9,
       "cut_target_t_rel_s": want, "run_dir": rd}


def mavlink_t_rel_offset(rd, t0_mono_ns):
    """Seconds between T0_mono_ns and the anchor used by the MAVLink capture's own t_rel_s column.

    The run uses two t_rel origins: the phase markers (and therefore the cut, which is resolved from
    T0_unix_ns) count from T0 = run start, while statustext/c2 count from the mission start, ~90 s later.
    Comparing the two directly makes a failsafe look like it preceded its own cause, so the offset is
    measured from a file that carries BOTH columns instead of being assumed to be zero.
    """
    for name in ("statustext_gcs.csv", "telemetry_downlink_gcs.csv"):
        p = os.path.join(rd, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    tm, tr = row.get("t_mono_ns"), row.get("t_rel_s")
                    if tm is None or tr is None:
                        break
                    return (int(tm) - float(tr) * 1e9 - t0_mono_ns) / 1e9
        except Exception:
            continue
    return None


try:
    m = json.load(open(os.path.join(rd, "run_meta.json")))
    t0u, t0m = m["T0_unix_ns"], m["T0_mono_ns"]
    rec["T0_unix_ns"], rec["T0_mono_ns"] = t0u, t0m
    # Cut times in the run's T0 base (same base as flyblind_phase_windows.json markers).
    rec["cut_start_t_rel_s"] = (cs - t0u) / 1e9
    rec["cut_end_t_rel_s"] = (ce - t0u) / 1e9
    o = m.get("outcomes") or {}
    rec["observed"] = {"failsafe": o.get("failsafe"), "failsafe_mode": o.get("failsafe_mode"),
                       "mode_transitions": o.get("mode_transitions"), "valid": m.get("valid")}
    off = mavlink_t_rel_offset(rd, t0m)
    rec["mavlink_t_rel_offset_s"] = off
    fs = ((json.load(open(os.path.join(rd, "c2.json"))).get("c2_metrics") or {}).get("failsafe") or {})
    if fs.get("time_s") is not None:
        rec["failsafe_t_rel_s_mavlink_base"] = fs["time_s"]
        if off is not None:
            rec["failsafe_t_rel_s"] = fs["time_s"] + off
            rec["failsafe_latency_after_cut_s"] = rec["failsafe_t_rel_s"] - rec["cut_start_t_rel_s"]
    # Sound only if the failsafe fired AFTER the cut and within a few FS_GCS_TIMEOUT periods of it.
    lat = rec.get("failsafe_latency_after_cut_s")
    rec["causally_attributed"] = bool(lat is not None and 0 <= lat <= 30)
    if off is None:
        rec["attribution_note"] = "could not measure the MAVLink t_rel anchor; latency not computed"
except Exception as e:
    rec["resolution_error"] = repr(e)
    rec["causally_attributed"] = None
json.dump(rec, open(out, "w"), indent=2)
print(json.dumps({k: rec.get(k) for k in
      ("cut_start_t_rel_s", "cut_end_t_rel_s", "mavlink_t_rel_offset_s", "failsafe_t_rel_s",
       "failsafe_latency_after_cut_s", "causally_attributed")}, indent=1))
PY
  log "poscontrol: injection record written to ${out}"
  if "$PY" -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get('causally_attributed') else 1)" "$out" 2>/dev/null; then
    log "poscontrol: OK - failsafe tripped after the cut (causally attributed); see ${out}"
  else
    log "poscontrol: WARN - the failsafe could not be causally attributed to the cut; inspect ${out} before using this run"
  fi
}

# --- Condition generation (inline; archived under the night root) -------------------------------------------------------------
# emit_json OUTFILE NOTE  [BASE_ID N ARGV]...
emit_json() {
  local out="$1" note="$2"; shift 2
  local -a items=()
  local base n argv i
  while (($#)); do
    base="$1"; n="$2"; argv="$3"; shift 3
    for ((i = 1; i <= n; i++)); do
      items+=("    {\"id\": \"${base}_r${i}\", \"argv\": \"${argv} --results-tag ${base^^}\"}")
    done
  done
  {
    echo "{"
    echo "  \"note\": \"${note}\","
    echo "  \"conditions\": ["
    local first=1 it
    for it in "${items[@]}"; do
      if (( first )); then first=0; else echo ","; fi
      printf '%s' "$it"
    done
    echo
    echo "  ]"
    echo "}"
  } >"$out"
  # Validate immediately: a malformed list must fail now, not at 03:00.
  if ! "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['conditions'], 'empty'; print(len(d['conditions']))" \
       "$out" >/dev/null 2>&1; then
    log "FAIL: generated condition file is not valid JSON: ${out}"
    return 1
  fi
  return 0
}

ATK="--condition attack --attack multi-rogue --attack-start-after-s 45"
QGC="--open-qgc"                  # stock operator view: EVERY run opens it (requires a graphical session)

write_conditions() {
  local rc=0
  # ---- S1_core: dose-response + video scope + plateau + no-video control + capacity probe + drain (48 runs) ----
  # Every condition opens the stock operator view (--open-qgc): the QGC map is part of the evidence of every run.
  #
  # The victim is only starved while its own demand exceeds the proportional-fair share C/(N+1). Two groups probe
  # that boundary, and both are predictions of the same one-parameter model AoI = (Q/C)(N+1), Q = RLC_TX_MAXSIZE:
  #   b_atk_n4_vid4/6/8  -> the ONSET. The measured UL capacity is C ~= 24..32 Mb/s, so the share at N=4 is
  #                         C/5 ~= 4.8..6.4 Mb/s. The attack is absent at 4 Mb/s and present at 8 Mb/s, so 6 Mb/s
  #                         sits exactly on the predicted boundary: it brackets the onset within 2 Mb/s either way.
  #   d_atk_n4_vid16/24  -> the PLATEAU. Once demand exceeds the share, the steady-state age is set by the buffer
  #                         drained at the share (Q/(C/(N+1))) and must NOT depend on how far above the share the
  #                         victim offers. Falsifiable: the age at 16 and 24 Mb/s must match the 12 Mb/s headline.
  emit_json "${COND_OUT}/s1_core.json" \
    "S1 core: dose-response N=0..4 (headline n=5 at N=4), video onset 4/6/8, video plateau 16/24, no-video control, capacity probe, drain. Stock QGC on every run." \
    a_baseline            3 "--condition baseline ${QGC}" \
    a_atk_n1              3 "${ATK} --multi-rogue-count 1 ${QGC}" \
    a_atk_n2              3 "${ATK} --multi-rogue-count 2 ${QGC}" \
    a_atk_n3              3 "${ATK} --multi-rogue-count 3 ${QGC}" \
    a_atk_n4              5 "${ATK} --multi-rogue-count 4 ${QGC}" \
    b_atk_n4_vid4         3 "${ATK} --multi-rogue-count 4 --companion-video-mbps 4 ${QGC}" \
    b_atk_n4_vid6         3 "${ATK} --multi-rogue-count 4 --companion-video-mbps 6 ${QGC}" \
    b_atk_n4_vid8         3 "${ATK} --multi-rogue-count 4 --companion-video-mbps 8 ${QGC}" \
    c_atk_n4_novideo      3 "${ATK} --multi-rogue-count 4 --companion-video-mbps 0 ${QGC}" \
    d_atk_n4_vid16        3 "${ATK} --multi-rogue-count 4 --companion-video-mbps 16 ${QGC}" \
    d_atk_n4_vid24        3 "${ATK} --multi-rogue-count 4 --companion-video-mbps 24 ${QGC}" \
    cap_base_vid24        3 "--condition baseline --companion-video-mbps 24 ${QGC}" \
    cap_base_vid32        2 "--condition baseline --companion-video-mbps 32 ${QGC}" \
    cap_base_vid40        2 "--condition baseline --companion-video-mbps 40 ${QGC}" \
    g_drain_video_off     3 "${ATK} --multi-rogue-count 4 --drain-until-baseline --video-stop-at-attack-end ${QGC}" \
    g_drain_video_on      3 "${ATK} --multi-rogue-count 4 --drain-until-baseline ${QGC}" || rc=1

  # ---- S3_prb51: bandwidth generality, 20 MHz (10 runs) ----
  # The capacity probe needs BOTH bounds to state C(51): a clean point below it and a self-congesting point above it.
  # vid16 is the lower bound and carries 3 repetitions, because a single one can be lost to a transient host gate
  # (it was, in the first campaign) and then C(51) has no floor at all.
  emit_json "${COND_OUT}/s3_prb51.json" \
    "S3 bandwidth generality: 51 PRB (20 MHz) baseline + attack N=4, plus a two-sided capacity probe (16 clean / 24 congesting). Stock QGC on every run." \
    e_base51              3 "--condition baseline --gnb-prb 51 --companion-video-mbps 8 ${QGC}" \
    e_atk51_n4            3 "${ATK} --gnb-prb 51 --multi-rogue-count 4 --companion-video-mbps 8 ${QGC}" \
    cap51_base_vid16      3 "--condition baseline --gnb-prb 51 --companion-video-mbps 16 ${QGC}" \
    cap51_base_vid24      1 "--condition baseline --gnb-prb 51 --companion-video-mbps 24 ${QGC}" || rc=1

  # ---- S4_slicing: the defence that works (3 runs) ----
  emit_json "${COND_OUT}/s4_slicing.json" \
    "S4 defence: uplink slice enforcement (victim on its own slice B, UL patch ON, floor 0) -- attack must be neutralised. Stock QGC on every run." \
    f1_slicing_n4         3 "${ATK} --multi-rogue-count 4 ${QGC}" || rc=1

  # ---- S5_aqm: the defence that does not work (3 runs) ----
  emit_json "${COND_OUT}/s5_aqm.json" \
    "S5 defence: host AQM (fq_codel on the UAV uplink) -- expected NOT to restore freshness. Stock QGC on every run." \
    f2_aqm_n4             3 "${ATK} --multi-rogue-count 4 ${QGC}" || rc=1

  # ---- S6_poscontrol: positive control -- the monitors DO fire on a real interruption (1 run) ----
  # Same testbed and same route as the headline runs, no adversary. While the vehicle is in steady flight a
  # blackhole route is installed in the GCS pod towards the UAV's PDU address, cutting the COMMAND leg
  # (GCS -> UAV) for longer than FS_GCS_TIMEOUT. See poscontrol_injector() for the mechanism.
  #
  # Direction matters: FS_GCS watches whether the UAV still HEARS the GCS, so the cut must be on the
  # GCS -> UAV leg. Cutting the return leg would age the operator's state without ever arming the failsafe
  # (which is, in fact, exactly what the attack does).
  #
  # This run is EXPECTED to be scored invalid: a tripped failsafe fails baseline_drone_quality
  # ("failsafe; missing GUIDED->LAND; abnormal_land") by construction. It is read as a control, not as a
  # scored run -- hence the pos_ prefix in the expected-invalid list.
  emit_json "${COND_OUT}/s6_poscontrol.json" \
    "S6 positive control: command-leg blackhole > FS_GCS_TIMEOUT in steady flight -- failsafe MUST trip (GUIDED->RTL) and QGC MUST alert. Expected invalid by construction." \
    pos_control_route     1 "--condition baseline ${QGC}" || rc=1

  return $rc
}

# --- Series execution --------------------------------------------------------------------------------------------------------
run_series() {  # $1 series id, $2 conditions file, $3 seed, $4 extra orchestrate args
  local sid="$1" cond="$2" seed="$3" extra="$4"
  local sd="${NIGHT_ROOT}/${sid}"
  local -a cmd=("$PY" -u "$WRAPPER" --conditions "$cond" --orchestrate paper --seed "$seed"
                --out-root "${sd}/runs" --continue-on-invalid --batch-log "${sd}/batch_log.json")
  local ea="${COMMON_ARGS} ${extra}"
  ea="$(echo "$ea" | xargs || true)"          # collapse whitespace; may legitimately be empty
  [[ -n "$ea" ]] && cmd+=("--extra-arg=${ea}")
  if [[ "$GO" == "1" ]]; then
    log "series ${sid}: batch start (seed=${seed}, conditions=$(basename "$cond"))"
    "${cmd[@]}" 2>&1 | tee -a "${sd}/batch.log"
    log "series ${sid}: batch finished rc=${PIPESTATUS[0]} (rc=1 means at least one invalid run; see the summary)"
  else
    if "${cmd[@]}" --dry-run >"${sd}/batch_dryrun.json" 2>&1; then
      log "series ${sid}: wrapper dry-run OK ($(grep -c '"id"' "${sd}/batch_dryrun.json") planned), plan in ${sd}/batch_dryrun.json"
    else
      log "FAIL: wrapper dry-run for ${sid} returned non-zero (see ${sd}/batch_dryrun.json)"
      return 1
    fi
  fi
}

summarize_series() {  # $1 series id, $2 condition-id prefixes expected to be invalid (space separated)
  local sid="$1" expinv="$2" sd="${NIGHT_ROOT}/$1"
  "$PY" - "${sd}/batch_log.json" "$sid" "$expinv" "${sd}/summary.json" "$GO" <<'PY' | tee -a "$SUMMARY_MD" | tee -a "$NIGHT_LOG"
import json, re, sys, collections
blog, sid, exp, out, go = sys.argv[1], sys.argv[2], sys.argv[3].split(), sys.argv[4], sys.argv[5]
try:
    b = json.load(open(blog))
except Exception as e:
    print(f"\n### {sid}\n- no batch log ({e})\n"); sys.exit(0)
planned = collections.Counter(re.sub(r"_r\d+$", "", o["id"]) for o in b.get("order", []))
grp = collections.OrderedDict((g, {"planned": n, "executed": 0, "valid": 0, "invalid": [], "unknown": 0})
                              for g, n in planned.items())
for r in b.get("runs", []):
    g = re.sub(r"_r\d+$", "", r["id"])
    d = grp.setdefault(g, {"planned": 0, "executed": 0, "valid": 0, "invalid": [], "unknown": 0})
    d["executed"] += 1
    if r.get("valid") is True:
        d["valid"] += 1
    elif r.get("valid") is False:
        d["invalid"].append({"id": r["id"], "run_dir": r.get("run_dir"),
                             "failed_gates": (r.get("detail") or {}).get("failed_gates")})
    else:
        d["unknown"] += 1
print(f"\n### {sid} (seed {b.get('seed')})\n")
print("| condition | planned | executed | valid | status |")
print("|---|---:|---:|---:|---|")
for g, d in grp.items():
    if go != "1":
        status = "dry-run"
    elif any(g.startswith(e) for e in exp):
        status = "expected invalid allowed (evaluate by goodput)"
    elif d["valid"] == d["planned"]:
        status = "OK"
    else:
        status = "NEEDS RERUN"
    print(f"| {g} | {d['planned']} | {d['executed']} | {d['valid']} | {status} |")
for g, d in grp.items():
    for inv in d["invalid"]:
        print(f"- invalid: {inv['id']} gates={inv['failed_gates']} dir={inv['run_dir']}")
json.dump(grp, open(out, "w"), indent=2)
PY
}

# series SID COND SEED "EXTRA" "EXPECTED_INVALID" START_SCRIPT "ENV" "ARGS" KEY=VALUE...
series() {
  local sid="$1" cond="$2" seed="$3" extra="$4" expinv="$5" script="$6" envs="$7" sargs="$8"; shift 8
  local sd="${NIGHT_ROOT}/${sid}" attempt up=0
  if ! selected "$sid"; then
    log "---- SERIES ${sid}: not selected (--only=${ONLY_SERIES}), skipping"
    SERIES_RESULTS+=("${sid}: not selected")
    return 0
  fi
  mkdir -p "$sd"
  log "================ SERIES ${sid}: ${envs} ./${script} ${sargs} | conditions=$(basename "$cond")"
  [[ -f "$cond" ]] || { log "SKIP ${sid}: missing ${cond}"; SERIES_RESULTS+=("${sid}: SKIPPED (missing conditions)"); return 1; }
  check_disk || { log "SKIP ${sid}: disk"; SERIES_RESULTS+=("${sid}: SKIPPED (disk)"); return 1; }
  for attempt in 1 2; do
    stop_clean "$sd" || { log "attempt ${attempt}: not clean"; continue; }
    settle_load
    start_stack "$sd" "$script" "$envs" "$sargs" || { log "attempt ${attempt}: start script failed"; continue; }
    if verify_stack "$sd" "$@"; then up=1; break; fi
    log "attempt ${attempt}: verification failed"
  done
  if [[ "$up" != "1" ]]; then
    log "SKIP ${sid}: testbed could not be started and verified (2 attempts)"
    SERIES_RESULTS+=("${sid}: SKIPPED (testbed start/verify failed twice)")
    echo -e "\n### ${sid}\n- SKIPPED: testbed start/verify failed twice (see ${sd}/start.log, verify.log)" >>"$SUMMARY_MD"
    return 1
  fi
  settle_load
  # Optional in-run hook (used by S6 to cut the command leg mid-flight). It runs alongside the runs and is
  # waited for afterwards, so its record is complete before the series is summarized.
  local hook_pid=""
  if [[ -n "$PRERUN_HOOK" ]]; then
    if [[ "$GO" == "1" ]]; then
      log "${sid}: starting in-run hook ${PRERUN_HOOK}"
      "$PRERUN_HOOK" "$sd" & hook_pid=$!
    else
      log "DRY-RUN would start the in-run hook ${PRERUN_HOOK} (cut at t_rel ${POSCTL_CUT_AT_S}s for ${POSCTL_CUT_S}s)"
    fi
  fi
  run_series "$sid" "$cond" "$seed" "$extra" || { SERIES_RESULTS+=("${sid}: FAILED (wrapper)"); return 1; }
  if [[ -n "$hook_pid" ]]; then
    wait "$hook_pid" 2>/dev/null \
      && log "${sid}: in-run hook finished" \
      || log "${sid}: WARN in-run hook reported a problem (see ${sd}/poscontrol.log)"
  fi
  summarize_series "$sid" "$expinv"
  SERIES_RESULTS+=("${sid}: done")
}

finish_campaign() {
  local sd="${NIGHT_ROOT}/_final"
  mkdir -p "$sd"
  log "================ END: stopping the testbed to leave the host clean"
  stop_clean "$sd" || log "WARN: final cleanup not confirmed"
  log "================ SERIES RESULTS"
  local r
  for r in "${SERIES_RESULTS[@]}"; do log "  ${r}"; done
  log "results: ${NIGHT_ROOT}"
  log "summary: ${SUMMARY_MD}"
}

list_plan() {
  cat <<'EOF'
FlyBlind campaign plan (65 runs, 5 series, 4 distinct testbed configurations)

EVERY run opens QGroundControl: the operator view is part of the evidence of every run, so the whole
campaign REQUIRES an active graphical session (and no screen lock / suspend during ~18 h).
EVERY run also records the FULL sidecar profile (per-leg OWD command+return, host load, ACK RTT, RLC,
UE iface). The ACK RTT probe needs the relay on UDP:5763, which the testbed brings up by default.
  All runs use the stock build (--open-qgc). The detector build ships with the artifact but is
  exercised separately (its alarm was validated visually), not inside this campaign.

  S1_core      48 runs  ./start_testbed.sh                            (default: co-located victim, 106 PRB, UL patch armed)
                        dose-response N=0..4 (n=5 at N=4) | video onset 4/6/8 Mb | video plateau 16/24 Mb
                        | no-video control | capacity probe 24/32/40 Mb (expected invalid, read by goodput)
                        | drain x2
  S3_prb51     10 runs  GNB_PRB=51 ./start_testbed.sh                 20 MHz generality + two-sided capacity probe
  S4_slicing    3 runs  ./start_testbed_mitigation.sh --slicing       defence that works
  S5_aqm        3 runs  ./start_testbed_mitigation.sh --aqm           defence that does not work
  S6_poscontrol 1 run   ./start_testbed.sh                            positive control: command-leg blackhole in
                        steady flight must trip FS_GCS (GUIDED->RTL) -- expected invalid by construction

Note on the dose-response range: N is capped at 4 by the testbed itself (start_testbed.sh accepts rogues=1..4
and only four rogue subscribers are provisioned), so the model is probed along the victim's own demand
(onset + plateau) rather than by adding co-tenants.

Estimated wall time: ~18 h (65 x ~16 min + 5 bring-ups).
EOF
}

# --- Main --------------------------------------------------------------------------------------------------------------------
main() {
  parse_args "$@"
  if [[ "$LIST_ONLY" == "1" ]]; then list_plan; exit 0; fi

  init_night
  list_plan | tee -a "$NIGHT_LOG" >/dev/null
  sudo_init

  if ! preflight; then
    log "ABORT: preflight failed (nothing was started)"
    exit 1
  fi
  log "preflight OK"

  if ! write_conditions; then
    log "ABORT: could not generate the condition files"
    exit 1
  fi
  log "conditions generated and validated under ${COND_OUT}"

  # S1 and S2 share the default configuration; they are separate series so the GUI runs stay isolated
  # and can be re-run on their own, and so each gets a fresh, verified testbed.
  series S1_core      "${COND_OUT}/s1_core.json"     20261111 "" "cap_" \
         start_testbed.sh "" "" \
         TOPOLOGY=cross GNB_PRB=106 MIN_RATIO_SLICE_B=0 VICTIM_IN_EMBB=1 MITIGATION_UL_RRMPOLICY=1 MITIGATION_AQM=0

  series S3_prb51     "${COND_OUT}/s3_prb51.json"   20261113 "" "cap51_" \
         start_testbed.sh "GNB_PRB=51" "" \
         TOPOLOGY=cross GNB_PRB=51 MIN_RATIO_SLICE_B=0 VICTIM_IN_EMBB=1 MITIGATION_UL_RRMPOLICY=1 MITIGATION_AQM=0

  series S4_slicing   "${COND_OUT}/s4_slicing.json" 20261114 "" "" \
         start_testbed_mitigation.sh "" "--slicing" \
         TOPOLOGY=cross GNB_PRB=106 MIN_RATIO_SLICE_B=0 VICTIM_IN_EMBB=0 MITIGATION_UL_RRMPOLICY=1 MITIGATION_AQM=0

  series S5_aqm       "${COND_OUT}/s5_aqm.json"     20261115 "" "" \
         start_testbed_mitigation.sh "" "--aqm" \
         TOPOLOGY=cross GNB_PRB=106 MIN_RATIO_SLICE_B=0 VICTIM_IN_EMBB=1 MITIGATION_UL_RRMPOLICY=1 MITIGATION_AQM=1

  # Positive control: the default testbed (identical to S1) with the command leg cut mid-flight by the hook.
  # pos_ is in the expected-invalid list because a tripped failsafe fails the baseline mission gates by design.
  PRERUN_HOOK="poscontrol_injector"
  series S6_poscontrol "${COND_OUT}/s6_poscontrol.json" 20261116 "" "pos_" \
         start_testbed.sh "" "" \
         TOPOLOGY=cross GNB_PRB=106 MIN_RATIO_SLICE_B=0 VICTIM_IN_EMBB=1 MITIGATION_UL_RRMPOLICY=1 MITIGATION_AQM=0
  PRERUN_HOOK=""

  finish_campaign
}

main "$@"

#!/usr/bin/env bash
##################################################################################################################################
# Script: stop_testbed.sh — universal teardown: any testbed configuration, from any partial state
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G
#
# Author: (anonymous; double-blind submission).
# Institution: withheld (double-blind review).
# Date: 2026-11
# License: MIT License
#
# Description: Tears down both stacks from any partial state: host leftovers, Kind clusters, leftover node containers,
# kubeconfig contexts, runtime/ and runtime-mitigation/. Does NOT delete vendor/, image tarballs, 02_results/,
# or Docker images (unless --prune-docker).
#
# Usage: ./stop_testbed.sh
# ./stop_testbed.sh --prune-docker
##################################################################################################################################

set -uo pipefail

PRUNE_DOCKER=0
for _arg in "$@"; do
  case "${_arg}" in
    --prune-docker) PRUNE_DOCKER=1 ;;
    --keep-images) PRUNE_DOCKER=0 ;;
    -h|--help)
      echo "Usage: $(basename "$0") [--prune-docker]"
      echo "  Stops paper + mitigation (Kind, host leftovers, runtime dirs)."
      echo "  Keeps vendor/, Docker images, and 02_results/."
      echo "  --prune-docker   also run 'docker system prune -af' (destroys compiled images)"
      echo "  --keep-images    no-op (images are kept by default)"
      exit 0
      ;;
    *)
      echo "Unknown option: ${_arg}" >&2
      echo "Usage: $(basename "$0") [--prune-docker]" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TERM="${TERM:-xterm-256color}"

CLUSTERS=(
  tm-oranslice
  tm-oranslice-mitigation
  tm1-oranslice
  tm1-oranslice-mitigation
)

RUNTIME_DIRS=(
  "${SCRIPT_DIR}/runtime"
  "${SCRIPT_DIR}/runtime-mitigation"
  "${SCRIPT_DIR}/.runtime"
  "${SCRIPT_DIR}/.runtime-mitigation"
)

# Host leftovers from start_testbed / orchestrate (Ctrl-C, crash, old host-SITL path).
# Character-class prefixes so pgrep does not match this script's own command line.
HOST_PATTERNS=(
  '[s]im_vehicle.py'
  'build/sitl/bin/arducopter'
  '[h]ost_telemetry.py'
  '[u]pf_telemetry.py'
  '[o]wd_probe.py'
  '[m]ac_prb.py'
  '[m]ac_prb_tool.py'
  '[m]avlink_proxy.py'
  '[s]etpoint_stream.py'
  '[m]ission_runner.py'
  '[c]2_sync_flood.py'
  '[u]dp_flood.py'
  '[c]o_tenant_load.py'
  '[c]o_tenant_sink.py'
  '[c]o_tenant_burst_sender.py'
  '[c]2_arrival_forwarder.py'
  '[o]rchestrate.py'
)

YELLOW="\033[33m"
RESET="\033[0m"
log() { echo -e "${YELLOW}$*${RESET}"; }

kind_has() {
  command -v kind >/dev/null 2>&1 || return 1
  kind get clusters 2>/dev/null | grep -qx "$1"
}

stop_matching_host() {
  local pat
  for pat in "${HOST_PATTERNS[@]}"; do
    pkill -f "${pat}" 2>/dev/null || true
  done
}

host_leftover_count() {
  local pat n=0
  for pat in "${HOST_PATTERNS[@]}"; do
    pgrep -f "${pat}" >/dev/null 2>&1 && n=$((n + 1))
  done
  printf '%s\n' "${n}"
}

teardown_host() {
  log "[00] Stopping host leftovers (SITL / probes / orchestrate helpers)..."
  if [[ "$(host_leftover_count)" -eq 0 ]]; then
    log "[00] none."
    return 0
  fi
  stop_matching_host
  sleep 1
  if [[ "$(host_leftover_count)" -gt 0 ]]; then
    local pat
    for pat in "${HOST_PATTERNS[@]}"; do
      pkill -9 -f "${pat}" 2>/dev/null || true
    done
    sleep 0.3
  fi
}

teardown_kind() {
  if ! command -v kind >/dev/null 2>&1; then
    log "[01] kind not found — skipping Kind cluster delete."
    return 0
  fi
  local name found=0
  for name in "${CLUSTERS[@]}"; do
    if kind_has "${name}"; then
      found=1
      log "[01] Deleting Kind cluster '${name}'..."
      kind delete cluster --name "${name}" >/dev/null 2>&1 || true
    fi
  done
  if [[ "${found}" -eq 0 ]]; then
    log "[01] No FlyBlind Kind clusters."
  fi
}

teardown_kind_containers() {
  command -v docker >/dev/null 2>&1 || return 0
  local name removed=0
  while IFS= read -r name; do
    [[ -z "${name}" ]] && continue
    case "${name}" in
      tm-oranslice*|tm1-oranslice*)
        log "[02] Removing leftover container '${name}'..."
        docker rm -f "${name}" >/dev/null 2>&1 || true
        removed=1
        ;;
    esac
  done < <(docker ps -a --format '{{.Names}}' 2>/dev/null || true)
  if [[ "${removed}" -eq 0 ]]; then
    log "[02] No leftover Kind node containers."
  fi
}

teardown_kubeconfig() {
  command -v kubectl >/dev/null 2>&1 || return 0
  local cur name
  cur="$(kubectl config current-context 2>/dev/null || true)"
  case "${cur}" in
    kind-tm-oranslice*|kind-tm1-oranslice*)
      kubectl config unset current-context >/dev/null 2>&1 || true
      ;;
  esac
  for name in "${CLUSTERS[@]}"; do
    kubectl config delete-context "kind-${name}" >/dev/null 2>&1 || true
    kubectl config delete-cluster "kind-${name}" >/dev/null 2>&1 || true
    kubectl config unset "users.kind-${name}" >/dev/null 2>&1 || true
  done
}

teardown_runtime() {
  local d found=0
  for d in "${RUNTIME_DIRS[@]}"; do
    if [[ -e "${d}" ]]; then
      found=1
      log "[03] Removing $(basename "${d}")/..."
      rm -rf "${d}"
    fi
  done
  if [[ "${found}" -eq 0 ]]; then
    log "[03] No runtime directories."
  fi
}

teardown_docker_prune() {
  if [[ "${PRUNE_DOCKER}" -ne 1 ]]; then
    log "[04] Docker images kept (pass --prune-docker to reclaim disk)."
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    log "[04] docker not found — skipping prune."
    return 0
  fi
  log "[04] Pruning unused Docker data (this deletes compiled ORANSlice images)..."
  docker system prune -af >/dev/null 2>&1 || true
}

verify_clean() {
  local warn=0 name d pat
  if command -v kind >/dev/null 2>&1; then
    for name in "${CLUSTERS[@]}"; do
      if kind_has "${name}"; then
        log "[warn] Kind cluster still present: ${name}"
        warn=1
      fi
    done
  fi
  if command -v docker >/dev/null 2>&1; then
    while IFS= read -r name; do
      case "${name}" in
        tm-oranslice*|tm1-oranslice*)
          log "[warn] Docker container still present: ${name}"
          warn=1
          ;;
      esac
    done < <(docker ps -a --format '{{.Names}}' 2>/dev/null || true)
  fi
  for d in "${RUNTIME_DIRS[@]}"; do
    if [[ -e "${d}" ]]; then
      log "[warn] Runtime still present: ${d}"
      warn=1
    fi
  done
  for pat in "${HOST_PATTERNS[@]}"; do
    if pgrep -f "${pat}" >/dev/null 2>&1; then
      log "[warn] Host process still matching: ${pat}"
      warn=1
    fi
  done
  return "${warn}"
}

main() {
  log "========================================"
  log " IEEE S&P 2027 FlyBlind — testbed teardown"
  log " paper + mitigation"
  log "========================================"

  teardown_host
  teardown_kind
  teardown_kind_containers
  teardown_kubeconfig
  teardown_runtime
  teardown_docker_prune

  if ! verify_clean; then
    log "[05] Retrying leftover sweep..."
    stop_matching_host
    teardown_kind
    teardown_kind_containers
    teardown_runtime
    verify_clean || true
  fi

  log "========================================"
  log " Teardown finished."
  log "========================================"
}

main "$@"

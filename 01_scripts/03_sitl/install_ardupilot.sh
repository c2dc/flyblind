#!/usr/bin/env bash
##################################################################################################################################
# Script: install_ardupilot.sh (bootstrap ArduPilot SITL for the C2 workloads)                                                   #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Installs ArduCopter SITL under 01_testbeds/vendor/ardupilot: reuse binary, extract runtime tarball, or clone+build#
# Invoked by start_testbed*.sh; writes runtime/env_sitl.sh.                                                                      #
#                                                                                                                                #
# Usage: 03_sitl/install_ardupilot.sh                                                                                            #
##################################################################################################################################

# Bootstrap ArduPilot SITL (ArduCopter) for the FlyBlind testbed.
#
# Installs into 01_testbeds/vendor/ardupilot (upstream tree — do not patch here).
# Our flight logic lives in 03_sitl/lib/*.py and is copied to pods separately.
#
# Resolution order:
#   1. arducopter + copter.parm already present → skip
#   2. ardupilot-sitl-runtime.tar.xz → fast extract (~5 MB, no git/build)
#   3. git clone Copter-4.5.7 + waf build (first run, needs network)
#
# Invoked by start_testbed.sh or start_testbed_mitigation.sh; writes runtime/env_sitl.sh.
set -euo pipefail

# --- Paths ---

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_ROOT="$(cd "${INSTALL_DIR}/.." && pwd)"
TESTBEDS_DIR="${SCRIPTS_ROOT}/01_testbeds"
SITL_ROOT="${SITL_ROOT:-${ARDUPILOT_ROOT:-${TESTBEDS_DIR}/vendor/ardupilot}}"
SITL_TAG="${SITL_TAG:-Copter-4.5.7}"
BUILD_LOG="${BUILD_LOG:-${TESTBEDS_DIR}/runtime/config/ardupilot-build.log}"
COPTER_BIN="${SITL_ROOT}/build/sitl/bin/arducopter"
DEFAULTS="${SITL_ROOT}/Tools/autotest/default_params/copter.parm"

log() { echo "[install] $*"; }
fail_build() {
  log "BUILD FAILED — last 50 lines of ${BUILD_LOG}:"
  tail -50 "${BUILD_LOG}" 2>/dev/null || true
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "missing command: $1"; exit 1; }
}

sitl_runtime_ready() {
  [[ -x "${COPTER_BIN}" && -f "${DEFAULTS}" ]]
}

write_env_sitl() {
  mkdir -p "${TESTBEDS_DIR}/runtime"
  cat > "${TESTBEDS_DIR}/runtime/env_sitl.sh" <<EOF
# Source: source ${TESTBEDS_DIR}/runtime/env_sitl.sh
export ARDUPILOT_ROOT="${SITL_ROOT}"
export PATH="\${ARDUPILOT_ROOT}/Tools/autotest:\${PATH}"
EOF
}

# --- Fast path: pre-built runtime tarball (artifact distribution) ---

find_runtime_tarball() {
  local candidates=(
    "${ARDUPILOT_RUNTIME_TARBALL:-}"
    "${INSTALL_DIR}/ardupilot-sitl-runtime.tar.xz"
  )
  local t
  for t in "${candidates[@]}"; do
    if [[ -n "${t}" && -f "${t}" ]]; then
      printf '%s\n' "${t}"
      return 0
    fi
  done
  return 1
}

extract_runtime_tarball() {
  local tarball="$1"
  log "Extracting SITL runtime ${tarball} → ${SITL_ROOT}"
  need_cmd tar
  mkdir -p "${SITL_ROOT}"
  tar -xJf "${tarball}" -C "${SITL_ROOT}"
  chmod +x "${COPTER_BIN}" 2>/dev/null || true
  if ! sitl_runtime_ready; then
    log "ERROR: tarball did not provide ${COPTER_BIN} and ${DEFAULTS}"
    exit 1
  fi
  log "Runtime tarball OK — ${COPTER_BIN}"
}

# --- Slow path: clone + waf build from upstream ---

install_from_source() {
  need_cmd git
  need_cmd python3

  mkdir -p "$(dirname "${BUILD_LOG}")"
  : > "${BUILD_LOG}"

  if [[ ! -d "${SITL_ROOT}/.git" ]]; then
    log "Cloning ArduPilot ${SITL_TAG} (shallow) → ${SITL_ROOT}"
    if ! git clone --depth 1 --branch "${SITL_TAG}" \
        https://github.com/ArduPilot/ardupilot.git "${SITL_ROOT}" >>"${BUILD_LOG}" 2>&1; then
      fail_build
    fi
  else
    log "ArduPilot tree present at ${SITL_ROOT} — skip clone"
  fi

  cd "${SITL_ROOT}"

  if [[ ! -f "${SITL_ROOT}/modules/mavlink/.git" ]]; then
    log "Updating submodules (quiet — log: ${BUILD_LOG})…"
    if ! git submodule update --init --recursive --depth 1 >>"${BUILD_LOG}" 2>&1; then
      fail_build
    fi
  fi

  if [[ ! -x "${SITL_ROOT}/Tools/autotest/sim_vehicle.py" ]]; then
    log "ERROR: sim_vehicle.py not found after submodule init"
    exit 1
  fi

  if [[ ! -x "${COPTER_BIN}" ]]; then
    if [[ -x "${SITL_ROOT}/Tools/environment_install/install-prereqs-ubuntu.sh" ]]; then
      log "Installing Ubuntu prereqs (may use sudo once — quiet)…"
      "${SITL_ROOT}/Tools/environment_install/install-prereqs-ubuntu.sh" -y >>"${BUILD_LOG}" 2>&1 || true
    fi
    log "Building arducopter SITL (quiet — log: ${BUILD_LOG})…"
    if ! ./waf configure --board sitl >>"${BUILD_LOG}" 2>&1; then
      fail_build
    fi
    if ! ./waf copter >>"${BUILD_LOG}" 2>&1; then
      fail_build
    fi
    log "Build OK — ${COPTER_BIN}"
  else
    log "arducopter binary already built — skip waf"
  fi
}

# --- Entry ---

if sitl_runtime_ready; then
  log "SITL runtime already present — skip install"
  write_env_sitl
  log "  ARDUPILOT_ROOT=${SITL_ROOT}"
  log "  arducopter:   ${COPTER_BIN}"
  exit 0
fi

if tarball="$(find_runtime_tarball)"; then
  extract_runtime_tarball "${tarball}"
else
  log "No runtime tarball — clone+build from GitHub (${SITL_TAG})"
  log "  Tip: place ardupilot-sitl-runtime.tar.xz in 03_sitl/ to skip build"
  install_from_source
fi

write_env_sitl
log "SITL ready:"
log "  ARDUPILOT_ROOT=${SITL_ROOT}"
log "  arducopter:   ${COPTER_BIN}"
log "  defaults:     ${DEFAULTS}"

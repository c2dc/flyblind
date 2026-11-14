#!/usr/bin/env bash
##################################################################################################################################
# Script: pack_ardupilot_runtime.sh (minimal SITL runtime tarball for artifact distribution)                                     #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Packs arducopter + copter.parm (~5 MB) after a local waf build for shipping with the artifact bundle.             #
#                                                                                                                                #
# Usage: ./pack_ardupilot_runtime.sh [output.tar.xz]                                                                             #
##################################################################################################################################

# Pack minimal ArduPilot SITL runtime for artifact distribution (~5 MB).
#
# Ships only what install_ardupilot.sh needs to extract — not the full git tree.
# Run after a successful waf build under 01_testbeds/vendor/ardupilot.
#
# Usage:
#   ./pack_ardupilot_runtime.sh [output.tar.xz]
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_ROOT="$(cd "${INSTALL_DIR}/.." && pwd)"
SITL_ROOT="${SITL_ROOT:-${SCRIPTS_ROOT}/01_testbeds/vendor/ardupilot}"
OUT="${1:-${INSTALL_DIR}/ardupilot-sitl-runtime.tar.xz}"
TAG="${SITL_TAG:-Copter-4.5.7}"

COPTER="${SITL_ROOT}/build/sitl/bin/arducopter"
DEFAULTS="${SITL_ROOT}/Tools/autotest/default_params/copter.parm"

log() { echo "[pack] $*"; }

[[ -x "${COPTER}" ]] || {
  log "missing ${COPTER} — run ../01_testbeds/start_testbed.sh (or start_testbed_mitigation.sh) once or 03_sitl/install_ardupilot.sh"
  exit 1
}
[[ -f "${DEFAULTS}" ]] || {
  log "missing ${DEFAULTS}"
  exit 1
}

log "Packing ${TAG} runtime → ${OUT}"
tar -cJf "${OUT}" \
  -C "${SITL_ROOT}" \
  build/sitl/bin/arducopter \
  Tools/autotest/default_params/copter.parm

log "Done: $(du -h "${OUT}" | awk '{print $1}')"
log "Ship this file with the artifact; install_ardupilot.sh extracts it on first bootstrap."

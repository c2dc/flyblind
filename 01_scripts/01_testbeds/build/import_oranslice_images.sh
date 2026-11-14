#!/usr/bin/env bash
##################################################################################################################################
# Script: import_oranslice_images.sh — load ORANSlice tar bundles into Docker
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G
#
# Author: (anonymous; double-blind submission).
# Institution: withheld (double-blind review).
# Date: 2026-11
# License: MIT License
#
# Description: Imports pre-built image tarballs into local Docker for start_testbed* ensure_oranslice_images().
#
# Usage: ./import_oranslice_images.sh [paper|mitigation]
##################################################################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTBEDS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_TAG="${ORANSLICE_IMAGE_TAG:-2024w28}"
PAPER_BUNDLE_DIR="${ORANSLICE_IMAGE_BUNDLE_DIR:-${TESTBEDS_DIR}/images/paper}"
MITIGATION_BUNDLE_DIR="${ORANSLICE_MITIGATION_BUNDLE_DIR:-${TESTBEDS_DIR}/images/mitigation}"
ARG="${1:-both}"

log() { echo "[import-oranslice] $*"; }
die() { echo "[import-oranslice][ERROR] $*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Command '$1' not found."; }

load_tar() {
  local tar="$1"
  [[ -f "${tar}" ]] || die "Bundle not found: ${tar}"
  log "Loading ${tar} ($(du -h "${tar}" | awk '{print $1}'))..."
  docker load -i "${tar}"
  log "OK loaded ${tar}"
}

need_cmd docker

case "${ARG}" in
  paper)
    load_tar "${PAPER_BUNDLE_DIR}/oranslice-paper-${IMAGE_TAG}.tar"
    ;;
  mitigation)
    load_tar "${MITIGATION_BUNDLE_DIR}/oranslice-mitigation-${IMAGE_TAG}.tar"
    ;;
  both)
    loaded=0
    if [[ -f "${PAPER_BUNDLE_DIR}/oranslice-paper-${IMAGE_TAG}.tar" ]]; then
      load_tar "${PAPER_BUNDLE_DIR}/oranslice-paper-${IMAGE_TAG}.tar"
      loaded=1
    else
      log "SKIP paper — missing ${PAPER_BUNDLE_DIR}/oranslice-paper-${IMAGE_TAG}.tar"
    fi
    if [[ -f "${MITIGATION_BUNDLE_DIR}/oranslice-mitigation-${IMAGE_TAG}.tar" ]]; then
      load_tar "${MITIGATION_BUNDLE_DIR}/oranslice-mitigation-${IMAGE_TAG}.tar"
      loaded=1
    else
      log "SKIP mitigation — missing ${MITIGATION_BUNDLE_DIR}/oranslice-mitigation-${IMAGE_TAG}.tar"
    fi
    (( loaded == 1 )) || die "No bundles found in ${PAPER_BUNDLE_DIR} or ${MITIGATION_BUNDLE_DIR}"
    ;;
  *.tar|*.tar.gz)
    load_tar "${ARG}"
    ;;
  -h|--help)
    sed -n '2,16p' "$0" | sed 's/^# \?//'
    exit 0
    ;;
  *)
    die "Unknown arg '${ARG}' (use: paper | mitigation | both | /path/to/file.tar)"
    ;;
esac

log "Verify tags:"
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' \
  | grep -E "oai-(gnb|nr-ue)-oranslice" || true
log "Done. ./start_testbed.sh / ./start_testbed_mitigation.sh will skip compile if tags are present."
log "Note: recreating a Kind cluster still needs kind load (minutes), not a full rebuild."

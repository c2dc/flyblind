#!/usr/bin/env bash
##################################################################################################################################
# Script: export_oranslice_images.sh — save ORANSlice images to tar bundles
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G
#
# Author: (anonymous; double-blind submission).
# Institution: withheld (double-blind review).
# Date: 2026-11
# License: MIT License
#
# Description: Exports local Docker tags to images/paper/ or images/mitigation/ for offline bring-up on other hosts.
# Does not change start_testbed* behavior after import.
#
# Usage: ./export_oranslice_images.sh [paper|mitigation]
##################################################################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTBEDS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_TAG="${ORANSLICE_IMAGE_TAG:-2024w28}"
PAPER_BUNDLE_DIR="${ORANSLICE_IMAGE_BUNDLE_DIR:-${TESTBEDS_DIR}/images/paper}"
MITIGATION_BUNDLE_DIR="${ORANSLICE_MITIGATION_BUNDLE_DIR:-${TESTBEDS_DIR}/images/mitigation}"
MODE="${1:-both}"

log() { echo "[export-oranslice] $*"; }
die() { echo "[export-oranslice][ERROR] $*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Command '$1' not found."; }

export_pair() {
  local build_hint="$1" gnb="$2" ue="$3" out="$4"
  docker image inspect "${gnb}" >/dev/null 2>&1 \
    || die "Missing image ${gnb} — build first: ${build_hint}"
  docker image inspect "${ue}" >/dev/null 2>&1 \
    || die "Missing image ${ue} — build first: ${build_hint}"
  mkdir -p "$(dirname "${out}")"
  log "Saving ${gnb} + ${ue} → ${out}"
  docker save "${gnb}" "${ue}" -o "${out}"
  log "OK $(du -h "${out}" | awk '{print $1}')  ${out}"
}

need_cmd docker
case "${MODE}" in
  paper)
    export_pair "${SCRIPT_DIR}/build_oranslice_images.sh" \
      "localhost/oai-gnb-oranslice:${IMAGE_TAG}" \
      "localhost/oai-nr-ue-oranslice:${IMAGE_TAG}" \
      "${PAPER_BUNDLE_DIR}/oranslice-paper-${IMAGE_TAG}.tar"
    ;;
  mitigation)
    export_pair "${SCRIPT_DIR}/build_oranslice_images_mitigation.sh" \
      "localhost/oai-gnb-oranslice-mitigation:${IMAGE_TAG}" \
      "localhost/oai-nr-ue-oranslice-mitigation:${IMAGE_TAG}" \
      "${MITIGATION_BUNDLE_DIR}/oranslice-mitigation-${IMAGE_TAG}.tar"
    ;;
  both)
    local_ok=0
    if docker image inspect "localhost/oai-gnb-oranslice:${IMAGE_TAG}" >/dev/null 2>&1 \
       && docker image inspect "localhost/oai-nr-ue-oranslice:${IMAGE_TAG}" >/dev/null 2>&1; then
      export_pair "${SCRIPT_DIR}/build_oranslice_images.sh" \
        "localhost/oai-gnb-oranslice:${IMAGE_TAG}" \
        "localhost/oai-nr-ue-oranslice:${IMAGE_TAG}" \
        "${PAPER_BUNDLE_DIR}/oranslice-paper-${IMAGE_TAG}.tar"
      local_ok=1
    else
      log "SKIP paper — images not present locally"
    fi
    if docker image inspect "localhost/oai-gnb-oranslice-mitigation:${IMAGE_TAG}" >/dev/null 2>&1 \
       && docker image inspect "localhost/oai-nr-ue-oranslice-mitigation:${IMAGE_TAG}" >/dev/null 2>&1; then
      export_pair "${SCRIPT_DIR}/build_oranslice_images_mitigation.sh" \
        "localhost/oai-gnb-oranslice-mitigation:${IMAGE_TAG}" \
        "localhost/oai-nr-ue-oranslice-mitigation:${IMAGE_TAG}" \
        "${MITIGATION_BUNDLE_DIR}/oranslice-mitigation-${IMAGE_TAG}.tar"
      local_ok=1
    else
      log "SKIP mitigation — images not present locally"
    fi
    (( local_ok == 1 )) || die "No paper/mitigation images found. Build at least one stack first."
    ;;
  -h|--help)
    sed -n '2,16p' "$0" | sed 's/^# \?//'
    exit 0
    ;;
  *)
    die "Unknown mode '${MODE}' (use: paper | mitigation | both)"
    ;;
esac

log "Done. Import later with: ${SCRIPT_DIR}/import_oranslice_images.sh"
log "Then: ../start_testbed.sh  OR  ../start_testbed_mitigation.sh  (build skipped if tags present)"

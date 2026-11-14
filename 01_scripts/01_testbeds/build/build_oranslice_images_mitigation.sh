#!/usr/bin/env bash
##################################################################################################################################
# Script: build_oranslice_images_mitigation.sh — ORANSlice mitigation Docker images
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G
#
# Author: (anonymous; double-blind submission).
# Institution: withheld (double-blind review).
# Date: 2026-11
# License: MIT License
#
# Description: Builds mitigation gNB + nr-UE images (ul_rrmpolicy patch). Clones vendor/ORANSlice-mitigation if missing.
# Env: ORANSLICE_ROOT, ORANSLICE_IMAGE_TAG, SKIP_KIND_LOAD=1.
#
# Usage: ./build_oranslice_images_mitigation.sh
##################################################################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTBEDS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENDOR_DIR="${TESTBEDS_DIR}/vendor"
ORANSLICE_ROOT="${ORANSLICE_ROOT:-${VENDOR_DIR}/ORANSlice-mitigation}"
ORANSLICE_RAN="${ORANSLICE_RAN:-${ORANSLICE_ROOT}/oai_ran}"
IMAGE_TAG="${ORANSLICE_IMAGE_TAG:-2024w28}"
GNB_IMAGE="localhost/oai-gnb-oranslice-mitigation:${IMAGE_TAG}"
UE_IMAGE="localhost/oai-nr-ue-oranslice-mitigation:${IMAGE_TAG}"
BASE_TAG="ran-base-mitigation:latest"
BUILD_TAG="ran-build-mitigation:latest"
CLUSTER_NAME="${CLUSTER_NAME:-tm-oranslice-mitigation}"
PATCH_FILE="${ORANSLICE_ROOT}/doc/rrmPolicyJson.patch"
UL_PATCH_FILE="${TESTBEDS_DIR}/patches/ul_rrmpolicy_mitigation.patch"

log() { echo "[build-oranslice-mitigation] $*"; }
die() { echo "[build-oranslice-mitigation][ERROR] $*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Command '$1' not found."; }

clone_oranslice() {
  if [[ -d "${ORANSLICE_RAN}/.git" || -f "${ORANSLICE_RAN}/CMakeLists.txt" ]]; then
    log "ORANSlice RAN tree: ${ORANSLICE_RAN}"
    return 0
  fi
  need_cmd git
  mkdir -p "$(dirname "${ORANSLICE_ROOT}")"
  log "Cloning ORANSlice → ${ORANSLICE_ROOT}"
  git clone --depth 1 https://github.com/wineslab/ORANSlice.git "${ORANSLICE_ROOT}"
}

apply_rrm_policy_patch() {
  [[ -f "${PATCH_FILE}" ]] || die "Patch missing: ${PATCH_FILE}"
  local sched="${ORANSLICE_RAN}/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c"
  # Match only an *active* call (not the commented-out upstream line).
  if grep -qE '^[[:space:]]*nr_update_slice_policy\(module_id\);' "${sched}" 2>/dev/null; then
    log "rrmPolicyJson.patch already applied (nr_update_slice_policy active)"
    return 0
  fi
  log "Applying doc/rrmPolicyJson.patch..."
  (cd "${ORANSLICE_ROOT}" && git apply --check doc/rrmPolicyJson.patch)
  (cd "${ORANSLICE_ROOT}" && git apply doc/rrmPolicyJson.patch)
  grep -qE '^[[:space:]]*nr_update_slice_policy\(module_id\);' "${sched}" \
    || die "Patch apply failed — nr_update_slice_policy still inactive in ${sched}"
  log "Patch applied."
}

ul_rrmpolicy_patch_present() {
  local sched="${ORANSLICE_RAN}/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c"
  local hdr="${ORANSLICE_RAN}/openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB.h"
  grep -q 'ul_thr_ue_slice' "${hdr}" 2>/dev/null \
    && grep -qE 'tm_pf_ul_rrmpolicy|FlyBlind(1)?_UL_RRMPOLICY' "${sched}" 2>/dev/null
}

apply_ul_rrmpolicy_patch() {
  [[ -f "${UL_PATCH_FILE}" ]] || die "Mitigation patch missing: ${UL_PATCH_FILE}"
  if ul_rrmpolicy_patch_present; then
    log "UL rrmPolicy mitigation patch already applied"
    return 0
  fi
  log "Applying UL rrmPolicy mitigation patch (NSSAI-keyed, 3GPP RRMPolicyRatio)..."
  (cd "${ORANSLICE_RAN}" && git apply --check "${UL_PATCH_FILE}") \
    || die "UL patch does not apply to ${ORANSLICE_RAN} — reset vendor/ORANSlice-mitigation or fix tree manually"
  (cd "${ORANSLICE_RAN}" && git apply "${UL_PATCH_FILE}")
  ul_rrmpolicy_patch_present \
    || die "UL rrmpolicy patch markers missing after apply (ul_thr_ue_slice / tm_pf_ul_rrmpolicy)"
}

# Silence Docker BuildKit LegacyKeyValueFormat on ENV TINI_VERSION (upstream uses space form).
fix_dockerfile_base_env_format() {
  local df="${ORANSLICE_RAN}/docker/Dockerfile.base.ubuntu22"
  [[ -f "${df}" ]] || die "Missing ${df}"
  if grep -qE '^ENV TINI_VERSION[[:space:]]+[^=]' "${df}"; then
    sed -i 's/^ENV TINI_VERSION[[:space:]]\+\(.*\)$/ENV TINI_VERSION=\1/' "${df}"
    log "Fixed legacy ENV TINI_VERSION format in Dockerfile.base.ubuntu22"
  fi
}

build_docker_images() {
  need_cmd docker
  local dockerfile_build="${TESTBEDS_DIR}/docker/Dockerfile.build.oranslice.mitigation.ubuntu22"
  local dockerfile_gnb="${TESTBEDS_DIR}/docker/Dockerfile.gNB.oranslice.mitigation.ubuntu22"
  local dockerfile_ue="${TESTBEDS_DIR}/docker/Dockerfile.nrUE.oranslice.mitigation.ubuntu22"

  fix_dockerfile_base_env_format
  log "Building ${BASE_TAG} (context ${ORANSLICE_RAN})..."
  docker build --target ran-base --tag "${BASE_TAG}" \
    --file "${ORANSLICE_RAN}/docker/Dockerfile.base.ubuntu22" "${ORANSLICE_RAN}"

  log "Building ${BUILD_TAG} (SIMU, gNB+nrUE) — expect 30–90 min..."
  docker build --target ran-build --tag "${BUILD_TAG}" \
    --file "${dockerfile_build}" "${ORANSLICE_RAN}"

  log "Building gNB target → ${GNB_IMAGE}"
  docker build --tag "${GNB_IMAGE}" \
    --file "${dockerfile_gnb}" "${TESTBEDS_DIR}/docker"

  log "Building nr-UE target → ${UE_IMAGE}"
  docker build --tag "${UE_IMAGE}" \
    --file "${dockerfile_ue}" "${TESTBEDS_DIR}/docker"

  for img in "${GNB_IMAGE}" "${UE_IMAGE}"; do
    docker image inspect "${img}" >/dev/null \
      || die "Build reported success but image missing: ${img}"
    log "  image OK: ${img}"
  done
}

load_into_kind() {
  if [[ "${SKIP_KIND_LOAD:-0}" == "1" ]]; then
    log "SKIP_KIND_LOAD=1 — skipping kind load"
    return 0
  fi
  need_cmd kind
  if ! kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
    log "Kind cluster ${CLUSTER_NAME} not found — skip load (run start_testbed_mitigation.sh after cluster exists)"
    return 0
  fi
  kind load docker-image "${GNB_IMAGE}" --name "${CLUSTER_NAME}"
  kind load docker-image "${UE_IMAGE}" --name "${CLUSTER_NAME}"
  log "Loaded into kind-${CLUSTER_NAME}"
}

main() {
  clone_oranslice
  apply_rrm_policy_patch
  apply_ul_rrmpolicy_patch
  build_docker_images
  load_into_kind
  log "Done. Images: ${GNB_IMAGE}, ${UE_IMAGE}"
  log "Intermediate tags (mitigation-only): ${BASE_TAG}, ${BUILD_TAG}"
}

main "$@"

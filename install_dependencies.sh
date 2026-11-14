#!/usr/bin/env bash
##################################################################################################################################
# Script: install_dependencies.sh (host bootstrap for the FlyBlind artifact testbeds)                                            #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: One-shot host setup for a clean Debian-derived Linux workstation before start_testbed*.sh.                        #
# Installs Docker Engine (+ Compose v2 plugin), kubectl, kind v0.20.0, SCTP, inotify sysctl tuning, and the                      #
# command-line tools the testbed/orchestrator scripts expect on the host (git, socat, iptables, iperf3, PyYAML, pymavlink).      #
# Step [00] removes stale Docker apt list fragments so apt update succeeds on Mint/Ubuntu derivatives.                           #
#                                                                                                                                #
# Requirements (recommended): amd64 or arm64, 16 GB+ RAM, 50 GB+ free disk, outbound HTTPS.                                      #
# Must run as root. After install: newgrp docker (or re-login), then start_testbed.sh / start_testbed_mitigation.sh.             #
#                                                                                                                                #
# Usage: sudo bash install_dependencies.sh                                                                                       #
##################################################################################################################################

set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
YELLOW="\033[33m"
RESET="\033[0m"
log() { echo -e "${YELLOW}$*${RESET}"; }
die() { echo -e "${YELLOW}[ERROR] $*${RESET}" >&2; exit 1; }

if [[ "${EUID:-0}" -ne 0 ]]; then
  die "Run as root: sudo bash ${0}"
fi

if [[ ! -f /etc/os-release ]]; then
  die "/etc/os-release not found — this script targets Debian-derived systems with apt."
fi
# shellcheck source=/dev/null
. /etc/os-release

# ---------------------------------------------------------------------------
# OS / Docker repo mapping (Ubuntu, Debian, Mint and ID_LIKE derivatives)
# ---------------------------------------------------------------------------
DOCKER_APT_ID=""
case "${ID}" in
  debian|ubuntu) DOCKER_APT_ID="${ID}" ;;
  linuxmint) DOCKER_APT_ID="ubuntu" ;;
  *)
    if echo "${ID_LIKE:-}" | grep -qi ubuntu; then
      DOCKER_APT_ID="ubuntu"
    elif echo "${ID_LIKE:-}" | grep -qi debian; then
      DOCKER_APT_ID="debian"
    else
      die "Unsupported OS ID='${ID}' (need Debian/Ubuntu or ID_LIKE=debian/ubuntu)."
    fi
    ;;
esac

[[ -n "${VERSION_CODENAME:-}" ]] || die "VERSION_CODENAME is empty."

DOCKER_APT_CODENAME="${VERSION_CODENAME}"
if [[ "${DOCKER_APT_ID}" == "ubuntu" ]]; then
  if [[ -n "${UBUNTU_CODENAME:-}" ]]; then
    DOCKER_APT_CODENAME="${UBUNTU_CODENAME}"
  elif [[ "${ID:-}" == "linuxmint" ]] && [[ -f /etc/upstream-release/lsb-release ]]; then
    # shellcheck source=/dev/null
    . /etc/upstream-release/lsb-release
    DOCKER_APT_CODENAME="${DISTRIB_CODENAME:-${VERSION_CODENAME}}"
  fi
fi
[[ -n "${DOCKER_APT_CODENAME}" ]] || die "Could not resolve Docker apt codename."

TARGET_USER="${SUDO_USER:-${USER:-root}}"
if [[ "${TARGET_USER}" == "root" && -n "${SUDO_USER:-}" ]]; then
  TARGET_USER="${SUDO_USER}"
fi

arch="$(dpkg --print-architecture)"
case "${arch}" in
  amd64) K8S_ARCH="amd64"; KIND_ARCH="amd64" ;;
  arm64) K8S_ARCH="arm64"; KIND_ARCH="arm64" ;;
  *)
    die "Unsupported dpkg architecture '${arch}' (need amd64 or arm64)."
    ;;
esac

# Download executable to /usr/local/bin without clobbering a stale /tmp/kind (curl error 23).
install_url_bin() {
  local url="$1" dest="$2" label="$3" tmp
  tmp="$(mktemp)"
  log "  GET ${label} <= ${url}"
  if ! curl -fsSL -o "${tmp}" "${url}"; then
    rm -f "${tmp}"
    die "curl failed downloading ${label} from ${url}"
  fi
  chmod +x "${tmp}"
  install -m 0755 "${tmp}" "${dest}"
  rm -f "${tmp}"
}

install_pymavlink_host() {
  if python3 -c "import pymavlink" 2>/dev/null; then
    log "  pymavlink already importable on host Python"
    return 0
  fi
  log "  installing pymavlink for host-side start_testbed / orchestrate tooling…"
  if python3 -m pip install --upgrade pip >/dev/null 2>&1; then
    :
  fi
  if python3 -m pip install pymavlink >/dev/null 2>&1; then
    :
  elif python3 -m pip install --break-system-packages pymavlink >/dev/null 2>&1; then
    :
  else
    die "failed to install pymavlink (needed by start_testbed when staging C2 tools)"
  fi
  python3 -c "import pymavlink; print('[OK] pymavlink')" \
    || die "'import pymavlink' failed after pip install"
}

verify_host_toolchain() {
  local missing=() cmd
  for cmd in \
    docker kubectl kind curl wget git python3 sha256sum taskset ss socat iptables nc iperf3 unzip jq; do
    command -v "${cmd}" >/dev/null 2>&1 || missing+=("${cmd}")
  done
  if ((${#missing[@]})); then
    die "post-install check failed — missing on PATH: ${missing[*]}"
  fi
  python3 -c "import yaml" || die "PyYAML import failed"
  python3 -c "import pymavlink" || die "pymavlink import failed"
  modprobe sctp 2>/dev/null || die "SCTP module not loadable (check kernel / kmod)"
  log "[verify] host toolchain OK (${arch})"
}

log "============================================================================"
log " IEEE S&P 2027 FlyBlind — dependency installation (automated)"
log "============================================================================"
log "OS: ${PRETTY_NAME:-${ID}} | Docker repo: ${DOCKER_APT_ID}/${DOCKER_APT_CODENAME}"
log "Host VERSION_CODENAME=${VERSION_CODENAME} | apt arch=${arch} | k8s/kind arch=${K8S_ARCH}"
log "docker group membership for: ${TARGET_USER}"
log ""

# ---------------------------------------------------------------------------
# [00] Remove stale Docker *.list/*.sources fragments so apt update succeeds
# ---------------------------------------------------------------------------
log "[00] Remove /etc/apt/sources.list.d entries pointing at download.docker.com (wrong codename)."
shopt -s nullglob
for f in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [[ -f "$f" ]] || continue
  if grep -q 'download\.docker\.com' "$f" 2>/dev/null; then
    log "  rm ${f}"
    rm -f "${f}"
  fi
done
shopt -u nullglob

# ---------------------------------------------------------------------------
# [01] Base packages used by start_testbed*, orchestrate*, and image builds
# ---------------------------------------------------------------------------
log "[01] apt update && base tooling…"
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  wget \
  unzip \
  xz-utils \
  tar \
  git \
  sudo \
  gnupg \
  jq \
  tcpdump \
  traceroute \
  net-tools \
  conntrack \
  wireshark \
  python3 \
  python3-yaml \
  python3-pip \
  iproute2 \
  iputils-ping \
  iperf3 \
  socat \
  netcat-openbsd \
  iptables \
  kmod \
  procps \
  util-linux \
  coreutils

echo "wireshark-common wireshark-common/install-setuid boolean true" | debconf-set-selections || true

# ---------------------------------------------------------------------------
# [02] Docker Engine (Compose v2 plugin included)
# ---------------------------------------------------------------------------
log "[02] Docker Engine: remove conflicting packages → keyring → stable repo → hello-world …"

apt-get remove -y docker-compose 2>/dev/null || true
apt-get --fix-broken install -y || true
apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

apt-get update -qq
apt-get install -y ca-certificates curl gnupg

install -m 0755 -d /etc/apt/keyrings
rm -f /etc/apt/keyrings/docker.gpg
curl -fsSL "https://download.docker.com/linux/${DOCKER_APT_ID}/gpg" | gpg --batch --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${DOCKER_APT_ID} ${DOCKER_APT_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

docker run --rm hello-world

groupadd docker 2>/dev/null || true
if id -u "${TARGET_USER}" >/dev/null 2>&1; then
  usermod -aG docker "${TARGET_USER}" || true
fi

systemctl start docker 2>/dev/null || true
systemctl enable docker 2>/dev/null || true

docker --version
docker compose version

# ---------------------------------------------------------------------------
# [03] kubectl (stable client; arch-aware)
# ---------------------------------------------------------------------------
log "[03] kubectl stable client → /usr/local/bin/kubectl"
kubectl_ver="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
install_url_bin \
  "https://dl.k8s.io/release/${kubectl_ver}/bin/linux/${K8S_ARCH}/kubectl" \
  /usr/local/bin/kubectl \
  "kubectl ${kubectl_ver} (${K8S_ARCH})"
kubectl version --client

# ---------------------------------------------------------------------------
# [04] kind v0.20.0 (pinned — matches testbed Kind clusters)
# ---------------------------------------------------------------------------
log "[04] kind v0.20.0 (linux-${KIND_ARCH}) → /usr/local/bin/kind"
rm -f /tmp/kind /tmp/kind.* 2>/dev/null || true
install_url_bin \
  "https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-${KIND_ARCH}" \
  /usr/local/bin/kind \
  "kind v0.20.0"
kind version

# ---------------------------------------------------------------------------
# [05] SCTP kernel module (OAI AMF NGAP port 38412)
# ---------------------------------------------------------------------------
log "[05] SCTP kernel module (modprobe sctp)…"
modprobe sctp 2>/dev/null || true
if [[ ! -f /etc/modules-load.d/sctp.conf ]]; then
  echo sctp > /etc/modules-load.d/sctp.conf
fi

# ---------------------------------------------------------------------------
# [06] inotify limits (Kind + many pod log streams — avoids "Too many open files")
# ---------------------------------------------------------------------------
log "[06] inotify limits (fs.inotify.max_user_* )…"
cat > /etc/sysctl.d/99-kind-inotify.conf <<EOF
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=512
EOF
sysctl --system >/dev/null 2>&1 || true
sysctl fs.inotify.max_user_watches fs.inotify.max_user_instances

# ---------------------------------------------------------------------------
# [07] Python packages used on the host (ConfigMap staging / orchestrate imports)
# ---------------------------------------------------------------------------
log "[07] Python host packages (PyYAML + pymavlink)…"
python3 -c "import yaml; print('[OK] PyYAML', yaml.__version__)" || die "'import yaml' failed."
install_pymavlink_host

# ---------------------------------------------------------------------------
# [08] Post-install verification
# ---------------------------------------------------------------------------
log "[08] Verifying host toolchain…"
verify_host_toolchain

log ""
log "============================================================================"
log " Done."
log "  1. newgrp docker   (or log out/in) — run Docker without sudo"
log "  2. cd artifacts/01_scripts/01_testbeds"
log "  3. ./start_testbed.sh              # paper stack (Kind tm-oranslice)"
log "     ./start_testbed_mitigation.sh   # mitigation stack"
log "  4. cd ../02_experiments && python3 orchestrate.py --help"
log ""
log " Notes:"
log "  - First ORANSlice image build clones git repos and may take 30–90 min."
log "  - Place 03_sitl/ardupilot-sitl-runtime.tar.xz to skip ArduPilot source build."
log "  - Recommended: 16 GB+ RAM, 50 GB+ disk, outbound HTTPS during bootstrap."
log "============================================================================"

#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

SERVICE_NAME="cnc-admin"
AUTO_SIZE_SERVICE_NAME="cnc-auto-size"
BACKEND_ALERTS_SERVICE_NAME="cnc-backend-alerts"
CLOUDFLARE_SYNC_SERVICE_NAME="cnc-cloudflare-sync"
UPDATE_CHECK_SERVICE_NAME="cnc-update-check"
ENV_FILE="/etc/cnc.env"
CLI_WRAPPER_PATH="/usr/local/bin/cnc-admin"
LLM_HELP_WRAPPER_PATH="/usr/local/bin/llm-help"
SSH_BACKEND_ROOT_WRAPPER_PATH="/usr/local/bin/cnc-ssh-backend-root"
SSH_BACKEND_SSHD_CONFIG_PATH="/etc/ssh/sshd_config.d/cnc-backend-users.conf"
SSH_BACKEND_SUDOERS_PATH="/etc/sudoers.d/cnc-backend-users"
SSH_BACKEND_AUTHORIZED_KEYS_PATH="/etc/ssh/cnc-backend-authorized_keys"
PACKAGED_SYSTEMD_UNIT_PATH="packaging/cnc-admin.service"
PACKAGED_AUTO_SIZE_SERVICE_PATH="packaging/cnc-auto-size.service"
PACKAGED_AUTO_SIZE_TIMER_PATH="packaging/cnc-auto-size.timer"
PACKAGED_BACKEND_ALERTS_SERVICE_PATH="packaging/cnc-backend-alerts.service"
PACKAGED_BACKEND_ALERTS_TIMER_PATH="packaging/cnc-backend-alerts.timer"
PACKAGED_CLOUDFLARE_SYNC_SERVICE_PATH="packaging/cnc-cloudflare-sync.service"
PACKAGED_CLOUDFLARE_SYNC_TIMER_PATH="packaging/cnc-cloudflare-sync.timer"
PACKAGED_UPDATE_CHECK_SERVICE_PATH="packaging/cnc-update-check.service"
PACKAGED_UPDATE_CHECK_TIMER_PATH="packaging/cnc-update-check.timer"
PACKAGED_SSH_BACKEND_ROOT_WRAPPER_PATH="packaging/cnc-ssh-backend-root"
SSH_BACKEND_HOME_ROOT="/var/lib/cnc/ssh-users"
NGINX_INCLUDE_PATH="/etc/nginx/conf.d/cnc-generated.conf"
NGINX_DEFAULT_DENY_PATH="/etc/nginx/conf.d/cnc-default-deny.conf"
NGINX_TUNING_PATH="/etc/nginx/conf.d/cnc-tuning.conf"
NGINX_MAIN_CONF="/etc/nginx/nginx.conf"
SYSCTL_CNC_PATH="/etc/sysctl.d/99-cnc.conf"
SYSCTL_BBR_PATH="/etc/sysctl.d/99-bbr.conf"
JOURNALD_CNC_RETENTION_PATH="/etc/systemd/journald.conf.d/99-cnc-retention.conf"
RSYSLOG_LOGROTATE_PATH="/etc/logrotate.d/rsyslog"
RUNTIME_ROOT="/var/lib/cnc"
CURRENT_LINK="${RUNTIME_ROOT}/current"
INSTALL_LOCK_FILE="${RUNTIME_ROOT}/install.lock"
NOFILE_LIMIT="262144"
PYTHON_BIN="${CNC_PYTHON_BIN:-python3}"
MANAGED_PODMAN_VERSION="5.6.2"
MANAGED_PODMAN_BASE_URL="https://github.com/mgoltzsche/podman-static/releases/download/v${MANAGED_PODMAN_VERSION}"
MANAGED_PODMAN_AMD64_SHA256="fb02d10525e924354c2992be5996218c24d3ffe45ca48803e4d1b19e2581e0d4"
MANAGED_PODMAN_ARM64_SHA256="7a931ca150f09cb0e5f80528e24b20c81b760e5852c15bd7ebc735933c8c13b2"
MANAGED_CRUN_VERSION="1.28"
MANAGED_CRUN_BASE_URL="https://github.com/containers/crun/releases/download/${MANAGED_CRUN_VERSION}"
MANAGED_CRUN_AMD64_SHA256="2aa6b7024a9c9f153895c0d11ae233d3758f54844011c3a039e3e89048d01d42"
MANAGED_CRUN_ARM64_SHA256="cc1e8ec89aef1422e0741be196f9ed099e2e09d2f48f30f27cd44a22ef1f0342"
MANAGED_CONMON_VERSION="2.1.13+ds1-2"
MANAGED_CONMON_AMD64_SHA256="61da0329bfc8b3f9af64b31a857a70640f41d21609f8cad4812e4ce1cd469dc5"
MANAGED_CONMON_ARM64_SHA256="f221d67b753ad395925a481257b6e2d8a08feee2d73405f5b6f9bd75b20e1fd6"
MANAGED_PODMAN_CACHE_ROOT="/var/cache/cnc/podman"
MANAGED_PODMAN_INSTALL_ROOT="/opt/cnc/podman"
MANAGED_PODMAN_COMMAND_PATH="/usr/bin/podman"
MANAGED_PODMAN_LOCAL_COMMAND_PATH="/usr/local/bin/podman"
MANAGED_PODMAN_DISTRO_PATH="/usr/bin/podman.cnc-distro"
MANAGED_PODMAN_CONFIG_PATH="/etc/containers/containers.conf.d/85-cnc-runtime.conf"
MANAGED_PODMAN_SYSTEM_GENERATOR_PATH="/etc/systemd/system-generators/podman-system-generator"
MANAGED_PODMAN_LOCK_TYPE="file"

INSTALL_LOG=""

APP_DIR=""
INSTALL_RELEASE_DIR=""
PRESERVED_ENV_FILE="0"
INITIAL_ACCESS_KEY=""
FAILED="0"
ERROR_CONTEXT=""
CURRENT_STEP_ID=""
START_TS="$(date +%s)"
UFW_SNAPSHOT_DIR=""
UFW_WAS_ACTIVE="0"

declare -a STEP_ORDER=()
declare -A STEP_LABEL=()
declare -A STEP_STATE=()
declare -A STEP_START=()
declare -A STEP_END=()
declare -A STEP_INFO=()

log() {
  printf '[install] %s\n' "$1"
  if [[ -n "${INSTALL_LOG:-}" && -f "${INSTALL_LOG}" ]]; then
    printf '[install] %s\n' "$1" >>"${INSTALL_LOG}"
  fi
}

installer_message() {
  printf '%s\n' "$1"
  if [[ -n "${INSTALL_LOG:-}" && -f "${INSTALL_LOG}" ]]; then
    printf '%s\n' "$1" >>"${INSTALL_LOG}"
  fi
}

run_cmd() {
  if [[ -n "${INSTALL_LOG}" ]]; then
    {
      printf '[cmd]'
      printf ' %q' "$@"
      printf '\n'
    } >>"${INSTALL_LOG}"
    if "$@" >>"${INSTALL_LOG}" 2>&1; then
      return 0
    else
      local rc=$?
      printf '[cmd][exit %s]\n' "${rc}" >>"${INSTALL_LOG}"
      return "${rc}"
    fi
  fi

  "$@" >/dev/null 2>&1
}

run_cmd_private() {
  local label="$1"
  shift
  if [[ -n "${INSTALL_LOG}" ]]; then
    printf '[cmd] %s (arguments redacted)\n' "${label}" >>"${INSTALL_LOG}"
    if "$@" >>"${INSTALL_LOG}" 2>&1; then
      return 0
    else
      local rc=$?
      printf '[cmd][exit %s]\n' "${rc}" >>"${INSTALL_LOG}"
      return "${rc}"
    fi
  fi
  "$@" >/dev/null 2>&1
}

with_retry() {
  local attempts="$1"
  local delay_seconds="$2"
  shift 2

  local try=1 rc
  while true; do
    if "$@"; then
      return 0
    else
      rc="$?"
    fi
    if (( try >= attempts )); then
      return "${rc}"
    fi
    log "retry ${try}/${attempts} failed for: $* (sleep ${delay_seconds}s)"
    sleep "${delay_seconds}"
    ((try += 1))
  done
}

print_rule() {
  printf '+------------------------------------------------------------------------+\n'
}

print_banner() {
  print_rule
  printf '| CNC INSTALLER                                                          |\n'
  printf '| Lean VPS setup for multi-project hosting                               |\n'
  print_rule
}

fail() {
  printf '[install][error] %s\n' "$1" >&2
  if [[ -n "${INSTALL_LOG:-}" && -f "${INSTALL_LOG}" ]]; then
    printf '[install][error] %s\n' "$1" >>"${INSTALL_LOG}"
  fi
  exit 1
}

acquire_install_lock() {
  install -d -m 0755 "${RUNTIME_ROOT}"
  exec 8>"${INSTALL_LOCK_FILE}"
  if ! flock -n 8; then
    fail "another install appears to be running (lock: ${INSTALL_LOCK_FILE})"
  fi
}

step_begin() {
  local id="$1"
  local label="$2"
  STEP_ORDER+=("${id}")
  STEP_LABEL["${id}"]="${label}"
  STEP_STATE["${id}"]="RUNNING"
  STEP_START["${id}"]="$(date +%s)"
  CURRENT_STEP_ID="${id}"
  installer_message "[install][....] ${label}"
}

step_finish() {
  local id="$1"
  local state="$2"
  local info="${3:-}"
  STEP_STATE["${id}"]="${state}"
  STEP_END["${id}"]="$(date +%s)"
  STEP_INFO["${id}"]="${info}"
  CURRENT_STEP_ID=""

  local elapsed=0
  if [[ -n "${STEP_START[$id]:-}" ]]; then
    elapsed=$(( STEP_END["${id}"] - STEP_START["${id}"] ))
  fi

  if [[ "${state}" == "OK" ]]; then
    installer_message "[install][ OK ] ${STEP_LABEL[$id]} (${elapsed}s)"
  else
    installer_message "[install][FAIL] ${STEP_LABEL[$id]} (${elapsed}s)"
  fi
}

show_log_tail() {
  if [[ -f "${INSTALL_LOG}" ]]; then
    printf '[install][error] tail of %s:\n' "${INSTALL_LOG}" >&2
    tail -n 40 "${INSTALL_LOG}" >&2 || true
  fi
}

run_step() {
  local id="$1"
  local label="$2"
  shift 2

  step_begin "${id}" "${label}"
  "$@"
  step_finish "${id}" "OK"
}

init_logging() {
  INSTALL_LOG="${CNC_INSTALL_LOG:-/var/log/cnc/installer.log}"
  install -d -m 0755 "$(dirname "${INSTALL_LOG}")"
  rotate_install_log
  : >"${INSTALL_LOG}"
  chmod 0600 "${INSTALL_LOG}"
  log "detailed command output -> ${INSTALL_LOG}"
  log "host: Ubuntu ${VERSION_ID} $(dpkg --print-architecture), kernel $(uname -r), Python command ${PYTHON_BIN}"
}

rotate_install_log() {
  local path="${INSTALL_LOG}"
  local keep="${CNC_INSTALL_LOG_KEEP:-5}"
  if ! [[ "${keep}" =~ ^[0-9]+$ ]] || (( keep < 1 )); then
    keep=5
  fi
  if [[ ! -e "${path}" ]]; then
    return 0
  fi

  local index previous
  for (( index = keep - 1; index >= 1; index -= 1 )); do
    previous="${path}.${index}"
    if [[ -e "${previous}" ]]; then
      mv -f "${previous}" "${path}.$((index + 1))"
    fi
  done
  mv -f "${path}" "${path}.1"
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "run this script as root (sudo ./scripts/install_ubuntu.sh)"
}

require_ubuntu() {
  if [[ ! -f /etc/os-release ]]; then
    fail "cannot detect OS: missing /etc/os-release"
  fi
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || fail "this installer targets Ubuntu only (detected: ${ID:-unknown})"

  case "${VERSION_ID:-}" in
    24.04 | 26.04) ;;
    *)
      fail "CNC supports Ubuntu 24.04 LTS and 26.04 LTS (detected: ${VERSION_ID:-unknown})."
      ;;
  esac
}

require_base_tools() {
  local missing=()
  local tool
  for tool in apt-get awk sed grep flock systemctl mktemp timeout dpkg dpkg-deb dpkg-divert sha256sum tar; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      missing+=("${tool}")
    fi
  done
  if (( ${#missing[@]} > 0 )); then
    fail "missing required system tools: ${missing[*]}"
  fi
}

require_supported_architecture() {
  local architecture
  architecture="$(dpkg --print-architecture)" || fail "cannot detect host architecture with dpkg"
  case "${architecture}" in
    amd64 | arm64) ;;
    *) fail "CNC supports amd64 and arm64 hosts (detected: ${architecture})" ;;
  esac
}

resolve_app_dir() {
  local input_dir="${CNC_APP_DIR:-$(pwd)}"
  if [[ "${input_dir}" != /* ]]; then
    input_dir="$(cd "${input_dir}" && pwd)"
  fi

  [[ "${input_dir}" != *" "* ]] || fail "app directory cannot contain spaces: ${input_dir}"
  [[ -f "${input_dir}/pyproject.toml" ]] || fail "missing pyproject.toml in ${input_dir}"
  [[ -f "${input_dir}/app/main.py" ]] || fail "missing app/main.py in ${input_dir}"

  APP_DIR="${input_dir}"
  log "using app dir: ${APP_DIR}"
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  with_retry 3 3 run_cmd apt-get -qq update || return $?
  with_retry 3 3 run_cmd apt-get -qq install -y \
    ca-certificates \
    openssl \
    curl \
    git \
    logrotate \
    nginx \
    podman \
    apparmor \
    acl \
    python3 \
    python3-pip \
    python3-venv \
    sqlite3 || return $?
}

require_supported_python() {
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    fail "Python runtime not found: ${PYTHON_BIN}"
  fi
  "${PYTHON_BIN}" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("CNC requires Python 3.11 or newer")
PY
}

require_supported_podman() {
  "${PYTHON_BIN}" "${APP_DIR}/app/services/guest_isolation.py" --check-runtime-only
}

select_managed_podman_asset() {
  case "$(dpkg --print-architecture)" in
    amd64)
      MANAGED_PODMAN_ARCH="amd64"
      MANAGED_PODMAN_SHA256="${MANAGED_PODMAN_AMD64_SHA256}"
      MANAGED_CRUN_SHA256="${MANAGED_CRUN_AMD64_SHA256}"
      MANAGED_CONMON_SHA256="${MANAGED_CONMON_AMD64_SHA256}"
      MANAGED_CONMON_BASE_URL="https://archive.ubuntu.com/ubuntu/pool/universe/c/conmon"
      ;;
    arm64)
      MANAGED_PODMAN_ARCH="arm64"
      MANAGED_PODMAN_SHA256="${MANAGED_PODMAN_ARM64_SHA256}"
      MANAGED_CRUN_SHA256="${MANAGED_CRUN_ARM64_SHA256}"
      MANAGED_CONMON_SHA256="${MANAGED_CONMON_ARM64_SHA256}"
      MANAGED_CONMON_BASE_URL="https://ports.ubuntu.com/pool/universe/c/conmon"
      ;;
    *)
      fail "CNC does not have a managed Podman build for host architecture: $(dpkg --print-architecture)"
      ;;
  esac
}

file_sha256() {
  local result
  result="$(sha256sum "$1")"
  printf '%s\n' "${result%% *}"
}

install_binary_atomic() {
  local source_path="$1"
  local destination_path="$2"
  run_cmd install -m 0755 "${source_path}" "${destination_path}.next" || return $?
  run_cmd mv -f "${destination_path}.next" "${destination_path}"
}

verify_staged_podman() {
  local runtime_root="$1"
  local required
  for required in \
    "${runtime_root}/bin/podman" \
    "${runtime_root}/bin/crun" \
    "${runtime_root}/libexec/conmon" \
    "${runtime_root}/libexec/quadlet" \
    "${runtime_root}/systemd/podman-system-generator"; do
    [[ -x "${required}" ]] || {
      log "staged runtime is missing ${required}"
      return 1
    }
  done
  local staged_version
  staged_version="$("${runtime_root}/bin/podman" --version 2>/dev/null | awk '{print $3}')" || true
  [[ -n "${staged_version}" ]] || {
    log "staged podman does not run"
    return 1
  }
  "${runtime_root}/bin/crun" --version >/dev/null 2>&1 || {
    log "staged crun does not run"
    return 1
  }
  "${runtime_root}/libexec/conmon" --version >/dev/null 2>&1 || {
    log "staged conmon does not run"
    return 1
  }
  log "staged runtime verified: podman ${staged_version}"
}

snapshot_managed_podman_activation() {
  local snapshot="$1"
  : >"${snapshot}"
  local path
  for path in \
    "${MANAGED_PODMAN_COMMAND_PATH}" \
    "${MANAGED_PODMAN_LOCAL_COMMAND_PATH}" \
    "${MANAGED_PODMAN_SYSTEM_GENERATOR_PATH}"; do
    if [[ -L "${path}" ]]; then
      printf 'link\t%s\t%s\n' "${path}" "$(readlink "${path}")" >>"${snapshot}"
    elif [[ -e "${path}" ]]; then
      printf 'file\t%s\t\n' "${path}" >>"${snapshot}"
    else
      printf 'absent\t%s\t\n' "${path}" >>"${snapshot}"
    fi
  done
  if [[ -e "${MANAGED_PODMAN_CONFIG_PATH}" ]]; then
    cp -f "${MANAGED_PODMAN_CONFIG_PATH}" "${snapshot}.conf"
    printf 'config\t%s\tpresent\n' "${MANAGED_PODMAN_CONFIG_PATH}" >>"${snapshot}"
  else
    rm -f "${snapshot}.conf"
    printf 'config\t%s\tabsent\n' "${MANAGED_PODMAN_CONFIG_PATH}" >>"${snapshot}"
  fi
  if dpkg-divert --list "${MANAGED_PODMAN_COMMAND_PATH}" 2>/dev/null | grep -q .; then
    printf 'divert\t%s\tpresent\n' "${MANAGED_PODMAN_COMMAND_PATH}" >>"${snapshot}"
  else
    printf 'divert\t%s\tabsent\n' "${MANAGED_PODMAN_COMMAND_PATH}" >>"${snapshot}"
  fi
}

restore_managed_podman_activation() {
  local snapshot="$1"
  [[ -f "${snapshot}" ]] || {
    log "no activation snapshot to restore from"
    return 1
  }
  log "restoring the previous runtime activation"
  local kind path value
  while IFS=$'\t' read -r kind path value; do
    case "${kind}" in
      link)
        ln -sfn "${value}" "${path}" 2>/dev/null \
          || log "restore: could not relink ${path}"
        ;;
      file)
        ;;
      absent)
        rm -f "${path}" 2>/dev/null || log "restore: could not remove ${path}"
        ;;
      config)
        if [[ "${value}" == "present" ]]; then
          cp -f "${snapshot}.conf" "${path}" 2>/dev/null \
            || log "restore: could not restore ${path}"
        else
          rm -f "${path}" 2>/dev/null || log "restore: could not remove ${path}"
        fi
        ;;
      divert)
        if [[ "${value}" == "absent" ]] \
          && dpkg-divert --list "${MANAGED_PODMAN_COMMAND_PATH}" 2>/dev/null | grep -q .; then
          rm -f "${MANAGED_PODMAN_COMMAND_PATH}" 2>/dev/null || true
          dpkg-divert --quiet --local --rename --remove \
            "${MANAGED_PODMAN_COMMAND_PATH}" >/dev/null 2>&1 \
            || log "restore: could not remove the podman diversion"
        fi
        ;;
    esac
  done <"${snapshot}"
  rm -f "${snapshot}" "${snapshot}.conf"
}

activate_managed_podman() {
  local runtime_root="$1"

  verify_staged_podman "${runtime_root}" || {
    log "refusing to activate an unverified runtime; leaving the current one in place"
    return 1
  }

  local snapshot="${MANAGED_PODMAN_INSTALL_ROOT}/.activation-snapshot"
  run_cmd install -d -m 0755 "${MANAGED_PODMAN_INSTALL_ROOT}" || return $?
  snapshot_managed_podman_activation "${snapshot}" || return $?

  if ! _activate_managed_podman_unsafe "${runtime_root}"; then
    restore_managed_podman_activation "${snapshot}"
    log "runtime activation failed and the previous runtime was restored"
    return 1
  fi

  if ! verify_supported_podman; then
    restore_managed_podman_activation "${snapshot}"
    log "the previous runtime was restored. Until the reason above is fixed on this host, app guests cannot start."
    return 1
  fi
  rm -f "${snapshot}" "${snapshot}.conf"
}

report_command_output() {
  local text="$1" line
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    log "  ${line}"
  done <<<"${text}"
}

verify_supported_podman() {
  local check_output
  if check_output="$(require_supported_podman 2>&1)"; then
    return 0
  fi
  report_command_output "${check_output}"
  return 1
}


_activate_managed_podman_unsafe() {
  local runtime_root="$1"

  run_cmd dpkg-divert --quiet --local --rename --add \
    --divert "${MANAGED_PODMAN_DISTRO_PATH}" "${MANAGED_PODMAN_COMMAND_PATH}" || return $?
  run_cmd ln -sfn "${runtime_root}/bin/podman" "${MANAGED_PODMAN_COMMAND_PATH}" || return $?
  run_cmd install -d -m 0755 "$(dirname "${MANAGED_PODMAN_LOCAL_COMMAND_PATH}")" || return $?
  run_cmd ln -sfn "${runtime_root}/bin/podman" "${MANAGED_PODMAN_LOCAL_COMMAND_PATH}" || return $?
  run_cmd install -d -m 0755 "$(dirname "${MANAGED_PODMAN_SYSTEM_GENERATOR_PATH}")" || return $?
  run_cmd ln -sfn \
    "${runtime_root}/systemd/podman-system-generator" \
    "${MANAGED_PODMAN_SYSTEM_GENERATOR_PATH}" || return $?

  write_managed_podman_config "${runtime_root}"
}

write_managed_podman_config() {
  local runtime_root="$1"
  run_cmd install -d -m 0755 "$(dirname "${MANAGED_PODMAN_CONFIG_PATH}")" || return $?
  write_file_atomic "${MANAGED_PODMAN_CONFIG_PATH}" 0644 <<EOF
# Managed by CNC
[engine]
cgroup_manager = "systemd"
lock_type = "${MANAGED_PODMAN_LOCK_TYPE}"
runtime = "crun"
conmon_path = ["${runtime_root}/libexec/conmon"]
helper_binaries_dir = ["${runtime_root}/libexec", "${runtime_root}/bin"]

[engine.runtimes]
crun = ["${runtime_root}/bin/crun"]
EOF
}

install_managed_podman() {
  select_managed_podman_asset

  local archive_name="podman-linux-${MANAGED_PODMAN_ARCH}.tar.gz"
  local archive_path="${MANAGED_PODMAN_CACHE_ROOT}/${archive_name}"
  local archive_partial="${archive_path}.partial"
  local crun_name="crun-${MANAGED_CRUN_VERSION}-linux-${MANAGED_PODMAN_ARCH}"
  local crun_path="${MANAGED_PODMAN_CACHE_ROOT}/${crun_name}"
  local crun_partial="${crun_path}.partial"
  local conmon_name="conmon_${MANAGED_CONMON_VERSION}_${MANAGED_PODMAN_ARCH}.deb"
  local conmon_path="${MANAGED_PODMAN_CACHE_ROOT}/${conmon_name}"
  local conmon_partial="${conmon_path}.partial"
  local conmon_source_root="${MANAGED_PODMAN_CACHE_ROOT}/conmon-${MANAGED_CONMON_VERSION}-${MANAGED_PODMAN_ARCH}"
  local source_root="${MANAGED_PODMAN_CACHE_ROOT}/source-${MANAGED_PODMAN_VERSION}-${MANAGED_PODMAN_ARCH}"
  local runtime_root="${MANAGED_PODMAN_INSTALL_ROOT}/${MANAGED_PODMAN_VERSION}-${MANAGED_PODMAN_ARCH}"
  local staging_root

  run_cmd install -d -m 0700 "${MANAGED_PODMAN_CACHE_ROOT}" || return $?
  run_cmd install -d -m 0755 "${MANAGED_PODMAN_INSTALL_ROOT}" || return $?

  if [[ ! -f "${archive_path}" ]] || [[ "$(file_sha256 "${archive_path}")" != "${MANAGED_PODMAN_SHA256}" ]]; then
    log "downloading CNC's pinned Podman ${MANAGED_PODMAN_VERSION} runtime"
    with_retry 3 3 run_cmd curl \
      --proto '=https' \
      --tlsv1.2 \
      --fail \
      --location \
      --silent \
      --show-error \
      --output "${archive_partial}" \
      "${MANAGED_PODMAN_BASE_URL}/${archive_name}" || return $?
    if [[ "$(file_sha256 "${archive_partial}")" != "${MANAGED_PODMAN_SHA256}" ]]; then
      fail "downloaded Podman archive failed its pinned SHA-256 check"
    fi
    run_cmd mv -f "${archive_partial}" "${archive_path}" || return $?
  fi

  if [[ ! -f "${crun_path}" ]] || [[ "$(file_sha256 "${crun_path}")" != "${MANAGED_CRUN_SHA256}" ]]; then
    log "downloading CNC's pinned systemd-enabled crun ${MANAGED_CRUN_VERSION} runtime"
    with_retry 3 3 run_cmd curl \
      --proto '=https' \
      --tlsv1.2 \
      --fail \
      --location \
      --silent \
      --show-error \
      --output "${crun_partial}" \
      "${MANAGED_CRUN_BASE_URL}/${crun_name}" || return $?
    if [[ "$(file_sha256 "${crun_partial}")" != "${MANAGED_CRUN_SHA256}" ]]; then
      fail "downloaded crun binary failed its pinned SHA-256 check"
    fi
    run_cmd mv -f "${crun_partial}" "${crun_path}" || return $?
  fi

  if [[ ! -f "${conmon_path}" ]] || [[ "$(file_sha256 "${conmon_path}")" != "${MANAGED_CONMON_SHA256}" ]]; then
    log "downloading CNC's pinned Ubuntu conmon 2.1.13 runtime"
    with_retry 3 3 run_cmd curl \
      --proto '=https' \
      --tlsv1.2 \
      --fail \
      --location \
      --silent \
      --show-error \
      --output "${conmon_partial}" \
      "${MANAGED_CONMON_BASE_URL}/${conmon_name}" || return $?
    if [[ "$(file_sha256 "${conmon_partial}")" != "${MANAGED_CONMON_SHA256}" ]]; then
      fail "downloaded conmon package failed its pinned SHA-256 check"
    fi
    run_cmd mv -f "${conmon_partial}" "${conmon_path}" || return $?
  fi

  if [[ ! -x "${conmon_source_root}/usr/bin/conmon" ]]; then
    run_cmd install -d -m 0700 "${conmon_source_root}" || return $?
    run_cmd dpkg-deb --extract "${conmon_path}" "${conmon_source_root}" || return $?
    [[ -x "${conmon_source_root}/usr/bin/conmon" ]] || fail "managed conmon package has an unexpected layout"
  fi

  if [[ ! -d "${source_root}" ]]; then
    staging_root="$(mktemp -d "${MANAGED_PODMAN_CACHE_ROOT}/extract.XXXXXX")"
    run_cmd tar --no-same-owner --no-same-permissions -xzf "${archive_path}" -C "${staging_root}" || return $?
    local extracted_root="${staging_root}/podman-linux-${MANAGED_PODMAN_ARCH}"
    [[ -d "${extracted_root}" ]] || fail "managed Podman archive has an unexpected layout"
    run_cmd mv "${extracted_root}" "${source_root}" || return $?
    run_cmd rmdir "${staging_root}" || return $?
  fi

  local required_file
  for required_file in \
    usr/local/bin/podman \
    usr/local/bin/pasta \
    usr/local/bin/fuse-overlayfs \
    usr/local/bin/fusermount3 \
    usr/local/bin/crun \
    usr/local/bin/runc \
    usr/local/libexec/podman/quadlet \
    usr/local/lib/podman/conmon \
    usr/local/lib/podman/rootlessport \
    usr/local/lib/podman/netavark \
    usr/local/lib/podman/catatonit \
    usr/local/lib/podman/aardvark-dns \
    usr/local/lib/systemd/system-generators/podman-system-generator; do
    [[ -f "${source_root}/${required_file}" ]] || fail "managed Podman archive is missing ${required_file}"
  done

  run_cmd install -d -m 0755 \
    "${runtime_root}/bin" \
    "${runtime_root}/libexec" \
    "${runtime_root}/systemd" || return $?
  local binary
  for binary in podman pasta fuse-overlayfs fusermount3 runc; do
    install_binary_atomic \
      "${source_root}/usr/local/bin/${binary}" \
      "${runtime_root}/bin/${binary}" || return $?
  done
  install_binary_atomic \
    "${source_root}/usr/local/libexec/podman/quadlet" \
    "${runtime_root}/libexec/quadlet" || return $?
  for binary in rootlessport netavark catatonit aardvark-dns; do
    install_binary_atomic \
      "${source_root}/usr/local/lib/podman/${binary}" \
      "${runtime_root}/libexec/${binary}" || return $?
  done
  install_binary_atomic \
    "${source_root}/usr/local/lib/systemd/system-generators/podman-system-generator" \
    "${runtime_root}/systemd/podman-system-generator" || return $?
  install_binary_atomic "${crun_path}" "${runtime_root}/bin/crun" || return $?
  install_binary_atomic \
    "${conmon_source_root}/usr/bin/conmon" \
    "${runtime_root}/libexec/conmon" || return $?

  activate_managed_podman "${runtime_root}"
}

podman_command_present() {
  command -v podman >/dev/null 2>&1
}

running_cnc_guest_names() {
  if ! podman_command_present; then
    return 0
  fi
  local listing_file rc
  listing_file="$(mktemp)" || return 1
  timeout 30s podman ps --format '{{.Names}}' >"${listing_file}" 2>/dev/null </dev/null
  rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    rm -f "${listing_file}"
    return 1
  fi
  grep '^cnc-app-' "${listing_file}" || true
  rm -f "${listing_file}"
}

running_cnc_guest_count() {
  local names
  names="$(running_cnc_guest_names)" || return 1
  if [[ -z "${names}" ]]; then
    printf '0\n'
  else
    printf '%s\n' "${names}" | wc -l | tr -d ' '
  fi
}

managed_podman_runtime_root() {
  local arch="${MANAGED_PODMAN_ARCH:-}"
  if [[ -z "${arch}" ]]; then
    case "$(dpkg --print-architecture 2>/dev/null)" in
      amd64) arch="amd64" ;;
      arm64) arch="arm64" ;;
      *) return 1 ;;
    esac
  fi
  printf '%s\n' "${MANAGED_PODMAN_INSTALL_ROOT}/${MANAGED_PODMAN_VERSION}-${arch}"
}

managed_podman_runtime_tree_is_current() {
  local runtime_root
  runtime_root="$(managed_podman_runtime_root)" || return 1
  [[ "$(readlink -f "${MANAGED_PODMAN_COMMAND_PATH}" 2>/dev/null)" == "${runtime_root}/bin/podman" ]] || return 1
  [[ -x "${runtime_root}/bin/crun" && -x "${runtime_root}/libexec/conmon" ]] || return 1
  local active_version
  active_version="$("${runtime_root}/bin/podman" --version 2>/dev/null | awk '{print $3}')" || return 1
  [[ "${active_version}" == "${MANAGED_PODMAN_VERSION}" ]]
}

managed_podman_config_is_current() {
  local runtime_root
  runtime_root="$(managed_podman_runtime_root)" || return 1
  grep -qF "${runtime_root}/libexec/conmon" "${MANAGED_PODMAN_CONFIG_PATH}" 2>/dev/null || return 1
  grep -qF "${runtime_root}/bin/crun" "${MANAGED_PODMAN_CONFIG_PATH}" 2>/dev/null || return 1
  grep -qF "lock_type = \"${MANAGED_PODMAN_LOCK_TYPE}\"" "${MANAGED_PODMAN_CONFIG_PATH}" 2>/dev/null || return 1
}

managed_podman_is_current() {
  managed_podman_runtime_tree_is_current || return 1
  managed_podman_config_is_current
}

managed_distro_podman_version() {
  local candidate version
  for candidate in "${MANAGED_PODMAN_DISTRO_PATH}" "${MANAGED_PODMAN_COMMAND_PATH}"; do
    if [[ -x "${candidate}" ]]; then
      version="$("${candidate}" --version 2>/dev/null | awk '{print $3}')" || true
      [[ -n "${version}" ]] || version="unknown"
      printf '%s\n' "${version}"
      return 0
    fi
  done
  printf 'unknown\n'
}

ensure_supported_podman() {
  select_managed_podman_asset
  local runtime_root
  runtime_root="$(managed_podman_runtime_root)" \
    || fail "cannot determine the pinned runtime path for host architecture: $(dpkg --print-architecture)"

  if managed_podman_is_current; then
    log "pinned container runtime already active: podman ${MANAGED_PODMAN_VERSION} on ${MANAGED_PODMAN_ARCH} at ${runtime_root}; verifying only"
    verify_supported_podman || return 1
    return
  fi

  if managed_podman_runtime_tree_is_current; then
    log "refreshing the managed runtime config: ${MANAGED_PODMAN_CONFIG_PATH} (podman ${MANAGED_PODMAN_VERSION} is already the active runtime, so no runtime change and no guest restarts)"
    if ! write_managed_podman_config "${runtime_root}"; then
      log "could not rewrite ${MANAGED_PODMAN_CONFIG_PATH}; the runtime already active on this host is unchanged"
      return 1
    fi
    if ! verify_supported_podman; then
      log "the pinned runtime did not satisfy its own check after the config refresh; the runtime is unchanged and the problem reported above is still on this host"
      return 1
    fi
    log "managed runtime config refreshed and verified"
    return
  fi

  log "container runtime decision: this host ships podman $(managed_distro_podman_version), CNC runs its own pinned podman ${MANAGED_PODMAN_VERSION} on ${MANAGED_PODMAN_ARCH} so that every supported release runs the same container stack"

  local guest_names
  if ! guest_names="$(running_cnc_guest_names)"; then
    log "cannot determine whether guests are running; podman did not answer"
    fail "refusing to change the container runtime while guest state is unknown. Resolve podman on this host, then rerun."
  fi

  local -a stopped_guests=()
  local guest unit
  if [[ -n "${guest_names}" ]]; then
    while IFS= read -r guest; do
      [[ -n "${guest}" ]] || continue
      unit="${guest}.service"
      log "stopping ${unit} for the runtime change"
      if run_cmd systemctl stop "${unit}" \
        && wait_for_guest_state "${guest}" absent 90; then
        stopped_guests+=("${unit}")
      else
        start_stopped_guests "${stopped_guests[@]+"${stopped_guests[@]}"}"
        fail "${unit} did not stop before the runtime change; nothing was changed"
      fi
    done <<<"${guest_names}"
  fi

  log "installing CNC's pinned container runtime (podman ${MANAGED_PODMAN_VERSION}, crun ${MANAGED_CRUN_VERSION}, conmon ${MANAGED_CONMON_VERSION})"
  if ! install_managed_podman; then
    start_stopped_guests "${stopped_guests[@]+"${stopped_guests[@]}"}"
    fail "the container runtime was not changed; guests were restarted on the runtime that was already there"
  fi
  if ! verify_supported_podman; then
    start_stopped_guests "${stopped_guests[@]+"${stopped_guests[@]}"}"
    fail "the container runtime did not satisfy its own check after installation; the previous runtime was restored and guests were restarted on it"
  fi
  start_stopped_guests "${stopped_guests[@]+"${stopped_guests[@]}"}"
}

wait_for_guest_state() {
  local guest="$1" want="$2" deadline=$(( SECONDS + ${3:-60} ))
  local rc present
  while (( SECONDS < deadline )); do
    rc=0
    timeout 15s podman container exists "${guest}" >/dev/null 2>&1 || rc=$?
    case "${rc}" in
      0) present=present ;;
      1) present=absent ;;
      *) present=unknown ;;
    esac
    [[ "${present}" == "${want}" ]] && return 0
    sleep 2
  done
  return 1
}

start_stopped_guests() {
  local unit guest name failed=0
  for unit in "$@"; do
    [[ -n "${unit}" ]] || continue
    guest="${unit%.service}"
    name="${guest#cnc-app-}"
    clear_stale_guest_rootfs_mount "${name}"
    log "starting ${unit} on the current runtime"
    if run_cmd systemctl start "${unit}" \
      && wait_for_guest_state "${guest}" present 90; then
      log "${guest} is running"
      continue
    fi
    log "${guest} did not start on the new runtime; repairing it for this runtime"
    clear_stale_guest_rootfs_mount "${name}"
    if repair_guest_for_runtime "${name}" && wait_for_guest_state "${guest}" present 120; then
      log "${guest} is running after repair"
      continue
    fi
    log "WARNING: ${guest} is still not running; run 'cnc-admin output fix ${name}'"
    failed=$(( failed + 1 ))
  done
  [[ "${failed}" -eq 0 ]]
}

clear_stale_guest_rootfs_mount() {
  local name="$1"
  local rootfs="${CNC_SANDBOX_DIR:-/var/lib/cnc/sandboxes}/${name}/rootfs"
  [[ -d "${rootfs}" ]] || return 0
  mountpoint -q "${rootfs}" 2>/dev/null || return 0
  findmnt -no OPTIONS "${rootfs}" 2>/dev/null | grep -q idmapped || return 0
  log "clearing a stale idmapped mount left on ${name}'s rootfs"
  umount "${rootfs}" 2>/dev/null || umount -l "${rootfs}" 2>/dev/null || {
    log "WARNING: could not clear the stale mount on ${rootfs}"
    return 1
  }
}

repair_guest_for_runtime() {
  local name="$1"
  local cli="${CLI_WRAPPER_PATH:-/usr/local/bin/cnc-admin}"
  [[ -x "${cli}" ]] || return 1
  run_cmd timeout 300s "${cli}" output fix "${name}"
}

configure_podman_network() {
  run_cmd install -d -m 0755 /etc/containers/containers.conf.d
  write_file_atomic /etc/containers/containers.conf.d/90-cnc-network.conf 0644 <<'EOF'
# Managed by CNC
[network]
firewall_driver = "iptables"
EOF
}

setup_directories() {
  run_cmd install -d -m 0755 /etc/nginx/generated || return $?
  run_cmd install -d -m 0755 /etc/containers/systemd || return $?
  run_cmd install -d -m 0755 "${RUNTIME_ROOT}/data" || return $?
  run_cmd install -d -m 0755 "${RUNTIME_ROOT}/sandboxes" || return $?
  run_cmd install -d -m 0755 "${RUNTIME_ROOT}/backups" || return $?
  run_cmd install -d -m 0755 "${RUNTIME_ROOT}/releases" || return $?
  run_cmd install -d -m 0755 "${RUNTIME_ROOT}/updater" || return $?
  run_cmd install -d -m 0755 "${SSH_BACKEND_HOME_ROOT}" || return $?
  run_cmd install -d -m 0700 /var/log/cnc || return $?
  run_cmd install -d -m 0700 /var/log/cnc/updates || return $?
}

install_tailscale() {
  local tailscale_installer="/tmp/tailscale-install.sh"

  if ! command -v tailscale >/dev/null 2>&1; then
    run_cmd curl -fsSL https://tailscale.com/install.sh -o "${tailscale_installer}" || return $?
    run_cmd sh "${tailscale_installer}" || return $?
    rm -f "${tailscale_installer}"
  fi

  if ! systemctl is-enabled --quiet tailscaled >/dev/null 2>&1; then
    run_cmd systemctl enable tailscaled || return $?
  fi
  if ! systemctl is-active --quiet tailscaled >/dev/null 2>&1; then
    run_cmd systemctl start tailscaled || return $?
  fi
}

read_tailnet_config() {
  if [[ -z "${TS_AUTHKEY:-}" && -f "${ENV_FILE}" ]]; then
    TS_AUTHKEY="$(awk -F= '/^TS_AUTHKEY=/{print substr($0, index($0, "=") + 1); exit}' "${ENV_FILE}" || true)"
  fi
  if [[ -z "${TS_HOSTNAME:-}" && -f "${ENV_FILE}" ]]; then
    TS_HOSTNAME="$(awk -F= '/^TS_HOSTNAME=/{print substr($0, index($0, "=") + 1); exit}' "${ENV_FILE}" || true)"
  fi
}

join_tailnet() {
  read_tailnet_config

  if tailscale ip -4 >/dev/null 2>&1; then
    log "tailscale already connected to tailnet"
    return 0
  fi

  local ts_hostname
  ts_hostname="${TS_HOSTNAME:-$(hostname -s 2>/dev/null || echo cnc-vps)}"

  if [[ -n "${TS_AUTHKEY:-}" ]]; then
    run_cmd_private "tailscale up" tailscale up --authkey "${TS_AUTHKEY}" --hostname "${ts_hostname}" || return $?
    log "tailscale joined using TS_AUTHKEY"
    return 0
  fi

  local up_output up_rc auth_url wait_timeout elapsed poll_interval
  if up_output="$(tailscale up --hostname "${ts_hostname}" --timeout 5s 2>&1)"; then
    up_rc=0
  else
    up_rc=$?
  fi

  if tailscale ip -4 >/dev/null 2>&1; then
    log "tailscale joined"
    return 0
  fi

  auth_url="$(printf '%s\n' "${up_output}" | grep -Eo 'https://login\.tailscale\.com/[[:alnum:]/?&=_-]+' | head -n1 || true)"
  if [[ -n "${auth_url}" ]]; then
    log "tailscale login URL: ${auth_url}"
    if [[ ! -t 0 || ! -t 1 ]]; then
      log "unattended install; continuing without browser approval"
      log "run manually: tailscale up --hostname ${ts_hostname}"
      return 0
    fi
    log "open the URL above to approve this node; waiting for tailnet join"
  else
    log "tailscale login URL unavailable (rc=${up_rc}); continuing without tailnet join"
    log "run manually: tailscale up --hostname ${ts_hostname}"
    return 0
  fi

  wait_timeout="${TS_LOGIN_WAIT_TIMEOUT_SEC:-600}"
  if ! [[ "${wait_timeout}" =~ ^[0-9]+$ ]] || (( wait_timeout < 30 )); then
    wait_timeout=600
  fi
  elapsed=0
  poll_interval=3
  while (( elapsed < wait_timeout )); do
    if tailscale ip -4 >/dev/null 2>&1; then
      log "tailscale joined after browser approval"
      return 0
    fi
    sleep "${poll_interval}"
    elapsed=$(( elapsed + poll_interval ))
  done

  log "tailscale join timed out after ${wait_timeout}s; continuing install"
  return 0
}

configure_tailscale_admin_access() {
  if ! tailscale ip -4 >/dev/null 2>&1; then
    log "tailscale not connected; skipping tailscale serve admin setup"
    return 0
  fi

  if ! tailscale serve --bg --https=443 http://127.0.0.1:9090 >/dev/null 2>&1; then
    log "tailscale serve setup failed; keeping SSH tunnel access path"
    return 0
  fi

  local admin_url
  admin_url="$(tailscale serve status 2>/dev/null | grep -Eo 'https://[^ ]+' | head -n1 || true)"
  if [[ -n "${admin_url}" ]]; then
    log "tailscale admin URL: ${admin_url}"
  else
    log "tailscale serve configured for admin on 443"
  fi
  return 0
}

write_file_atomic() {
  local destination="$1"
  local mode="$2"
  local tmp_file
  tmp_file="$(mktemp "${destination}.tmp.XXXXXX")"
  cat >"${tmp_file}"
  chmod "${mode}" "${tmp_file}"
  mv -f "${tmp_file}" "${destination}"
}

remove_insecure_packages() {
  local packages=(
    inetd
    openbsd-inetd
    inetutils-inetd
    xinetd
    telnetd
    rsh-server
    ypserv
    nis
    tftpd
    tftpd-hpa
    atftpd
  )
  local pkg
  for pkg in "${packages[@]}"; do
    if dpkg -s "${pkg}" >/dev/null 2>&1; then
      with_retry 3 2 run_cmd apt-get -qq purge -y "${pkg}" || return $?
    fi
  done
}

enforce_hosts_equiv_absent() {
  if [[ -e /etc/hosts.equiv ]]; then
    run_cmd rm -f /etc/hosts.equiv || return $?
  fi
}

write_sysctl_tuning() {
  write_file_atomic "${SYSCTL_CNC_PATH}" 0644 <<'EOF'
# Managed by cnc install_ubuntu.sh
net.core.somaxconn=8192
net.ipv4.tcp_max_syn_backlog=8192
net.core.netdev_max_backlog=16384
fs.file-max=1000000
net.ipv4.tcp_syncookies=1
net.ipv4.conf.all.accept_source_route=0
net.ipv4.conf.default.accept_source_route=0
net.ipv4.conf.all.accept_redirects=0
net.ipv4.conf.default.accept_redirects=0
net.ipv4.conf.all.secure_redirects=0
net.ipv4.conf.default.secure_redirects=0
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.default.send_redirects=0
net.ipv6.conf.all.forwarding=0
net.ipv6.conf.default.forwarding=0
net.ipv6.conf.all.accept_redirects=0
net.ipv6.conf.default.accept_redirects=0
EOF
}

write_bbr_sysctl() {
  local available
  available="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true)"
  if grep -qw "bbr" <<<"${available}"; then
    write_file_atomic "${SYSCTL_BBR_PATH}" 0644 <<'EOF'
# Managed by cnc install_ubuntu.sh
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
    return 0
  fi

  write_file_atomic "${SYSCTL_BBR_PATH}" 0644 <<'EOF'
# Managed by cnc install_ubuntu.sh
EOF
  log "bbr unavailable on this kernel; skipped net.ipv4.tcp_congestion_control=bbr"
}

configure_system_log_retention() {
  run_cmd install -d -m 0755 "$(dirname "${JOURNALD_CNC_RETENTION_PATH}")" || return $?
  write_file_atomic "${JOURNALD_CNC_RETENTION_PATH}" 0644 <<'EOF'
# Managed by cnc install_ubuntu.sh
[Journal]
SystemMaxUse=1G
EOF

  write_file_atomic "${RSYSLOG_LOGROTATE_PATH}" 0644 <<'EOF'
# Managed by cnc install_ubuntu.sh
/var/log/syslog
/var/log/mail.log
/var/log/kern.log
/var/log/auth.log
/var/log/user.log
/var/log/cron.log
{
    su root syslog
    rotate 15
    daily
    maxsize 200M
    missingok
    notifempty
    compress
    sharedscripts
    postrotate
        /usr/lib/rsyslog/rsyslog-rotate
    endscript
}
EOF

  run_cmd logrotate -d "${RSYSLOG_LOGROTATE_PATH}" || return $?
  run_cmd systemctl restart systemd-journald || return $?
}

apply_sysctl_tuning() {
  run_cmd sysctl -e -p "${SYSCTL_CNC_PATH}" || return $?
  if grep -q '^net.ipv4.tcp_congestion_control=' "${SYSCTL_BBR_PATH}"; then
    run_cmd sysctl -e -p "${SYSCTL_BBR_PATH}" || return $?
  fi
}

configure_nginx_main_tuning() {
  [[ -f "${NGINX_MAIN_CONF}" ]] || fail "missing ${NGINX_MAIN_CONF}"

  if grep -Eq '^[[:space:]]*worker_processes[[:space:]]+' "${NGINX_MAIN_CONF}"; then
    sed -i -E 's/^[[:space:]]*worker_processes[[:space:]]+[^;]+;/worker_processes auto;/' "${NGINX_MAIN_CONF}"
  else
    sed -i '1i worker_processes auto;' "${NGINX_MAIN_CONF}"
  fi

  if grep -Eq '^[[:space:]]*worker_rlimit_nofile[[:space:]]+' "${NGINX_MAIN_CONF}"; then
    sed -i -E "s/^[[:space:]]*worker_rlimit_nofile[[:space:]]+[^;]+;/worker_rlimit_nofile ${NOFILE_LIMIT};/" "${NGINX_MAIN_CONF}"
  elif grep -Eq '^[[:space:]]*worker_processes[[:space:]]+' "${NGINX_MAIN_CONF}"; then
    sed -i "/^[[:space:]]*worker_processes[[:space:]]\+/a worker_rlimit_nofile ${NOFILE_LIMIT};" "${NGINX_MAIN_CONF}"
  else
    sed -i "1i worker_rlimit_nofile ${NOFILE_LIMIT};" "${NGINX_MAIN_CONF}"
  fi

  if grep -Eq '^[[:space:]]*worker_connections[[:space:]]+[0-9]+' "${NGINX_MAIN_CONF}"; then
    sed -i -E 's/^[[:space:]]*worker_connections[[:space:]]+[0-9]+;/    worker_connections 4096;/' "${NGINX_MAIN_CONF}"
  else
    if grep -Eq '^[[:space:]]*events[[:space:]]*\{' "${NGINX_MAIN_CONF}"; then
      local tmp_file
      tmp_file="$(mktemp "${NGINX_MAIN_CONF}.tmp.XXXXXX")"
      awk '
        BEGIN { in_events=0; inserted=0 }
        {
          if ($0 ~ /^[[:space:]]*events[[:space:]]*\{/) {
            in_events=1
          }
          if (in_events && $0 ~ /^[[:space:]]*\}/ && inserted==0) {
            print "    worker_connections 4096;"
            inserted=1
            in_events=0
          }
          print $0
        }
        END {
          if (inserted==0) {
            print ""
            print "events {"
            print "    worker_connections 4096;"
            print "}"
          }
        }
      ' "${NGINX_MAIN_CONF}" >"${tmp_file}" || {
        rm -f "${tmp_file}"
        fail "unable to set worker_connections in ${NGINX_MAIN_CONF}"
      }
      mv -f "${tmp_file}" "${NGINX_MAIN_CONF}"
    else
      cat >>"${NGINX_MAIN_CONF}" <<'EOF'

events {
    worker_connections 4096;
}
EOF
    fi
  fi
}

write_nginx_http_tuning() {
  write_file_atomic "${NGINX_TUNING_PATH}" 0644 <<'EOF'
# Managed by cnc install_ubuntu.sh
keepalive_requests 1000;
proxy_http_version 1.1;
proxy_set_header Connection "";
open_file_cache max=20000 inactive=30s;
open_file_cache_valid 60s;
open_file_cache_min_uses 2;
open_file_cache_errors on;
client_header_timeout 10s;
client_body_timeout 10s;
send_timeout 10s;
reset_timedout_connection on;
access_log /var/log/nginx/access.log combined buffer=256k flush=1s;
log_not_found off;
EOF
}

configure_systemd_nofile_limits() {
  run_cmd install -d -m 0755 /etc/systemd/system/nginx.service.d || return $?
  run_cmd install -d -m 0755 "/etc/systemd/system/${SERVICE_NAME}.service.d" || return $?
  write_file_atomic "/etc/systemd/system/nginx.service.d/override.conf" 0644 <<EOF
[Service]
LimitNOFILE=${NOFILE_LIMIT}
EOF
  write_file_atomic "/etc/systemd/system/${SERVICE_NAME}.service.d/override.conf" 0644 <<EOF
[Service]
LimitNOFILE=${NOFILE_LIMIT}
EOF
  run_cmd systemctl daemon-reload || return $?
}

setup_nginx_include() {
  if [[ -L /etc/nginx/sites-enabled/default || -f /etc/nginx/sites-enabled/default ]]; then
    run_cmd rm -f /etc/nginx/sites-enabled/default || return $?
  fi

  write_file_atomic "${NGINX_DEFAULT_DENY_PATH}" 0644 <<'EOF'
# Managed by cnc install_ubuntu.sh
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;
}
EOF

  write_file_atomic "${NGINX_INCLUDE_PATH}" 0644 <<'EOF'
# Managed by cnc install_ubuntu.sh
include /etc/nginx/generated/*.conf;
EOF
  if ! run_cmd nginx -t; then
    log "nginx -t failed; leaving current nginx state untouched (run apply after fixing config)"
    return 0
  fi
  run_cmd systemctl enable nginx || return $?
  if systemctl is-active --quiet nginx; then
    run_cmd systemctl reload nginx || run_cmd systemctl restart nginx || return $?
  else
    run_cmd systemctl start nginx || return $?
  fi
}

setup_python_env() {
  [[ -n "${INSTALL_RELEASE_DIR}" ]] || fail "install release directory not prepared"
  run_cmd "${PYTHON_BIN}" -m venv "${INSTALL_RELEASE_DIR}/.venv" || return $?
  run_cmd "${INSTALL_RELEASE_DIR}/.venv/bin/pip" --disable-pip-version-check -q install --require-hashes --only-binary=:all: -r "${INSTALL_RELEASE_DIR}/requirements.txt" || return $?
  run_cmd "${INSTALL_RELEASE_DIR}/.venv/bin/pip" --disable-pip-version-check -q install --no-deps -e "${INSTALL_RELEASE_DIR}" || return $?
}

validate_staged_release() {
  local database_url
  database_url="$(read_env_key_or_default "DATABASE_URL" "sqlite+aiosqlite:////var/lib/cnc/data/app.db")"
  (
    cd "${INSTALL_RELEASE_DIR}" &&
      "${INSTALL_RELEASE_DIR}/.venv/bin/python" -m app.release_preflight \
        --database-url "${database_url}" --installer-mode
  )
}

prepare_install_release() {
  local stamp source_rev release_name
  stamp="$(date +%Y%m%d%H%M%S)"
  source_rev="local"
  if git -C "${APP_DIR}" rev-parse --short=12 HEAD >/dev/null 2>&1; then
    source_rev="$(git -C "${APP_DIR}" rev-parse --short=12 HEAD)"
  fi
  release_name="${stamp}-${source_rev}"
  INSTALL_RELEASE_DIR="${RUNTIME_ROOT}/releases/${release_name}"

  run_cmd install -d -m 0755 "${INSTALL_RELEASE_DIR}" || return $?
  run_cmd cp -a "${APP_DIR}/." "${INSTALL_RELEASE_DIR}/" || return $?
  if [[ -e "${INSTALL_RELEASE_DIR}/.venv" ]]; then
    run_cmd rm -rf "${INSTALL_RELEASE_DIR}/.venv" || return $?
  fi
  log "prepared initial release ${INSTALL_RELEASE_DIR}"
}

append_env_if_missing() {
  local key="$1"
  local value="$2"
  if ! grep -q "^${key}=" "${ENV_FILE}"; then
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
    log "added missing ${key} to ${ENV_FILE}"
  fi
}

set_env_key_value() {
  local key="$1"
  local value="$2"
  local tmp_file
  tmp_file="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
  awk -v k="${key}" -v v="${value}" '
    BEGIN { updated=0 }
    {
      if ($0 ~ "^" k "=") {
        print k "=" v
        updated=1
        next
      }
      print
    }
    END {
      if (updated == 0) {
        print k "=" v
      }
    }
  ' "${ENV_FILE}" >"${tmp_file}"
  mv -f "${tmp_file}" "${ENV_FILE}"
}

print_initial_access_key() (
  if { true >/dev/tty; } 2>/dev/null; then
    exec >/dev/tty
  fi
  printf '\n[install] Initial CNC admin access key (shown once):\n'
  printf '  %s\n' "${INITIAL_ACCESS_KEY}"
  printf '[install] Store this key now; only its Argon2 hash is saved.\n\n'
)

ensure_initial_access_key() {
  local current_hash
  local -a generated=()
  current_hash="$(awk -F= '$1 == "ACCESS_KEY_HASH" { print substr($0, index($0, "=") + 1); exit }' "${ENV_FILE}" 2>/dev/null || true)"
  if [[ -n "${current_hash}" ]]; then
    return 0
  fi

  mapfile -t generated < <(
    "${INSTALL_RELEASE_DIR}/.venv/bin/python" -c \
      'import secrets; from app.access import hash_access_key; key = secrets.token_urlsafe(24); print(key); print(hash_access_key(key))'
  )
  if (( ${#generated[@]} != 2 )) || [[ -z "${generated[0]}" || -z "${generated[1]}" ]]; then
    fail "failed to generate the initial CNC admin access key"
  fi
  INITIAL_ACCESS_KEY="${generated[0]}"
  set_env_key_value "ACCESS_KEY_HASH" "${generated[1]}"
  chmod 0640 "${ENV_FILE}"
  print_initial_access_key
}

infer_install_github_repo() {
  local origin
  origin="$(git -C "${APP_DIR}" config --get remote.origin.url 2>/dev/null || true)"
  [[ -n "${origin}" ]] || return 1
  origin="${origin%.git}"
  case "${origin}" in
    https://github.com/*/*)
      printf '%s\n' "${origin#https://github.com/}"
      ;;
    git@github.com:*)
      printf '%s\n' "${origin#git@github.com:}"
      ;;
    ssh://git@github.com/*/*)
      printf '%s\n' "${origin#ssh://git@github.com/}"
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_updater_repo_default() {
  local current_repo inferred_repo
  current_repo="$(awk -F= '$1 == "GITHUB_REPO" { print $2; exit }' "${ENV_FILE}" 2>/dev/null || true)"
  if [[ -n "${current_repo}" ]]; then
    return 0
  fi
  inferred_repo="$(infer_install_github_repo || true)"
  if [[ -n "${inferred_repo}" ]]; then
    set_env_key_value "GITHUB_REPO" "${inferred_repo}"
  fi
}

persist_updater_credentials_from_env() {
  if [[ -n "${GITHUB_REPO:-}" ]]; then
    set_env_key_value "GITHUB_REPO" "${GITHUB_REPO}"
  fi
  if [[ -n "${GITHUB_REF:-}" ]]; then
    set_env_key_value "GITHUB_REF" "${GITHUB_REF}"
  fi
  if [[ -n "${GITHUB_READONLY_PAT:-}" ]]; then
    set_env_key_value "GITHUB_READONLY_PAT" "${GITHUB_READONLY_PAT}"
    log "persisted GITHUB_READONLY_PAT to ${ENV_FILE}"
  fi
  chmod 0640 "${ENV_FILE}"
}

persist_installer_preferences_from_env() {
  if [[ -n "${NGINX_CLOUDFLARE_ONLY+x}" ]]; then
    set_env_key_value "NGINX_CLOUDFLARE_ONLY" "${NGINX_CLOUDFLARE_ONLY}"
  fi
  if [[ -n "${TS_AUTHKEY:-}" ]]; then
    set_env_key_value "TS_AUTHKEY" "${TS_AUTHKEY}"
  fi
  if [[ -n "${TS_HOSTNAME:-}" ]]; then
    set_env_key_value "TS_HOSTNAME" "${TS_HOSTNAME}"
  fi
  if [[ -n "${ADMIN_ALLOWED_HOSTS:-}" ]]; then
    set_env_key_value "ADMIN_ALLOWED_HOSTS" "${ADMIN_ALLOWED_HOSTS}"
  fi
  chmod 0640 "${ENV_FILE}"
}

ensure_env_defaults() {
  append_env_if_missing "DATABASE_URL" "sqlite+aiosqlite:////var/lib/cnc/data/app.db"
  append_env_if_missing "ADMIN_UNSAFE_ALLOW_REMOTE" "0"
  append_env_if_missing "ADMIN_ALLOWED_HOSTS" ""
  append_env_if_missing "ACCESS_KEY_HASH" ""
  append_env_if_missing "ACCESS_SESSION_TTL_SEC" "2592000"
  append_env_if_missing "CSRF_TOKEN" ""
  append_env_if_missing "LOG_PATH" "/var/log/cnc/app.log"
  append_env_if_missing "LOG_MAX_BYTES" "10485760"
  append_env_if_missing "LOG_BACKUP_COUNT" "5"
  append_env_if_missing "LOG_COMPRESS_ROTATED" "1"
  append_env_if_missing "LOG_APP_ID" "cnc.admin"
  append_env_if_missing "LOG_ENV" "prod"
  append_env_if_missing "UPDATE_LOG_DIR" "/var/log/cnc/updates"
  append_env_if_missing "UPDATE_OUTPUT_MAX_CHARS" "16000"
  append_env_if_missing "NGINX_CLOUDFLARE_ONLY" "1"
  append_env_if_missing "NGINX_CLOUDFLARE_IPS" "173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,141.101.64.0/18,108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,197.234.240.0/22,198.41.128.0/17,162.158.0.0/15,104.16.0.0/13,104.24.0.0/14,172.64.0.0/13,131.0.72.0/22,2400:cb00::/32,2606:4700::/32,2803:f800::/32,2405:b500::/32,2405:8100::/32,2a06:98c0::/29,2c0f:f248::/32"
  append_env_if_missing "CLOUDFLARE_IPS_V4_URL" "https://www.cloudflare.com/ips-v4"
  append_env_if_missing "CLOUDFLARE_IPS_V6_URL" "https://www.cloudflare.com/ips-v6"
  append_env_if_missing "CLOUDFLARE_SYNC_TIMEOUT_SEC" "15"
  append_env_if_missing "HOST_STATE_PATH" "/var/lib/cnc/host-state.json"
  append_env_if_missing "AUTO_RESOURCE_LIMITS" "1"
  append_env_if_missing "AUTO_MEMORY_RESERVE_PERCENT" "30"
  append_env_if_missing "AUTO_CPU_RESERVE_PERCENT" "20"
  append_env_if_missing "AUTO_MIN_MEMORY_HIGH_MB" "128"
  append_env_if_missing "AUTO_MIN_CPU_QUOTA_PERCENT" "25"
  append_env_if_missing "DEFAULT_MEMORY_SWAP_MAX" "0"
  append_env_if_missing "DEFAULT_TASKS_MAX" "512"
  append_env_if_missing "DEFAULT_OOM_POLICY" "kill"
  append_env_if_missing "DEFAULT_RESTART_SEC" "2"
  append_env_if_missing "DEFAULT_START_LIMIT_INTERVAL_SEC" "30"
  append_env_if_missing "DEFAULT_START_LIMIT_BURST" "10"
  append_env_if_missing "SSH_ADVERTISE_HOST" ""
  append_env_if_missing "TAILSCALE_TAILNET_DNS_NAME" ""
  append_env_if_missing "MULTI_NODE_ENABLED" "0"
  append_env_if_missing "CLUSTER_JOIN_TAILNET_BASE_URL" ""
  append_env_if_missing "CLUSTER_JOIN_TOKEN_TTL_SEC" "900"
  append_env_if_missing "NETDATA_ENABLED" "0"
  append_env_if_missing "STATUS_CACHE_TTL_SEC" "60"
  append_env_if_missing "APPLY_LOCK_PATH" "/var/lib/cnc/apply.lock"
  append_env_if_missing "COMMAND_TIMEOUT_APPLY_SEC" "300"
  append_env_if_missing "UPDATER_SCRIPT_PATH" "/var/lib/cnc/current/scripts/update_from_github.sh"
  append_env_if_missing "GITHUB_REPO" ""
  append_env_if_missing "GITHUB_REF" "main"
  append_env_if_missing "TS_AUTHKEY" ""
  append_env_if_missing "TS_HOSTNAME" ""
  append_env_if_missing "TS_LOGIN_WAIT_TIMEOUT_SEC" "600"
  append_env_if_missing "CNC_RELEASES_DIR" "/var/lib/cnc/releases"
  append_env_if_missing "CNC_CURRENT_LINK" "/var/lib/cnc/current"
  append_env_if_missing "CNC_SERVICE_NAME" "cnc-admin"
  append_env_if_missing "CNC_KEEP_RELEASES" "5"
}

write_env_file() {
  if [[ -f "${ENV_FILE}" && "${CNC_OVERWRITE_ENV:-0}" != "1" ]]; then
    PRESERVED_ENV_FILE="1"
    if grep -q '^DATABASE_URL=sqlite+aiosqlite:///var/lib/cnc/data/app.db$' "${ENV_FILE}"; then
      sed -i 's#^DATABASE_URL=sqlite+aiosqlite:///var/lib/cnc/data/app.db$#DATABASE_URL=sqlite+aiosqlite:////var/lib/cnc/data/app.db#' "${ENV_FILE}"
      log "patched legacy DATABASE_URL format in ${ENV_FILE}"
    fi
    if grep -q '^COMMAND_TIMEOUT_APPLY_SEC=30$' "${ENV_FILE}"; then
      sed -i 's#^COMMAND_TIMEOUT_APPLY_SEC=30$#COMMAND_TIMEOUT_APPLY_SEC=300#' "${ENV_FILE}"
      log "patched legacy COMMAND_TIMEOUT_APPLY_SEC in ${ENV_FILE}"
    fi
    if grep -q '^UPDATE_OUTPUT_MAX_CHARS=4000$' "${ENV_FILE}"; then
      sed -i 's#^UPDATE_OUTPUT_MAX_CHARS=4000$#UPDATE_OUTPUT_MAX_CHARS=16000#' "${ENV_FILE}"
      log "patched legacy UPDATE_OUTPUT_MAX_CHARS in ${ENV_FILE}"
    fi
    ensure_env_defaults
    ensure_initial_access_key
    ensure_updater_repo_default
    persist_installer_preferences_from_env
    persist_updater_credentials_from_env
    chmod 0640 "${ENV_FILE}"
    return 0
  fi

  write_file_atomic "${ENV_FILE}" 0640 <<'EOF'
APP_NAME=cnc
ADMIN_HOST=127.0.0.1
ADMIN_PORT=9090
ADMIN_UNSAFE_ALLOW_REMOTE=0
ADMIN_ALLOWED_HOSTS=
ACCESS_KEY_HASH=
ACCESS_SESSION_TTL_SEC=2592000
CSRF_TOKEN=
DATABASE_URL=sqlite+aiosqlite:////var/lib/cnc/data/app.db
LOG_PATH=/var/log/cnc/app.log
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
LOG_COMPRESS_ROTATED=1
LOG_APP_ID=cnc.admin
LOG_ENV=prod
UPDATE_LOG_DIR=/var/log/cnc/updates
UPDATE_OUTPUT_MAX_CHARS=16000
NGINX_GENERATED_DIR=/etc/nginx/generated
NGINX_CLOUDFLARE_ONLY=1
NGINX_CLOUDFLARE_IPS=173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,141.101.64.0/18,108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,197.234.240.0/22,198.41.128.0/17,162.158.0.0/15,104.16.0.0/13,104.24.0.0/14,172.64.0.0/13,131.0.72.0/22,2400:cb00::/32,2606:4700::/32,2803:f800::/32,2405:b500::/32,2405:8100::/32,2a06:98c0::/29,2c0f:f248::/32
CLOUDFLARE_IPS_V4_URL=https://www.cloudflare.com/ips-v4
CLOUDFLARE_IPS_V6_URL=https://www.cloudflare.com/ips-v6
CLOUDFLARE_SYNC_TIMEOUT_SEC=15
HOST_STATE_PATH=/var/lib/cnc/host-state.json
APPLY_BACKUP_DIR=/var/lib/cnc/backups
PORT_RANGE_START=12000
PORT_RANGE_END=12999
DEFAULT_MEMORY_HIGH=256M
DEFAULT_MEMORY_MAX=384M
DEFAULT_MEMORY_SWAP_MAX=0
DEFAULT_CPU_QUOTA=50%
DEFAULT_TASKS_MAX=512
DEFAULT_OOM_POLICY=kill
DEFAULT_RESTART_SEC=2
DEFAULT_START_LIMIT_INTERVAL_SEC=30
DEFAULT_START_LIMIT_BURST=10
SSH_ADVERTISE_HOST=
MULTI_NODE_ENABLED=0
CLUSTER_JOIN_TAILNET_BASE_URL=
CLUSTER_JOIN_TOKEN_TTL_SEC=900
NETDATA_ENABLED=0
AUTO_RESOURCE_LIMITS=1
AUTO_MEMORY_RESERVE_PERCENT=30
AUTO_CPU_RESERVE_PERCENT=20
AUTO_MIN_MEMORY_HIGH_MB=128
AUTO_MIN_CPU_QUOTA_PERCENT=25
APPLY_LOCK_PATH=/var/lib/cnc/apply.lock
COMMAND_TIMEOUT_APPLY_SEC=300
COMMAND_TIMEOUT_STATUS_SEC=5
STATUS_CACHE_TTL_SEC=60
UPDATER_SCRIPT_PATH=/var/lib/cnc/current/scripts/update_from_github.sh

GITHUB_REPO=
GITHUB_READONLY_PAT=
GITHUB_REF=main
TS_AUTHKEY=
TS_HOSTNAME=
TS_LOGIN_WAIT_TIMEOUT_SEC=600
CNC_RELEASES_DIR=/var/lib/cnc/releases
CNC_CURRENT_LINK=/var/lib/cnc/current
CNC_SERVICE_NAME=cnc-admin
CNC_KEEP_RELEASES=5
EOF
  ensure_env_defaults
  ensure_initial_access_key
  ensure_updater_repo_default
  persist_installer_preferences_from_env
  persist_updater_credentials_from_env
}

configure_firewall_baseline() {
  if [[ "${CNC_SKIP_FIREWALL:-0}" == "1" ]]; then
    log "CNC_SKIP_FIREWALL=1; skipping UFW baseline"
    return 0
  fi

  if ! command -v ufw >/dev/null 2>&1; then
    run_cmd apt-get -qq install -y ufw || return $?
  fi

  local ssh_ports current_client_ip port raw_port expected_ports=()
  ssh_ports="$(detect_ssh_ports)"
  current_client_ip="$(current_ssh_client_ip)"
  IFS=',' read -r -a ports <<<"${ssh_ports}"
  for raw_port in "${ports[@]}"; do
    port="${raw_port//[[:space:]]/}"
    [[ -n "${port}" ]] || continue
    [[ "${port}" =~ ^[0-9]+$ ]] || fail "invalid CNC_SSH_PORTS entry: ${raw_port}"
    expected_ports+=("${port}")
  done

  snapshot_ufw_state || return $?

  for port in "${expected_ports[@]}"; do
    [[ -n "${port}" ]] || continue
    [[ "${port}" =~ ^[0-9]+$ ]] || fail "invalid firewall TCP port: ${port}"
    if [[ -n "${current_client_ip}" ]]; then
      run_cmd ufw allow from "${current_client_ip}" to any port "${port}" proto tcp || {
        restore_ufw_state || true
        fail "failed to add UFW client-pin rule for ${current_client_ip}:${port}"
      }
    fi
    run_cmd ufw allow "${port}/tcp" || {
      restore_ufw_state || true
      fail "failed to add UFW allow rule for ${port}/tcp"
    }
  done

  if cloudflare_only_http_ingress_enabled; then
    configure_cloudflare_http_firewall_rules || {
      restore_ufw_state || true
      fail "failed to add Cloudflare-only HTTP/S firewall rules"
    }
  else
    configure_public_http_firewall_rules || {
      restore_ufw_state || true
      fail "failed to add public HTTP/S firewall rules"
    }
  fi

  run_cmd ufw --force default deny incoming || {
    restore_ufw_state || true
    fail "failed to set UFW default deny incoming"
  }
  run_cmd ufw --force default allow outgoing || {
    restore_ufw_state || true
    fail "failed to set UFW default allow outgoing"
  }
  run_cmd ufw --force enable || {
    restore_ufw_state || true
    fail "failed to enable UFW"
  }
  run_cmd ufw reload || {
    restore_ufw_state || true
    fail "failed to reload UFW"
  }

  verify_ufw_tcp_rules_loaded "${expected_ports[@]}" || {
    run_cmd ufw reload || true
    verify_ufw_tcp_rules_loaded "${expected_ports[@]}" || {
      restore_ufw_state || true
      fail "ufw enabled but expected SSH allow rules were not loaded; restored previous firewall state to avoid lockout"
    }
  }

  verify_http_firewall_rules || {
    run_cmd ufw reload || true
    verify_http_firewall_rules || {
      restore_ufw_state || true
      fail "ufw enabled but expected HTTP/S firewall rules were not loaded; restored previous firewall state to avoid lockout"
    }
  }

  cleanup_ufw_snapshot || true
}

detect_ssh_ports() {
  if [[ -n "${CNC_SSH_PORTS:-}" ]]; then
    printf '%s\n' "${CNC_SSH_PORTS}"
    return 0
  fi

  local ports
  ports="$(
    {
      if command -v sshd >/dev/null 2>&1; then
        sshd -T 2>/dev/null | awk '/^port / { print $2 }'
      fi
    } | awk '/^[0-9]+$/ { seen[$1]=1 } END { for (port in seen) print port }' | sort -n | paste -sd, -
  )"

  if [[ -n "${ports}" ]]; then
    printf '[install] detected SSH ports from sshd: %s\n' "${ports}" >&2
    printf '%s\n' "${ports}"
    return 0
  fi

  printf '[install] unable to detect SSH ports from sshd; defaulting to 22\n' >&2
  printf '22\n'
}

current_ssh_client_ip() {
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    awk '{ print $1 }' <<<"${SSH_CONNECTION}"
    return 0
  fi

  if [[ -n "${SSH_CLIENT:-}" ]]; then
    awk '{ print $1 }' <<<"${SSH_CLIENT}"
    return 0
  fi

  return 0
}

read_env_key_or_default() {
  local key="$1"
  local default_value="${2:-}"
  local value=""

  if [[ -f "${ENV_FILE}" ]]; then
    value="$(sed -n "s/^${key}=//p" "${ENV_FILE}" | head -n1)"
  fi
  if [[ -z "${value}" ]]; then
    value="${default_value}"
  fi
  printf '%s\n' "${value}"
}

run_cmd_in_app_dir() {
  local previous_dir="${PWD}"
  cd "${APP_DIR}" || return $?
  run_cmd "$@"
  local rc="$?"
  cd "${previous_dir}" || return 1
  return "${rc}"
}

run_cloudflare_ingress_tool() {
  run_cmd_in_app_dir python3 -m app.services.cloudflare_ingress "$@"
}

cloudflare_only_http_ingress_enabled() {
  local raw_value
  raw_value="${NGINX_CLOUDFLARE_ONLY:-$(read_env_key_or_default "NGINX_CLOUDFLARE_ONLY" "1")}"
  raw_value="${raw_value//[[:space:]]/}"
  case "${raw_value,,}" in
    1|true|yes|on)
      return 0
      ;;
  esac
  return 1
}

cloudflare_cidrs_from_env() {
  local raw_value cidr
  raw_value="${NGINX_CLOUDFLARE_IPS:-$(read_env_key_or_default "NGINX_CLOUDFLARE_IPS" "")}"
  raw_value="${raw_value//$'\n'/,}"
  IFS=',' read -r -a cidrs <<<"${raw_value}"
  for cidr in "${cidrs[@]}"; do
    cidr="${cidr//[[:space:]]/}"
    [[ -n "${cidr}" ]] || continue
    printf '%s\n' "${cidr}"
  done
}

delete_ufw_port_rules() {
  local port="$1"
  local rule_number
  local -a rule_numbers=()

  mapfile -t rule_numbers < <(
    ufw status numbered 2>/dev/null | sed -nE "s/^\[ *([0-9]+)\] +.*${port}\/tcp.*$/\1/p" | sort -rn
  )
  for rule_number in "${rule_numbers[@]}"; do
    [[ -n "${rule_number}" ]] || continue
    run_cmd ufw --force delete "${rule_number}" || return $?
  done
}

reset_http_firewall_rules() {
  delete_ufw_port_rules "80" || return $?
  delete_ufw_port_rules "443" || return $?
}

configure_public_http_firewall_rules() {
  local port
  reset_http_firewall_rules || return $?
  for port in 80 443; do
    run_cmd ufw allow "${port}/tcp" || return $?
  done
}

configure_cloudflare_http_firewall_rules() {
  local cidr port
  local -a cidrs=()
  local -a args=(rewrite-ufw)

  mapfile -t cidrs < <(cloudflare_cidrs_from_env)
  if (( ${#cidrs[@]} == 0 )); then
    fail "NGINX_CLOUDFLARE_ONLY=1 requires at least one Cloudflare CIDR for host firewall rules"
  fi

  for cidr in "${cidrs[@]}"; do
    args+=(--cidr "${cidr}")
  done
  run_cloudflare_ingress_tool "${args[@]}" || return $?
  run_cmd ufw reload || return $?
}

snapshot_ufw_state() {
  UFW_SNAPSHOT_DIR="$(mktemp -d /tmp/cnc-ufw.XXXXXX)"
  cp /etc/default/ufw "${UFW_SNAPSHOT_DIR}/default.ufw" || return $?
  cp /etc/ufw/user.rules "${UFW_SNAPSHOT_DIR}/user.rules" || return $?
  cp /etc/ufw/user6.rules "${UFW_SNAPSHOT_DIR}/user6.rules" || return $?
  cp /etc/ufw/ufw.conf "${UFW_SNAPSHOT_DIR}/ufw.conf" || return $?

  if ufw status 2>/dev/null | head -n1 | grep -q '^Status: active'; then
    UFW_WAS_ACTIVE="1"
  else
    UFW_WAS_ACTIVE="0"
  fi
}

restore_ufw_state() {
  [[ -n "${UFW_SNAPSHOT_DIR:-}" ]] || return 0

  cp "${UFW_SNAPSHOT_DIR}/default.ufw" /etc/default/ufw || return $?
  cp "${UFW_SNAPSHOT_DIR}/user.rules" /etc/ufw/user.rules || return $?
  cp "${UFW_SNAPSHOT_DIR}/user6.rules" /etc/ufw/user6.rules || return $?
  cp "${UFW_SNAPSHOT_DIR}/ufw.conf" /etc/ufw/ufw.conf || return $?

  if [[ "${UFW_WAS_ACTIVE}" == "1" ]]; then
    run_cmd ufw --force enable || return $?
    run_cmd ufw reload || return $?
  else
    run_cmd ufw --force disable || return $?
  fi

  cleanup_ufw_snapshot || true
}

cleanup_ufw_snapshot() {
  if [[ -n "${UFW_SNAPSHOT_DIR:-}" && -d "${UFW_SNAPSHOT_DIR}" ]]; then
    rm -rf "${UFW_SNAPSHOT_DIR}"
  fi
  UFW_SNAPSHOT_DIR=""
}

verify_ufw_tcp_rules_loaded() {
  local raw_port port
  for raw_port in "$@"; do
    port="${raw_port//[[:space:]]/}"
    [[ -n "${port}" ]] || continue
    if ! iptables -S ufw-user-input 2>/dev/null | grep -Eq -- "--dport ${port} .* -j ACCEPT|--dport ${port} -j ACCEPT"; then
      printf '[install] missing live UFW TCP allow rule for port %s\n' "${port}" >&2
      return 1
    fi
  done
  return 0
}

verify_ufw_cloudflare_http_rules_loaded() {
  local -a cidrs=()
  local -a args=(verify-live)

  mapfile -t cidrs < <(cloudflare_cidrs_from_env)
  if (( ${#cidrs[@]} == 0 )); then
    printf '[install] missing Cloudflare CIDRs for HTTP/S firewall verification\n' >&2
    return 1
  fi

  for cidr in "${cidrs[@]}"; do
    args+=(--cidr "${cidr}")
  done
  run_cloudflare_ingress_tool "${args[@]}"
}

verify_http_firewall_rules() {
  if cloudflare_only_http_ingress_enabled; then
    verify_ufw_cloudflare_http_rules_loaded
    return $?
  fi
  verify_ufw_tcp_rules_loaded "80" "443"
}

setup_runtime_link() {
  [[ -n "${INSTALL_RELEASE_DIR}" ]] || fail "install release directory not prepared"
  local tmp_link="${CURRENT_LINK}.new"
  run_cmd ln -sfn "${INSTALL_RELEASE_DIR}" "${tmp_link}" || return $?
  run_cmd mv -Tf "${tmp_link}" "${CURRENT_LINK}" || return $?
}

write_cli_wrapper() {
  write_file_atomic "${CLI_WRAPPER_PATH}" 0755 <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec ${CURRENT_LINK}/.venv/bin/cnc-admin "\$@"
EOF
  write_file_atomic "${LLM_HELP_WRAPPER_PATH}" 0755 <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec ${CURRENT_LINK}/.venv/bin/cnc-admin llm-help "\$@"
EOF
}

write_backend_ssh_assets() {
  run_cmd "${CURRENT_LINK}/.venv/bin/python" -c \
    'from app.config import get_settings; from app.services.runtime_assets import reconcile_guest_runtime_helper; reconcile_guest_runtime_helper(get_settings())' || return $?
  local wrapper_template="${APP_DIR}/${PACKAGED_SSH_BACKEND_ROOT_WRAPPER_PATH}"
  [[ -f "${wrapper_template}" ]] || fail "missing packaged backend SSH wrapper: ${wrapper_template}"
  sed 's/__CNC_ADMIN_PORT__/9090/g' "${wrapper_template}" \
    | write_file_atomic "${SSH_BACKEND_ROOT_WRAPPER_PATH}" 0755

  write_file_atomic "${SSH_BACKEND_SSHD_CONFIG_PATH}" 0644 <<'EOF'
# Managed by CNC
Match Group cnc-backends Address 100.64.0.0/10,fd7a:115c:a1e0::/48
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication yes
    MaxAuthTries 10
    AuthorizedKeysFile /etc/ssh/cnc-backend-authorized_keys .ssh/authorized_keys
    PermitTTY yes
    X11Forwarding no
    AllowAgentForwarding no
    AllowTcpForwarding no
    PermitTunnel no
    GatewayPorts no
    PermitUserRC no
    ForceCommand /usr/bin/sudo -n /usr/local/bin/cnc-ssh-backend-root
Match Group cnc-backends
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication no
    AuthorizedKeysFile none
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    AllowTcpForwarding no
    PermitTunnel no
    GatewayPorts no
    PermitUserRC no
    ForceCommand /bin/false
EOF

  write_file_atomic "${SSH_BACKEND_SUDOERS_PATH}" 0440 <<'EOF'
# Managed by CNC
%cnc-backends ALL=(root) NOPASSWD: /usr/local/bin/cnc-ssh-backend-root
Defaults!/usr/local/bin/cnc-ssh-backend-root env_keep += "SSH_ORIGINAL_COMMAND SSH_CONNECTION"
EOF

  if ! getent group cnc-backends >/dev/null 2>&1; then
    run_cmd groupadd --system cnc-backends || return $?
  fi
  if [[ -f /root/.ssh/authorized_keys ]]; then
    run_cmd install -D -m 0644 /root/.ssh/authorized_keys "${SSH_BACKEND_AUTHORIZED_KEYS_PATH}" || return $?
  fi
}

reload_ssh_service() {
  run_cmd sshd -t || return $?
  run_cmd systemctl reload ssh || run_cmd systemctl reload sshd || return $?
}

write_systemd_unit() {
  [[ -n "${INSTALL_RELEASE_DIR}" ]] || fail "install release directory not prepared"
  install_packaged_systemd_asset "${PACKAGED_SYSTEMD_UNIT_PATH}" "${SERVICE_NAME}.service" || return $?
  install_packaged_systemd_asset "${PACKAGED_AUTO_SIZE_SERVICE_PATH}" "${AUTO_SIZE_SERVICE_NAME}.service" || return $?
  install_packaged_systemd_asset "${PACKAGED_AUTO_SIZE_TIMER_PATH}" "${AUTO_SIZE_SERVICE_NAME}.timer" || return $?
  install_packaged_systemd_asset "${PACKAGED_BACKEND_ALERTS_SERVICE_PATH}" "${BACKEND_ALERTS_SERVICE_NAME}.service" || return $?
  install_packaged_systemd_asset "${PACKAGED_BACKEND_ALERTS_TIMER_PATH}" "${BACKEND_ALERTS_SERVICE_NAME}.timer" || return $?
  install_packaged_systemd_asset "packaging/cnc-cloudflare-tunnel.service" "cnc-cloudflare-tunnel.service" || return $?
  install_packaged_systemd_asset "packaging/cnc-cloudflare-domains.service" "cnc-cloudflare-domains.service" || return $?
  install_packaged_systemd_asset "packaging/cnc-cloudflare-domains.timer" "cnc-cloudflare-domains.timer" || return $?
  install_packaged_systemd_asset "packaging/cnc-origin-tls.service" "cnc-origin-tls.service" || return $?
  install_packaged_systemd_asset "packaging/cnc-origin-tls.timer" "cnc-origin-tls.timer" || return $?
  install_packaged_systemd_asset "${PACKAGED_CLOUDFLARE_SYNC_SERVICE_PATH}" "${CLOUDFLARE_SYNC_SERVICE_NAME}.service" || return $?
  install_packaged_systemd_asset "${PACKAGED_CLOUDFLARE_SYNC_TIMER_PATH}" "${CLOUDFLARE_SYNC_SERVICE_NAME}.timer" || return $?
  install_packaged_systemd_asset "${PACKAGED_UPDATE_CHECK_SERVICE_PATH}" "${UPDATE_CHECK_SERVICE_NAME}.service" || return $?
  install_packaged_systemd_asset "${PACKAGED_UPDATE_CHECK_TIMER_PATH}" "${UPDATE_CHECK_SERVICE_NAME}.timer" || return $?
  install_packaged_systemd_asset "packaging/systemd/cnc-edge-public-ingress.slice" "cnc-edge-public-ingress.slice" || return $?
  install_packaged_systemd_asset "packaging/systemd/cnc-edge-tailnet.slice" "cnc-edge-tailnet.slice" || return $?
  install_packaged_systemd_asset "packaging/systemd/cnc-admin-control.slice" "cnc-admin-control.slice" || return $?
  install_packaged_systemd_asset "packaging/systemd/cnc-apps.slice" "cnc-apps.slice" || return $?
}

install_packaged_systemd_asset() {
  local source_path="$1"
  local destination_name="$2"
  local packaged_path="${INSTALL_RELEASE_DIR}/${source_path}"

  [[ -f "${packaged_path}" ]] || fail "missing packaged systemd asset: ${packaged_path}"
  run_cmd install -D -m 0644 "${packaged_path}" "/etc/systemd/system/${destination_name}" || return $?
}

enable_services() {
  run_cmd systemctl daemon-reload || return $?
  run_cmd systemctl enable "${SERVICE_NAME}" || return $?
  run_cmd systemctl restart "${SERVICE_NAME}" || return $?
  run_cmd systemctl enable --now "${AUTO_SIZE_SERVICE_NAME}.timer" || return $?
  run_cmd systemctl enable --now "${BACKEND_ALERTS_SERVICE_NAME}.timer" || return $?
  run_cmd systemctl enable --now cnc-cloudflare-domains.timer || return $?
  run_cmd systemctl enable --now cnc-origin-tls.timer || return $?
  run_cmd systemctl enable --now "${CLOUDFLARE_SYNC_SERVICE_NAME}.timer" || return $?
  run_cmd systemctl enable --now "${UPDATE_CHECK_SERVICE_NAME}.timer" || return $?
}

verify_admin_service() {
  local tries=0
  while (( tries < 30 )); do
    if systemctl is-active --quiet "${SERVICE_NAME}" && curl -fsS --max-time 2 "http://127.0.0.1:9090/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    ((tries += 1))
  done

  run_cmd systemctl status "${SERVICE_NAME}" --no-pager || true
  run_cmd journalctl -u "${SERVICE_NAME}" -n 80 --no-pager || true
  return 1
}

verify_admin_service_write_paths() {
  local unit_text protect_system required_path main_pid probe_path
  unit_text="$(systemctl cat "${SERVICE_NAME}" 2>/dev/null || true)"
  [[ -n "${unit_text}" ]] || return 1

  protect_system="$(systemctl show "${SERVICE_NAME}" -p ProtectSystem --value 2>/dev/null || true)"
  if [[ "${protect_system}" != "no" ]]; then
    printf '[install] cnc-admin ProtectSystem must be no, got %s\n' "${protect_system:-unset}" >&2
    return 1
  fi

  for required_path in /etc /etc/containers/systemd /var/lib/cnc /var/log/cnc; do
    if ! grep -Fq "${required_path}" <<<"${unit_text}"; then
      printf '[install] missing cnc-admin ReadWritePaths entry for %s\n' "${required_path}" >&2
      return 1
    fi
  done

  if ! command -v nsenter >/dev/null 2>&1; then
    printf '[install] nsenter is required to verify cnc-admin mount namespace writes\n' >&2
    return 1
  fi

  main_pid="$(systemctl show "${SERVICE_NAME}" -p MainPID --value 2>/dev/null || true)"
  if [[ ! "${main_pid}" =~ ^[0-9]+$ || "${main_pid}" -le 1 ]]; then
    printf '[install] cnc-admin MainPID is not available for write-path verification\n' >&2
    return 1
  fi

  probe_path="/etc/containers/systemd/.cnc-write-test.install.$$"
  if ! nsenter -t "${main_pid}" -m -- sh -c "probe_path=\"\$1\"; : > \"\${probe_path}\" && chmod 0644 \"\${probe_path}\" && rm -f \"\${probe_path}\"" sh "${probe_path}"; then
    printf '[install] cnc-admin cannot write %s inside its active mount namespace\n' "${probe_path}" >&2
    return 1
  fi

  return 0
}

verify_cli_wrapper() {
  command -v cnc-admin >/dev/null 2>&1 || return 1
  cnc-admin --help >/dev/null 2>&1 || return 1
  command -v llm-help >/dev/null 2>&1 || return 1
  llm-help --help >/dev/null 2>&1 || return 1
}

verify_backend_ssh_assets() {
  run_cmd sshd -t || return $?
  run_cmd visudo -cf "${SSH_BACKEND_SUDOERS_PATH}" || return $?
}

verify_ssh_firewall_rules() {
  if ! ufw status 2>/dev/null | head -n1 | grep -q '^Status: active'; then
    return 0
  fi

  local ssh_ports raw_port port expected_ports=()
  ssh_ports="$(detect_ssh_ports)"
  IFS=',' read -r -a ports <<<"${ssh_ports}"
  for raw_port in "${ports[@]}"; do
    port="${raw_port//[[:space:]]/}"
    [[ -n "${port}" ]] || continue
    expected_ports+=("${port}")
  done
  verify_ufw_tcp_rules_loaded "${expected_ports[@]}" || return $?
  verify_http_firewall_rules
}

verify_nginx_runtime() {
  systemctl is-active --quiet nginx || return 1
  run_cmd nginx -t || return $?
}

verify_podman_basic_run() {
  local image_ref="${CNC_INSTALL_SMOKE_IMAGE:-docker.io/library/busybox:1.36}"
  local container_name="cnc-install-runtime-$$-${RANDOM}"
  local -a podman_cmd=(setsid --wait timeout --signal=KILL 30s podman)

  if ! run_cmd "${podman_cmd[@]}" run --detach --name "${container_name}" \
    --pull=always --log-driver=journald --network none "${image_ref}" sleep 300; then
    setsid --wait timeout --signal=KILL 15s podman rm --force "${container_name}" >/dev/null 2>&1 || true
    return 1
  fi
  if ! run_cmd "${podman_cmd[@]}" exec "${container_name}" true; then
    setsid --wait timeout --signal=KILL 15s podman rm --force "${container_name}" >/dev/null 2>&1 || true
    return 1
  fi
  if ! run_cmd "${podman_cmd[@]}" stop --time 5 "${container_name}"; then
    setsid --wait timeout --signal=KILL 15s podman rm --force "${container_name}" >/dev/null 2>&1 || true
    return 1
  fi
  run_cmd "${podman_cmd[@]}" rm "${container_name}"
}

verify_loopback_proxy_start() {
  local socket_unit="cnc-install-proxy-smoke.socket"
  local service_unit="cnc-install-proxy-smoke.service"
  local socket_path="/run/systemd/system/${socket_unit}"
  local service_path="/run/systemd/system/${service_unit}"
  local backend_port="19091"
  local proxy_port="19090"
  local backend_pid=""

  cleanup_loopback_proxy_smoke() {
    if [[ -n "${backend_pid}" ]]; then
      kill "${backend_pid}" >/dev/null 2>&1 || true
      wait "${backend_pid}" >/dev/null 2>&1 || true
    fi
    systemctl stop "${socket_unit}" "${service_unit}" >/dev/null 2>&1 || true
    rm -f "${socket_path}" "${service_path}"
    systemctl daemon-reload >/dev/null 2>&1 || true
  }

  cleanup_loopback_proxy_smoke
  python3 -m http.server "${backend_port}" --bind 127.0.0.1 >>"${INSTALL_LOG}" 2>&1 &
  backend_pid=$!
  sleep 1

  write_file_atomic "${socket_path}" 0644 <<EOF
[Unit]
Description=cnc install proxy smoke socket

[Socket]
ListenStream=127.0.0.1:${proxy_port}
Service=${service_unit}
NoDelay=true
EOF
  write_file_atomic "${service_path}" 0644 <<EOF
[Unit]
Description=cnc install proxy smoke service
Requires=${socket_unit}
After=${socket_unit}

[Service]
ExecStart=/lib/systemd/systemd-socket-proxyd 127.0.0.1:${backend_port}
DynamicUser=true
NoNewPrivileges=true
PrivateTmp=true
EOF

  run_cmd systemctl daemon-reload || {
    cleanup_loopback_proxy_smoke
    return 1
  }
  run_cmd systemctl start "${socket_unit}" || {
    cleanup_loopback_proxy_smoke
    return 1
  }
  run_cmd curl -fsS --max-time 2 "http://127.0.0.1:${proxy_port}" >/dev/null || {
    cleanup_loopback_proxy_smoke
    return 1
  }
  cleanup_loopback_proxy_smoke
  return 0
}

verify_isolated_app_network() {
  local network_name="cnc-install-smoke-$$"
  local image_ref="${CNC_INSTALL_SMOKE_IMAGE:-docker.io/library/busybox:1.36}"
  local -a dns_servers=()
  local -a dns_args=()

  while IFS= read -r resolver; do
    [[ -n "${resolver}" ]] || continue
    dns_servers+=("${resolver}")
  done < <(
    awk '
      $1 == "nameserver" && $2 != "" && $2 != "127.0.0.1" && $2 != "::1" && $2 != "127.0.0.53" {
        print $2
      }
    ' /etc/resolv.conf 2>/dev/null
  )

  if (( ${#dns_servers[@]} == 0 )); then
    dns_servers=("1.1.1.1" "1.0.0.1")
  fi

  local resolver
  for resolver in "${dns_servers[@]}"; do
    dns_args+=(--dns "${resolver}")
  done

  run_cmd podman network create --driver bridge --disable-dns "${network_name}" || return $?
  if ! run_cmd podman run --rm --pull=missing --network "${network_name}" "${dns_args[@]}" "${image_ref}" sh -ec \
    'nslookup example.com && wget -T 10 -O /dev/null http://example.com'; then
    podman network rm "${network_name}" >/dev/null 2>&1 || true
    return 1
  fi
  podman network rm "${network_name}" >/dev/null 2>&1 || true
  return 0
}

render_summary() {
  local finish_ts
  finish_ts="$(date +%s)"
  local total_elapsed=$(( finish_ts - START_TS ))

  local nginx_version
  nginx_version="$(nginx -v 2>&1 | head -n1 || echo "nginx unavailable")"
  local podman_version
  podman_version="$(podman --version 2>/dev/null || echo "podman unavailable")"
  local python_version
  python_version="$(python3 --version 2>/dev/null || echo "python3 unavailable")"
  local service_state
  service_state="$(systemctl is-active "${SERVICE_NAME}" 2>/dev/null || echo "unknown")"
  local tailscale_version
  tailscale_version="$(tailscale version 2>/dev/null | head -n1 || echo "tailscale unavailable")"
  local tailscale_ip
  tailscale_ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  local tailscale_admin_url
  tailscale_admin_url="$(tailscale serve status 2>/dev/null | grep -Eo 'https://[^ ]+' | head -n1 || true)"
  local tailscaled_state
  tailscaled_state="$(systemctl is-active tailscaled 2>/dev/null || echo "unknown")"
  local host_short
  host_short="$(hostname -s 2>/dev/null || echo "your-server-name")"
  local ssh_target
  ssh_target="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [[ -z "${ssh_target}" ]]; then
    ssh_target="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -z "${ssh_target}" ]]; then
    ssh_target="${host_short}"
  fi
  local ssh_tunnel_command
  ssh_tunnel_command="ssh -o ControlMaster=auto -o ControlPersist=5m -o ControlPath=/tmp/cnc-ssh-%C -fN -L 9090:127.0.0.1:9090 root@${ssh_target}"

  print_rule
  if [[ "${FAILED}" == "0" ]]; then
    printf '| INSTALL SUMMARY: SUCCESS                                              |\n'
  else
    printf '| INSTALL SUMMARY: FAILED                                               |\n'
  fi
  print_rule
  printf '| Total time: %-58ss |\n' "${total_elapsed}"
  printf '| App dir: %-61s |\n' "${APP_DIR:-unknown}"
  printf '| Current link: %-56s |\n' "$(readlink -f "${CURRENT_LINK}" 2>/dev/null || echo "missing")"
  printf '| Env file: %-60s |\n' "${ENV_FILE}"
  printf '| Detailed log: %-57s |\n' "${INSTALL_LOG}"
  print_rule
  printf '| %-44s | %-7s | %-8s |\n' "Step" "Status" "Seconds"
  print_rule

  local id
  for id in "${STEP_ORDER[@]}"; do
    local step_elapsed=0
    if [[ -n "${STEP_END[$id]:-}" && -n "${STEP_START[$id]:-}" ]]; then
      step_elapsed=$(( STEP_END["$id"] - STEP_START["$id"] ))
    fi
    printf '| %-44s | %-7s | %8s |\n' "${STEP_LABEL[$id]}" "${STEP_STATE[$id]}" "${step_elapsed}"
    if [[ "${STEP_STATE[$id]}" != "OK" && -n "${STEP_INFO[$id]:-}" ]]; then
      printf '|   detail: %-58s |\n' "${STEP_INFO[$id]}"
    fi
  done
  print_rule
  printf '| %-67s |\n' "${nginx_version}"
  printf '| %-67s |\n' "${podman_version}"
  printf '| %-67s |\n' "${python_version}"
  printf '| cnc-admin state: %-50s |\n' "${service_state}"
  printf '| %-67s |\n' "${tailscale_version}"
  printf '| tailscaled state: %-49s |\n' "${tailscaled_state}"
  print_rule

  if [[ "${PRESERVED_ENV_FILE}" == "1" ]]; then
    log "kept existing ${ENV_FILE} (set CNC_OVERWRITE_ENV=1 to regenerate defaults)"
  fi

  if [[ "${FAILED}" == "0" ]]; then
    local tailnet_step
    if [[ -n "${tailscale_ip}" ]]; then
      tailnet_step="Tailnet connected (${tailscale_ip})"
    else
      tailnet_step="Join Tailnet: tailscale up --hostname ${host_short}"
    fi
    if [[ -n "${tailscale_admin_url}" ]]; then
      cat <<EOF
[install] next steps:
1) ${tailnet_step}
2) Open admin in browser:
   ${tailscale_admin_url}
   (Use HTTPS explicitly. Plain HTTP goes to nginx/public routing.)
3) Optional SSH tunnel fallback:
   ${ssh_tunnel_command}
   - -N = no remote shell
4) Verify service:
   systemctl status cnc-admin --no-pager
EOF
    else
      cat <<EOF
[install] next steps:
1) ${tailnet_step}
2) Open a secure tunnel from your laptop:
   ${ssh_tunnel_command}
   - -N = no remote shell
3) Open http://127.0.0.1:9090 and verify:
   systemctl status cnc-admin --no-pager
EOF
    fi
  else
    if [[ -n "${ERROR_CONTEXT}" ]]; then
      printf '[install][error] context: %s\n' "${ERROR_CONTEXT}" >&2
    fi
    show_log_tail
  fi
}

on_error() {
  local rc="$?"
  FAILED="1"
  ERROR_CONTEXT="line ${BASH_LINENO[0]}: ${BASH_COMMAND}"
  if [[ -n "${INSTALL_LOG:-}" && -f "${INSTALL_LOG}" ]]; then
    printf '[install][error] %s (exit %s)\n' "${ERROR_CONTEXT}" "${rc}" >>"${INSTALL_LOG}"
  fi
  if [[ -n "${CURRENT_STEP_ID}" && "${STEP_STATE[${CURRENT_STEP_ID}]:-}" == "RUNNING" ]]; then
    step_finish "${CURRENT_STEP_ID}" "FAIL" "${ERROR_CONTEXT}"
  fi
  return "${rc}"
}

on_exit() {
  local rc="$?"
  if [[ "${rc}" -ne 0 ]]; then
    FAILED="1"
    if [[ -n "${CURRENT_STEP_ID}" && "${STEP_STATE[${CURRENT_STEP_ID}]:-}" == "RUNNING" ]]; then
      step_finish "${CURRENT_STEP_ID}" "FAIL" "exit=${rc}"
    fi
  fi
  if [[ "${CNC_SUPPRESS_INSTALLER_SUMMARY:-0}" != "1" ]]; then
    render_summary
  elif [[ "${rc}" -ne 0 ]]; then
    show_log_tail
  fi
}

main() {
  print_banner
  run_step "preflight.root" "Validate root privileges" require_root
  run_step "preflight.os" "Validate Ubuntu host" require_ubuntu
  run_step "preflight.tools" "Validate base install tools" require_base_tools
  run_step "preflight.arch" "Validate host architecture" require_supported_architecture
  run_step "preflight.lock" "Acquire installer lock" acquire_install_lock
  run_step "preflight.path" "Resolve application directory" resolve_app_dir
  run_step "logging.init" "Initialize installer logging" init_logging
  run_step "packages.install" "Install OS dependencies" install_packages
  run_step "python.version" "Validate Python runtime version" require_supported_python
  run_step "podman.version" "Install supported Podman guest runtime" ensure_supported_podman
  run_step "podman.network" "Configure Podman firewall integration" configure_podman_network
  run_step "packages.hygiene" "Purge insecure legacy network packages" remove_insecure_packages
  run_step "logs.retention" "Configure system log retention" configure_system_log_retention
  run_step "hosts.equiv" "Disable trusted-host login file" enforce_hosts_equiv_absent
  run_step "sysctl.write" "Write kernel/network tuning sysctls" write_sysctl_tuning
  run_step "sysctl.bbr" "Configure BBR when kernel supports it" write_bbr_sysctl
  run_step "sysctl.apply" "Apply kernel/sysctl tuning" apply_sysctl_tuning
  run_step "tailscale.install" "Install and start Tailscale" install_tailscale
  run_step "filesystem.setup" "Create CNC system directories" setup_directories
  run_step "release.prepare" "Create initial immutable release" prepare_install_release
  run_step "systemd.limits" "Configure service NOFILE ceilings" configure_systemd_nofile_limits
  run_step "nginx.main" "Tune nginx workers and file descriptor limits" configure_nginx_main_tuning
  run_step "nginx.http" "Write nginx HTTP throughput tuning defaults" write_nginx_http_tuning
  run_step "nginx.setup" "Configure nginx generated include" setup_nginx_include
  run_step "python.setup" "Build release python runtime and install cnc" setup_python_env
  run_step "env.setup" "Write cnc environment file" write_env_file
  run_step "release.preflight" "Check staged release and database" validate_staged_release
  run_step "tailscale.join" "Join Tailnet (TS_AUTHKEY if provided)" join_tailnet
  run_step "runtime.link" "Update runtime symlink" setup_runtime_link
  run_step "cli.wrapper" "Install cnc-admin host wrapper" write_cli_wrapper
  run_step "ssh.assets" "Install backend SSH access assets" write_backend_ssh_assets
  run_step "systemd.unit" "Write cnc-admin systemd unit" write_systemd_unit
  run_step "systemd.enable" "Enable and restart cnc-admin" enable_services
  run_step "ssh.reload" "Reload SSH daemon for backend aliases" reload_ssh_service
  run_step "network.firewall" "Configure baseline firewall (UFW)" configure_firewall_baseline
  run_step "systemd.health" "Verify cnc-admin health endpoint" verify_admin_service
  run_step "systemd.paths" "Verify cnc-admin writable system paths" verify_admin_service_write_paths
  run_step "cli.health" "Verify cnc-admin CLI wrapper" verify_cli_wrapper
  run_step "ssh.health" "Verify backend SSH assets" verify_backend_ssh_assets
  run_step "network.ssh-rules" "Verify SSH and HTTP firewall rules are live" verify_ssh_firewall_rules
  run_step "nginx.health" "Verify nginx runtime health" verify_nginx_runtime
  run_step "podman.basic" "Verify Podman basic container run health" verify_podman_basic_run
  run_step "network.app-smoke" "Verify isolated app container networking" verify_isolated_app_network
  run_step "network.proxy-smoke" "Verify loopback proxy unit start" verify_loopback_proxy_start
  run_step "tailscale.serve" "Expose admin on tailnet HTTPS URL" configure_tailscale_admin_access
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  trap on_error ERR
  trap on_exit EXIT
  main "$@"
fi

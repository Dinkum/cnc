#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${ENV_FILE:-/etc/cnc.env}"
DEFAULT_GITHUB_REPO="${DEFAULT_GITHUB_REPO:-}"
CLI_WRAPPER_PATH="${CLI_WRAPPER_PATH:-/usr/local/bin/cnc-admin}"
LLM_HELP_WRAPPER_PATH="${LLM_HELP_WRAPPER_PATH:-/usr/local/bin/llm-help}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
MIGRATION_BOUNDARY_CROSSED=0

log() {
  printf '[updater] %s\n' "$1"
}

fail() {
  printf '[updater][error] %s\n' "$1" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    fail "run as root (sudo ./scripts/update_from_github.sh)"
  fi
}

normalize_github_repo() {
  local raw="$1"
  local cleaned
  cleaned="$(printf '%s' "${raw}" | sed -E \
    -e 's#^https?://[^@/]+@github\.com/#https://github.com/#' \
    -e 's#^https?://github\.com/##' \
    -e 's#^ssh://git@github\.com/##' \
    -e 's#^git@github\.com:##' \
    -e 's#\.git$##' \
    -e 's#/$##')"

  if [[ "${cleaned}" != */* ]]; then
    return 1
  fi
  local owner repo
  owner="${cleaned%%/*}"
  repo="${cleaned#*/}"
  repo="${repo%%/*}"
  if [[ -z "${owner}" || -z "${repo}" ]]; then
    return 1
  fi
  printf '%s/%s\n' "${owner}" "${repo}"
}

infer_github_repo() {
  local candidate remote inferred
  for candidate in "${CNC_CURRENT_LINK:-}" "${CNC_BASE_DIR:-}" "/opt/cnc"; do
    [[ -n "${candidate}" ]] || continue
    [[ -d "${candidate}" ]] || continue
    remote="$(git -C "${candidate}" config --get remote.origin.url 2>/dev/null || true)"
    if [[ -z "${remote}" ]]; then
      continue
    fi
    inferred="$(normalize_github_repo "${remote}" || true)"
    if [[ -n "${inferred}" ]]; then
      printf '%s\n' "${inferred}"
      return 0
    fi
  done
  return 1
}

load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    local line trimmed key value
    while IFS= read -r line || [[ -n "${line}" ]]; do
      trimmed="${line#"${line%%[![:space:]]*}"}"
      [[ -n "${trimmed}" ]] || continue
      if [[ "${trimmed}" == \#* || "${trimmed}" == \;* ]]; then
        continue
      fi
      if [[ ! "${trimmed}" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
        continue
      fi
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      if [[ "${value}" =~ ^\"(.*)\"$ ]]; then
        value="${BASH_REMATCH[1]}"
      elif [[ "${value}" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
      fi
      export "${key}=${value}"
    done < "${ENV_FILE}"
  fi
}

require_vars() {
  GITHUB_REF="${GITHUB_REF:-main}"
  CNC_BASE_DIR="${CNC_BASE_DIR:-/var/lib/cnc}"
  CNC_RELEASES_DIR="${CNC_RELEASES_DIR:-/var/lib/cnc/releases}"
  CNC_CURRENT_LINK="${CNC_CURRENT_LINK:-/var/lib/cnc/current}"
  CNC_SERVICE_NAME="${CNC_SERVICE_NAME:-cnc-admin}"
  CNC_KEEP_RELEASES="${CNC_KEEP_RELEASES:-5}"
  CNC_UPDATER_DIR="${CNC_UPDATER_DIR:-/var/lib/cnc/updater}"
  if [[ -z "${GITHUB_REPO:-}" ]]; then
    GITHUB_REPO="$(infer_github_repo || true)"
  fi
  if [[ -z "${GITHUB_REPO:-}" && -n "${DEFAULT_GITHUB_REPO}" ]]; then
    GITHUB_REPO="${DEFAULT_GITHUB_REPO}"
    log "GITHUB_REPO not set; using DEFAULT_GITHUB_REPO=${GITHUB_REPO}"
  fi
  [[ -n "${GITHUB_REPO}" ]] || fail "GITHUB_REPO is not set and could not be inferred from git origin"
  [[ "${GITHUB_REPO}" == */* ]] || fail "invalid GITHUB_REPO=${GITHUB_REPO} (expected owner/repo)"
}

validate_path_guards() {
  local base releases updater current
  base="$(realpath -m "${CNC_BASE_DIR}")"
  releases="$(realpath -m "${CNC_RELEASES_DIR}")"
  updater="$(realpath -m "${CNC_UPDATER_DIR}")"
  current="$(realpath -m -s "${CNC_CURRENT_LINK}")"

  [[ "${base}" != "/" ]] || fail "CNC_BASE_DIR cannot be /"
  [[ "${releases}" == "${base}"/* ]] || fail "CNC_RELEASES_DIR must be under ${base}"
  [[ "${updater}" == "${base}"/* ]] || fail "CNC_UPDATER_DIR must be under ${base}"
  [[ "${current}" == "${base}"/* ]] || fail "CNC_CURRENT_LINK must be under ${base}"
}

setup_dirs() {
  install -d -m 0755 "${CNC_RELEASES_DIR}"
  install -d -m 0755 "${CNC_UPDATER_DIR}"
}

write_wrapper() {
  local path="$1"
  local command_line="$2"
  local tmp_file
  tmp_file="$(mktemp "${CNC_UPDATER_DIR}/wrapper.XXXXXX")"
  cat > "${tmp_file}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
${command_line}
EOF
  install -m 0755 "${tmp_file}" "${path}" || return
  rm -f "${tmp_file}" || return
}

sync_cli_wrappers() {
  log "syncing managed CLI wrappers"
  write_wrapper "${CLI_WRAPPER_PATH}" "exec ${CNC_CURRENT_LINK}/.venv/bin/cnc-admin \"\$@\"" || return
  write_wrapper "${LLM_HELP_WRAPPER_PATH}" "exec ${CNC_CURRENT_LINK}/.venv/bin/cnc-admin llm-help \"\$@\"" || return
}

sync_backend_ssh_wrapper() {
  log "syncing managed backend SSH wrapper"
  "${CNC_CURRENT_LINK}/.venv/bin/python" -c \
    'from app.config import get_settings; from app.services.ssh_access import reconcile_backend_ssh_root_wrapper; reconcile_backend_ssh_root_wrapper(get_settings())'
}

sync_systemd_units_from_dir() {
  local source_dir="$1"
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-admin.service" "cnc-admin.service" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-auto-size.service" "cnc-auto-size.service" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-auto-size.timer" "cnc-auto-size.timer" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-backend-alerts.service" "cnc-backend-alerts.service" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-backend-alerts.timer" "cnc-backend-alerts.timer" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-cloudflare-sync.service" "cnc-cloudflare-sync.service" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-cloudflare-sync.timer" "cnc-cloudflare-sync.timer" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-update-check.service" "cnc-update-check.service" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/cnc-update-check.timer" "cnc-update-check.timer" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/systemd/cnc-edge-public-ingress.slice" "cnc-edge-public-ingress.slice" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/systemd/cnc-edge-tailnet.slice" "cnc-edge-tailnet.slice" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/systemd/cnc-admin-control.slice" "cnc-admin-control.slice" || return
  sync_systemd_asset_from_dir "${source_dir}" "packaging/systemd/cnc-apps.slice" "cnc-apps.slice" || return
  systemctl daemon-reload || return
  systemctl enable --now cnc-auto-size.timer || return
  systemctl enable --now cnc-backend-alerts.timer || return
  systemctl enable --now cnc-cloudflare-sync.timer || return
  systemctl enable --now cnc-update-check.timer || return
}

sync_systemd_asset_from_dir() {
  local source_dir="$1"
  local source_path="$2"
  local destination_name="$3"

  install -D -m 0644 "${source_dir}/${source_path}" "${SYSTEMD_UNIT_DIR}/${destination_name}" || return
}

sync_systemd_units() {
  log "syncing managed systemd units"
  sync_systemd_units_from_dir "${RELEASE_DIR}"
}

verify_admin_service_write_paths() {
  local unit_text protect_system required_path main_pid probe_path
  unit_text="$(systemctl cat "${CNC_SERVICE_NAME}" 2>/dev/null || true)"
  if [[ -z "${unit_text}" ]]; then
    log "unable to inspect ${CNC_SERVICE_NAME} unit"
    return 1
  fi

  protect_system="$(systemctl show "${CNC_SERVICE_NAME}" -p ProtectSystem --value 2>/dev/null || true)"
  if [[ "${protect_system}" != "no" ]]; then
    log "${CNC_SERVICE_NAME} ProtectSystem must be no, got ${protect_system:-unset}"
    return 1
  fi

  for required_path in /etc /etc/containers/systemd /var/lib/cnc /var/log/cnc; do
    if ! grep -Fq "${required_path}" <<<"${unit_text}"; then
      log "missing ${CNC_SERVICE_NAME} ReadWritePaths entry for ${required_path}"
      return 1
    fi
  done

  if ! command -v nsenter >/dev/null 2>&1; then
    log "nsenter is required to verify ${CNC_SERVICE_NAME} mount namespace writes"
    return 1
  fi

  main_pid="$(systemctl show "${CNC_SERVICE_NAME}" -p MainPID --value 2>/dev/null || true)"
  if [[ ! "${main_pid}" =~ ^[0-9]+$ || "${main_pid}" -le 1 ]]; then
    log "${CNC_SERVICE_NAME} MainPID is not available for write-path verification"
    return 1
  fi

  probe_path="/etc/containers/systemd/.cnc-write-test.update.$$"
  if ! nsenter -t "${main_pid}" -m -- sh -c "probe_path=\"\$1\"; : > \"\${probe_path}\" && chmod 0644 \"\${probe_path}\" && rm -f \"\${probe_path}\"" sh "${probe_path}"; then
    log "${CNC_SERVICE_NAME} cannot write ${probe_path} inside its active mount namespace"
    return 1
  fi
}

restore_previous_systemd_units() {
  local previous_target="$1"
  if [[ -z "${previous_target}" || ! -d "${previous_target}/packaging" ]]; then
    systemctl daemon-reload || true
    return 0
  fi
  log "restoring managed systemd units from previous release"
  sync_systemd_units_from_dir "${previous_target}" || systemctl daemon-reload || true
}

rollback_release_switch() {
  local previous_target="$1"
  local tmp_link="$2"
  if [[ -z "${previous_target}" ]]; then
    return 0
  fi
  if [[ "${MIGRATION_BOUNDARY_CROSSED}" == "1" ]]; then
    log "migration boundary crossed; automatic rollback to previous release is disabled"
    return 1
  fi
  ln -sfn "${previous_target}" "${tmp_link}"
  mv -Tf "${tmp_link}" "${CNC_CURRENT_LINK}"
  restore_previous_systemd_units "${previous_target}"
  systemctl restart "${CNC_SERVICE_NAME}" || true
}

acquire_lock() {
  LOCK_FILE="${CNC_UPDATER_DIR}/update.lock"
  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    fail "another update is already running"
  fi
}

build_askpass() {
  if [[ -z "${GITHUB_READONLY_PAT:-}" ]]; then
    ASKPASS_FILE=""
    return 0
  fi
  ASKPASS_FILE="$(mktemp "${CNC_UPDATER_DIR}/askpass.XXXXXX")"
  chmod 0700 "${ASKPASS_FILE}"
  cat > "${ASKPASS_FILE}" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *Password*) printf '%s\n' "${GITHUB_READONLY_PAT}" ;;
  *) printf '\n' ;;
esac
EOF
}

cleanup() {
  local code="$?"
  if [[ -n "${WORK_DIR:-}" && -d "${WORK_DIR}" ]]; then
    rm -rf "${WORK_DIR}"
  fi
  if [[ -n "${ASKPASS_FILE:-}" && -f "${ASKPASS_FILE}" ]]; then
    rm -f "${ASKPASS_FILE}"
  fi
  if [[ "${code}" -ne 0 ]]; then
    log "update failed"
  fi
}

clone_repo() {
  WORK_DIR="$(mktemp -d "${CNC_UPDATER_DIR}/work.XXXXXX")"
  log "cloning ${GITHUB_REPO}@${GITHUB_REF}"
  if [[ -n "${GITHUB_READONLY_PAT:-}" ]]; then
    GITHUB_READONLY_PAT="${GITHUB_READONLY_PAT}" \
    GIT_TERMINAL_PROMPT=0 \
    GIT_ASKPASS="${ASKPASS_FILE}" \
    git clone --depth 1 --branch "${GITHUB_REF}" "https://github.com/${GITHUB_REPO}.git" "${WORK_DIR}/repo"
    return
  fi
  GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch "${GITHUB_REF}" "https://github.com/${GITHUB_REPO}.git" "${WORK_DIR}/repo"
}

prepare_release() {
  local commit
  commit="$(git -C "${WORK_DIR}/repo" rev-parse --short=12 HEAD)"
  local stamp
  stamp="$(date +%Y%m%d%H%M%S)"
  RELEASE_DIR="${CNC_RELEASES_DIR}/${stamp}-${commit}"
  mv "${WORK_DIR}/repo" "${RELEASE_DIR}"
  log "created release ${RELEASE_DIR}"
}

validate_release() {
  log "building virtualenv for new release"
  python3 -m venv "${RELEASE_DIR}/.venv"
  "${RELEASE_DIR}/.venv/bin/pip" install --require-hashes --only-binary=:all: -r "${RELEASE_DIR}/requirements.txt"
  "${RELEASE_DIR}/.venv/bin/pip" install --no-deps -e "${RELEASE_DIR}"

  log "running release preflight checks"
  "${RELEASE_DIR}/.venv/bin/python" -m compileall "${RELEASE_DIR}/app" >/dev/null
  (
    cd "${RELEASE_DIR}" &&
      "${RELEASE_DIR}/.venv/bin/python" -c "import app.main"
  )
  (
    cd "${RELEASE_DIR}" &&
      "${RELEASE_DIR}/.venv/bin/python" -m app.release_preflight --database-url "${DATABASE_URL}"
  )
}

switch_release() {
  local previous_target
  previous_target="$(readlink -f "${CNC_CURRENT_LINK}" || true)"

  local tmp_link
  tmp_link="${CNC_CURRENT_LINK}.new"
  ln -sfn "${RELEASE_DIR}" "${tmp_link}"
  mv -Tf "${tmp_link}" "${CNC_CURRENT_LINK}"
  log "switched ${CNC_CURRENT_LINK} -> ${RELEASE_DIR}"
  if ! sync_systemd_units; then
    log "systemd unit sync failed, rolling back"
    rollback_release_switch "${previous_target}" "${tmp_link}"
    fail "update failed while syncing systemd units"
  fi
  if ! sync_cli_wrappers; then
    log "CLI wrapper sync failed, rolling back"
    rollback_release_switch "${previous_target}" "${tmp_link}"
    fail "update failed while syncing CLI wrappers"
  fi

  MIGRATION_BOUNDARY_CROSSED=1
  log "migration boundary crossed; old-release rollback is disabled after service restart begins"
  if ! systemctl restart "${CNC_SERVICE_NAME}"; then
    log "service restart failed after migration boundary"
    rollback_release_switch "${previous_target}" "${tmp_link}"
    fail "update failed while restarting ${CNC_SERVICE_NAME}"
  fi

  if ! systemctl is-active --quiet "${CNC_SERVICE_NAME}"; then
    log "service active check failed after migration boundary"
    rollback_release_switch "${previous_target}" "${tmp_link}"
    fail "${CNC_SERVICE_NAME} is not active"
  fi
  if ! verify_admin_service_write_paths; then
    log "service write-path verification failed after migration boundary"
    rollback_release_switch "${previous_target}" "${tmp_link}"
    fail "${CNC_SERVICE_NAME} cannot write required host runtime paths"
  fi
  if ! sync_backend_ssh_wrapper; then
    fail "update failed while syncing backend SSH wrapper"
  fi
}

prune_releases() {
  local keep
  keep="${CNC_KEEP_RELEASES}"
  if [[ "${keep}" -lt 1 ]]; then
    keep=1
  fi

  local current_target
  current_target="$(readlink -f "${CNC_CURRENT_LINK}" || true)"
  mapfile -t releases < <(find "${CNC_RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d | sort -r)

  local kept=0
  for release in "${releases[@]}"; do
    if [[ "${release}" == "${current_target}" ]]; then
      ((kept += 1))
      continue
    fi
    if [[ "${kept}" -lt "${keep}" ]]; then
      ((kept += 1))
      continue
    fi
    [[ "${release}" == "${CNC_RELEASES_DIR}"/* ]] || fail "unsafe release path: ${release}"
    rm -rf "${release}"
  done
}

main() {
  require_root
  load_env
  require_vars
  validate_path_guards
  setup_dirs
  acquire_lock
  trap cleanup EXIT
  build_askpass
  clone_repo
  prepare_release
  validate_release
  switch_release
  prune_releases
  log "update complete"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

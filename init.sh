#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="${ROOT_DIR}/scripts/install_ubuntu.sh"

print_rule() {
  printf '+------------------------------------------------------------------------+\n'
}

box_line() {
  printf '| %-70.70s |\n' "$1"
}

box_line_wrap() {
  local value="$1"
  if [[ -z "${value}" ]]; then
    box_line ""
    return 0
  fi
  while [[ -n "${value}" ]]; do
    box_line "${value:0:70}"
    value="${value:70}"
  done
}

blank_line() {
  printf '|                                                                        |\n'
}

print_banner() {
  print_rule
  printf '|   _______   _   _   _______                                            |\n'
  printf '|  / _____/  / | / |  / _____/                                           |\n'
  printf '| | |       /  |/  | | |                                                 |\n'
  printf '| | |____  / /|   /  | |____                                             |\n'
  printf '|  \\_____/ /_/ |__/    \\_____/                                           |\n'
  blank_line
  printf '| One VPS. Many tiny projects. Zero panel bloat.                         |\n'
  printf '| Friendly Ubuntu setup for CNC                                          |\n'
  print_rule
}

fail() {
  printf '[init][error] %s\n' "$1" >&2
  exit 1
}

interactive() {
  [[ -t 0 && -t 1 ]]
}

prompt_default() {
  local var_name="$1"
  local prompt="$2"
  local default_value="$3"
  local value
  if [[ -n "${!var_name:-}" ]]; then
    return 0
  fi
  if ! interactive; then
    printf -v "${var_name}" '%s' "${default_value}"
    return 0
  fi
  read -r -p "${prompt} [${default_value}]: " value
  printf -v "${var_name}" '%s' "${value:-$default_value}"
}

prompt_optional_secret() {
  local var_name="$1"
  local prompt="$2"
  local value
  if [[ -n "${!var_name:-}" ]]; then
    return 0
  fi
  if ! interactive; then
    return 0
  fi
  read -r -s -p "${prompt} (blank to skip): " value
  printf '\n'
  printf -v "${var_name}" '%s' "${value}"
}

prompt_yes_no() {
  local var_name="$1"
  local prompt="$2"
  local default_value="$3"
  local suffix answer normalized
  if [[ -n "${!var_name:-}" ]]; then
    return 0
  fi
  if ! interactive; then
    printf -v "${var_name}" '%s' "${default_value}"
    return 0
  fi
  if [[ "${default_value}" == "1" ]]; then
    suffix="Y/n"
  else
    suffix="y/N"
  fi
  read -r -p "${prompt} [${suffix}]: " answer
  normalized="$(printf '%s' "${answer}" | tr '[:upper:]' '[:lower:]')"
  case "${normalized}" in
    y|yes)
      printf -v "${var_name}" '1'
      ;;
    n|no)
      printf -v "${var_name}" '0'
      ;;
    "")
      printf -v "${var_name}" '%s' "${default_value}"
      ;;
    *)
      fail "expected yes or no for: ${prompt}"
      ;;
  esac
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "run this wrapper as root: sudo ./init.sh"
}

require_installer() {
  [[ -x "${INSTALLER}" ]] || fail "missing executable installer: ${INSTALLER}"
}

collect_preferences() {
  local default_hostname
  default_hostname="$(hostname -s 2>/dev/null || echo cnc-vps)"

  print_rule
  box_line "Welcome. This wrapper asks a few questions, then runs the installer."
  box_line "Press Enter to accept defaults. Secrets are not echoed."
  print_rule
  printf '\n'

  printf '[1/5] GitHub updates\n'
  prompt_optional_secret "GITHUB_READONLY_PAT" "GitHub token for private repos or rate limits"
  printf '\n[2/5] Tailnet join\n'
  prompt_optional_secret "TS_AUTHKEY" "Tailscale auth key"
  printf '\n[3/5] Node identity\n'
  prompt_default "TS_HOSTNAME" "Tailscale hostname" "${default_hostname}"
  printf '\n[4/5] Public ingress guard\n'
  prompt_yes_no "NGINX_CLOUDFLARE_ONLY" "Restrict public 80/443 to Cloudflare CIDRs" "1"
  printf '\n[5/5] Admin trusted hosts\n'
  prompt_default "ADMIN_ALLOWED_HOSTS" "Extra trusted admin hostnames, comma-separated" ""
  printf '\n'
}

render_plan() {
  print_rule
  printf '| CNC SETUP PLAN                                                         |\n'
  print_rule
  box_line "The installer will use these choices:"
  blank_line
  printf '| Tailscale hostname: %-50.50s |\n' "${TS_HOSTNAME:-}"
  if [[ -n "${TS_AUTHKEY:-}" ]]; then
    printf '| Tailscale auth key: %-49s |\n' "provided"
  else
    printf '| Tailscale auth key: %-49s |\n' "browser login if needed"
  fi
  if [[ -n "${GITHUB_READONLY_PAT:-}" ]]; then
    printf '| GitHub PAT: %-58s |\n' "provided"
  else
    printf '| GitHub PAT: %-58s |\n' "not provided"
  fi
  if [[ "${NGINX_CLOUDFLARE_ONLY:-1}" == "1" ]]; then
    printf '| Public HTTP/S: %-55s |\n' "Cloudflare-only"
  else
    printf '| Public HTTP/S: %-55s |\n' "open 80/443"
  fi
  if [[ -n "${ADMIN_ALLOWED_HOSTS:-}" ]]; then
    printf '| Extra admin hosts: %-50s |\n' "${ADMIN_ALLOWED_HOSTS}"
  else
    printf '| Extra admin hosts: %-50s |\n' "none"
  fi
  print_rule
}

read_env_value() {
  local key="$1"
  local path="/etc/cnc.env"
  [[ -f "${path}" ]] || return 0
  awk -F= -v k="${key}" '$1 == k { print substr($0, index($0, "=") + 1); exit }' "${path}" 2>/dev/null || true
}

service_state() {
  local name="$1"
  systemctl is-active "${name}" 2>/dev/null || printf 'unknown'
}

tailscale_admin_url() {
  command -v tailscale >/dev/null 2>&1 || return 0
  tailscale serve status 2>/dev/null | grep -Eo 'https://[^ ]+' | head -n1 || true
}

tailscale_ip() {
  command -v tailscale >/dev/null 2>&1 || return 0
  tailscale ip -4 2>/dev/null | head -n1 || true
}

ssh_target() {
  local target
  target="$(tailscale_ip)"
  if [[ -z "${target}" ]]; then
    target="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -z "${target}" ]]; then
    target="$(hostname -s 2>/dev/null || echo your-vps)"
  fi
  printf '%s\n' "${target}"
}

ssh_tunnel_command() {
  local target="$1"
  printf 'ssh -o ControlMaster=auto -o ControlPersist=5m -o ControlPath=/tmp/cnc-ssh-%%C -fN -L 9090:127.0.0.1:9090 root@%s\n' "${target}"
}

render_final_summary() {
  local installer_rc="$1"
  local install_status admin_state nginx_state tailscaled_state cloudflare_mode admin_url target
  if [[ "${installer_rc}" == "0" ]]; then
    install_status="SUCCESS"
  else
    install_status="FAILED"
  fi

  admin_state="$(service_state cnc-admin)"
  nginx_state="$(service_state nginx)"
  tailscaled_state="$(service_state tailscaled)"
  cloudflare_mode="$(read_env_value NGINX_CLOUDFLARE_ONLY)"
  [[ -n "${cloudflare_mode}" ]] || cloudflare_mode="${NGINX_CLOUDFLARE_ONLY:-1}"
  if [[ "${cloudflare_mode}" == "1" ]]; then
    cloudflare_mode="Cloudflare-only"
  else
    cloudflare_mode="open 80/443"
  fi
  admin_url="$(tailscale_admin_url)"
  target="$(ssh_target)"

  printf '\n'
  print_rule
  printf '| CNC INIT SUMMARY                                                       |\n'
  print_rule
  box_line "Install result: ${install_status}"
  box_line "App checkout: ${ROOT_DIR}"
  box_line "Runtime state: /var/lib/cnc"
  box_line "Host settings: /etc/cnc.env"
  box_line "Installer log: /var/log/cnc/installer.log"
  box_line "Public HTTP/S: ${cloudflare_mode}"
  box_line "cnc-admin.service: ${admin_state}"
  box_line "nginx.service: ${nginx_state}"
  box_line "tailscaled.service: ${tailscaled_state}"
  print_rule

  if [[ "${installer_rc}" == "0" ]]; then
    printf '| NEXT STEPS                                                             |\n'
    print_rule
    if [[ -n "${admin_url}" ]]; then
      box_line "Open CNC:"
      box_line_wrap "${admin_url}"
      box_line "Use HTTPS explicitly. Plain HTTP is public routing."
    else
      box_line "Open a tunnel from your laptop:"
      box_line_wrap "$(ssh_tunnel_command "${target}")"
      box_line "Then visit: http://127.0.0.1:9090"
    fi
    blank_line
    box_line "Create your admin access key on first visit."
    box_line "Then create an Output, create an Input, attach, and Save."
    print_rule
    printf '| USEFUL COMMANDS                                                        |\n'
    print_rule
    box_line "systemctl status cnc-admin --no-pager"
    box_line "journalctl -u cnc-admin -n 80 --no-pager"
    box_line "cnc-admin logs admin"
    box_line "llm-help"
    print_rule
  else
    printf '| RECOVERY                                                               |\n'
    print_rule
    box_line "Installer failed. The detailed installer log is usually at:"
    box_line "/var/log/cnc/installer.log"
    box_line "Check service/log state after fixing the reported error:"
    box_line "systemctl status cnc-admin --no-pager"
    box_line "journalctl -u cnc-admin -n 80 --no-pager"
    print_rule
  fi
}

main() {
  local installer_rc
  print_banner
  require_root
  require_installer
  collect_preferences
  render_plan

  export CNC_APP_DIR="${ROOT_DIR}"
  export GITHUB_READONLY_PAT="${GITHUB_READONLY_PAT:-}"
  export TS_AUTHKEY="${TS_AUTHKEY:-}"
  export TS_HOSTNAME="${TS_HOSTNAME:-}"
  export NGINX_CLOUDFLARE_ONLY="${NGINX_CLOUDFLARE_ONLY:-1}"
  export ADMIN_ALLOWED_HOSTS="${ADMIN_ALLOWED_HOSTS:-}"
  export CNC_SUPPRESS_INSTALLER_SUMMARY=1

  set +e
  "${INSTALLER}" "$@"
  installer_rc="$?"
  set -e

  render_final_summary "${installer_rc}"
  exit "${installer_rc}"
}

main "$@"

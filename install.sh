#!/usr/bin/env bash
# Install warp-healthcheck on a PasarGuard panel server.
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/H-Alireza/pasarguard-warp-healthcheck/main/install.sh)
#   bash <(curl -fsSL .../install.sh) update
#   sudo bash install.sh uninstall
set -euo pipefail

PREFIX="${WARP_HEALTHCHECK_PREFIX:-/opt/warp-healthcheck}"
ETCDIR="${WARP_HEALTHCHECK_ETC:-/etc/warp-healthcheck}"
STATE_DIR="/var/lib/warp-healthcheck"
SERVICE_NAME="warp-healthcheck"
BIN_LINK="/usr/local/bin/warp-healthcheck"
APP_DIR="${PREFIX}/app"
VENV_DIR="${PREFIX}/.venv"
CONFIG_FILE="${ETCDIR}/config.yaml"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
REPO_URL="${WARP_HEALTHCHECK_REPO:-https://github.com/H-Alireza/pasarguard-warp-healthcheck.git}"
REPO_BRANCH="${WARP_HEALTHCHECK_BRANCH:-main}"
TMP_DIR=""

ACTION="install"
NON_INTERACTIVE=0
FORCE_CONFIG=0
SKIP_DOCTOR=0
SKIP_SETUP=1
RUN_SETUP=0
START_SERVICE=1
NO_TLS_VERIFY=0
BASE_URL="${PASARGUARD_BASE_URL:-}"
USERNAME="${PASARGUARD_USERNAME:-}"
PASSWORD="${PASARGUARD_PASSWORD:-}"
TELEGRAM_TOKEN="${WARP_HEALTHCHECK_TELEGRAM_TOKEN:-}"
TELEGRAM_CHAT_ID="${WARP_HEALTHCHECK_TELEGRAM_CHAT_ID:-}"
KEEP_CONFIG=1
TTY="/dev/tty"

usage() {
  cat <<'EOF'
Install warp-healthcheck (PasarGuard Warp outbound monitor).

Usage:
  sudo bash install.sh [install] [options]
  sudo bash install.sh update             # fetch latest from GitHub, keep config
  sudo bash install.sh reconfigure
  sudo bash install.sh uninstall [--purge]

Options:
  --base-url URL         Panel URL (or PASARGUARD_BASE_URL)
  --username NAME        Panel admin username (or PASARGUARD_USERNAME)
  --password PASS        Panel admin password (or PASARGUARD_PASSWORD)
  --telegram-token TOK   Telegram bot token for alerts (optional)
  --telegram-chat-id ID  Telegram chat id for alerts (optional)
  --no-tls-verify        Set panel.verify_tls: false
  --force-config         Overwrite /etc/warp-healthcheck/config.yaml
  --setup                Run Observatory setup (-y) after install
  --skip-doctor          Do not run doctor after install
  --no-start             Install the unit but do not enable/start it
  --non-interactive      No prompts; required values must be set
  --purge                With uninstall, also delete config
  -h, --help             Show this help

Environment:
  WARP_HEALTHCHECK_PREFIX   Install prefix (default /opt/warp-healthcheck)
  WARP_HEALTHCHECK_ETC      Config dir (default /etc/warp-healthcheck)
  WARP_HEALTHCHECK_REPO     Git URL to install from (default: the GitHub repo)
  WARP_HEALTHCHECK_BRANCH   Branch to install from (default: main)
EOF
}

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

cleanup() {
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

is_piped() {
  local src="${BASH_SOURCE[0]:-}"
  [[ -z "$src" || "$src" == /dev/fd/* || "$src" == /proc/self/fd/* ]]
}

need_root() {
  [[ "$(id -u)" -eq 0 ]] || die "Run as root: sudo bash install.sh"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

python_ok() {
  local bin="$1"
  have_cmd "$bin" || return 1
  "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

detect_python() {
  local cand
  for cand in python3.13 python3.12 python3.11 python3; do
    if python_ok "$cand"; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

install_python() {
  if have_cmd apt-get; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip ca-certificates
    if python_ok python3; then
      return 0
    fi
    apt-get install -y python3.12 python3.12-venv python3.11 python3.11-venv || true
  elif have_cmd dnf; then
    dnf install -y python3 python3-pip python3-virtualenv ca-certificates || \
      dnf install -y python3.12 python3.11 ca-certificates
  elif have_cmd yum; then
    yum install -y python3 python3-pip ca-certificates
  else
    die "Install Python 3.11+ (with venv) and re-run"
  fi
}

ensure_python() {
  if PYTHON_BIN="$(detect_python)"; then
    return 0
  fi
  log "Installing Python 3.11+"
  install_python
  PYTHON_BIN="$(detect_python)" || die "Python 3.11+ is required"
}

ensure_git() {
  have_cmd git && return 0
  if have_cmd apt-get; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y git ca-certificates
  elif have_cmd dnf; then
    dnf install -y git ca-certificates
  elif have_cmd yum; then
    yum install -y git ca-certificates
  else
    die "git is required to fetch the source"
  fi
}

# Pick the source to install from. With FROM_REMOTE=1 (the `update` action),
# always fetch from GitHub. Otherwise use the checkout this script lives in,
# unless that is the installed copy itself, which would be a no-op upgrade.
resolve_source_dir() {
  local from_remote="${1:-0}"
  if [[ -n "${SOURCE_DIR:-}" && -f "${SOURCE_DIR}/pyproject.toml" ]]; then
    return 0
  fi
  if [[ "$from_remote" -ne 1 ]] && ! is_piped; then
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "${script_dir}/pyproject.toml" && "$script_dir" != "$APP_DIR" ]]; then
      SOURCE_DIR="$script_dir"
      return 0
    fi
  fi
  [[ -n "$REPO_URL" ]] || die "Run this script from the cloned repo, or set WARP_HEALTHCHECK_REPO"
  ensure_git
  TMP_DIR="$(mktemp -d /tmp/warp-healthcheck.XXXXXX)"
  log "Fetching ${REPO_URL} (${REPO_BRANCH})"
  git clone --quiet --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "${TMP_DIR}/src"
  SOURCE_DIR="${TMP_DIR}/src"
}

installed_version() {
  "${VENV_DIR}/bin/warp-healthcheck" --version 2>/dev/null | awk '{print $2}' || true
}

copy_app() {
  mkdir -p "$PREFIX" "$APP_DIR"
  if [[ "$(cd "$SOURCE_DIR" && pwd)" == "$(cd "$APP_DIR" && pwd)" ]]; then
    return 0
  fi
  log "Installing source to ${APP_DIR}"
  if have_cmd rsync; then
    rsync -a --delete \
      --exclude '.git/' \
      --exclude '.venv/' \
      --exclude '__pycache__/' \
      --exclude '*.egg-info/' \
      --exclude 'config.yaml' \
      "${SOURCE_DIR}/" "${APP_DIR}/"
  else
    rm -rf "${APP_DIR}"
    mkdir -p "${APP_DIR}"
    cp -a "${SOURCE_DIR}/." "${APP_DIR}/"
    rm -rf "${APP_DIR}/.git" "${APP_DIR}/.venv"
  fi
}

create_venv() {
  log "Creating venv at ${VENV_DIR}"
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
  "${VENV_DIR}/bin/pip" install --quiet --upgrade "$APP_DIR"
}

write_wrapper() {
  mkdir -p "$(dirname "$BIN_LINK")"
  cat > "$BIN_LINK" <<EOF
#!/bin/sh
exec "${VENV_DIR}/bin/warp-healthcheck" "\$@"
EOF
  chmod 755 "$BIN_LINK"
}

write_unit() {
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PasarGuard Warp outbound health check
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${VENV_DIR}/bin/warp-healthcheck --config ${CONFIG_FILE} run
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StateDirectory=${SERVICE_NAME}

# Sandboxing: the daemon only needs outbound HTTPS and its state directory.
CapabilityBoundingSet=
AmbientCapabilities=
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF
}

# Prompts read from the terminal, so `curl ... | bash` can still ask questions.
prompt() {
  local q="$1" default="${2:-}"
  local reply=""
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    printf '%s\n' "$default"
    return 0
  fi
  if [[ -n "$default" ]]; then
    read -r -p "${q} [${default}]: " reply <"$TTY" || true
    printf '%s\n' "${reply:-$default}"
  else
    read -r -p "${q}: " reply <"$TTY" || true
    printf '%s\n' "$reply"
  fi
}

prompt_secret() {
  local q="$1"
  local reply=""
  read -r -s -p "${q}: " reply <"$TTY" || true
  printf '\n' >&2
  printf '%s\n' "$reply"
}

prompt_yes() {
  local q="$1" default="${2:-n}"
  local reply=""
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    [[ "$default" == "y" ]]
    return $?
  fi
  read -r -p "${q} [$([[ "$default" == y ]] && echo Y/n || echo y/N)]: " reply <"$TTY" || true
  reply="$(printf '%s' "${reply:-$default}" | tr '[:upper:]' '[:lower:]')"
  [[ "$reply" == "y" || "$reply" == "yes" ]]
}

write_config_file() {
  local url="$1" user="$2" pass="$3" verify_tls="$4" tg_token="$5" tg_chat="$6"
  mkdir -p "$ETCDIR"
  # Secrets go through the environment, not argv, so they don't show up in `ps`.
  ( umask 077
    WHC_PASSWORD="$pass" WHC_TG_TOKEN="$tg_token" \
      "$PYTHON_BIN" - "$CONFIG_FILE" "$url" "$user" "$verify_tls" "$tg_chat" <<'PY'
import json
import os
import pathlib
import sys

path, url, user, verify, tg_chat = sys.argv[1:]
password = os.environ["WHC_PASSWORD"]
tg_token = os.environ["WHC_TG_TOKEN"]
verify_tls = verify.lower() in {"1", "true", "yes", "y"}
text = f"""panel:
  base_url: {json.dumps(url)}
  username: {json.dumps(user)}
  password: {json.dumps(password)}
  verify_tls: {str(verify_tls).lower()}

check:
  interval_seconds: 10
  timeout_seconds: 8
  fail_threshold: 3
  restart_cooldown_seconds: 90
  max_restarts_per_hour: 6
  stale_after_seconds: 20
  outbound_tag: warp

core_ids: []

# Optional alerts on restart / recovery / hourly cap. Leave empty to disable.
telegram:
  bot_token: {json.dumps(tg_token)}
  chat_id: {json.dumps(tg_chat)}
  proxy: ""
"""
pathlib.Path(path).write_text(text, encoding="utf-8")
PY
  )
  chmod 600 "$CONFIG_FILE"
}

configure() {
  mkdir -p "$ETCDIR"
  if [[ -f "$CONFIG_FILE" && "$FORCE_CONFIG" -ne 1 ]]; then
    log "Keeping existing ${CONFIG_FILE}"
    chmod 600 "$CONFIG_FILE" || true
    return 0
  fi

  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    [[ -n "$BASE_URL" && -n "$USERNAME" && -n "$PASSWORD" ]] || \
      die "Non-interactive install needs --base-url, --username, and --password (or env vars)"
  else
    log "Panel credentials (stored in ${CONFIG_FILE})"
    BASE_URL="$(prompt "Panel URL" "${BASE_URL:-https://127.0.0.1:8000}")"
    USERNAME="$(prompt "Admin username" "${USERNAME:-admin}")"
    if [[ -z "$PASSWORD" ]]; then
      PASSWORD="$(prompt_secret "Admin password")"
    fi
    if [[ "$NO_TLS_VERIFY" -ne 1 ]]; then
      if prompt_yes "Verify TLS" "y"; then
        NO_TLS_VERIFY=0
      else
        NO_TLS_VERIFY=1
      fi
    fi
    if [[ -z "$TELEGRAM_TOKEN" ]] && prompt_yes "Send Telegram alerts when a core is restarted?" "n"; then
      TELEGRAM_TOKEN="$(prompt_secret "Telegram bot token (from @BotFather)")"
      TELEGRAM_CHAT_ID="$(prompt "Telegram chat id" "$TELEGRAM_CHAT_ID")"
    fi
  fi

  [[ -n "$BASE_URL" && -n "$USERNAME" && -n "$PASSWORD" ]] || die "Panel URL, username, and password are required"
  local verify="true"
  [[ "$NO_TLS_VERIFY" -eq 1 ]] && verify="false"
  write_config_file "$BASE_URL" "$USERNAME" "$PASSWORD" "$verify" "$TELEGRAM_TOKEN" "$TELEGRAM_CHAT_ID"
  log "Wrote ${CONFIG_FILE}"
}

run_doctor() {
  [[ "$SKIP_DOCTOR" -eq 1 ]] && return 0
  log "Running doctor"
  if "${VENV_DIR}/bin/warp-healthcheck" --config "$CONFIG_FILE" doctor; then
    return 0
  fi
  warn "doctor failed — check URL, credentials, Observatory, and node connectivity"
  return 1
}

maybe_setup() {
  if [[ "$RUN_SETUP" -eq 1 ]]; then
    log "Adding Observatory to Warp cores"
    "${VENV_DIR}/bin/warp-healthcheck" --config "$CONFIG_FILE" setup -y
    return 0
  fi
  if [[ "$SKIP_SETUP" -eq 1 && "$NON_INTERACTIVE" -eq 1 ]]; then
    return 0
  fi
  if [[ "$NON_INTERACTIVE" -eq 0 ]] && prompt_yes "Add Observatory to Warp cores now? (restarts those cores)" "n"; then
    "${VENV_DIR}/bin/warp-healthcheck" --config "$CONFIG_FILE" setup -y
  fi
}

start_service() {
  if ! have_cmd systemctl; then
    warn "systemctl not found; start manually: ${VENV_DIR}/bin/warp-healthcheck --config ${CONFIG_FILE} run"
    return 0
  fi
  systemctl daemon-reload
  if [[ "$START_SERVICE" -eq 1 ]]; then
    systemctl enable --now "$SERVICE_NAME"
    systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    systemctl enable "$SERVICE_NAME"
    log "Service installed but not started (--no-start)"
  fi
}

cmd_install() {
  need_root
  resolve_source_dir
  ensure_python
  log "Using $($PYTHON_BIN --version 2>&1)"
  copy_app
  create_venv
  write_wrapper
  write_unit
  configure
  local doctor_failed=0
  if ! run_doctor; then
    doctor_failed=1
    START_SERVICE=0
    warn "Service was installed but not started because doctor failed"
    warn "The live config is ${CONFIG_FILE} — re-running install.sh will not ask for the password again."
  fi
  if [[ "$doctor_failed" -eq 0 ]]; then
    maybe_setup
  fi
  start_service
  cat <<EOF

Installed warp-healthcheck $(installed_version).

  Config:  ${CONFIG_FILE}
  Binary:  ${BIN_LINK}
  Service: ${SERVICE_NAME}.service
  Source:  ${APP_DIR}

  sudo warp-healthcheck            # menu: live status, pings, update, logs...
  warp-healthcheck status          # one-shot status table
  sudo warp-healthcheck update     # update from GitHub

EOF
  if [[ "$doctor_failed" -ne 0 ]]; then
    cat >&2 <<EOF
warning: Fix credentials, then:

  nano ${CONFIG_FILE}
  warp-healthcheck doctor
  warp-healthcheck setup
  systemctl enable --now ${SERVICE_NAME}

Or rewrite config interactively:

  sudo bash install.sh reconfigure
EOF
    return 1
  fi
}

cmd_reconfigure() {
  need_root
  [[ -x "${VENV_DIR}/bin/warp-healthcheck" ]] || die "Not installed. Run: sudo bash install.sh"
  [[ "$NON_INTERACTIVE" -eq 0 || -n "$PASSWORD" ]] || die "reconfigure needs a terminal, or --base-url/--username/--password"
  PYTHON_BIN="${VENV_DIR}/bin/python"
  FORCE_CONFIG=1
  configure
  if ! run_doctor; then
    die "doctor still failing; check username/password against the panel login page"
  fi
  maybe_setup
  START_SERVICE=1
  start_service
  log "Reconfigured and started ${SERVICE_NAME}"
}

# upgrade: install from this checkout (or GitHub if run from the installed copy).
# update:  always fetch the latest from GitHub.
cmd_upgrade() {
  local from_remote="${1:-0}"
  need_root
  if [[ ! -x "${VENV_DIR}/bin/warp-healthcheck" ]]; then
    log "Not installed yet; running a fresh install"
    cmd_install
    return $?
  fi
  local before
  before="$(installed_version)"
  resolve_source_dir "$from_remote"
  ensure_python
  copy_app
  create_venv
  write_wrapper
  write_unit
  if have_cmd systemctl; then
    systemctl daemon-reload
    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
      systemctl restart "$SERVICE_NAME"
    fi
  fi
  log "Updated warp-healthcheck ${before:-?} -> $(installed_version). Config kept at ${CONFIG_FILE}"
}

cmd_uninstall() {
  need_root
  if have_cmd systemctl; then
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  fi
  rm -f "$SERVICE_FILE" "$BIN_LINK"
  have_cmd systemctl && systemctl daemon-reload || true
  rm -rf "$PREFIX" "$STATE_DIR"
  if [[ "$KEEP_CONFIG" -eq 0 ]]; then
    rm -rf "$ETCDIR"
    log "Removed ${ETCDIR}"
  else
    log "Kept ${ETCDIR} (use --purge to delete)"
  fi
  log "Uninstalled warp-healthcheck"
}

parse_args() {
  if [[ $# -gt 0 ]]; then
    case "$1" in
      install|update|upgrade|uninstall|reconfigure)
        ACTION="$1"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
    esac
  fi
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --base-url)
        [[ $# -ge 2 ]] || die "--base-url needs a value"
        BASE_URL="$2"
        shift 2
        ;;
      --username)
        [[ $# -ge 2 ]] || die "--username needs a value"
        USERNAME="$2"
        shift 2
        ;;
      --password)
        [[ $# -ge 2 ]] || die "--password needs a value"
        PASSWORD="$2"
        shift 2
        ;;
      --telegram-token)
        [[ $# -ge 2 ]] || die "--telegram-token needs a value"
        TELEGRAM_TOKEN="$2"
        shift 2
        ;;
      --telegram-chat-id)
        [[ $# -ge 2 ]] || die "--telegram-chat-id needs a value"
        TELEGRAM_CHAT_ID="$2"
        shift 2
        ;;
      --no-tls-verify) NO_TLS_VERIFY=1; shift ;;
      --force-config) FORCE_CONFIG=1; shift ;;
      --setup) RUN_SETUP=1; SKIP_SETUP=0; shift ;;
      --skip-setup) SKIP_SETUP=1; RUN_SETUP=0; shift ;;
      --skip-doctor) SKIP_DOCTOR=1; shift ;;
      --no-start) START_SERVICE=0; shift ;;
      --non-interactive) NON_INTERACTIVE=1; shift ;;
      --purge) KEEP_CONFIG=0; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown argument: $1" ;;
    esac
  done
  # Prompts read /dev/tty, so a piped stdin is fine as long as a terminal exists.
  if ! { true <"$TTY"; } 2>/dev/null; then
    NON_INTERACTIVE=1
  fi
}

parse_args "$@"
case "$ACTION" in
  install) cmd_install ;;
  update) cmd_upgrade 1 ;;
  upgrade) cmd_upgrade 0 ;;
  uninstall) cmd_uninstall ;;
  reconfigure) cmd_reconfigure ;;
  *) die "Unknown action: $ACTION" ;;
esac

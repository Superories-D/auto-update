#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"
INSTALL_TIMER=1
RUN_NOW=0
NON_INTERACTIVE=0
TIMER_BACKEND=auto

usage() {
  cat <<USAGE
Usage: ./setup.sh [--env-file PATH] [--timer-backend auto|user|system] [--no-timer] [--run-now] [--non-interactive]

Installs Python dependencies, writes updater settings to .env, and creates a systemd timer by default.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="$2"
      shift
      ;;
    --timer-backend)
      TIMER_BACKEND="$2"
      shift
      ;;
    --no-timer)
      INSTALL_TIMER=0
      ;;
    --run-now)
      RUN_NOW=1
      ;;
    --non-interactive)
      NON_INTERACTIVE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

case "$TIMER_BACKEND" in
  auto|user|system)
    ;;
  *)
    echo "Invalid --timer-backend: $TIMER_BACKEND" >&2
    usage
    exit 2
    ;;
esac

if [[ "$ENV_FILE" != /* ]]; then
  ENV_FILE="$APP_DIR/$ENV_FILE"
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "setup.sh is intended for Ubuntu/Linux deployment." >&2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required. Install it with: sudo apt install python3 python3-venv" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR/.venv" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi

"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  cp "$APP_DIR/.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE"
fi

set_env_value() {
  local key="$1"
  local value="$2"
  local escaped
  escaped="$(printf '%s' "$value" | sed -e 's/[\/&]/\\&/g')"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s/^${key}=.*/${key}=${escaped}/" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

get_env_value() {
  local key="$1"
  local value
  value="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

ensure_env_default() {
  local key="$1"
  local default_value="$2"
  if ! grep -q "^${key}=" "$ENV_FILE"; then
    set_env_value "$key" "$default_value"
  fi
}

section() {
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    return
  fi
  printf '\n== %s ==\n' "$1"
}

prompt_env() {
  local key="$1"
  local label="$2"
  local fallback="${3:-}"
  local current
  current="$(get_env_value "$key")"
  if [[ -z "$current" && -n "$fallback" ]]; then
    current="$fallback"
  fi
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    return
  fi
  read -r -p "$label [$current]: " answer
  if [[ -n "$answer" ]]; then
    set_env_value "$key" "$answer"
  fi
}

prompt_yes_no() {
  local label="$1"
  local default="$2"
  local answer
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    return 0
  fi
  read -r -p "$label [$default]: " answer
  answer="${answer:-$default}"
  case "${answer,,}" in
    y|yes|true|1|on)
      return 0
      ;;
    n|no|false|0|off)
      return 1
      ;;
    *)
      echo "Invalid answer: $answer" >&2
      exit 2
      ;;
  esac
}

choose_timer_backend() {
  if [[ "$TIMER_BACKEND" != "auto" ]]; then
    printf '%s' "$TIMER_BACKEND"
    return
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    printf 'system'
    return
  fi
  if systemctl --user show-environment >/dev/null 2>&1; then
    printf 'user'
    return
  fi
  if [[ -d /run/systemd/system ]]; then
    printf 'system'
    return
  fi
  printf 'none'
}

install_root_file() {
  local source_file="$1"
  local target_file="$2"
  if [[ "$(id -u)" -eq 0 ]]; then
    install -m 0644 "$source_file" "$target_file"
    return
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to install a system timer as a non-root user." >&2
    exit 1
  fi
  sudo install -m 0644 "$source_file" "$target_file"
}

run_systemctl() {
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl "$@"
    return
  fi
  sudo systemctl "$@"
}

detect_ese_dir() {
  local candidate
  for candidate in "$APP_DIR/../ESE" "$APP_DIR/ESE" "$HOME/ESE"; do
    if [[ -d "$candidate" ]]; then
      (cd "$candidate" && pwd)
      return
    fi
  done
  printf '%s' "$APP_DIR/../ESE"
}

if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
  echo "Configure ESE auto-updater. Press Enter to keep the value in brackets."
fi

ensure_env_default "GIT_PROXY_URL" "socks5h://127.0.0.1:40000"

section "Core"
prompt_env "ESE_DIR" "ESE repo path (blank is allowed; detected path shown)" "$(detect_ese_dir)"
prompt_env "SITE_URL" "Taiko site base URL"
prompt_env "STATE_FILE" "Uploaded state JSON path"

section "Git"
prompt_env "GIT_PULL_ARGS" "git pull arguments"
prompt_env "GIT_PROXY_URL" "Git pull SOCKS proxy URL (blank = direct)"

section "Upload"
if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
  echo "Song uploads are forced to direct connection; they do not use Git proxy or system proxy variables."
fi
prompt_env "AUDIO_EXTENSIONS" "Audio extensions, comma separated"
prompt_env "UPLOAD_ATTEMPTS" "Upload retry attempts"
prompt_env "RETRY_WAIT_SECONDS" "Seconds between upload retries"

section "Resend Email"
prompt_env "RESEND_API_KEY" "Resend API key"
prompt_env "RESEND_FROM" "Sender address, for example ESE Updater <noreply@example.com>"
prompt_env "RESEND_TO" "Recipient email(s), comma separated"
prompt_env "EMAIL_SUBJECT_PREFIX" "Email subject prefix"
prompt_env "EMAIL_MAX_SONGS" "Maximum song names included in email"

section "Schedule"
prompt_env "UPDATE_ON_CALENDAR" "Daily schedule in systemd OnCalendar format"
prompt_env "RANDOMIZED_DELAY_SEC" "Randomized delay seconds"

if [[ "$INSTALL_TIMER" -eq 1 && "$NON_INTERACTIVE" -eq 0 ]]; then
  if ! prompt_yes_no "Install or update the systemd timer?" "Y"; then
    INSTALL_TIMER=0
  fi
fi

if [[ "$RUN_NOW" -eq 0 && "$NON_INTERACTIVE" -eq 0 ]]; then
  if prompt_yes_no "Run updater once after setup?" "n"; then
    RUN_NOW=1
  fi
fi

chmod +x "$APP_DIR/upload.py"

if [[ "$INSTALL_TIMER" -eq 1 ]]; then
  ON_CALENDAR="$(get_env_value "UPDATE_ON_CALENDAR")"
  RANDOMIZED_DELAY="$(get_env_value "RANDOMIZED_DELAY_SEC")"
  ON_CALENDAR="${ON_CALENDAR:-*-*-* 04:00:00}"
  RANDOMIZED_DELAY="${RANDOMIZED_DELAY:-0}"

  BACKEND="$(choose_timer_backend)"
  SERVICE_TMP="$(mktemp)"
  TIMER_TMP="$(mktemp)"
  cleanup_units() {
    rm -f "$SERVICE_TMP" "$TIMER_TMP"
  }
  trap cleanup_units EXIT

  SYSTEM_USER_LINE=""
  if [[ "$BACKEND" == "system" && "$(id -u)" -ne 0 ]]; then
    SYSTEM_USER_LINE="User=$(id -un)"
  fi

  cat > "$SERVICE_TMP" <<SERVICE
[Unit]
Description=ESE auto pull and upload new songs

[Service]
Type=oneshot
$SYSTEM_USER_LINE
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
TimeoutStartSec=infinity
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/upload.py run --env-file $ENV_FILE
SERVICE

  cat > "$TIMER_TMP" <<TIMER
[Unit]
Description=Run ESE auto updater daily

[Timer]
OnCalendar=$ON_CALENDAR
Persistent=true
RandomizedDelaySec=$RANDOMIZED_DELAY

[Install]
WantedBy=timers.target
TIMER

  case "$BACKEND" in
    user)
      USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
      mkdir -p "$USER_SYSTEMD_DIR"
      install -m 0644 "$SERVICE_TMP" "$USER_SYSTEMD_DIR/ese-auto-updater.service"
      install -m 0644 "$TIMER_TMP" "$USER_SYSTEMD_DIR/ese-auto-updater.timer"
      systemctl --user daemon-reload
      systemctl --user enable --now ese-auto-updater.timer
      echo "Installed user timer: ese-auto-updater.timer"
      echo "Check timer: systemctl --user list-timers ese-auto-updater.timer"
      echo "View logs: journalctl --user -u ese-auto-updater.service -n 100 --no-pager"
      ;;
    system)
      if [[ ! -d /run/systemd/system ]]; then
        echo "System systemd is not available. Timer was not installed." >&2
        exit 1
      fi
      install_root_file "$SERVICE_TMP" "/etc/systemd/system/ese-auto-updater.service"
      install_root_file "$TIMER_TMP" "/etc/systemd/system/ese-auto-updater.timer"
      run_systemctl daemon-reload
      run_systemctl enable --now ese-auto-updater.timer
      echo "Installed system timer: ese-auto-updater.timer"
      echo "Check timer: systemctl list-timers ese-auto-updater.timer"
      echo "View logs: journalctl -u ese-auto-updater.service -n 100 --no-pager"
      ;;
    *)
      echo "No usable systemd backend found. Timer was not installed." >&2
      echo "Try running as root, or use: sudo ./setup.sh --timer-backend system" >&2
      exit 1
      ;;
  esac
fi

if [[ "$RUN_NOW" -eq 1 ]]; then
  "$APP_DIR/.venv/bin/python" "$APP_DIR/upload.py" run --env-file "$ENV_FILE"
fi

echo "Done. Manual run:"
echo "  $APP_DIR/.venv/bin/python $APP_DIR/upload.py run --env-file $ENV_FILE"

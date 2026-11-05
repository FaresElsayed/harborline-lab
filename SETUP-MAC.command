#!/bin/bash
# Harborline Lab setup for Mac. Double-click this file, or run: bash SETUP-MAC.command
# Every step checks the machine first and skips what is already done, so running this file again is always safe and
# carries on where it stopped. macOS ships bash 3.2, so nothing newer is used here.

# Started with zsh or sh by mistake: restart under bash. Under Rosetta on Apple Silicon: restart natively.
[ -n "$BASH_VERSION" ] || exec /bin/bash "$0" "$@"
[ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ] && exec arch -arm64 /bin/bash "$0" "$@"
if [ "$(id -u)" = "0" ]; then
  echo "Run this as yourself, not as root or with sudo. It asks for your password when it needs it."
  exit 1
fi
if [ ! -t 0 ] && [ -z "$HL_SETUP_STUBS" ]; then
  # No keyboard attached, for example started from another program. Questions would otherwise loop forever.
  exec </dev/tty 2>/dev/null || { echo "Open Terminal and run this file there."; exit 1; }
fi

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${HL_LAB_ROOT:-$HOME/dev}"
LAB_NAME="harborline-lab"
REPO_NAME="harborline-analytics"
LAB_DIR="$ROOT/$LAB_NAME"
REPO_DIR="$ROOT/$REPO_NAME"
OLD_PROJECT="harborline"
PROJECT="harborline-lab"
OLD_FOLDERS="harborline-simulator harborline-3pl-simulator harborline-data-platform"
STATE_DIR="${HL_STATE_DIR:-$HOME/Library/Logs/HarborlineLab}"
LOG_FILE="$STATE_DIR/setup.log"
ERROR_FILE="$STATE_DIR/setup-error.txt"
UPDATE_DIR="$STATE_DIR/update"
UPDATED_TO_FILE="$STATE_DIR/updated-to.txt"
FILE_LIST="$STATE_DIR/installed-files.txt"
ENV_BACKUP="$STATE_DIR/env.backup"
KEY_NAMES="POSTGRES_PASSWORD HL_APP_PASSWORD HL_ETL_PASSWORD HL_REVIEWER_PASSWORD SF_CLIENT_ID SF_CLIENT_SECRET CARRIER_API_KEY"
OWNED_FOLDERS=".github answer_key brand config db docs handbook setup simulator team tracker web"
STEP_NO=0
STEP_TOTAL=22
DONE_COUNT=0
SKIP_COUNT=0
RETRY_LIST=""
LAST_OUTPUT=""
LAST_ERROR=""
LAST_COMMAND=""
LAST_CODE=0
FAILURE=""
USED_FIXES=""
INSTALLED_THIS_RUN="${INSTALLED_THIS_RUN:-0}"

mkdir -p "$STATE_DIR"
# Leftovers from other tools that would silently point this run at the wrong account or Docker engine.
unset GH_TOKEN GITHUB_TOKEN DOCKER_HOST DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONTEXT

# ---------------------------------------------------------------------------------------------- output
if [ -t 1 ]; then
  C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_OFF=""
fi
log() { printf '%s  %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$*" >>"$LOG_FILE" 2>/dev/null; }
say() { printf '    %s\n' "$*"; log "$*"; }
say_good() { printf '%s    %s%s\n' "$C_GREEN" "$*" "$C_OFF"; log "$*"; }
say_warn() { printf '%s    %s%s\n' "$C_YELLOW" "$*" "$C_OFF"; log "WARN $*"; }

your_turn() {
  # your_turn [--no-wait] line...
  local wait=1
  if [ "$1" = "--no-wait" ]; then wait=0; shift; fi
  printf '\n%s    >>> YOUR TURN%s\n' "$C_YELLOW" "$C_OFF"
  local l
  for l in "$@"; do printf '%s    %s%s\n' "$C_YELLOW" "$l" "$C_OFF"; log "MANUAL $l"; done
  if [ "$wait" = 1 ]; then printf '    Press Enter when that is done: '; read -r _; fi
  printf '\n'
}
ask_text() {
  # ask_text "prompt" "default": the answer lands in ANSWER
  local label="$1"
  if [ -n "$2" ]; then label="$1 [$2]"; fi
  printf '    %s: ' "$label"
  if ! read -r ANSWER && [ -z "$ANSWER" ]; then
    printf '\n    No more input, so setup is stopping here. Run this file again in Terminal to carry on.\n'
    exit 1
  fi
  if [ -z "$ANSWER" ]; then ANSWER="$2"; fi
}

# ---------------------------------------------------------------------------------------------- running things
run() {
  # Runs a program, keeps its output for error reports, returns its exit code.
  LAST_COMMAND="$*"
  log "RUN $*"
  LAST_OUTPUT="$("$@" 2>&1)"
  LAST_CODE=$?
  [ -n "$LAST_OUTPUT" ] && printf '%s\n' "$LAST_OUTPUT" >>"$LOG_FILE" 2>/dev/null
  return "$LAST_CODE"
}
run_live() {
  # Same as run, but shows progress while it works.
  LAST_COMMAND="$*"
  case "$1" in brew|uv) INSTALLED_THIS_RUN=1 ;; esac
  log "RUN $*"
  local tmp
  tmp="$(mktemp)"
  "$@" 2>&1 | while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "$line" >>"$tmp"
    printf '%s      %s%s\n' "$C_DIM" "$line" "$C_OFF"
  done
  LAST_CODE=${PIPESTATUS[0]}
  LAST_OUTPUT="$(cat "$tmp")"
  cat "$tmp" >>"$LOG_FILE" 2>/dev/null
  rm -f "$tmp"
  return "$LAST_CODE"
}
has() { command -v "$1" >/dev/null 2>&1; }
restart_fresh_shell() {
  # A terminal only learns about newly installed programs when it opens. refresh_path covers nearly all of that, but
  # when a step fails right after an install, reloading the login shell once has the same effect as a new window.
  [ "$INSTALLED_THIS_RUN" = "1" ] || return 0
  [ -z "$HL_SETUP_RESTARTED" ] || return 0
  [ -z "$NO_FRESH_SHELL" ] || return 0
  say "Something was just installed, and a fresh shell often sees it when this one cannot. Carrying on in a fresh shell."
  HL_SETUP_RESTARTED=1
  export HL_SETUP_RESTARTED
  exec /bin/zsh -l -c 'exec /bin/bash "$1"' _ "$SOURCE_DIR/$(basename "$0")"
}
refresh_path() {
  local p
  for p in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" "$HOME/.docker/bin" /Applications/Docker.app/Contents/Resources/bin; do
    case ":$PATH:" in *":$p:"*) ;; *) [ -d "$p" ] && PATH="$PATH:$p" ;; esac
  done
  if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
  export PATH
}

# ---------------------------------------------------------------------------------------------- machine facts
ram_gb() { echo $(($(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824)); }
free_gb() {
  local dir="$ROOT"
  while [ ! -d "$dir" ]; do dir="$(dirname "$dir")"; done
  df -g "$dir" 2>/dev/null | awk 'NR==2 {print $4}'
}
# lsof without sudo can't see other users' listeners, so also try connecting.
port_free() { ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1 && ! nc -z -G 2 127.0.0.1 "$1" >/dev/null 2>&1; }
use_system_proxy() {
  # curl ignores the proxy in macOS's network settings.
  local host port
  host="$(scutil --proxy 2>/dev/null | awk '/HTTPSProxy/ {print $3}')"
  port="$(scutil --proxy 2>/dev/null | awk '/HTTPSPort/ {print $3}')"
  if [ -n "$host" ] && [ -z "$https_proxy" ]; then
    https_proxy="http://$host:$port"; http_proxy="$https_proxy"; export https_proxy http_proxy
  fi
}

# ---------------------------------------------------------------------------------------------- settings file
env_path() { echo "$LAB_DIR/.env"; }
env_get() {
  local v=""
  if [ -f "$(env_path)" ]; then
    v="$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$(env_path)" | tail -n 1 | cut -d= -f2- | tr -d '[:space:]')"
  fi
  if [ -n "$v" ]; then echo "$v"; else echo "$2"; fi
}
env_set() {
  local file tmp
  file="$(env_path)"
  tmp="$(mktemp)"
  if grep -qE "^[[:space:]]*$1[[:space:]]*=" "$file"; then
    sed -E "s|^[[:space:]]*$1[[:space:]]*=.*|$1=$2|" "$file" >"$tmp"
  else
    cat "$file" >"$tmp"
    printf '%s=%s\n' "$1" "$2" >>"$tmp"
  fi
  mv "$tmp" "$file"
}
app_port() { env_get HL_APP_PORT 8080; }

# Every password and key is made here, on this machine, once. What is already in .env always wins: Postgres keeps its
# first password in the Docker volume, so a new one would lock the person out of their own data.
new_key() {
  local k
  k="$(openssl rand -hex 24 2>/dev/null)"
  if [ "${#k}" -lt 32 ]; then k="$(od -An -tx1 -N24 /dev/urandom 2>/dev/null | tr -d ' \n')"; fi
  printf '%s' "$k"
}
backup_value() {
  [ -f "$ENV_BACKUP" ] || return 0
  grep -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_BACKUP" | tail -n 1 | cut -d= -f2- | tr -d '[:space:]'
}
keys_complete() {
  local key
  [ -f "$(env_path)" ] || return 1
  for key in $KEY_NAMES; do
    [ -n "$(env_get "$key" "")" ] || return 1
  done
  return 0
}
save_env_backup() {
  # Only a complete file: a damaged .env must never replace the good copy.
  keys_complete || return 0
  cp "$(env_path)" "$ENV_BACKUP" && chmod 600 "$ENV_BACKUP"
}
stop_if_data_would_lock_out() {
  # No password anywhere. A new one is only safe if there is no data for it to lock.
  local volume="${PROJECT}_pgdata"
  if ! run docker volume ls -q --filter "name=$volume"; then
    LAST_ERROR="Docker did not say whether your data exists, so setup did not make new passwords. Run this file again."
    return 1
  fi
  printf '%s\n' "$LAST_OUTPUT" | grep -qxF "$volume" || return 0
  log "STOPPED: .env and its backup are gone and the data volume exists."
  your_turn --no-wait "Your settings file (.env) and its backup are both gone, but your simulator data is still here in Docker." \
    "The data is locked with the password that was in that file. A new password would not open it, so setup stopped here." \
    "Nothing has been changed or deleted. You have two choices:" \
    "  1  Keep your data: put your old .env back in $LAB_DIR, from a backup or another copy, then run this file again." \
    "  2  Start fresh: this deletes all your simulator data and tracker progress. In Docker Desktop, delete the" \
    "     $PROJECT containers under Containers and $volume under Volumes, then run this file again." \
    "Not sure? Choose 1 first, or ask Kevin. Setup will not delete anything by itself."
  exit 1
}
repair_env_file() {
  local key value made=0
  if [ ! -f "$(env_path)" ]; then
    if [ -f "$ENV_BACKUP" ]; then
      cp "$ENV_BACKUP" "$(env_path)" || return 1
      say "Your settings file was missing, so setup restored it from its backup. Your passwords still match your data."
    else
      stop_if_data_would_lock_out || return 1
      cp "$LAB_DIR/.env.example" "$(env_path)" || return 1
    fi
  fi
  chmod 600 "$(env_path)"
  for key in $KEY_NAMES; do
    [ -z "$(env_get "$key" "")" ] || continue
    value="$(backup_value "$key")"
    if [ -z "$value" ]; then
      if [ "$key" = "POSTGRES_PASSWORD" ]; then stop_if_data_would_lock_out || return 1; fi
      value="$(new_key)"
      if [ "${#value}" -lt 32 ]; then LAST_ERROR="Could not make a random key on this Mac."; return 1; fi
      made=$((made + 1))
    fi
    env_set "$key" "$value"
    # The running app has the old value: this sends it through the usual restart, which asks first if a build is running.
    rm -f "${STATE_DIR:?}/app-version.txt"
  done
  if [ "$made" -gt 0 ]; then say "Made $made new passwords and keys, just for this install. Find them on Connections in the Control Center."; fi
  save_env_backup
}
pg_port() { env_get HL_PG_PORT 5432; }
app_url() { echo "http://localhost:$(app_port)"; }

# ---------------------------------------------------------------------------------------------- docker
test_docker() {
  has docker || return 1
  run docker info --format '{{.OSType}}' || return 1
  case "$LAST_OUTPUT" in *linux*) return 0 ;; esac
  return 1
}
wait_docker() {
  local waited=0
  while [ "$waited" -lt "$1" ]; do
    test_docker && return 0
    sleep 5
    waited=$((waited + 5))
    if [ $((waited % 30)) -eq 0 ]; then say "Still waiting for Docker... ($waited s)"; fi
  done
  return 1
}
open_docker() {
  if has docker && docker desktop start --detach >/dev/null 2>&1; then return 0; fi
  open -a Docker >/dev/null 2>&1 || open -a "Docker Desktop" >/dev/null 2>&1
}
start_docker_engine() {
  open_docker
  your_turn --no-wait "Docker Desktop is opening. If it shows an agreement, press Accept." \
    "If it asks for your password, enter it. If it asks you to sign in or fill in a survey, press Skip." \
    "Setup is waiting for it and carries on by itself."
  wait_docker 45 && return 0
  if run docker context ls && printf '%s' "$LAST_OUTPUT" | grep -q desktop-linux; then
    say "Pointing Docker at the Docker Desktop engine."
    run docker context use desktop-linux
  fi
  wait_docker 75 && return 0
  say "Docker has not started. Quitting and reopening it once."
  docker desktop stop >/dev/null 2>&1 || osascript -e 'quit app "Docker"' >/dev/null 2>&1
  sleep 8
  open_docker
  wait_docker 150 && return 0
  run docker version
  LAST_ERROR="Docker Desktop did not start within about 5 minutes, even after reopening it."
  return 1
}
project_containers() {
  # project_containers name [-a]: prints the ids
  docker ps ${2:+-a} -q --filter "label=com.docker.compose.project=$1" 2>/dev/null
}
move_busy_ports() {
  local key start label p
  for spec in "HL_PG_PORT:5432:Postgres" "HL_APP_PORT:8080:the app"; do
    key="${spec%%:*}"; start="${spec#*:}"; label="${start#*:}"; start="${start%%:*}"
    start="$(env_get "$key" "$start")"
    port_free "$start" && continue
    p=$((start + 1))
    while [ "$p" -lt $((start + 50)) ]; do
      if port_free "$p"; then
        env_set "$key" "$p"
        say_warn "Port $start is taken by another program, so $label now uses port $p."
        break
      fi
      p=$((p + 1))
    done
  done
}

# ---------------------------------------------------------------------------------------------- app api
# 127.0.0.1 with no proxy: the app is on this machine, and localhost can try IPv6 first.
api_url() { echo "http://127.0.0.1:$(app_port)$1"; }
api_get() { curl -fsS --noproxy '*' --max-time 20 "$(api_url "$1")" 2>/dev/null; }
api_post() { curl -fsS --noproxy '*' --max-time 20 -X POST -H 'Content-Type: application/json' -d "$2" "$(api_url "$1")" 2>&1; }
api_put() { curl -fsS --noproxy '*' --max-time 20 -X PUT -H 'Content-Type: application/json' -d "$2" "$(api_url "$1")" 2>&1; }
json_field() { printf '%s' "$1" | sed -n "s/.*\"$2\":\"\([^\"]*\)\".*/\1/p" | head -n 1; }
test_health() { api_get /api/health | grep -q '"ok":true'; }
NO_ANSWER_TEXT="The app did not answer for 5 minutes. Nothing was wiped and no build was started."
# A slow answer must never read as "no data yet": a build wipes whatever is there. Fails after five minutes of silence.
get_sim_status() {
  local try=1
  while [ "$try" -le 5 ]; do
    SIM_STATUS="$(curl -fsS --noproxy '*' --max-time 45 "$(api_url /api/sim/status)" 2>/dev/null)"
    [ -n "$SIM_STATUS" ] && return 0
    [ "$try" -eq 1 ] && say "The app is busy. Giving it up to 5 minutes to answer."
    sleep 15
    try=$((try + 1))
  done
  LAST_COMMAND="GET /api/sim/status"
  return 1
}
wait_health() {
  local waited=0 started took
  while [ "$waited" -lt "$1" ]; do
    started=$SECONDS
    test_health && return 0
    sleep 3
    # Real time: a try can spend 20 seconds waiting for an answer before this pause.
    took=$((SECONDS - started))
    if [ "$took" -lt 3 ]; then took=3; fi
    waited=$((waited + took))
  done
  return 1
}

# ---------------------------------------------------------------------------------------------- github
# An API call, not 'gh auth status': that fails if any old account is stale, even when the active one works.
test_gh_auth() { run gh api user --jq .login; }
github_user() { if run gh api user --jq .login; then printf '%s' "$LAST_OUTPUT"; fi; }
test_github_ssh() {
  local user
  user="$(github_user)"
  run ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 git@github.com
  printf '%s' "$LAST_OUTPUT" | grep -q 'successfully authenticated' || return 1
  # "Hi someone-else!" means this key belongs to another account, and pushes would be refused.
  [ -z "$user" ] || printf '%s' "$LAST_OUTPUT" | grep -qF "Hi $user!"
}
test_github_access() {
  has gh || return 1
  test_gh_auth || return 1
  if run gh config get git_protocol --host github.com && [ "$LAST_OUTPUT" = "https" ]; then return 0; fi
  test_github_ssh
}
connect_github() {
  if ! test_gh_auth; then
    your_turn --no-wait "GitHub sign-in. No account yet? Create one first at https://github.com/signup" \
      "A one-time code appears next. Press Enter, sign in on the page that opens, paste the code, press Authorize."
    local clip=""
    if ! gh auth login --help 2>&1 | grep -q -- '--skip-ssh-key'; then
      say "Your GitHub CLI is too old. Updating it."
      run_live brew upgrade gh
    fi
    if gh auth login --help 2>&1 | grep -q -- '--clipboard'; then clip="--clipboard"; fi
    LAST_COMMAND="gh auth login --web"
    # shellcheck disable=SC2086
    if ! gh auth login --hostname github.com --git-protocol ssh --web --skip-ssh-key --scopes admin:public_key $clip; then
      LAST_ERROR="GitHub sign-in did not finish."
      LAST_CODE=1
      return 1
    fi
  fi
  # An SSH key with no passphrase is the one-go choice. An existing key is kept as it is.
  local key="$HOME/.ssh/id_ed25519"
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  if [ ! -f "$key" ]; then
    local email
    email="$(git config --global user.email)"
    say "Creating an SSH key for GitHub."
    run ssh-keygen -t ed25519 -C "${email:-harborline}" -f "$key" -q -N ""
  fi
  local title
  title="harborline-$(hostname -s)"
  if ! run gh ssh-key add "$key.pub" --title "$title"; then
    case "$LAST_OUTPUT" in
      *admin:public_key*|*scope*)
        your_turn --no-wait "GitHub needs one more permission. Press Enter, then approve it on the page that opens."
        gh auth refresh --hostname github.com --scopes admin:public_key
        run gh ssh-key add "$key.pub" --title "$title"
        ;;
    esac
  fi
  test_github_ssh && return 0
  case "$LAST_OUTPUT" in
    *"IDENTIFICATION HAS CHANGED"*|*"Host key verification failed"*)
      say "GitHub's saved host key is out of date. Refreshing it."
      run ssh-keygen -R github.com
      test_github_ssh && return 0
      ;;
    *"Bad owner or permissions"*)
      say "Repairing the permissions on your SSH settings."
      chmod 700 "$HOME/.ssh" 2>/dev/null
      chmod 600 "$HOME/.ssh/config" 2>/dev/null
      test_github_ssh && return 0
      ;;
  esac
  case "$LAST_OUTPUT" in
    *"port 22"*|*"timed out"*|*refused*|*"Could not resolve"*|*unreachable*)
      say "Your network blocks the usual SSH port. Using GitHub's port 443 instead."
      if ! grep -q 'ssh.github.com' "$HOME/.ssh/config" 2>/dev/null; then
        printf '\nHost github.com\n  Hostname ssh.github.com\n  Port 443\n  User git\n' >>"$HOME/.ssh/config"
      fi
      test_github_ssh && return 0
      ;;
  esac
  # Last resort that always works if GitHub is reachable at all: HTTPS through gh's own login.
  say_warn "SSH to GitHub is not working here, so Git will use HTTPS through your GitHub login instead."
  run gh config set git_protocol https --host github.com
  run gh auth setup-git
}
test_repo_ready() {
  [ -d "$REPO_DIR/.git" ] || return 1
  run git -C "$REPO_DIR" remote get-url origin || return 1
  case "$LAST_OUTPUT" in *"$REPO_NAME"*) ;; *) return 1 ;; esac
  run git -C "$REPO_DIR" rev-parse --verify origin/main
}
test_repo_hooks() { [ -f "$REPO_DIR/.git/hooks/pre-commit" ]; }
copy_repo_template() {
  local template="$LAB_DIR/setup/repo-template" rel
  (cd "$template" && find . -type f) | while IFS= read -r rel; do
    rel="${rel#./}"
    if [ ! -e "$REPO_DIR/$rel" ]; then
      mkdir -p "$(dirname "$REPO_DIR/$rel")"
      cp "$template/$rel" "$REPO_DIR/$rel"
    fi
  done
}
new_analytics_repo() {
  local user full on_github=1 url use_https=1 has_remote=1 has_commit=1 id
  user="$(github_user)"
  if [ -z "$user" ]; then LAST_ERROR="Could not read your GitHub username."; return 1; fi
  full="$user/$REPO_NAME"
  run gh repo view "$full" --json name && on_github=0
  if run gh config get git_protocol --host github.com && [ "$LAST_OUTPUT" = "https" ]; then use_https=0; fi
  mkdir -p "$REPO_DIR"
  # Harmless here, and needed if something else created this folder.
  run git config --global --add safe.directory "$REPO_DIR"
  if [ "$on_github" = 0 ] && [ -z "$(ls -A "$REPO_DIR")" ]; then
    say "$full is already on GitHub. Cloning it."
    run gh repo clone "$full" "$REPO_DIR" || return 1
  fi
  if [ ! -d "$REPO_DIR/.git" ]; then
    if ! run git -C "$REPO_DIR" init -b main; then
      run git -C "$REPO_DIR" init || return 1
      run git -C "$REPO_DIR" symbolic-ref HEAD refs/heads/main
    fi
  fi
  if [ "$on_github" = 0 ] && ! run git -C "$REPO_DIR" remote get-url origin; then
    url="https://github.com/$full.git"
    if [ "$use_https" != 0 ]; then run gh repo view "$full" --json sshUrl --jq .sshUrl; url="$LAST_OUTPUT"; fi
    run git -C "$REPO_DIR" remote add origin "$url" || return 1
  fi
  if run git -C "$REPO_DIR" remote get-url origin; then
    case "$use_https:$LAST_OUTPUT" in 0:git@*) run git -C "$REPO_DIR" remote set-url origin "https://github.com/$full.git" ;; esac
    run git -C "$REPO_DIR" fetch origin
  fi
  run git -C "$REPO_DIR" rev-parse --verify origin/main && has_remote=0
  run git -C "$REPO_DIR" rev-parse --verify HEAD && has_commit=0
  if [ "$has_remote" = 0 ] && [ "$has_commit" != 0 ]; then
    # Files already in the folder stay as they are; this only adopts GitHub's history.
    run git -C "$REPO_DIR" reset origin/main
    return 0
  fi
  if [ "$has_remote" = 0 ]; then
    if ! run git -C "$REPO_DIR" merge-base HEAD origin/main; then
      LAST_OUTPUT="unrelated histories: this folder and $full on GitHub have different commits."
      LAST_ERROR="Your folder and the GitHub repository disagree, and setup will not overwrite either."
      return 1
    fi
    return 0
  fi
  copy_repo_template
  if [ "$has_commit" != 0 ]; then
    run git -C "$REPO_DIR" add -A
    run git -C "$REPO_DIR" -c commit.gpgsign=false commit -m "chore: repository skeleton" || return 1
  fi
  if ! run git -C "$REPO_DIR" remote get-url origin; then
    say "Creating $full on GitHub (public, so branch protection works on a free account)."
    run gh repo create "$REPO_NAME" --public --source "$REPO_DIR" --remote origin \
      --description "Data platform and dashboards for Harborline Logistics (practice program)" || return 1
  fi
  # A leftover from an earlier attempt may be on 'master'.
  run git -C "$REPO_DIR" branch -M main
  if ! run git -C "$REPO_DIR" push -u origin main; then
    case "$LAST_OUTPUT" in
      *GH007*|*"email privacy"*)
        say "GitHub keeps your email private. Using your GitHub no-reply address for this repository."
        run gh api user --jq .id
        id="$LAST_OUTPUT"
        run git -C "$REPO_DIR" config user.email "$id+$user@users.noreply.github.com"
        run git -C "$REPO_DIR" -c commit.gpgsign=false commit --amend --reset-author --no-edit
        run git -C "$REPO_DIR" push -u origin main || return 1
        ;;
      *) return 1 ;;
    esac
  fi
}
fix_repo_hooks() {
  say "Installing the commit checks (a few minutes the first time)."
  LAST_COMMAND="pre-commit install --install-hooks"
  LAST_OUTPUT="$(cd "$REPO_DIR" && pre-commit install --install-hooks 2>&1)"
  LAST_CODE=$?
  printf '%s\n' "$LAST_OUTPUT" >>"$LOG_FILE" 2>/dev/null
  [ "$LAST_CODE" = 0 ] && return 0
  case "$LAST_OUTPUT" in
    *"Cowardly refusing"*)
      # A setting in this repository alone is safe to clear; a global one may be company policy.
      if git -C "$REPO_DIR" config --local --unset-all core.hooksPath 2>/dev/null; then
        LAST_OUTPUT="$(cd "$REPO_DIR" && pre-commit install --install-hooks 2>&1)"
        LAST_CODE=$?
      fi
      ;;
  esac
}
test_repo_protected() {
  local user
  user="$(github_user)"
  [ -n "$user" ] || return 1
  run gh api "repos/$user/$REPO_NAME/rulesets" --jq '.[].name' || return 1
  case "$LAST_OUTPUT" in *"protect main"*) return 0 ;; esac
  return 1
}

# ---------------------------------------------------------------------------------------------- dbeaver
dbeaver_app() {
  local a
  for a in /Applications/DBeaver.app "$HOME/Applications/DBeaver.app"; do
    if [ -d "$a" ]; then echo "$a"; return 0; fi
  done
  return 1
}
dbeaver_sources() { echo "$HOME/Library/DBeaverData/workspace6/General/.dbeaver/data-sources.json"; }
test_dbeaver_connections() {
  dbeaver_app >/dev/null || return 1
  grep -qs "Harborline WMS" "$(dbeaver_sources)"
}
add_dbeaver_connections() {
  # One connection per system, so an editor can never be on the wrong database or on the sample database.
  # One launch: a second copy starting before the first has finished would find the workspace locked.
  local app bin pw port waited=0 common
  app="$(dbeaver_app)" || { LAST_ERROR="DBeaver was not found."; return 1; }
  bin="$app/Contents/MacOS/dbeaver"
  pw="$(env_get HL_ETL_PASSWORD "")"
  port="$(pg_port)"
  common="driver=postgresql|host=localhost|port=$port|user=etl_reader|password=$pw|savePassword=true|create=true|save=true|connect=false|openConsole=false"
  your_turn --no-wait "DBeaver is opening. If it offers to create a sample database, press No."
  "$bin" -nosplash \
    -con "$common|database=wms|name=Harborline WMS" \
    -con "$common|database=ax2012|name=Harborline AX 2012" \
    -con "$common|database=iot|name=Harborline IoT" \
    -con "$common|database=tracker|name=Harborline Tracker" >/dev/null 2>&1 &
  while [ "$waited" -lt 60 ]; do
    test_dbeaver_connections && return 0
    sleep 5
    waited=$((waited + 5))
  done
  LAST_OUTPUT="DBeaver did not save the connections within a minute. If it is showing a dialog, answer it, then run this file again."
  return 1
}

# ---------------------------------------------------------------------------------------------- team chats
# No AI site lets a script create chats: he signs in and sends each first message, the setup file does the rest.
open_url() {
  if [ -n "$HL_SETUP_NO_OPEN" ]; then return 0; fi
  open "$1" >/dev/null 2>&1 || say "Open this in your browser: $1"
}
team_index() { echo "$LAB_DIR/team/prompts/index.tsv"; }
count_missing_chats() {
  # Prints how many colleagues have no saved chat yet.
  local saved id rest missing=0
  saved="$(api_get /api/handbook/team-links)"
  while IFS="$(printf '\t')" read -r id rest; do
    case "$saved" in *"\"$id\":\"http"*) ;; *) missing=$((missing + 1)) ;; esac
  done <"$(team_index)"
  echo "$missing"
}
check_team_chats() {
  test_health || return 1
  [ "$(count_missing_chats)" = "0" ]
}
chat_link_problem() {
  # Prints what is wrong with the address, or nothing.
  case "$1" in
    https://*) ;;
    *) echo "That is not a web address. It should start with https://"; return ;;
  esac
  case "$1" in
    *" "*|*\"*) echo "That is not a web address. It should start with https://"; return ;;
    *[?\&]temporary-chat*|*[?\&]temporary=*|*[?\&]incognito*) echo "That is a temporary chat, which cannot be reopened. Start a normal chat instead."; return ;;
  esac
  if printf '%s' "$1" | grep -qE '^https://[^/?#]+(/(new|app|chat|chats|c)?/?)?([?#].*)?$'; then
    echo "That is the site's start page, not a chat. Send the first message, wait for the reply, then copy the address again."
  fi
}
load_ai_site() {
  # Any AI website works. Asked once, then remembered here. Sets SITE_NAME and SITE_URL.
  local file="$STATE_DIR/ai-site.txt" choice
  if [ -f "$file" ]; then
    SITE_NAME="$(cut -d'|' -f1 "$file")"
    SITE_URL="$(cut -d'|' -f2- "$file")"
    return 0
  fi
  your_turn --no-wait "Your colleagues can live on any AI website. Pick the one you use:" \
    "  1  ChatGPT    2  Claude    3  Gemini    4  Microsoft Copilot    5  Another site"
  ANSWER=""
  until printf '%s' "$ANSWER" | grep -qE '^[1-5]$'; do ask_text "Your choice" "1"; done
  choice="$ANSWER"
  case "$choice" in
    1) SITE_NAME="ChatGPT"; SITE_URL="https://chatgpt.com/" ;;
    2) SITE_NAME="Claude"; SITE_URL="https://claude.ai/new" ;;
    3) SITE_NAME="Gemini"; SITE_URL="https://gemini.google.com/app" ;;
    4) SITE_NAME="Microsoft Copilot"; SITE_URL="https://copilot.microsoft.com/" ;;
    5)
      ANSWER=""
      until printf '%s' "$ANSWER" | grep -qE '^https://[^[:space:]"|]+$'; do ask_text "The address where a new chat starts (https://...)" ""; done
      SITE_URL="$ANSWER"
      SITE_NAME="$(printf '%s' "$ANSWER" | sed -E 's#^https://(www\.)?([^/]+).*$#\2#')"
      ;;
  esac
  printf '%s|%s\n' "$SITE_NAME" "$SITE_URL" >"$file"
  api_put /api/handbook/team-site "{\"name\":\"$SITE_NAME\",\"url\":\"$SITE_URL\"}" >/dev/null
}
fix_team_chats() {
  local zip="$ROOT/harborline-chat.zip" source="$LAB_DIR/setup/harborline-chat.zip" hello saved total n=0 problem
  local id name title needs_zip item tab
  tab="$(printf '\t')"
  hello="Hi, I just joined. Say hello in one line; I will come back with questions."
  if [ -f "$source" ]; then
    cp "$source" "$zip"
    if [ -z "$HL_SETUP_NO_OPEN" ]; then open -R "$zip" >/dev/null 2>&1; fi
  fi
  load_ai_site
  if [ ! -f "$STATE_DIR/ai-memory.done" ]; then
    if [ "$SITE_NAME" = "ChatGPT" ]; then
      open_url "https://chatgpt.com/#settings/Personalization"
      your_turn "ChatGPT is opening. Sign in if it asks." \
        "In Settings > Personalization, turn off saved memories and chat history, so colleagues cannot read each other's chats."
    else
      open_url "$SITE_URL"
      your_turn "$SITE_NAME is opening. Sign in if it asks." \
        "If this site has a memory that works across chats, turn it off in its settings, so colleagues cannot read each other's chats."
    fi
    date +%Y-%m-%dT%H:%M:%S >"$STATE_DIR/ai-memory.done"
  fi
  saved="$(api_get /api/handbook/team-links)"
  total="$(count_missing_chats)"
  your_turn --no-wait "Now one chat per colleague: $total to go, about a minute each. You can stop at any point and carry on later." \
    "IMPORTANT: send harborline-chat.zip WITH the prompt, in the same first message, every time." \
    "The zip holds the handbook, tickets and brand kit. Without it your colleague can't see the company and will guess." \
    "A Finder window shows the zip: keep it open, you drag it into each chat." \
    "Only if $SITE_NAME refuses the zip: send the prompt alone, then attach single files when a question needs them."
  while IFS="$tab" read -r id name title needs_zip item <&3; do
    case "$saved" in *"\"$id\":\"http"*) continue ;; esac
    n=$((n + 1))
    if [ "$needs_zip" = "yes" ]; then
      hello="Hi, I just joined and attached the Harborline zip. Say hello in one line and tell me if you can open it; I will come back with questions."
    else
      hello="Hi, I just joined. Say hello in one line; I will come back with questions."
    fi
    { cat "$LAB_DIR/team/prompts/$id.txt"; printf '\n\n%s\n' "$hello"; } | LC_CTYPE=UTF-8 pbcopy
    open_url "$SITE_URL"
    printf '\n%s    [%s/%s] %s, %s%s\n' "$C_YELLOW" "$n" "$total" "$name" "$title" "$C_OFF"
    if [ "$needs_zip" = "yes" ]; then
      printf '%s    In the new tab, all in ONE message:%s\n' "$C_YELLOW" "$C_OFF"
      printf '%s      1. Drag in harborline-chat.zip and wait until it finishes uploading.%s\n' "$C_YELLOW" "$C_OFF"
      printf '%s      2. Press Cmd+V to paste the prompt.%s\n' "$C_YELLOW" "$C_OFF"
      printf '%s      3. Press Return.%s\n' "$C_YELLOW" "$C_OFF"
    else
      printf '%s    In the new tab: press Cmd+V, press Return. This one does not need the zip.%s\n' "$C_YELLOW" "$C_OFF"
    fi
    printf '%s    When it replies, copy the address from the top of the browser and paste it here.%s\n' "$C_YELLOW" "$C_OFF"
    while :; do
      ask_text "Chat address (Return skips this person, later stops for now)" ""
      if [ -z "$ANSWER" ]; then say "Skipped."; break; fi
      case "$ANSWER" in later|stop|q|quit) say "Stopping here. Run this file again to carry on."; exec 3<&-; return 0 ;; esac
      problem="$(chat_link_problem "$ANSWER")"
      if [ -n "$problem" ]; then say_warn "$problem"; continue; fi
      if api_put "/api/handbook/team-links/$id" "{\"url\":\"$ANSWER\",\"item\":\"$item\"}" >/dev/null; then say_good "Saved."; else say_warn "Could not save that. Run this file again to retry."; fi
      break
    done
  done 3<"$(team_index)"
}

# ---------------------------------------------------------------------------------------------- errors
# A pattern seen in the output -> what to try. Each fix runs at most once per step, so nothing can loop.
failure_has() { printf '%s' "$FAILURE" | grep -qEi "$1"; }
own_app_failed() { case "$FAILED_COMMAND" in "GET /api/"*|"POST /api/"*|"PUT /api/"*) return 0 ;; esac; return 1; }
try_fix() {
  case " $USED_FIXES " in *" $1 "*) return 1 ;; esac
  USED_FIXES="$USED_FIXES $1"
  say "Known problem ($1). Fixing it and trying again."
  log "HEAL $1"
  return 0
}
NETWORK_ERRORS='TLS handshake|i/o timeout|timed out|Temporary failure|connection reset|unexpected EOF|Service Unavailable|Bad Gateway|no such host|dial tcp|ECONNRESET|ETIMEDOUT|EAI_AGAIN|Could not resolve|Failed to connect to|SSL_ERROR_SYSCALL'
known_fix() {
  # Returns 0 when a fix was run.
  if failure_has 'cannot write to the landing folder|Permission denied: .*/data/landing' && try_fix landing-folder; then
    say "The app cannot write to the landing folder. Restarting it, which repairs the folder."
    (cd "$LAB_DIR" && docker compose restart app) >>"$LOG_FILE" 2>&1 </dev/null
    wait_health 120
    return 0
  fi
  # A slow answer from the app on this machine is not a network problem, and a retry would only repeat the call.
  if own_app_failed; then return 1; fi
  if failure_has 'Cannot connect to the Docker daemon|failed to connect to the docker API|dockerDesktopLinuxEngine|docker\.sock' && try_fix docker-engine; then
    start_docker_engine; return 0
  fi
  if failure_has 'port is already allocated|address already in use|ports are not available' && try_fix busy-ports; then
    (cd "$LAB_DIR" && docker compose down) >>"$LOG_FILE" 2>&1
    move_busy_ports
    return 0
  fi
  if failure_has 'error getting credentials|docker-credential-desktop' && try_fix docker-credentials; then
    if [ -f "$HOME/.docker/config.json" ]; then
      say "Docker's saved-login helper is missing. Turning it off (a backup is kept next to the file)."
      sed -i.bak 's/"credsStore"/"credStore"/' "$HOME/.docker/config.json"
    fi
    return 0
  fi
  if failure_has 'no space left on device' && try_fix disk-space; then
    say "Docker is out of disk space. Clearing its build cache, which only costs time."
    run docker builder prune -f
    return 0
  fi
  if failure_has 'dependency failed to start|is unhealthy' && try_fix slow-database; then
    say "Postgres is taking longer than expected. Waiting a minute and trying again."
    sleep 60
    return 0
  fi
  if failure_has 'HTTP 401|Bad credentials|sign-in did not finish|failed to authenticate|slow_down|expired_token|token_expired' && try_fix github-login; then
    run gh auth logout --hostname github.com
    connect_github
    return 0
  fi
  if failure_has 'dubious ownership' && try_fix git-ownership; then
    run git config --global --add safe.directory "$REPO_DIR"
    return 0
  fi
  if failure_has 'invalid peer certificate|UnknownIssuer' && try_fix uv-certificates; then
    say "Your network inspects secure traffic. Telling uv to use the system's certificates."
    UV_SYSTEM_CERTS=true; export UV_SYSTEM_CERTS
    return 0
  fi
  if failure_has 'Executable already exists' && try_fix uv-overwrite; then
    run uv tool install pre-commit --force
    return 0
  fi
  if failure_has 'CERTIFICATE_VERIFY_FAILED|go\.dev/dl' && try_fix go-for-commit-checks; then
    run_live brew install go
    return 0
  fi
  if failure_has 'not compatible with the current version' && try_fix old-vscode; then
    run_live brew upgrade --cask visual-studio-code
    return 0
  fi
  if failure_has 'agreed to the Xcode license' && try_fix xcode-license; then
    say "Xcode's license needs accepting. It asks for your Mac password."
    sudo xcodebuild -license accept
    return 0
  fi
  if failure_has 'invalid active developer path|Command Line Tools are too outdated' && try_fix command-line-tools; then
    fix_clt
    return 0
  fi
  if failure_has 'Fetching .* failed|Failed to download|No available formula|No Cask with this name|Error: Cask' && try_fix homebrew; then
    say "Refreshing Homebrew's package list."
    HOMEBREW_CURL_RETRIES=5; export HOMEBREW_CURL_RETRIES
    use_system_proxy
    run brew update
    return 0
  fi
  if failure_has "$NETWORK_ERRORS" && try_fix network; then
    say "That looks like a network hiccup. Waiting 20 seconds and trying again."
    use_system_proxy
    sleep 20
    return 0
  fi
  if failure_has "$NETWORK_ERRORS" && try_fix network-second-try; then
    say "Still a network problem. Waiting a minute and trying once more."
    sleep 60
    return 0
  fi
  return 1
}
known_hint() {
  # Problems only a person can solve: pattern -> what to do. handbook/known-issues.html has them all.
  local text="$1" pattern advice
  while IFS='~' read -r pattern advice; do
    [ -n "$pattern" ] || continue
    if printf '%s' "$text" | grep -qEi "$pattern"; then printf '%s' "$advice"; return 0; fi
  done <<'HINTS'
cannot write to the landing folder|Permission denied: .*/data/landing~The app still cannot write to the landing folder after a restart. Check your security software, then run this file again. Kevin can read this report.
did not answer|stopped answering~The app is busy or stuck. Your data is safe. Wait a few minutes and run this file again.
command not found~A terminal only sees programs installed before it opened. Close this window, open a new one, and run this file again.
Operation not permitted~macOS is blocking Terminal from this folder. System Settings > Privacy & Security > Files & Folders > Terminal: allow it, or move the extracted folder to your home folder.
not currently available from the Software Update server~Apple's server has no Command Line Tools for this Mac right now. Download them from developer.apple.com/download/all, then run this file again.
Insufficient permissions to install Homebrew|Need sudo access|not in the sudoers~Homebrew needs an administrator account. Log in as one, or ask whoever manages this Mac.
only supported on Apple Silicon~Homebrew's installer no longer supports Intel Macs. Install Git, gh, uv, Docker Desktop, VS Code and DBeaver from their own websites, then run this file again.
This installation may not succeed|do not provide support for this old version~This macOS is too old for Homebrew's support. Update macOS if you can.
is not writable|Permission denied @ dir_s_mkdir~Homebrew's folder belongs to another account. On your own Mac, run: sudo chown -R "$(whoami)" "$(brew --prefix)". On a shared Mac, ask first.
Resource deadlock avoided~The folder is in iCloud and some files are not on this Mac yet. Extract the zip outside iCloud, for example in your home folder.
toomanyrequests|429 Too Many|pull rate limit~Docker Hub limits downloads to 100 per 6 hours per network. Wait, change network, or run: docker login
x509: certificate|SSL certificate problem|CERTIFICATE_VERIFY_FAILED|invalid peer certificate|UnknownIssuer~Your network inspects secure traffic. Ask IT for its root certificate, or try another network.
failed to fetch oauth token|failed to authorize~A stale Docker Hub login. Run: docker logout, then run this file again.
Sign in enforced~Your organisation requires a Docker sign-in. Sign in to Docker Desktop with your work account, or ask IT.
no space left on device~The disk is full. Free some space, or raise Docker Desktop > Settings > Resources > Disk usage limit.
exit code: 137|OOMKilled|terminated by signal 9~It ran out of memory. Close other apps, raise Docker Desktop > Settings > Resources > Memory, or build a smaller data set in the Control Center.
dependency failed to start|is unhealthy~Postgres did not become healthy. Its log is in this report: Kevin can read it.
env_file|'compose' is not a docker command~Docker Desktop is too old. Run: brew upgrade --cask docker-desktop
Docker.app is damaged~Run: brew update, then brew reinstall --cask docker-desktop
Mounts denied|is not shared from the host~Docker Desktop > Settings > Resources > File sharing: add this folder.
certificate has expired or is not yet valid~Docker's clock drifted after the Mac slept. Restart Docker Desktop.
verify your email|email.*verif~Verify your email at github.com/settings/emails, then run this file again.
Upgrade to GitHub Pro~Branch protection needs a public repository on a free account. Make it public, or skip this: nothing else depends on it.
fetch first|non-fast-forward|Updates were rejected|unrelated histories~Your folder and the GitHub repository have different commits. Ask Kevin. Do not force-push.
Cowardly refusing~A global Git setting (core.hooksPath) blocks the commit checks. See it with: git config --show-origin --get-all core.hooksPath. It may be company policy, so ask before removing it.
Push cannot contain secrets~GitHub found a secret in the commit. Remove that file, then: git commit --amend --all
Changes must be made through a pull request~main is protected. Push a branch and open a pull request instead.
invalid format~Your SSH key file is damaged. Do not overwrite it blindly: ask Kevin.
XHR failed|Failed Installing Extensions|Error while installing extensions~VS Code cannot reach its Marketplace. Allow marketplace.visualstudio.com and *.vsassets.io, or install the extensions from inside VS Code.
python-build-standalone~uv cannot download Python, usually a proxy. Set HTTPS_PROXY, or try another network.
Can't lock workspace~DBeaver is already starting. In its dialog press Abort, never Ignore, then run this file again.
HINTS
  return 1
}
stop_with_error() {
  local report
  report="$(
    printf 'Harborline Lab setup stopped at: %s\n' "$1"
    printf 'Time: %s\n' "$(date +%Y-%m-%dT%H:%M:%S)"
    printf 'Command: %s\n' "$LAST_COMMAND"
    printf 'Exit code: %s\n' "$LAST_CODE"
    [ -n "$LAST_ERROR" ] && printf 'Message: %s\n' "$LAST_ERROR"
    hint="$(known_hint "$FAILURE")"
    [ -n "$hint" ] && printf 'What this usually means: %s\n' "$hint"
    printf '\nOutput:\n'
    printf '%s\n' "$FAILURE" | tail -n 40
    printf '\nMachine: Mac setup file, macOS %s. Setup skips finished steps when it runs again.\n' "$(sw_vers -productVersion 2>/dev/null)"
  )"
  printf '%s\n' "$report" >"$ERROR_FILE"
  local copied="no"
  if has pbcopy && printf '%s\n' "$report" | LC_CTYPE=UTF-8 pbcopy 2>/dev/null; then copied="yes"; fi
  printf '\n%s    ============================== SETUP STOPPED ==============================%s\n' "$C_RED" "$C_OFF"
  printf '%s\n' "$report" | while IFS= read -r line; do printf '%s    %s%s\n' "$C_RED" "$line" "$C_OFF"; done
  printf '%s    ===========================================================================%s\n\n' "$C_RED" "$C_OFF"
  if [ "$copied" = "yes" ]; then
    printf '%s    What to do (this report is already on your clipboard):%s\n' "$C_YELLOW" "$C_OFF"
  else
    printf '%s    What to do (copy the report above, or the file named below):%s\n' "$C_YELLOW" "$C_OFF"
  fi
  printf '%s    1. Paste it into your chat with Kevin O'"'"'Hara (IT help), or any AI chat. His prompt is in handbook/team.html.%s\n' "$C_YELLOW" "$C_OFF"
  printf '%s    2. Do what he says.%s\n' "$C_YELLOW" "$C_OFF"
  printf '%s    3. Run this file again. Everything finished so far is skipped.%s\n' "$C_YELLOW" "$C_OFF"
  printf '%s    The report is also saved at %s%s\n' "$C_YELLOW" "$ERROR_FILE" "$C_OFF"
  printf '%s    Every known error is listed in handbook/known-issues.html, with what to do.%s\n' "$C_YELLOW" "$C_OFF"
  exit 1
}

# ---------------------------------------------------------------------------------------------- the step runner
step() {
  # step [--optional] "title" check_function fix_function
  local optional=0
  if [ "$1" = "--optional" ]; then optional=1; shift; fi
  local title="$1" check="$2" fix="$3" attempt=1
  STEP_NO=$((STEP_NO + 1))
  printf '\n%s[%s/%s] %s%s\n' "$C_CYAN" "$STEP_NO" "$STEP_TOTAL" "$title" "$C_OFF"
  log "STEP $title"
  if "$check"; then
    say_good "${PASS_TEXT:-Already done.}"
    SKIP_COUNT=$((SKIP_COUNT + 1))
    return 0
  fi
  USED_FIXES=""
  while [ "$attempt" -le 4 ]; do
    LAST_ERROR=""
    LAST_OUTPUT=""
    "$fix"
    # Saved now: the check below runs commands of its own, and the report must show the one that failed.
    FAILURE="$LAST_OUTPUT
$LAST_ERROR"
    FAILED_COMMAND="$LAST_COMMAND"
    FAILED_CODE="$LAST_CODE"
    FAILED_MESSAGE="$LAST_ERROR"
    refresh_path
    if "$check"; then
      say_good "Done."
      DONE_COUNT=$((DONE_COUNT + 1))
      return 0
    fi
    # Some checks find out the reason themselves, such as a data build that failed.
    if [ -n "$LAST_ERROR" ] && [ "$LAST_ERROR" != "$FAILED_MESSAGE" ]; then
      FAILURE="$FAILURE
$LAST_OUTPUT
$LAST_ERROR"
      FAILED_MESSAGE="$LAST_ERROR"
    fi
    known_fix || break
    attempt=$((attempt + 1))
  done
  LAST_COMMAND="$FAILED_COMMAND"
  LAST_CODE="$FAILED_CODE"
  LAST_ERROR="$FAILED_MESSAGE"
  restart_fresh_shell
  if [ "$optional" = 1 ]; then
    say_warn "Skipped for now: $title. Nothing else depends on it. Run this file again to retry."
    local hint
    hint="$(known_hint "$FAILURE")"
    if [ -n "$hint" ]; then say_warn "$hint"; fi
    RETRY_LIST="$RETRY_LIST
      $title"
    return 0
  fi
  stop_with_error "$title"
}

# ---------------------------------------------------------------------------------------------- the checks and fixes
check_computer() {
  local major
  major="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
  say "macOS $(sw_vers -productVersion 2>/dev/null), $(uname -m). $(ram_gb) GB memory, $(free_gb) GB free."
  if [ "${major:-99}" -lt 14 ]; then say_warn "Docker Desktop needs macOS 14 or newer. Update macOS if Docker fails to install."; fi
  if [ -n "$SSH_CONNECTION" ]; then say_warn "You are connected over SSH. Run this at the Mac itself: it opens apps and uses the clipboard."; fi
  if [ "$(ram_gb)" -lt 15 ]; then say_warn "Under 16 GB of memory: setup will build the Medium data set instead of Large."; fi
  if [ "${HL_MIN_DISK:-40}" -gt "$(free_gb)" ]; then say_warn "Under 40 GB free: the Large data set may not fit. Free up space if the build fails."; fi
  return 0
}
fix_nothing() { return 0; }

# Not just xcode-select -p: that succeeds on a path that no longer exists.
check_clt() {
  local dir
  dir="$(xcode-select -p 2>/dev/null)" || return 1
  [ -x "$dir/usr/bin/git" ]
}
install_clt_from_softwareupdate() {
  local flag=/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress label
  touch "$flag"
  label="$(softwareupdate -l 2>/dev/null | grep -B 1 -E 'Command Line Tools' | awk -F'*' '/^ *\*/ {print $2}' | sed -e 's/^ *Label: //' -e 's/^ *//' | sort -V | tail -n 1)"
  if [ -n "$label" ]; then
    say "Installing $label through Software Update. It asks for your Mac password."
    LAST_COMMAND="softwareupdate -i $label"
    sudo softwareupdate -i "$label"
    LAST_CODE=$?
  else
    LAST_CODE=1
    LAST_OUTPUT="not currently available from the Software Update server"
  fi
  rm -f "$flag"
}
fix_clt() {
  LAST_COMMAND="xcode-select --install"
  LAST_OUTPUT="$(xcode-select --install 2>&1)"
  LAST_CODE=$?
  your_turn --no-wait "A window is asking to install the Command Line Tools. Press Install and wait for it to finish." \
    "Setup carries on by itself when it is done."
  local waited=0
  sleep 5
  while [ "$waited" -lt 1800 ]; do
    check_clt && return 0
    # The window closed without installing: cancelled, or Apple had nothing to offer.
    if ! pgrep -x "Install Command Line Developer Tools" >/dev/null 2>&1 && [ "$waited" -ge 15 ]; then
      install_clt_from_softwareupdate
      check_clt && return 0
      break
    fi
    sleep 5
    waited=$((waited + 5))
  done
  LAST_ERROR="The Command Line Tools did not install."
  return 1
}

check_brew() {
  has brew || return 1
  # An old Homebrew carried over to an Apple Silicon Mac is in the wrong place and refuses to install things.
  [ "$(uname -m)" != "arm64" ] || [ "$(brew --prefix 2>/dev/null)" = "/opt/homebrew" ]
}
fix_brew() {
  local script tmp
  use_system_proxy
  your_turn --no-wait "Installing Homebrew, the Mac package manager." \
    "It asks for your Mac password once, then to press Return. Press only Return: any other key cancels it."
  LAST_COMMAND="Homebrew install script"
  INSTALLED_THIS_RUN=1
  script="$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh 2>&1)"
  LAST_CODE=$?
  if [ "$LAST_CODE" != 0 ]; then
    LAST_OUTPUT="$script"
    LAST_ERROR="Could not download the Homebrew installer."
    return 1
  fi
  tmp="$(mktemp)"
  /bin/bash -c "$script" 2>&1 | tee "$tmp"
  LAST_CODE=${PIPESTATUS[0]}
  LAST_OUTPUT="$(tail -n 40 "$tmp")"
  rm -f "$tmp"
  if [ "$LAST_CODE" != 0 ]; then LAST_ERROR="The Homebrew installer failed."; fi
  refresh_path
  local line='eval "$(/opt/homebrew/bin/brew shellenv)"'
  if [ -x /opt/homebrew/bin/brew ] && ! grep -qs 'brew shellenv' "$HOME/.zprofile"; then printf '\n%s\n' "$line" >>"$HOME/.zprofile"; fi
}
brew_cask() {
  # An app put there by hand makes Homebrew refuse: adopt it. Left-over command-line files: let Homebrew replace them.
  run_live brew install --cask "$1" && return 0
  case "$LAST_OUTPUT" in
    *"already an App at"*) run_live brew install --cask --adopt "$1" ;;
    *"It seems there is already"*) run_live brew install --cask "$1" --force ;;
    *) return 1 ;;
  esac
}

check_docker_app() { [ -d /Applications/Docker.app ] || [ -d "$HOME/Applications/Docker.app" ] || test_docker; }
fix_docker_app() {
  say "Installing Docker Desktop (a few minutes; it may ask for your Mac password)."
  brew_cask docker-desktop
}

check_engine() { test_docker; }
fix_engine() { start_docker_engine; }

check_git() {
  has git || return 1
  # Not just the exit code: a blank name passes it, then every commit fails.
  run git config --global user.name && [ -n "$LAST_OUTPUT" ] || return 1
  run git config --global user.email && [ -n "$LAST_OUTPUT" ]
}
fix_git() {
  has git || run_live brew install git
  if ! run git config --global user.name || [ -z "$LAST_OUTPUT" ]; then
    your_turn --no-wait "Git puts your name and email on every commit."
    ANSWER=""
    while [ -z "$ANSWER" ]; do ask_text "Your full name" ""; done
    run git config --global user.name "$ANSWER"
  fi
  if ! run git config --global user.email || [ -z "$LAST_OUTPUT" ]; then
    ANSWER=""
    until printf '%s' "$ANSWER" | grep -qE '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'; do ask_text "Your email (the one you use on GitHub)" ""; done
    run git config --global user.email "$ANSWER"
  fi
  local pair
  for pair in "init.defaultBranch:main" "pull.ff:only" "fetch.prune:true"; do
    run git config --global "${pair%%:*}" || run git config --global "${pair%%:*}" "${pair#*:}"
  done
}

check_gh() { has gh; }
fix_gh() { run_live brew install gh; }

check_github() { test_github_access; }
fix_github() { connect_github; }

EXTENSIONS="ms-python.python ms-python.vscode-pylance charliermarsh.ruff ms-azuretools.vscode-containers eamodio.gitlens EditorConfig.EditorConfig redhat.vscode-yaml mtxr.sqltools mtxr.sqltools-driver-pg innoverio.vscode-dbt-power-user databricks.databricks snowflake.snowflake-vsc mechatroner.rainbow-csv GrapeCity.gc-excelviewer"
code_cmd() {
  # The real VS Code before whatever 'code' means on PATH: other editors claim that name too.
  local c="/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
  if [ -x "$c" ]; then echo "$c"; return 0; fi
  c="$HOME/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
  if [ -x "$c" ]; then echo "$c"; return 0; fi
  if has code; then echo code; return 0; fi
  return 1
}
check_vscode() {
  local c ext
  c="$(code_cmd)" || return 1
  run "$c" --list-extensions || return 1
  for ext in $EXTENSIONS; do
    case "$LAST_OUTPUT" in *"$ext"*) ;; *) return 1 ;; esac
  done
  return 0
}
fix_vscode() {
  local c have ext failed=""
  code_cmd >/dev/null || brew_cask visual-studio-code
  refresh_path
  c="$(code_cmd)" || return 1
  run "$c" --list-extensions
  have="$LAST_OUTPUT"
  for ext in $EXTENSIONS; do
    case "$have" in
      *"$ext"*) ;;
      *) say "Adding $ext"; run "$c" --install-extension "$ext" --force || failed="$failed $ext: $LAST_OUTPUT;" ;;
    esac
  done
  if [ -n "$failed" ]; then LAST_ERROR="Could not add:$failed"; fi
  if ! run git config --global core.editor; then
    if has code && [ "$c" = "code" ]; then run git config --global core.editor "code --wait"
    else run git config --global core.editor "'$c' --wait"; fi
  fi
}

check_uv() { has uv && has pre-commit && run uv python find 3.12; }
fix_uv() {
  has uv || run_live brew install uv
  has uv || return 1
  # Stop at the first failure, so the report shows the real error and not the last command's.
  run_live uv python install 3.12 || { LAST_ERROR="$LAST_OUTPUT"; return 1; }
  run_live uv tool install pre-commit || { LAST_ERROR="$LAST_OUTPUT"; return 1; }
  # Known problem: uv's tools folder is not on PATH, so pre-commit is "command not found".
  run uv tool update-shell
  refresh_path
}

check_dbeaver() { dbeaver_app >/dev/null; }
fix_dbeaver() { brew_cask dbeaver-community; }

check_copied() {
  [ -f "$LAB_DIR/docker-compose.yml" ] || return 1
  [ -f "$LAB_DIR/setup/VERSION" ] || return 1
  [ "$(cat "$SOURCE_DIR/setup/VERSION")" = "$(cat "$LAB_DIR/setup/VERSION")" ]
}
fix_copied() {
  local elsewhere="" files
  mkdir -p "$LAB_DIR/landing"
  if [ "$SOURCE_DIR" != "$(cd "$LAB_DIR" && pwd)" ]; then
    elsewhere=1
    say "Copying the package."
    # setup/VERSION goes last: until it is there the step reads as not done, so a copy that stopped halfway is simply done again.
    run rsync -a --exclude='/.env' --exclude='/landing/' --exclude='/answer_key/anomalies.csv' --exclude='/setup/VERSION' "$SOURCE_DIR/" "$LAB_DIR/" || return 1
    files="$(mktemp)"
    package_files "$SOURCE_DIR" >"$files"
    remove_stale_files "$files"
    mv "$files" "$FILE_LIST"
  fi
  chmod +x "$LAB_DIR/SETUP-MAC.command" "$LAB_DIR/start.sh" 2>/dev/null
  xattr -dr com.apple.quarantine "$LAB_DIR" 2>/dev/null
  if [ -n "$elsewhere" ]; then cp "$SOURCE_DIR/setup/VERSION" "$LAB_DIR/setup/VERSION" || return 1; fi
  return 0
}

check_settings() {
  keys_complete || return 1
  # Each run, so it follows ports he changed, and an install from before there was a backup gets one.
  save_env_backup
  if [ -n "$(project_containers "$OLD_PROJECT")" ]; then return 1; fi
  if [ -n "$(project_containers "$PROJECT")" ]; then return 0; fi
  port_free "$(pg_port)" && port_free "$(app_port)"
}
fix_settings() {
  repair_env_file || return 1
  local old
  old="$(project_containers "$OLD_PROJECT" -a)"
  if [ -n "$old" ]; then
    say "Stopping the containers from the earlier attempt. Its data stays on disk, untouched."
    # shellcheck disable=SC2086
    run docker rm -f $old
  fi
  move_busy_ports
  # Kept in the settings, so a plain docker compose up later still writes files as this user.
  env_set HL_UID "$(id -u)"
  env_set HL_GID "$(id -g)"
}

package_version() { tr -d '[:space:]' <"$LAB_DIR/setup/VERSION" 2>/dev/null; }
app_current() { [ -f "$STATE_DIR/app-version.txt" ] && [ "$(tr -d '[:space:]' <"$STATE_DIR/app-version.txt")" = "$(package_version)" ]; }
# Asked of the database, not the app, because the app may be what is busy.
running_build() {
  (cd "$LAB_DIR" && docker compose exec -T postgres psql -U postgres -d sim_internal -Atc "SELECT label || ', started ' || to_char(started_at, 'YYYY-MM-DD HH24:MI') || ' UTC' FROM sim.run_log WHERE status = 'running' ORDER BY run_id DESC LIMIT 1") </dev/null 2>/dev/null | tr -d '\r'
}
check_running() {
  [ "$(project_containers "$PROJECT" | wc -l | tr -d ' ')" -ge 2 ] || return 1
  [ -n "$KEEP_OLD_APP" ] || app_current || return 1
  # Slow is not down. Restarting here would stop a data build that is running fine.
  wait_health 300
}
fix_running() {
  local running
  if [ "$(project_containers "$PROJECT" | wc -l | tr -d ' ')" -ge 2 ] && ! app_current; then
    running="$(running_build)"
    if [ -n "$running" ]; then
      your_turn --no-wait "A data build is running: $running." \
        "This version needs the app restarted, which stops that build and loses its progress." \
        "  K  Keep it running. You stay on the old version until it finishes. Run this file again after that." \
        "  R  Restart now on this version, then choose a data size and build again."
      ANSWER=""
      until printf '%s' "$ANSWER" | grep -qE '^[KkRr]$'; do ask_text "Type K or R" "K"; done
      case "$ANSWER" in [Kk]) KEEP_OLD_APP=1; return 0 ;; esac
    fi
  fi
  say "Building and starting the containers (several minutes the first time)."
  (cd "$LAB_DIR" && docker compose up -d --build) >"$STATE_DIR/compose.out" 2>&1
  LAST_CODE=$?
  LAST_COMMAND="docker compose up -d --build (in $LAB_DIR)"
  LAST_OUTPUT="$(tail -n 60 "$STATE_DIR/compose.out")"
  cat "$STATE_DIR/compose.out" >>"$LOG_FILE"
  if [ "$LAST_CODE" -ne 0 ]; then
    # Plus the database's own log, which often holds the real reason.
    LAST_OUTPUT="$LAST_OUTPUT
--- postgres log ---
$(cd "$LAB_DIR" && docker compose logs --tail 40 postgres 2>&1)"
    return 1
  fi
  package_version >"$STATE_DIR/app-version.txt"
  say "Waiting for the app to answer."
  if ! wait_health 300; then
    LAST_OUTPUT="$(cd "$LAB_DIR" && docker compose logs --tail 60 app 2>&1)"
    LAST_ERROR="The app did not answer within 5 minutes."
    return 1
  fi
}

choose_preset() {
  if [ -n "$PRESET" ]; then return 0; fi
  if [ -n "$HL_SETUP_PRESET" ]; then PRESET="$HL_SETUP_PRESET"; return 0; fi
  if [ -n "$BUILD_WAS_CANCELLED" ]; then say "You cancelled the build. Choose a size to build again, or close this window to stop for now."; fi
  your_turn --no-wait "How much data should the simulator build? It runs in the background while setup carries on." \
    "  1  Small   6 months, 3 warehouses. About 3 GB and 10 minutes. Only for a quick try: several project stories are missing." \
    "  2  Medium  2 years, all 8 warehouses. About 12 GB and 1 hour. Has everything the eight projects need." \
    "  3  Large   3 years, all 8 warehouses. About 30 GB and 3 hours. The same stories with more volume." \
    "Times are rough, for a typical laptop. This drive has $(free_gb) GB free." \
    "You can build another size later from the Control Center."
  if [ "$(ram_gb)" -lt 15 ]; then say_warn "Under 16 GB of memory: Large may fail on this computer."; fi
  ANSWER=""
  until printf '%s' "$ANSWER" | grep -qE '^[123]$'; do ask_text "Type 1, 2 or 3" "2"; done
  case "$ANSWER" in 1) PRESET=small ;; 3) PRESET=large ;; *) PRESET=medium ;; esac
}
start_data_build() {
  choose_preset
  say "Starting the $PRESET build. It runs in the background while setup carries on."
  LAST_COMMAND="POST /api/sim/build"
  # Never asks to replace data, so the app refuses this if a finished build exists. A slow answer is fine: the check reads what happened.
  LAST_OUTPUT="$(curl -fsS --noproxy '*' --max-time 120 -X POST -H 'Content-Type: application/json' -d "{\"preset\":\"$PRESET\",\"confirm\":true}" "$(api_url /api/sim/build)" 2>&1)"
}
# built_at is written only after a build has finished every step and checked its data, and it is cleared before a new
# build wipes anything. It lives in the database, next to the data it describes. Nothing else counts as finished.
check_build_started() {
  SIM_ANSWERED=1
  if ! get_sim_status; then SIM_ANSWERED=""; return 1; fi
  if [ "$(json_field "$SIM_STATUS" state)" = "building" ]; then
    say "A $(json_field "$SIM_STATUS" preset) build is running. To pick another size, press Cancel on the Control Center and run this file again."
    return 0
  fi
  if [ -n "$(json_field "$SIM_STATUS" built_at)" ]; then
    say "Your $(json_field "$SIM_STATUS" preset) data is already built. Setup will not touch it."
    return 0
  fi
  return 1
}
fix_build_started() {
  if [ -z "$SIM_ANSWERED" ]; then LAST_ERROR="$NO_ANSWER_TEXT"; return 1; fi
  start_data_build
}

check_tracker() { api_get /api/tracker/meta | grep -q '"configured":true'; }
next_monday() {
  local dow days
  dow="$(date +%u)"
  days=$(((8 - dow) % 7))
  date -v+"${days}"d +%Y-%m-%d
}
fix_tracker() {
  your_turn --no-wait "Every due date in the eight projects counts from your start date."
  ANSWER=""
  until printf '%s' "$ANSWER" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; do
    ask_text "The Monday you start, as YYYY-MM-DD (Enter accepts this one)" "$(next_monday)"
  done
  LAST_COMMAND="POST /api/tracker/setup"
  LAST_OUTPUT="$(api_post /api/tracker/setup "{\"start_date\":\"$ANSWER\"}")"
  api_post /api/tracker/sprints/1/start '{}' >/dev/null 2>&1
  return 0
}

check_repo() { test_repo_ready; }
fix_repo() { new_analytics_repo; }

check_protected() { test_repo_protected; }
fix_protected() {
  local user
  user="$(github_user)"
  run gh api --method POST "repos/$user/$REPO_NAME/rulesets" --input "$LAB_DIR/setup/repo-ruleset.json"
}

check_connections() { test_dbeaver_connections; }
fix_connections() { add_dbeaver_connections; }

check_build_done() {
  local s state built last="" line pct phase silent=0
  BUILD_FAILED=""
  printf '\n  Everything is set up. The data build is the last thing running.\n'
  printf '  You can close this window at any time: the build keeps going by itself.\n'
  while :; do
    s="$(api_get /api/sim/status)"
    if [ -z "$s" ]; then
      # Don't wait forever on an app that has gone away.
      silent=$((silent + 1))
      if [ "$silent" -ge 20 ]; then
        LAST_COMMAND="GET /api/sim/status"
        LAST_OUTPUT="$(cd "$LAB_DIR" && docker compose logs --tail 40 app 2>&1)"
        LAST_ERROR="The app stopped answering for 10 minutes. Nothing was wiped, and the build may still be running."
        return 1
      fi
      sleep 10
      continue
    fi
    silent=0
    state="$(json_field "$s" state)"
    [ "$state" = "building" ] || break
    pct="$(printf '%s' "$s" | sed -n 's/.*"progress":\([0-9.]*\).*/\1/p' | head -n 1)"
    phase="$(json_field "$s" phase)"
    line="  Data build: $(awk -v p="${pct:-0}" 'BEGIN { printf "%d", p * 100 }')% (${phase:-working})"
    if [ "$line" != "$last" ]; then printf '%s\n' "$line"; last="$line"; fi
    sleep 15
  done
  built="$(json_field "$s" built_at)"
  [ -n "$built" ] && return 0
  # It answered, and the data has no finished stamp: stopped, cancelled or failed.
  BUILD_FAILED=1
  LAST_COMMAND="POST /api/sim/build"
  if printf '%s' "$s" | grep -q '"status":"cancelled"'; then
    BUILD_WAS_CANCELLED=1
    PRESET=""
    LAST_ERROR="The data build was cancelled."
    return 1
  fi
  LAST_ERROR="The data build did not finish: $(json_field "$s" error)"
  BUILD_FAILURE="$LAST_ERROR"
  LAST_OUTPUT="$LAST_ERROR
--- app log ---
$(cd "$LAB_DIR" && docker compose logs --tail 2000 app 2>&1 | grep -v 'GET /api/health' | tail -n 40)"
  return 1
}
fix_build_done() {
  [ -n "$BUILD_FAILED" ] || return 0
  if [ -z "$BUILD_WAS_CANCELLED" ]; then say "$BUILD_FAILURE Starting it again."; fi
  start_data_build
  BUILD_WAS_CANCELLED=""
}

# ---------------------------------------------------------------------------------------------- updates
# A new version is the same as running a newer zip: get the files, then run the new setup file from them. Finished steps
# skip, and the Simulator and Start steps do the rest. Nothing here may stop or slow down someone with no internet.
version_in() { { tr -d '[:space:]' <"$1/setup/VERSION"; } 2>/dev/null; }
version_ok() { printf '%s' "$1" | grep -Eq '^[0-9]{1,9}(\.[0-9]{1,9}){0,5}$'; }
version_newer() {
  # Is $1 newer than $2? Part by part as numbers: as text, 2026.11.10 would sort before 2026.11.9. Anything unreadable is never newer.
  version_ok "$1" && version_ok "$2" || return 1
  local a="$1." b="$2." x y
  while [ -n "$a" ] || [ -n "$b" ]; do
    x="${a%%.*}"; a="${a#*.}"
    y="${b%%.*}"; b="${b#*.}"
    [ "${x:-0}" -gt "${y:-0}" ] && return 0
    [ "${x:-0}" -lt "${y:-0}" ] && return 1
  done
  return 1
}
update_repo() {
  # The environment wins, for testing and forks. Blank turns update checks off.
  local text=""
  if [ -n "${HL_UPDATE_REPO+set}" ]; then text="$HL_UPDATE_REPO"
  elif [ -f "$SOURCE_DIR/setup/UPDATE_REPO" ]; then text="$(cat "$SOURCE_DIR/setup/UPDATE_REPO")"; fi
  text="$(printf '%s' "$text" | tr -d '[:space:]')"
  if [ -n "$text" ] && ! printf '%s' "$text" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$'; then
    log "Update check skipped: '$text' is not owner/name."
    return 0
  fi
  printf '%s' "$text"
}
# In a subshell, so the proxy setting stays out of everything else. Errors go to the log only.
update_curl() { ( use_system_proxy; exec curl -fsSLS "$@" ) </dev/null 2>>"$LOG_FILE"; }
stop_if_older_than_installed() {
  # An old zip run by mistake. Works with no internet, and never copies old files over new ones.
  local mine installed
  mine="$(version_in "$SOURCE_DIR")"
  installed="$(version_in "$LAB_DIR")"
  version_newer "$installed" "$mine" || return 0
  printf '\n'
  say_warn "This file is version $mine, but version $installed is already installed, so setup will not copy old files over it."
  if [ -f "$LAB_DIR/SETUP-MAC.command" ]; then
    say "Carrying on with the installed copy."
    unset HL_SETUP_UPDATED
    exec /bin/bash "$LAB_DIR/SETUP-MAC.command"
  fi
  say "Nothing was changed. To carry on, run the newest copy of this file you have."
  exit 1
}
update_failed() {
  rm -rf "${UPDATE_DIR:?}"
  say_warn "The update did not work, so setup is carrying on with version $1. Nothing was changed."
}
start_update() {
  local repo="$1" version="$2" here zip top got
  here="$(version_in "$SOURCE_DIR")"
  top="$UPDATE_DIR/${repo#*/}-main"
  zip="$UPDATE_DIR/update.zip"
  rm -rf "${UPDATE_DIR:?}"
  mkdir -p "$UPDATE_DIR" || { update_failed "$here"; return 0; }
  say "Downloading it. This usually takes under a minute."
  if ! update_curl --connect-timeout 20 --max-time 900 -o "$zip" "https://github.com/$repo/archive/refs/heads/main.zip"; then
    log "Update failed: the download did not finish."
    update_failed "$here"; return 0
  fi
  # </dev/null: unzip asks questions on stdin, and the answers to setup's own questions are there.
  if ! unzip -q -o "$zip" -d "$UPDATE_DIR" </dev/null >>"$LOG_FILE" 2>&1; then
    log "Update failed: the download is not a working zip."
    update_failed "$here"; return 0
  fi
  got="$(version_in "$top")"
  if [ ! -f "$top/SETUP-MAC.command" ] || [ ! -f "$top/docker-compose.yml" ] || [ "$got" != "$version" ] || ! /bin/bash -n "$top/SETUP-MAC.command" 2>>"$LOG_FILE"; then
    log "Update failed: the download is version '$got', expected $version, or does not hold the package."
    update_failed "$here"; return 0
  fi
  rm -f "${zip:?}"
  chmod +x "$top/SETUP-MAC.command" "$top/start.sh" 2>/dev/null
  printf '%s\n' "$version" >"$UPDATED_TO_FILE"
  say "Carrying on with version $version."
  # The marker stops the new file from checking again. exec: the old file is gone from memory before anything is copied over it.
  HL_SETUP_UPDATED="$version"
  export HL_SETUP_UPDATED
  exec /bin/bash "$top/SETUP-MAC.command"
}
start_update_check() {
  local repo here latest
  # The run an update started has already been through this, and it must not ask again.
  if [ -n "$HL_SETUP_UPDATED" ]; then log "Running the update to $HL_SETUP_UPDATED."; return 0; fi
  # Left from the last update, unless this file is running from it.
  case "$SOURCE_DIR/" in "$UPDATE_DIR"/*) ;; *) rm -rf "${UPDATE_DIR:?}" ;; esac
  repo="$(update_repo)"
  if [ -z "$repo" ]; then log "Update checks are off."; return 0; fi
  here="$(version_in "$SOURCE_DIR")"
  if ! latest="$(update_curl --max-time 10 "https://raw.githubusercontent.com/$repo/main/setup/VERSION")"; then
    log "Update check skipped: could not read the latest version."
    return 0
  fi
  latest="$(printf '%s' "$latest" | tr -d '[:space:]')"
  if ! version_ok "$latest" || ! version_ok "$here"; then
    log "Update check skipped: the version numbers could not be read."
    return 0
  fi
  if ! version_newer "$latest" "$here"; then say "You have the latest version ($here)."; return 0; fi
  printf '    Version %s is available. Update now? Your data, settings and progress are kept. (Y/n): ' "$latest"
  # No keyboard is a no, unlike an empty answer.
  if ! read -r ANSWER; then ANSWER="n"; printf '\n'; fi
  case "$ANSWER" in
    ""|[Yy]|[Yy][Ee][Ss]) start_update "$repo" "$latest" ;;
    *) say "Carrying on with version $here." ;;
  esac
}
show_update_result() {
  # Kept until the app really runs the new version, which is later if he chose to let a running build finish.
  [ -f "$UPDATED_TO_FILE" ] || return 0
  local version message
  version="$(tr -d '[:space:]' <"$UPDATED_TO_FILE")"
  app_current || return 0
  rm -f "${UPDATED_TO_FILE:?}"
  [ "$version" = "$(package_version)" ] || return 0
  say_good "Updated to $version."
  # For example: data from an earlier version needs one rebuild. That stays his decision, so it is shown and nothing more.
  # The top-level error only: json_field would find the last run's error further along.
  message="$(api_get /api/sim/status | sed -n 's/^{"state":"[^"]*","error":"\(.*\)","preset".*/\1/p' | sed 's/\\"/"/g')"
  if [ -n "$message" ]; then say_warn "$message"; fi
}

# Everything setup copied last time, so a later version can remove only what it put there itself. A file he made was
# never on this list, so it can't be removed. With no list yet, nothing is removed.
package_files() {
  (cd "$1" && find . -type f ! -path './landing/*' ! -path './.git/*') 2>/dev/null | sed 's#^\./##' | grep -vE '^(\.env|answer_key/anomalies\.csv)$'
}
remove_stale_files() {
  local current="$1" gone rel top dir removed=0
  [ -f "$FILE_LIST" ] || return 0
  gone="$(mktemp)"
  # Ignoring case, like the disk: a file renamed from Foo to foo must not count as removed.
  awk 'NR == FNR { keep[tolower($0)] = 1; next } !(tolower($0) in keep)' "$current" "$FILE_LIST" >"$gone"
  while IFS= read -r rel; do
    case "$rel" in
      ""|/*|../*|*/../*|*/..|.env|landing/*|answer_key/anomalies.csv) continue ;;
      */*) ;;
      # Only inside folders the package owns: a file at the top of the folder stays.
      *) continue ;;
    esac
    top="${rel%%/*}"
    case " $OWNED_FOLDERS " in *" $top "*) ;; *) continue ;; esac
    [ -f "$LAB_DIR/$rel" ] || continue
    rm -f "${LAB_DIR:?}/${rel:?}" || continue
    log "REMOVED $rel"
    removed=$((removed + 1))
    dir="${rel%/*}"
    while [ "$dir" != "$top" ]; do
      rmdir "$LAB_DIR/$dir" 2>/dev/null || break
      dir="${dir%/*}"
    done
  done <"$gone"
  rm -f "${gone:?}"
  if [ "$removed" -gt 0 ]; then say "Removed $removed old files this version no longer ships."; fi
}

# ---------------------------------------------------------------------------------------------- hook for the tests
if [ -n "$HL_SETUP_STUBS" ]; then
  # shellcheck disable=SC1090
  . "$HL_SETUP_STUBS"
fi

# ---------------------------------------------------------------------------------------------- the steps
printf '\n  Harborline Lab setup\n'
printf '  It does everything it can by itself and stops only when it needs you. Safe to run again at any time.\n'
printf '  Log: %s\n' "$LOG_FILE"

refresh_path
if [ ! -f "$SOURCE_DIR/docker-compose.yml" ]; then
  printf '\n%s    This file is being run without the rest of the package.%s\n' "$C_RED" "$C_OFF"
  printf '%s    Double-click the zip to extract it, open the extracted folder and run this file there.%s\n' "$C_YELLOW" "$C_OFF"
  exit 1
fi

stop_if_older_than_installed
start_update_check

step "Check this computer" check_computer fix_nothing
step "Command Line Tools" check_clt fix_clt
step "Homebrew" check_brew fix_brew
step "Docker Desktop" check_docker_app fix_docker_app
step "Docker engine running" check_engine fix_engine
step "Git and your name on commits" check_git fix_git
step "GitHub CLI" check_gh fix_gh
step "GitHub sign-in and SSH key" check_github fix_github
step --optional "VS Code and extensions" check_vscode fix_vscode
step "uv, Python 3.12 and pre-commit" check_uv fix_uv
step --optional "DBeaver (database client)" check_dbeaver fix_dbeaver
step "Simulator in $LAB_DIR" check_copied fix_copied
step "Settings and free ports" check_settings fix_settings
step "Start the simulator" check_running fix_running
step "Start building the data" check_build_started fix_build_started
step "Tracker start date" check_tracker fix_tracker
step "Your repository $REPO_NAME" check_repo fix_repo
step --optional "Commit checks in your repository" test_repo_hooks fix_repo_hooks
step --optional "Protect main on GitHub" check_protected fix_protected
step --optional "DBeaver connections" check_connections fix_connections
# Stopping here is his choice, not a failure a fresh shell could cure.
NO_FRESH_SHELL=1
step --optional "Your team chats on your AI site" check_team_chats fix_team_chats
NO_FRESH_SHELL=""
PASS_TEXT="Finished."
step "Data build finished" check_build_done fix_build_done

# ---------------------------------------------------------------------------------------------- finish
url="$(app_url)"
rm -f "$ERROR_FILE"
printf '\n%s  ================================ ALL DONE ================================%s\n' "$C_GREEN" "$C_OFF"
if [ -n "$RETRY_LIST" ]; then printf '%s    Needs a retry (run this file again):%s%s\n' "$C_YELLOW" "$RETRY_LIST" "$C_OFF"; fi
printf '    %s steps done now, %s were already in place.\n' "$DONE_COUNT" "$SKIP_COUNT"
show_update_result
printf '\n'
printf '    Simulator      %s\n' "$LAB_DIR"
printf '    Your repo      %s\n' "$REPO_DIR"
printf '    Control Center %s\n' "$url"
printf '    Team           %s/team\n' "$url"
printf '    Chat zip       %s/harborline-chat.zip (attach it with every prompt)\n' "$ROOT"
printf '    Tracker        %s/tracker\n' "$url"
printf '    Databases      localhost:%s, login etl_reader, password HL_ETL_PASSWORD in .env\n' "$(pg_port)"
printf '                   wms (schema wms), ax2012 (schema dbo), iot (schema telemetry), tracker (schema tracker)\n\n'
printf '%s    Next, by hand:%s\n' "$C_YELLOW" "$C_OFF"
printf '%s    1. Close any terminal or VS Code window you had open, and open a new one. Old windows cannot see what was just installed.%s\n' "$C_YELLOW" "$C_OFF"
printf '%s    2. Read the project brief, then sign up for your cloud platform.%s\n' "$C_YELLOW" "$C_OFF"
printf '%s    3. Anyone you skipped in the team chats: run this file again.%s\n' "$C_YELLOW" "$C_OFF"
if [ -n "$KEEP_OLD_APP" ]; then printf '%s    4. You kept the old version running so the build could finish. Run this file again to update.%s\n' "$C_YELLOW" "$C_OFF"; fi
left=""
for f in $OLD_FOLDERS; do
  if [ -e "$ROOT/$f" ]; then left="$left
      $ROOT/$f"; fi
done
if [ -n "$left" ]; then
  printf '\n    Left over from the earlier attempt, unused and safe to delete when you are happy:%s\n' "$left"
  printf '      Docker data: docker volume rm %s_pgdata\n' "$OLD_PROJECT"
  printf '      GitHub repo: harborline-data-platform, if you made one\n'
fi
printf '\n'
if [ -z "$HL_SETUP_NO_OPEN" ]; then open "$url/START_HERE.html" >/dev/null 2>&1; fi
exit 0

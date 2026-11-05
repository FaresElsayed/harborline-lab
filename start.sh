#!/usr/bin/env bash
# Harborline 3PL Simulator - start everything and open the Control Center (macOS / Linux).
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "[FAIL] Docker is not installed. Install Docker Desktop (macOS) or Docker Engine + Compose v2 (Linux)." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "[FAIL] Docker is not running. Start Docker, then run ./start.sh again." >&2
  exit 1
fi

# Passwords and keys are made here, once, the same way setup does it. What is already in .env always wins: Postgres keeps
# its first password in the Docker volume, so a new one would lock you out of your own data.
key_names="POSTGRES_PASSWORD HL_APP_PASSWORD HL_ETL_PASSWORD HL_REVIEWER_PASSWORD SF_CLIENT_ID SF_CLIENT_SECRET CARRIER_API_KEY"
case "$(uname -s)" in
  Darwin) state_dir="${HL_STATE_DIR:-$HOME/Library/Logs/HarborlineLab}" ;;
  *) state_dir="${HL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/HarborlineLab}" ;;
esac
env_backup="$state_dir/env.backup"
mkdir -p "$state_dir"
key_in() { { grep -E "^[[:space:]]*$1[[:space:]]*=" "$2" || true; } 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '[:space:]'; }
new_key() {
  local k
  k="$(openssl rand -hex 24 2>/dev/null || true)"
  if [ "${#k}" -lt 32 ]; then k="$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n')"; fi
  if [ "${#k}" -lt 32 ]; then echo "[FAIL] Could not make a random key on this computer." >&2; exit 1; fi
  printf '%s' "$k"
}
set_key() {
  local tmp
  tmp="$(mktemp)"
  if grep -qE "^[[:space:]]*$1[[:space:]]*=" .env; then
    sed -E "s|^[[:space:]]*$1[[:space:]]*=.*|$1=$2|" .env >"$tmp"
  else
    cat .env >"$tmp"
    printf '%s=%s\n' "$1" "$2" >>"$tmp"
  fi
  mv "$tmp" .env
}
stop_if_data_would_lock_out() {
  docker volume ls -q --filter "name=harborline-lab_pgdata" | grep -qxF "harborline-lab_pgdata" || return 0
  cat >&2 <<STOP
Your settings file (.env) and its backup are both gone, but your simulator data is still here in Docker.
The data is locked with the password that was in that file. A new password would not open it, so this stopped here.
Nothing has been changed or deleted. You have two choices:
  1  Keep your data: put your old .env back in this folder, from a backup or another copy, then run this again.
  2  Start fresh: this deletes all your simulator data and tracker progress. Run these, then run this again:
       docker rm -f harborline-lab-app-1 harborline-lab-postgres-1
       docker volume rm harborline-lab_pgdata
STOP
  exit 1
}
if [ ! -f .env ]; then
  if [ -f "$env_backup" ]; then
    cp "$env_backup" .env
    echo "Your .env was missing, so it was restored from its backup. Your passwords still match your data."
  else
    stop_if_data_would_lock_out
    cp .env.example .env
    echo "Created .env with passwords and keys made just for this computer."
  fi
fi
chmod 600 .env
for key in $key_names; do
  [ -z "$(key_in "$key" .env)" ] || continue
  value="$(key_in "$key" "$env_backup")"
  if [ -z "$value" ]; then
    if [ "$key" = "POSTGRES_PASSWORD" ]; then stop_if_data_would_lock_out; fi
    value="$(new_key)"
  fi
  set_key "$key" "$value"
done
cp .env "$env_backup"
chmod 600 "$env_backup"
mkdir -p landing

# Run the app container as you, so files it writes to ./landing and ./answer_key stay yours.
HL_UID="$(id -u)"
HL_GID="$(id -g)"
export HL_UID HL_GID

port="$(grep -E '^[[:space:]]*HL_APP_PORT[[:space:]]*=' .env | tail -n 1 | cut -d= -f2 | tr -d '[:space:]' || true)"
url="http://localhost:${port:-8080}"

echo "Building and starting the containers (the first time takes a few minutes)..."
docker compose up -d --build

echo "Waiting for the simulator at ${url} ..."
for _ in $(seq 1 150); do
  if curl -fsS -o /dev/null "${url}/api/health" 2>/dev/null; then
    echo
    echo "Harborline is running: ${url}"
    echo "  Control Center ... ${url}/"
    echo "  Handbook ......... ${url}/START_HERE.html"
    echo "  Stop it with: docker compose down   (data is kept)"
    if command -v open >/dev/null 2>&1; then open "${url}/"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "${url}/" >/dev/null 2>&1 || true
    fi
    exit 0
  fi
  sleep 2
done
echo "[WARN] The simulator did not answer within 5 minutes. Check: docker compose logs --tail 100 app" >&2
exit 1

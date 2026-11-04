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

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example (edit it to change ports or passwords)."
fi
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

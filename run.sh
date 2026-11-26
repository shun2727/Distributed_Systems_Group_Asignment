#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

INTERNAL_KEY="${INTERNAL_KEY:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
ADMIN_NUMBER="${ADMIN_NUMBER:-AIRN7K29ADMIN}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin123}"
AVAILABILITY_PORT="${AVAILABILITY_PORT:-5002}"
BOOKING_PORT="${BOOKING_PORT:-5001}"
NOTIFICATION_PORT="${NOTIFICATION_PORT:-5003}"
WEBSITE_PORT="${PORT:-${WEBSITE_PORT:-3000}}"
AVAILABILITY_URL="${AVAILABILITY_URL:-http://localhost:${AVAILABILITY_PORT}}"
NOTIFICATION_URL="${NOTIFICATION_URL:-http://localhost:${NOTIFICATION_PORT}}"
CLIENT_ORIGINS="${CLIENT_ORIGINS:-http://localhost:${WEBSITE_PORT}}"
export INTERNAL_KEY ADMIN_NUMBER ADMIN_PASSWORD

PIDS=()

stop_services() {
  trap - INT TERM EXIT
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
}

trap stop_services INT TERM EXIT

PORT="$AVAILABILITY_PORT" python3 -u availability/availability.py & PIDS+=("$!")
PORT="$NOTIFICATION_PORT" python3 -u notification/notification.py & PIDS+=("$!")
PORT="$BOOKING_PORT" AVAILABILITY_URL="$AVAILABILITY_URL" NOTIFICATION_URL="$NOTIFICATION_URL" CLIENT_ORIGINS="$CLIENT_ORIGINS" python3 -u booking/booking.py & PIDS+=("$!")
PORT="$WEBSITE_PORT" BOOKING_PORT="$BOOKING_PORT" python3 -u website/app.py & PIDS+=("$!")

printf 'AirnStay services are starting. Open http://localhost:%s\n' "$WEBSITE_PORT"
printf 'Admin login defaults: number %s, password %s (set ADMIN_NUMBER and ADMIN_PASSWORD to change them).\n' "$ADMIN_NUMBER" "$ADMIN_PASSWORD"
printf 'Press Ctrl+C to stop all services.\n'

# Exit and clean up if any service exits unexpectedly.
wait -n "${PIDS[@]}"
status=$?
printf 'A service stopped; shutting down the rest.\n'
exit "$status"

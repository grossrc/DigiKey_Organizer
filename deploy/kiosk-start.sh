#!/usr/bin/env bash
set -euo pipefail

URL="http://localhost/"
LOG="/tmp/kiosk.log"

log(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }

log "kiosk-start: waiting for $URL"
for _ in $(seq 1 60); do
  if curl -fsS "$URL" >/dev/null 2>&1; then
    log "server is up"
    break
  fi
  sleep 1
done

# Use a dedicated Chromium profile so we never hit "profile in use" locks
PROFILE_DIR="/tmp/kiosk-profile"
mkdir -p "$PROFILE_DIR"

# Find chromium binary across images
if command -v chromium-browser >/dev/null 2>&1; then
  BROWSER=chromium-browser
elif command -v chromium >/dev/null 2>&1; then
  BROWSER=chromium
else
  log "chromium not found; install it with: sudo apt -y install chromium-browser"
  exit 1
fi

log "launching $BROWSER in kiosk"
"$BROWSER" \
  --noerrdialogs \
  --disable-infobars \
  --kiosk "$URL" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --password-store=basic \
  --ozone-platform=wayland \
  >>"$LOG" 2>&1 \
  || "$BROWSER" \
       --noerrdialogs \
       --disable-infobars \
       --kiosk "$URL" \
       --user-data-dir="$PROFILE_DIR" \
       --no-first-run \
       --password-store=basic \
       >>"$LOG" 2>&1

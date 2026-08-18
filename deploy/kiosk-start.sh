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

# Use a dedicated, persistent Chromium profile so kiosk permissions survive reboots.
PROFILE_DIR="${HOME}/.config/digikey-organizer-kiosk"
mkdir -p "$PROFILE_DIR"

# Find chromium binary across images
if command -v chromium-browser >/dev/null 2>&1; then
  BROWSER=chromium-browser
elif command -v chromium >/dev/null 2>&1; then
  BROWSER=chromium
else
  log "chromium not found; install it with: sudo apt -y install chromium (Trixie+) or chromium-browser (older)"
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
  --touch-events=enabled \
  --ozone-platform=wayland \
  >>"$LOG" 2>&1 \
  || "$BROWSER" \
       --noerrdialogs \
       --disable-infobars \
       --kiosk "$URL" \
       --user-data-dir="$PROFILE_DIR" \
       --no-first-run \
       --password-store=basic \
      --touch-events=enabled \
       >>"$LOG" 2>&1

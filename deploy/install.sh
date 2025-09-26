#!/usr/bin/env bash
set -euo pipefail

# ===== Config (override via env or edit here) =====
APP_REPO_URL="${APP_REPO_URL:-https://github.com/grossrc/Read-Digikey-DataMatrix.git}"
APP_DIR="/opt/catalog"
HOSTNAME_DESIRED="${HOSTNAME_DESIRED:-lab-parts}"
PG_VER="17"
DB_USER="${DB_USER:-murph}"
DB_NAME="${DB_NAME:-parts_DB}"
DB_PASS_DEFAULT="${DB_PASS_DEFAULT:-password}"   # used if you press Enter at prompt
LOG_FILE="/var/log/catalog-install.log"
NGINX_SITE="/etc/nginx/sites-available/catalog"
KIOSK_DESKTOP_FILE="$HOME/.config/autostart/catalog-kiosk.desktop"

# ===== Logging =====
sudo mkdir -p "$(dirname "$LOG_FILE")"
exec > >(sudo tee -a "$LOG_FILE") 2>&1

echo "== $(date -Is) Starting DigiKey Organizer setup =="

# tiny built-in retry (avoid external deps)
retry() { # retry <times> <sleep> -- <cmd...>
  local -i times="$1"; shift
  local -i delay="$1"; shift
  local i
  for ((i=1;i<=times;i++)); do
    "$@" && return 0 || true
    echo "Retry $i/$times failed; sleeping $delay..." >&2
    sleep "$delay"
  done
  echo "ERROR: command failed after $times attempts: $*" >&2
  return 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1"; exit 1; }
}

# ===== 1) mDNS hostname =====
echo "== Setting hostname to $HOSTNAME_DESIRED and enabling avahi =="
retry 3 2 sudo apt-get update
retry 3 2 sudo apt-get install -y avahi-daemon
sudo raspi-config nonint do_hostname "$HOSTNAME_DESIRED" || true
echo "$HOSTNAME_DESIRED" | sudo tee /etc/hostname >/dev/null
sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1   $HOSTNAME_DESIRED/" /etc/hosts
sudo systemctl enable --now avahi-daemon
sudo systemctl restart avahi-daemon
echo "Hostname now: $(hostname)"

# ===== 2) System packages (Python, git, nginx, Postgres 17) =====
echo "== Installing base packages =="
retry 3 2 sudo apt-get install -y python3-venv python3-pip git nginx curl ca-certificates gnupg lsb-release dos2unix

# Add PostgreSQL APT repo (if not already)
if ! [ -f /etc/apt/sources.list.d/pgdg.list ]; then
  echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" | \
    sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | \
    sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg
fi
retry 3 2 sudo apt-get update
retry 3 2 sudo apt-get install -y "postgresql-$PG_VER" "postgresql-client-$PG_VER" libpq-dev

# sanity
sudo systemctl enable --now postgresql

# ===== 3) App code in /opt/catalog + venv =====
echo "== Preparing application at $APP_DIR =="
sudo mkdir -p "$APP_DIR"
sudo chown "$USER":"$USER" "$APP_DIR"

if [ ! -d "$APP_DIR/.git" ] && [ ! -f "$APP_DIR/requirements.txt" ]; then
  echo "Cloning app repo: $APP_REPO_URL"
  git clone "$APP_REPO_URL" "$APP_DIR"
else
  echo "App already present; pulling latest…"
  (cd "$APP_DIR" && git pull --ff-only || true)
fi

cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
python --version
pip install -U pip wheel
pip install -r requirements.txt

# ===== 4) PostgreSQL DB + schema =====
echo "== Configuring PostgreSQL database =="
# Create user if missing (prompt for password)
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1; then
  echo -n "Enter DB password for user '${DB_USER}' [default: ${DB_PASS_DEFAULT}]: "
  read -r DB_PASS_INPUT || true
  DB_PASS="${DB_PASS_INPUT:-$DB_PASS_DEFAULT}"
  sudo -u postgres psql -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '$DB_PASS';"
else
  # try to use default unless overridden via env
  DB_PASS="${DB_PASS_DEFAULT}"
fi

# Create DB if missing and set owner
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
fi

# Load schema (idempotent-ish, will stop on error)
if [ -f deploy/schema.sql ]; then
  echo "Loading schema from deploy/schema.sql"
  PSQL_URL="postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"
  psql "$PSQL_URL" -v ON_ERROR_STOP=1 -f deploy/schema.sql || {
    echo "Schema load reported errors. Check $LOG_FILE"; exit 1; }
else
  echo "WARN: deploy/schema.sql not found; skipping schema load"
fi

# ===== 5) .env =====
echo "== Creating/updating .env =="
if [ ! -f .env ]; then
  [ -f deploy/.env.example ] && cp deploy/.env.example .env || touch .env
fi

# ensure DB vars present; patch password if needed
grep -q '^DB_URL=' .env || echo "DB_URL=postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}" >> .env
sed -i "s|^DB_URL=.*|DB_URL=postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}|" .env

# Prompt for Digi-Key credentials if missing/blank
if ! grep -q '^DIGIKEY_CLIENT_ID=' .env || [ -z "$(grep '^DIGIKEY_CLIENT_ID=' .env | cut -d= -f2-)" ]; then
  read -rp "Enter DigiKey CLIENT_ID: " DK_ID
  sed -i "/^DIGIKEY_CLIENT_ID=/d" .env
  echo "DIGIKEY_CLIENT_ID=$DK_ID" >> .env
fi
if ! grep -q '^DIGIKEY_CLIENT_SECRET=' .env || [ -z "$(grep '^DIGIKEY_CLIENT_SECRET=' .env | cut -d= -f2-)" ]; then
  read -rsp "Enter DigiKey CLIENT_SECRET: " DK_SECRET; echo
  sed -i "/^DIGIKEY_CLIENT_SECRET=/d" .env
  echo "DIGIKEY_CLIENT_SECRET=$DK_SECRET" >> .env
fi

# ===== 6) systemd (gunicorn) =====
echo "== Installing systemd service =="
sudo tee /etc/systemd/system/catalog.service >/dev/null <<EOF
[Unit]
Description=Catalog Flask App (gunicorn)
Wants=network-online.target
After=network-online.target postgresql@${PG_VER}-main.service

[Service]
User=${USER}
Group=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env

ExecStartPre=/bin/sh -c 'for i in \$(seq 1 60); do /usr/bin/pg_isready -q -h 127.0.0.1 -p 5432 && exit 0; sleep 1; done; exit 1'
ExecStart=${APP_DIR}/.venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 app:app

Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
Restart=always
RestartSec=3
TimeoutStartSec=90

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now catalog
sudo systemctl status catalog --no-pager || true

# ===== 7) nginx reverse proxy =====
echo "== Configuring nginx reverse proxy =="
# Make UI symlink (use underscores)
if [ -d "${APP_DIR}/UI Pages" ]; then
  sudo ln -sfn "${APP_DIR}/UI Pages" "${APP_DIR}/ui_pages"
fi

# Disable default site
sudo rm -f /etc/nginx/sites-enabled/default || true

sudo tee "$NGINX_SITE" >/dev/null <<'EOF'
server {
    listen 80 default_server;
    server_name lab-parts.local 127.0.0.1 127.0.1.1 localhost _;

    location /static/ {
        alias /opt/catalog/ui_pages/;
        expires 30d;
        access_log off;
    }

    location / {
        include proxy_params;
        proxy_pass http://127.0.0.1:5000;
        client_max_body_size 10m;
    }
}
EOF
sudo ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/catalog
sudo nginx -t
sudo systemctl reload nginx

# ===== 8) Kiosk (Chromium autostart) =====
echo "== Setting up kiosk autostart =="
# A) Desktop autologin
sudo raspi-config nonint do_boot_behaviour B4 || true

# B) Chromium
retry 3 2 sudo apt-get install -y chromium-browser curl

# C) kiosk launcher from repo (if present)
if [ -f deploy/kiosk-start.sh ]; then
  dos2unix deploy/kiosk-start.sh || true
  sudo install -o "$USER" -g "$USER" -m 0755 deploy/kiosk-start.sh /opt/kiosk-start.sh
fi

# D) desktop autostart entry
mkdir -p "$(dirname "$KIOSK_DESKTOP_FILE")"
tee "$KIOSK_DESKTOP_FILE" >/dev/null <<'EOF'
[Desktop Entry]
Type=Application
Name=Catalog Kiosk
Exec=/opt/kiosk-start.sh
X-GNOME-Autostart-enabled=true
X-LXQt-Need-Tray=false
EOF

# E) Clear old Chromium locks
rm -rf "$HOME/.config/chromium/Singleton"* "$HOME/.config/chromium/Crash Reports" 2>/dev/null || true

echo "== Installation complete =="
echo "Reach the app at: http://${HOSTNAME_DESIRED}.local/catalog (or http://<Pi-IP>/catalog)"
echo "Log: $LOG_FILE"

# Optional: reboot countdown
if [ "${NO_REBOOT:-0}" != "1" ]; then
  echo -n "Rebooting to finalize setup in "
  for s in 10 9 8 7 6 5 4 3 2 1; do echo -n "$s "; sleep 1; done
  echo
  sudo reboot
fi

#!/usr/bin/env bash
# ============================================================================
# Lab Parts Catalog - Resumable One-Shot Installer (Raspberry Pi)
# - Safe to re-run: step checkpoints + idempotent operations
# - Robust: retries for network ops, clear logging, fail-fast, cleanup traps
# - Prompts ONLY for DigiKey client_id and client_secret on first run
#
# Defaults (edit if desired):
#   HOSTNAME=lab-parts
#   DB_USER=murph
#   DB_PASS=password
#   DB_NAME=parts_DB
#   SERVICE_USER=pi
#   APP_DIR=/opt/catalog
#   GIT_REPO=https://github.com/grossrc/Read-Digikey-DataMatrix.git
#   PYTHON_VERSION=3.13.2
# ============================================================================

set -Eeuo pipefail

# ---------- Editable defaults ----------
HOSTNAME="lab-parts"
APP_DIR="/opt/catalog"
GIT_REPO="https://github.com/grossrc/Read-Digikey-DataMatrix.git"

DB_USER="murph"
DB_PASS="password"
DB_NAME="parts_DB"

SERVICE_USER="pi"
PYTHON_VERSION="3.13.2"
PG_VERSION="17"

# ---------- Paths, logging, state ----------
STATE_DIR="/var/local/catalog-install"
STATE_FILE="${STATE_DIR}/state"
LOG_FILE="/var/log/catalog-install.log"
mkdir -p "$STATE_DIR"
sudo touch "$LOG_FILE" && sudo chown "${USER}:${USER}" "$LOG_FILE"

# Log everything to file + stdout
exec > >(tee -a "$LOG_FILE") 2>&1

# ---------- Utilities ----------
timestamp() { date +"%Y-%m-%d %H:%M:%S"; }

retry() {
  # retry <max_attempts> <sleep_seconds> -- cmd...
  local -i max="$1"; shift
  local -i sleep_sec="$1"; shift
  local -i attempt=1
  until "$@"; do
    if (( attempt >= max )); then
      echo "[${PWD}] $(timestamp) :: ERROR: '$*' failed after ${max} attempts"
      return 1
    fi
    echo "[${PWD}] $(timestamp) :: WARN: '$*' failed (attempt ${attempt}/${max}); retrying in ${sleep_sec}s..."
    sleep "${sleep_sec}"
    ((attempt++))
  done
}

have() { command -v "$1" >/dev/null 2>&1; }

mark_done() { grep -Fxq "$1" "$STATE_FILE" 2>/dev/null || echo "$1" >> "$STATE_FILE"; }
is_done() { grep -Fxq "$1" "$STATE_FILE" 2>/dev/null; }

run_step() {
  local name="$1"; shift
  if is_done "$name"; then
    echo "== Skipping ${name} (already completed)"
    return 0
  fi
  echo "== Running ${name}"
  if "$@"; then
    mark_done "$name"
    echo "== Completed ${name}"
  else
    echo "== FAILED ${name} (see ${LOG_FILE})"
    exit 1
  fi
}

on_exit() {
  local code=$?
  if (( code != 0 )); then
    echo
    echo "------------------------------------------------------------"
    echo "Installer exited with code ${code}. Last lines of ${LOG_FILE}:"
    echo "------------------------------------------------------------"
    tail -n 60 "$LOG_FILE" || true
    echo "------------------------------------------------------------"
    echo "Fix the issue and re-run the same script; it will resume."
  fi
}
trap on_exit EXIT

# Noninteractive apt
export DEBIAN_FRONTEND=noninteractive
APT_INSTALL="sudo apt-get -y --no-install-recommends install"
APT_UPDATE="sudo apt-get update -o Acquire::Retries=3"

# ---------- Prompt DigiKey creds once ----------
if ! grep -qE '^(DIGIKEY_CLIENT_ID|DIGIKEY_CLIENT_SECRET)=' "$STATE_DIR/creds" 2>/dev/null; then
  read -r -p "Enter DigiKey CLIENT_ID: " DIGIKEY_CLIENT_ID
  read -r -s -p "Enter DigiKey CLIENT_SECRET (input hidden): " DIGIKEY_CLIENT_SECRET
  echo
  {
    echo "DIGIKEY_CLIENT_ID=${DIGIKEY_CLIENT_ID}"
    echo "DIGIKEY_CLIENT_SECRET=${DIGIKEY_CLIENT_SECRET}"
  } > "$STATE_DIR/creds"
else
  # shellcheck disable=SC1091
  source "$STATE_DIR/creds"
fi

# ---------- Helper: upsert KEY=VALUE in .env ----------
set_kv() {
  local key="$1" val="$2"
  local esc_val
  esc_val="$(printf '%s' "$val" | sed 's/[&/]/\\&/g')"
  if [ -f .env ] && grep -qE "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${esc_val}|" .env
  else
    printf "%s=%s\n" "$key" "$val" >> .env
  fi
}

# ---------- Sanity checks ----------
run_step "00_network_check" bash -c '
  echo "Checking outbound network..."
  retry 5 3 ping -c1 -W3 8.8.8.8 >/dev/null
  retry 5 3 curl -fsSLI https://www.python.org >/dev/null
  retry 5 3 curl -fsSLI https://github.com >/dev/null
'

run_step "01_refresh_apt" bash -c '
  retry 5 5 '"$APT_UPDATE"'
  '"$APT_INSTALL"' ca-certificates curl gnupg lsb-release software-properties-common
'

run_step "02_hostname_mdns" bash -c '
  '"$APT_INSTALL"' avahi-daemon raspi-config
  sudo raspi-config nonint do_hostname "'"$HOSTNAME"'"
  echo "'"$HOSTNAME"'" | sudo tee /etc/hostname >/dev/null
  sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1   '"$HOSTNAME"'/g" /etc/hosts
  sudo systemctl enable --now avahi-daemon
  sudo systemctl restart avahi-daemon
'

run_step "03_enable_ssh" bash -c '
  '"$APT_INSTALL"' openssh-server
  if have raspi-config; then sudo raspi-config nonint do_ssh 0 || true; fi
  sudo systemctl enable --now ssh
  # authorize local key if present
  mkdir -p "${HOME}/.ssh"
  touch "${HOME}/.ssh/authorized_keys"
  chmod 700 "${HOME}/.ssh" && chmod 600 "${HOME}/.ssh/authorized_keys"
  for k in id_ed25519.pub id_rsa.pub id_ecdsa.pub; do
    if [ -f "${HOME}/.ssh/${k}" ]; then
      grep -q -F "$(cat "${HOME}/.ssh/${k}")" "${HOME}/.ssh/authorized_keys" || cat "${HOME}/.ssh/${k}" >> "${HOME}/.ssh/authorized_keys"
    fi
  done
'

run_step "04_sys_packages_postgres" bash -c '
  '"$APT_INSTALL"' git nginx build-essential make cmake pkg-config \
    zlib1g-dev libssl-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
    libncurses5-dev libncursesw5-dev xz-utils tk-dev liblzma-dev uuid-dev

  # PGDG repo for PostgreSQL
  echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" | \
    sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
  retry 5 5 curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | \
    sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg
  retry 5 5 '"$APT_UPDATE"'
  '"$APT_INSTALL"' postgresql-'"$PG_VERSION"' postgresql-client-'"$PG_VERSION"' libpq-dev
'

run_step "05_build_python" bash -c '
  PY_PREFIX="/opt/python-'"$PYTHON_VERSION"'"
  if [ ! -x "${PY_PREFIX}/bin/python3" ]; then
    cd /tmp
    retry 5 5 curl -fsSLo "Python-'"$PYTHON_VERSION"'.tar.xz" "https://www.python.org/ftp/python/'"$PYTHON_VERSION"'/Python-'"$PYTHON_VERSION"'.tar.xz"
    tar -xf "Python-'"$PYTHON_VERSION"'.tar.xz"
    cd "Python-'"$PYTHON_VERSION"'"
    ./configure --prefix="${PY_PREFIX}" --enable-optimizations --with-lto --enable-shared
    make -j"$(nproc)"
    sudo make install
    echo "${PY_PREFIX}/lib" | sudo tee /etc/ld.so.conf.d/python-'"$PYTHON_VERSION"'.conf >/dev/null
    sudo ldconfig
  else
    echo "Python '"$PYTHON_VERSION"' already present at ${PY_PREFIX}"
  fi
'

run_step "06_checkout_app" bash -c '
  sudo mkdir -p "'"$APP_DIR"'"
  sudo chown "${USER}:${USER}" "'"$APP_DIR"'"
  if [ ! -d "'"$APP_DIR"'/.git" ]; then
    retry 5 5 git clone "'"$GIT_REPO"'" "'"$APP_DIR"'"
  else
    retry 5 5 git -C "'"$APP_DIR"'" fetch --all --prune
    retry 5 5 git -C "'"$APP_DIR"'" pull --ff-only
  fi
'

run_step "07_venv_deps" bash -c '
  cd "'"$APP_DIR"'"
  PY="/opt/python-'"$PYTHON_VERSION"'/bin/python3"
  '"$PY_INSTALL_FIX"'
  "${PY}" -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python --version
  pip install -U pip wheel
  if [ -f requirements.txt ]; then
    retry 5 5 pip install -r requirements.txt
  fi
'

run_step "08_pg_bootstrap" bash -c '
  # Ensure service up
  sudo systemctl enable --now postgresql@'"$PG_VERSION"'-main || sudo systemctl enable --now postgresql || true

  sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '"'$DB_USER'"') THEN
    CREATE ROLE '"$DB_USER"' LOGIN PASSWORD '"'$DB_PASS'"';
  ELSE
    ALTER ROLE '"$DB_USER"' LOGIN PASSWORD '"'$DB_PASS'"';
  END IF;
END
\$\$;
SQL

  sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_database WHERE datname = '"'$DB_NAME'"') THEN
    CREATE DATABASE '"$DB_NAME"' OWNER '"$DB_USER"';
  END IF;
END
\$\$;
ALTER DATABASE '"$DB_NAME"' OWNER TO '"$DB_USER"';
SQL

  sudo -u postgres psql -d "'"$DB_NAME"'" -v ON_ERROR_STOP=1 -c "ALTER SCHEMA public OWNER TO '"$DB_USER"'; GRANT ALL ON SCHEMA public TO '"$DB_USER"';"
'

run_step "09_load_schema" bash -c '
  cd "'"$APP_DIR"'"
  export PGPASSWORD="'"$DB_PASS"'"
  DBURL="postgresql://'"$DB_USER"':'"$DB_PASS"'@localhost:5432/'"$DB_NAME"'"

  if [ ! -f deploy/schema.sql ]; then
    echo "No deploy/schema.sql found; skipping DB schema load."
    exit 0
  fi

  # 1) Pre-create schemas/extensions OUTSIDE functions/DO blocks
  #    Extract bare CREATE SCHEMA/EXTENSION lines that are not obviously inside a function/DO.
  #    (Heuristic: we grab top-level lines; anything else gets executed as-is later.)
  tmpdir="$(mktemp -d)"
  pre="${tmpdir}/pre.sql"
  main="${tmpdir}/main.sql"

  # Normalize newlines
  sed "s/\r$//" deploy/schema.sql > "${tmpdir}/schema.norm.sql"

  # Greedy but effective: pull CREATE SCHEMA/EXTENSION statements to run first.
  # You can expand this list if your file has more forbidden-in-function DDL.
  awk '
    BEGIN{IGN=0}
    /CREATE[[:space:]]+OR[[:space:]]+REPLACE[[:space:]]+FUNCTION|DO[[:space:]]+\$\$/ {IGN=1}
    IGN==1 {buf=buf $0 "\n"; if ($0 ~ /\$\$[[:space:]]*;[[:space:]]*$/) {IGN=0; print buf; buf="";} next}
    {print}
  ' "${tmpdir}/schema.norm.sql" > "${tmpdir}/schema.split.sql"

  # From the top-level portion, extract CREATE SCHEMA / CREATE EXTENSION statements
  grep -E "^[[:space:]]*CREATE[[:space:]]+SCHEMA|^[[:space:]]*CREATE[[:space:]]+EXTENSION" "${tmpdir}/schema.split.sql" > "${pre}" || true

  # Remove those lines from the main script to avoid re-running them
  if [ -s "${pre}" ]; then
    grep -v -E "^[[:space:]]*CREATE[[:space:]]+SCHEMA|^[[:space:]]*CREATE[[:space:]]+EXTENSION" "${tmpdir}/schema.norm.sql" > "${main}"
  else
    cp "${tmpdir}/schema.norm.sql" "${main}"
  fi

  echo "-- PRE DDL" > "${tmpdir}/pre.final.sql"
  # Ensure schemas are created with the right owner and are idempotent
  while read -r line; do
    case "$line" in
      (*"CREATE SCHEMA "*)
        sch=$(echo "$line" | sed -n "s/.*CREATE[[:space:]]\+SCHEMA[[:space:]]\+\([a-zA-Z0-9_]\+\).*/\1/p")
        [ -n "$sch" ] && echo "CREATE SCHEMA IF NOT EXISTS ${sch} AUTHORIZATION "'"$DB_USER"';" >> "${tmpdir}/pre.final.sql"
        ;;
      (*"CREATE EXTENSION "*)
        ext=$(echo "$line" | sed -n "s/.*CREATE[[:space:]]\+EXTENSION[[:space:]]\+\(IF NOT EXISTS[[:space:]]\+\)\{0,1\}\"\{0,1\}\([a-zA-Z0-9_\-]\+\)\"\{0,1\}.*/\2/p")
        [ -n "$ext" ] && echo "CREATE EXTENSION IF NOT EXISTS \"$ext\";" >> "${tmpdir}/pre.final.sql"
        ;;
    esac
  done < "${pre}"

  # Always ensure public schema ownership (safe to re-run)
  echo "ALTER SCHEMA public OWNER TO '"$DB_USER"'; GRANT ALL ON SCHEMA public TO '"$DB_USER"';" >> "${tmpdir}/pre.final.sql"

  # 2) Run pre-DDL and then the main schema file with ON_ERROR_STOP
  if [ -s "${tmpdir}/pre.final.sql" ]; then
    echo "== Running pre-DDL outside of functions/DO =="
    psql "$DBURL" -v ON_ERROR_STOP=1 -f "${tmpdir}/pre.final.sql"
  fi

  echo "== Running main schema =="
  psql "$DBURL" -v ON_ERROR_STOP=1 -f "${main}"

  unset PGPASSWORD
'


run_step "10_write_env" bash -c '
  cd "'"$APP_DIR"'"
  [ -f .env ] || cp deploy/.env.example .env 2>/dev/null || touch .env
  set_kv DIGIKEY_CLIENT_ID      "'"$DIGIKEY_CLIENT_ID"'"
  set_kv DIGIKEY_CLIENT_SECRET  "'"$DIGIKEY_CLIENT_SECRET"'"
  set_kv PGHOST     "localhost"
  set_kv PGPORT     "5432"
  set_kv PGDATABASE "'"$DB_NAME"'"
  set_kv PGUSER     "'"$DB_USER"'"
  set_kv PGPASSWORD "'"$DB_PASS"'"
  set_kv FLASK_DEBUG "0"
  set_kv DATABASE_URL "postgresql://'"$DB_USER"':'"$DB_PASS"'@localhost:5432/'"$DB_NAME"'"
  echo "== .env summary =="
  grep -E "^(DIGIKEY_|PGHOST|PGPORT|PGDATABASE|PGUSER|PGPASSWORD|DATABASE_URL|FLASK_DEBUG)=" .env || true
'

run_step "11_systemd_gunicorn" bash -c '
  cd "'"$APP_DIR"'"
  sudo tee /etc/systemd/system/catalog.service >/dev/null <<EOF
[Unit]
Description=Catalog Flask App (gunicorn)
Wants=network-online.target
After=network-online.target postgresql@'"$PG_VERSION"'-main.service

[Service]
User='"$SERVICE_USER"'
Group=www-data
WorkingDirectory='"$APP_DIR"'
EnvironmentFile='"$APP_DIR"'/.env
ExecStartPre=/bin/sh -c '\''for i in $(seq 1 60); do /usr/bin/pg_isready -q -h 127.0.0.1 -p 5432 && exit 0; sleep 1; done; exit 1'\''
ExecStart='"$APP_DIR"'/.venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 app:app
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
  sudo systemctl --no-pager status catalog || true
'

run_step "12_verify_local" bash -c '
  ss -lntp | grep -q ":5000" || (echo "gunicorn not listening on 5000 yet" && false)
  retry 10 3 curl -fsSI http://127.0.0.1:5000/ >/dev/null
'

run_step "13_nginx" bash -c '
  sudo ln -sf "'"$APP_DIR"'/UI Pages" "'"$APP_DIR"'/ui_pages" || true
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo tee /etc/nginx/sites-available/catalog >/dev/null <<EOF
server {
    listen 80 default_server;
    server_name '"$HOSTNAME"'.local 127.0.0.1 127.0.1.1 localhost _;

    location /static/ {
        alias '"$APP_DIR"'/ui_pages/;
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
  sudo ln -sf /etc/nginx/sites-available/catalog /etc/nginx/sites-enabled/catalog
  sudo nginx -t
  sudo systemctl reload nginx
'

run_step "14_optional_kiosk" bash -c '
  if have raspi-config; then sudo raspi-config nonint do_boot_behaviour B4 || true; fi
  '"$APT_INSTALL"' chromium-browser dos2unix || true
  have chromium-browser || '"$APT_INSTALL"' chromium || true
  cd "'"$APP_DIR"'"
  [ -f deploy/kiosk-start.sh ] && dos2unix deploy/kiosk-start.sh || true
  [ -f deploy/kiosk-start.sh ] && sudo install -o "'"$USER"'" -g "'"$USER"'" -m 0755 deploy/kiosk-start.sh /opt/kiosk-start.sh || true
  mkdir -p "${HOME}/.config/autostart"
  tee "${HOME}/.config/autostart/catalog-kiosk.desktop" >/dev/null <<'EOF'
[Desktop Entry]
Type=Application
Name=Catalog Kiosk
Exec=/opt/kiosk-start.sh
X-GNOME-Autostart-enabled=true
X-LXQt-Need-Tray=false
EOF
  rm -rf "${HOME}/.config/chromium/Singleton"* "${HOME}/.config/chromium/Crash Reports" 2>/dev/null || true
'

echo
echo "===================================================================="
echo "DONE!"
echo " - App URL:      http://${HOSTNAME}.local/catalog   (or the Pi's IP)"
echo " - SSH:          enabled (service 'ssh')"
echo " - Python:       ${PYTHON_VERSION}"
echo " - Service:      sudo systemctl status catalog"
echo " - Log:          ${LOG_FILE}"
echo " - State:        ${STATE_FILE}"
echo "Reboot recommended to finalize mDNS/desktop bits: sudo reboot"
echo "If anything broke, fix it and re-run ./install.sh — it resumes."
echo "===================================================================="
# Auto-reboot with countdown
countdown=10
while [ "$countdown" -gt 0 ]; do
  printf "\rRebooting to finalize setup in %d" "$countdown"
  sleep 1
  countdown=$((countdown - 1))
done
echo
sudo reboot



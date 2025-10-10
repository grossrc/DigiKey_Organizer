## DigiKey Part Organizer
<a href="https://www.buymeacoffee.com/ryonicle" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>

This program enables users to *scan, store, browse, and check out* DigiKey packages using a local storage system. It provides an easy way to track available parts, reuse components in future projects, and maintain a clear view of inventory. The system’s main goal is to simplify part management, minimize waste, and reduce costs.

# Scanning (stocking)

Every DigiKey package comes with a DataMatrix printed on the package which resembles a QR code. Embedded in this code is various information like the part_#, quantity, or lot code. The program scans this code (or you can input the part_# manually) and uses it to retrieve additional information about the part from DigiKey. You tell the program what bin/location you are storing it in, and it's input into the Database.

![alt text](scan.gif)

# Searching Inventory
A core aspect of the program is the ability to access this inventory in a way that's useful. The Pi stores all the categorical and stock-specific information in the database.The Pi also serves as a locally hosted platform that you can access on the local network to see what's in stock. Once you have it up and running, you simply go to http://lab-parts.local/catalog to see everything currently stocked. Click 'Add to List' for every part you intend to use in your project. When you're done, you'll be able to download a text file with every local part you selected. Alternatively, you can download the list as a QR code which the system will use to checkout each part step-by-step.

![alt text](browse.gif)

# Checkout

Parts can be checked out out manually or through a guided process. During checkout, the downloaded QR code is taken over to the storage hub and scanned. The program identifies each part and guides you to where it's located. This streamlines the process of getting your desired parts, and encourages reuse when possible.

![alt text](checkout.gif)

### Notes:
My lab specifically uses DigiKey parts, so as of now this program only works with those parts and the DigiKey associated DataMatrix code/API. However, I know many labs- especially larger ones- use other wholesalers like Mouser, RS Components, etc. so this would be a good future contribution to the project. As far as I know, you're still able to input the MFR_part_# from other sites into the system, but it will be referenced through DigiKey's API. If the part doesn't exist in the DigiKey system, it's handling is not garanteed through the DB (give it a shot - I'm curious).

## Project Requirements
- Raspberry Pi4 or Pi5
- 7" Pi Touchscreen
- Webcam (you might have to manually adjust the focus to the optimal distance)
- Storage Locations
    - Any storage scheme with labelling to denote a part's location works. The program just requires an arbitrary "location" input to catalog the part. It's a text input, so you could put "Steve's house" as the location for all it cares. However, it's probably more useful to input *A1, A2, ..., B1, B2, ..., etc.* or something recognizable so you know where the location is.
- Keyboard (optional- useful when inputting part_#s manually)
- DigiKey API credentials (see ## DigiKey API below)

# Installing the system one-shot
This method automatically deploys the program on your raspberry Pi. It pulls all the necessary code and schema which then installs itself on the Pi's system. This method is simpler, but it's tougher to debug if anything goes wrong. You will be prompted for your DigiKey api credentials at the start, so have this ready. All other values will be used as the default (database username, password, etc.) so you don't need to change any of that.
On a fresh Raspberry Pi running 64bit OS, paste the following line in the terminal. The installation process will take ~10 minutes, and when done, your program should be running on boot.
```
bash -c '
set -euo pipefail
sudo apt-get update
sudo apt-get install -y git
sudo mkdir -p /opt/catalog
sudo chown "$USER":"$USER" /opt/catalog
git clone https://github.com/grossrc/DigiKey_Organizer.git /opt/catalog
cd /opt/catalog
chmod +x deploy/install.sh
./deploy/install.sh
'
```


# Installing the System (step-by-step)
## 1. Set the Pi’s local hostname (mDNS)
```
sudo apt update
sudo apt -y install avahi-daemon
sudo raspi-config nonint do_hostname lab-parts

# Make sure the files reflect it (safety)
echo "lab-parts" | sudo tee /etc/hostname
sudo sed -i 's/^127\.0\.1\.1.*/127.0.1.1   lab-parts/' /etc/hosts

# Apply immediately
sudo systemctl enable --now avahi-daemon
sudo systemctl restart avahi-daemon
```
This makes the Pi reachable at http://lab-parts.local/catalog without any router changes or DNS fiddling. If .local doesn’t resolve on a particular machine, you can always use the Pi’s IP address as the URL. As a quick sanity check, run *hostname* to ensure it returns *lab-parts*.

## 2. Install system packages
```
# Install Python, Git, nginx, and PostgreSQL 17
sudo apt -y install python3-venv python3-pip git nginx curl ca-certificates gnupg lsb-release

echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" | \
  sudo tee /etc/apt/sources.list.d/pgdg.list
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | \
  sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg
sudo apt update

# Install PostgreSQL 17 and dev headers (DO NOT USE V15 IT WILL NOT WORK!)
sudo apt -y install postgresql-17 postgresql-client-17 libpq-dev
```
This installs:
- Python and venv support
- Git (for automatically downloading the program)
- nginx for server hosting
- Postgresql as the database. Check is it's running after install with *sudo systemctl status postgresql* 
- Dependencies for postgresql querying
Check if postgresql is running with *sudo systemctl status postgresql*

## 3. Pull the program code and set up a Python environment
```
sudo mkdir -p /opt/catalog && sudo chown "$USER":"$USER" /opt/catalog
git clone https://github.com/grossrc/Read-Digikey-DataMatrix.git /opt/catalog
cd /opt/catalog

# Create venv (use explicit interpreter if you’ve pinned Python)
python3 -m venv .venv
source .venv/bin/activate
python --version   # sanity check
pip install -U pip wheel
pip install -r requirements.txt
```

## 4. Setup the local database schema

You must create the database within postgresql for all the part information to be stored in. Make the password the default 'password' or you must reflect your password in the subsequent step and env file. 
A Database called 'parts_DB' under the user murph is created (don't leave me murph!).
```
sudo -u postgres createuser -P murph # will prompt for password
sudo -u postgres createdb -O murph parts_DB
```

Load the schema into the DB with the following line. Replace the word password with your actual password if you didn't use the default "password" as your password.
```
psql "postgresql://murph:password@localhost:5432/parts_DB" -v ON_ERROR_STOP=1 -f deploy/schema.sql
```

## 5. Input your .env variables
```
[ -f .env ] || cp deploy/.env.example .env
nano .env
```
Copy over your digikey credentials and database credentials if different from the default.

## 6. Run the app with gunicorn (via systemd)

Create the systemd service:
```
sudo tee /etc/systemd/system/catalog.service >/dev/null <<'EOF'
[Unit]
Description=Catalog Flask App (gunicorn)
Wants=network-online.target
After=network-online.target postgresql@17-main.service

[Service]
User=pi
Group=www-data
WorkingDirectory=/opt/catalog
EnvironmentFile=/opt/catalog/.env

# wait for Postgres to be ready (up to ~60s)
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do /usr/bin/pg_isready -q -h 127.0.0.1 -p 5432 && exit 0; sleep 1; done; exit 1'

ExecStart=/opt/catalog/.venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 app:app

# nicer logging + startup tolerance
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
Restart=always
RestartSec=3
TimeoutStartSec=90

[Install]
WantedBy=multi-user.target
EOF
```
then run
```
sudo systemctl daemon-reload
sudo systemctl enable --now catalog
```

(optional) VERIFY:
Check the output to ensure it's running.
```
sudo systemctl status catalog --no-pager # should return a green 'running' status
ss -lntp | grep 5000        # should show gunicorn on 127.0.0.1:5000
curl -I http://127.0.0.1:5000/   # expect 200 OK (server: gunicorn)
```

## 7. Configure nginx reverse proxy

This is used to proxy port 5000 (used by the program to host the platform via gunicorn) from port 80 (the default http port open to traffic)
```
sudo ln -sf "/opt/catalog/UI Pages" /opt/catalog/ui_pages #learn_from_my_mistakes_and_use_underscores

# Disable the default site so it can't catch requests
sudo rm -f /etc/nginx/sites-enabled/default

# Install your site and make it the default server
sudo tee /etc/nginx/sites-available/catalog >/dev/null <<'EOF'
server {
    listen 80 default_server;
    server_name lab-parts.local 127.0.0.1 127.0.1.1 localhost _;

    # Static files (your Flask static_url_path="/static")
    location /static/ {
        alias /opt/catalog/ui_pages/;
        expires 30d;
        access_log off;
    }

    # Everything else -> gunicorn on localhost
    location / {
        include proxy_params;
        proxy_pass http://127.0.0.1:5000;
        client_max_body_size 10m;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/catalog /etc/nginx/sites-enabled/catalog
sudo nginx -t && sudo systemctl reload nginx
```

## 8. Configure UI to automatically open fullscreen on boot

This will open the UI in a fullscreen Chromium window every time the Pi boots into the desktop, pointing at the local server URL.

```
# A) Make sure the Pi logs into the desktop automatically (needed for kiosk)
sudo raspi-config nonint do_boot_behaviour B4   # "Desktop Autologin"

# B) Install Chromium & curl (if not already)
sudo apt -y install chromium-browser curl

# C) Install the kiosk launcher script from the repo to /opt with proper perms
sudo apt -y install dos2unix && dos2unix deploy/kiosk-start.sh
sudo install -o "$USER" -g "$USER" -m 0755 deploy/kiosk-start.sh /opt/kiosk-start.sh

# D) Create a desktop autostart entry so it runs when the desktop loads
mkdir -p ~/.config/autostart
tee ~/.config/autostart/catalog-kiosk.desktop >/dev/null <<'EOF'
[Desktop Entry]
Type=Application
Name=Catalog Kiosk
Exec=/opt/kiosk-start.sh
X-GNOME-Autostart-enabled=true
X-LXQt-Need-Tray=false
EOF

# E) (Once) clear any old Chromium locks from previous attempts (harmless if none)
rm -rf ~/.config/chromium/Singleton* ~/.config/chromium/"Crash Reports" 2>/dev/null || true

# F) Reboot to test
countdown=10
  while [ "$countdown" -gt 0 ]; do
    printf "\rRebooting to finalize setup in %d" "$countdown"
    sleep 1
    countdown=$((countdown - 1))
  done
echo
sudo reboot
```
After rebooting, open the app from any device on your LAN (preferably desktop) at
http://lab-parts.local/catalog

## 9. Support this project

If this project is useful to you, and if it saves you money, please share the love at:
https://www.buymeacoffee.com/ryonicle

# Updating the codebase
The project code will change periodically to fix bugs and make improvements. This section outlines how to simply update your system to the latest version. To keep your running instance healthy you perform an **Update + Migrate + Re‑index** sequence. This preserves all part IDs, locations, and movement history while bringing old rows up to date with the latest decoder logic and schema. The codebase is also brought up to the most recent version.

SSH into the Pi (enable SSH first via raspi-config if needed):
```
ssh pi@lab-parts.local
```
Run the one‑shot update sequence below. It is safe to re-run; each step is idempotent.
```
cd /opt/catalog
set -euo pipefail

# env + DB check
set -a; [ -f .env ] && . ./.env; set +a
psql -v ON_ERROR_STOP=1 -c 'select 1;' >/dev/null

# 1. Pull newest code & dependencies
git pull
source .venv/bin/activate
pip install -r requirements.txt

# 2. Apply any pending schema migrations (new columns / indexes)
#    Each *.sql file in deploy/migrations is meant to be run exactly once.
#    However, they are written such that re-running should be harmless.
for f in deploy/migrations/*.sql; do
  echo "Applying migration: $f"; \
  psql "$DB_URL" -v ON_ERROR_STOP=1 -f "$f"; \
  echo "Done: $f"; \
  echo
done

# 3. Re-index existing parts so older rows gain any new category/attribute logic
python reformat.py --dry-run

# 4. If dry-run looks reasonable, APPLY changes (will also backfill new columns):
python reformat.py --cleanup-unused-categories

# 5. Restart services so running app code matches the updated library code
sudo systemctl restart catalog
sudo systemctl reload nginx
```

### Why the re-index step is now mandatory
Older parts were stored using earlier decoding rules. Without re-indexing:
 - New hierarchical category path columns (e.g. `category_path`) would be NULL.
 - Improved attribute extraction / unknown parameter isolation won’t appear.
 - Analytics or UI features depending on new columns won’t see legacy parts.

Re-indexing is **in-place** and preserves:
 - `part_id`, creation timestamps, intake & movement history, quantities
 - Raw vendor payload (`raw_vendor_json`) for reproducibility

It updates:
 - Category id, source name, full hierarchical path
 - Attributes & unknown parameters according to newest profiles
 - (Optionally) lifecycle flags & price unless you override flags in the script

### Advanced / selective usage
If you have a very large DB and want to stage the upgrade:
```
python reformat.py --dry-run --limit 200
python reformat.py --limit 200
```
Repeat in batches, then run a final pass without --limit to catch any remainder.

### Verifying after update
```
psql "$DB_URL" -c "\d+ public.parts" | grep category_path
psql "$DB_URL" -c "SELECT count(*) FILTER (WHERE category_path IS NULL) AS missing_path FROM public.parts;"
```
`missing_path` should be 0 after a successful re-index.

# Troubleshooting
- If the program boots up, but you're having trouble scanning in parts, this is probably an issue with your API credentials. I included a Digikey_API_TEST.py script which you can use to input your credentials (or allow them to be pulled directly from the .env file) and ensure you get a proper return. If not- this is an issue with DigiKey API or your credentials.
- If the program boots up, but you cannot access lab-parts.local/catalog, this might mean that your database isn't connected properly. Make sure you didn't accidently change any of the default .env variables for the DB. If you did, make sure that they line up. Some devices might also fail to resolve the mDNS .local address. Make sure to try this on a few devices (not mobile) first.
  - If you ever need to change the API credentials you need to change the .env file located in /opt/catalog. Do this by running ```nano /opt/catalog/.env```
- If your program does not boot up at all, use the verification in step 6 to see if the server is actually running. If it's running and you don't see anything then it's likely an issue in step 8. If there's a server issue, then check the logs to see what caused the failure.
- For the sake of time, I chose not explicitly require the python version 3.13.2 the program was written on. However, this always has the risk of causing dependency issues in the future if Python depreciates libraries I use in here.

# Technical Points & Design Decisions

## Part categorization
The parts are categorized via a decoder (dk_decoder.py) whose categories are referenced from the /profiles folder. This creates a robust categorizing scheme from the raw JSON returned by the DigiKey API which is cumbersome. While the API does return the part's category, the prescense or non-prescence of subcategories and other parameters made this a beast to tackle. Mainly, the relevant parameters change depending on what the part is (e.g. The tolerance parameter is relevant for a resistor, the sample speed for an ADC, the forward voltage for a diode, etc). The dk_decoder file invoked in the main script to reference and pull speciific parameters depending on the anticipated, expected, common categories of parts. Unknown parts/parameters should be handled appropriatelty and listed as such.

## .yaml profile categorization
These are the lookup tables to be used for categorization and relevant parameters. For enhanced care to the categorization of the parts, this will likely have to be built out further. Each profile is outfitted with what appears to be (at the present moment) a few catch-all qualifiers to put it in the proper category. However, I noticed parts that should've been in a category but were not due to a mismatch between the yaml files' source_categorie parameter and the actual listed category. For example, there's an adc.yaml profile but if the part is categorized by a name (e.g. "High Speed Analog to Digital Converter") it might struggle to find its way into this profile with the relevant attributes extracted. If this is noticed, simply go into adc.yaml and add  "High Speed Analog to Digital Converter" to the source_categories and you're good to go.

## DigiKey API
A (free) DigiKey API is required for operating this program. While the Pi services the local Network and the packages are scanned directly, the API is needed for additional part information. This is mostly used for categorization, and, by extension, the program's search functionality via the catalog.

## Getting your Credentials
A Client_ID and Client_Secret are what's used by the DigiKey API to verify queries. To get these parameters from your DigiKey account, you must go to https://developer.digikey.com/ and create an organization/project/production app. The only specific API used in this program is "ProductInformation V4", so make sure this is what's selected when creating your production app. Once created, you should have credentials which can be copied over to your .env file. This file will be referenced by the program anytime your credentails are needed.

## Part List Filter
The filter in the top left corner of the list page allows users to filter the part number/description/attribute. This is useful to providing useful accounting of what's available and to quickly verify if a desired part is in stock. This is just a catch-all search where the list will display everything with some level of matching. To narrow the search further, type more attributes for a more refined view. The search is built with AND search capability, but can be adapted for OR searching if desired (you'd need to impliment this yourself in the code).
Examples
- Query: "0402 20k"
  - AND → only rows with both “0402” and “20k” (good for targeted filtering).
  - OR → rows with either “0402” or “20k” (can be too many results).
- Query: "led 3mm"
  - AND → 3mm LEDs (likely desired).
  - OR → all LEDs and all 3mm parts (noisy).


# Updates
## (2025-10-08)
Solved some categorization issues with duplicated or changing categories for parts whose .yaml file is not yet created. Also added `reformat.py`, a maintenance script that re-indexes all existing parts from their stored `raw_vendor_json` using the current decoder/profile logic, updating categories and attributes in-place while preserving part IDs, creation timestamps, and inventory history. Run it with `--dry-run` first to preview changes before applying them.




[![Watch the video](https://img.youtube.com/vi/4L8dW_dunqc/hqdefault.jpg)](https://youtu.be/4L8dW_dunqc)

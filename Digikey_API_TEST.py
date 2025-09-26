'''
This is a simple test script to test your API credentials. From this, you'll
be able to determine if it's you API connection causing issues or if it's 
something else.
'''

from __future__ import annotations

# --- Standard library ---
import os
import time
import json
from pathlib import Path
from urllib.parse import quote
from contextlib import closing
from typing import Any, Iterable

# --- Third-party libraries ---
import requests
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from dotenv import load_dotenv

# --- Local modules ---
from Code_Scanner import scan_part
import dk_decoder
from dk_decoder import load_registry, decode_product  # (and extract_offers if needed)
from db_helper import get_conn, run_query

# --- Reload dk_decoder in case it’s under active development ---
import importlib
importlib.reload(dk_decoder)

# --- Load environment variables ---
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

# --- DigiKey API constants ---
CLIENT_ID = os.getenv("DIGIKEY_CLIENT_ID") # HARD-CODE THIS IF YOU JUST WANT TO TEST
CLIENT_SECRET = os.getenv("DIGIKEY_CLIENT_SECRET") # HARD-CODE THIS IF YOU JUST WANT TO TEST
DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_BASE = "https://api.digikey.com/products/v4"

_token_cache = {"token": None, "expires_at": 0.0}

# --- Load registry (profiles + traits) ---
HERE = Path(__file__).resolve().parent
REGISTRY = load_registry(
    profiles_dir=str(HERE / "profiles"),
    traits_path=str(HERE / "traits.yaml"),
)


def get_access_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 15:
        return _token_cache["token"]

    resp = requests.post(
        DIGIKEY_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    token = data.get("access_token")
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + float(data.get("expires_in", 600))
    return token

def productdetails(product_number):
    token = get_access_token()
    url = f"{DIGIKEY_BASE}/search/{quote(str(product_number))}/productdetails"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": CLIENT_ID,
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD",
        "X-DIGIKEY-Customer-Id": "0",
        "Accept": "application/json",
    }
    return requests.get(url, headers=headers, timeout=30)

def call_DigiKey_API(part_number):

    resp = productdetails(part_number)
    print(f"HTTP {resp.status_code}")

    try:
        dk_json = resp.json()
    except Exception:
        dk_json = None

    if dk_json is None:
        print("No JSON returned from Digi-Key.")
        return None

    # OPTIONAL: uncomment to see raw payload
    # print(json.dumps(dk_json, indent=2))

    return dk_json


def main():
    MFR_PN = '1965-ESP32-S3-WROOM-1U-N4CT-ND'
    dk_json = call_DigiKey_API(MFR_PN)  # Call DigiKey API and return JSON
    if dk_json:
        print('----Digikey Return-----')
        print(dk_json)
    else:
        print("API call failed.")

if __name__ == "__main__":
    main()
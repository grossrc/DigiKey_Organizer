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
import sys

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
    # Allow passing a part number on the command line for quick testing
    MFR_PN = sys.argv[1] if len(sys.argv) > 1 else 'L1CU-FRD1000000000'
    dk_json = call_DigiKey_API(MFR_PN)  # Call DigiKey API and return JSON
    if dk_json:
        print('\n---- DigiKey raw JSON (truncated pretty) -----')
        try:
            print(json.dumps(dk_json, indent=2))
        except Exception:
            # Fallback if not JSON-serializable for some reason
            print(repr(dk_json))

        # Decode product using the registry to get category + attributes
        decoded = decode_product(dk_json, REGISTRY)

        print('\n---- Decoded category & profile -----')
        cat_id = decoded.get('category_id')
        cat_name = decoded.get('category_source_name')
        print(f"Category id: {cat_id!s}")
        print(f"Category name: {cat_name!s}")

        # Profile info
        if cat_id and cat_id in REGISTRY:
            prof = REGISTRY.get(cat_id) or {}
            print(f"Matched profile id: {prof.get('id')}")
            if prof.get('version'):
                print(f"Profile version: {prof.get('version')}")
            sc = prof.get('source_categories') or prof.get('source_category_patterns') or []
            print(f"Profile source categories/patterns: {len(sc)} entries")
        else:
            print('No matching profile found for this category (unknown_other or unmatched).')
            # Provide hints: try to find candidate profiles by substring or pattern match
            if cat_name:
                import re
                candidates = []
                for pid, prof in REGISTRY.items():
                    # check explicit source_categories
                    for s in (prof.get('source_categories') or []):
                        try:
                            if isinstance(s, str) and cat_name.lower() in s.lower():
                                candidates.append(pid); break
                        except Exception:
                            pass
                    # check source_category_patterns (regex)
                    for pat in (prof.get('source_category_patterns') or []):
                        try:
                            if re.search(pat, cat_name, re.I):
                                candidates.append(pid); break
                        except Exception:
                            pass
                if candidates:
                    print('\nProfile candidates based on category name:')
                    for c in sorted(set(candidates)):
                        p = REGISTRY.get(c) or {}
                        print(f" - {c}: {p.get('source_categories') or p.get('source_category_patterns')}")

        print('\n---- Extracted attributes -----')
        attrs = decoded.get('attributes') or {}
        if attrs:
            try:
                print(json.dumps(attrs, indent=2))
            except Exception:
                print(attrs)
        else:
            print('(no attributes extracted)')

        print('\n---- Unknown / unmapped vendor parameters -----')
        unknown = decoded.get('unknown_parameters') or {}
        if unknown:
            try:
                print(json.dumps(unknown, indent=2))
            except Exception:
                print(unknown)
        else:
            print('(no unknown parameters)')
    else:
        print("API call failed.")

if __name__ == "__main__":
    main()
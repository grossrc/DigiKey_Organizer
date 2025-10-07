"""
Overview
This program's main function is to take the raw scanned data invoked from Code_Scanner.py and
query the DigiKey API to get more relevant and deeper information about the part. This information
is also referenced by the dk_decoder.py program (see that file's description) to internally categorize
based on relevant parameters. Most of the returned info from the API is irrelevant, so this script- in
conjunction with the dk_decoder.py file- prepares the data to be cleanly inserted into the DB.
"""

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
CLIENT_ID = os.getenv("DIGIKEY_CLIENT_ID")
CLIENT_SECRET = os.getenv("DIGIKEY_CLIENT_SECRET")
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
    # Percent-encode the product number so embedded '/' characters become '%2F'
    url = f"{DIGIKEY_BASE}/search/{quote(str(product_number), safe='')}/productdetails"
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

def call_DigiKey_API(fields):
    part_number = fields.get("digikey_part_number") or fields.get("mfr_part_number")
    if not part_number:
        print("No usable part number found.")
        return None

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

def normalize_and_save(dk_json, scan_fields, REGISTRY=None,
                       decode_product=None, derive_lifecycle_flags=None, derive_unit_price=None):
    """
    Normalize Digi-Key JSON + scan fields and index into the DB.
    Returns: {"part_id": int, "intake_id": int, "position": str, "mpn": str}
    """

    # --- dependency fallbacks (so callers can omit these) ---
    if decode_product is None:
        from dk_decoder import decode_product as _decode_product
        decode_product = _decode_product
    if derive_lifecycle_flags is None:
        derive_lifecycle_flags = globals().get("derive_lifecycle_flags")
    if derive_unit_price is None:
        derive_unit_price = globals().get("derive_unit_price")

    # --- decode & field prep ---
    decoded = decode_product(dk_json, REGISTRY)
    prod = dk_json.get("Product", {}) or {}
    lifec = derive_lifecycle_flags(prod)
    unit_price = derive_unit_price(prod)

    quantity_scanned = scan_fields.get("quantity") or scan_fields.get("qty")
    invoice_number = scan_fields.get("invoice") or scan_fields.get("invoice_number")
    lot_code = scan_fields.get("lot_code") or scan_fields.get("lot")
    sales_order = scan_fields.get("sales_order") or scan_fields.get("so") or scan_fields.get("sales_order_number")

    header = {
        "mpn": prod.get("ManufacturerProductNumber"),
        "manufacturer": (prod.get("Manufacturer") or {}).get("Name"),
        "description": ((prod.get("Description") or {}).get("ProductDescription") or "").strip(),
        "detailed_description": ((prod.get("Description") or {}).get("DetailedDescription") or "").strip(),
        "product_url": prod.get("ProductUrl"),
        "datasheet_url": prod.get("DatasheetUrl"),
        "image_url": prod.get("PhotoUrl"),
        "unit_price": unit_price,
        **lifec,
    }
    if not header["mpn"]:
        raise ValueError("No MPN found in payload; refusing DB insert.")

    position = (
        scan_fields.get("Part_Cataloged_Position")
        or scan_fields.get("part_cataloged_position")
        or scan_fields.get("position")
        or scan_fields.get("bin")
        or "UNASSIGNED"
    )

    with closing(get_conn()) as conn:
        with conn:  # commit on success, rollback on exception
            with conn.cursor() as cur:
                # 1) Upsert category
                cat_id = decoded.get("category_id")
                cat_name = decoded.get("category_source_name")
                if cat_id:
                    cur.execute(
                        """
                        INSERT INTO categories (category_id, source_name)
                        VALUES (%s, %s)
                        ON CONFLICT (category_id)
                        DO UPDATE SET source_name = EXCLUDED.source_name;
                        """,
                        (cat_id, cat_name),
                    )

                # 2) Upsert part and get part_id
                cur.execute(
                    """
                    INSERT INTO parts (
                        mpn, manufacturer, description, detailed_description,
                        product_url, datasheet_url, image_url,
                        unit_price, product_status, lifecycle_active, lifecycle_obsolete,
                        category_id, category_source_name,
                        attributes, unknown_parameters, raw_vendor_json
                    ) VALUES (
                        %(mpn)s, %(manufacturer)s, %(description)s, %(detailed_description)s,
                        %(product_url)s, %(datasheet_url)s, %(image_url)s,
                        %(unit_price)s, %(product_status)s, %(lifecycle_active)s, %(lifecycle_obsolete)s,
                        %(category_id)s, %(category_source_name)s,
                        %(attributes)s, %(unknown_parameters)s, %(raw_vendor_json)s
                    )
                    ON CONFLICT (mpn) DO UPDATE SET
                        manufacturer          = EXCLUDED.manufacturer,
                        description           = EXCLUDED.description,
                        detailed_description  = EXCLUDED.detailed_description,
                        product_url           = EXCLUDED.product_url,
                        datasheet_url         = EXCLUDED.datasheet_url,
                        image_url             = EXCLUDED.image_url,
                        unit_price            = EXCLUDED.unit_price,
                        product_status        = EXCLUDED.product_status,
                        lifecycle_active      = EXCLUDED.lifecycle_active,
                        lifecycle_obsolete    = EXCLUDED.lifecycle_obsolete,
                        category_id           = EXCLUDED.category_id,
                        category_source_name  = EXCLUDED.category_source_name,
                        attributes            = EXCLUDED.attributes,
                        unknown_parameters    = EXCLUDED.unknown_parameters,
                        raw_vendor_json       = EXCLUDED.raw_vendor_json,
                        updated_at            = now()
                    RETURNING part_id;
                    """,
                    {
                        **header,
                        "category_id": cat_id,
                        "category_source_name": cat_name,
                        "attributes": Json(decoded.get("attributes") or {}),
                        "unknown_parameters": Json(decoded.get("unknown_parameters") or {}),
                        "raw_vendor_json": Json(dk_json),
                    },
                )
                part_id = cur.fetchone()[0]

                # 3) Ensure location exists (FK)
                cur.execute(
                    "INSERT INTO locations (position_code) VALUES (%s) ON CONFLICT DO NOTHING;",
                    (position,),
                )

                # 4) Insert intake (event) and RETURN id
                cur.execute(
                    """
                    INSERT INTO intakes (
                        part_id,
                        quantity_scanned, unit_price, currency,
                        invoice_number, sales_order, lot_code, date_code,
                        customer_reference, digikey_part_number, manufacturer_part_number,
                        packing_list_number, country_of_origin, label_type, internal_part_id,
                        raw_scan_fields, part_cataloged_position
                    ) VALUES (
                        %(part_id)s,
                        %(quantity_scanned)s, %(unit_price)s, %(currency)s,
                        %(invoice_number)s, %(sales_order)s, %(lot_code)s, %(date_code)s,
                        %(customer_reference)s, %(digikey_part_number)s, %(manufacturer_part_number)s,
                        %(packing_list_number)s, %(country_of_origin)s, %(label_type)s, %(internal_part_id)s,
                        %(raw_scan_fields)s, %(position)s
                    )
                    RETURNING intake_id;
                    """,
                    {
                        "part_id": part_id,
                        "quantity_scanned": int(quantity_scanned or 0),
                        "unit_price": unit_price,
                        "currency": "USD",
                        "invoice_number": invoice_number,
                        "sales_order": sales_order,
                        "lot_code": lot_code,
                        "date_code": scan_fields.get("date_code"),
                        "customer_reference": scan_fields.get("customer_reference"),
                        "digikey_part_number": scan_fields.get("digikey_part_number"),
                        "manufacturer_part_number": scan_fields.get("manufacturer_part_number")
                            or scan_fields.get("mfr_part_number"),
                        "packing_list_number": scan_fields.get("packing_list_number"),
                        "country_of_origin": scan_fields.get("country_of_origin"),
                        "label_type": scan_fields.get("label_type"),
                        "internal_part_id": scan_fields.get("internal_part_id"),
                        "raw_scan_fields": Json(scan_fields),
                        "position": position,
                    },
                )
                intake_id = cur.fetchone()[0]

                # 5) Also append to movements ledger (so availability reflects scan intake)
                #    - quantity_delta is positive for intake
                #    - if movements table doesn't exist yet, silently skip
                qty = int(quantity_scanned or 0)
                if qty > 0:
                    try:
                        cur.execute(
                            """
                            INSERT INTO public.movements (part_id, position_code, quantity_delta, movement_type, note)
                            VALUES (%s, %s, %s, 'intake', 'Scan intake')
                            """,
                            (part_id, position, qty),
                        )
                    except Exception:
                        # movements table not present or other non-critical issue; skip to keep intake successful
                        pass

    # Return details for the UI
    return {
        "part_id": part_id,
        "intake_id": intake_id,
        "position": position,
        "mpn": header["mpn"],
    }

def derive_lifecycle_flags(prod: dict) -> dict:
    """
    Return a small, consistent set of lifecycle flags for the UI/DB.

      product_status: free-text status from Digi-Key payload if present
      lifecycle_active: True if looks "active"/"production"
      lifecycle_obsolete: True if looks "obsolete"/"EOL"/"NRND"
    """
    status = None

    # Try a few common shapes seen in Digi-Key payloads
    ps = prod.get("ProductStatus")
    if isinstance(ps, dict):
        # Try common keys
        status = ps.get("ProductStatus") or ps.get("Status") or ps.get("Value")
    elif isinstance(ps, str):
        status = ps

    # Fallbacks
    status = status or prod.get("Status") or ""

    s = status.lower()
    obsolete_words = ["obsolete", "end of life", "eol", "nrnd", "not recommended", "discontinued"]
    is_obsolete = any(w in s for w in obsolete_words)
    is_active = (("active" in s or "production" in s or "stock" in s) and not is_obsolete)

    return {
        "product_status": status or None,
        "lifecycle_active": bool(is_active),
        "lifecycle_obsolete": bool(is_obsolete),
    }

def derive_unit_price(prod: dict):
    # Primary: Product.UnitPrice (Digi-Key often gives a single-unit guide price)
    up = prod.get("UnitPrice")
    if isinstance(up, (int, float)) and up > 0:
        return float(up)

    # Fallback: lowest available StandardPricing across variations
    best = None
    for var in prod.get("ProductVariations", []) or []:
        for br in var.get("StandardPricing", []) or []:
            price = br.get("UnitPrice")
            if price is None:
                continue
            price = float(price)
            if best is None or price < best:
                best = price
    return best  # may be None if nothing available

def main():
    res = scan_part(
        fullscreen=False, # opens fullscreen 
        show_window=True, # show preview UI 
        roi_only=False, # faster on Pi/PC 
        timeout=5, # seconds 
        mode="balanced", # or "fast"/"aggressive" 
        ) 
    if res.success:
        fields = res.data.get("fields", {}) # fields["digikey_part_number"], fields["quantity"], ... 
        dk_json = call_DigiKey_API(fields)  # Call DigiKey API and return JSON

        if dk_json:
            print('----Normally would index parts into DB here-----')
            #normalize_and_save(dk_json, fields)
        else:
            print("Could not normalize- API call failed.")
    else:
        print("Scanning Failed. PROMPT FOR MANUAL ENTRY")
        # Service a manual entry page for part number (wait for it)
        # Allow user to exit process or retry scaning if they don't want manual entry
        #dk_json = call_DigiKey_API(fields) #Structure 'fields' to service the mfr_part_num in a suitible format
        #normalize_and_save(dk_json, fields) #Handle properly

if __name__ == "__main__":
    main()

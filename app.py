# app.py
from __future__ import annotations

# Standard library
import json
import os
from contextlib import closing
from pathlib import Path
from typing import Any, Dict
import importlib

# Third-party
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

# Local Files/Helpers
from db_helper import get_conn
from Code_Scanner import fields_from_parsed, parse_from_web_text
from Scan_Part import (
    call_DigiKey_API,
    derive_lifecycle_flags,
    derive_unit_price,
    normalize_and_save,
)
import dk_decoder
from dk_decoder import decode_product, load_registry

# -------------------------------------------------------------------
# Flask setup
# -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "UI Pages"

app = Flask(
    __name__,
    template_folder=str(UI_DIR),
    static_folder=str(UI_DIR),     # serve files directly from /UI Pages
    static_url_path="/static",     # so url_for('static', filename='style.css') works
)

# Prepare registry for the decode step once at startup
REGISTRY = load_registry(
    profiles_dir=str(ROOT / "profiles"),
    traits_path=str(ROOT / "traits.yaml"),
)

# -------------------------------------------------------------------
# Helper Functions
# -------------------------------------------------------------------

def _parse_scanned_text_to_fields(scanned_text: str) -> Dict[str, Any]:
    """
    Best-effort conversion of a scanned barcode/DM string into the field dict
    expected by the Digi-Key lookup (e.g., digikey_part_number, mfr_part_number, quantity, ...).

    Order of operations:
      1) Try YOUR DataMatrix/MH10 parser from Code_Scanner (keeps logic consistent).
      2) Try JSON payloads (some labels/fixtures emit JSON).
      3) Try key=value tokens (split on | , ; whitespace and parse '=' or ':').
      4) Fallback: treat entire string as a DKPN if it looks like one, else an MPN.

    Notes:
      - We ignore the "digits_only_internal_id" inference from the parser so the
        web flow doesn’t get stuck with an unhelpful ID.
      - If a quantity is present and looks numeric, we coerce it to int.
    """
    from typing import Any, Dict

    if not scanned_text:
        return {}

    s = scanned_text.strip()

    # --- 1) Use your existing parser first (preferred) -----------------------
    try:
        parsed = parse_from_web_text(s)          # -> same rules as desktop scanner
        f: Dict[str, Any] = fields_from_parsed(parsed) or {}

        # If the parser only inferred an internal id (digits-only) without useful DIs,
        # ignore and continue to other strategies for the web flow.
        inf = parsed.get("inference") or {}
        only_internal = set(f.keys()) == {"internal_part_id"}
        if not (only_internal or getattr(inf, "get", lambda *_: None)("inference") == "digits_only_internal_id"):
            # Normalize quantity if present
            if "quantity" in f:
                try:
                    f["quantity"] = int(str(f["quantity"]).strip())
                except Exception:
                    pass
            return f
    except Exception:
        # fall through to other strategies
        pass

    # --- 2) JSON-ish payloads ------------------------------------------------
    try:
        import json
        data = json.loads(s)
        if isinstance(data, dict) and data:
            # normalize quantity if present
            if "quantity" in data:
                try:
                    data["quantity"] = int(str(data["quantity"]).strip())
                except Exception:
                    pass
            return data  # keys as provided by the source (e.g., digikey_part_number / mfr_part_number)
    except Exception:
        pass

    # --- 3) key=value tokens (split on common delimiters) --------------------
    if any(sym in s for sym in ("=", ":")):
        buf = s.replace("|", " ").replace(",", " ").replace(";", " ")
        tokens = [t.strip() for t in buf.split() if t.strip()]
        fields: Dict[str, Any] = {}
        for tok in tokens:
            if "=" in tok:
                k, v = tok.split("=", 1)
            elif ":" in tok:
                k, v = tok.split(":", 1)
            else:
                continue
            k = k.strip()
            v = v.strip()
            if not k:
                continue
            # Be forgiving about key casing
            kl = k.lower()
            fields[kl] = v

        if fields:
            if "quantity" in fields:
                try:
                    fields["quantity"] = int(str(fields["quantity"]).strip())
                except Exception:
                    pass
            return fields

    # --- 4) Fallback: raw part number heuristic ------------------------------
    # Prefer Digi-Key PN if it "looks like one" (often contains a dash or DK prefix).
    if "-" in s or s.upper().startswith("DK"):
        return {"digikey_part_number": s}
    return {"mfr_part_number": s}

def _build_preview(dk_json: Dict[str, Any], scan_fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Squash Digi-Key payload + decoded traits into a compact UI preview.
    Also picks the default quantity if your scanned fields included one.
    """
    prod = (dk_json or {}).get("Product", {}) or {}
    decoded = decode_product(dk_json, REGISTRY)

    lifec = derive_lifecycle_flags(prod)
    unit_price = derive_unit_price(prod)

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

    # Default quantity comes from scan fields if present (works with your Scan_Part/Code_Scanner flow)
    qty_default = scan_fields.get("quantity") or scan_fields.get("qty") or 0
    try:
        qty_default = int(qty_default)
    except Exception:
        qty_default = 0

    position_default = (
        scan_fields.get("Part_Cataloged_Position")
        or scan_fields.get("part_cataloged_position")
        or scan_fields.get("position")
        or scan_fields.get("bin")
        or ""
    )

    return {
        "header": header,
        "category": {
            "id": decoded.get("category_id"),
            "name": decoded.get("category_source_name"),
        },
        "attributes": decoded.get("attributes") or {},
        "unknown_parameters": decoded.get("unknown_parameters") or {},
        "qty_default": qty_default,
        "position_default": position_default,
        # Echo inputs back for a later save
        "raw_scan_fields": scan_fields,
    }

def _find_existing_position_by_mpn(mpn: str):
    """
    Returns one of:
      - {"position": <bin>, "part_id": int, "qty_on_hand": int, "source": "..."}  # suggested bin
      - {"blocked_by": {"position": <bin>, "part_id": int, "mpn": str, "qty_on_hand": int}}  # last bin is occupied
      - None  # no info
    Strategy:
      A) If a bin currently holds this part (>0 on-hand), suggest it.
      B) Else get the last-known bin (latest intake). If it's empty now, suggest it.
         If it's occupied by a different part, return blocked_by and NO suggestion.
    """
    if not mpn:
        return None

    with closing(get_conn()) as conn:
        with conn.cursor() as cur:

            # --- A) Prefer a bin that currently holds this part (>0) ---
            if _has_relation("v_inventory_available"):
                # stock-aware via view
                cur.execute("""
                    SELECT b.position_code, p.part_id, b.qty_on_hand
                      FROM public.parts p
                      JOIN public.v_inventory_available b ON b.part_id = p.part_id
                     WHERE upper(p.mpn) = upper(%s)
                       AND b.qty_on_hand > 0
                     ORDER BY b.qty_on_hand DESC
                     LIMIT 1
                """, (mpn,))
            elif _has_relation("movements"):
                # stock-aware via movements
                cur.execute("""
                    SELECT m.position_code, p.part_id, SUM(m.quantity_delta)::int AS qty_on_hand
                      FROM public.movements m
                      JOIN public.parts p ON p.part_id = m.part_id
                     WHERE upper(p.mpn) = upper(%s)
                       AND m.position_code NOT LIKE 'OUT%%'
                     GROUP BY m.position_code, p.part_id
                     HAVING SUM(m.quantity_delta) > 0
                     ORDER BY qty_on_hand DESC
                     LIMIT 1
                """, (mpn,))
            else:
                # No stock awareness available → skip A)
                cur.execute("SELECT NULL WHERE FALSE")

            row = cur.fetchone()
            if row:
                return {
                    "position": row[0],
                    "part_id": row[1],
                    "qty_on_hand": int(row[2]),
                    "source": "current_stock",
                }

            # --- B) Fallback: last-known bin (latest intake), only if EMPTY now ---
            cur.execute("""
                SELECT i.part_cataloged_position, p.part_id
                  FROM public.parts p
                  JOIN public.intakes i ON i.part_id = p.part_id
                 WHERE upper(p.mpn) = upper(%s)
                 ORDER BY i.created_at DESC
                 LIMIT 1
            """, (mpn,))
            last = cur.fetchone()
            if not last:
                return None

            last_pos, part_id = last[0], last[1]

            # If we can't check occupancy (no movements table), we *assume unknown* (don't suggest).
            if not _has_relation("movements"):
                return None

            # Check if the last_pos is empty now (sum over ALL parts at that bin)
            cur.execute("""
                SELECT COALESCE(SUM(quantity_delta),0)::int
                  FROM public.movements
                 WHERE position_code = %s
                   AND position_code NOT LIKE 'OUT%%'
            """, (last_pos,))
            bin_balance = int(cur.fetchone()[0])

            if bin_balance <= 0:
                # empty → safe to suggest last-known bin
                return {
                    "position": last_pos,
                    "part_id": part_id,
                    "qty_on_hand": 0,
                    "source": "last_known_empty",
                }

            # If not empty, check if it's occupied by THIS part (rare if A) didn't find it)
            cur.execute("""
                SELECT m.part_id, p.mpn, SUM(m.quantity_delta)::int AS qty
                  FROM public.movements m
                  JOIN public.parts p ON p.part_id = m.part_id
                 WHERE m.position_code = %s
                   AND m.position_code NOT LIKE 'OUT%%'
                 GROUP BY m.part_id, p.mpn
                 HAVING SUM(m.quantity_delta) > 0
                 ORDER BY qty DESC
                 LIMIT 1
            """, (last_pos,))
            occ = cur.fetchone()
            if occ:
                occ_part_id, occ_mpn, occ_qty = int(occ[0]), occ[1], int(occ[2])
                if occ_part_id != part_id:
                    # Occupied by a different part → return blocked info, no suggestion
                    return {
                        "blocked_by": {
                            "position": last_pos,
                            "part_id": occ_part_id,
                            "mpn": occ_mpn,
                            "qty_on_hand": occ_qty,
                        }
                    }

            # Otherwise (occupied by same part) we'd have found it in A). Be conservative:
            return None

def _list_available_parts():
    """
    Returns a list of dicts: [{part_id, mpn, manufacturer, position_code, qty_on_hand}, ...]
    Only bins that currently have >0 qty and are NOT OUT.
    Prefers view v_inventory_available; else falls back to movements; else to intakes.
    """
    rows = []
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            if _has_relation("v_inventory_available"):
                cur.execute(
                    """
                    SELECT p.part_id, p.mpn, p.manufacturer, v.position_code, v.qty_on_hand
                    FROM public.v_inventory_available v
                    JOIN public.parts p ON p.part_id = v.part_id
                    WHERE v.qty_on_hand > 0
                    ORDER BY upper(p.mpn), v.position_code
                    """
                )
                rows = cur.fetchall()
            elif _has_relation("movements"):
                cur.execute(
                    """
                    SELECT p.part_id, p.mpn, p.manufacturer, m.position_code, SUM(m.quantity_delta)::int AS qty_on_hand
                    FROM public.movements m
                    JOIN public.parts p ON p.part_id = m.part_id
                    WHERE m.position_code NOT LIKE 'OUT%%'
                    GROUP BY p.part_id, p.mpn, p.manufacturer, m.position_code
                    HAVING SUM(m.quantity_delta) > 0
                    ORDER BY upper(p.mpn), m.position_code
                    """
                )
                rows = cur.fetchall()
            else:
                # Fallback: aggregate intakes table (assumes positive quantities only)
                cur.execute(
                    """
                    SELECT p.part_id, p.mpn, p.manufacturer, i.part_cataloged_position AS position_code,
                           SUM(i.quantity_scanned)::int AS qty_on_hand
                    FROM public.intakes i
                    JOIN public.parts p ON p.part_id = i.part_id
                    GROUP BY p.part_id, p.mpn, p.manufacturer, i.part_cataloged_position
                    HAVING SUM(i.quantity_scanned) > 0
                    ORDER BY upper(p.mpn), i.part_cataloged_position
                    """
                )
                rows = cur.fetchall()

    # normalize rows
    return [
        {
            "part_id": r[0],
            "mpn": r[1],
            "manufacturer": r[2],
            "position_code": r[3],
            "qty_on_hand": int(r[4]),
        }
        for r in rows
    ]

def _has_relation(relname: str) -> bool:
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1
                  FROM pg_catalog.pg_class c
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname = %s
                   AND c.relkind IN ('r','v','m')
                 LIMIT 1
            """, (relname,))
            return cur.fetchone() is not None

def _categories_with_stock():
    """
    Returns categories that currently have >0 available stock (excluding OUT).
    Rows: [{category_id, source_name, part_count, total_qty}]
    Prefers your views if present; otherwise falls back.
    """
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            if _has_relation("v_inventory_totals"):
                # ✅ Use 'available' column
                cur.execute("""
                    SELECT
                        c.category_id,
                        COALESCE(c.source_name, p.category_source_name) AS source_name,
                        COUNT(DISTINCT p.part_id)                       AS part_count,
                        SUM(t.available)::int                            AS total_qty
                    FROM public.v_inventory_totals t
                    JOIN public.parts p ON p.part_id = t.part_id
                    LEFT JOIN public.categories c ON c.category_id = p.category_id
                    WHERE t.available > 0
                    GROUP BY 1, 2
                    ORDER BY 2 NULLS LAST;
                """)
            elif _has_relation("v_current_inventory"):
                # Aggregate per part from current inventory (per-bin rows)
                cur.execute("""
                    WITH per_part AS (
                        SELECT part_id, SUM(quantity)::int AS available
                        FROM public.v_current_inventory
                        GROUP BY part_id
                        HAVING SUM(quantity) > 0
                    )
                    SELECT
                        c.category_id,
                        COALESCE(c.source_name, p.category_source_name) AS source_name,
                        COUNT(DISTINCT p.part_id)                       AS part_count,
                        SUM(pp.available)::int                          AS total_qty
                    FROM per_part pp
                    JOIN public.parts p ON p.part_id = pp.part_id
                    LEFT JOIN public.categories c ON c.category_id = p.category_id
                    GROUP BY 1, 2
                    ORDER BY 2 NULLS LAST;
                """)
            elif _has_relation("movements"):
                # Sum movements excluding OUT*
                cur.execute("""
                    WITH avail AS (
                        SELECT m.part_id,
                               SUM(CASE WHEN m.position_code NOT LIKE 'OUT%%' THEN m.quantity_delta ELSE 0 END)::int AS available
                        FROM public.movements m
                        GROUP BY m.part_id
                        HAVING SUM(CASE WHEN m.position_code NOT LIKE 'OUT%%' THEN m.quantity_delta ELSE 0 END) > 0
                    )
                    SELECT
                        c.category_id,
                        COALESCE(c.source_name, p.category_source_name) AS source_name,
                        COUNT(DISTINCT p.part_id)                       AS part_count,
                        SUM(avail.available)::int                       AS total_qty
                    FROM avail
                    JOIN public.parts p ON p.part_id = avail.part_id
                    LEFT JOIN public.categories c ON c.category_id = p.category_id
                    GROUP BY 1, 2
                    ORDER BY 2 NULLS LAST;
                """)
            else:
                # Weak fallback: intakes only (no checkouts considered)
                cur.execute("""
                    WITH per_part AS (
                        SELECT i.part_id, SUM(i.quantity_scanned)::int AS qty
                        FROM public.intakes i
                        GROUP BY i.part_id
                        HAVING SUM(i.quantity_scanned) > 0
                    )
                    SELECT
                        c.category_id,
                        COALESCE(c.source_name, p.category_source_name) AS source_name,
                        COUNT(DISTINCT p.part_id)                       AS part_count,
                        SUM(pp.qty)::int                                AS total_qty
                    FROM per_part pp
                    JOIN public.parts p ON p.part_id = pp.part_id
                    LEFT JOIN public.categories c ON c.category_id = p.category_id
                    GROUP BY 1, 2
                    ORDER BY 2 NULLS LAST;
                """)

            rows = cur.fetchall()

    return [
        {
            "category_id": r[0],
            "source_name": r[1] or "(Uncategorized)",
            "part_count": int(r[2]),
            "total_qty": int(r[3]),
        }
        for r in rows
    ]

def _parts_in_category(category_id: str):
    with closing(get_conn()) as conn:
        with conn.cursor() as cur:
            if _has_relation("movements"):
                # ✅ Pre-aggregate per-bin first; then aggregate to bins JSON and totals
                cur.execute(
                    """
                    WITH pos_qty AS (
                        SELECT m.part_id,
                               m.position_code,
                               SUM(m.quantity_delta)::int AS qty
                          FROM public.movements m
                         WHERE m.position_code NOT LIKE 'OUT%%'
                      GROUP BY m.part_id, m.position_code
                        HAVING SUM(m.quantity_delta) > 0
                    ),
                    part_totals AS (
                        SELECT part_id, SUM(qty)::int AS qty
                          FROM pos_qty
                      GROUP BY part_id
                    )
                    SELECT p.part_id,
                           p.mpn,
                           p.manufacturer,
                           p.description,
                           p.detailed_description,
                           p.product_url,
                           p.datasheet_url,
                           p.image_url,
                           p.attributes,
                           pt.qty AS qty,
                           json_agg(
                             json_build_object('position', pq.position_code, 'qty', pq.qty)
                             ORDER BY pq.qty DESC
                           ) AS bin_list
                      FROM public.parts p
                      JOIN part_totals pt ON pt.part_id = p.part_id
                      JOIN pos_qty pq     ON pq.part_id = p.part_id
                     WHERE p.category_id = %s
                  GROUP BY p.part_id, pt.qty
                  ORDER BY UPPER(p.mpn)
                    """,
                    (category_id,),
                )
            elif _has_relation("v_inventory_available"):
                # (unchanged) view-based path
                cur.execute(
                    """
                    SELECT p.part_id, p.mpn, p.manufacturer, p.description, p.detailed_description,
                           p.product_url, p.datasheet_url, p.image_url, p.attributes,
                           SUM(v.qty_on_hand)::int AS qty,
                           json_agg(
                             json_build_object('position', v.position_code, 'qty', v.qty_on_hand)
                             ORDER BY v.qty_on_hand DESC
                           ) AS bin_list
                      FROM public.parts p
                      JOIN public.v_inventory_available v ON v.part_id = p.part_id
                     WHERE p.category_id = %s
                  GROUP BY p.part_id
                    HAVING SUM(v.qty_on_hand) > 0
                  ORDER BY UPPER(p.mpn)
                    """,
                    (category_id,),
                )
            else:
                # (unchanged) intakes fallback
                cur.execute(
                    """
                    SELECT p.part_id, p.mpn, p.manufacturer, p.description, p.detailed_description,
                           p.product_url, p.datasheet_url, p.image_url, p.attributes,
                           SUM(i.quantity_scanned)::int AS qty,
                           json_agg(
                             json_build_object('position', i.part_cataloged_position, 'qty', SUM(i.quantity_scanned))
                             ORDER BY SUM(i.quantity_scanned) DESC
                           ) AS bin_list
                      FROM public.parts p
                      JOIN public.intakes i ON i.part_id = p.part_id
                     WHERE p.category_id = %s
                  GROUP BY p.part_id
                    HAVING SUM(i.quantity_scanned) > 0
                  ORDER BY UPPER(p.mpn)
                    """,
                    (category_id,),
                )

            rows = cur.fetchall()

    items = []
    for r in rows:
        items.append({
            "part_id": r[0],
            "mpn": r[1],
            "manufacturer": r[2],
            "description": r[3],
            "detailed": r[4],
            "product_url": r[5],
            "datasheet_url": r[6],
            "image_url": r[7],
            "attributes": r[8] or {},
            "qty": int(r[9]),
            "bins": r[10] or [],
        })
    return items

def _table_exists_public(table_name: str) -> bool:
    """
    Lightweight check used only by api_checkout_part.
    Looks for a base table in the 'public' schema.
    Safe to coexist with any existing has_relation() you already use elsewhere.
    """
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.tables
              WHERE table_schema = 'public'
                AND table_name = %s
            )
            """,
            (table_name,),
        )
        return bool(cur.fetchone()[0])
# -------------------------------------------------------------------
# Routes: UI
# -------------------------------------------------------------------
@app.get("/")
def home():
    # HOMEPAGE  
    return render_template("index.html")

@app.get("/scan")
def scan_in():  # Scan in a new part
    return render_template("scan.html")

@app.get("/checkout")
def checkout():
    # Landing page: choose Manual vs QR, with Back to Home
    return render_template("checkout/index.html")

@app.get("/manual_checkout")
def manual_checkout():
    return render_template("checkout/manual/manual_checkout.html")

@app.get("/checkout/qr")
def checkout_qr():
    # Placeholder page for the QR workflow we’ll build next
    return render_template("checkout/guided/qr.html")

@app.get("/favicon.ico")
def favicon():
    if (UI_DIR / "favicon.ico").exists():
        return send_from_directory(str(UI_DIR), "favicon.ico")
    return ("", 204)

@app.get("/api/available_parts")
def api_available_parts():
    try:
        return jsonify({"ok": True, "items": _list_available_parts()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/catalog")
def catalog_home():
    cats = _categories_with_stock()
    return render_template("catalog/catalog_categories.html", categories=cats)

@app.get("/catalog/<category_id>")
def catalog_category(category_id):
    parts = _parts_in_category(category_id)
    cat_name = next((c["source_name"] for c in _categories_with_stock() if c["category_id"] == category_id), None)
    return render_template("catalog/catalog_parts.html", category_id=category_id, category_name=cat_name, parts=parts)

@app.get("/DBreset")
def DBreset():
    return render_template("DBreset.html")
    
@app.get("/info_screen")
def info():
    return render_template("info.html")

# -------------------------------------------------------------------
# Routes: API
# -------------------------------------------------------------------
@app.post("/api/lookup")
def api_lookup():
    """
    Accepts either:
      { scanned_text: "<raw barcode/QR text>" }
    or
      { manual_part_number: "296-12345-1-ND" }
    Optional extras can be included (quantity, position) and will flow into the preview defaults.
    """
    data = request.get_json(force=True, silent=True) or {}

    # Build fields from manual entry or scanned text
    if data.get("manual_part_number"):
        fields = {"digikey_part_number": str(data["manual_part_number"]).strip()}
    elif data.get("scanned_text"):
        fields = _parse_scanned_text_to_fields(str(data["scanned_text"]))
    else:
        return jsonify({"ok": False, "error": "No input provided"}), 400

    # Pass through optional defaults from the UI (e.g., if user pre-typed quantity/position)
    for k in ("quantity", "qty", "position", "Part_Cataloged_Position"):
        if k in data:
            fields[k] = data[k]

    # Call your existing Digi-Key integration
    dk_json = call_DigiKey_API(fields)
    if not dk_json:
        return jsonify({"ok": False, "error": "Digi-Key lookup failed"}), 502

    preview = _build_preview(dk_json, fields)

    mpn = (preview.get("header") or {}).get("mpn")
    existing = _find_existing_position_by_mpn(mpn)
    if existing:
        if existing.get("position"):
            preview["position_default"] = existing["position"]
        preview["existing"] = existing
    return jsonify({"ok": True, "preview": preview})

@app.post("/api/intake")
def api_intake():
    """
    Persists the selected/edited part to Postgres.
    Requires: payload = { edits: {quantity, position}, raw_scan_fields: {...}, mpn?: str }
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        edits = payload.get("edits") or {}
        raw = payload.get("raw_scan_fields") or {}

        fields = {**raw}
        if "quantity" in edits:
            fields["quantity"] = edits["quantity"]
        if "position" in edits:
            fields["position"] = (edits["position"] or "").strip()

        # HARD REQUIREMENT: Position/Bin must be present
        if not fields.get("position"):
            return jsonify({"ok": False, "error": "Position/Bin is required"}), 400

        # If user entered a manual part number earlier and it didn't carry through:
        if payload.get("mpn") and not fields.get("manufacturer_part_number") and not fields.get("mfr_part_number"):
            fields["mfr_part_number"] = payload["mpn"]

        # Get fresh Digi-Key details (or use a cache if you add one later)
        dk_json = call_DigiKey_API(fields)
        if not dk_json:
            return jsonify({"ok": False, "error": "Digi-Key lookup failed"}), 502

        # Save to DB via your shared routine
        from Scan_Part import normalize_and_save  # now exists in Scan_Part.py
        save_result = normalize_and_save(
            dk_json, fields,
            REGISTRY=REGISTRY,
            decode_product=decode_product,
            derive_lifecycle_flags=derive_lifecycle_flags,
            derive_unit_price=derive_unit_price,
        )

        return jsonify({"ok": True, "saved": save_result})

    except Exception as e:
        # Log server-side if you like
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/checkout_part")
def api_checkout_part():
    data = request.get_json(force=True, silent=True) or {}
    part_id = data.get("part_id")
    pos = (data.get("position_code") or "").strip()

    if not part_id or not pos:
        return jsonify({"ok": False, "error": "part_id and position_code are required"}), 400
    if not _table_exists_public("movements"):
        return jsonify({"ok": False, "error": "movements table not found. Please create it first."}), 400

    with closing(get_conn()) as conn:
        with conn:
            with conn.cursor() as cur:
                # Ensure OUT exists (FK target)
                cur.execute("""
                    INSERT INTO public.locations (position_code, description)
                    VALUES ('OUT', 'Checked-out items')
                    ON CONFLICT DO NOTHING;
                """)

                # Ensure the source bin exists (usually does via intake)
                cur.execute("""
                    INSERT INTO public.locations (position_code)
                    VALUES (%s)
                    ON CONFLICT DO NOTHING;
                """, (pos,))

                # Current qty in that bin (from movements)
                cur.execute("""
                    SELECT COALESCE(SUM(quantity_delta),0)::int
                    FROM public.movements
                    WHERE part_id = %s AND position_code = %s
                """, (part_id, pos))
                qty = int(cur.fetchone()[0])
                if qty <= 0:
                    return jsonify({"ok": False, "error": "No quantity available in this bin"}), 400

                # Transfer: -qty from bin, +qty to OUT (movement_type required by your schema)
                cur.execute("""
                    INSERT INTO public.movements (part_id, position_code, quantity_delta, movement_type, note)
                    VALUES (%s, %s, %s, 'transfer', 'Checkout to OUT')
                    RETURNING movement_id
                """, (part_id, pos, -qty))
                _ = cur.fetchone()[0]

                cur.execute("""
                    INSERT INTO public.movements (part_id, position_code, quantity_delta, movement_type, note)
                    VALUES (%s, 'OUT', %s, 'transfer', 'Checkout to OUT')
                """, (part_id, qty))

    return jsonify({"ok": True, "moved": qty, "from": pos, "to": "OUT"})

@app.post("/api/resolve_mpns")
def api_resolve_mpns():
    data = request.get_json(force=True, silent=True) or {}
    mpns = [str(m).strip() for m in (data.get("mpns") or []) if str(m).strip()]
    if not mpns: return jsonify({"ok": False, "error": "No MPNs provided"}), 400

    out_available = []
    out_missing = []

    with closing(get_conn()) as conn, conn.cursor() as cur:
        # v_inventory_available: (part_id, position_code, qty_on_hand)
        cur.execute("""
            SELECT p.part_id, p.mpn, p.manufacturer, p.description, p.detailed_description,
                   p.image_url, p.datasheet_url, p.product_url
            FROM parts p
            WHERE p.mpn = ANY(%s)
        """, (mpns,))
        parts = { row[1]: dict(
            part_id=row[0], mpn=row[1], manufacturer=row[2],
            description=row[3] or row[4] or '', image_url=row[5],
            datasheet_url=row[6], product_url=row[7]
        ) for row in cur.fetchall() }

        for mpn in mpns:
            base = parts.get(mpn)
            if not base:
                out_missing.append({"mpn": mpn, "reason": "not_found"})
                continue

            # bins + qty
            cur.execute("""
                SELECT position_code, qty_on_hand
                FROM v_inventory_available
                WHERE part_id = %s AND qty_on_hand > 0
                ORDER BY qty_on_hand DESC, position_code
            """, (base["part_id"],))
            bins = [{"position": r[0], "qty": int(r[1])} for r in cur.fetchall()]
            qty = sum(b["qty"] for b in bins)
            if qty <= 0:
                out_missing.append({"mpn": mpn, "reason": "out_of_stock"})
            else:
                out_available.append({ **base, "bins": bins, "qty": qty })

    return jsonify({"ok": True, "available": out_available, "missing": out_missing})

@app.post("/api/checkout_one")
def api_checkout_one():
    data = request.get_json(force=True, silent=True) or {}
    part_id = data.get("part_id")
    position = (data.get("position_code") or "").strip()
    if not part_id:
        return jsonify({"ok": False, "error": "part_id required"}), 400

    with closing(get_conn()) as conn:
        with conn:
            with conn.cursor() as cur:
                # If a position is provided, take from that bin; else, take from the bin with max qty
                if position:
                    cur.execute(
                        "SELECT qty_on_hand FROM v_inventory_available WHERE part_id=%s AND position_code=%s",
                        (part_id, position),
                    )
                    row = cur.fetchone()
                    if not row or int(row[0]) <= 0:
                        return jsonify({"ok": False, "error": "No stock in chosen bin"}), 400
                    qty = int(row[0])
                else:
                    cur.execute(
                        """
                        SELECT position_code, qty_on_hand
                        FROM v_inventory_available
                        WHERE part_id=%s AND qty_on_hand>0
                        ORDER BY qty_on_hand DESC, position_code
                        LIMIT 1
                        """,
                        (part_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return jsonify({"ok": False, "error": "No stock"}), 400
                    position, qty = row[0], int(row[1])

                # NOTE: correct column is quantity_delta (not qty_delta)
                cur.execute(
                    """
                    INSERT INTO movements (part_id, position_code, quantity_delta, note)
                    VALUES (%s, %s, %s, %s)
                    RETURNING movement_id
                    """,
                    (part_id, position, -qty, "QR guided checkout"),
                )
                movement_id = cur.fetchone()[0]

    return jsonify({"ok": True, "movement_id": movement_id, "qty_out": qty, "position": position})

@app.post("/DBreset/confirm")
def dbreset_confirm():
    # Checkbox guard
    checks_ok = all((request.form.get("chk1"),
                     request.form.get("chk2"),
                     request.form.get("chk3")))
    if not checks_ok:
        return redirect(url_for("DBreset"))   # <-- your GET endpoint name

    # Split into individual statements (psycopg won’t execute multi-statement strings by default)
    stmts = [
        "TRUNCATE TABLE public.categories RESTART IDENTITY CASCADE;",
        "TRUNCATE TABLE public.intakes RESTART IDENTITY CASCADE;",
        "TRUNCATE TABLE public.locations RESTART IDENTITY CASCADE;",
        "TRUNCATE TABLE public.movements RESTART IDENTITY CASCADE;",
        "TRUNCATE TABLE public.parts RESTART IDENTITY CASCADE;",
    ]

    try:
        with closing(get_conn()) as conn:
            # `with conn:` starts a transaction and commits on success / rolls back on exception
            with conn:
                with conn.cursor() as cur:
                    for s in stmts:
                        cur.execute(s)
        return redirect(url_for("home"))
    except Exception as e:
        app.logger.exception("DB reset failed: %s", e)
        # On error, just go back to the reset page (or return a JSON error if you prefer)
        return redirect(url_for("DBreset"))


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    # Windows dev: http://localhost:5000
    # Raspberry Pi (kiosk): Chromium at http://localhost:5000; camera works without HTTPS in localhost context.
    app.run(host="0.0.0.0", port=5000, debug=(os.getenv("FLASK_DEBUG") == "1"))

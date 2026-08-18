# mcp_server/tools.py
"""MCP tools. Curated, parameterised queries plus a guarded raw-SELECT escape hatch."""
from __future__ import annotations

import json
from typing import Any, Callable

from . import config, context
from .readonly_db import dump, fetch, readonly_cursor, jsonify_row
from .sql_guard import ALLOWED_TABLES, SqlRejected, validate, validate_and_wrap

# raw_vendor_json is deliberately absent: a single row can be tens of kilobytes.
PART_COLUMNS = """
    p.part_id, p.mpn, p.manufacturer, p.description, p.detailed_description,
    p.category_id, p.category_source_name, p.category_path,
    p.unit_price, p.product_status, p.lifecycle_active, p.lifecycle_obsolete,
    p.datasheet_url, p.product_url, p.attributes, p.unknown_parameters,
    p.created_at, p.updated_at
"""


def _limit(args: dict) -> int:
    requested = args.get("limit")
    if requested is None:
        return config.default_rows()
    try:
        return max(1, min(config.max_rows(), int(requested)))
    except (TypeError, ValueError):
        return config.default_rows()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _list_tables(_args: dict) -> str:
    return dump(context.tables())


def _describe_table(args: dict) -> str:
    name = str(args.get("table", "")).strip().lower()
    relations = context.tables()["relations"]
    match = next((r for r in relations if r["name"] == name), None)
    if match is None:
        return dump({
            "error": f"Unknown relation '{name}'.",
            "available": sorted(ALLOWED_TABLES),
        })
    return dump(match)


def _list_categories(args: dict) -> str:
    data = context.categories()
    search = (args.get("search") or "").strip().lower()
    rows = data["categories"]
    if search:
        rows = [
            r for r in rows
            if search in (r.get("category_id") or "").lower()
            or search in (r.get("source_name") or "").lower()
            or search in (r.get("example_category_path") or "").lower()
        ]
    return dump({"note": data["note"], "rows": rows})


def _attribute_keys(args: dict) -> str:
    category_id = (args.get("category_id") or "").strip()
    live = context.jsonb()["attributes_by_category"]
    profile = context.profiles()["profiles"].get(category_id, {})
    if not category_id:
        return dump({"error": "category_id is required.", "known": sorted(live)})
    return dump({
        "category_id": category_id,
        "note": "Values are stored in base SI units. 'defined' is authoritative; "
                "'observed' is what actually appears in the data.",
        "defined": profile.get("attribute_keys", {}),
        "display_order": profile.get("display_order", []),
        "observed": live.get(category_id, []),
    })


_DISTINCT_VALUES_SQL = """
SELECT p.attributes -> %(key)s AS value, count(*)::int AS parts
FROM parts p
WHERE p.attributes ? %(key)s
  AND (%(category_id)s IS NULL OR p.category_id = %(category_id)s)
GROUP BY 1
ORDER BY parts DESC, 1
LIMIT %(limit)s
"""


def _distinct_attribute_values(args: dict) -> str:
    key = (args.get("key") or "").strip()
    if not key:
        return dump({"error": "key is required."})
    rows = fetch(_DISTINCT_VALUES_SQL, {
        "key": key,
        "category_id": (args.get("category_id") or "").strip() or None,
        "limit": _limit(args),
    })
    return dump({"key": key, "rows": rows})


_SEARCH_SQL = f"""
SELECT {PART_COLUMNS},
       COALESCE(t.available, 0) AS available,
       COALESCE(t.on_loan, 0)   AS on_loan
FROM parts p
LEFT JOIN v_inventory_totals t ON t.part_id = p.part_id
WHERE (%(text)s IS NULL
       OR p.mpn ILIKE %(pattern)s
       OR p.manufacturer ILIKE %(pattern)s
       OR p.description ILIKE %(pattern)s
       OR p.detailed_description ILIKE %(pattern)s
       OR p.category_path ILIKE %(pattern)s)
  AND (%(category_id)s IS NULL OR p.category_id = %(category_id)s)
  AND (%(attributes)s IS NULL OR p.attributes @> %(attributes)s::jsonb)
  AND (%(category_contains)s IS NULL
       OR p.category_path_names @> %(category_contains)s::jsonb)
  AND (NOT %(in_stock_only)s OR COALESCE(t.available, 0) > 0)
ORDER BY COALESCE(t.available, 0) DESC, p.mpn
LIMIT %(limit)s
"""


def _search_parts(args: dict) -> str:
    text = (args.get("text") or "").strip() or None
    attributes = args.get("attributes")
    if isinstance(attributes, str):
        try:
            attributes = json.loads(attributes)
        except json.JSONDecodeError:
            return dump({"error": "attributes must be a JSON object."})
    if attributes is not None and not isinstance(attributes, dict):
        return dump({"error": "attributes must be a JSON object."})

    ancestor = (args.get("category_contains") or "").strip()
    rows = fetch(_SEARCH_SQL, {
        "text": text,
        "pattern": f"%{text}%" if text else None,
        "category_id": (args.get("category_id") or "").strip() or None,
        "attributes": json.dumps(attributes) if attributes else None,
        "category_contains": json.dumps([ancestor]) if ancestor else None,
        "in_stock_only": bool(args.get("in_stock_only", False)),
        "limit": _limit(args),
    })
    return dump({
        "note": "raw_vendor_json is omitted. Use get_part with include_raw_vendor_json "
                "only if a specification is missing from both attributes and unknown_parameters.",
        "count": len(rows),
        "rows": rows,
    })


_GET_PART_SQL = f"""
SELECT {PART_COLUMNS},
       COALESCE(t.available, 0) AS available,
       COALESCE(t.on_loan, 0)   AS on_loan,
       COALESCE(t.owned, 0)     AS owned
FROM parts p
LEFT JOIN v_inventory_totals t ON t.part_id = p.part_id
WHERE (%(mpn)s IS NOT NULL AND p.mpn = %(mpn)s)
   OR (%(part_id)s IS NOT NULL AND p.part_id = %(part_id)s)
LIMIT 1
"""

_PART_BINS_SQL = """
SELECT a.position_code, a.qty_on_hand, l.state, l.description
FROM v_inventory_available a
LEFT JOIN locations l ON l.position_code = a.position_code
WHERE a.part_id = %(part_id)s AND a.qty_on_hand <> 0
UNION ALL
SELECT o.position_code, o.qty_on_loan, l.state, l.description
FROM v_inventory_on_loan o
LEFT JOIN locations l ON l.position_code = o.position_code
WHERE o.part_id = %(part_id)s AND o.qty_on_loan <> 0
"""

_PART_MOVEMENTS_SQL = """
SELECT created_at, movement_type, quantity_delta, position_code, unit_price,
       lot_code, reference_doc, note
FROM movements
WHERE part_id = %(part_id)s
ORDER BY created_at DESC
LIMIT 25
"""

_RAW_JSON_SQL = "SELECT raw_vendor_json FROM parts WHERE part_id = %(part_id)s"


def _get_part(args: dict) -> str:
    mpn = (args.get("mpn") or "").strip() or None
    part_id = args.get("part_id")
    try:
        part_id = int(part_id) if part_id is not None else None
    except (TypeError, ValueError):
        return dump({"error": "part_id must be an integer."})
    if mpn is None and part_id is None:
        return dump({"error": "Provide either mpn or part_id."})

    with readonly_cursor() as cur:
        cur.execute(_GET_PART_SQL, {"mpn": mpn, "part_id": part_id})
        row = cur.fetchone()
        if row is None:
            return dump({"error": f"No part matching mpn={mpn!r} part_id={part_id!r}."})
        part = jsonify_row(dict(row))

        cur.execute(_PART_BINS_SQL, {"part_id": part["part_id"]})
        part["locations"] = [jsonify_row(dict(r)) for r in cur.fetchall()]

        cur.execute(_PART_MOVEMENTS_SQL, {"part_id": part["part_id"]})
        part["recent_movements"] = [jsonify_row(dict(r)) for r in cur.fetchall()]

        if args.get("include_raw_vendor_json"):
            cur.execute(_RAW_JSON_SQL, {"part_id": part["part_id"]})
            raw = cur.fetchone()
            part["raw_vendor_json"] = raw["raw_vendor_json"] if raw else None
        else:
            part["raw_vendor_json"] = (
                "omitted (large) -- re-call with include_raw_vendor_json=true only if a "
                "needed specification is missing from attributes and unknown_parameters"
            )

    return dump(part)


_INVENTORY_SQL = """
SELECT p.part_id, p.mpn, p.manufacturer, p.description, p.category_id,
       t.available, t.on_loan, t.owned
FROM v_inventory_totals t
JOIN parts p USING (part_id)
WHERE (%(part_id)s IS NULL OR p.part_id = %(part_id)s)
  AND (%(category_id)s IS NULL OR p.category_id = %(category_id)s)
  AND (%(max_available)s IS NULL OR t.available <= %(max_available)s)
  AND (NOT %(owned_only)s OR t.owned > 0)
ORDER BY t.available ASC, p.mpn
LIMIT %(limit)s
"""


def _inventory_summary(args: dict) -> str:
    part_id = args.get("part_id")
    max_available = args.get("max_available")
    try:
        part_id = int(part_id) if part_id is not None else None
        max_available = int(max_available) if max_available is not None else None
    except (TypeError, ValueError):
        return dump({"error": "part_id and max_available must be integers."})

    rows = fetch(_INVENTORY_SQL, {
        "part_id": part_id,
        "category_id": (args.get("category_id") or "").strip() or None,
        "max_available": max_available,
        "owned_only": bool(args.get("owned_only", True)),
        "limit": _limit(args),
    })
    return dump({
        "note": "available = in normal bins; on_loan = in OUT% bins; owned = available + on_loan.",
        "count": len(rows),
        "rows": rows,
    })


def _execute_sql(args: dict) -> str:
    sql = args.get("sql") or ""
    limit = _limit(args)
    try:
        wrapped = validate_and_wrap(sql, limit)
    except SqlRejected as exc:
        return dump({"error": str(exc), "hint": "Only a single read-only SELECT is accepted."})

    try:
        with readonly_cursor() as cur:
            cur.execute(wrapped)
            rows = [jsonify_row(dict(r)) for r in cur.fetchall()] if cur.description else []
    except Exception as exc:
        return dump({"error": f"{type(exc).__name__}: {exc}".strip(), "sql": wrapped})

    payload: dict[str, Any] = {"row_count": len(rows), "row_limit": limit, "rows": rows}
    if len(rows) == limit:
        payload["note"] = f"Result was capped at {limit} rows; there may be more."
    return dump(payload)


def _explain_sql(args: dict) -> str:
    try:
        sql = validate(args.get("sql") or "")
    except SqlRejected as exc:
        return dump({"error": str(exc)})
    try:
        with readonly_cursor() as cur:
            cur.execute(f"EXPLAIN {sql}")
            plan = [list(r.values())[0] for r in cur.fetchall()]
    except Exception as exc:
        return dump({"error": f"{type(exc).__name__}: {exc}".strip()})
    return dump({"plan": plan})


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def _tool(name: str, description: str, properties: dict, required: list[str],
          handler: Callable[[dict], str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "handler": handler,
    }


_LIMIT_PROP = {
    "type": "integer",
    "description": "Maximum rows to return.",
    "minimum": 1,
}

TOOLS: list[dict] = [
    _tool(
        "list_tables",
        "List every readable table and view with columns, keys, indexes and approximate row "
        "counts. Read the schema://overview resource first for the semantics behind them.",
        {}, [], _list_tables,
    ),
    _tool(
        "describe_table",
        "Full definition of one relation: columns, types, constraints, indexes, and the view "
        "SQL if it is a view.",
        {"table": {"type": "string", "description": "Relation name, e.g. 'parts' or 'v_inventory_totals'."}},
        ["table"], _describe_table,
    ),
    _tool(
        "list_categories",
        "Map human wording to a category_id. Returns every category with its Digi-Key source "
        "name, part count and an example category path. Use this before filtering by category.",
        {"search": {"type": "string", "description": "Case-insensitive substring filter, e.g. 'capacitor'."}},
        [], _list_categories,
    ),
    _tool(
        "attribute_keys",
        "The specification keys available for a category: the authoritative definitions from the "
        "decoder profile (units, enums, validators) plus the keys actually observed in the data.",
        {"category_id": {"type": "string", "description": "A category_id from list_categories."}},
        ["category_id"], _attribute_keys,
    ),
    _tool(
        "distinct_attribute_values",
        "Distinct values of one parts.attributes key with part counts. Use it to discover valid "
        "filter values before calling search_parts.",
        {
            "key": {"type": "string", "description": "Attribute key, e.g. 'dielectric'."},
            "category_id": {"type": "string", "description": "Optional category_id to scope to."},
            "limit": _LIMIT_PROP,
        },
        ["key"], _distinct_attribute_values,
    ),
    _tool(
        "search_parts",
        "Primary search. Free-text match across MPN, manufacturer, description and category path, "
        "combined with exact JSONB attribute filtering and stock status. Returns current available "
        "and on-loan quantities. raw_vendor_json is never included.",
        {
            "text": {"type": "string", "description": "Free-text fragment; matched case-insensitively."},
            "category_id": {"type": "string", "description": "Exact category_id, e.g. 'capacitor_ceramic'."},
            "category_contains": {
                "type": "string",
                "description": "A Digi-Key category name anywhere in the hierarchy, e.g. 'Resistors'.",
            },
            "attributes": {
                "type": "object",
                "description": "JSONB containment filter against parts.attributes, e.g. "
                               "{\"dielectric\":\"X7R\"}. Values are base SI units: 10k ohm is 10000.",
            },
            "in_stock_only": {"type": "boolean", "description": "Only parts with available > 0."},
            "limit": _LIMIT_PROP,
        },
        [], _search_parts,
    ),
    _tool(
        "get_part",
        "Everything about one part: catalog fields, decoded attributes, unmapped vendor "
        "parameters, the bins it sits in, and its 25 most recent ledger movements. Set "
        "include_raw_vendor_json only as a last-resort verification when a specification is "
        "genuinely missing from both attributes and unknown_parameters -- the payload is large.",
        {
            "mpn": {"type": "string", "description": "Manufacturer part number (exact)."},
            "part_id": {"type": "integer", "description": "Numeric part id."},
            "include_raw_vendor_json": {
                "type": "boolean",
                "description": "Include the full Digi-Key API response. Large; off by default.",
            },
        },
        [], _get_part,
    ),
    _tool(
        "inventory_summary",
        "Stock roll-up per part from the movements ledger: available, on loan, owned. Filter by "
        "part, category, or a max_available threshold to find items running low.",
        {
            "part_id": {"type": "integer", "description": "Restrict to one part."},
            "category_id": {"type": "string", "description": "Restrict to one category_id."},
            "max_available": {"type": "integer", "description": "Only parts at or below this available quantity."},
            "owned_only": {"type": "boolean", "description": "Exclude parts never stocked. Default true."},
            "limit": _LIMIT_PROP,
        },
        [], _inventory_summary,
    ),
    _tool(
        "execute_sql",
        "Run an arbitrary read-only PostgreSQL SELECT when the other tools cannot express the "
        "question. A single SELECT (CTEs allowed) against parts, intakes, movements, locations, "
        "categories and the v_inventory_* views. Writes, multiple statements and side-effecting "
        "functions are rejected, and an outer LIMIT is always applied. See the "
        "schema://query_cookbook resource for worked examples.",
        {
            "sql": {"type": "string", "description": "One SELECT statement."},
            "limit": _LIMIT_PROP,
        },
        ["sql"], _execute_sql,
    ),
    _tool(
        "explain_sql",
        "Return the PostgreSQL query plan for a SELECT without executing it. Use this to check an "
        "expensive query before running it.",
        {"sql": {"type": "string", "description": "One SELECT statement."}},
        ["sql"], _explain_sql,
    ),
]

_BY_NAME = {t["name"]: t for t in TOOLS}


def list_tools() -> list[dict]:
    return [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


def call_tool(name: str, arguments: dict) -> str:
    tool = _BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    return tool["handler"](arguments or {})

# mcp_server/context.py
"""Schema context published to MCP clients.

Column metadata alone is not enough to query this database usefully, because the
electrical specifications live in JSONB. These builders combine live
introspection with the profile YAMLs so a client can discover the real shape of
`parts.attributes` without trial-and-error queries.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import config
from .readonly_db import fetch
from .sql_guard import ALLOWED_TABLES

ROOT = Path(__file__).resolve().parent.parent
OVERVIEW_PATH = Path(__file__).resolve().parent / "schema_overview.md"

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def cached(key: str, builder: Callable[[], Any]) -> Any:
    ttl = config.cache_ttl_seconds()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and ttl and now - hit[0] < ttl:
            return hit[1]
    value = builder()
    with _lock:
        _cache[key] = (now, value)
    return value


def invalidate() -> None:
    with _lock:
        _cache.clear()


# --------------------------------------------------------------------------
# schema://overview
# --------------------------------------------------------------------------

def overview() -> str:
    return OVERVIEW_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# schema://tables
# --------------------------------------------------------------------------

_COLUMNS_SQL = """
SELECT c.table_name,
       c.column_name,
       c.data_type,
       c.is_nullable = 'YES' AS nullable,
       c.column_default
FROM information_schema.columns c
WHERE c.table_schema = 'public'
ORDER BY c.table_name, c.ordinal_position
"""

_CONSTRAINTS_SQL = """
SELECT rel.relname AS table_name,
       con.conname AS name,
       CASE con.contype WHEN 'p' THEN 'PRIMARY KEY'
                        WHEN 'f' THEN 'FOREIGN KEY'
                        WHEN 'u' THEN 'UNIQUE'
                        WHEN 'c' THEN 'CHECK' END AS kind,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = rel.relnamespace
WHERE ns.nspname = 'public'
ORDER BY rel.relname, con.contype
"""

_INDEXES_SQL = """
SELECT tablename AS table_name, indexname AS name, indexdef AS definition
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname
"""

_VIEWS_SQL = """
SELECT viewname AS table_name, definition
FROM pg_views
WHERE schemaname = 'public'
ORDER BY viewname
"""

_ROWCOUNT_SQL = """
SELECT relname AS table_name, GREATEST(reltuples, 0)::bigint AS approx_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm')
"""


def _tables() -> dict:
    tables: dict[str, dict] = {}

    def slot(name: str) -> dict:
        return tables.setdefault(
            name,
            {"name": name, "kind": "table", "columns": [], "constraints": [], "indexes": []},
        )

    for row in fetch(_COLUMNS_SQL):
        if row["table_name"] not in ALLOWED_TABLES:
            continue
        col = {
            "name": row["column_name"],
            "type": row["data_type"],
            "nullable": row["nullable"],
        }
        if row["column_default"]:
            col["default"] = row["column_default"]
        slot(row["table_name"])["columns"].append(col)

    for row in fetch(_CONSTRAINTS_SQL):
        if row["table_name"] in tables:
            slot(row["table_name"])["constraints"].append(
                {"kind": row["kind"], "definition": row["definition"]}
            )

    for row in fetch(_INDEXES_SQL):
        if row["table_name"] in tables:
            slot(row["table_name"])["indexes"].append(row["definition"])

    for row in fetch(_VIEWS_SQL):
        if row["table_name"] in tables:
            entry = slot(row["table_name"])
            entry["kind"] = "view"
            entry["definition"] = " ".join(row["definition"].split())

    for row in fetch(_ROWCOUNT_SQL):
        if row["table_name"] in tables:
            slot(row["table_name"])["approx_rows"] = row["approx_rows"]

    return {
        "note": (
            "These are the only relations readable through this server. "
            "Specifications are in parts.attributes (JSONB) -- see schema://jsonb "
            "and schema://profiles. Never SELECT parts.raw_vendor_json in bulk."
        ),
        "relations": [tables[k] for k in sorted(tables)],
    }


def tables() -> dict:
    return cached("tables", _tables)


# --------------------------------------------------------------------------
# schema://jsonb
# --------------------------------------------------------------------------

_JSONB_KEYS_SQL = """
SELECT p.category_id,
       k.key,
       count(*)::int AS parts_with_key,
       (array_agg(DISTINCT jsonb_typeof(p.attributes -> k.key)))[1:3] AS json_types,
       (array_agg(DISTINCT left(p.attributes ->> k.key, 40)))[1:5] AS sample_values
FROM parts p
CROSS JOIN LATERAL jsonb_object_keys(p.attributes) AS k(key)
GROUP BY p.category_id, k.key
ORDER BY p.category_id, parts_with_key DESC, k.key
"""

_UNKNOWN_KEYS_SQL = """
SELECT k.key,
       count(*)::int AS parts_with_key,
       (array_agg(DISTINCT left(p.unknown_parameters ->> k.key, 40)))[1:3] AS sample_values
FROM parts p
CROSS JOIN LATERAL jsonb_object_keys(p.unknown_parameters) AS k(key)
GROUP BY k.key
ORDER BY parts_with_key DESC, k.key
LIMIT 200
"""


def _jsonb() -> dict:
    by_category: dict[str, list[dict]] = {}
    for row in fetch(_JSONB_KEYS_SQL):
        cat = row.pop("category_id") or "(uncategorized)"
        by_category.setdefault(cat, []).append(row)

    return {
        "note": (
            "Observed keys in parts.attributes, grouped by category_id. Values are "
            "stored in base SI units (10k ohm -> 10000, 100 nF -> 1e-07). Filter with "
            "attributes @> '{\"key\":value}'::jsonb to use idx_parts_attributes_gin."
        ),
        "attributes_by_category": by_category,
        "unknown_parameters": {
            "note": (
                "Raw Digi-Key parameter names with no canonical mapping. Always text. "
                "Check here when an expected spec is absent from attributes."
            ),
            "keys": fetch(_UNKNOWN_KEYS_SQL),
        },
    }


def jsonb() -> dict:
    return cached("jsonb", _jsonb)


# --------------------------------------------------------------------------
# schema://categories
# --------------------------------------------------------------------------

_CATEGORIES_SQL = """
SELECT COALESCE(p.category_id, c.category_id) AS category_id,
       COALESCE(c.source_name, min(p.category_source_name)) AS source_name,
       count(p.part_id)::int AS part_count,
       min(p.category_path) AS example_category_path
FROM categories c
FULL OUTER JOIN parts p ON p.category_id = c.category_id
GROUP BY COALESCE(p.category_id, c.category_id), c.source_name
ORDER BY part_count DESC, category_id
"""


def _categories() -> dict:
    return {
        "note": (
            "category_id is an internal decoder profile id (e.g. 'capacitor_ceramic'), "
            "not a Digi-Key category name. Map user phrasing to a category_id here "
            "before filtering."
        ),
        "categories": fetch(_CATEGORIES_SQL),
    }


def categories() -> dict:
    return cached("categories", _categories)


# --------------------------------------------------------------------------
# schema://profiles
# --------------------------------------------------------------------------

_UNIT_SUFFIXES = {
    "_f": "farads", "_ohm": "ohms", "_v": "volts", "_a": "amperes",
    "_w": "watts", "_hz": "hertz", "_s": "seconds", "_m": "metres",
    "_c": "degrees Celsius", "_pct": "percent", "_ppm": "parts per million",
    "_db": "decibels", "_bits": "bits", "_bytes": "bytes",
}


def _unit_for(key: str) -> str | None:
    for suffix, unit in sorted(_UNIT_SUFFIXES.items(), key=lambda kv: -len(kv[0])):
        if key.endswith(suffix):
            return unit
    return None


def profiles(registry: dict[str, dict] | None = None) -> dict:
    """Summarise the decoder profiles that define canonical attribute keys."""

    def build() -> dict:
        reg = registry
        if reg is None:
            from dk_decoder import load_registry

            reg = load_registry(
                profiles_dir=str(ROOT / "profiles"),
                traits_path=str(ROOT / "traits.yaml"),
            )

        out: dict[str, Any] = {}
        traits_def: dict = {}
        for cat_id, profile in sorted(reg.items()):
            traits_def = profile.get("traits_def") or traits_def
            attrs = profile.get("attributes") or {}
            validators = attrs.get("validators") or {}
            keys: dict[str, Any] = {}
            for key, sources in (attrs.get("map") or {}).items():
                entry: dict[str, Any] = {"vendor_sources": sources}
                unit = _unit_for(key)
                if unit:
                    entry["unit"] = unit
                if key in validators:
                    entry["validator"] = validators[key]
                keys[key] = entry
            out[cat_id] = {
                "source_categories": profile.get("source_categories", []),
                "traits": profile.get("traits", []),
                "display_order": profile.get("display_order", []),
                "attribute_keys": keys,
            }

        return {
            "note": (
                "Canonical attribute keys per category_id, taken from profiles/*.yaml. "
                "The key suffix carries the unit and values are stored in base SI units. "
                "'validator' shows the constraint the decoder applied; 'enum' lists the "
                "only values you should filter on."
            ),
            "traits": traits_def,
            "profiles": out,
        }

    return cached("profiles", build)


# --------------------------------------------------------------------------
# schema://query_cookbook
# --------------------------------------------------------------------------

COOKBOOK = """# Query cookbook

Worked examples for `execute_sql`. An outer LIMIT is always applied on top of
whatever you write.

## 1. What do we actually have in stock, most plentiful first
```sql
SELECT p.mpn, p.manufacturer, p.description, t.available, t.on_loan
FROM v_inventory_totals t
JOIN parts p USING (part_id)
WHERE t.available > 0
ORDER BY t.available DESC;
```

## 2. Where is a specific part stored
```sql
SELECT a.position_code, a.qty_on_hand, l.state, l.description
FROM v_inventory_available a
JOIN parts p USING (part_id)
JOIN locations l ON l.position_code = a.position_code
WHERE p.mpn = 'JMK105BJ105KV-F' AND a.qty_on_hand > 0;
```

## 3. Filter by specification (GIN-indexed containment)
```sql
SELECT mpn, attributes
FROM parts
WHERE category_id = 'capacitor_ceramic'
  AND attributes @> '{"dielectric":"X7R","package_code":"0402"}'::jsonb;
```

## 4. Numeric range on an attribute (values are base SI units)
```sql
SELECT mpn,
       (attributes->>'resistance_ohm')::numeric AS ohms,
       (attributes->>'tolerance_pct')::numeric  AS tol_pct
FROM parts
WHERE category_id = 'resistor_chip_smd'
  AND (attributes->>'resistance_ohm')::numeric BETWEEN 9500 AND 10500
ORDER BY ohms;
```

## 5. Browse by Digi-Key hierarchy instead of profile id
```sql
SELECT mpn, category_path
FROM parts
WHERE category_path_names @> '["Capacitors"]'::jsonb;
```

## 6. Running low (owned but nearly gone)
```sql
SELECT p.mpn, p.description, t.available
FROM v_inventory_totals t
JOIN parts p USING (part_id)
WHERE t.owned > 0 AND t.available <= 5
ORDER BY t.available;
```

## 7. What is currently checked out
```sql
SELECT p.mpn, o.position_code, o.qty_on_loan
FROM v_inventory_on_loan o
JOIN parts p USING (part_id)
WHERE o.qty_on_loan > 0;
```

## 8. Recent activity for a part
```sql
SELECT m.created_at, m.movement_type, m.quantity_delta, m.position_code, m.note
FROM movements m
JOIN parts p USING (part_id)
WHERE p.mpn = 'LM317T'
ORDER BY m.created_at DESC;
```

## 9. Spend by receiving event
```sql
SELECT i.invoice_number,
       min(i.created_at) AS received,
       sum(i.quantity_scanned)                    AS units,
       sum(i.quantity_scanned * i.unit_price)     AS total_cost
FROM intakes i
WHERE i.invoice_number IS NOT NULL
GROUP BY i.invoice_number
ORDER BY received DESC;
```

## 10. Which attribute keys exist for a category
```sql
SELECT k.key, count(*)::int AS n
FROM parts p
CROSS JOIN LATERAL jsonb_object_keys(p.attributes) AS k(key)
WHERE p.category_id = 'mosfet_single'
GROUP BY k.key
ORDER BY n DESC;
```

## 11. Parts the decoder could not classify
```sql
SELECT mpn, category_source_name, unknown_parameters
FROM parts
WHERE category_id IS NULL OR category_id LIKE 'unknown%';
```

## 12. Empty bins available for new stock
```sql
SELECT l.position_code, l.description, l.state
FROM locations l
LEFT JOIN v_inventory_available a
       ON a.position_code = l.position_code AND a.qty_on_hand > 0
WHERE a.position_code IS NULL
ORDER BY l.position_code;
```
"""

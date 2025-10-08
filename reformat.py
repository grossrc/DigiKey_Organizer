"""reformat.py
=====================================================================
Purpose
-------
One‑time (or occasional) maintenance utility to *re‑index* every part
already stored in the database using the **current** decoding /
categorization logic (profiles + traits in `profiles/` + `traits.yaml`).

Motivation
----------
As the ingestion / decoding code evolves (bug fixes, new attribute
normalization, profile tweaks, category heuristics, etc.), newly
scanned parts benefit from the improvements while older entries
retain legacy attribute mappings, category IDs, and
unknown parameter sets. This tool replays the *new* decoding logic against
the *old* vendor payload (`raw_vendor_json`) so historical parts are
brought up to parity **without** losing critical operational data.

What It Does (In-Place Update Strategy)
--------------------------------------
For each row in `public.parts` that has a non‑NULL `raw_vendor_json`:
 1. Re-run `decode_product(raw_vendor_json, REGISTRY)` to obtain the
    current canonical: `category_id`, `category_source_name`,
    `attributes`, and `unknown_parameters`.
 2. Recalculate lifecycle flags and guide unit price via existing
    helpers (`derive_lifecycle_flags`, `derive_unit_price`).
 3. Re-extract descriptive header fields (manufacturer, description,
    etc.) from the raw JSON (mirroring the current intake path) so any
    cleanup improvements are applied.
 4. Compare new values to what is stored; if different, issue an
    UPDATE (part_id is preserved; trigger updates `updated_at`).
 5. Upsert a row into `public.categories` for any (possibly new)
    category id / source name pair.

Preserved / NOT Touched
-----------------------
 - `part_id` (primary key)
 - `created_at` timestamp
 - Inventory / quantity history: lives in `intakes` & `movements`
 - `raw_vendor_json` (we do not alter it; it's the canonical source)
    -This fact must be understood and the fact that part status along
    with other fields are not updated. I.e. a part that has become
    obsolete in the vendor catalog since the original scan would still
    show as active. To reflect the current status, a separate process
    would be needed to identify and update such parts.

Optionally (flags) you can:
 - Run the script standalone to enact the changes.
 - Perform a dry run (`--dry-run`) to see a summary of planned changes
   without modifying the DB.
 - Limit processing to a subset (`--limit N` or `--mpn-filter LIKE`)
 - Skip header/lifecycle/price recalculation if you *only* want to
   rebuild category + attributes (`--attributes-only`).
 - Clean up category rows that are no longer referenced by any part
   (`--cleanup-unused-categories`). This is conservative and only
   deletes categories with zero referencing parts **after** updates.
 - Export a backup snapshot of the original parts rows to CSV
   (`--backup-csv path`) prior to any modifications.

Why In-Place Instead of Full Rebuild?
-------------------------------------
Updating rows in place is safe for downstream tables because all FKs
(`intakes.part_id`, `movements.part_id`, etc.) target the immutable
`part_id`. Category changes are also safe: we upsert categories first
and only update the FK value on `parts`; dependent rows reference
`parts`, not `categories` directly (except the optional lookup for
display). A full export / truncate / re-import cycle would be more
invasive and risks accidentally losing movement / intake history.
This script purposefully avoids any destructive operation on those
history tables.

When Might a Clean Rebuild Be Preferable?
----------------------------------------
If a future breaking change requires altering table *structure* (e.g.,
renaming columns, changing data types) or if historical `raw_vendor_json`
payloads are deemed untrustworthy / inconsistent, a staged export of
`part_id`, `raw_vendor_json`, and stock history followed by schema
migration could be warranted. For routine decoder/profile evolution,
this in-place method should remain robust.

Stability / Maintenance
-----------------------
Because the script delegates all domain logic to the same reusable
functions used in live ingestion (`decode_product`, lifecycle / price
helpers, profile registry loader), it should remain stable as long as
those function signatures stay consistent. If future refactors rename
or relocate those helpers, adjust the imports here—no additional logic
duplication should be needed.

Usage Examples
--------------
Dry run all parts:
    python reformat.py --dry-run

Actually apply updates (all parts):
    python reformat.py

Only reprocess first 100 parts, backing up to CSV:
    python reformat.py --limit 100 --backup-csv parts_backup.csv

Only rebuild attributes/categories (skip header + lifecycle + price):
    python reformat.py --attributes-only

Target only MPNs matching a pattern (SQL ILIKE):
    python reformat.py --mpn-filter "%TPS7A%"

Clean up unused categories after re-index:
    python reformat.py --cleanup-unused-categories

---------------------------------------------------------------------
DISCLAIMER: Always test with --dry-run first in a staging / local copy
of the database. Ensure you have physical or logical backups before
making bulk modifications.
---------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from contextlib import closing

from psycopg2.extras import RealDictCursor, Json

from db_helper import get_conn
from dk_decoder import load_registry, decode_product
from Scan_Part import derive_lifecycle_flags, derive_unit_price


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------
@dataclass
class PartRow:
    part_id: int
    mpn: str
    manufacturer: Optional[str]
    description: Optional[str]
    detailed_description: Optional[str]
    product_url: Optional[str]
    datasheet_url: Optional[str]
    image_url: Optional[str]
    unit_price: Optional[float]
    product_status: Optional[str]
    lifecycle_active: Optional[bool]
    lifecycle_obsolete: Optional[bool]
    category_id: Optional[str]
    category_source_name: Optional[str]
    attributes: Dict[str, Any]
    unknown_parameters: Dict[str, Any]
    raw_vendor_json: Dict[str, Any] | None

    @staticmethod
    def from_row(row: Dict[str, Any]) -> "PartRow":
        return PartRow(
            part_id=row["part_id"],
            mpn=row["mpn"],
            manufacturer=row.get("manufacturer"),
            description=row.get("description"),
            detailed_description=row.get("detailed_description"),
            product_url=row.get("product_url"),
            datasheet_url=row.get("datasheet_url"),
            image_url=row.get("image_url"),
            unit_price=row.get("unit_price"),
            product_status=row.get("product_status"),
            lifecycle_active=row.get("lifecycle_active"),
            lifecycle_obsolete=row.get("lifecycle_obsolete"),
            category_id=row.get("category_id"),
            category_source_name=row.get("category_source_name"),
            attributes=row.get("attributes") or {},
            unknown_parameters=row.get("unknown_parameters") or {},
            raw_vendor_json=row.get("raw_vendor_json"),
        )


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------
def fetch_parts(limit: Optional[int], mpn_filter: Optional[str]) -> List[PartRow]:
    sql = [
        "SELECT part_id, mpn, manufacturer, description, detailed_description,",
        "       product_url, datasheet_url, image_url, unit_price, product_status,",
        "       lifecycle_active, lifecycle_obsolete, category_id, category_source_name,",
        "       attributes, unknown_parameters, raw_vendor_json",
        "  FROM public.parts",
    ]
    params: List[Any] = []
    if mpn_filter:
        sql.append(" WHERE mpn ILIKE %s")
        params.append(mpn_filter)
    sql.append(" ORDER BY part_id ASC")
    if limit:
        sql.append(" LIMIT %s")
        params.append(limit)

    with closing(get_conn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("\n".join(sql), params)
        return [PartRow.from_row(r) for r in cur.fetchall()]


def extract_header_from_raw(raw_vendor_json: Dict[str, Any]) -> Dict[str, Any]:
    prod = (raw_vendor_json or {}).get("Product", {}) or {}
    return {
        "mpn": prod.get("ManufacturerProductNumber"),  # should match existing
        "manufacturer": (prod.get("Manufacturer") or {}).get("Name"),
        "description": ((prod.get("Description") or {}).get("ProductDescription") or "").strip() or None,
        "detailed_description": ((prod.get("Description") or {}).get("DetailedDescription") or "").strip() or None,
        "product_url": prod.get("ProductUrl"),
        "datasheet_url": prod.get("DatasheetUrl"),
        "image_url": prod.get("PhotoUrl"),
    }


def compute_new_state(
    row: PartRow,
    registry: Dict[str, dict],
    attributes_only: bool,
    recompute_price: bool,
    recompute_lifecycle: bool,
) -> Dict[str, Any] | None:
    """Return dict of updated columns (excluding part_id) or None if no changes."""
    if not row.raw_vendor_json:
        return None  # nothing to re-decode

    decoded = decode_product(row.raw_vendor_json, registry)
    new_cat_id = decoded.get("category_id")
    new_cat_name = decoded.get("category_source_name")

    new_attrs = decoded.get("attributes") or {}
    new_unknown = decoded.get("unknown_parameters") or {}

    updates: Dict[str, Any] = {}

    # Always consider category + attributes
    if new_cat_id != row.category_id or new_cat_name != row.category_source_name:
        updates["category_id"] = new_cat_id
        updates["category_source_name"] = new_cat_name
    if new_attrs != row.attributes:
        updates["attributes"] = new_attrs
    if new_unknown != row.unknown_parameters:
        updates["unknown_parameters"] = new_unknown

    if not attributes_only:
        header = extract_header_from_raw(row.raw_vendor_json)
        # Price & lifecycle flags optionally recomputed
        prod = (row.raw_vendor_json or {}).get("Product", {}) or {}
        if recompute_lifecycle:
            lifec = derive_lifecycle_flags(prod)
        else:
            lifec = {
                "product_status": row.product_status,
                "lifecycle_active": row.lifecycle_active,
                "lifecycle_obsolete": row.lifecycle_obsolete,
            }
        if recompute_price:
            price = derive_unit_price(prod)
        else:
            price = row.unit_price

        # Compare & queue updates
        compare_fields = {
            "manufacturer": header.get("manufacturer"),
            "description": header.get("description"),
            "detailed_description": header.get("detailed_description"),
            "product_url": header.get("product_url"),
            "datasheet_url": header.get("datasheet_url"),
            "image_url": header.get("image_url"),
            "unit_price": price,
            "product_status": lifec.get("product_status"),
            "lifecycle_active": lifec.get("lifecycle_active"),
            "lifecycle_obsolete": lifec.get("lifecycle_obsolete"),
        }
        for k, v in compare_fields.items():
            if getattr(row, k) != v:
                updates[k] = v

    return updates or None


def upsert_category(cur, category_id: str | None, source_name: str | None):
    if not category_id:
        return
    cur.execute(
        """
        INSERT INTO public.categories (category_id, source_name)
        VALUES (%s, %s)
        ON CONFLICT (category_id) DO UPDATE SET source_name = EXCLUDED.source_name;
        """,
        (category_id, source_name),
    )


def apply_updates(rows: List[PartRow], planned: Dict[int, Dict[str, Any]], cleanup_unused: bool):
    if not planned:
        print("No changes to apply.")
        return
    with closing(get_conn()) as conn:
        with conn:  # transaction
            with conn.cursor() as cur:
                for part_id, changes in planned.items():
                    cat_id = changes.get("category_id")
                    cat_name = changes.get("category_source_name")
                    if cat_id:
                        upsert_category(cur, cat_id, cat_name)

                    sets = []
                    params = []
                    for k, v in changes.items():
                        if k in ("attributes", "unknown_parameters"):
                            sets.append(f"{k} = %s")
                            params.append(Json(v))
                        else:
                            sets.append(f"{k} = %s")
                            params.append(v)
                    # updated_at will be handled by trigger BEFORE UPDATE
                    params.append(part_id)
                    sql = f"UPDATE public.parts SET {', '.join(sets)} WHERE part_id = %s"
                    cur.execute(sql, params)

                if cleanup_unused:
                    cur.execute(
                        """
                        DELETE FROM public.categories c
                        WHERE NOT EXISTS (
                            SELECT 1 FROM public.parts p WHERE p.category_id = c.category_id
                        );
                        """
                    )
    print("Applied updates to", len(planned), "parts.")


def write_backup_csv(rows: List[PartRow], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "part_id", "mpn", "manufacturer", "description", "detailed_description",
            "product_url", "datasheet_url", "image_url", "unit_price", "product_status",
            "lifecycle_active", "lifecycle_obsolete", "category_id", "category_source_name",
            "attributes_json", "unknown_parameters_json"
        ])
        for r in rows:
            w.writerow([
                r.part_id, r.mpn, r.manufacturer, r.description, r.detailed_description,
                r.product_url, r.datasheet_url, r.image_url, r.unit_price, r.product_status,
                r.lifecycle_active, r.lifecycle_obsolete, r.category_id, r.category_source_name,
                json.dumps(r.attributes, ensure_ascii=False),
                json.dumps(r.unknown_parameters, ensure_ascii=False)
            ])
    print(f"Backup CSV written: {path}")


# ------------------------------------------------------------------
# Main CLI
# ------------------------------------------------------------------
def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Re-index existing parts using current decoder logic.")
    p.add_argument("--dry-run", action="store_true", help="Show planned changes only; no DB writes")
    p.add_argument("--limit", type=int, help="Process only first N parts (by part_id ascending)")
    p.add_argument("--mpn-filter", help="SQL ILIKE pattern to restrict MPNs (e.g. %TPS7A%)")
    p.add_argument("--attributes-only", action="store_true", help="Only rebuild category + attributes; skip headers, lifecycle, price")
    p.add_argument("--no-recompute-price", action="store_true", help="Do NOT recompute unit_price; keep existing")
    p.add_argument("--no-recompute-lifecycle", action="store_true", help="Do NOT recompute lifecycle flags; keep existing")
    p.add_argument("--cleanup-unused-categories", action="store_true", help="Delete category rows no longer referenced after updates")
    p.add_argument("--backup-csv", help="Path to write a CSV snapshot BEFORE applying changes")
    return p.parse_args(argv)


def main(argv: List[str]):
    args = parse_args(argv)

    print("Loading profile registry...")
    registry = load_registry(profiles_dir="profiles", traits_path="traits.yaml")
    rows = fetch_parts(limit=args.limit, mpn_filter=args.mpn_filter)
    print(f"Fetched {len(rows)} part rows for evaluation.")

    if args.backup_csv and not args.dry_run:
        write_backup_csv(rows, args.backup_csv)

    planned: Dict[int, Dict[str, Any]] = {}
    changed_cats = 0
    changed_attrs = 0
    total_updates = 0

    for r in rows:
        upd = compute_new_state(
            r,
            registry=registry,
            attributes_only=args.attributes_only,
            recompute_price=not args.no_recompute_price,
            recompute_lifecycle=not args.no_recompute_lifecycle,
        )
        if not upd:
            continue
        planned[r.part_id] = upd
        total_updates += 1
        if any(k in upd for k in ("category_id", "category_source_name")):
            changed_cats += 1
        if any(k in upd for k in ("attributes", "unknown_parameters")):
            changed_attrs += 1

    # Summary
    print("\nSummary (planned changes)")
    print("-------------------------")
    print("Parts needing update:", total_updates)
    print(" - With category changes:", changed_cats)
    print(" - With attribute/unknown changes:", changed_attrs)
    if args.dry_run:
        # Show a sample of diffs
        sample = list(planned.items())[:10]
        for part_id, changes in sample:
            print(f"Part {part_id}: {list(changes.keys())}")
        if total_updates > len(sample):
            print(f"... ({total_updates - len(sample)} more)")
        print("\n(Dry run) No database changes applied.")
        return 0

    print("Applying updates...")
    apply_updates(rows, planned, cleanup_unused=args.cleanup_unused_categories)
    print("Done.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))

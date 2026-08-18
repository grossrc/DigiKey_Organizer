# Parts Database — Orientation for LLM Clients

This is the inventory database behind a small electronics-lab parts organizer.
Parts are scanned in from Digi-Key packaging, stored in physical bins, and
checked out again. **Everything here is read-only.**

Read this document first, then `schema://profiles` and `schema://jsonb` before
writing any query against the `attributes` column.

---

## The five tables and their grain

| Relation | One row is... | Key |
|---|---|---|
| `parts` | one distinct part number in the catalog | `part_id` (PK), `mpn` (UNIQUE) |
| `intakes` | one *receiving event* (a scan of a Digi-Key bag) | `intake_id` |
| `movements` | one *ledger entry* changing quantity at a bin | `movement_id` |
| `locations` | one physical bin / storage position | `position_code` (PK, text) |
| `categories` | one part-category identifier | `category_id` (PK, text) |

`parts` is the catalog master. `intakes` and `movements` are event logs — a part
with ten intakes is still one row in `parts`.

---

## How to compute stock — read this carefully

**`movements` is the authoritative quantity ledger.** Current quantity is a sum
of signed deltas, never a column you can read directly:

```sql
SELECT part_id, SUM(quantity_delta) AS qty
FROM movements
GROUP BY part_id;
```

`quantity_delta` is positive for intake and negative for consumption/checkout.
`movement_type` is one of `intake`, `transfer`, `consumption`, `adjustment`.

**Bins whose `position_code` starts with `OUT` mean "checked out / on loan"**,
not "in storage". The shipped views already encode this and you should prefer
them over hand-rolled sums:

- `v_inventory_available` — `(part_id, position_code, qty_on_hand)` for bins **not** matching `OUT%`
- `v_inventory_on_loan` — `(part_id, position_code, qty_on_loan)` for bins matching `OUT%`
- `v_inventory_totals` — `(part_id, available, on_loan, owned)`, one row per part, zero-filled
- `v_current_inventory` — sums `intakes.quantity_scanned`, i.e. *lifetime received*, **not** current stock

> Pitfall: `v_current_inventory` and `intakes.quantity_scanned` describe what was
> ever received. If someone asks "how many do we have", use `v_inventory_totals.available`.

`locations.state` is a coarse UI label (`Available`, `Reserved`, `Stocked`,
`Checked out`) maintained by a trigger. It is a hint, not a quantity — do not
derive stock from it.

---

## Where a part physically lives

`intakes.part_cataloged_position` and `movements.position_code` both reference
`locations.position_code`. To find a part's bins:

```sql
SELECT position_code, qty_on_hand
FROM v_inventory_available
WHERE part_id = $1 AND qty_on_hand > 0;
```

---

## The JSONB columns — where the real data is

Standard column introspection tells you almost nothing about this database,
because the electrical specifications live in JSONB.

### `parts.attributes` — normalized, canonical, queryable
A flat object of decoded specifications, keyed by **canonical** names whose
suffix carries the unit:

| Suffix | Unit | Example |
|---|---|---|
| `_f` | farads | `"capacitance_f": 1e-9` (1 nF) |
| `_ohm` | ohms | `"resistance_ohm": 10000` |
| `_v` | volts | `"voltage_rating_v": 50` |
| `_w` | watts | `"power_rating_w": 0.25` |
| `_a` | amperes | `"current_rating_a": 2` |
| `_hz` | hertz | `"frequency_hz": 16000000` |
| `_pct` | percent | `"tolerance_pct": 5` |
| `_ppm` | parts per million | `"temp_coefficient_ppm": 100` |

**Values are stored in base SI units, never with a prefix.** A 10 kΩ resistor is
`10000`, a 100 nF capacitor is `1e-7`. Never compare against `"10k"` or `"100nF"`.

Which keys exist depends on the part's `category_id`. `schema://jsonb` lists the
observed keys per category with sample values; `schema://profiles` gives the
authoritative key definitions, allowed enum values, and validators.

There is a GIN index on this column, so containment is the fast way to filter:

```sql
SELECT mpn FROM parts
WHERE attributes @> '{"dielectric":"X7R"}'::jsonb;   -- uses idx_parts_attributes_gin
```

Use `->>` (text) or `->` + a cast for range comparisons, which do **not** use the
GIN index but are still fine on a catalog this size:

```sql
SELECT mpn, (attributes->>'resistance_ohm')::numeric AS r
FROM parts
WHERE category_id = 'resistor_chip_smd'
  AND (attributes->>'resistance_ohm')::numeric BETWEEN 900 AND 1100;
```

### `parts.unknown_parameters` — vendor parameters with no canonical mapping
Raw `{"Digi-Key Parameter Name": "value text"}` pairs, always strings, never
normalized. Check here when an expected specification is missing from
`attributes` — it usually means the profile has no mapping for it yet.

### `parts.raw_vendor_json` — the full Digi-Key API response
**Do not select this column in normal queries.** A single row can be tens of
kilobytes and will exhaust the response budget. It exists only as a verification
fallback: if `attributes` *and* `unknown_parameters` both appear to be missing a
specification that must exist, call `get_part` with
`include_raw_vendor_json: true` for that one part.

### `intakes.raw_scan_fields`
The parsed barcode field dictionary from the Digi-Key label (invoice number, lot
code, quantity, DKPN, ...). Most useful fields are already promoted to real
columns on `intakes`.

---

## Categories

`parts.category_id` is a *profile id* such as `resistor_chip_smd`,
`capacitor_ceramic`, or `mosfet_single` — **not** a Digi-Key category name. Parts
the decoder could not classify get an `unknown_*` slug.

- `parts.category_source_name` — the deepest Digi-Key category name
- `parts.category_path` — full hierarchy as text, joined with ` › `
- `parts.category_path_names` — the same hierarchy as a JSONB string array (GIN indexed)

To resolve a human phrase like "ceramic capacitors" to a `category_id`, use the
`list_categories` tool or `schema://categories` rather than guessing.

```sql
SELECT mpn FROM parts
WHERE category_path_names @> '["Resistors"]'::jsonb;
```

---

## Suggested workflow

1. `schema://categories` or `list_categories` — map the user's words to a `category_id`.
2. `schema://profiles` / `attribute_keys` — learn the exact attribute keys and units for that category.
3. `search_parts` — structured filtering; covers most questions with no SQL.
4. `execute_sql` — anything the curated tools cannot express. Read-only SELECT only; an outer `LIMIT` is always applied.
5. `explain_sql` — sanity-check an expensive query before running it.

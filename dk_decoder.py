# dk_decoder.py
# ----------------------------------------------------------------------
# Data-driven Digi-Key decoder:
# - load_registry(profiles_dir, traits_path=None)
# - decode_product(raw_json, registry)
#
# Expects YAML profiles in `profiles_dir/` and (optionally) a traits.yaml
# anywhere you like (pass its path as traits_path). If traits_path is None
# or missing, traits are simply empty and profiles still load.
#
# Compatible with Python 3.8+.
# ----------------------------------------------------------------------

import re
import pathlib
from typing import Dict, Any, Tuple, Optional

import yaml

# ----------------------------- Registry -------------------------------

def load_registry(profiles_dir: str, traits_path: Optional[str] = None) -> Dict[str, dict]:
    """
    Load all YAML profiles in `profiles_dir` and optional traits from `traits_path`.
    Returns a dict keyed by profile id.

    - Skips any file named 'traits.yaml' inside profiles_dir.
    - traits_path can be absolute or relative. If it doesn't exist, traits={}.

    Usage:
        REGISTRY = load_registry("profiles", "traits.yaml")
        # or just:
        REGISTRY = load_registry("profiles")
    """
    # 1) Load traits (optional)
    traits: Dict[str, Any] = {}
    if traits_path:
        tpath = pathlib.Path(traits_path)
        if tpath.exists():
            with tpath.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                # Support both formats: entire file is the traits dict,
                # or nested under a top-level "traits" key.
                traits = data.get("traits", data) or {}

    # 2) Load profiles
    profiles: Dict[str, dict] = {}
    pdir = pathlib.Path(profiles_dir)
    if not pdir.exists():
        raise FileNotFoundError(f"profiles_dir not found: {pdir}")

    for p in pdir.glob("*.yaml"):
        if p.name.lower() == "traits.yaml":
            # Never treat traits.yaml as a profile
            continue
        y = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if "id" not in y:
            # Skip files that aren't valid profiles
            continue
        # Attach the traits dictionary for trait-based parsing
        y["traits_def"] = traits
        profiles[y["id"]] = y

    if not profiles:
        raise RuntimeError(f"No profiles loaded from {pdir}. "
                           f"Ensure you have YAML files with an `id` and `source_categories`.")

    return profiles

# --------------------------- Parse helpers ----------------------------

_SI = {
    "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
    "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15
}

def _num(x: str) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def parse_number_with_prefix(s: str) -> Optional[float]:
    """
    Parses strings like "0.022 µF", "2.5A", "1kΩ" into a float in base units.
    Unit suffix is ignored; only SI prefix is applied.
    """
    s = (s or '').strip()
    # Extract the leading numeric value first
    m = re.match(r'^\s*([+-]?\d+(?:\.\d+)?)', s)
    if not m:
        return None
    val = m.group(1)
    n = _num(val)
    if n is None:
        return None

    # Inspect the remainder of the string for SI prefix hints. Many vendors
    # write things like '100 mOhm', '100mΩ', '100 milliohm' or '0.1 Ohm'.
    # We look for a symbol or word that indicates the SI prefix and apply it.
    rest = s[m.end():].strip().lower()

    # Quick symbol detection (single-letter or unicode mu). Prefer the first
    # meaningful prefix found in the remainder.
    if rest:
        # Common single-letter prefixes (case-insensitive) and unicode mu
        for sym, mult in (('k', 1e3), ('m', 1e-3), ('u', 1e-6), ('µ', 1e-6),
                          ('n', 1e-9), ('p', 1e-12), ('f', 1e-15), ('g', 1e9), ('t', 1e12)):
            # Match symbol either as standalone token or as the first letter of a unit
            # e.g., 'mΩ', 'mohm', ' milliohm', ' mOhm'
            if re.search(r'(^|[^a-z0-9])' + re.escape(sym) + r'(?=[^a-z0-9]|[a-z]|$)', rest, flags=re.I):
                return n * mult

        # Full-word prefixes (e.g., 'milli', 'micro')
        if re.search(r'\bmilli|\bmilliohm|\bmilliohms\b', rest):
            return n * 1e-3
        if re.search(r'\bmicro|\bmicroohm|\bmicroohms\b', rest) or '\u00b5' in rest:
            return n * 1e-6
        if re.search(r'\bnano', rest):
            return n * 1e-9
        if re.search(r'\bpico', rest):
            return n * 1e-12

    # No prefix detected — return the numeric value unchanged
    return n


def parse_number_with_unit(s: str) -> Optional[Tuple[float, Optional[str]]]:
    """
    Parse a string into (numeric_value_in_base_units, normalized_unit_base) where
    normalized_unit_base is a canonical unit name such as 'ohm', 'a', 'v', 'f', 'w', 'hz', 'ah'
    or None if no unit could be detected. This is conservative: it will not coerce
    between incompatible units (e.g., 'mAh' -> 'Ah' is detected, but it's considered
    distinct from 'A').
    """
    if not s:
        return None
    s = s.strip()
    # Extract leading number
    m = re.match(r'^\s*([+-]?\d+(?:\.\d+)?)(.*)$', s)
    if not m:
        return None
    val_str = m.group(1)
    rest = (m.group(2) or '').strip()
    n = _num(val_str)
    if n is None:
        return None

    rest_l = rest.lower()

    # Determine canonical unit base from the remainder
    unit = None

    # OHM family (include the literal Greek capital omega 'Ω')
    if re.search(r'ohm|Ω|omega|mohm|milliohm', rest_l, flags=re.I):
        unit = 'ohm'
    # AMP / CURRENT
    elif re.search(r'\bma\b|milliamp|milliampere|\bmilli amp\b|\bamp\b|\bamperes?\b|\ba\b', rest_l, flags=re.I):
        # Careful: catch 'mah' (capacity) separately below
        if 'mah' in rest_l or re.search(r'\bmah\b', rest_l):
            unit = 'ah'  # capacity (Ah), distinct from current (A)
        else:
            unit = 'a'
    elif re.search(r'\bah\b|milliamp-hour|milliamphour|amp-hour', rest_l):
        unit = 'ah'
    # VOLT
    elif re.search(r'\bv\b|volt|volts', rest_l):
        unit = 'v'
    # FARAD / CAPACITANCE
    elif re.search(r'farad|f\b|f\s|microfarad|nf|pf|uf|\buf\b|\bmu f\b', rest_l, flags=re.I):
        unit = 'f'
    # WATT
    elif re.search(r'\bw\b|watt', rest_l):
        unit = 'w'
    # HERTZ
    elif re.search(r'hz|khz|mhz|ghz', rest_l, flags=re.I):
        unit = 'hz'
    else:
        # Try to infer from short unit tokens (single-letter or prefixed letter)
        # e.g., 'mohm', 'mΩ', 'mA', 'mAh'
        if re.search(r'\bm?ohm\b|mΩ', rest_l):
            unit = 'ohm'
        elif re.search(r'\bm?ah\b', rest_l, flags=re.I):
            unit = 'ah'
        elif re.search(r'\bm?a\b', rest_l, flags=re.I):
            unit = 'a'

    # Apply SI prefix if present in the rest. Look for e.g. 'm', 'k', 'M', 'µ', 'u', 'n', 'p'
    multiplier = 1.0
    # Search for a prefix token either immediately before unit or as standalone token
    pref_m = re.search(r'\b(k|K|M|G|T|m|u|µ|n|p|f)\b', rest)
    if not pref_m:
        # also check single-char prefix attached to unit like 'mohm' or 'mΩ' or 'mA'
        pref_m2 = re.match(r'^\s*([kKMGMmµunpfd])', rest_l)
        if pref_m2:
            pref = pref_m2.group(1)
            multiplier = _SI.get(pref, 1.0)
    else:
        pref = pref_m.group(1)
        multiplier = _SI.get(pref, 1.0)

    # If rest contains explicit symbol like 'mΩ' where prefix is directly attached to unit
    # attempt to capture it as well (e.g., 'mΩ' -> 'm')
    m_sym = re.search(r'([kKMGMµunpf])\s*(?:ohm|Ω|a|v|f|hz)?', rest)
    if m_sym:
        multiplier = _SI.get(m_sym.group(1), multiplier)

    value_in_base = n * multiplier
    return (value_in_base, unit)

def parse_range(s: str, inner_parser):
    """
    Splits ranges like "-55°C ~ 125°C", "2.7V to 5.5V", "1–10".
    Avoid splitting on leading negatives by using a numeric-boundary hyphen.
    """
    # First try explicit words/symbols that aren't ambiguous
    for sep in [r'~', r'\bto\b', r'–', r'—']:
        parts = re.split(rf'\s*{sep}\s*', s)
        if len(parts) == 2:
            return inner_parser(parts[0]), inner_parser(parts[1])

    # Fallback: hyphen only when it's between numbers
    parts = re.split(r'(?<=\d)\s*-\s*(?=\d)', s)
    if len(parts) == 2:
        return inner_parser(parts[0]), inner_parser(parts[1])

    return (None, None)


def parse_temp_c(s: str) -> Optional[float]:
    m = re.search(r'([+-]?\d+(?:\.\d+)?)', s)
    return _num(m.group(1)) if m else None

def parse_mm(s: str):
    """
    Returns a list of metric values found, e.g. "1.00mm x 0.50mm" -> [1.0, 0.5]
    Prefers metric text; if only inches are present, returns empty list.
    """
    mm = re.findall(r'([\d\.]+)\s*mm', s, re.I)
    return [_num(x) for x in mm] if mm else []

def parse_package(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    "0402 (1005 Metric)" -> ("0402", "1005")
    """
    m = re.match(r'^\s*([0-9A-Za-z\-\.]+)\s*(?:\(([\d\s]+)\s*Metric\))?', text)
    if not m:
        return (None, None)
    code = m.group(1)
    metric = m.group(2).replace(" ", "") if m.group(2) else None
    return code, metric

# ----------------------- Category & mapping utils ---------------------

def deepest_category_name(cat: Dict[str, Any]) -> Optional[str]:
    """
    Returns the deepest available Digi-Key category name from the Category object.
    """
    if not cat:
        return None
    name = cat.get("Name")
    children = cat.get("ChildCategories") or []
    if children:
        node = children[0]
        while node.get("ChildCategories"):
            node = node["ChildCategories"][0]
        return node.get("Name") or name
    return name

def match_profile_by_source_category(registry, category_obj):
    names = category_name_path(category_obj)
    names_lc = [n.lower() for n in names]
    # Avoid very generic single-token matches (e.g., "capacitor(s)")
    GENERIC_TOKENS = {
        "capacitors", "capacitor", "resistors", "resistor",
        "connectors", "connector", "inductors", "inductor",
        "diodes", "diode", "led", "leds", "module", "modules",
        "ic", "ics", "semiconductors",
    }

    for prof in registry.values():
        sc_list = prof.get("source_categories", []) or []
        sc = [s.lower() for s in sc_list]

        # 1) exact (case-insensitive)
        if any(n in sc for n in names_lc):
            return prof

        # 2) regex patterns (allow profiles to opt into precise matching)
        for pat in prof.get("source_category_patterns", []) or []:
            rx = re.compile(pat, flags=re.I)
            if any(rx.search(n) for n in names):
                return prof

        # 3) safer substring / word-boundary matching
        #    - require the matched token not be a highly-generic word like "capacitors"
        #    - use word-boundary regex so we don't match tiny fragments
        for s_orig in sc_list:
            s = (s_orig or "").strip().lower()
            if not s or s in GENERIC_TOKENS:
                continue
            for n in names:
                n_l = (n or "").strip().lower()
                if not n_l or n_l in GENERIC_TOKENS:
                    continue
                # word-boundary either way
                if re.search(r"\b" + re.escape(s) + r"\b", n, flags=re.I) or re.search(r"\b" + re.escape(n_l) + r"\b", s, flags=re.I):
                    return prof

    return None


def _slugify(s: str) -> str:
    """Create a filesystem/DB-safe slug from a category name.
    Keeps lowercase letters, digits, and dashes. Replaces whitespace and
    punctuation with dashes and collapses repeats. In the future, it would be ideal 
    to make this a separate column flag in the DB, but this functionality is being
    built out on an existing DB schema for now.
    """
    if not s:
        return ""
    # Lowercase
    s2 = s.lower()
    # Replace non-alphanumeric with dash
    import re

    s2 = re.sub(r"[^a-z0-9]+", "-", s2)
    s2 = re.sub(r"-+", "-", s2).strip("-")
    return s2 or "unknown"


def first_present(d: Dict[str, str], keys) -> Optional[str]:
    for k in keys:
        if k in d and d[k] not in (None, "", "-"):
            return d[k]
    return None

def category_name_path(category_obj):
    """Return a list of category names from top to deepest child."""
    out = []
    def walk(node):
        if not node:
            return
        name = node.get("Name")
        if name:
            out.append(name)
        for c in node.get("ChildCategories") or []:
            walk(c)
    walk(category_obj or {})
    return out

def _prefer_rf_module_if_applicable(profile: Optional[dict],
                                    registry: Dict[str, dict],
                                    params_dict: Dict[str, str]) -> Optional[dict]:
    """
    If an item looks like an RF MCU/module (e.g., ESP32, nRF52 modules),
    prefer the `rf_mcu_module` profile over `mcu` (or over a mis-hit like battery chargers).
    This only triggers when RF-ish parameters are present.
    """
    # If we already matched a profile that is NOT 'mcu' or 'battery_charger', keep it.
    if profile and profile.get("id") not in {"mcu", "battery_charger"}:
        return profile

    keys = set(params_dict.keys())
    rf_keys = {
        "RF Family/Standard", "RF Family", "Standard",
        "Frequency", "Frequency - Center/Band",
        "Data Rate", "Data Rate - Max",
        "Power - Output", "Output Power", "Sensitivity",
        "Antenna Type", "Approvals", "Certifications", "Regulatory Certifications"
    }

    # Only flip if we have strong RF signals AND the rf_mcu_module profile exists
    if keys.intersection(rf_keys) and "rf_mcu_module" in registry:
        return registry["rf_mcu_module"]

    return profile

# ------------------------- Normalization layer ------------------------

def normalize_value(canon_key: str, text: str) -> Optional[Any]:
    """
    Map text to a canonical Python value given a canonical key.
    Numeric keys end with unit hints (_f, _ohm, _v, _a, _w, _hz).
    """
    t = text.strip()

    # numeric with SI — use the more robust parser that returns (value, unit)
    if canon_key.endswith(("_f", "_ohm", "_v", "_a", "_w", "_hz")):
        parsed = parse_number_with_unit(t)
        if not parsed:
            return None
        value, unit = parsed

        # Map canon_key to expected base unit
        expected = None
        if canon_key.endswith("_ohm"):
            expected = 'ohm'
        elif canon_key.endswith("_f"):
            expected = 'f'
        elif canon_key.endswith("_v"):
            expected = 'v'
        elif canon_key.endswith("_a"):
            expected = 'a'
        elif canon_key.endswith("_w"):
            expected = 'w'
        elif canon_key.endswith("_hz"):
            expected = 'hz'

        # If we detected a unit, ensure it's compatible with the expected unit.
        # Conservative approach: if unit is None (no unit text found), accept numeric value as-is.
        if unit is None:
            return value

        if expected == unit:
            return value

        # If units are closely related (e.g., 'ohm' variants), allow
        # 'ohm' synonyms have already been normalized to 'ohm' so this is covered.

        # Mismatch (e.g., 'ah' vs 'a'): do not coerce automatically; return None
        # so callers can decide handling or manual review can be triggered.
        return None

    # ppm
    if canon_key.endswith("_ppm"):
        m = re.search(r'([0-9\.]+)', t)
        return _num(m.group(1)) if m else None

    # tolerance percent
    if canon_key == "tolerance_pct":
        m = re.search(r'([0-9\.]+)\s*%', t)
        return _num(m.group(1)) if m else None

    # temp range -> split into min/max keys
    if canon_key == "operating_temp_range":
        a, b = parse_range(t, parse_temp_c)
        return {"operating_temp_min_c": a, "operating_temp_max_c": b} if a is not None and b is not None else None

    # raw text that a trait will later parse
    if canon_key in ("size_dim_text", "thickness_text", "package_case_text"):
        return {"_raw_" + canon_key: t}

    # pass-through text enums / strings
    return t

def apply_traits(profile: dict, params_by_text: Dict[str, str]) -> Dict[str, Any]:
    """
    Use trait definitions (from traits.yaml) to derive structured fields.
    """
    out: Dict[str, Any] = {}
    trait_defs = profile.get("traits_def", {}) or {}
    for trait in profile.get("traits", []):
        spec = trait_defs.get(trait, {}) or {}

        if trait == "has_package_code":
            src = first_present(params_by_text, spec.get("sources", []))
            if src:
                code, metric = parse_package(src)
                if code: out["package_code"] = code
                if metric: out["package_metric"] = metric

        elif trait == "has_mounting_type":
            src = first_present(params_by_text, spec.get("sources", []))
            if src:
                syn = (spec.get("synonyms") or {})
                mt = syn.get(src, src)
                out["mounting_type"] = "SMD" if "Surface Mount" in mt else ("TH" if "Through" in mt else mt)

        elif trait == "has_operating_temp":
            src = first_present(params_by_text, spec.get("sources", []))
            if src:
                a, b = parse_range(src, parse_temp_c)
                if a is not None: out["operating_temp_min_c"] = a
                if b is not None: out["operating_temp_max_c"] = b

        elif trait == "has_dimensions":
            dim = params_by_text.get("Size / Dimension")
            if dim:
                mm = parse_mm(dim)
                if len(mm) >= 2:
                    out["size_l_mm"], out["size_w_mm"] = mm[0], mm[1]
            h = params_by_text.get("Height - Seated (Max)") or params_by_text.get("Height")
            if h:
                mm = parse_mm(h)
                if mm:
                    out["height_mm"] = mm[0]
            th = params_by_text.get("Thickness (Max)")
            if th:
                mm = parse_mm(th)
                if mm:
                    out["thickness_mm"] = mm[0]

        elif trait == "has_voltage_current_basic":
            v = first_present(params_by_text, ["Voltage - Rated", "Voltage - DC", "Voltage Rating"])
            if v:
                out["voltage_rating_v"] = parse_number_with_prefix(v)
            c = first_present(params_by_text, ["Current", "Current - Output", "Current - Average Rectified (Io)"])
            if c:
                out["current_a"] = parse_number_with_prefix(c)

        elif trait == "has_interface":
            src = first_present(params_by_text, spec.get("sources", []))
            if src:
                out["interface"] = src.replace(" ", "").upper() if len(src) <= 6 else src

    return out

# --------------------------- Public decoder ---------------------------

def decode_product(raw_json: Dict[str, Any], registry: Dict[str, dict]) -> Dict[str, Any]:
    """
    Given one Digi-Key product JSON object and a loaded registry,
    returns a dict with:
      - category_id            (matched internal profile id or 'unknown_other')
      - category_source_name   (deepest Digi-Key category name)
      - attributes             (canonicalized key->value)
      - unknown_parameters     (any vendor parameters not mapped by profile)
    """
    prod = raw_json.get("Product", {}) or {}

    # 1) Determine the deepest Digi-Key category name, then match a profile
    category_obj = prod.get("Category", {}) or {}
    profile = match_profile_by_source_category(registry, category_obj)

    # Keep deepest name for reporting
    path = category_name_path(category_obj)
    src_cat = path[-1] if path else None

    # Build params NOW so we can run the RF module tie-break
    params = {p.get("ParameterText"): p.get("ValueText")
              for p in prod.get("Parameters", [])
              if p.get("ParameterText")}

    # Prefer rf_mcu_module over mcu/battery_charger when RF keys are present
    profile = _prefer_rf_module_if_applicable(profile, registry, params)

    if profile:
        cat_id = profile["id"]
    else:
        # For unmatched categories, generate a deterministic id from the
        # deepest source category name so different unknown categories do
        # not all map to a single 'unknown_other' id (which causes clobbers).
        src_name = src_cat or "unknown"
        cat_id = "unknown_" + _slugify(src_name)

    # 2) Build a lookup of Digi-Key parameter text -> value
    params = {p.get("ParameterText"): p.get("ValueText") for p in prod.get("Parameters", []) if p.get("ParameterText")}

    # 3) Extract mapped attributes per profile
    attrs: Dict[str, Any] = {}
    unknown: Dict[str, Any] = {}

    if profile:
        # Profile-driven mapping + normalization
        attr_map: Dict[str, list] = (profile.get("attributes", {}) or {}).get("map", {}) or {}
        for canon_key, source_keys in attr_map.items():
            # Find the first present source key in the vendor params
            val_text = next((params.get(k) for k in source_keys if k in params), None)
            # skip placeholder values like "-" so they don't pollute attributes
            if val_text is None or str(val_text).strip() == "-":
                continue

            norm = normalize_value(canon_key, val_text)
            if isinstance(norm, dict):
                # e.g., operating_temp_range -> split min/max dict
                attrs.update(norm)
            elif norm is not None:
                attrs[canon_key] = norm


        # Traits may derive more canonical fields
        attrs.update(apply_traits(profile, params))

        # Anything not mapped goes into unknown_parameters for later analysis
        mapped_sources = {s for lst in attr_map.values() for s in lst}
        for k, v in params.items():
            if k not in mapped_sources:
                unknown[k] = v
    else:
        # Unknown category: expose all params as unknown so nothing is lost
        unknown = params

    return {
        "category_id": cat_id,
        "category_source_name": src_cat,
        "attributes": attrs,
        "unknown_parameters": unknown
    }

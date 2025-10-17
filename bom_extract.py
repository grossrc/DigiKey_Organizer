"""
BOM Parser & Matcher
Parses BOMs from various EDA tools (KiCad, Eagle, Altium, generic CSV)
and matches against local inventory with fuzzy matching for close alternatives.
"""
from __future__ import annotations
import csv
import re
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from io import StringIO


@dataclass
class BOMLine:
    """Single line from a BOM."""
    row_num: int
    designators: List[str] = field(default_factory=list)  # ["R1", "R2", "R3"]
    quantity: int = 1
    value: Optional[str] = None  # "10k", "100nF", etc.
    footprint: Optional[str] = None  # "0402", "SOT-23", etc.
    mpn: Optional[str] = None  # Manufacturer part number
    manufacturer: Optional[str] = None
    description: Optional[str] = None
    raw_data: Dict[str, str] = field(default_factory=dict)  # All original columns


@dataclass
class ComponentMatch:
    """A potential match from inventory."""
    part_id: int
    mpn: str
    manufacturer: Optional[str]
    description: Optional[str]
    category: Optional[str]
    attributes: Dict[str, Any]
    qty_available: int
    bins: List[Dict[str, Any]]  # [{"position": "A1", "qty": 5}, ...]
    match_type: str  # "exact", "close", "suggested", "alternate"
    confidence: float  # 0.0 to 1.0
    match_reason: str  # Human-readable explanation


def parse_bom_csv(content: str) -> Tuple[List[BOMLine], List[str]]:
    """
    Parse a CSV BOM from various EDA tools.
    Returns (list of BOMLine objects, list of warnings/notes).
    
    Supports:
    - KiCad (Comment/Value, Footprint, Designator/Reference columns)
    - Eagle (Qty, Value, Device, Parts columns)
    - Altium (Designator, Qty, Description, Footprint, MPN columns)
    - Generic CSV (auto-detect likely columns)
    """
    warnings = []
    lines = []
    
    # Try to parse as CSV
    try:
        # Detect dialect
        sniffer = csv.Sniffer()
        sample = '\n'.join(content.splitlines()[:10])
        try:
            dialect = sniffer.sniff(sample, delimiters=',;\t')
        except Exception:
            dialect = csv.excel
        
        # Read all rows
        reader = csv.DictReader(StringIO(content), dialect=dialect)
        headers = reader.fieldnames or []
        
        if not headers:
            warnings.append("No headers detected in CSV")
            return [], warnings
        
        # Normalize headers (lowercase, strip)
        norm_headers = {h: h.lower().strip() for h in headers}
        
        # Detect column mappings with heuristics
        col_map = _detect_columns(headers, norm_headers)
        
        if not col_map.get('designator') and not col_map.get('value'):
            warnings.append("Could not detect designator or value columns. Using first few columns as fallback.")
        
        row_num = 1
        for row in reader:
            row_num += 1
            try:
                bom_line = _extract_bom_line(row, col_map, row_num)
                if bom_line:
                    lines.append(bom_line)
            except Exception as e:
                warnings.append(f"Row {row_num}: {str(e)}")
        
        if not lines:
            warnings.append("No valid BOM lines extracted")
        
    except Exception as e:
        warnings.append(f"Parse error: {str(e)}")
    
    return lines, warnings


def _detect_columns(headers: List[str], norm_headers: Dict[str, str]) -> Dict[str, str]:
    """
    Detect which columns correspond to standard BOM fields.
    Returns mapping: field_name -> actual_column_name
    """
    mapping = {}
    
    # Designator/Reference column
    designator_keys = ['designator', 'reference', 'ref', 'part', 'parts', 'refs', 'item']
    for h in headers:
        nh = norm_headers[h]
        if any(k in nh for k in designator_keys):
            mapping['designator'] = h
            break
    
    # Quantity column
    qty_keys = ['qty', 'quantity', 'qnty', 'count', 'number']
    for h in headers:
        nh = norm_headers[h]
        if any(k in nh for k in qty_keys):
            mapping['quantity'] = h
            break
    
    # Value column
    value_keys = ['value', 'comment', 'val', 'description']
    for h in headers:
        nh = norm_headers[h]
        if any(k in nh for k in value_keys):
            mapping['value'] = h
            break
    
    # Footprint column
    footprint_keys = ['footprint', 'package', 'foot', 'pkg', 'device']
    for h in headers:
        nh = norm_headers[h]
        if any(k in nh for k in footprint_keys):
            mapping['footprint'] = h
            break
    
    # MPN column
    mpn_keys = ['mpn', 'mfr part', 'part number', 'partnumber', 'mfr_part', 'manufacturer part']
    for h in headers:
        nh = norm_headers[h]
        if any(k in nh for k in mpn_keys):
            mapping['mpn'] = h
            break
    
    # Manufacturer column
    mfr_keys = ['manufacturer', 'mfr', 'brand', 'vendor']
    for h in headers:
        nh = norm_headers[h]
        if any(k == nh or nh.startswith(k) for k in mfr_keys):
            mapping['manufacturer'] = h
            break
    
    # Description column
    desc_keys = ['description', 'desc', 'notes', 'comment']
    for h in headers:
        nh = norm_headers[h]
        if any(k in nh for k in desc_keys) and h != mapping.get('value'):
            mapping['description'] = h
            break
    
    return mapping


def _extract_bom_line(row: Dict[str, str], col_map: Dict[str, str], row_num: int) -> Optional[BOMLine]:
    """Extract a BOMLine from a CSV row using detected column mappings."""
    
    # Get designators
    designators = []
    if col_map.get('designator'):
        raw_des = (row.get(col_map['designator']) or '').strip()
        # Split on common delimiters: comma, space, semicolon
        designators = [d.strip() for d in re.split(r'[,;\s]+', raw_des) if d.strip()]
    
    # Get quantity
    quantity = 1
    if col_map.get('quantity'):
        qty_str = (row.get(col_map['quantity']) or '').strip()
        try:
            quantity = int(qty_str) if qty_str else len(designators) or 1
        except ValueError:
            quantity = len(designators) or 1
    elif designators:
        quantity = len(designators)
    
    # Get value
    value = None
    if col_map.get('value'):
        value = (row.get(col_map['value']) or '').strip() or None
    
    # Get footprint
    footprint = None
    if col_map.get('footprint'):
        footprint = (row.get(col_map['footprint']) or '').strip() or None
    
    # Get MPN
    mpn = None
    if col_map.get('mpn'):
        mpn = (row.get(col_map['mpn']) or '').strip() or None
    
    # Get manufacturer
    manufacturer = None
    if col_map.get('manufacturer'):
        manufacturer = (row.get(col_map['manufacturer']) or '').strip() or None
    
    # Get description
    description = None
    if col_map.get('description'):
        description = (row.get(col_map['description']) or '').strip() or None
    
    # Skip rows with no meaningful data
    if not any([designators, value, mpn, footprint, description]):
        return None
    
    return BOMLine(
        row_num=row_num,
        designators=designators,
        quantity=quantity,
        value=value,
        footprint=footprint,
        mpn=mpn,
        manufacturer=manufacturer,
        description=description,
        raw_data=dict(row)
    )


def normalize_value(value: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Parse a component value string into (numeric_value, unit).
    Examples:
      "10k" -> (10000.0, "ohm") for resistors
      "100nF" -> (100e-9, "F") for capacitors
      "1uH" -> (1e-6, "H") for inductors
    Returns (None, None) if unparseable.
    """
    if not value:
        return None, None
    
    value = value.strip().upper()
    
    # Multiplier mapping
    multipliers = {
        'P': 1e-12, 'N': 1e-9, 'U': 1e-6, 'M': 1e-3,
        'K': 1e3, 'MEG': 1e6, 'G': 1e9,
        # Greek mu (μ) sometimes used
        'Μ': 1e-6, 'µ': 1e-6,
    }
    
    # Try to extract numeric part and unit/multiplier
    # Pattern: optional number, optional multiplier, optional unit
    match = re.match(r'^([0-9.]+)\s*([PNUMKG]|MEG|µ|Μ)?\s*([A-Z]+)?', value)
    if not match:
        return None, None
    
    num_str, mult_str, unit_str = match.groups()
    
    try:
        num = float(num_str)
    except ValueError:
        return None, None
    
    # Apply multiplier
    if mult_str:
        mult_str = mult_str.upper()
        if mult_str == 'µ' or mult_str == 'Μ':
            mult_str = 'U'
        num *= multipliers.get(mult_str, 1.0)
    
    # Infer unit if not present
    unit = unit_str if unit_str else None
    
    # Common heuristics
    if not unit:
        # If value has 'R' or ends in ohm range, assume resistor
        if 'R' in value or 1 <= num <= 1e9:
            unit = 'OHM'
        # If value has 'F', assume capacitor
        elif 'F' in value:
            unit = 'F'
        # If value has 'H', assume inductor
        elif 'H' in value:
            unit = 'H'
    
    return num, unit


def normalize_footprint(fp: str) -> Optional[str]:
    """
    Normalize footprint strings to a canonical form.
    Examples: "0402" -> "0402", "SOT-23" -> "SOT23", "R_0805_2012Metric" -> "0805"
    """
    if not fp:
        return None
    
    fp = fp.strip().upper()
    
    # Extract common package codes
    # Imperial sizes: 0201, 0402, 0603, 0805, 1206, 1210, etc.
    imperial = re.search(r'\b(0201|0402|0603|0805|1206|1210|1812|2010|2512)\b', fp)
    if imperial:
        return imperial.group(1)
    
    # SOT, SOD, etc.
    package = re.search(r'\b(SOT[-]?\d+[A-Z]?|SOD[-]?\d+|QFN[-]?\d+|QFP[-]?\d+|DFN[-]?\d+)\b', fp)
    if package:
        return package.group(1).replace('-', '')
    
    # SOIC, TSSOP, etc.
    ic_package = re.search(r'\b(SOIC|TSSOP|SSOP|MSOP|DIP|PLCC|BGA)[-]?\d*\b', fp)
    if ic_package:
        return ic_package.group(0).replace('-', '')
    
    return fp


def find_matches(bom_line: BOMLine, inventory: List[Dict[str, Any]]) -> List[ComponentMatch]:
    """
    Find all potential matches for a BOM line from inventory.
    Returns list sorted by confidence (highest first).
    """
    matches = []
    
    for part in inventory:
        match = _score_match(bom_line, part)
        if match and match.confidence > 0:
            matches.append(match)
    
    # Sort by confidence descending
    matches.sort(key=lambda m: m.confidence, reverse=True)
    
    return matches


def _score_match(bom_line: BOMLine, part: Dict[str, Any]) -> Optional[ComponentMatch]:
    """
    Score a single part against a BOM line.
    Returns ComponentMatch with confidence score or None if no match.
    """
    confidence = 0.0
    reasons = []
    match_type = "suggested"
    
    part_mpn = (part.get('mpn') or '').strip().upper()
    part_mfr = (part.get('manufacturer') or '').strip().upper()
    part_attrs = part.get('attributes') or {}
    part_desc = (part.get('description') or '').strip().upper()
    
    # 1. MPN exact match (highest priority)
    if bom_line.mpn and part_mpn:
        if bom_line.mpn.strip().upper() == part_mpn:
            confidence = 1.0
            match_type = "exact"
            reasons.append("Exact MPN match")
            return _build_match(part, match_type, confidence, '; '.join(reasons))
        elif bom_line.mpn.strip().upper() in part_mpn or part_mpn in bom_line.mpn.strip().upper():
            confidence = 0.85
            match_type = "close"
            reasons.append("Partial MPN match")
    
    # 2. Manufacturer match (if available)
    mfr_bonus = 0.0
    if bom_line.manufacturer and part_mfr:
        if bom_line.manufacturer.strip().upper() == part_mfr:
            mfr_bonus = 0.1
            reasons.append("Manufacturer match")
        elif bom_line.manufacturer.strip().upper() in part_mfr or part_mfr in bom_line.manufacturer.strip().upper():
            mfr_bonus = 0.05
    
    # 3. Value matching (for passive components)
    if bom_line.value:
        bom_val, bom_unit = normalize_value(bom_line.value)
        
        # Try to find value in part attributes
        part_val = None
        part_unit = None
        
        # Resistor
        if 'resistance' in part_attrs:
            part_val, part_unit = normalize_value(str(part_attrs['resistance']))
        # Capacitor
        elif 'capacitance' in part_attrs:
            part_val, part_unit = normalize_value(str(part_attrs['capacitance']))
        # Inductor
        elif 'inductance' in part_attrs:
            part_val, part_unit = normalize_value(str(part_attrs['inductance']))
        
        if bom_val and part_val and bom_unit == part_unit:
            # Exact value match
            if abs(bom_val - part_val) / bom_val < 0.001:  # within 0.1%
                confidence = max(confidence, 0.9)
                match_type = "exact" if match_type == "suggested" else match_type
                reasons.append(f"Exact value match ({bom_line.value})")
            # Close value match (within 10%)
            elif abs(bom_val - part_val) / bom_val < 0.1:
                confidence = max(confidence, 0.75)
                match_type = "close" if match_type == "suggested" else match_type
                reasons.append(f"Close value match ({bom_line.value} ≈ {_format_value(part_val, part_unit)})")
            # Suggested alternate (within 20%)
            elif abs(bom_val - part_val) / bom_val < 0.2:
                confidence = max(confidence, 0.5)
                match_type = "alternate" if confidence < 0.7 else match_type
                reasons.append(f"Alternate value ({_format_value(part_val, part_unit)} vs {bom_line.value})")
    
    # 4. Footprint matching
    if bom_line.footprint:
        bom_fp = normalize_footprint(bom_line.footprint)
        
        # Check part attributes for package info
        part_fp = None
        if 'package' in part_attrs:
            part_fp = normalize_footprint(str(part_attrs['package']))
        elif 'footprint' in part_attrs:
            part_fp = normalize_footprint(str(part_attrs['footprint']))
        
        # Also check description for footprint hints
        if not part_fp and bom_fp:
            if bom_fp in part_desc:
                part_fp = bom_fp
        
        if bom_fp and part_fp:
            if bom_fp == part_fp:
                confidence += 0.2
                reasons.append(f"Footprint match ({bom_fp})")
            else:
                confidence -= 0.1  # penalize footprint mismatch
                reasons.append(f"Footprint mismatch ({bom_fp} vs {part_fp})")
    
    # Apply manufacturer bonus
    confidence = min(1.0, confidence + mfr_bonus)
    
    # Threshold: require at least some confidence
    if confidence < 0.3:
        return None
    
    return _build_match(part, match_type, confidence, '; '.join(reasons) if reasons else "Potential match")


def _build_match(part: Dict[str, Any], match_type: str, confidence: float, reason: str) -> ComponentMatch:
    """Build a ComponentMatch from a part dict."""
    return ComponentMatch(
        part_id=part.get('part_id', 0),
        mpn=part.get('mpn', ''),
        manufacturer=part.get('manufacturer'),
        description=part.get('description'),
        category=part.get('category_source_name'),
        attributes=part.get('attributes', {}),
        qty_available=part.get('qty', 0),
        bins=part.get('bins', []),
        match_type=match_type,
        confidence=confidence,
        match_reason=reason
    )


def _format_value(val: float, unit: Optional[str]) -> str:
    """Format a numeric value back to human-readable form."""
    if val >= 1e6:
        return f"{val/1e6:.3g}M{unit or ''}"
    elif val >= 1e3:
        return f"{val/1e3:.3g}k{unit or ''}"
    elif val >= 1:
        return f"{val:.3g}{unit or ''}"
    elif val >= 1e-3:
        return f"{val*1e3:.3g}m{unit or ''}"
    elif val >= 1e-6:
        return f"{val*1e6:.3g}u{unit or ''}"
    elif val >= 1e-9:
        return f"{val*1e9:.3g}n{unit or ''}"
    else:
        return f"{val*1e12:.3g}p{unit or ''}"

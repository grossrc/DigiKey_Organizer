#!/usr/bin/env python3
"""
Overview
This script encompasses the end-to-end decoding of the DataMatrix (resembles a QR
code) present on every DigiKey low volume part packaging. The script was developed
independently to provide an easier standalone framework for workshopping the decoding
of this DataMatrix. The standard output shows the parameters embedded into the
DataMatrix which are layed out and encoded specifically according to:
https://forum.digikey.com/t/digikey-product-labels-decoding-digikey-barcodes/41097

Either MFR-Part-#/DigiKey-Part-# decoded here are used in a different script to pull
the remainder information through DigiKey's API. Most information is the same, but
using the decoded DataMatrix to then query the API gives us basically everything we
need to know about a part to catalog it.

Special Features
- **Much higher UI frame rate** on laptops and Raspberry Pi 4 by decoupling the
  preview loop from the decode loop (background worker thread with a small queue).
- **Progressive effort strategy**: start with a very fast decode (ROI only,
  no rotations, no heavy pre-processing). If we miss for a while, we escalate to
  stronger image transforms (CLAHE, sharpen, invert, up/down-scale, rotations).
- **Time budgets** for each decode attempt and a cap on candidate images so we
  never tank the UI framerate.
- **Pi-friendly** knobs: ROI-only default, decode interval, FPS cap, camera flags
  (autofocus/exposure/WB), and snapshot hooks for debugging.

Usage examples
  python digikey_datamatrix_scanner.py --timeout 20 --width 1280 --height 720
  python digikey_datamatrix_scanner.py --timeout 20 --roi-only \
      --decode-interval 0.08 --decode-budget-ms 30 --mode fast
  python digikey_datamatrix_scanner.py --auto-focus off --auto-exposure off --exposure -6 \
      --roi-only --mode balanced --timeout 25

Notes
- On Windows we use DirectShow; on Linux (incl. Pi OS) we use V4L2 by default.
- ZXing-C++ (zxing-cpp) remains the preferred backend; fallbacks attempt some
  parameter sweeps that often help marginal labels.
- This file replaces the previous revision; functionality is superset.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import cv2
import numpy as np

cv2.setUseOptimized(True)
# You may tune this if your CPU has many cores; OpenCV ops here are light.
try:
    cv2.setNumThreads(2)
except Exception:
    pass

# ------------------------------ Decoder Backend Selection ------------------------------
BACKEND: Optional[str] = None  # one of: "zxingcpp", "pylibdmtx", "pyzbar"
ZXING: Optional[object] = None
DMTX_DECODE = None
ZBAR_DECODE = None

# Preferred: zxing-cpp
try:
    import zxingcpp as _zxingcpp  # type: ignore

    ZXING = _zxingcpp
    BACKEND = "zxingcpp"
except Exception:  # pragma: no cover
    try:
        from pylibdmtx.pylibdmtx import decode as _dmtx_decode  # type: ignore

        DMTX_DECODE = _dmtx_decode
        BACKEND = "pylibdmtx"
    except Exception:  # pragma: no cover
        try:
            from pyzbar.pyzbar import decode as _zbar_decode  # type: ignore

            ZBAR_DECODE = _zbar_decode
            BACKEND = "pyzbar"
        except Exception:
            BACKEND = None

IMPORT_BACKEND_ERROR: Optional[RuntimeError] = None
if BACKEND is None:
    IMPORT_BACKEND_ERROR = RuntimeError(
        "No barcode backend available. Install one of:"
        "  pip install zxing-cpp   (recommended)"
        "  pip install pylibdmtx   (needs libdmtx)"
        "  pip install pyzbar pillow   (needs zbar)"
    )

# ------------------------------ Parsing Utilities ------------------------------
GS = 0x1D  # Group Separator
RS = 0x1E  # Record Separator
EOT = 0x04  # End of Transmission

# Envelope regex for ISO/IEC 15434 / MH10.8.2 Format 06
ENVELOPE_RE = re.compile(
    b"^" + re.escape(b"[)>") + bytes([RS]) + b"06" + bytes([GS]) + b"(?P<body>.*)" + bytes([RS, EOT]) + b"$",
    re.DOTALL,
)

# Known Digi-Key DIs for pick labels (ordered longest-first so the longest wins)
DI_ORDER: List[str] = [
    "30P", "12Z", "11Z", "11K", "10K", "10D",
    "1K", "1P", "1T", "9D", "4L", "Q", "K", "P", "E", "13Z", "20Z",
]

# Friendly names for output fields
DI_TO_FIELD: Dict[str, str] = {
    "P": "customer_reference",
    "1P": "mfr_part_number",
    "30P": "digikey_part_number",
    "K": "purchase_order",
    "1K": "sales_order",
    "10K": "invoice_number",
    "9D": "date_code",
    "10D": "date_code",  # alternate DI used by some suppliers
    "1T": "lot_code",
    "11K": "packing_list_number",
    "4L": "country_of_origin",
    "Q": "quantity",
    "11Z": "label_type",
    "12Z": "internal_part_id",
    "13Z": "internal_unused",
    "20Z": "padding",
    "E": "compliance",
}

# Light backwards-compatibility map (if you used old keys elsewhere)
LEGACY_ALIASES: Dict[str, str] = {
    "manufacturer_part_number": "mfr_part_number",
}

# Minimal acceptance policy: require DK part number and quantity
REQUIRED_DIs: List[str] = ["30P", "Q"]

FIELD_CODE_RE = re.compile(rb"^([0-9]*[A-Z]+)(.*)$", re.DOTALL)


def _normalize_controls(b: bytes) -> bytes:
    """Map placeholder strings like '<GS>' back to control bytes and strip stray whitespace.
    Avoid regex here to sidestep escape pitfalls and speed things up.
    Also decode *textual* hex escapes like \x1D into their control bytes.
    """
    # Common textual placeholders sometimes emitted by scanners or test fixtures
    b = b.replace(b"<GS>", bytes([GS]))
    b = b.replace(b"<RS>", bytes([RS]))
    b = b.replace(b"<EOT>", bytes([EOT]))

    # Convert textual sequences like b"\x1D" -> control byte 0x1D
    def _unescape_textual_hex(buf: bytes) -> bytes:
        out = bytearray()
        i = 0
        L = len(buf)
        while i < L:
            if i + 3 < L and buf[i] == 0x5C and (buf[i+1] in (0x78, 0x58)):
                h1, h2 = buf[i+2], buf[i+3]
                if ((48 <= h1 <= 57) or (65 <= h1 <= 70) or (97 <= h1 <= 102)) and \
                   ((48 <= h2 <= 57) or (65 <= h2 <= 70) or (97 <= h2 <= 102)):
                    out.append(int(bytes([h1, h2]).decode('ascii'), 16))
                    i += 4
                    continue
            out.append(buf[i])
            i += 1
        return bytes(out)

    b = _unescape_textual_hex(b)

    return b.strip()


REQUIRED_ACCEPT_DIs = ("30P", "Q", "1P")  # tweak: if you prefer just ("30P","Q"), change here

def _is_complete_payload(parsed: Dict[str, Any], required_dis=REQUIRED_ACCEPT_DIs) -> bool:
    by_di = parsed.get("by_di", {})
    # explicit reject: digits-only internal id fallback
    inf = parsed.get("inference") or {}
    if (not by_di) and ("fields" in parsed) and ("internal_part_id" in parsed["fields"]):
        return False
    if isinstance(inf, dict) and (inf.get("inference") == "digits_only_internal_id"):
        return False
    # require all specified DIs
    return all(di in by_di for di in required_dis)

def _bytes_clean(b: bytes) -> bytes:
    """Remove ISO/IEC 15434 header/terminators if present and return payload bytes."""
    b = _normalize_controls(b)
    # Trim trailing RS/EOT that sometimes follow the envelope
    b = b.rstrip(bytes([RS, EOT]))
    header = b"[)>" + bytes([RS]) + b"06" + bytes([GS])
    if b.startswith(header):
        b = b[len(header) :]
    return b


def _visible(b: bytes) -> str:
    return (
        b.decode("latin-1", errors="replace")
        .replace(chr(GS), "<GS>")
        .replace(chr(RS), "<RS>")
        .replace(chr(EOT), "<EOT>")
    )


def parse_digikey_payload(raw_bytes: bytes) -> Dict[str, Any]:
    """Parse a Digi-Key DataMatrix payload per MH10.8.2 Format 06.

    - Validates the 15434 envelope when present
    - Splits on GS and peels off the *longest* known DI
    - Normalizes/validates select fields (Q, 4L)
    - Keeps unknown groups and duplicate DI diagnostics
    - Applies a minimal acceptance policy (30P + Q)
    """
    raw_norm = _normalize_controls(raw_bytes)

    m = ENVELOPE_RE.match(raw_norm)
    if m:
        envelope_valid = True
        body = m.group("body")
    else:
        envelope_valid = False
        body = _bytes_clean(raw_norm)

    tokens = [t for t in body.split(bytes([GS])) if t]

    by_di: Dict[str, str] = {}
    unknown_tokens: List[str] = []
    duplicates: List[Dict[str, str]] = []

    for tok in tokens:
        # Identify DI by longest known prefix
        di: Optional[str] = None
        for cand in DI_ORDER:
            if tok.startswith(cand.encode("ascii")):
                di = cand
                break
        if not di:
            # If not in our table, keep the printable fragment for diagnostics
            unknown_tokens.append(tok.decode("latin-1", errors="replace"))
            continue

        value = tok[len(di) :].decode("latin-1", errors="replace").strip()
        if di in by_di:
            duplicates.append({"di": di, "previous": by_di[di], "kept": value})
        by_di[di] = value  # last-wins policy

    # Map DIs to friendly fields
    fields: Dict[str, Any] = {}

    def _put(di: str, fn: str):
        if di in by_di:
            fields[fn] = by_di[di]

    # Straight mappings
    for di, fn in DI_TO_FIELD.items():
        if di in ("9D", "10D"):
            continue  # handled below
        _put(di, fn)

    # Date code preference: 9D then 10D, but keep both if distinct
    if "9D" in by_di:
        fields[DI_TO_FIELD["9D"]] = by_di["9D"].strip()
    elif "10D" in by_di:
        fields[DI_TO_FIELD["10D"]] = by_di["10D"].strip()
    if "9D" in by_di and "10D" in by_di and by_di["10D"] != by_di["9D"]:
        fields.setdefault("date_code_sources", {})["10D"] = by_di["10D"]

    # Normalizations / validations
    if "quantity" in fields:
        try:
            fields["quantity"] = int(str(fields["quantity"]).strip())
        except Exception:
            pass  # leave as-is if non-integer

    if "country_of_origin" in fields:
        coo = str(fields["country_of_origin"]).strip().upper()
        fields["country_of_origin"] = coo
        if not re.fullmatch(r"[A-Z]{2}", coo):
            fields.setdefault("_warnings", []).append("country_of_origin not 2-letter code")

    # Add legacy aliases if present
    for legacy, canonical in LEGACY_ALIASES.items():
        if canonical in fields and legacy not in fields:
            fields[legacy] = fields[canonical]
    # Fallbacks when no MH10 DIs were found ---------------------------------
    # 1) Digits-only payloads sometimes represent Digi-Key's internal product id
    #    (12Z-like) printed without the MH10.8.2 envelope on certain packages.
    body_str = body.decode("latin-1", errors="ignore").strip()
    if not by_di and body_str.isdigit() and 6 <= len(body_str) <= 14:
        fields["internal_part_id"] = body_str
        result_hint = {"inference": "digits_only_internal_id"}
    else:
        result_hint = None

    # Acceptance policy -------------------------------------------------------
    missing_required: List[str] = [di for di in REQUIRED_DIs if di not in by_di]

    result: Dict[str, Any] = {
        "standard": "ISO/IEC 15434 (MH10.8.2) Format 06",
        "envelope": {"valid": envelope_valid},
        "fields": fields,
        "by_di": by_di,
    }

    if result_hint:
        result["inference"] = result_hint

    diag: Dict[str, Any] = {}
    if unknown_tokens:
        diag["unknown_groups"] = unknown_tokens
    if duplicates:
        diag["duplicates"] = duplicates
    if missing_required:
        diag["missing_required"] = missing_required
    if diag:
        result["diagnostics"] = diag

    # Raw views ---------------------------------------------------------------
    result["raw"] = {
        "bytes": list(raw_bytes),
        "string_visible_separators": _visible(raw_bytes),
        "payload_visible_separators": _visible(body),
    }

    return result

# --- Web helpers (safe for Flask import) -------------------------
def parse_from_web_text(s: str) -> Dict[str, Any]:
    # Parse a DataMatrix payload that came from the browser (string).
    # Handles <GS>/<RS>/<EOT> and \x1D style escapes using your existing parser.
    b = s.encode("latin-1", errors="replace")
    return parse_digikey_payload(b)

def fields_from_parsed(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the friendly 'fields' mapping out of your parsed object."""
    return dict(parsed.get("fields") or {})


# ------------------------------ Video & Decode ------------------------------
@dataclass
class ScanResult:
    success: bool
    data: Optional[Dict[str, Any]] = None
    message: str = ""
    raw: Optional[bytes] = None
    backend: Optional[str] = None


# ---- Image utilities (lightweight first) ----

def fast_contrast(gray: np.ndarray, alpha: float = 1.6, beta: float = 5.0) -> np.ndarray:
    # y = alpha*x + beta, clipped to [0, 255]
    return cv2.convertScaleAbs(gray, alpha=alpha, beta=beta)


def unsharp_mask(gray: np.ndarray, sigma: float = 1.2, amount: float = 1.0) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (0, 0), sigma)
    sharp = cv2.addWeighted(gray, 1 + amount, blur, -amount, 0)
    return sharp


def clahe(gray: np.ndarray, clip: float = 2.0, grid: Tuple[int, int] = (8, 8)) -> np.ndarray:
    c = cv2.createCLAHE(clipLimit=clip, tileGridSize=grid)
    return c.apply(gray)


def adaptive_bw(gray: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 7
    )


def rotations(img: np.ndarray) -> Iterable[Tuple[np.ndarray, str]]:
    yield img, "r0"
    yield cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE), "r90"
    yield cv2.rotate(img, cv2.ROTATE_180), "r180"
    yield cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE), "r270"


# Progressive candidate generation by effort level (keep cheap early)
# level 0: gray
# level 1: + fast_contrast
# level 2: + clahe OR unsharp
# level 3: + invert variants
# level 4: + simple upscales (1.5x)
# level 5: + rotations

def generate_candidates(gray: np.ndarray, level: int, max_candidates: int) -> Iterable[Tuple[np.ndarray, str]]:
    count = 0
    def _yield(img, tag):
        nonlocal count
        if count < max_candidates:
            count += 1
            yield img, tag

    # Level 0
    for out in _yield(gray, "gray"):
        yield out

    if level >= 1:
        fc = fast_contrast(gray)
        for out in _yield(fc, "fastc"):
            yield out

    if level >= 2:
        for out in _yield(clahe(gray), "clahe"):
            yield out
        for out in _yield(unsharp_mask(gray), "sharp"):
            yield out

    if level >= 3:
        for base, tag in [(gray, "gray"), (fc if 'fc' in locals() else fast_contrast(gray), "fastc")]:
            inv = 255 - base
            for out in _yield(inv, tag+"+inv"):
                yield out

    if level >= 4:
        # a single upscale is usually enough; upscale helps tiny modules
        h, w = gray.shape[:2]
        up = cv2.resize(gray, (int(w * 1.5), int(h * 1.5)), interpolation=cv2.INTER_CUBIC)
        for out in _yield(up, "up1.5"):
            yield out

    if level >= 5:
        for img, tag in list(generate_candidates(gray, 0, max_candidates)):
            for rot, rtag in rotations(img):
                for out in _yield(rot, f"{tag}:{rtag}"):
                    yield out


# Backend-specific decode wrappers (ZXing preferred)

def _zxing_decode(gray: np.ndarray, max_count: int = 1):
    assert ZXING is not None
    kwargs: Dict[str, Any] = {}
    try:
        kwargs["formats"] = ZXING.BarcodeFormat.DataMatrix
    except Exception:
        pass
    for key, val in ("try_harder", True), ("try_rotate", False), ("max_number_of_symbols", max_count):
        try:
            kwargs[key] = val
        except Exception:
            pass
    try:
        return ZXING.read_barcodes(gray, **kwargs)
    except TypeError:
        return ZXING.read_barcodes(gray)


def _pylibdmtx_decode(gray: np.ndarray, max_count: int = 1):
    assert DMTX_DECODE is not None
    for shrink in (1, 2):
        for threshold in (None, 60):
            kw = {"max_count": max_count, "shrink": shrink}
            if threshold is not None:
                kw["threshold"] = threshold
            res = DMTX_DECODE(gray, **kw)
            if res:
                return res
    return []


def _pyzbar_decode(gray: np.ndarray, max_count: int = 1):
    assert ZBAR_DECODE is not None
    out = []
    res = ZBAR_DECODE(gray)
    for r in res:
        if getattr(r, "type", "").upper() != "DATAMATRIX":
            continue
        rect = type(
            "Rect",
            (),
            {
                "left": int(r.rect.left),
                "top": int(r.rect.top),
                "width": int(r.rect.width),
                "height": int(r.rect.height),
            },
        )
        out.append(type("Decoded", (), {"data": r.data, "rect": rect})())
        if len(out) >= max_count:
            break
    return out


def try_decode_image(gray: np.ndarray, backend: str, max_count: int = 1):
    if backend == "zxingcpp":
        return _zxing_decode(gray, max_count)
    if backend == "pylibdmtx":
        return _pylibdmtx_decode(gray, max_count)
    if backend == "pyzbar":
        return _pyzbar_decode(gray, max_count)
    return []


# ------------------------------ Decode worker (thread) ------------------------------
@dataclass
class WorkItem:
    img_gray: np.ndarray
    roi_offset: Tuple[int, int]
    level: int


def _decode_worker(
    backend: str,
    in_q: "queue.Queue[WorkItem]",
    out_q: "queue.Queue[Optional[Tuple[bytes, Tuple[int,int,int,int]]]]",
    decode_budget_ms: int,
    max_candidates: int,
    stop_event: threading.Event,
):
    """Background worker that decodes at a controlled cadence and returns first hit."""
    while not stop_event.is_set():
        try:
            item = in_q.get(timeout=0.05)
        except queue.Empty:
            continue
        gray = item.img_gray
        ox, oy = item.roi_offset

        start = time.time()
        got = None
        for img, tag in generate_candidates(gray, level=item.level, max_candidates=max_candidates):
            # ask for multiple symbols instead of just one
            res = try_decode_image(img, backend, max_count=6)
            if res:
                # Evaluate all symbols; accept only "complete" ones
                for d in res:
                    raw = getattr(d, "bytes", None) or getattr(d, "data", None)
                    if raw is None and hasattr(d, "text"):
                        raw = str(getattr(d, "text")).encode("latin-1", errors="replace")
                    if not raw:
                        continue

                    # Parse & gate on completeness
                    parsed = parse_digikey_payload(bytes(raw))
                    if not _is_complete_payload(parsed):
                        continue  # keep scanning; do not accept partial or digits-only

                    # If complete, compute rect and accept
                    left = int(getattr(getattr(d, "rect", None), "left", 0)) + ox
                    top = int(getattr(getattr(d, "rect", None), "top", 0)) + oy
                    width = int(getattr(getattr(d, "rect", None), "width", 0))
                    height = int(getattr(getattr(d, "rect", None), "height", 0))
                    got = (bytes(raw), (left, top, width, height))
                    break

            # Check time budget after each candidate
            if got is None and (time.time() - start) * 1000.0 > decode_budget_ms:
                break
        try:
            out_q.put_nowait(got)  # None means "no acceptable decode yet"
        except queue.Full:
            pass
        finally:
            in_q.task_done()


# ------------------------------ Capture / UI / Orchestration ------------------------------
@dataclass
class ScanConfig:
    camera_index: int
    timeout_s: int
    width: Optional[int]
    height: Optional[int]
    show_window: bool
    backend: str
    roi_only: bool
    decode_interval_s: float
    decode_budget_ms: int
    max_candidates: int
    mode: str  # fast | balanced | aggressive
    snapshot_success: Optional[str]
    snapshot_fail: Optional[str]
    auto_focus: Optional[bool]
    focus: Optional[float]
    auto_exposure: Optional[bool]
    exposure: Optional[float]
    auto_wb: Optional[bool]
    wb_temperature: Optional[float]
    fullscreen: bool
    cancel_button: bool
    cancel_text: str
    debug: bool


def _configure_camera(
    cap: cv2.VideoCapture,
    *,
    auto_focus: Optional[bool] = None,
    focus: Optional[float] = None,
    auto_exposure: Optional[bool] = None,
    exposure: Optional[float] = None,
    auto_wb: Optional[bool] = None,
    wb_temperature: Optional[float] = None,
) -> None:
    def _set(prop, val):
        try:
            cap.set(prop, float(val))
        except Exception:
            pass

    if auto_focus is not None and hasattr(cv2, "CAP_PROP_AUTOFOCUS"):
        _set(cv2.CAP_PROP_AUTOFOCUS, 1 if auto_focus else 0)
    if focus is not None and hasattr(cv2, "CAP_PROP_FOCUS"):
        _set(cv2.CAP_PROP_FOCUS, focus)

    if auto_exposure is not None and hasattr(cv2, "CAP_PROP_AUTO_EXPOSURE"):
        _set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25 if not auto_exposure else 0.75)
    if exposure is not None and hasattr(cv2, "CAP_PROP_EXPOSURE"):
        _set(cv2.CAP_PROP_EXPOSURE, exposure)

    if auto_wb is not None and hasattr(cv2, "CAP_PROP_AUTO_WB"):
        _set(cv2.CAP_PROP_AUTO_WB, 1 if auto_wb else 0)
    if wb_temperature is not None and hasattr(cv2, "CAP_PROP_WB_TEMPERATURE"):
        _set(cv2.CAP_PROP_WB_TEMPERATURE, wb_temperature)

    # Reduce latency if supported
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass


def _resolve_backend(choice: str) -> str:
    if choice != "auto":
        return choice
    if ZXING is not None:
        return "zxingcpp"
    if DMTX_DECODE is not None:
        return "pylibdmtx"
    if ZBAR_DECODE is not None:
        return "pyzbar"
    # Defer the error to here:
    raise IMPORT_BACKEND_ERROR or RuntimeError("No decoder backend is available")


# Public alias to maintain compatibility
def resolve_backend(choice: str) -> str:
    return _resolve_backend(choice)


def _effort_schedule(mode: str) -> Tuple[int, float, int]:
    """Return (start_level, escalate_every_seconds, max_level)."""
    if mode == "fast":
        return 0, 2.5, 3
    if mode == "aggressive":
        return 1, 1.0, 5
    # balanced default
    return 0, 1.8, 5


def scan_loop(cfg: ScanConfig) -> ScanResult:
    # Backend API selection
    api = (
        cv2.CAP_DSHOW
        if platform.system() == "Windows"
        else cv2.CAP_V4L2
    )
    # Prepare window early to allow fullscreen and mouse callbacks
    win_name = "Digi-Key DataMatrix Scanner"
    if cfg.show_window:
        try:
            cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
            if cfg.fullscreen:
                cv2.setWindowProperty(win_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        except Exception:
            pass

    cap = cv2.VideoCapture(cfg.camera_index, api)

    if cfg.width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
    if cfg.height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)

    _configure_camera(
        cap,
        auto_focus=cfg.auto_focus,
        focus=cfg.focus,
        auto_exposure=cfg.auto_exposure,
        exposure=cfg.exposure,
        auto_wb=cfg.auto_wb,
        wb_temperature=cfg.wb_temperature,
    )

    if not cap.isOpened():
        return ScanResult(False, message=f"Unable to open camera index {cfg.camera_index}")

    # Worker thread setup
    in_q: "queue.Queue[WorkItem]" = queue.Queue(maxsize=1)   # drop frames if worker is busy
    out_q: "queue.Queue[Optional[Tuple[bytes, Tuple[int,int,int,int]]]]" = queue.Queue(maxsize=1)
    stop_event = threading.Event()

    worker = threading.Thread(
        target=_decode_worker,
        args=(cfg.backend, in_q, out_q, cfg.decode_budget_ms, cfg.max_candidates, stop_event),
        daemon=True,
    )
    worker.start()

    win_name = "Digi-Key DataMatrix Scanner"  # already created if show_window
    start_time = time.time()
    found_data: Optional[bytes] = None
    found_rect: Tuple[int, int, int, int] = (0, 0, 0, 0)
    user_cancel = False
    button_rect = [0, 0, 0, 0]  # x0,y0,x1,y1

    # Mouse callback for cancel
    if cfg.show_window and cfg.cancel_button:
        def _on_mouse(event, x, y, flags, param):
            nonlocal user_cancel
            if event == cv2.EVENT_LBUTTONDOWN:
                x0, y0, x1b, y1b = button_rect
                if x0 <= x <= x1b and y0 <= y <= y1b:
                    user_cancel = True
        try:
            cv2.setMouseCallback(win_name, _on_mouse)
        except Exception:
            pass

    # Effort escalation
    level, escalate_sec, max_level = _effort_schedule(cfg.mode)
    last_escalate_t = time.time()

    # Decode cadence clock
    last_submit_t = 0.0

    # FPS estimation for overlay
    last_frame_t = time.time()
    fps_smooth = 0.0

    try:
        while True:
            if cfg.timeout_s and (time.time() - start_time) > cfg.timeout_s:
                if cfg.snapshot_fail:
                    try:
                        ret, snap = cap.read()
                        if ret:
                            cv2.imwrite(cfg.snapshot_fail, snap)
                    except Exception:
                        pass
                return ScanResult(False, message="Timed out without detecting a DataMatrix code.")

            ret, frame = cap.read()
            if not ret:
                return ScanResult(False, message="Failed to read from camera.")

            h, w = frame.shape[:2]
            overlay = frame.copy()

            # ROI rectangle (50% of frame by default)
            gh, gw = int(h * 0.5), int(w * 0.5)
            y1, y2 = (h - gh) // 2, (h + gh) // 2
            x1, x2 = (w - gw) // 2, (w + gw) // 2
            if cfg.roi_only:
                cv2.rectangle(overlay, (x1, y1), (x2, y2), (255, 255, 255), 1)

            # Submit frame to worker at the requested cadence
            now = time.time()
            if (now - last_submit_t) >= cfg.decode_interval_s:
                last_submit_t = now
                if cfg.roi_only:
                    roi = frame[y1:y2, x1:x2]
                    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                    off = (x1, y1)
                else:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    off = (0, 0)
                # Non-blocking put: drop if busy
                try:
                    in_q.put_nowait(WorkItem(gray, off, level))
                except queue.Full:
                    pass

            # Poll results (non-blocking)
            try:
                got = out_q.get_nowait()
                out_q.task_done()
            except queue.Empty:
                got = None

            if got is not None:
                raw, rect = got
                if raw:
                    found_data = raw
                    found_rect = rect
                    # Optional snapshot for records
                    if cfg.snapshot_success:
                        try:
                            cv2.imwrite(cfg.snapshot_success, frame)
                        except Exception:
                            pass
                    # We intentionally break after drawing once more below

            # Escalate effort over time if no hit yet
            if found_data is None and (time.time() - last_escalate_t) > escalate_sec:
                if level < max_level:
                    level += 1
                last_escalate_t = time.time()

            # Overlay info (labels removed per request)
            dt = now - last_frame_t
            if dt > 0:
                fps = 1.0 / dt
                fps_smooth = 0.9 * fps_smooth + 0.1 * fps if fps_smooth else fps
            last_frame_t = now

            # Draw on-screen Cancel button (top-right)
            if cfg.show_window and cfg.cancel_button:
                bw, bh = 180, 44
                pad = 16
                x0 = w - bw - pad
                y0 = pad
                x1b = x0 + bw
                y1b = y0 + bh
                button_rect[:] = [x0, y0, x1b, y1b]
                cv2.rectangle(overlay, (x0, y0), (x1b, y1b), (0, 0, 255), -1)
                cv2.rectangle(overlay, (x0, y0), (x1b, y1b), (220, 220, 220), 1)
                cv2.putText(overlay, cfg.cancel_text or "Cancel (Esc)", (x0 + 12, y0 + 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)

            if found_data is not None:
                left, top, width_r, height_r = found_rect
                if width_r and height_r:
                    cv2.rectangle(overlay, (left, top), (left + width_r, top + height_r), (0, 255, 0), 2)
                cv2.putText(overlay, "Decoded! Parsing...", (10, h - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2, cv2.LINE_AA)

            if cfg.show_window:
                cv2.imshow(win_name, overlay)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord('q'), ord('c')) or user_cancel:
                    return ScanResult(False, message="Cancelled by user.")

            if found_data is not None:
                break

    finally:
        stop_event.set()
        try:
            in_q.put_nowait(WorkItem(np.zeros((1,1), np.uint8), (0,0), 0))
        except Exception:
            pass
        try:
            worker.join(timeout=0.5)
        except Exception:
            pass
        cap.release()
        if cfg.show_window:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    parsed = parse_digikey_payload(found_data or b"")
    return ScanResult(True, data=parsed, message="OK", raw=found_data, backend=cfg.backend)


# ------------------------------ CLI ------------------------------

# Integration helpers for your larger program -----------------------

def scan_part(
    camera: int = 0,
    *,
    timeout: int = 0,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fullscreen: bool = True,
    show_window: bool = True,
    roi_only: bool = False,
    backend: str = "auto",
    mode: str = "balanced",
    decode_interval: float = 0.08,
    decode_budget_ms: int = 30,
    max_candidates: int = 6,
    auto_focus: Optional[bool] = None,
    focus: Optional[float] = None,
    auto_exposure: Optional[bool] = None,
    exposure: Optional[float] = None,
    auto_wb: Optional[bool] = None,
    wb_temperature: Optional[float] = None,
    cancel_button: bool = True,
    cancel_text: str = "Cancel (Esc)",
) -> ScanResult:
    """Call this from your master program to scan once and get structured data.

    Returns a ScanResult: {success, data, message, raw, backend}
    On cancel or timeout, success=False and message explains why.
    """
    resolved = resolve_backend(backend)
    cfg = ScanConfig(
        camera_index=camera,
        timeout_s=timeout,
        width=width,
        height=height,
        show_window=show_window,
        backend=resolved,
        roi_only=roi_only,
        decode_interval_s=max(0.01, float(decode_interval)),
        decode_budget_ms=max(8, int(decode_budget_ms)),
        max_candidates=max(1, int(max_candidates)),
        mode=mode,
        snapshot_success=None,
        snapshot_fail=None,
        auto_focus=auto_focus,
        focus=focus,
        auto_exposure=auto_exposure,
        exposure=exposure,
        auto_wb=auto_wb,
        wb_temperature=wb_temperature,
        fullscreen=bool(fullscreen and show_window),
        cancel_button=bool(cancel_button and show_window),
        cancel_text=cancel_text,
        debug=False,
    )
    return scan_loop(cfg)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan Digi-Key DataMatrix and print JSON result.")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--no-gui", action="store_true")
    parser.add_argument("--roi-only", action="store_false")
    parser.add_argument("--backend", choices=["auto", "zxingcpp", "pylibdmtx", "pyzbar"], default="auto")
    parser.add_argument("--mode", choices=["fast", "balanced", "aggressive"], default="balanced")
    parser.add_argument("--decode-interval", type=float, default=0.08)
    parser.add_argument("--decode-budget-ms", type=int, default=30)
    parser.add_argument("--max-candidates", type=int, default=6)
    parser.add_argument("--auto-focus", choices=["on", "off"], default=None)
    parser.add_argument("--focus", type=float, default=None)
    parser.add_argument("--auto-exposure", choices=["on", "off"], default=None)
    parser.add_argument("--exposure", type=float, default=None)
    parser.add_argument("--auto-wb", choices=["on", "off"], default=None)
    parser.add_argument("--wb-temperature", type=float, default=None)
    parser.add_argument("--output", type=str, default=None)

    args = parser.parse_args()

    res = scan_part(
        camera=args.camera,
        timeout=args.timeout,
        width=args.width,
        height=args.height,
        fullscreen=True,
        show_window=not args.no_gui,
        roi_only=bool(args.roi_only),
        backend=args.backend,
        mode=args.mode,
        decode_interval=args.decode_interval,
        decode_budget_ms=args.decode_budget_ms,
        max_candidates=args.max_candidates,
        auto_focus=(None if args.auto_focus is None else args.auto_focus == "on"),
        focus=args.focus,
        auto_exposure=(None if args.auto_exposure is None else args.auto_exposure == "on"),
        exposure=args.exposure,
        auto_wb=(None if args.auto_wb is None else args.auto_wb == "on"),
        wb_temperature=args.wb_temperature,
        cancel_button=True,
        cancel_text="Cancel (Esc)",
    )

    if not res.success:
        print(json.dumps({"success": False, "error": res.message}, indent=2))
        sys.exit(1)

    payload = {
        "success": True,
        "source": "digikey_datamatrix_scanner",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backend": res.backend,
        "result": res.data,
    }
    txt = json.dumps(payload, indent=2, ensure_ascii=False)
    print(txt)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(txt)
        except Exception as e:
            print(json.dumps({"success": False, "error": f"Failed to write output: {e}"}), file=sys.stderr)


if __name__ == "__main__":
    main()

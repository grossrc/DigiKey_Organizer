# mcp_server/resources.py
"""MCP resources: everything a client needs to understand the schema."""
from __future__ import annotations

import json

from . import context

_RESOURCES = [
    {
        "uri": "schema://overview",
        "name": "Database orientation",
        "description": (
            "START HERE. Table grain, how to compute stock from the movements ledger, "
            "the meaning of OUT% bins, and how the JSONB specification columns work."
        ),
        "mimeType": "text/markdown",
        "read": context.overview,
    },
    {
        "uri": "schema://profiles",
        "name": "Canonical attribute keys and units",
        "description": (
            "Per-category definitions of every key in parts.attributes: unit implied by "
            "the key suffix, allowed enum values, validators, and display order."
        ),
        "mimeType": "application/json",
        "read": context.profiles,
    },
    {
        "uri": "schema://jsonb",
        "name": "Observed JSONB keys",
        "description": (
            "Live catalog of keys actually present in parts.attributes and "
            "parts.unknown_parameters, grouped by category, with sample values."
        ),
        "mimeType": "application/json",
        "read": context.jsonb,
    },
    {
        "uri": "schema://categories",
        "name": "Categories and part counts",
        "description": "Maps category_id to Digi-Key source names, part counts, and example paths.",
        "mimeType": "application/json",
        "read": context.categories,
    },
    {
        "uri": "schema://tables",
        "name": "Table, view and index definitions",
        "description": "Live introspection of the readable relations: columns, keys, checks, indexes.",
        "mimeType": "application/json",
        "read": context.tables,
    },
    {
        "uri": "schema://query_cookbook",
        "name": "Worked SQL examples",
        "description": "Twelve ready-made queries covering stock, location, spec filtering and history.",
        "mimeType": "text/markdown",
        "read": lambda: context.COOKBOOK,
    },
]

_BY_URI = {r["uri"]: r for r in _RESOURCES}


def list_resources() -> list[dict]:
    return [{k: v for k, v in r.items() if k != "read"} for r in _RESOURCES]


def read_resource(uri: str) -> dict:
    resource = _BY_URI.get(uri)
    if resource is None:
        raise KeyError(uri)
    payload = resource["read"]()
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"uri": uri, "mimeType": resource["mimeType"], "text": text}

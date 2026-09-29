"""Booking Receiving Reader — Gemini drafts a booking from an uploaded vendor
receiving/order document (PDF / photo / Excel). It produces a DRAFT the handler
reviews and confirms; it never creates the booking itself."""

from __future__ import annotations

from typing import Any

from aiagents.gemini import extract

_SYSTEM = (
    "You read a clothing/apparel vendor's 'receiving' or purchase-order document "
    "for an Indian multi-brand retailer (KDPS). Extract the ordered line items "
    "accurately. Quantities are integers (pieces). Reply with STRICT JSON only — "
    "no prose, no markdown fences."
)

_PROMPT = (
    "Extract the order from this document as JSON with exactly this shape:\n"
    "{\n"
    '  "brand": string|null,\n'
    '  "season": string|null,\n'
    '  "vendor_name": string|null,\n'
    '  "vendor_ref": string|null,\n'
    '  "lines": [ {"style_code": string, "size": string|null, '
    '"description": string|null, "quantity": integer, "cost": number|null, '
    '"mrp": number|null} ],\n'
    '  "missing": [string],   // required fields you could not read\n'
    '  "confidence": number   // 0..1 overall read confidence\n'
    "}\n"
    "Rules: one line per style+size. If quantity is unreadable use 0 and add a "
    "note to 'missing'. Cost is the vendor's price per piece (what KDPS pays), "
    "MRP is the printed selling price; both are rupees (number) or null. "
    "Do not invent data."
)


def read_booking_receipt(content: bytes, content_type: str) -> dict[str, Any]:
    data = extract(content, content_type, _SYSTEM, _PROMPT)
    data.setdefault("lines", [])
    data.setdefault("missing", [])
    return data

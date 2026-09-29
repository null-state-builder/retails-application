"""OpenAPI shapes for the finance APIViews that return assembled dictionaries."""

from __future__ import annotations

from typing import Any


def obj(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


INTEGER = {"type": "integer"}
TEXT = {"type": "string"}
RUPEES = {"oneOf": [{"type": "number"}, {"type": "string"}]}
DETAIL = obj({"detail": TEXT}, "detail")

VENDOR_BALANCE = obj(
    {
        "vendor_id": INTEGER,
        "vendor_code": TEXT,
        "vendor_name": TEXT,
        "outstanding_paise": INTEGER,
        "outstanding_rupees": TEXT,
        "entries": INTEGER,
    },
    "vendor_id", "vendor_code", "vendor_name", "outstanding_paise", "outstanding_rupees", "entries",
)
VENDOR_BALANCES = obj(
    {
        "total_payable_paise": INTEGER,
        "total_payable_rupees": TEXT,
        "vendors_with_dues": INTEGER,
        "rows": {"type": "array", "items": VENDOR_BALANCE},
    },
    "total_payable_paise", "total_payable_rupees", "vendors_with_dues", "rows",
)
VENDOR_AGEING_ROW = obj(
    {
        "vendor_id": INTEGER,
        "vendor_code": TEXT,
        "vendor_name": TEXT,
        "payment_terms": TEXT,
        "oldest_days": INTEGER,
        "bucket_0_30_paise": INTEGER,
        "bucket_0_30_rupees": TEXT,
        "bucket_31_60_paise": INTEGER,
        "bucket_31_60_rupees": TEXT,
        "bucket_60_plus_paise": INTEGER,
        "bucket_60_plus_rupees": TEXT,
        "total_due_paise": INTEGER,
        "total_due_rupees": TEXT,
        "open_bill_count": INTEGER,
    },
    "vendor_id", "vendor_code", "vendor_name", "oldest_days", "total_due_paise",
)
VENDOR_AGEING = obj(
    {
        "as_of": {"type": "string", "format": "date"},
        "total_due_paise": INTEGER,
        "total_due_rupees": TEXT,
        "bucket_0_30_paise": INTEGER,
        "bucket_0_30_rupees": TEXT,
        "bucket_31_60_paise": INTEGER,
        "bucket_31_60_rupees": TEXT,
        "bucket_60_plus_paise": INTEGER,
        "bucket_60_plus_rupees": TEXT,
        "rows": {"type": "array", "items": VENDOR_AGEING_ROW},
    },
    "as_of", "total_due_paise", "rows",
)
VENDOR_POST = obj(
    {"vendor_id": INTEGER, "amount": RUPEES, "description": TEXT, "reference": TEXT},
    "vendor_id", "amount",
)
VENDOR_PAYMENT = obj(
    {
        "vendor_id": INTEGER,
        "amount": RUPEES,
        "description": TEXT,
        "mode": TEXT,
        "also_cash": {"type": "boolean"},
    },
    "vendor_id", "amount",
)

CASH_DAY_ACCOUNT = obj(
    {"account": TEXT, "in_paise": INTEGER, "out_paise": INTEGER, "net_paise": INTEGER},
    "account", "in_paise", "out_paise", "net_paise",
)
CASH_DAILY = obj(
    {
        "date": {"type": "string", "format": "date"},
        "money_in_paise": INTEGER,
        "money_out_paise": INTEGER,
        "net_paise": INTEGER,
        "accounts": {"type": "array", "items": CASH_DAY_ACCOUNT},
        "entries": {"type": "array", "items": {"$ref": "#/components/schemas/CashLedgerEntry"}},
    },
    "date", "money_in_paise", "money_out_paise", "net_paise", "accounts", "entries",
)
CASH_BALANCE = obj(
    {
        "account": TEXT,
        "balance_paise": INTEGER,
        "balance_rupees": TEXT,
        "entries": INTEGER,
    },
    "account", "balance_paise", "balance_rupees", "entries",
)
CASH_SUMMARY = obj(
    {
        "total_paise": INTEGER,
        "total_rupees": TEXT,
        "accounts": {"type": "array", "items": CASH_BALANCE},
    },
    "total_paise", "total_rupees", "accounts",
)
CASH_MOVEMENT = obj(
    {
        "direction": {"type": "string", "enum": ["in", "out"]},
        "amount": RUPEES,
        "description": TEXT,
        "account": TEXT,
        "mode": TEXT,
    },
    "direction", "amount",
)

BANK_IMPORT = obj(
    {
        "id": INTEGER,
        "created_at": {"type": "string", "format": "date-time"},
        "filename": TEXT,
        "bank_label": TEXT,
        "row_count": INTEGER,
        "matched_count": INTEGER,
        "uploaded_by_name": TEXT,
    },
    "id", "created_at", "filename", "bank_label", "row_count", "matched_count", "uploaded_by_name",
)
BANK_IMPORTS = obj({"rows": {"type": "array", "items": BANK_IMPORT}}, "rows")
BANK_UPLOAD = obj(
    {"file": {"type": "string", "format": "binary"}, "bank_label": TEXT}, "file"
)
BANK_UPLOAD_RESULT = obj(
    {"id": INTEGER, "filename": TEXT, "row_count": INTEGER, "matched_count": INTEGER},
    "id", "filename", "row_count", "matched_count",
)
BANK_LINE = obj(
    {
        "id": INTEGER,
        "import_id": INTEGER,
        "txn_date": {"type": "string", "format": "date"},
        "narration": TEXT,
        "debit_paise": INTEGER,
        "credit_paise": INTEGER,
        "balance_paise": {"type": "integer", "nullable": True},
        "status": TEXT,
        "match_confidence": INTEGER,
        "candidates": {"type": "array", "items": {"type": "object"}},
        "matched_entry_id": {"type": "integer", "nullable": True},
        "matched_entry_doc_number": {"type": "string", "nullable": True},
        "matched_by_name": TEXT,
    },
    "id", "import_id", "txn_date", "status", "candidates",
)
BANK_LINES = obj({"rows": {"type": "array", "items": BANK_LINE}}, "rows")
BANK_MATCH = obj(
    {"action": {"type": "string", "enum": ["link", "unlink", "ignore"]}, "entry_id": INTEGER}
)

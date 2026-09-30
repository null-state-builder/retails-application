"""Read-only legacy SOH adapter. Tqty is current stock; movements are evidence."""

from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook

from core.canonical import content_hash
from core.goods_money import MoneyInvalid, paise_from_amount
from core.refusals import Refusal
from ptmapper.goods_workbook import read_rows

MAX_SOURCE_ROWS = 50_000
REQUIRED = frozenset({"barcode", "tqty", "mrp"})
IDENTITY_COLUMNS = frozenset({"itemname", "name", "item", "brand", "size", "supplier", "designno", "category", "gender", "fit", "season"})
TOTAL_COLUMNS = frozenset({"opqty", "purchase", "sale", "adjustment", "slret", "sttrf", "stfreciept", "purret", "tqty", "amount"})


@dataclass(frozen=True)
class SourceRow:
    ordinal: int
    key: str
    barcode: str
    brand: str
    quantity: int
    mrp: int | None
    rate: int | None
    source: dict[str, str | None]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _integer(value: Any, label: str, number: int) -> int:
    try:
        amount = Decimal(_text(value))
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise InvalidOperation
    except (InvalidOperation, ValueError) as exc:
        raise Refusal("SOH_SOURCE_INVALID", f"Source row {number}: {label} must be a whole quantity.", status=422) from exc
    return int(amount)


def _money(value: Any, label: str, number: int) -> int | None:
    try:
        return paise_from_amount(value)
    except MoneyInvalid as exc:
        raise Refusal("SOH_SOURCE_INVALID", f"Source row {number}: {label} must be exact money.", status=422) from exc


def parse_soh(data: bytes) -> tuple[dict[str, Any], list[SourceRow]]:
    """Find a confirmed header in the first sheet, preserving every source field."""
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = workbook.worksheets[0].title
        workbook.close()
        source_rows = read_rows(data, max_rows=MAX_SOURCE_ROWS + 30)
        header: list[str] | None = None
        header_number = 0
        preamble: list[list[str]] = []
        parsed: list[SourceRow] = []
        seen: set[str] = set()
        footer: dict[str, str | None] | None = None
        for number, values in source_rows:
            if header is None:
                names = [_text(v).casefold().replace(" ", "") for v in values]
                if REQUIRED <= set(names):
                    if len([n for n in names if n]) != len(set(n for n in names if n)):
                        raise Refusal("SOH_SOURCE_INVALID", "Source headers must be unique.", status=422)
                    header = names
                    header_number = number
                elif number <= 20:
                    preamble.append([_text(v) for v in values])
                else:
                    raise Refusal("SOH_SOURCE_INVALID", "Find a header containing Barcode, Tqty and MRP in the first 20 rows.", status=422)
                continue
            cells = {header[i]: values[i] if i < len(values) else None for i in range(len(header)) if header[i]}
            if not any(v not in (None, "") for v in cells.values()):
                continue
            barcode = _text(cells.get("barcode"))
            # A totals row has a closed schema. Never skip an arbitrary row whose
            # barcode is missing: it may be real stock with unresolved identity.
            if not barcode and all(cells.get(key) in (None, "") for key in IDENTITY_COLUMNS | {"mrp", "rate"}) and cells.get("tqty") is not None:
                if footer is not None:
                    raise Refusal("SOH_SOURCE_INVALID", "The source contains more than one totals footer.", status=422)
                if any(value not in (None, "") and key not in TOTAL_COLUMNS for key, value in cells.items()):
                    raise Refusal("SOH_SOURCE_INVALID", "Unknown values occur in the totals footer.", status=422)
                for key in TOTAL_COLUMNS - {"amount"}:
                    if cells.get(key) not in (None, ""):
                        _integer(cells[key], f"footer {key}", number)
                if cells.get("amount") not in (None, ""):
                    _money(cells["amount"], "footer Amount", number)
                footer = {key: None if value is None else _text(value) for key, value in cells.items()}
                continue
            if footer is not None:
                raise Refusal("SOH_SOURCE_INVALID", "Item rows cannot follow the totals footer.", status=422)
            if not barcode or len(barcode) > 128 or barcode in seen:
                raise Refusal("SOH_SOURCE_INVALID", f"Source row {number}: missing, duplicate or overlong barcode.", status=422)
            seen.add(barcode)
            if len(parsed) >= MAX_SOURCE_ROWS:
                raise Refusal("SOH_SOURCE_INVALID", "An SOH source may contain at most 50,000 item rows.", status=422)
            source = {key: None if value is None else _text(value) for key, value in cells.items()}
            parsed.append(SourceRow(number, f"{sheet}:{number}", barcode,
                                    _text(cells.get("brand") or cells.get("company") or cells.get("name")),
                                    _integer(cells.get("tqty"), "Tqty", number),
                                    _money(cells.get("mrp"), "MRP", number),
                                    _money(cells.get("rate"), "Rate", number), source))
    except Refusal:
        raise
    except Exception as exc:
        raise Refusal("SOH_SOURCE_INVALID", "The source XLSX could not be read.", status=422) from exc
    if header is None or not parsed:
        raise Refusal("SOH_SOURCE_INVALID", "The SOH source contains no item rows.", status=422)
    stocked = [row for row in parsed if row.quantity > 0]
    if footer is not None and _integer(footer["tqty"], "footer Tqty", 0) != sum(r.quantity for r in parsed):
        raise Refusal("SOH_SOURCE_INVALID", "The source footer Tqty disagrees with its item quantities.", status=422)
    source_amount = sum((_money(r.source.get("amount"), "Amount", r.ordinal) or 0) for r in parsed)
    if footer is not None and footer.get("amount") is not None and _money(footer["amount"], "footer Amount", 0) != source_amount:
        raise Refusal("SOH_SOURCE_INVALID", "The source footer Amount disagrees with its item values.", status=422)
    quantity_rate = sum((r.rate or 0) * r.quantity for r in parsed)
    return {"sheet": sheet, "header_row": header_number, "columns": header, "preamble": preamble,
            "row_count": len(parsed), "stocked_rows": len(stocked), "quantity": sum(r.quantity for r in stocked),
            "zero_stock_rows": sum(r.quantity == 0 for r in parsed), "negative_rows": sum(r.quantity < 0 for r in parsed),
            "stock_mrp_paise": str(sum((r.mrp or 0) * r.quantity for r in stocked)),
            "rate_meaning": "unconfirmed", "source_amount_paise": str(source_amount), "quantity_rate_paise": str(quantity_rate),
            "source_rate_difference_paise": str(source_amount - quantity_rate),
            "footer": footer, "parsed_hash": content_hash([r.source for r in parsed])}, parsed

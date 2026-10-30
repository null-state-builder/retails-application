"""Read-only adapter for the legacy software's daily SALE REPORT export.

Only what moves stock is kept: the business date, the bill number, the barcode
and the signed quantity, plus rate and net amount as evidence. Customer name,
phone and salesperson columns are dropped on read and never stored.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook

from core.canonical import content_hash
from core.goods_money import MoneyInvalid, paise_from_amount
from core.refusals import Refusal
from ptmapper.goods_workbook import read_rows

MAX_SOURCE_ROWS = 20_000
REQUIRED = frozenset({"billdate", "billno", "barcode", "qty"})
#: Never read past the header: personal data has no business in a stock sync.
DROPPED = frozenset({"customer", "phone", "mobile", "salesman", "salesperson"})


@dataclass(frozen=True)
class SaleRow:
    ordinal: int
    business_date: date
    bill_no: str
    barcode: str
    quantity: int
    rate_paise: int | None
    net_paise: int | None


def _name(value: Any) -> str:
    return "" if value is None else "".join(str(value).casefold().split()).replace(".", "")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        number = Decimal(value)
        if number == number.to_integral_value():
            return str(int(number))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _date(value: Any, number: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for pattern in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise Refusal("LEGACY_SALES_INVALID", f"Source row {number}: Bill Date is not a date.", status=422)


def _quantity(value: Any, number: int) -> int:
    try:
        amount = Decimal(_text(value))
        if not amount.is_finite() or amount != amount.to_integral_value() or amount == 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as exc:
        raise Refusal("LEGACY_SALES_INVALID", f"Source row {number}: Qty must be a non-zero whole number.", status=422) from exc
    return int(amount)


def _money(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return paise_from_amount(value)
    except MoneyInvalid:
        return None


def parse_sales(data: bytes) -> tuple[dict[str, Any], list[SaleRow]]:
    """Legacy sales for a short date range; continuation rows inherit their bill."""
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = workbook.worksheets[0].title
        workbook.close()
        header: list[str] | None = None
        header_number = 0
        rows: list[SaleRow] = []
        bill_no, bill_date = "", None
        for number, values in read_rows(data, max_rows=MAX_SOURCE_ROWS + 30):
            if header is None:
                names = [_name(v) for v in values]
                if REQUIRED <= set(names):
                    header, header_number = names, number
                elif number > 20:
                    raise Refusal("LEGACY_SALES_INVALID",
                                  "Find a header with Bill Date, Bill No, Barcode and Qty in the first 20 rows.", status=422)
                continue
            cells = {header[i]: values[i] if i < len(values) else None
                     for i in range(len(header)) if header[i] and header[i] not in DROPPED}
            barcode = _text(cells.get("barcode"))
            if not barcode:
                # Totals and blank rows carry no barcode and move nothing.
                continue
            if cells.get("billno") not in (None, ""):
                bill_no = _text(cells["billno"])
                bill_date = _date(cells.get("billdate"), number)
            elif cells.get("billdate") not in (None, ""):
                bill_date = _date(cells["billdate"], number)
            if not bill_no or bill_date is None:
                raise Refusal("LEGACY_SALES_INVALID", f"Source row {number}: the item has no bill above it.", status=422)
            if len(rows) >= MAX_SOURCE_ROWS:
                raise Refusal("LEGACY_SALES_INVALID", "A daily sales source may contain at most 20,000 item rows.", status=422)
            rows.append(SaleRow(number, bill_date, bill_no, barcode[:128], _quantity(cells.get("qty"), number),
                                _money(cells.get("rate")), _money(cells.get("netamount"))))
    except Refusal:
        raise
    except Exception as exc:
        raise Refusal("LEGACY_SALES_INVALID", "The sales report XLSX could not be read.", status=422) from exc
    if header is None or not rows:
        raise Refusal("LEGACY_SALES_INVALID", "The sales report contains no item rows.", status=422)
    dates = sorted({row.business_date for row in rows})
    if (dates[-1] - dates[0]).days > 31:
        raise Refusal("LEGACY_SALES_INVALID", "One sales report may cover at most 31 days.", status=422)
    sold = [row for row in rows if row.quantity > 0]
    returned = [row for row in rows if row.quantity < 0]
    return {
        "sheet": sheet,
        "header_row": header_number,
        "date_from": dates[0].isoformat(),
        "date_to": dates[-1].isoformat(),
        "row_count": len(rows),
        "bill_count": len({row.bill_no for row in rows}),
        "sold_quantity": sum(row.quantity for row in sold),
        "returned_quantity": -sum(row.quantity for row in returned),
        "net_paise": str(sum(row.net_paise or 0 for row in rows)),
        "parsed_hash": content_hash([[r.bill_no, r.barcode, r.quantity] for r in rows]),
    }, rows

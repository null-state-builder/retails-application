"""Each brand's saved report layout (store operations ST-BRD-2, ticket 29).

A layout says how one brand wants one report: its title lines, its columns in
order - each a field, the header the brand uses and a format - which columns
are totalled, and the file type. The company's standard layouts (a row with no
brand) serve every brand with no layout of its own; the first two are the KDPS
Sale and SOH sheets (``STANDARD``), put in by migration ``reporting.0012``.

A layout is data, checked here (``clean``) before it is saved, never code: a new
brand's format is a new row. Only the fields in ``FIELDS`` can be asked for, and
none of them is cost or margin, so a brand report never carries either.

Saving is one command (``save``): the audit record holds the layout before and
after. Who may save is ``may_edit`` (baseline B314).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from accounts.principal import access_for_user
from core.commands import CommandRun, LockRank
from core.refusals import Refusal
from masters.models import Brand
from reporting.models import BrandReportKind, BrandReportLayout

SALE = BrandReportKind.SALE.value
SOH = BrandReportKind.SOH.value
KINDS = (SALE, SOH)

#: The audit action a saved layout is recorded under.
SAVE_ACTION = "reports.brand_layout.save"

# -- what a column can show --------------------------------------------------------

#: A field's kind decides the formats it may take.
DATE, TEXT, COUNT, MONEY, PERCENT = "date", "text", "count", "money", "percent"

#: Format code -> words. Dates and numbers use the spreadsheet's own format codes.
FORMATS: dict[str, str] = {
    "text": "Text",
    "d-mmm-yy": "Date like 1-Apr-26",
    "dd-mm-yyyy": "Date like 01-04-2026",
    "0": "Whole number",
    "0.00": "Two decimals",
}
FORMATS_OF_KIND: dict[str, tuple[str, ...]] = {
    DATE: ("d-mmm-yy", "dd-mm-yyyy"),
    TEXT: ("text",),
    COUNT: ("0",),
    MONEY: ("0", "0.00"),
    PERCENT: ("0", "0.00"),
}
#: Only these kinds may be totalled.
TOTALLED = (COUNT, MONEY)


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str


FIXED = Field("fixed", "Fixed text (the same on every row)", TEXT)

FIELDS: dict[str, tuple[Field, ...]] = {
    SALE: (
        Field("day", "Bill date", DATE),
        Field("bill_number", "Bill number", TEXT),
        Field("item", "Item (category)", TEXT),
        Field("brand_on_bill", "Brand as on the bill", TEXT),
        Field("brand_name", "Brand name", TEXT),
        Field("brand_code", "Brand short code", TEXT),
        Field("size", "Size", TEXT),
        Field("design", "Style code", TEXT),
        Field("barcode", "Barcode", TEXT),
        Field("colour", "Colour", TEXT),
        Field("season", "Season", TEXT),
        Field("qty", "Pieces (negative when given back)", COUNT),
        Field("mrp", "MRP of one piece", MONEY),
        Field("gross", "Price: MRP times pieces", MONEY),
        Field("disc_pct", "Discount %", PERCENT),
        Field("disc", "Discount", MONEY),
        Field("value", "Total: what the customer paid, GST inside", MONEY),
        Field("gst", "GST inside the total", MONEY),
        FIXED,
    ),
    SOH: (
        Field("month_start", "First day of the month", DATE),
        Field("stock_day", "Day the stock was taken", DATE),
        Field("item", "Item (category)", TEXT),
        Field("brand_name", "Brand name", TEXT),
        Field("brand_code", "Brand short code", TEXT),
        Field("size", "Size", TEXT),
        Field("design", "Style code", TEXT),
        Field("barcode", "Barcode", TEXT),
        Field("colour", "Colour", TEXT),
        Field("season", "Season", TEXT),
        Field("qty", "Pieces", COUNT),
        Field("mrp", "MRP of one piece", MONEY),
        Field("mrp_value", "Total MRP: MRP times pieces", MONEY),
        FIXED,
    ),
}


def field_of(kind: str, key: str) -> Field | None:
    return next((f for f in FIELDS[kind] if f.key == key), None)


# -- title lines -------------------------------------------------------------------

#: What a title line may name. In capitals (``{COMPANY}``) the value is written in capitals.
PLACEHOLDERS: dict[str, str] = {
    "company": "The company's legal name",
    "brand": "The brand's name",
    "brand_code": "The brand's short code",
    "store": "The store's name",
    "store_code": "The store's code",
    "from": "The period's first day, DD-MM-YYYY",
    "to": "The period's last day, DD-MM-YYYY",
    "month": "The month, like April 2026",
}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")

FILE_TYPES = {"xlsx": "Excel workbook (.xlsx)", "csv": "Comma-separated text (.csv)"}

MAX_COLUMNS = 40
MAX_TITLE_LINES = 5
_FONT = re.compile(r"^[A-Za-z][A-Za-z0-9 ]{0,39}$")


def fill(text: str, values: dict[str, str]) -> str:
    """``text`` with each placeholder replaced; capitals give capitals."""

    def one(found: re.Match[str]) -> str:
        name = found.group(1)
        value = values.get(name.lower(), "")
        return value.upper() if name.isupper() else value

    return _PLACEHOLDER.sub(one, text)


# -- the KDPS sheets: the first layouts -------------------------------------------------


def _col(
    field: str, header: str, fmt: str, *, total: bool = False, value: str = ""
) -> dict[str, Any]:
    column: dict[str, Any] = {"field": field, "header": header, "format": fmt, "total": total}
    if field == FIXED.key:
        column["value"] = value
    return column


STANDARD: dict[str, dict[str, Any]] = {
    SALE: {
        "name": "KDPS Sale sheet (List of Sales Vouchers)",
        "layout": {
            "file_type": "xlsx",
            "sheet_name": "Sale",
            "font": "Cambria",
            "header_size": 10,
            "header_border": "grid",
            "title_lines": [
                {"text": "{COMPANY}", "size": 18},
                {"text": "List of Sales Vouchers", "size": 14},
                {"text": "Voucher Series : {BRAND}  (From {from} To {to})", "size": 14},
            ],
            "columns": [
                _col("day", "Date", "d-mmm-yy"),
                _col("bill_number", "Vch/Bill No.", "text"),
                _col(FIXED.key, "Particulars", "text", value="CASH"),
                _col("item", "Item Details", "text"),
                _col("brand_on_bill", "Brand", "text"),
                _col("size", "Size", "text"),
                _col("design", "Style Code", "text"),
                _col("barcode", "Barcode", "text"),
                _col(FIXED.key, "Unit", "text", value="PCS"),
                _col("qty", "Qty.", "0", total=True),
                _col("gross", "Price", "0", total=True),
                _col("disc_pct", "Dis %", "0.00"),
                _col("disc", "Dis", "0", total=True),
                _col("value", "Total", "0", total=True),
            ],
        },
    },
    SOH: {
        "name": "KDPS SOH sheet (List of Stock Details)",
        "layout": {
            "file_type": "xlsx",
            "sheet_name": "SOH",
            "font": "Cambria",
            "header_size": 11,
            "header_border": "box",
            "title_lines": [
                {"text": "{COMPANY}", "size": 18},
                {"text": "List of Stock Details", "size": 14},
                {"text": "Voucher Series : {BRAND} (From {from})", "size": 12},
            ],
            "columns": [
                _col("month_start", "Date", "d-mmm-yy"),
                _col("item", "Item Details", "text"),
                _col("barcode", "Barcode", "text"),
                _col("size", "Size", "text"),
                _col("design", "Style Code", "text"),
                _col("brand_code", "Brand", "text"),
                _col("qty", "Qty.", "0", total=True),
                _col(FIXED.key, "Unit", "text", value="PCS"),
                _col("mrp", "MRP", "0"),
                _col("mrp_value", "Total MRP", "0", total=True),
            ],
        },
    },
}


# -- checking a layout ---------------------------------------------------------------


def _bad(text: str) -> Refusal:
    return Refusal("INVALID_LAYOUT", text, status=400)


def _text(value: Any, what: str, longest: int, *, blank: bool = False) -> str:
    if not isinstance(value, str):
        raise _bad(f"{what} must be text.")
    text = value.strip()
    if not text and not blank:
        raise _bad(f"{what} cannot be empty.")
    if len(text) > longest:
        raise _bad(f"{what} is longer than {longest} characters.")
    return text


def _size(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 8 <= value <= 24:
        raise _bad(f"{what} must be a whole number from 8 to 24.")
    return value


LAYOUT_KEYS = {
    "file_type",
    "sheet_name",
    "font",
    "header_size",
    "header_border",
    "title_lines",
    "columns",
}
#: How the header row is boxed: a thin grid with a heavy top (the KDPS Sale
#: sheet), or one heavy box round the whole row (the KDPS SOH sheet).
HEADER_BORDERS = {"grid": "Thin grid, heavy top", "box": "Heavy box round the row"}
COLUMN_KEYS = {"field", "header", "format", "total", "value"}


def clean(kind: str, raw: Any) -> dict[str, Any]:
    """The layout as it will be saved, or ``INVALID_LAYOUT`` naming what is wrong."""
    if kind not in KINDS:
        raise _bad("The report must be sale or soh.")
    if not isinstance(raw, dict):
        raise _bad("The layout must be an object.")
    unknown = set(raw) - LAYOUT_KEYS
    if unknown:
        raise _bad(f"The layout has fields it cannot have: {', '.join(sorted(unknown))}.")
    file_type = raw.get("file_type")
    if file_type not in FILE_TYPES:
        raise _bad("The file type must be xlsx or csv.")
    sheet_name = _text(raw.get("sheet_name") or "", "The sheet name", 31, blank=True)
    if re.search(r"[\[\]:*?/\\]", sheet_name):
        raise _bad("The sheet name cannot hold [ ] : * ? / or \\.")
    font = _text(raw.get("font") or "Calibri", "The font", 40)
    if not _FONT.match(font):
        raise _bad("The font must be a font's name, letters, digits and spaces only.")
    columns_in = raw.get("columns")
    if not isinstance(columns_in, list) or not columns_in:
        raise _bad("A layout needs at least one column.")
    if len(columns_in) > MAX_COLUMNS:
        raise _bad(f"A layout has at most {MAX_COLUMNS} columns.")
    return {
        "file_type": file_type,
        "sheet_name": sheet_name,
        "font": font,
        "header_size": _size(raw.get("header_size", 11), "The header size"),
        "header_border": _header_border(raw.get("header_border") or "grid"),
        "title_lines": _title_lines(raw.get("title_lines") or []),
        "columns": [
            _column(kind, number, column) for number, column in enumerate(columns_in, start=1)
        ],
    }


def _header_border(value: Any) -> str:
    if value not in HEADER_BORDERS:
        raise _bad("The header border must be grid or box.")
    return str(value)


def _title_lines(lines: Any) -> list[dict[str, Any]]:
    if not isinstance(lines, list) or len(lines) > MAX_TITLE_LINES:
        raise _bad(f"A layout has at most {MAX_TITLE_LINES} title lines.")
    out = []
    for number, line in enumerate(lines, start=1):
        if not isinstance(line, dict) or set(line) - {"text", "size"}:
            raise _bad(f"Title line {number} must have a text and a size.")
        text = _text(line.get("text"), f"Title line {number}", 120)
        for name in _PLACEHOLDER.findall(text):
            if name.lower() not in PLACEHOLDERS:
                raise _bad(f"Title line {number} names {{{name}}}, which is not a known value.")
        out.append(
            {"text": text, "size": _size(line.get("size", 12), f"Title line {number}'s size")}
        )
    return out


def _column(kind: str, number: int, column: Any) -> dict[str, Any]:
    what = f"Column {number}"
    if not isinstance(column, dict) or set(column) - COLUMN_KEYS:
        raise _bad(f"{what} must have a field, a header and a format.")
    field = field_of(kind, str(column.get("field") or ""))
    if field is None:
        raise _bad(f"{what} asks for a field this report does not have.")
    header = _text(column.get("header"), f"{what}'s header", 60)
    fmt = column.get("format")
    if fmt not in FORMATS_OF_KIND[field.kind]:
        allowed = " or ".join(FORMATS[f] for f in FORMATS_OF_KIND[field.kind])
        raise _bad(f"{what} ({header}) must be formatted as {allowed}.")
    total = column.get("total", False)
    if not isinstance(total, bool):
        raise _bad(f"{what}'s total must be yes or no.")
    if total and field.kind not in TOTALLED:
        raise _bad(f"{what} ({header}) cannot be totalled: only pieces and money can.")
    out: dict[str, Any] = {"field": field.key, "header": header, "format": fmt, "total": total}
    if field.key == FIXED.key:
        out["value"] = _text(column.get("value") or "", f"{what}'s fixed text", 60, blank=True)
    elif "value" in column:
        raise _bad(f"{what} is not fixed text, so it has no fixed value.")
    return out


# -- which layout a brand uses -----------------------------------------------------


def standard(tenant_id: Any, kind: str) -> BrandReportLayout:
    """The company's standard layout for ``kind``. A company that never saved one
    uses the built-in KDPS sheet, unsaved (no id, revision 0): a read writes nothing."""
    row = BrandReportLayout.objects.filter(
        tenant_id=tenant_id, brand__isnull=True, kind=kind
    ).first()
    if row is not None:
        return row
    return BrandReportLayout(
        tenant_id=tenant_id,
        brand=None,
        kind=kind,
        name=STANDARD[kind]["name"],
        layout=STANDARD[kind]["layout"],
        revision=0,
    )


def effective(brand: Brand, kind: str) -> BrandReportLayout:
    """The brand's own layout for ``kind``, else its company's standard one."""
    own = BrandReportLayout.objects.filter(brand=brand, kind=kind).first()
    return own or standard(brand.tenant_id, kind)


# -- saving ----------------------------------------------------------------------------


def may_edit(user: Any, brand_id: int | None = None) -> bool:
    """Accounts saves layouts (baseline B314): the brand's reports are settlement work."""
    return access_for_user(user).covers_all({'brand_report.layout.manage'}, [(None, brand_id)], ['financial'])


def snapshot(row: BrandReportLayout | None) -> dict[str, Any] | None:
    if row is None or row.pk is None:
        return None
    return {
        "id": row.pk,
        "brand_id": row.brand_id,
        "kind": row.kind,
        "name": row.name,
        "revision": row.revision,
        "layout": row.layout,
    }


def save(
    run: CommandRun,
    *,
    user: Any,
    brand: Brand | None,
    kind: str,
    layout: dict[str, Any],
    expected_revision: int | None,
) -> BrandReportLayout:
    """Save one layout; a screen opened on an older revision is refused.

    Saves of one layout are taken in turn (an advisory key per company, brand and
    report), so two first saves cannot both create it."""
    cleaned = clean(kind, layout)
    target = brand.pk if brand else "standard"
    run.advisory_lock(LockRank.CHAIN, [f"brand-layout:{target}:{kind}"])
    rows = BrandReportLayout.objects.select_for_update().filter(tenant_id=run.tenant_id, kind=kind)
    existing = (
        rows.filter(brand__isnull=True) if brand is None else rows.filter(brand=brand)
    ).first()
    current = existing.revision if existing else None
    if expected_revision != current:
        raise Refusal(
            "STALE_REVISION",
            "Someone saved this layout since you opened it. Reload and try again.",
            status=409,
        )
    before = snapshot(existing)
    by = user if getattr(user, "pk", None) else None
    if existing is None:
        name = (
            f"{brand.name} {dict(BrandReportKind.choices)[kind]}"
            if brand
            else str(STANDARD[kind]["name"])
        )
        row = BrandReportLayout.objects.create(
            tenant_id=run.tenant_id,
            brand=brand,
            kind=kind,
            name=name,
            layout=cleaned,
            updated_by=by,
        )
    else:
        row = existing
        row.layout = cleaned
        row.revision += 1
        row.updated_by = by
        row.save(update_fields=["layout", "revision", "updated_by", "updated_at"])
    run.audit_before = before
    run.audit_after = snapshot(row)
    run.audit_subject_key = f"brand_layout:{row.pk}"
    return row

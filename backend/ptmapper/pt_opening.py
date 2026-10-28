"""Store test run: KDPS PT workbooks as a store's opening source (synthetic only).

A PT file says what arrived, row by row, in the KDPS 22-column layout. For a test
store the owner chose (28 Oct 2026) to open stock from the store's PT files and
then bring it to the legacy SOH with the nightly sync. This reads every sheet of
every file that carries the PT header, adds up each barcode's QTY, and returns
the same ``SourceRow`` the SOH parser does, so the reviewed opening path takes
it unchanged. Cost is P RATE (owner decision); HSN is the row's own.

Rows that cannot open stock are kept and listed, never silently dropped: each
carries its ``problem`` in its source, and the opening excludes it with that reason.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook

from core.canonical import content_hash
from core.refusals import Refusal
from ptmapper.goods_workbook import Amount, read_rows, workbook_bytes
from ptmapper.soh_parser import SourceRow

MAX_FILES = 40
MAX_SOURCE_ROWS = 50_000
#: Every PT sheet carries these (headers compared without case or spaces).
REQUIRED = frozenset({"barcode", "qty", "mrp", "prate", "hsn"})
#: PT column → the key the opening path reads from ``SourceRow.source``.
RENAME = {"item": "itemname", "design": "designno", "subcategory": "category"}
KEPT = ("itemname", "brand", "color", "size", "designno", "category", "type", "gender", "fit", "season",
        "hsn", "basic", "prate", "inputtax", "outputtax")
#: The columns of the normalised workbook kept as the source's evidence.
EVIDENCE_HEADER = ["Item Name", "Brand", "Color", "Size", "Barcode", "Design No", "Category", "Type", "Gender",
                   "Fit", "Season", "HSN", "Tqty", "Mrp", "Rate", "Basic", "Input Tax", "Output Tax", "From"]


@dataclass
class _Barcode:
    qty: int = 0
    mrp: set[int] = field(default_factory=set)
    rate: set[int] = field(default_factory=set)
    last: dict[str, str] = field(default_factory=dict)
    mrp_last: int | None = None
    rate_last: int | None = None
    key: str = ""
    origins: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return str(int(value))
    return str(value).strip()


def _paise(value: Any) -> int | None:
    """A rupee amount rounded to paise. P RATE is a computed cell (e.g. 3367.07904)."""
    text = _text(value).replace(",", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _quantity(value: Any) -> int | None:
    text = _text(value).replace(",", "")
    if not text:
        return 0
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount != amount.to_integral_value():
        return None
    return int(amount)


def _sheets(data: bytes) -> list[str]:
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        return [sheet.title for sheet in workbook.worksheets]
    finally:
        workbook.close()


def parse_pt_files(files: list[tuple[str, bytes]]) -> tuple[dict[str, Any], list[SourceRow], dict[str, str]]:
    """``(metadata, rows, problems)``; ``problems`` maps a barcode to why it opens no stock."""
    if not 1 <= len(files) <= MAX_FILES:
        raise Refusal("PT_SOURCE_INVALID", f"Attach 1 to {MAX_FILES} PT workbooks.", status=422)
    found: dict[str, _Barcode] = {}
    unreadable: list[dict[str, Any]] = []
    file_notes: list[dict[str, Any]] = []
    read = 0
    for number, (name, data) in enumerate(files, start=1):
        try:
            titles = _sheets(data)
        except Exception as exc:
            raise Refusal("PT_SOURCE_INVALID", f"{name} could not be read as an XLSX workbook.", status=422) from exc
        used: list[dict[str, Any]] = []
        for index, title in enumerate(titles):
            header: list[str] | None = None
            rows_here = 0
            try:
                for row_number, values in read_rows(data, sheet_index=index, max_rows=MAX_SOURCE_ROWS + 30):
                    if header is None:
                        names = [_text(v).casefold().replace(" ", "") for v in values]
                        if REQUIRED <= set(names):
                            header = names
                        elif row_number > 20:
                            break
                        continue
                    cells: dict[str, Any] = {}
                    for i, column in enumerate(header):
                        if column and column not in cells:
                            cells[column] = values[i] if i < len(values) else None
                    if not any(_text(v) for v in cells.values()):
                        continue
                    where = f"{name} › {title} › row {row_number}"
                    barcode = _text(cells.get("barcode"))
                    qty = _quantity(cells.get("qty"))
                    if not barcode and not qty:
                        continue  # a stray cell (a fill-down FIT, say), not a piece
                    if not barcode or len(barcode) > 128:
                        unreadable.append({"where": where, "reason": "No barcode"})
                        continue
                    if qty is None or qty < 0:
                        unreadable.append({"where": where, "reason": "QTY is not a whole number of pieces"})
                        continue
                    read += 1
                    rows_here += 1
                    if read > MAX_SOURCE_ROWS:
                        raise Refusal("PT_SOURCE_INVALID", "PT files may hold at most 50,000 rows together.", status=422)
                    entry = found.setdefault(barcode, _Barcode())
                    entry.qty += qty
                    mrp, rate = _paise(cells.get("mrp")), _paise(cells.get("prate"))
                    if mrp is not None:
                        entry.mrp.add(mrp)
                        entry.mrp_last = mrp
                    if rate is not None:
                        entry.rate.add(rate)
                        entry.rate_last = rate
                    named = {RENAME.get(k, k): _text(v) for k, v in cells.items()}
                    entry.last = {k: named.get(k, "") for k in KEPT}
                    entry.key = f"{number}:{title}:{row_number}"[:100]
                    entry.origins.append(where)
            except Refusal:
                raise
            except Exception as exc:
                raise Refusal("PT_SOURCE_INVALID", f"{name} › {title} could not be read.", status=422) from exc
            if header is not None:
                used.append({"sheet": title, "rows": rows_here})
        if not used:
            raise Refusal("PT_SOURCE_INVALID", f"{name} has no sheet with the PT columns BARCODE, QTY, MRP, P RATE and HSN.", status=422)
        file_notes.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "sheets": used})
    if not found:
        raise Refusal("PT_SOURCE_INVALID", "The PT files contain no barcode rows.", status=422)

    rows: list[SourceRow] = []
    problems: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for ordinal, (barcode, entry) in enumerate(found.items(), start=1):
        mrp, rate = entry.mrp_last, entry.rate_last
        hsn = entry.last.get("hsn", "")
        if len(entry.mrp) > 1 or len(entry.rate) > 1:
            conflicts.append({"barcode": barcode, "mrp_paise": sorted(str(v) for v in entry.mrp),
                              "rate_paise": sorted(str(v) for v in entry.rate), "used": entry.origins[-1]})
        if not mrp or mrp <= 0:
            problems[barcode] = "PT: no MRP"
        elif not rate or rate <= 0:
            problems[barcode] = "PT: no P RATE"
        elif rate > mrp:
            problems[barcode] = "PT: P RATE is above MRP"
        elif not (hsn.isdigit() and len(hsn) in {4, 6, 8}):
            problems[barcode] = "PT: HSN is not 4, 6 or 8 digits"
        source: dict[str, str | None] = {**entry.last, "barcode": barcode, "tqty": str(entry.qty),
                                         "mrp": None if mrp is None else str(Decimal(mrp) / 100),
                                         "rate": None if rate is None else str(Decimal(rate) / 100),
                                         "from": "; ".join(entry.origins[-3:])[:500]}
        if barcode in problems:
            source["problem"] = problems[barcode]
        rows.append(SourceRow(ordinal, entry.key, barcode, entry.last.get("brand", ""), entry.qty, mrp, rate, source))
    stocked = [row for row in rows if row.quantity > 0]
    value = sum((row.rate or 0) * row.quantity for row in rows)
    metadata = {"sheet": "PT files", "header_row": 0, "columns": sorted({k for row in rows for k in row.source}),
                "preamble": [], "row_count": len(rows), "stocked_rows": len(stocked),
                "quantity": sum(row.quantity for row in stocked), "zero_stock_rows": len(rows) - len(stocked),
                "negative_rows": 0, "stock_mrp_paise": str(sum((row.mrp or 0) * row.quantity for row in stocked)),
                "rate_meaning": "pt_prate", "source_amount_paise": str(value), "quantity_rate_paise": str(value),
                "source_rate_difference_paise": "0", "footer": None,
                "parsed_hash": content_hash([row.source for row in rows]),
                "pt_opening": {"files": file_notes, "pt_rows": read, "unreadable": unreadable[:500],
                               "unreadable_count": len(unreadable), "conflicts": conflicts[:500],
                               "conflict_count": len(conflicts),
                               "problems": [{"barcode": b, "reason": r} for b, r in list(problems.items())[:500]],
                               "problem_count": len(problems)}}
    return metadata, rows, problems


def evidence_workbook(rows: list[SourceRow]) -> bytes:
    """The combined, normalised PT source kept as the import's evidence file."""
    def cell(row: SourceRow, key: str) -> str | None:
        return row.source.get(key) or None

    body: list[list[Any]] = [EVIDENCE_HEADER]
    for row in rows:
        body.append([cell(row, "itemname"), row.brand or None, cell(row, "color"), cell(row, "size"), row.barcode,
                     cell(row, "designno"), cell(row, "category"), cell(row, "type"), cell(row, "gender"),
                     cell(row, "fit"), cell(row, "season"), cell(row, "hsn"), row.quantity,
                     Amount(row.mrp) if row.mrp is not None else None, Amount(row.rate) if row.rate is not None else None,
                     cell(row, "basic"), cell(row, "inputtax"), cell(row, "outputtax"), cell(row, "from")])
    return workbook_bytes("PT opening", body)

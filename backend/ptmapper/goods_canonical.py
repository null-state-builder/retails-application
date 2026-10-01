"""Canonical PT workbook intake for goods-v1 (design §4.4, E122).

Reads one confirmed XLSX evidence object into canonical PT lines. Formulas are
never executed: cached values only. Derived columns in the workbook (P RATE)
become check values, never inputs. A row that cannot be resolved keeps its
supplied text and gets a visible issue at review rather than a guessed identity.
Money cells are read from their exact stored text: blank is unknown, zero is zero,
and a cell that is not an exact rupee amount refuses the whole file.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from core.goods_money import MoneyInvalid, paise_from_amount
from core.offbox import OffboxError, get_store
from core.refusals import Refusal, issue
from files.goods_models import EvidenceObject
from files.goods_services import readable_by
from inbound.goods_models import GoodsGrn
from masters.goods_identity_models import SkuAlias
from ptmapper.goods_item_match import season_for
from ptmapper.goods_pt_services import coverage_lots, match_lot, receipt_pool
from ptmapper.goods_workbook import read_rows

MAX_ROWS = 50_000
HEADER_ALIASES = {
    "SEASON": "season",
    "BARCODE": "barcode",
    "HSN": "hsn",
    "QTY": "qty",
    "MRP": "mrp",
    "BASIC": "basic",
    "P RATE": "p_rate",
}
REQUIRED = ("barcode", "hsn", "qty", "mrp")


def _file_invalid(message: str) -> Refusal:
    return Refusal("PT_FILE_INVALID", message, status=422)


LABELS = {key: label for label, key in HEADER_ALIASES.items()}


def _paise(value: Any, column: str, row: int, problems: list[dict[str, Any]]) -> str | None:
    """An exact amount as integer paise text; a refused cell is noted, not rounded."""
    try:
        paise = paise_from_amount(value)
    except MoneyInvalid as refused:
        label = LABELS[column]
        problems.append(issue(refused.code, f"Row {row}, {label}: {refused.message}.", field=label))
        return None
    return None if paise is None else str(paise)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _qty(value: Any) -> int | None:
    try:
        amount = Decimal(_text(value))
    except (InvalidOperation, ValueError):
        return None
    return int(amount) if amount == amount.to_integral_value() else None


def lines_from_evidence(
    access: Any,
    evidence_id: uuid.UUID,
    grn: GoodsGrn,
    receipt_kind: str = "primary",
    profile_version_id: Any = None,
) -> list[dict[str, Any]]:
    """Whole-file validation first; valid rows may still carry row issues.

    The KDPS PT file itself (a Master Sheet beside staff work sheets, or one work
    sheet) carries every KDPS column, so it is read by the work sheet profile of
    the one brand file mapper rather than as the barcode-only canonical layout.
    """
    evidence = EvidenceObject.objects.filter(tenant_id=access.tenant_id, pk=evidence_id).first()
    if evidence is None or not readable_by(access, evidence):
        raise Refusal("NOT_FOUND", "That evidence file was not found.")
    if not evidence.media_type.endswith("sheet"):
        raise _file_invalid("A canonical PT upload must be an XLSX workbook.")
    if _is_work_sheet_file(evidence):
        from ptmapper.goods_brand_intake import lines_from_brand_file

        return lines_from_brand_file(access, evidence_id, grn, receipt_kind, profile_version_id)
    rows = _rows(evidence)
    first = next(rows, None)
    columns = _columns(first[1] if first is not None else None)
    lots = coverage_lots(receipt_pool(grn.document_id, receipt_kind))
    lines: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for count, (number, raw) in enumerate(rows, start=1):
        if count > MAX_ROWS:
            raise _file_invalid(f"A PT workbook may hold at most {MAX_ROWS} rows.")
        values = {columns[i]: raw[i] for i in range(min(len(raw), len(columns))) if columns[i]}
        if any(value not in (None, "") for value in values.values()):
            source = f"{evidence.filename}!row {number}"
            lines.append(_line(access.tenant_id, values, lots, source, number, problems))
    if problems:
        raise Refusal(
            "PT_FILE_INVALID",
            "Some money cells are not exact rupee amounts; correct them and upload again.",
            status=422,
            issues=problems[:1000],
        )
    return lines


def _is_work_sheet_file(evidence: EvidenceObject) -> bool:
    """The workbook is the KDPS PT file: a Master Sheet tab, or work sheet headers
    (on its first or second row) on any tab."""
    import io

    import openpyxl

    from ptmapper.profiles import MASTER_SHEET_NAME, WORK_SHEET_HEADERS

    try:
        data = get_store().get(evidence.object_key)
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - the canonical reader refuses an unreadable file
        return False
    try:
        for ws in book.worksheets:
            if " ".join(str(ws.title).split()).upper() == MASTER_SHEET_NAME:
                return True
            for row in ws.iter_rows(max_row=2, values_only=True):
                names = {_text(c).upper() for c in row}
                if all(h in names for h in WORK_SHEET_HEADERS):
                    return True
        return False
    finally:
        book.close()


def _rows(evidence: EvidenceObject) -> Iterator[tuple[int, tuple[Any, ...]]]:
    try:
        data = get_store().get(evidence.object_key)
    except OffboxError as exc:
        raise Refusal("EVIDENCE_UNAVAILABLE", "The workbook is not available.") from exc
    try:
        # Read eagerly so an unreadable file is refused here, not half-way through.
        # The header, the row limit and one row more, so an oversized file is refused.
        rows = list(read_rows(data, max_rows=MAX_ROWS + 2))
    except Exception as exc:  # noqa: BLE001 - any unreadable workbook is the same refusal
        raise _file_invalid("The workbook could not be read.") from exc
    return iter(rows)


def _columns(header: tuple[Any, ...] | None) -> list[str | None]:
    if header is None:
        raise _file_invalid("The workbook is empty.")
    known = [HEADER_ALIASES.get(_text(name).upper()) for name in header]
    seen = [column for column in known if column]
    if len(seen) != len(set(seen)):
        raise _file_invalid("Workbook headers must be unique.")
    missing = [column for column in REQUIRED if column not in seen]
    if missing:
        raise _file_invalid(f"Missing required column(s): {', '.join(missing)}.")
    return known


def _line(
    tenant_id: uuid.UUID,
    values: dict[str | None, Any],
    lots: list[dict[str, Any]],
    source_ref: str,
    row: int,
    problems: list[dict[str, Any]],
) -> dict[str, Any]:
    cells = read_cells(tenant_id, {column: values.get(column) for column in LABELS}, row, problems)
    qty = cells.fields["qty"]
    return {
        "line_key": str(uuid.uuid4()),
        "sku_id": cells.fields["sku_id"],
        "season_id": cells.fields["season_id"],
        "alias_id": cells.fields["alias_id"],
        "alias_as_used": cells.fields["alias_as_used"],
        "hsn": cells.fields["hsn"],
        "qty": qty,
        "coverage_requests": match_lot(lots, cells.fields["sku_id"], qty),
        "supplied": cells.supplied,
        "source_ref": source_ref[:200],
    }


# ---------------------------------------------------------------------------
# One canonical row's cells, uploaded or pasted (ticket 06A)
# ---------------------------------------------------------------------------

#: The line money key each canonical money column fills. P RATE is derived, so a
#: supplied P RATE is a check value, never an input (design §4.4).
MONEY_KEYS = {"basic": "basic_paise", "mrp": "mrp_paise", "p_rate": "check_p_rate_paise"}


@dataclass
class CanonicalRead:
    """What one canonical row's cells say, read the one way upload and paste share.

    ``fields`` holds the line values of the columns that were present; ``supplied``
    the money of the money columns that were present, as integer-paise text or
    ``None`` for blank (unknown). ``barcode_skus`` is every item the barcode names,
    so a caller binding the row to counted goods can tell *none* from *several*.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    supplied: dict[str, str | None] = field(default_factory=dict)
    barcode_skus: set[str] = field(default_factory=set)
    text: dict[str, str] = field(default_factory=dict)


def read_cells(
    tenant_id: uuid.UUID,
    values: dict[str, Any],
    row: int,
    problems: list[dict[str, Any]],
) -> CanonicalRead:
    """Read the canonical cells present in ``values`` (keyed ``season``, ``barcode``,
    ``hsn``, ``qty``, ``mrp``, ``basic``, ``p_rate``).

    Money is exact or refused (a refused cell is added to ``problems``, never
    rounded); a barcode names an item only through one effective alias; a season is
    named by its code or its name; a quantity is a whole number or nothing.
    """
    out = CanonicalRead()
    for column, value in values.items():
        out.text[column] = _text(value)
    if "barcode" in values:
        barcode = out.text["barcode"][:128]
        aliases = (
            list(
                SkuAlias.objects.filter(
                    tenant_id=tenant_id, value=barcode, governance_state="effective"
                )
            )
            if barcode
            else []
        )
        out.barcode_skus = {str(alias.sku_id) for alias in aliases}
        single = len(out.barcode_skus) == 1
        out.fields["sku_id"] = str(aliases[0].sku_id) if single else None
        out.fields["alias_id"] = str(aliases[0].pk) if single else None
        out.fields["alias_as_used"] = barcode or None
    if "season" in values:
        # The canonical workbook export writes a season's name; one Season master
        # row must answer, or the row keeps no season.
        season_id, _ambiguous = season_for([out.text["season"]])
        out.fields["season_id"] = str(season_id) if season_id else None
    if "hsn" in values:
        out.fields["hsn"] = out.text["hsn"][:24] or None
    if "qty" in values:
        out.fields["qty"] = _qty(values["qty"])
    for column, key in MONEY_KEYS.items():
        if column in values:
            out.supplied[key] = _paise(values[column], column, row, problems)
    return out

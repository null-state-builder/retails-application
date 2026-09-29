"""A brand's own PT file as goods-v1 receipt PT lines (E122 ``brand_upload``; OPS-15).

Store and warehouse operations PRD §5.4, the third way to fill the grid. The file is
confirmed evidence first (E120), exactly as a canonical upload's is; here it is read
through the one mapping rulebook (:mod:`ptmapper.goods_mapper`) with the brand, vendor
and invoice date of the GRN's arrival, and every item row becomes one draft line:

* **Values.** A describing cell a confirmed rule or the file itself settles is kept
  as an attribute (a vocabulary value); a cell with only close matches keeps *no*
  value - its choices wait under ``suggestions`` until a person picks one. SEASON
  becomes a Season master row only when exactly one row has that code or name.
  Derived columns in the file (P RATE, taxes) are checks, never inputs.
* **Origins.** ``origins`` records, per KDPS column, whether the value came from the
  file, a rule, is a suggestion waiting, or is missing. A person's later edit makes
  that cell ``person`` (``goods_pt_services._EditContext``).
* **Item.** Barcode first, then the describing values, else a drafted new item with
  no SKU and a ``NEW_ITEM`` issue (:mod:`ptmapper.goods_item_match`).
* **Coverage.** Each row is matched to a counted lot as the canonical upload matches
  it. A row no lot can cover is kept with its reason, never dropped, and never adds
  a piece the count did not find.

Nothing is written here: the lines go through the ordinary draft command.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone

from core.goods_fields import MAX_LINE_QTY
from core.goods_money import MoneyInvalid, paise_from_amount
from core.offbox import OffboxError, get_store
from core.refusals import Refusal
from files.goods_models import EvidenceObject
from files.goods_services import readable_by
from inbound.goods_models import GoodsGrn
from masters.goods_models import ConfigVersion
from ptmapper import goods_item_match as items
from ptmapper.goods_mapper import (
    COLUMN_DIMENSIONS,
    MappedCell,
    MappedRow,
    MappingContext,
    UnsupportedFormat,
    map_file,
)
from ptmapper.goods_pt_services import coverage_lots, match_lot, receipt_pool, uncovered_reason
from ptmapper.goods_rulebook import FILE, NONE, RULE, SUGGESTION, Rulebook

SOURCE = "brand_upload"
#: KDPS money column -> the line's supplied key (P RATE is a check, never an input).
MONEY = {"MRP": "mrp_paise", "BASIC": "basic_paise", "P RATE": "check_p_rate_paise"}


def _file_invalid(message: str) -> Refusal:
    return Refusal("PT_FILE_INVALID", message, status=422)


def lines_from_brand_file(
    access: Any,
    evidence_id: uuid.UUID,
    grn: GoodsGrn,
    receipt_kind: str,
    profile_version_id: Any,
) -> list[dict[str, Any]]:
    """Every item row of the brand file as a draft line (whole-file refusals first)."""
    evidence = EvidenceObject.objects.filter(tenant_id=access.tenant_id, pk=evidence_id).first()
    if evidence is None or not readable_by(access, evidence):
        raise Refusal("NOT_FOUND", "That evidence file was not found.")
    try:
        content = get_store().get(evidence.object_key)
    except OffboxError as exc:
        raise Refusal("EVIDENCE_UNAVAILABLE", "The brand file is not available.") from exc
    now = timezone.now()
    arrival = grn.arrival
    context = MappingContext(
        brand_id=arrival.brand_id,
        issuer_key=getattr(arrival.vendor, "code", None) or None,
        invoice_date=arrival.invoice_date or timezone.localdate(arrival.actual_arrival_at),
    )
    try:
        mapped = map_file(
            content,
            evidence.filename,
            evidence.media_type,
            Rulebook.load(access.tenant_id, now),
            context,
        )
    except UnsupportedFormat as exc:
        raise _file_invalid(str(exc)) from exc
    except Refusal:
        raise
    except Exception as exc:  # noqa: BLE001 - any unreadable file is the same refusal
        raise _file_invalid("The brand file could not be read.") from exc
    if mapped.truncated:
        raise _file_invalid("The brand file has more rows than one PT may hold; split it.")
    if not mapped.rows:
        raise _file_invalid("No item rows were found in the brand file.")
    site_id = grn.document.site_id
    barcodes = items.barcode_skus(
        access.tenant_id,
        [row.cells["BARCODE"].value or "" for row in mapped.rows],
        site_id=site_id,
        as_of=now,
    )
    index = items.ItemIndex(access.tenant_id, _family(access.tenant_id, profile_version_id), now)
    lots = coverage_lots(receipt_pool(grn.document_id, receipt_kind))
    seasons: dict[tuple[str, ...], tuple[int | None, bool]] = {}
    source = f"{evidence.filename}!{mapped.sheet}"
    return [
        _line(row, index=index, barcodes=barcodes, lots=lots, seasons=seasons, source=source)
        for row in mapped.rows
    ]


def _family(tenant_id: uuid.UUID, profile_version_id: Any) -> str:
    """The PT profile's product family (the draft command re-checks the profile itself)."""
    try:
        pk = uuid.UUID(str(profile_version_id))
    except ValueError:
        return ""
    version = ConfigVersion.objects.filter(tenant_id=tenant_id, pk=pk, kind="profile").first()
    payload = version.payload if version is not None and isinstance(version.payload, dict) else {}
    return str(payload.get("family") or "")


def _line(
    row: MappedRow,
    *,
    index: items.ItemIndex,
    barcodes: dict[str, list[str]],
    lots: list[dict[str, Any]],
    seasons: dict[tuple[str, ...], tuple[int | None, bool]],
    source: str,
) -> dict[str, Any]:
    cells = row.cells
    line: dict[str, Any] = {
        "line_key": str(uuid.uuid4()),
        "origins": {},
        "suggestions": {},
        "match": {"by": None, "candidates": [], "notes": []},
        "source_ref": f"{source} row {row.source_row}"[:200],
    }
    for column in items.LINE_COLUMNS:
        _origin(line, column, cells[column])
    attributes = [
        {"field_id": dimension, "vocabulary_value_id": cells[column].value_id, "unknown": False}
        for column, dimension in COLUMN_DIMENSIONS.items()
        if column != "SEASON" and _settled(cells[column]) and cells[column].value_id
    ]
    line["attributes"] = attributes
    brand = cells["BRAND"]
    design = cells["DESIGN"].value
    line["describing"] = {
        "brand_id": int(brand.value) if _settled(brand) and brand.value else None,
        "design": design[:120] if design else None,
    }
    line["season_id"] = _season(line, cells["SEASON"], seasons)
    barcode = (cells["BARCODE"].value or "")[:128] or None
    line["alias_as_used"] = barcode
    hsn = cells["HSN"].value
    line["hsn"] = hsn[:24] if hsn else None
    line["qty"] = _qty(line, cells["QTY"])
    line["supplied"] = {key: _paise(line, column, cells[column]) for column, key in MONEY.items()}
    found = items.match_item(
        barcode=barcode,
        brand_id=line["describing"]["brand_id"],
        design=line["describing"]["design"],
        attributes=attributes,
        index=index,
        barcodes=barcodes,
    )
    items.apply_match(line, found)
    line["coverage_requests"] = match_lot(lots, line["sku_id"], line["qty"])
    if not line["coverage_requests"]:
        items.set_note(
            line,
            items.NOTE_UNCOVERED,
            uncovered_reason(lots, line["sku_id"], line),
            field="coverage_requests",
        )
    return line


def _settled(cell: MappedCell) -> bool:
    """A value the file or a confirmed rule gives; a suggestion is never a value."""
    return cell.origin in (FILE, RULE) and cell.value is not None


def _origin(line: dict[str, Any], column: str, cell: MappedCell) -> None:
    line["origins"][column] = cell.origin if cell.origin in (FILE, RULE, SUGGESTION) else NONE
    if cell.origin == SUGGESTION:
        line["suggestions"][column] = {
            "source": (cell.source or "")[:240],
            "choices": [
                {key: str(value)[:240] for key, value in choice.as_dict().items()}
                for choice in cell.suggestions[:10]
            ],
        }


def _season(
    line: dict[str, Any],
    cell: MappedCell,
    seasons: dict[tuple[str, ...], tuple[int | None, bool]],
) -> str | None:
    """The Season master row the mapped season names, or none and a note saying why."""
    if not _settled(cell):
        return None
    names = tuple(sorted({text for text in (cell.value, cell.label) if text}))
    if names not in seasons:
        seasons[names] = items.season_for(names)
    season_id, ambiguous = seasons[names]
    if season_id is not None:
        return str(season_id)
    shown = cell.label or cell.value or ""
    line["origins"]["SEASON"] = NONE
    items.set_note(
        line,
        items.NOTE_SEASON,
        f"Season {shown} names more than one season; choose one"
        if ambiguous
        else f"Season {shown} has no season in the season master; choose one",
        field="season_id",
    )
    return None


def _qty(line: dict[str, Any], cell: MappedCell) -> int | None:
    text = cell.value
    if not text:
        return None
    if text.isascii() and text.isdigit() and 1 <= int(text) <= MAX_LINE_QTY:
        return int(text)
    line["origins"]["QTY"] = NONE
    items.set_note(
        line, items.NOTE_VALUE, f"QTY {text} is not a whole number of pieces from 1", field="qty"
    )
    return None


def _paise(line: dict[str, Any], column: str, cell: MappedCell) -> str | None:
    """An exact rupee amount as paise text; an inexact one is kept out, never rounded."""
    if not cell.value:
        return None
    try:
        paise = paise_from_amount(cell.value)
    except MoneyInvalid as refused:
        line["origins"][column] = NONE
        items.set_note(
            line,
            items.NOTE_VALUE,
            f"{column} {cell.value} {refused.message}",
            field=MONEY[column],
        )
        return None
    return None if paise is None else str(paise)

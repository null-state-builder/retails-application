"""The ``stock_csv`` durable export (design §5.3 ExportSpec, E189-E190).

The rows are the ones E173 (stock on hand) answers, produced through the same
``goods_reads.resolve_query`` - so the file cannot follow different scope, money
or eligibility rules from the screen. That is the whole point of doing it here
rather than in the jobs module: one reader, two renderings.

Three rules the file itself has to keep (GSA-T18 acceptance):

* money is integer paise, written as digits with no scale or separator;
* a value that is not known is the word ``unknown``, never ``0`` and never blank
  - a piece with no origin has no value, which is a different fact from zero;
* the totals row carries ``value_completeness``, because a value total over a mix
  of valued and unvalued pieces is partial and must say so.

Scope is not a parameter of the file: it is the requester's grants. The spec's
``scope`` only *narrows* - asking for a site or brand outside the requester's
reach is NOT_FOUND, exactly as the screen answers it.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.principal import AccessContext
from core.refusals import Refusal, issue
from files.goods_exports import ExportFile, ExportSpec
from stockledger import goods_engine as engine
from stockledger import goods_reads as reads

KIND = "stock_csv"

#: The file's columns, in order. ``key`` reads a stock row; ``money`` marks the
#: cells that are integer paise and may be unknown.
COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("site_id", "SITE", False),
    ("location_id", "LOCATION", False),
    ("sku_id", "SKU", False),
    ("description", "DESCRIPTION", False),
    ("origin_id", "ORIGIN", False),
    ("source_kind", "SOURCE", False),
    ("condition", "CONDITION", False),
    ("physical_qty", "PHYSICAL QTY", False),
    ("accepted_qty", "ACCEPTED QTY", False),
    ("valued_qty", "VALUED QTY", False),
    ("held_qty", "HELD QTY", False),
    ("reserved_qty", "RESERVED QTY", False),
    ("ats_qty", "ATS QTY", False),
    ("transferable_qty", "TRANSFERABLE QTY", False),
    (engine.COST_VALUE, "COST VALUE PAISE", True),
    (engine.TICKET_VALUE, "TICKET VALUE PAISE", True),
)
#: Its own column, not a word borrowed from CONDITION: on a row it says whether
#: that row's value is the whole row's value, and on the totals row whether the
#: total is the whole scope's. Only written when value is in the file at all -
#: with no money there is no aggregate that can be partial.
COMPLETENESS_LABEL = "VALUE COMPLETENESS"
QUANTITY_KEYS = tuple(key for key, _label, money in COLUMNS if key.endswith("_qty"))
#: What a money cell says when the value is not known for every piece in the row.
UNKNOWN = "unknown"
#: Spreadsheets execute a cell that opens with one of these; exports escape them
#: (design §4.4). A stock description comes from vendor data, so this is real.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _query_params(spec: ExportSpec, site_id: int | None) -> dict[str, str]:
    params: dict[str, str] = {"basis": "cost" if "cost" in spec.field_set else "quantity"}
    if site_id is not None:
        params["site_id"] = str(site_id)
    if spec.brand_ids:
        params["brand_id"] = str(spec.brand_ids[0])
    if spec.sbu_ids:
        params["sbu_id"] = spec.sbu_ids[0]
    if spec.as_of:
        params["as_of"] = spec.as_of
    return params


def _check_output_scope(spec: ExportSpec, now: datetime) -> None:
    """E189 step 9: is the declared output scope and watermark one this file can have?

    ``EXPORT_SCOPE_INVALID``, not ``INVALID_REQUEST``: the spec parses, and each
    part of it is a thing the design allows - it is the *output* this export would
    have to produce that cannot be honoured.

    Four conditions here. A stock read filters by one brand and one SBU, so a spec
    naming several would quietly export the first one. A watermark in the future
    cannot be frozen: the worker runs later, so "as at a time that has not
    happened" would produce whatever happened to be true when it ran. And this
    file has exactly one sensitive dimension (``cost``) and no entity filter, so a
    spec asking for ``margin``, ``layer_value``, ``personal`` or an ``entity_id``
    is asking for an output this kind cannot produce.

    The last two refuse rather than drop: a dropped dimension is recorded in the
    audit and the manifest as though it were honoured, so the requester is told
    they got a margin export and never got one.
    """
    for name, values in (("brand_ids", spec.brand_ids), ("sbu_ids", spec.sbu_ids)):
        if len(values) > 1:
            message = f"A stock export names at most one {name[:-4]}."
            raise Refusal(
                "EXPORT_SCOPE_INVALID",
                message,
                issues=[issue("EXPORT_SCOPE_INVALID", message, field=f"scope.{name}")],
            )
    unsupported = sorted(set(spec.field_set) - {"cost"})
    if unsupported:
        message = f"A stock export cannot carry {', '.join(unsupported)}."
        raise Refusal(
            "EXPORT_SCOPE_INVALID",
            message,
            issues=[issue("EXPORT_SCOPE_INVALID", message, field="field_set")],
        )
    if spec.scope.get("entity_id") is not None:
        message = "A stock export is not filtered by entity; name sites, brands or an SBU."
        raise Refusal(
            "EXPORT_SCOPE_INVALID",
            message,
            issues=[issue("EXPORT_SCOPE_INVALID", message, field="scope.entity_id")],
        )
    if spec.as_of:
        asked = parse_datetime(spec.as_of)
        if asked is not None and asked > now:
            message = "An export cannot be taken as at a time that has not happened yet."
            raise Refusal(
                "EXPORT_SCOPE_INVALID",
                message,
                issues=[issue("EXPORT_SCOPE_INVALID", message, field="as_of")],
            )


def _queries(access: AccessContext, spec: ExportSpec) -> list[reads.StockQuery]:
    """One resolved query per named site, or a single whole-scope query.

    Each one re-runs the endpoint's own scope, filter and field checks, so a site
    or brand outside the requester's reach refuses here and never reaches a row.
    """
    _check_output_scope(spec, timezone.now())
    sites: list[int | None] = list(spec.site_ids) or [None]
    return [reads.resolve_query(access, _query_params(spec, site)) for site in sites]


def check(access: AccessContext, spec: ExportSpec) -> None:
    """E189 step 9: is this spec answerable inside the caller's scope, before any rows?"""
    _queries(access, spec)


def _cells(queries: list[reads.StockQuery]) -> tuple[tuple[int | None, int | None], ...]:
    """The ``(site, brand)`` pairs the file was authorised under.

    Taken from the resolved grant terms, not from the rows: an export with no rows
    today still belongs to the same people, and a grant revoked afterwards must
    still be able to close the download (E190 step 5).

    ``reads.reach_cells`` is the one definition of "cells these terms can yield",
    shared with the value-grant check. Pairing a brand with the sites of a term
    limited to a *different* brand would put a cell in the file's scope that the
    requester does not cover, and E121 would then refuse them their own download.
    """
    found: set[tuple[int | None, int | None]] = set()
    for query in queries:
        found |= reads.reach_cells(query.terms, query.brand_id)
    return tuple(sorted(found, key=lambda cell: (cell[0] or 0, cell[1] or 0)))


def _safe(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in FORMULA_STARTS:
        return "'" + value
    return value


def _line(row: dict[str, Any], shows_value: bool) -> list[Any]:
    """One stock row as cells: money as digits or ``unknown``, text made inert."""
    line: list[Any] = []
    for key, _label, money in COLUMNS:
        if money and not shows_value:
            continue
        value = row.get(key)
        if money:
            line.append(UNKNOWN if value is None else str(int(value)))
        else:
            line.append("" if value is None else _safe(value))
    if shows_value:
        # A row's pieces are either all valued or not: the engine writes the money
        # as None the moment one is not (`goods_engine.build_stock_rows`).
        line.append("complete" if row[engine.COST_VALUE] is not None else "unknown")
    return line


def produce(access: AccessContext, spec: ExportSpec) -> ExportFile:
    queries = _queries(access, spec)
    shows_value = "cost" in spec.field_set
    rows: list[dict[str, Any]] = []
    for query in queries:
        rows.extend(reads.on_hand_rows(query))
    watermark = queries[0].watermark.isoformat() if queries else ""

    buffer = io.StringIO()
    # `\r\n` and QUOTE_MINIMAL are RFC 4180; `lineterminator` is set explicitly
    # because csv's default depends on the platform the worker happens to run on.
    writer = csv.writer(buffer, lineterminator="\r\n")
    labels = [label for key, label, money in COLUMNS if shows_value or not money]
    if shows_value:
        labels.append(COMPLETENESS_LABEL)
    writer.writerow(labels)
    physical = valued = 0
    totals: dict[str, int] = dict.fromkeys(QUANTITY_KEYS, 0)
    money_totals = {engine.COST_VALUE: 0, engine.TICKET_VALUE: 0}
    for row in rows:
        writer.writerow(_line(row, shows_value))
        physical += int(row["physical_qty"])
        valued += int(row["valued_qty"])
        for key in QUANTITY_KEYS:
            totals[key] += int(row[key])
        for key in money_totals:
            if row.get(key) is not None:
                money_totals[key] += int(row[key])
    completeness = "complete" if physical == valued else "partial" if valued else "unknown"
    if rows:
        _write_totals(writer, totals, money_totals, completeness, shows_value)

    data = buffer.getvalue().encode("utf-8")
    return ExportFile(
        filename=f"stock-{watermark[:10] or 'export'}.csv",
        media_type="text/csv",
        data=data,
        row_count=len(rows),
        cells=_cells(queries),
        contains_fields=("cost",) if shows_value else (),
        source_watermark=watermark,
        completeness=completeness,
        manifest={"columns": labels},
    )


def _write_totals(
    writer: Any,
    totals: dict[str, int],
    money_totals: dict[str, int],
    completeness: str,
    shows_value: bool,
) -> None:
    """One totals row, saying out loud whether its value total is the whole story."""
    line: list[Any] = []
    for key, _label, money in COLUMNS:
        if money and not shows_value:
            continue
        if key == "site_id":
            line.append("TOTAL")
        elif key in totals:
            line.append(str(totals[key]))
        elif money:
            # The value total is only a total when every piece is valued; otherwise
            # it is the value of the valued pieces, and the completeness cell says so.
            line.append(str(money_totals[key]))
        else:
            line.append("")
    if shows_value:
        line.append(completeness)
    writer.writerow(line)

"""The document each shipment travels with (store operations PRD ST-TRF-2; ticket 36).

    from outbound import transfer_documents

    transfer_documents.issue_for_dispatch(run, transfer, record)   # inside dispatch
    transfer_documents.document_dto(access, document, transfer, issued_by)  # to print

**Which document.** Worked out from the two sites' registrations, never from a
state name (overall PRD R-FIN-017):

* the same GSTIN on both ends: a **delivery challan** in the sending site's
  ``XXX/DC/2627/n`` series (CGST Rules 55). No tax.
* two GSTINs: a **tax invoice** from the sending GSTIN to the receiving one in
  the sending site's ``XXX/26-27/n`` series. IGST when the two GSTINs' state
  codes (their first two characters) differ, CGST and SGST when they match.

**When.** Only where the sending site's ``transfer-documents`` switch is on, and
inside the dispatch command itself, so the shipment, its number and its document
are written together or not at all. The goods never leave without their paper:
a number the series refuses (no prefix, series full) refuses the dispatch, and
``masters.document_series.alert_on_refusal`` around the command puts it in front
of head office. Off, a shipment leaves exactly as it did before this ticket.
The document is dated, its financial year chosen and its rates read by the
server's clock when it is issued - never by the dispatch's business time, which
may be earlier.

**Value (baseline, CA to confirm).** Each piece at the receipt cost its origin
carries (the PT's purchase rate, before tax). The rate is the one that piece
would take sold at its MRP at the sending store that day, under the tax version
that store bills with: the ticket price is never below cost, so where the rate
depends on price this is the higher of the two (§6 principle 4). Tax is worked
out per line to the paisa, half up; the invoice total is not rounded. A tax
invoice needs every value, so a piece with no known cost or ticket price (a
pre-PT custody line), no HSN, or an HSN the store's tax settings have no rule
for refuses the dispatch rather than print a guess.
A delivery challan carries the value where it is known and says "not known"
where it is not, because a movement within one registration is not a supply.

**Who sees money.** The document's figures are cost. They are sent only to a
reader holding the ``cost`` field grant at the sending site for that line's
brand, the rule every other stock value on a transfer follows (goods ticket
13A); without it the lines come without value or tax, never as zeros.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.utils import timezone

from core.commands import CommandRun
from core.kernel_models import AuditEvent
from core.refusals import Refusal
from masters.document_series import DocumentSeries, issue_number
from masters.hsn import is_hsn
from masters.models import Store
from masters.store_feature_registry import TRANSFER_DOCUMENTS
from masters.store_features import is_feature_on
from outbound.goods_models import GoodsTransfer, TransferDispatch
from outbound.transfer_document_models import (
    TransferDocument,
    TransferDocumentKind,
    TransferTaxKind,
)
from sell import gstin as gstin_rules

FEATURE_KEY = TRANSFER_DOCUMENTS
ISSUE_ACTION = "outbound.transfer_document.issue"
#: The stock-read action a value grant must ride on, as on every transfer read.
VALUE_READ = "stock.view"

_HUNDRED = Decimal("100")

KIND = TransferDocumentKind
TAX = TransferTaxKind


# ---------------------------------------------------------------------------
# Which document, from the two registrations
# ---------------------------------------------------------------------------


def document_kind(source_gstin: str, destination_gstin: str) -> str:
    """A delivery challan within one GSTIN, a tax invoice between two."""
    if gstin_rules.normalise(source_gstin) == gstin_rules.normalise(destination_gstin):
        return KIND.DELIVERY_CHALLAN
    return KIND.TAX_INVOICE


def tax_kind(source_gstin: str, destination_gstin: str) -> str:
    """No tax within one GSTIN; IGST across states; CGST and SGST within one."""
    if document_kind(source_gstin, destination_gstin) == KIND.DELIVERY_CHALLAN:
        return TAX.NONE
    if gstin_rules.state_code(source_gstin) != gstin_rules.state_code(destination_gstin):
        return TAX.IGST
    return TAX.CGST_SGST


def series_of(kind: str) -> str:
    if kind == KIND.DELIVERY_CHALLAN:
        return DocumentSeries.DELIVERY_CHALLAN
    return DocumentSeries.TAX_INVOICE


# ---------------------------------------------------------------------------
# The lines: value and tax
# ---------------------------------------------------------------------------


def tax_on(taxable_paise: int, rate: Decimal) -> int:
    """Tax on a line, to the paisa, half up (B9)."""
    return int((Decimal(taxable_paise) * rate / _HUNDRED).quantize(Decimal(1), ROUND_HALF_UP))


def split_tax(tax_paise: int, kind: str) -> tuple[int, int, int]:
    """``(cgst, sgst, igst)``: the whole tax as IGST, or CGST the half rounded down."""
    if kind == TAX.IGST:
        return 0, 0, tax_paise
    if kind == TAX.CGST_SGST:
        cgst = tax_paise // 2
        return cgst, tax_paise - cgst, 0
    return 0, 0, 0


@dataclass(frozen=True)
class _Piece:
    """What one origin's pieces on one shipped line are worth."""

    sku_id: str | None
    origin_id: str | None
    qty: int
    unit_cost: int | None
    mrp: int | None
    hsn: str


def _pieces(record: TransferDispatch) -> list[_Piece]:
    from stockledger import ranges
    from stockledger.goods_models import Origin

    wanted = {
        str(piece["origin_id"])
        for line in record.lines
        for piece in line.get("portions") or []
        if piece.get("origin_id")
    }
    origins = {str(o.pk): o for o in Origin.objects.filter(pk__in=sorted(wanted))}
    out: list[_Piece] = []
    for line in record.lines:
        shares: dict[str | None, int] = {}
        for piece in line.get("portions") or []:
            key = str(piece["origin_id"]) if piece.get("origin_id") else None
            size = ranges.length((int(piece["lower"]), int(piece["upper"])))
            shares[key] = shares.get(key, 0) + size
        if not shares:
            shares[None] = int(line["qty"])
        for origin_id, qty in sorted(shares.items(), key=str):
            origin = origins.get(origin_id) if origin_id else None
            out.append(
                _Piece(
                    sku_id=str(line["sku_id"]) if line.get("sku_id") else None,
                    origin_id=origin_id,
                    qty=qty,
                    unit_cost=int(origin.unit_cost) if origin is not None else None,
                    mrp=int(origin.mrp) if origin is not None and origin.mrp else None,
                    hsn=str((origin.frozen_evidence or {}).get("hsn") or "")
                    if origin is not None
                    else "",
                )
            )
    return out


def _descriptions(tenant_id: Any, sku_ids: set[str]) -> dict[str, str]:
    from masters.goods_identity_services import candidates_for

    out: dict[str, str] = {}
    for row in candidates_for(tenant_id, sku_ids):
        words = [row.get("brand"), row.get("style"), row.get("grade"), row.get("colour")]
        size = row.get("size")
        text = " ".join(str(w) for w in words if w)
        out[str(row["sku_id"])] = f"{text}, size {size}" if size else text
    return out


def _refuse_unpriceable(transfer: GoodsTransfer, pieces: list[_Piece]) -> None:
    """A tax invoice needs every piece's cost, ticket price and HSN; none is guessed."""
    route = f"{transfer.source_site.code} to {transfer.destination_site.code}"
    unvalued = sum(p.qty for p in pieces if p.unit_cost is None or p.mrp is None)
    if unvalued:
        raise Refusal(
            "TRANSFER_VALUE_UNKNOWN",
            f"{unvalued} piece(s) in this shipment have no known cost or ticket price, so a "
            f"tax invoice from {route} cannot be made. Nothing was sent. Send them to a site "
            "under the same GSTIN, or ask head office.",
            status=422,
        )
    no_hsn = sum(p.qty for p in pieces if not is_hsn(p.hsn))
    if no_hsn:
        raise Refusal(
            "TRANSFER_HSN_MISSING",
            f"{no_hsn} piece(s) in this shipment have no HSN, so a tax invoice from {route} "
            "cannot be made. Nothing was sent. Head office corrects the item's HSN first.",
            status=422,
        )


def _priced(
    transfer: GoodsTransfer, record: TransferDispatch, kind: str, taxed: str, at: datetime
) -> tuple[list[dict[str, Any]], int | None]:
    """The document's lines, and the tax version their rates came from.

    ``at`` is when the document is issued (the server's clock): the version in
    force then taxes it, whatever business time the dispatch names.
    """
    from sell.services.tax_rulebook import RULE_NONE, StoreTaxBooks

    pieces = _pieces(record)
    if kind == KIND.TAX_INVOICE:
        _refuse_unpriceable(transfer, pieces)
    names = _descriptions(transfer.tenant_id, {p.sku_id for p in pieces if p.sku_id})
    rulebook = StoreTaxBooks(transfer.source_site).at(at) if kind == KIND.TAX_INVOICE else None
    lines: list[dict[str, Any]] = []
    for piece in pieces:
        taxable = piece.unit_cost * piece.qty if piece.unit_cost is not None else None
        rate = Decimal(0)
        rule_kind = None
        if rulebook is not None:
            # The rate the piece takes sold at its ticket price (see the module).
            line_tax = rulebook.line_tax(piece.hsn, int(piece.mrp or 0), 1)
            if line_tax.rule_kind == RULE_NONE:
                raise Refusal(
                    "TRANSFER_TAX_RULE_MISSING",
                    f"HSN {piece.hsn} has no tax rule in the settings "
                    f"{transfer.source_site.code} bills with, so a tax invoice cannot be made. "
                    "Nothing was sent. Admin adds the rule in Tax Settings first.",
                    status=422,
                )
            rate = line_tax.split.rate
            rule_kind = line_tax.rule_kind
        tax = tax_on(taxable, rate) if taxable is not None and rulebook is not None else 0
        cgst, sgst, igst = split_tax(tax, taxed)
        lines.append(
            {
                "sku_id": piece.sku_id,
                "origin_id": piece.origin_id,
                "description": names.get(piece.sku_id or "", "")
                or "Goods counted at receiving, not yet on a PT",
                "hsn": piece.hsn,
                "qty": piece.qty,
                "unit_cost_paise": piece.unit_cost,
                "taxable_paise": taxable,
                "rate": str(rate.quantize(Decimal("0.01"))) if rulebook is not None else None,
                #: Which rule set the rate (``slab`` or a saved rule's kind), as bills record it.
                "rule_kind": rule_kind,
                "cgst_paise": cgst,
                "sgst_paise": sgst,
                "igst_paise": igst,
                "tax_paise": tax,
            }
        )
    return lines, (rulebook.version if rulebook is not None else None)


# ---------------------------------------------------------------------------
# Issuing, inside the dispatch
# ---------------------------------------------------------------------------


def _party(site: Store) -> dict[str, Any]:
    registration = site.gstin
    return {
        "site_id": str(site.pk),
        "code": site.code,
        "name": site.name,
        "city": site.city,
        "gstin": gstin_rules.normalise(registration.gstin),
        "state_code": gstin_rules.state_code(registration.gstin),
        # Printed for the reader only; the rules above never read it.
        "state_name": registration.state_name,
        "legal_name": registration.legal_entity.name,
    }


def snapshot(document: TransferDocument) -> dict[str, Any]:
    """The document as its audit record holds it. Every figure is cost, so the
    money sits under ``cost_figures``, which the Audit Log strips for a reader
    without the cost grant."""
    return {
        "id": str(document.pk),
        "kind": document.kind,
        "number": document.number,
        "issued_on": document.issued_on.isoformat(),
        "dispatch_id": str(document.dispatch_id),
        "source_gstin": document.parties["source"]["gstin"],
        "destination_gstin": document.parties["destination"]["gstin"],
        "tax_kind": document.tax_kind,
        "tax_version": document.tax_version,
        "pieces": sum(int(line["qty"]) for line in document.lines),
        "cost_figures": {
            "taxable_paise": _text(document.taxable_paise),
            "tax_paise": _text(document.tax_paise),
            "total_paise": _text(document.total_paise),
        },
    }


def _text(value: int | None) -> str | None:
    return None if value is None else str(int(value))


def issue_for_dispatch(
    run: CommandRun, transfer: GoodsTransfer, record: TransferDispatch
) -> TransferDocument | None:
    """Give a shipment that just left its document, if the sending site's switch is on.

    Runs inside the dispatch command, after the shipment row is written and
    before the command commits. Refuses - and so undoes the whole dispatch -
    when the number cannot be issued or a tax invoice would need a value that
    is not known. Wrap the command in ``alert_on_refusal`` from outside.
    """
    source = transfer.source_site
    if not is_feature_on(source, FEATURE_KEY):
        return None
    destination = transfer.destination_site
    kind = document_kind(source.gstin.gstin, destination.gstin.gstin)
    taxed = tax_kind(source.gstin.gstin, destination.gstin.gstin)
    # Dated and taxed by the server's clock when it is issued, never by the
    # business time the dispatch names, which may be earlier.
    issued_at = run.now
    lines, version = _priced(transfer, record, kind, taxed, issued_at)
    known = [line["taxable_paise"] for line in lines]
    taxable = None if any(v is None for v in known) else sum(int(v) for v in known)
    tax = sum(int(line["tax_paise"]) for line in lines)
    issued_on = timezone.localdate(issued_at)
    number = issue_number(
        series=series_of(kind),
        site=source,
        on=issued_on,
        document_type=f"transfer_{kind}",
        document_ref=str(record.pk),
    )
    assert run.principal.human_id is not None
    document = TransferDocument.objects.create(
        tenant_id=run.tenant_id,
        dispatch=record,
        kind=kind,
        number=number.number,
        issued_on=issued_on,
        source_site=source,
        destination_site=destination,
        parties={"source": _party(source), "destination": _party(destination)},
        tax_kind=taxed,
        tax_version=version,
        lines=lines,
        taxable_paise=taxable,
        tax_paise=tax,
        total_paise=None if taxable is None else taxable + tax,
        issued_by_id=run.principal.human_id,
    )
    run.record(
        AuditEvent(
            action=ISSUE_ACTION,
            subject_key=f"transfer_document:{document.pk}",
            site_id=source.pk,
            outcome="recorded",
            reason_code="DISPATCHED",
            before=None,
            after=snapshot(document),
            authority=run.authority,
        )
    )
    return document


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def summary_dto(record: TransferDispatch) -> dict[str, Any] | None:
    """What the shipment card says about its paper: kind and number, no money."""
    document = TransferDocument.objects.filter(dispatch=record).first()
    if document is None:
        return None
    return {
        "id": str(document.pk),
        "kind": document.kind,
        "number": document.number,
        "issued_on": document.issued_on.isoformat(),
    }


def document_dto(
    access: Any, document: TransferDocument, transfer: GoodsTransfer, issued_by: str
) -> dict[str, Any]:
    """The printable document. Money only for a reader with the cost grant per line."""
    from masters.goods_identity_models import ProductSku

    sku_ids = {str(line["sku_id"]) for line in document.lines if line.get("sku_id")}
    brands = {
        str(pk): brand
        for pk, brand in ProductSku.objects.filter(pk__in=sku_ids).values_list(
            "pk", "style__brand_id"
        )
    }
    lines: list[dict[str, Any]] = []
    all_shown = True
    for line in document.lines:
        shown = "cost" in access.field_grants(
            site_id=document.source_site_id,
            brand_id=brands.get(str(line.get("sku_id"))),
            actions={VALUE_READ},
        )
        all_shown = all_shown and shown
        row: dict[str, Any] = {
            "sku_id": line.get("sku_id"),
            "description": line["description"],
            "hsn": line["hsn"],
            "qty": int(line["qty"]),
            "values_shown": shown,
        }
        if shown:
            row.update(
                {
                    "unit_cost_paise": _text(line["unit_cost_paise"]),
                    "taxable_paise": _text(line["taxable_paise"]),
                    "rate": line["rate"],
                    "cgst_paise": _text(line["cgst_paise"]),
                    "sgst_paise": _text(line["sgst_paise"]),
                    "igst_paise": _text(line["igst_paise"]),
                    "tax_paise": _text(line["tax_paise"]),
                }
            )
        lines.append(row)
    out: dict[str, Any] = {
        "id": str(document.pk),
        "kind": document.kind,
        "number": document.number,
        "issued_on": document.issued_on.isoformat(),
        "transfer_id": str(transfer.pk),
        "dispatch_id": str(document.dispatch_id),
        "sequence_no": document.dispatch.sequence_no,
        "dispatched_at": document.dispatch.dispatched_at.isoformat(),
        "transport": document.dispatch.transport,
        "source": document.parties["source"],
        "destination": document.parties["destination"],
        "tax_kind": document.tax_kind,
        "issued_by": issued_by,
        "pieces": sum(int(line["qty"]) for line in document.lines),
        "lines": lines,
        "values_shown": all_shown,
    }
    if all_shown:
        out.update(
            {
                "taxable_paise": _text(document.taxable_paise),
                "tax_paise": _text(document.tax_paise),
                "total_paise": _text(document.total_paise),
            }
        )
    return out

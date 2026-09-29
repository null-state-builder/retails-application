"""Damaged pre-PT custody on a controlled custody transfer (goods ticket 13E).

Overall PRD §15.2.1 rule 10 and R-INV-004 (CH-2026-09-24-01): damaged goods a
GRN counted, and that no PT covers yet, may move between a store and a
warehouse under a separately approved movement. They stay in quarantine at
every site, never become available, and nothing is invented for them - no PT,
SKU, cost, tax or stock layer. Their value stays unknown.

That makes this pool different from the other two in one respect only: the
pieces are identified *from their GRN* - the GRN, its line and the lot the count
opened - rather than by a SKU and an origin a PT registered. Everything after
the draft is the ordinary transfer lifecycle (``outbound.transfers``) run over
this pool: approval by a different person reserves the exact pieces, several
shipments may carry them, each is counted whole into the destination's
quarantine, and a failed delivery comes back into the source's quarantine.
The one change in the lifecycle is the document: a PT is never made for
pre-PT custody, so the approved movement is the transfer's own document,
officialised (``transfers.approve``), and no transfer PT exists.

*Pre-PT custody* here means, exactly: pieces of a lot a GRN (or its approved
counter-GRN) opened, standing physically in the site's quarantine, counted or
reported ``damaged``, under a damage hold, with no origin and no value basis of
any kind (no live PT coverage, no ``value_damage`` memo), and reserved to
nobody. Wrong, unidentified and excess goods keep their own routes; a damaged
piece a PT or ``value_damage`` has given a value is recorded stock and moves
on a quarantine transfer (13D) instead.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.goods_fields import bounds
from core.refusals import Refusal
from stockledger import ranges
from stockledger.goods_models import (
    ActiveHold,
    ActiveReservation,
    LiveCoverage,
    LiveValueBasis,
    Position,
)

if TYPE_CHECKING:
    from inbound.goods_models import GoodsGrn

#: A pre-PT draft line names the GRN line the pieces were counted on, never a SKU.
LINE_FIELDS = frozenset({"line_key", "grn_id", "grn_line_key", "qty", "note"})
#: What a frozen pre-PT line says about the value it carries: nothing is known.
UNKNOWN_VALUE = "unknown"


def _not_eligible(message: str) -> Refusal:
    return Refusal("INSUFFICIENT_ELIGIBLE_STOCK", message)


@dataclass(frozen=True)
class Piece:
    """One exact range of damaged pre-PT custody and the GRN line it was counted on."""

    lot_id: uuid.UUID
    interval: ranges.Interval
    grn_id: uuid.UUID
    grn_line_key: uuid.UUID
    sku_id: uuid.UUID | None
    description: str
    location_id: uuid.UUID | None
    source_time: datetime


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


def _grn_lines(
    source_line_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]:
    """Official GRN / counter-GRN line id → (the GRN's document id, the GRN line key)."""
    from core.kernel_models import OfficialLine
    from inbound.goods_models import CounterGrnDraft, GoodsGrn

    rows = list(
        OfficialLine.objects.filter(pk__in=list(source_line_ids)).values_list(
            "pk", "stable_line_key", "version__document_id"
        )
    )
    documents = {document for _pk, _key, document in rows}
    grns = set(
        GoodsGrn.objects.filter(document_id__in=documents).values_list("document_id", flat=True)
    )
    counters = dict(
        CounterGrnDraft.objects.filter(document_id__in=documents).values_list(
            "document_id", "grn__document_id"
        )
    )
    out: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
    for pk, key, document in rows:
        grn = document if document in grns else counters.get(document)
        if grn is not None:
            out[pk] = (grn, key)
    return out


def _by_lot(queryset: Any) -> dict[uuid.UUID, list[ranges.Interval]]:
    out: dict[uuid.UUID, list[ranges.Interval]] = defaultdict(list)
    for lot_id, stored in queryset.values_list("lot_id", "portion"):
        out[lot_id].append(bounds(stored))
    return out


def pool(
    site_id: int, *, grn_id: uuid.UUID | None = None, grn_line_key: uuid.UUID | None = None
) -> list[Piece]:
    """Damaged pre-PT custody a pre-PT transfer from ``site_id`` could take, oldest first.

    Nothing at a site that is not goods-ready, or is frozen by a count - the
    same fence every other allocation pool has.
    """
    from inbound.goods_services import DAMAGE_HOLD_KINDS
    from masters.goods_models import SiteGuard

    guard = SiteGuard.objects.filter(site_id=site_id).first()
    if guard is None or not guard.goods_ready or guard.freeze_id:
        return []
    positions = list(
        Position.objects.select_related("lot").filter(
            site_id=site_id,
            boundary="physical",
            location__kind="quarantine",
            location__system=True,
            condition="damaged",
            origin_id__isnull=True,
            value_basis_origin_id__isnull=True,
        )
    )
    lot_ids = sorted({p.lot_id for p in positions}, key=str)
    links = _grn_lines({p.lot.source_line_id for p in positions if p.lot.source_line_id})
    damage = _by_lot(ActiveHold.objects.filter(lot_id__in=lot_ids, kind__in=DAMAGE_HOLD_KINDS))
    taken = _by_lot(ActiveReservation.objects.filter(lot_id__in=lot_ids))
    valued = _by_lot(LiveCoverage.objects.filter(lot_id__in=lot_ids))
    memo = _by_lot(LiveValueBasis.objects.filter(lot_id__in=lot_ids))
    out: list[Piece] = []
    for position in positions:
        link = links.get(position.lot.source_line_id) if position.lot.source_line_id else None
        if link is None:
            continue
        grn, key = link
        if (grn_id is not None and grn != grn_id) or (
            grn_line_key is not None and key != grn_line_key
        ):
            continue
        held = ranges.intersect([bounds(position.portion)], damage.get(position.lot_id, []))
        free = ranges.subtract(
            held,
            [
                *taken.get(position.lot_id, []),
                *valued.get(position.lot_id, []),
                *memo.get(position.lot_id, []),
            ],
        )
        out.extend(
            Piece(
                lot_id=position.lot_id,
                interval=interval,
                grn_id=grn,
                grn_line_key=key,
                sku_id=position.sku_id,
                description=position.description,
                location_id=position.location_id,
                source_time=position.lot.source_time,
            )
            for interval in free
        )
    return sorted(out, key=lambda p: (p.source_time, str(p.lot_id), p.interval[0]))


def take(pieces: Sequence[Piece], qty: int) -> list[Piece]:
    """The oldest ``qty`` pieces of ``pieces``, cutting the last range if it must."""
    from dataclasses import replace

    chosen: list[Piece] = []
    left = qty
    for piece in pieces:
        if left <= 0:
            break
        size = min(left, ranges.length(piece.interval))
        lower = piece.interval[0]
        chosen.append(replace(piece, interval=(lower, lower + size)))
        left -= size
    return chosen


# ---------------------------------------------------------------------------
# Drafting and freezing
# ---------------------------------------------------------------------------


def grn_at(grn_id: uuid.UUID, site_id: int) -> GoodsGrn:
    """The GRN a pre-PT line names, which must be one issued at the sending site."""
    from inbound.goods_services import goods_grn

    grn = goods_grn(grn_id)
    if grn is None or grn.document.held_site_id != site_id:
        raise Refusal("NOT_FOUND", "That GRN was not found at the sending site.")
    return grn


def check_draft(source: Any, destination: Any, lines: Sequence[Any]) -> set[int]:
    """A pre-PT draft moves between a store and a warehouse, from its own site's GRNs.

    Rule 10 names exactly that route (store ↔ warehouse); a store-to-store or
    warehouse-to-warehouse movement of pre-PT custody is not authorised. Every
    line names a GRN issued at the source and a line that GRN actually has.
    Answers the brands of the GRNs named, for the business-unit check.
    """
    from inbound.goods_services import grn_line_state
    from masters.models import Store

    kinds = {source.store_type, destination.store_type}
    if kinds != {Store.StoreType.STORE, Store.StoreType.WAREHOUSE}:
        raise Refusal(
            "TRANSFER_INVALID",
            "Damaged goods that are not yet on a PT move only between a store and a "
            "warehouse (overall PRD §15.2.1 rule 10).",
            status=422,
        )
    brands: set[int] = set()
    for line in lines:
        grn = grn_at(line.grn_id, source.pk)
        if line.grn_line_key not in grn_line_state(grn.document_id):
            raise Refusal("NOT_FOUND", "That GRN has no such line.")
        brands.add(int(grn.arrival.brand_id))
    return brands


def _reports_by_lot(lot_ids: Iterable[uuid.UUID]) -> dict[str, set[str]]:
    """Every damage report naming any of these lots, per lot - in one query."""
    from django.db.models import Q

    from outbound.goods_models import DamageReport

    wanted = sorted({str(lot) for lot in lot_ids})
    if not wanted:
        return {}
    match = Q()
    for lot_id in wanted:
        match |= Q(lines__contains=[{"lot_id": lot_id}])
    out: dict[str, set[str]] = defaultdict(set)
    for pk, lines in DamageReport.objects.filter(match).values_list("pk", "lines"):
        for line in lines or []:
            lot = str(line.get("lot_id"))
            if lot in wanted:
                out[lot].add(str(pk))
    return out


def evidence(lot_ids: Iterable[uuid.UUID]) -> tuple[list[str], list[str]]:
    """The damage reports over these lots, and the evidence files their holds cite."""
    from django.db.models import Q

    from inbound.goods_models import Disposition
    from outbound.goods_models import DamageReport

    lots = sorted(set(lot_ids), key=str)
    wanted = [str(lot) for lot in lots]
    if not wanted:
        return [], []
    match = Q()
    for lot_id in wanted:
        match |= Q(lines__contains=[{"lot_id": lot_id}])
    reports = sorted(
        str(pk) for pk in DamageReport.objects.filter(match).values_list("pk", flat=True)
    )
    files: set[str] = set()
    for decision in Disposition.objects.filter(
        lot_id__in=lots, kind=Disposition.Kind.HOLD_DAMAGE
    ).values_list("decision", flat=True):
        files.update(str(e) for e in (decision or {}).get("evidence_ids") or [])
    return reports, sorted(files)


def freeze(site_id: int, line: Any) -> tuple[list[Piece], dict[str, Any]]:
    """The exact pieces a pre-PT line takes, and what the approver reviews about them.

    Oldest first within the GRN line. The line carries what the GRN actually
    says - its number, the line, the identity it counted (a SKU only if the
    count gave one), the condition it counted, any discrepancy remark and tag -
    and the damage reports and evidence on these pieces. Its value is
    ``unknown``: no cost, MRP or tax is looked up or made up.
    """
    from inbound.goods_services import grn_line_state

    grn = grn_at(line.grn_id, site_id)
    available = pool(site_id, grn_id=line.grn_id, grn_line_key=line.grn_line_key)
    have = sum(ranges.length(p.interval) for p in available)
    if have < line.qty:
        raise _not_eligible(
            f"Only {have} damaged piece(s) of that GRN line are held in the source's "
            "quarantine, on no PT and reserved to nobody."
        )
    chosen = take(available, line.qty)
    state = grn_line_state(grn.document_id).get(line.grn_line_key) or {}
    payload = state.get("payload") or {}
    identity = payload.get("identity") or {}
    reports, files = evidence(p.lot_id for p in chosen)
    sku = chosen[0].sku_id if len({p.sku_id for p in chosen}) == 1 else None
    return chosen, {
        "grn_id": str(grn.document_id),
        "grn_number": grn.document.official_number,
        "grn_line_key": str(line.grn_line_key),
        "sku_id": str(sku) if sku else None,
        "description": chosen[0].description or str(identity.get("description") or ""),
        "raw_alias": identity.get("raw_alias"),
        "condition": "damaged",
        "counted_condition": state.get("condition"),
        "discrepancy_remark": payload.get("discrepancy_remark"),
        "value": UNKNOWN_VALUE,
        "damage_report_ids": reports,
        "evidence_ids": files,
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def rows(site_id: int) -> list[dict[str, Any]]:
    """The pool as a person choosing what to send reads it: one row per GRN line.

    ``_brand_id`` is the GRN's brand, for the caller's scope check; it is not
    part of the answer.
    """
    from inbound.goods_models import GoodsGrn

    pieces = pool(site_id)
    grns = {
        grn.document_id: grn
        for grn in GoodsGrn.objects.select_related("document", "arrival").filter(
            document_id__in={p.grn_id for p in pieces}
        )
    }
    kinds: dict[uuid.UUID, list[tuple[ranges.Interval, str]]] = defaultdict(list)
    for hold in ActiveHold.objects.filter(lot_id__in={p.lot_id for p in pieces}):
        kinds[hold.lot_id].append((bounds(hold.portion), hold.kind))
    reported = _reports_by_lot(p.lot_id for p in pieces)
    grouped: dict[tuple[uuid.UUID, uuid.UUID], list[Piece]] = defaultdict(list)
    for piece in pieces:
        grouped[(piece.grn_id, piece.grn_line_key)].append(piece)
    out: list[dict[str, Any]] = []
    for (grn_id, key), mine in grouped.items():
        grn = grns[grn_id]
        skus = {p.sku_id for p in mine}
        reports = sorted({r for p in mine for r in reported.get(str(p.lot_id), set())})
        out.append(
            {
                "grn_id": str(grn_id),
                "grn_number": grn.document.official_number,
                "grn_line_key": str(key),
                "sku_id": str(next(iter(skus))) if len(skus) == 1 and None not in skus else None,
                "description": mine[0].description,
                "condition": "damaged",
                "qty": sum(ranges.length(p.interval) for p in mine),
                "received_at": min(p.source_time for p in mine).isoformat(),
                "hold_kinds": sorted(
                    {
                        kind
                        for p in mine
                        for interval, kind in kinds.get(p.lot_id, [])
                        if ranges.intersect([interval], [p.interval])
                    }
                ),
                "damage_report_ids": reports,
                "_brand_id": grn.arrival.brand_id,
            }
        )
    return sorted(out, key=lambda row: (row["received_at"], row["grn_id"], row["grn_line_key"]))


def transfers_of_grn(grn: GoodsGrn) -> list[dict[str, Any]]:
    """Every pre-PT custody transfer that names this GRN's goods, oldest first.

    The GRN's own record of where its damaged pieces went: each movement's id,
    state, both ends and how many of this GRN's pieces it names.
    """
    from core.kernel_models import DraftLine, OfficialLine
    from outbound import transfers
    from outbound.goods_models import GoodsTransfer

    out: list[dict[str, Any]] = []
    wanted = str(grn.document_id)
    # Only the movements whose own document names this GRN on some line, found
    # in two queries; each of those is then read as its detail reads it.
    documents = set(
        DraftLine.objects.filter(payload__grn_id=wanted).values_list("document_id", flat=True)
    ) | set(
        OfficialLine.objects.filter(payload__grn_id=wanted).values_list(
            "version__document_id", flat=True
        )
    )
    for transfer in (
        GoodsTransfer.objects.select_related("document")
        .filter(
            custody=GoodsTransfer.Custody.PRE_PT,
            source_site_id=grn.document.held_site_id,
            document_id__in=documents,
        )
        .order_by("document__created_at", "id")
    ):
        mine = [
            line for line in transfers.plan_lines(transfer) if str(line.get("grn_id")) == wanted
        ]
        if not mine:
            continue
        out.append(
            {
                "id": str(transfer.pk),
                "state": transfer.state,
                "source_site_id": str(transfer.source_site_id),
                "destination_site_id": str(transfer.destination_site_id),
                "qty": sum(int(line.get("qty") or 0) for line in mine),
                "line_keys": sorted({str(line.get("grn_line_key")) for line in mine}),
            }
        )
    return out


def where_now(
    lot_ids: Sequence[uuid.UUID], home_site_id: int
) -> Mapping[uuid.UUID, dict[str, int]]:
    """Per lot, how much is on the road and how much stands at a site other than home."""
    out: dict[uuid.UUID, dict[str, int]] = defaultdict(lambda: {"in_transit": 0, "elsewhere": 0})
    for position in Position.objects.filter(lot_id__in=list(lot_ids)):
        size = ranges.length(bounds(position.portion))
        if position.boundary == "transit":
            out[position.lot_id]["in_transit"] += size
        elif position.boundary == "physical" and position.site_id != home_site_id:
            out[position.lot_id]["elsewhere"] += size
    return out

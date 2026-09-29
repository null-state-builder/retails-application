"""SBU retirement residuals (GSA-T02, design E246).

An SBU is a site x brand business unit (or the site's one brand-less fallback).
It may be retired only when nothing still refers to it: no physical, quarantined,
unvalued, reserved or in-transit quantity, no open document and no unresolved
exception. There is no residual override - each residual leaves through its own
authorised transition first.

Attribution is conservative, never an invented zero:

* A stock portion belongs to the brand its lot froze when it was counted
  (``CustodyLot.initial_identity["brand_id"]``), else to its SKU's brand. A
  portion with neither cannot be placed in any SBU, so the quantities it could
  touch are *unknown* for every SBU at the site.
* The fallback SBU holds whatever is at the site under a brand that has no live
  branded SBU there.
* Legacy-ledger stock is invisible to these ranges; while any remains at the
  site the quantities it could touch are unknown (as in ``readiness_residuals``).
* Documents and exceptions that carry no brand could refer to any SBU at their
  site, so they block every one of them (Anand, 18 September 2026). Bookings,
  arrivals (counted or still awaiting their count), GRNs, counter-GRNs, count
  sessions and approval requests carry a brand and block only its SBU.

Retiring an SBU also ends every live grant scoped to it (future ones never take
effect) and moves each holder's security epoch, as retiring a person does
(change PRD rule P5). Once retired, brand-bound commands refuse new work for that
site and brand (``require_active_sbu``; Anand, 18 September 2026).

Future movement tickets that add a stock, document or exception state must
extend these readers; the retirement rule itself does not change.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.refusals import Refusal, issue
from masters.goods_models import Sbu

if TYPE_CHECKING:
    from core.commands import CommandRun

#: Stock quantities that block retirement, in reporting order.
SBU_QUANTITIES = ("physical_qty", "quarantined_qty", "unvalued_qty", "reserved_qty", "transit_qty")
_HELD = ("physical_qty", "quarantined_qty", "unvalued_qty", "reserved_qty")

_UNKNOWN = object()

_LABELS = {
    "physical_qty": "pieces physically at the site",
    "quarantined_qty": "pieces in quarantine",
    "unvalued_qty": "pieces without a value",
    "reserved_qty": "pieces reserved",
    "transit_qty": "pieces in transit to or from the site",
}


def _as_brand(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _Scope:
    """Which brands this SBU answers for at its site."""

    def __init__(self, sbu: Sbu) -> None:
        self.sbu = sbu
        self.branded = {
            b
            for b in Sbu.objects.filter(site_id=sbu.site_id, retired_at__isnull=True)
            .exclude(pk=sbu.pk)
            .exclude(brand__isnull=True)
            .values_list("brand_id", flat=True)
        }

    def owns(self, brand: Any) -> bool | None:
        """True/False for a known brand; None when the brand is unknown."""
        if brand is _UNKNOWN:
            return None
        if self.sbu.brand_id is not None:
            return bool(brand == self.sbu.brand_id)
        return brand not in self.branded

    def may_own(self, brand: int | None) -> bool:
        """For a record that may carry no brand: brand-less records refer to every SBU."""
        return True if brand is None else bool(self.owns(brand))


def _stock_brands(positions: list[Any]) -> dict[Any, Any]:
    """The frozen brand of each position's lot, else its SKU's brand, else unknown."""
    from masters.goods_identity_models import ProductSku

    sku_brand = dict(
        ProductSku.objects.filter(pk__in={p.sku_id for p in positions if p.sku_id}).values_list(
            "pk", "style__brand_id"
        )
    )
    brands: dict[Any, Any] = {}
    for position in positions:
        frozen = _as_brand((position.lot.initial_identity or {}).get("brand_id"))
        if frozen is not None:
            brands[position.pk] = frozen
        elif position.sku_id and sku_brand.get(position.sku_id) is not None:
            brands[position.pk] = sku_brand[position.sku_id]
        else:
            brands[position.pk] = _UNKNOWN
    return brands


def _positions(site_id: int) -> tuple[list[Any], list[Any]]:
    """Goods-ledger portions physically at the site, and those in transit to or from it."""
    from django.db.models import Q

    from stockledger.goods_models import Position

    held = list(
        Position.objects.select_related("lot", "location").filter(
            site_id=site_id, boundary="physical"
        )
    )
    moving = list(
        Position.objects.select_related("lot")
        .filter(boundary="transit")
        .filter(Q(transfer__source_site_id=site_id) | Q(transfer__destination_site_id=site_id))
    )
    return held, moving


def _reserved(physical: dict[Any, list[tuple[int, int]]]) -> int:
    from core.goods_fields import bounds
    from stockledger import ranges
    from stockledger.goods_models import ActiveReservation

    reserved: dict[Any, list[tuple[int, int]]] = {}
    for reservation in ActiveReservation.objects.filter(lot_id__in=list(physical)):
        reserved.setdefault(reservation.lot_id, []).append(bounds(reservation.portion))
    return sum(
        ranges.total(ranges.intersect(physical[lot], intervals))
        for lot, intervals in reserved.items()
    )


def _ledger_quantities(scope: _Scope, quantities: dict[str, Any], unknown: set[str]) -> bool:
    """Add this SBU's goods-ledger quantities; True if any portion has no known brand."""
    from core.goods_fields import bounds
    from stockledger import ranges

    held, moving = _positions(scope.sbu.site_id)
    brands = _stock_brands(held + moving)
    physical: dict[Any, list[tuple[int, int]]] = {}
    for position in held:
        owned = scope.owns(brands[position.pk])
        if owned is None:
            unknown.update(_HELD)
        if not owned:
            continue
        interval = bounds(position.portion)
        size = ranges.length(interval)
        physical.setdefault(position.lot_id, []).append(interval)
        quantities["physical_qty"] += size
        if position.location is not None and position.location.kind == "quarantine":
            quantities["quarantined_qty"] += size
        if position.origin_id is None and position.value_basis_origin_id is None:
            quantities["unvalued_qty"] += size
    for position in moving:
        owned = scope.owns(brands[position.pk])
        if owned is None:
            unknown.add("transit_qty")
        elif owned:
            quantities["transit_qty"] += ranges.length(bounds(position.portion))
    quantities["reserved_qty"] = _reserved(physical)
    return any(brand is _UNKNOWN for brand in brands.values())


def _legacy_unknowns(site_id: int, unknown: set[str]) -> list[dict[str, Any]]:
    """Legacy-ledger stock is invisible to the goods ranges: what it touches is unknown."""
    from django.db.models import Q

    from stockledger.models import InTransitStock, QuarantineStock, StockOnHand

    reasons: list[dict[str, Any]] = []
    if (
        StockOnHand.objects.filter(store_id=site_id).exclude(net_qty=0).exists()
        or QuarantineStock.objects.filter(store_id=site_id).exclude(qty=0).exists()
    ):
        unknown.update(_HELD)
        reasons.append(
            issue(
                "LEGACY_STOCK_NOT_ASSESSED",
                "The site still holds legacy-ledger stock, which these quantities cannot see.",
            )
        )
    if (
        InTransitStock.objects.filter(Q(source_store_id=site_id) | Q(destination_store_id=site_id))
        .exclude(qty=0)
        .exists()
    ):
        unknown.add("transit_qty")
        reasons.append(
            issue(
                "LEGACY_TRANSIT_NOT_ASSESSED",
                "Legacy-ledger stock is in transit to or from the site.",
            )
        )
    return reasons


def _stock(scope: _Scope) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    quantities: dict[str, Any] = dict.fromkeys(SBU_QUANTITIES, 0)
    unknown: set[str] = set()
    reasons: list[dict[str, Any]] = []
    if _ledger_quantities(scope, quantities, unknown):
        reasons.append(
            issue(
                "UNATTRIBUTED_STOCK",
                "The site holds or moves goods with no known brand, so it cannot be told "
                "which business unit they belong to.",
            )
        )
    reasons += _legacy_unknowns(scope.sbu.site_id, unknown)
    for key in unknown:
        quantities[key] = None
    return quantities, reasons


def _document(identity: Any, brand: int | None) -> dict[str, Any]:
    return {
        "id": str(identity.pk),
        "kind": identity.kind,
        "number": identity.official_number,
        "brand_id": str(brand) if brand is not None else None,
    }


def _record(pk: Any, kind: str, brand: int | None = None) -> dict[str, Any]:
    return {
        "id": str(pk),
        "kind": kind,
        "number": None,
        "brand_id": str(brand) if brand is not None else None,
    }


def _numbered_documents(scope: _Scope) -> list[dict[str, Any]]:
    """Unfinished drafts, open bookings, unfinished transfers, counts and RTVs."""
    from django.db.models import Q

    from core.kernel_models import DocumentHead
    from outbound.goods_models import GoodsMovement, GoodsStocktake, GoodsTransfer
    from outbound.transfers import TRANSFER_DOC_KIND
    from vendors.goods_models import GoodsBooking

    site_id = scope.sbu.site_id
    found: dict[str, dict[str, Any]] = {}

    def add(identity: Any, brand: int | None) -> None:
        if scope.may_own(brand):
            found.setdefault(str(identity.pk), _document(identity, brand))

    # A transfer's own document head stays a draft for its whole life - its
    # plan is the transfer PT - except on a pre-PT custody transfer (goods
    # ticket 13E), which has no transfer PT and whose own head is submitted and
    # then officialised. Either way a transfer is read from its own state
    # below, never from that head; otherwise a completed transfer would be an
    # open document at its source for ever (found by goods ticket 13C).
    # A count's own head likewise stays a draft for its whole life (goods ticket
    # 17: its number is given at the start, and it ends closed or cancelled on
    # its own row), so it too is read from its own state below.
    heads = list(
        DocumentHead.objects.select_related("document")
        .filter(document__site_id=site_id, state__in=("draft", "submitted"))
        .exclude(document__kind=TRANSFER_DOC_KIND)
        .exclude(document__purpose="count")
    )
    frozen = _document_brands([head.document_id for head in heads])
    for head in heads:
        add(head.document, frozen.get(head.document_id))
    for booking in GoodsBooking.objects.select_related("document").filter(
        document__site_id=site_id, closed_at__isnull=True
    ):
        add(booking.document, booking.brand_id)
    # A transfer is open until it completes or is cancelled whole. Cancelling
    # only the undispatched balance leaves a movement with shipments still to
    # account for, so a "cancelled" event alone does not close it (13A).
    transfers = (
        GoodsTransfer.objects.select_related("document")
        .filter(Q(source_site_id=site_id) | Q(destination_site_id=site_id))
        .exclude(state__in=(GoodsTransfer.State.COMPLETED, GoodsTransfer.State.CANCELLED))
        .exclude(document__head__state="reversed")
    )
    counts = GoodsStocktake.objects.select_related("document").filter(
        site_id=site_id, state__in=("requested", "open", "review")
    )
    # An approved RTV stays open while any of it waits to leave (goods ticket 15B)
    # or a shipment of it is not yet accounted for by the vendor's acknowledgement
    # or a return to source (15F); a draft or submitted one is already an
    # unfinished document above.
    returns = GoodsMovement.objects.select_related("document").filter(
        document__site_id=site_id, rtv_state="initiated"
    )
    for identity in [
        *(row.document for row in transfers),
        *(row.document for row in counts),
        *(row.document for row in returns),
    ]:
        add(identity, None)
    return sorted(found.values(), key=lambda d: (d["kind"], d["id"]))


def _open_sessions(scope: _Scope) -> list[dict[str, Any]]:
    """Open counts, arrivals awaiting count, acceptances, shipments being scanned for
    dispatch, unapproved opening manifests and pending approvals."""
    from approvals.goods_models import ApprovalRequest
    from inbound.goods_models import ArrivalHead, CountSession
    from ptmapper.goods_models import OpeningManifest
    from stockledger.goods_models import AcceptanceSession

    site_id = scope.sbu.site_id
    sessions = list(
        CountSession.objects.select_related("arrival").filter(
            arrival__site_id=site_id, state="open"
        )
    )
    rows = [_record(session.pk, "COUNT_SESSION", session.arrival.brand_id) for session in sessions]
    # An arrival no GRN has come from yet is still awaiting its count (as the
    # receiving queue reads it). One already shown through its open count session
    # is not listed twice.
    counting = {session.arrival_id for session in sessions}
    rows += [
        _record(arrival_id, "ARRIVAL", brand)
        for arrival_id, brand in ArrivalHead.objects.filter(
            arrival__site_id=site_id, grn_count=0
        ).values_list("arrival_id", "arrival__brand_id")
        if arrival_id not in counting
    ]
    rows += [
        _record(pk, "ACCEPTANCE_SESSION")
        for pk in AcceptanceSession.objects.filter(site_id=site_id, state="open").values_list(
            "pk", flat=True
        )
    ]
    # A shipment being scanned at the sending site is unfinished work there
    # (goods ticket 13B), even though it moves nothing until it is dispatched.
    from outbound.goods_models import DispatchPreparation

    rows += [
        _record(pk, "DISPATCH_PREPARATION")
        for pk in DispatchPreparation.objects.filter(
            transfer__source_site_id=site_id, state="open"
        ).values_list("pk", flat=True)
    ]
    rows += [
        _record(pk, "OPENING_MANIFEST")
        for pk in OpeningManifest.objects.filter(
            site_id=site_id, approved_version__isnull=True, current_version__isnull=False
        ).values_list("pk", flat=True)
    ]
    rows += [
        _record(pk, "APPROVAL_REQUEST", brand)
        for pk, brand in ApprovalRequest.objects.filter(
            site_id=site_id, state="pending"
        ).values_list("pk", "brand_id")
    ]
    owned = [row for row in rows if scope.may_own(_as_brand(row["brand_id"]))]
    return sorted(owned, key=lambda d: (d["kind"], d["id"]))


def _open_documents(scope: _Scope) -> list[dict[str, Any]]:
    """Open documents at the site that refer, or may refer, to this SBU."""
    return _numbered_documents(scope) + _open_sessions(scope)


def _document_brands(document_ids: list[Any]) -> dict[Any, int]:
    """The brand each document freezes, where its family carries one (booking, GRN,
    counter-GRN), read in three queries for the whole set."""
    from inbound.goods_models import CounterGrnDraft, GoodsGrn
    from vendors.goods_models import GoodsBooking

    brands: dict[Any, int] = {}
    if not document_ids:
        return brands
    for reader in (
        GoodsBooking.objects.filter(document_id__in=document_ids).values_list(
            "document_id", "brand_id"
        ),
        GoodsGrn.objects.filter(document_id__in=document_ids).values_list(
            "document_id", "arrival__brand_id"
        ),
        CounterGrnDraft.objects.filter(document_id__in=document_ids).values_list(
            "document_id", "grn__arrival__brand_id"
        ),
    ):
        for document_id, brand in reader:
            brands.setdefault(document_id, int(brand))
    return brands


def sbu_residuals(sbu: Sbu) -> dict[str, Any]:
    """Everything that still refers to ``sbu``: quantities (or null if unknowable),
    open documents, unresolved exceptions, and the completeness of the answer."""
    from alerts.goods_models import GoodsException

    scope = _Scope(sbu)
    quantities, reasons = _stock(scope)
    unknown = [key for key in SBU_QUANTITIES if quantities[key] is None]
    if not unknown:
        completeness = "complete"
    elif len(unknown) == len(SBU_QUANTITIES):
        completeness = "unknown"
    else:
        completeness = "partial"
    exceptions = sorted(
        (
            {"id": str(pk), "kind": kind}
            for pk, kind in GoodsException.objects.filter(
                site_id=sbu.site_id, state="open"
            ).values_list("pk", "kind")
        ),
        key=lambda e: e["id"],
    )
    return {
        **quantities,
        "open_documents": _open_documents(scope),
        "open_exceptions": exceptions,
        "completeness": completeness,
        "reasons": reasons,
    }


def retirement_blockers(residuals: dict[str, Any]) -> list[dict[str, Any]]:
    """One ``Issue`` per residual that blocks retirement. Empty means it may retire."""
    blockers: list[dict[str, Any]] = []
    for key in SBU_QUANTITIES:
        value = residuals[key]
        if value is None:
            blockers.append(
                issue(
                    "RESIDUAL_UNKNOWN",
                    f"The {_LABELS[key]} cannot be measured, so they cannot be shown to be none.",
                    field=key,
                )
            )
        elif value:
            blockers.append(
                issue(
                    "RESIDUAL_STOCK",
                    f"{value} {_LABELS[key]}.",
                    field=key,
                    quantity=value,
                )
            )
    for document in residuals["open_documents"]:
        label = document["number"] or document["kind"].replace("_", " ").lower()
        blockers.append(
            issue(
                "OPEN_DOCUMENT",
                f"Open {document['kind'].replace('_', ' ').lower()}: {label}.",
                field=document["id"],
            )
        )
    for exception in residuals["open_exceptions"]:
        blockers.append(
            issue(
                "OPEN_EXCEPTION",
                f"Unresolved exception at this site: {exception['kind'].replace('_', ' ')}.",
                field=exception["id"],
            )
        )
    return blockers + list(residuals["reasons"])


# ---------------------------------------------------------------------------
# After retirement: scoped grants end, new brand work is refused
# ---------------------------------------------------------------------------


def lock_grant_holders(run: CommandRun, sbu_id: uuid.UUID) -> None:
    """Serialise on the security guard of everyone holding a grant scoped to the SBU.

    Every access change locks the person's guard (``lock_person``); the SBU
    retirement takes the same locks, at their lower rank, before the site guard.
    """
    from accounts.goods_models import RoleGrant, SecurityGuard
    from core.commands import LockRank

    humans = set(
        RoleGrant.objects.filter(tenant_id=run.tenant_id, sbu_id=sbu_id).values_list(
            "human_id", flat=True
        )
    )
    for human_id in humans:
        SecurityGuard.objects.get_or_create(tenant_id=run.tenant_id, human_id=human_id)
    run.lock(
        LockRank.SECURITY,
        SecurityGuard.objects.filter(tenant_id=run.tenant_id, human_id__in=humans),
    )


def end_sbu_grants(run: CommandRun, sbu: Sbu) -> list[str]:
    """End every live grant scoped to ``sbu`` at ``run.now``; returns their ids.

    Change PRD rule P5 (as for a retiring person): a current grant closes now and a
    future one never takes effect - retirement does not wait on them. Ending uses
    the ordinary grant revocation, and each holder's security epoch moves so their
    sessions and step-ups end with the grant.
    """
    from accounts.goods_admin_services import revoke_grant, unrevoked_grants
    from accounts.sessions import bump_security_epoch
    from masters.goods_models import EffectiveVersionPeriod

    grants = unrevoked_grants(run.tenant_id, sbu_id=sbu.pk)
    periods = {
        period.target_id: period
        for period in EffectiveVersionPeriod.objects.filter(
            tenant_id=run.tenant_id, target_kind="grant", target_id__in=[g.pk for g in grants]
        )
    }
    ended: list[str] = []
    holders: set[uuid.UUID] = set()
    for grant in grants:
        period = periods.get(grant.pk)
        ends = [
            end
            for end in (grant.effective_to, period.effective_to if period else None)
            if end is not None
        ]
        if ends and min(ends) <= run.now:
            continue  # already over; nothing to end
        revoke_grant(run, grant, run.now)
        ended.append(str(grant.pk))
        holders.add(grant.human_id)
    for human_id in sorted(holders, key=str):
        bump_security_epoch(human_id, run.tenant_id)
    return ended


def require_active_sbu(site_id: int, brand_id: int | None, moment: datetime) -> None:
    """Refuse new work binding ``brand_id`` to ``site_id`` once its SBU is retired.

    Only a site x brand SBU that exists and was retired at ``moment`` refuses; a
    site with no SBU row for the brand keeps its current behaviour, and work with
    no brand (unknown or unidentified goods) is never refused here.
    """
    if brand_id is None:
        return
    if Sbu.objects.filter(
        site_id=site_id, brand_id=brand_id, retired_at__isnull=False, retired_at__lte=moment
    ).exists():
        raise Refusal(
            "SBU_RETIRED",
            "This brand's business unit at this site has been retired, so no new work "
            "can be started for it here.",
            status=409,
            issues=[
                issue(
                    "SBU_RETIRED",
                    "The business unit for this site and brand is retired.",
                    field="brand_id",
                )
            ],
        )

"""Goods-v1 receiving: arrival, durable count, quantity-only GRN and its corrections.

Design §3.2 (custody lots), §5.2/§5.3/§5.6 (arrival, observations, GRN, counter-GRN,
dispositions, arrival decisions), §6.1 and the posting catalogue §7.2 P01-P03.

What physically arrived is recorded before anyone knows its identity or value. A
GRN is issued once from durable observations and opens one unvalued custody lot
per line at the site where the goods actually are. Counts are never edited: a
counter-GRN appends a linked lot or removes uncovered portions, and a disposition
decides what happens to discrepant quantity without inventing a SKU or a cost.

The PT slice reads custody through ``grn_custody`` and ``effective_count``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from django.apps import apps
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from accounts.goods_models import HumanIdentity
from accounts.principal import AccessContext, effective_grants
from alerts.goods_models import GoodsException
from alerts.goods_services import open_exception, resolve_exceptions
from approvals.goods_models import ActionDraft, ApprovalRequest
from approvals.goods_policy import BAND_MESSAGES, Amounts, band_failure, pin
from approvals.goods_services import DecisionContext, create_request, register_subject_handler
from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.goods_documents import (
    append_revision,
    lock_heads,
    new_document,
    officialise,
    record_event,
    revision_lines,
)
from core.goods_fields import bounds, portion
from core.kernel_models import DocumentHead, DocumentIdentity, OfficialLine
from core.numbering import allocate
from core.operational import ValuePair
from core.refusals import Refusal, issue
from core.tenancy import require_tenant_id
from files.goods_models import EvidenceObject
from files.goods_services import covers_cell
from inbound import debit_notes, sor_records, three_way_match
from inbound import goods_input as inp
from inbound.goods_models import (
    Arrival,
    ArrivalDecision,
    ArrivalHead,
    CounterGrnDraft,
    CountHandover,
    CountSession,
    Disposition,
    DuplicateArrivalAcknowledgement,
    GoodsGrn,
    InvoiceClaimVersion,
    ScanObservation,
)
from inbound.sor_models import BrandDispatchDate
from masters.goods_config import ConfigTarget, resolve
from masters.goods_identity_models import IdentityPick, ProductSku
from masters.goods_identity_services import profile_context, resolve_alias
from masters.goods_models import ConfigVersion, Location, SiteGuard
from masters.goods_sbu import require_active_sbu
from masters.models import Brand, LegalEntity, Store
from masters.store_feature_registry import SOR_AGEING
from masters.store_features import is_feature_on, require_feature
from stockledger import ranges
from stockledger.goods_engine import (
    Address,
    Plan,
    change_address,
    end_positions,
    event_key,
    lock_lots,
    open_lot,
    place_hold,
    post,
    release_hold,
    system_location,
)
from stockledger.goods_models import (
    ActiveHold,
    CustodyLot,
    LiveCoverage,
    LiveValueBasis,
    Origin,
    Position,
)
from vendors.goods_models import BookingReceiptLink, GoodsBooking
from vendors.models import Vendor

CONDITIONS = ("good", "damaged", "wrong", "unidentified")
RECEIVE_ACTION = "receive.arrival"
COUNTER_ACTION = "receipt.counter_grn.approve"
DISPOSITION_ACTION = "receipt.disposition.decide"
NO_BOOKING_ACTION = "arrival.no_booking.confirm"

#: Grants that may read arrivals, GRNs and the inbound queue at a site.
READ_ACTIONS = (
    RECEIVE_ACTION,
    "pt.prepare",
    "pt.view",
    "stock.view",
    COUNTER_ACTION,
    DISPOSITION_ACTION,
    "booking.manage",
    NO_BOOKING_ACTION,
    "exception.view",
)

#: ``hold_damage`` is not among them any more (ticket 05C): damage found while
#: receiving is *reported*, on its own route, and joins the common damage review
#: (``report_receipt_damage``). A disposition decides what happens to disputed
#: goods; whether goods are damaged is not a disposition.
DISPOSITION_KINDS = (
    "accept_shortage",
    "hold_excess",
    "accept_excess",
    "value_damage",
    "return",
    "dispose",
    "resolve_identity",
    "accept_wrong",
)
#: Default policy (Phase 1 §9.3): these decisions need a second person.
CHECKER_KINDS = frozenset({"return", "dispose", "accept_excess", "value_damage", "accept_wrong"})
#: Decisions a second person must approve whatever a policy says: ``value_damage``
#: (overall PRD §15.2.1 rule 4) and accepting wrong or unidentified goods (rule 2,
#: CH-2026-09-24-01: "a different authorised person approves their acceptance").
ALWAYS_CHECKED_KINDS = frozenset({"value_damage", "accept_wrong"})
#: Decisions that settle a unit for good; a later decision never takes the same unit.
COVERING_KINDS = frozenset({"accept_excess", "value_damage", "resolve_identity", "accept_wrong"})
#: Conditions whose pieces ``accept_wrong`` may accept once their identity is resolved.
WRONG_CONDITIONS = ("wrong", "unidentified")
#: Who may *ask* for a decision on E119. Deciding one - directly, or as its
#: approver - stays ``receipt.disposition.decide``. Accepting resolved wrong or
#: unidentified goods is prepared by the site (change PRD §14.10 GSA-R01:
#: Warehouse and Store person prepare at their own site with the common
#: ``movement.draft`` grant, as a damage report is) and always approved by a
#: different person, so asking for it decides nothing. No role gains or loses a grant.
DISPOSITION_REQUEST_ACTIONS: dict[str, tuple[str, ...]] = {
    "accept_wrong": (DISPOSITION_ACTION, "movement.draft"),
}
#: Decisions that settle counted excess: held, accepted, or gone back to the vendor.
EXCESS_DECISIONS = frozenset({"hold_excess", "accept_excess", "return", "dispose"})
#: Excess decisions after which a piece no longer needs the excess hold.
SETTLING_EXCESS = EXCESS_DECISIONS - {"hold_excess"}
#: The hold every undecided excess piece stays under (``sync_excess_holds``).
EXCESS_HOLD = "receipt_excess"
#: The hold a receipt's damaged pieces stand under, whether the count found the
#: damage or somebody found it on the goods before their PT (ticket 05C).
DAMAGE_HOLD = "receipt_damage"
#: Every hold kind that means "these pieces are damaged": the receipt's own, and
#: the one the stock screen's mark-damaged and a transfer arrival place.
DAMAGE_HOLD_KINDS = (DAMAGE_HOLD, "damage")
#: Who may report damage on a GRN's goods: the common reporting grant (design,
#: "Damage report and review") or the receiving decision grant that held damage
#: here before ticket 05C. No role gains or loses a grant.
DAMAGE_REPORT_ACTIONS = ("movement.draft", DISPOSITION_ACTION)
#: The reason written on a report the GRN itself opens for pieces counted damaged.
COUNTED_DAMAGE_REASON = "COUNTED_DAMAGED"


# ---------------------------------------------------------------------------
# Scope and site guards
# ---------------------------------------------------------------------------


def can_read(access: AccessContext, site_id: int | None, brand_id: int | None = None) -> bool:
    return any(access.can(a, site_id=site_id, brand_id=brand_id) for a in READ_ACTIONS)


def can_read_site(access: AccessContext, site_id: int) -> bool:
    """A site filter on a receiving list: the site itself, whatever brand the grant reaches."""
    return any(access.can_reach_site(a, site_id) for a in READ_ACTIONS)


def require_read_action(access: AccessContext) -> None:
    """A receiving list: permission to read somewhere. Each row is still checked on its own."""
    if not any(access.holds(a) for a in READ_ACTIONS):
        raise Refusal("ACTION_DENIED", "You do not have permission to read receiving records.")


def require_read(access: AccessContext, site_id: int | None, brand_id: int | None = None) -> None:
    if not any(a in access.all_actions() for a in READ_ACTIONS):
        raise Refusal("ACTION_DENIED", "You do not have permission to read receiving records.")
    if not can_read(access, site_id, brand_id):
        raise Refusal("NOT_FOUND", "That record was not found.")


def require_goods_site(run: CommandRun, site_id: int, *, physical: bool = True) -> SiteGuard:
    """Lock the site guard (rank SITE) and check the goods-v1 fence, capability and freeze."""
    guards = run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=site_id))
    guard: SiteGuard | None = guards[0] if guards else None
    if guard is None or guard.stock_contract != SiteGuard.StockContract.GOODS_V1:
        raise Refusal("CONTRACT_DISABLED", "This site does not receive goods through goods-v1.")
    if not guard.goods_ready or guard.lifecycle in ("planned", "closing", "closed"):
        raise Refusal("SITE_NOT_READY", "This site is not approved to receive goods.")
    if physical and guard.freeze_id:
        raise Refusal("UNDER_COUNT", "A stock count has frozen this site.")
    return guard


def _check_not_frozen(site_id: int) -> None:
    guard = SiteGuard.objects.filter(site_id=site_id).first()
    if guard is not None and guard.freeze_id:
        raise Refusal("UNDER_COUNT", "A stock count has frozen this site.")


def person_can_receive(tenant_id: uuid.UUID, human_id: uuid.UUID, site_id: int) -> bool:
    human = HumanIdentity.objects.filter(pk=human_id, active=True).first()
    if human is None:
        return False
    grants = [g for g in effective_grants(human.pk) if RECEIVE_ACTION in g.actions]
    context = AccessContext(
        user=None, human_id=human.pk, tenant_id=tenant_id, session=None, grants=grants
    )
    return context.can_reach_site(RECEIVE_ACTION, site_id)


def _evidence_missing(ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    wanted = {i for i in ids if i is not None}
    found = set(EvidenceObject.objects.filter(pk__in=list(wanted)).values_list("pk", flat=True))
    return sorted(wanted - found, key=str)


def _subject_uuid(subject_key: str, prefix: str) -> uuid.UUID:
    head, _, tail = subject_key.partition(":")
    if head != prefix:
        raise Refusal("NOT_FOUND", "That approval subject was not found.")
    return inp.uuid_value(tail, "subject_key")


# ---------------------------------------------------------------------------
# Arrival (E113, E095, E236)
# ---------------------------------------------------------------------------

ARRIVAL_KEYS = frozenset(
    {
        "site_id",
        "vendor_id",
        "brand_id",
        "subbrand_key",
        "actual_arrival_at",
        "transporter_ref",
        "booking_id",
        "invoice_number",
        "invoice_date",
        "invoice_evidence_id",
        # The answer to a duplicate-invoice warning, not a way past it: the exact
        # warning being answered, and why the goods in front of the recorder are
        # a further arrival rather than the same one entered twice (design §5.8).
        "duplicate_warning_hash",
        "duplicate_reason",
        # The brand's dispatch date, from its challan or invoice (store operations
        # ticket 24): SOR stock ages from it. Taken only where SOR ageing is on.
        "brand_dispatch_date",
    }
)


@dataclass(frozen=True)
class ArrivalInput:
    site_id: int
    vendor_id: int
    brand_id: int
    subbrand_key: str | None
    actual_arrival_at: datetime
    transporter_ref: str | None
    booking_id: uuid.UUID | None
    invoice_number: str | None
    invoice_date: date | None
    invoice_evidence_id: uuid.UUID | None
    duplicate_warning_hash: str | None = None
    duplicate_reason: str | None = None
    brand_dispatch_date: date | None = None


def parse_arrival(body: dict[str, Any]) -> ArrivalInput:
    invoice_number = inp.text(body.get("invoice_number"), "invoice_number", 80)
    warning_hash = inp.text(body.get("duplicate_warning_hash"), "duplicate_warning_hash", 64)
    if warning_hash is None and body.get("duplicate_reason") is not None:
        # The two travel together or not at all. A reason on its own would be
        # kept against nothing, or - worse - quietly dropped, leaving somebody
        # believing they had explained themselves.
        raise inp.bad(
            "A reason answers a duplicate warning; send the warning it answers.",
            "duplicate_warning_hash",
            "REQUIRED",
        )
    return ArrivalInput(
        site_id=inp.legacy_id(body.get("site_id"), "site_id"),
        vendor_id=inp.legacy_id(body.get("vendor_id"), "vendor_id"),
        brand_id=inp.legacy_id(body.get("brand_id"), "brand_id"),
        subbrand_key=inp.text(body.get("subbrand_key"), "subbrand_key", 100),
        actual_arrival_at=inp.timestamp(body.get("actual_arrival_at"), "actual_arrival_at"),
        transporter_ref=inp.text(body.get("transporter_ref"), "transporter_ref", 160),
        booking_id=inp.optional_uuid(body.get("booking_id"), "booking_id"),
        # Stored as it is matched on: an invoice number typed with a stray space
        # is the same invoice number, and two arrivals that differ only by that
        # space must not read as two different invoices to the warning below.
        invoice_number=None if invoice_number is None else invoice_number.strip(),
        invoice_date=inp.optional_day(body.get("invoice_date"), "invoice_date"),
        invoice_evidence_id=inp.optional_uuid(
            body.get("invoice_evidence_id"), "invoice_evidence_id"
        ),
        duplicate_warning_hash=warning_hash,
        duplicate_reason=inp.text(body.get("duplicate_reason"), "duplicate_reason", 500),
        brand_dispatch_date=inp.optional_day(
            body.get("brand_dispatch_date"), "brand_dispatch_date"
        ),
    )


def booking_is_open(booking: GoodsBooking) -> bool:
    head = DocumentHead.objects.filter(document_id=booking.document_id).first()
    return bool(head and head.live_version_id) and booking.closed_at is None


#: A same-vendor, same-invoice arrival already recorded in the receiving legal
#: entity, as the person recording the next one is allowed to see it.
@dataclass(frozen=True)
class DuplicateCandidate:
    arrival: Arrival
    state: str


@dataclass(frozen=True)
class DuplicateWarning:
    """What the server found, and the hash that pins exactly this finding.

    ``warning_hash`` is opaque to the caller and covers the matching question -
    entity, vendor, invoice number - together with the records that answered it.
    Acknowledging a warning therefore acknowledges those records: one more
    matching arrival appears and the hash changes, so a reason given against the
    old set cannot carry the new one through (design §5.8).
    """

    entity_id: int
    vendor_id: int
    invoice_number: str
    candidates: list[DuplicateCandidate]

    @property
    def hash(self) -> str:
        return content_hash(
            {
                "kind": "duplicate_arrival_warning",
                "entity_id": self.entity_id,
                "vendor_id": self.vendor_id,
                "invoice_number": self.invoice_number,
                "arrival_ids": sorted(str(row.arrival.pk) for row in self.candidates),
            }
        )


def entity_of_site(site_id: int) -> int | None:
    """The legal entity that receives at this site; its GSTIN's company."""
    return Store.objects.filter(pk=site_id).values_list("gstin__legal_entity_id", flat=True).first()


def match_key(invoice_number: str | None) -> str:
    """The form two invoice numbers are compared in: no case, no outer space.

    Two people typing the same number off the same piece of paper produce
    "GJ/1207" and "gj/1207 ", and a warning that does not fire for that is no
    warning at all.

    ``INVOICE_MATCH`` is the same rule in SQL, and ``ix_arrival_invoice_match``
    is the index over it, so the comparison happens in the database rather than
    over every arrival this vendor ever sent.
    """
    return (invoice_number or "").strip().casefold()


#: The stored invoice number in ``match_key`` form. Arrivals are written with
#: the number already stripped, so ``Trim`` only reaches rows recorded before
#: that; ``Lower`` and ``casefold`` part company only outside the Latin
#: alphabet, which an invoice number is not.
INVOICE_MATCH = Lower(Trim("invoice_number"))


def duplicate_warning(
    access: AccessContext,
    *,
    site_id: int,
    vendor_id: int,
    invoice_number: str | None,
) -> DuplicateWarning | None:
    """Arrivals already carrying this vendor and invoice in this legal entity.

    Two limits, both deliberate. The search stops at the receiving legal entity,
    because the same invoice number under another company is another company's
    invoice. And each candidate is checked against the caller's own read scope,
    so a warning can only ever name a record they could open for themselves - a
    match they may not see is not reported, not counted and not hinted at, which
    is why this returns nothing at all rather than "some others exist"
    (GSA-T05).
    """
    key = match_key(invoice_number)
    if not key:
        return None
    entity_id = entity_of_site(site_id)
    if entity_id is None:
        return None
    rows = list(
        Arrival.objects.annotate(invoice_match=INVOICE_MATCH)
        .filter(
            vendor_id=vendor_id,
            site__gstin__legal_entity_id=entity_id,
            invoice_match=key,
        )
        .order_by("-recorded_at", "id")
    )
    heads = {
        head.arrival_id: head
        for head in ArrivalHead.objects.filter(arrival_id__in=[row.pk for row in rows])
    }
    candidates = [
        DuplicateCandidate(arrival=row, state=arrival_state(row, heads[row.pk]))
        for row in rows
        if row.pk in heads and can_read(access, row.site_id, row.brand_id)
    ]
    if not candidates:
        return None
    return DuplicateWarning(
        entity_id=entity_id,
        vendor_id=vendor_id,
        invoice_number=key,
        candidates=candidates,
    )


def check_duplicate_invoice(
    run: CommandRun, data: ArrivalInput, *, access: AccessContext
) -> DuplicateWarning | None:
    """Enforce the duplicate-invoice warning and return the one being answered.

    The warning is evaluated from the arrival being recorded, never from the
    request's command identity: a second attempt under a fresh command key is a
    second arrival and hears the same warning, while the identical retry of one
    already recorded never reaches here at all - the command kernel replays its
    stored outcome. Business warning and command replay stay two different
    things (design §5.8).

    Two people recording the same invoice at the same moment would each look,
    each find nothing and each record - so this vendor and invoice are locked
    for the rest of the command, and the second one looks after the first has
    finished. The rank is SITE, the rank ``require_goods_site`` has already
    claimed, so nothing is taken out of order.
    """
    key = match_key(data.invoice_number)
    entity_id = entity_of_site(data.site_id)
    if key and entity_id is not None:
        run.advisory_lock(LockRank.SITE, [f"arrival-invoice:{entity_id}:{data.vendor_id}:{key}"])
    warning = duplicate_warning(
        access,
        site_id=data.site_id,
        vendor_id=data.vendor_id,
        invoice_number=data.invoice_number,
    )
    current = warning.hash if warning is not None else None
    given = data.duplicate_warning_hash
    reason = (data.duplicate_reason or "").strip()
    if current is None and given is None:
        return None
    if given != current:
        # Either nothing was answered and there is a warning to answer, or what
        # was answered is no longer what the server finds - another matching
        # arrival since, or a changed vendor or invoice number. Both end the
        # same way: read the warning that exists now.
        raise Refusal(
            "DUPLICATE_ARRIVAL",
            (
                "This vendor and invoice are already recorded here. Read the warning "
                "before recording another arrival against them."
                if current is not None
                else "The duplicate warning you answered is not the one this arrival raises."
            ),
            issues=[
                issue(
                    "WARNING_SUPERSEDED" if given is not None else "DUPLICATE_INVOICE",
                    "Read the current warning and answer that one.",
                    field="duplicate_warning_hash",
                )
            ],
        )
    if not reason:
        raise Refusal(
            "DUPLICATE_ARRIVAL",
            "Say why this is a further arrival for the same invoice, such as the "
            "remaining cartons.",
            issues=[
                issue(
                    "REASON_REQUIRED",
                    "A reason is required to record another arrival for this invoice.",
                    field="duplicate_reason",
                )
            ],
        )
    return warning


def duplicate_acknowledgement_data(arrival_id: uuid.UUID) -> dict[str, Any] | None:
    """The answer given to this arrival's duplicate warning, for whoever may read it.

    The matched arrivals stay on the evidence row and out of this answer, and so
    does how many there were. They were the records the *recorder* could see; a
    later reader of this arrival may reach fewer sites, so both the list and its
    length would tell them about records they cannot open - exactly the
    disclosure the warning itself avoids (GSA-T05).
    """
    row = DuplicateArrivalAcknowledgement.objects.filter(arrival_id=arrival_id).first()
    if row is None:
        return None
    return {
        "warning_hash": row.warning_hash,
        "reason": row.reason,
        "actor_id": str(row.actor_id) if row.actor_id else None,
        "recorded_at": row.recorded_at.isoformat(),
    }


def record_arrival(run: CommandRun, data: ArrivalInput, *, access: AccessContext) -> Arrival:
    from vendors.goods_services import booking_readable

    require_goods_site(run, data.site_id)
    problems: list[dict[str, Any]] = []
    if data.actual_arrival_at > run.now:
        problems.append(
            issue(
                "AFTER_RECORDING",
                "The arrival time cannot be later than the moment it is recorded.",
                field="actual_arrival_at",
            )
        )
    vendor = Vendor.objects.filter(pk=data.vendor_id).first()
    if vendor is None or not vendor.is_active:
        problems.append(issue("UNKNOWN_VENDOR", "No active vendor has that ID.", field="vendor_id"))
    if not Brand.objects.filter(pk=data.brand_id, is_active=True).exists():
        problems.append(issue("UNKNOWN_BRAND", "No active brand has that ID.", field="brand_id"))
    booking: GoodsBooking | None = None
    if data.booking_id is not None:
        booking = (
            GoodsBooking.objects.select_related("document")
            .filter(document_id=data.booking_id)
            .first()
        )
        # A booking the caller may not read - another site's - or one with no site
        # yet (GSA-T05: received against only once a destination is named, whoever
        # the caller is) is refused exactly like one that does not exist, whatever
        # an older picker option offered (ticket 02D).
        if (
            booking is None
            or booking.document.site_id is None
            or not booking_readable(access, booking)
            or not booking_is_open(booking)
            or booking.vendor_id != data.vendor_id
            or booking.brand_id != data.brand_id
        ):
            problems.append(
                issue(
                    "BOOKING_MISMATCH",
                    "The booking must be a confirmed, open booking for this vendor and brand.",
                    field="booking_id",
                )
            )
    if data.invoice_evidence_id and _evidence_missing([data.invoice_evidence_id]):
        problems.append(
            issue(
                "UNKNOWN_EVIDENCE", "The invoice file was not found.", field="invoice_evidence_id"
            )
        )
    if data.brand_dispatch_date is not None:
        # Asked for only where SOR ageing is on (ticket 24); sent anyway, it is
        # refused rather than quietly dropped.
        require_feature(data.site_id, SOR_AGEING)
        problems.extend(
            sor_records.dispatch_problems(
                data.brand_dispatch_date,
                timezone.localtime(data.actual_arrival_at).date(),
                timezone.localdate(run.now),
            )
        )
    if problems:
        raise Refusal(
            "ARRIVAL_INVALID",
            "The arrival cannot be recorded as entered.",
            status=422,
            issues=problems,
        )
    require_active_sbu(data.site_id, data.brand_id, run.now)
    warning = check_duplicate_invoice(run, data, access=access)
    arrival = Arrival(
        site_id=data.site_id,
        vendor_id=data.vendor_id,
        brand_id=data.brand_id,
        subbrand_key=data.subbrand_key,
        actual_arrival_at=data.actual_arrival_at,
        transporter_ref=data.transporter_ref,
        booking_id=booking.pk if booking is not None else None,
        invoice_evidence_id=data.invoice_evidence_id,
        invoice_number=data.invoice_number,
        invoice_date=data.invoice_date,
    )
    run.record(arrival)
    ArrivalHead.objects.create(tenant_id=run.tenant_id, arrival_id=arrival.pk)
    if warning is not None:
        run.record(
            DuplicateArrivalAcknowledgement(
                arrival_id=arrival.pk,
                warning_hash=warning.hash,
                reason=str(data.duplicate_reason).strip(),
                matched_arrivals=sorted(str(row.arrival.pk) for row in warning.candidates),
            )
        )
    if booking is None:
        open_exception(
            run,
            kind="unbooked_arrival",
            site_id=data.site_id,
            subject_key=f"arrival:{arrival.pk}",
            reason_code="NO_BOOKING",
            source_event_key=event_key("unbooked_arrival", arrival.pk),
            allowed_resolution_actions=[
                "bookings/{id}/receipt-links",
                "inbound/arrivals/{id}/no-booking",
            ],
        )
    run.audit_after = {"arrival_id": str(arrival.pk), "site_id": str(data.site_id)}
    if data.brand_dispatch_date is not None:
        run.record(BrandDispatchDate(arrival_id=arrival.pk, dispatch_date=data.brand_dispatch_date))
        run.audit_after["brand_dispatch_date"] = data.brand_dispatch_date.isoformat()
    if warning is not None:
        # The reason is audited beside the arrival it let through, so why a
        # second arrival exists for one invoice is readable without opening the
        # receiving tables (design §5.8).
        run.audit_after["duplicate_warning_hash"] = warning.hash
        run.audit_after["duplicate_reason"] = str(data.duplicate_reason).strip()
    return arrival


def lock_arrival_head(run: CommandRun, arrival_id: uuid.UUID) -> ArrivalHead:
    rows = run.lock(LockRank.DOCUMENT, ArrivalHead.objects.filter(arrival_id=arrival_id))
    if not rows:
        raise Refusal("NOT_FOUND", "That arrival was not found.")
    head: ArrivalHead = rows[0]
    return head


def arrival_data(arrival: Arrival) -> dict[str, Any]:
    booking_document = (
        GoodsBooking.objects.filter(pk=arrival.booking_id)
        .values_list("document_id", flat=True)
        .first()
        if arrival.booking_id
        else None
    )
    return {
        "site_id": str(arrival.site_id),
        "vendor_id": str(arrival.vendor_id),
        "brand_id": str(arrival.brand_id),
        "subbrand_key": arrival.subbrand_key,
        "actual_arrival_at": arrival.actual_arrival_at.isoformat(),
        "transporter_ref": arrival.transporter_ref,
        "booking_id": str(booking_document) if booking_document else None,
        "invoice_number": arrival.invoice_number,
        "invoice_date": arrival.invoice_date.isoformat() if arrival.invoice_date else None,
        "invoice_evidence_id": str(arrival.invoice_evidence_id)
        if arrival.invoice_evidence_id
        else None,
        # Present only on an arrival recorded against a duplicate-invoice
        # warning: who said carry on, when, which warning and why. It is read
        # by whoever may read the arrival, and names only records the person
        # who answered the warning could already see.
        "duplicate_acknowledgement": duplicate_acknowledgement_data(arrival.pk),
    }


def latest_claim(arrival_id: uuid.UUID) -> InvoiceClaimVersion | None:
    return InvoiceClaimVersion.objects.filter(arrival_id=arrival_id).order_by("-revision").first()


def arrival_state(arrival: Arrival, head: ArrivalHead) -> str:
    return "awaiting_count" if head.grn_count == 0 else "counted"


def arrival_hash(arrival: Arrival, head: ArrivalHead) -> str:
    claim = latest_claim(arrival.pk)
    decisions = sorted(
        str(pk)
        for pk in ArrivalDecision.objects.filter(arrival_id=arrival.pk).values_list("pk", flat=True)
    )
    return content_hash(
        {
            "arrival": arrival_data(arrival),
            "revision": head.revision,
            "claim": claim.content_hash if claim else None,
            "decisions": decisions,
        }
    )


def confirm_no_booking(
    run: CommandRun,
    arrival: Arrival,
    *,
    reason_code: str,
    evidence_id: uuid.UUID | None,
    expected_revision: int | None,
) -> ArrivalDecision:
    head = lock_arrival_head(run, arrival.pk)
    inp.check_revision(expected_revision, head.revision)
    if evidence_id is not None and _evidence_missing([evidence_id]):
        raise inp.bad("The evidence file was not found.", "evidence_id")
    grn_documents = list(
        GoodsGrn.objects.filter(arrival_id=arrival.pk).values_list("document_id", flat=True)
    )
    linked = BookingReceiptLink.objects.filter(
        grn_id__in=grn_documents, counter_of__isnull=True, counters__isnull=True
    ).exists()
    already = ArrivalDecision.objects.filter(
        arrival_id=arrival.pk, kind=ArrivalDecision.Kind.NO_BOOKING_CONFIRMED
    ).exists()
    if arrival.booking_id is not None or linked or already:
        raise Refusal(
            "BOOKING_LINK_INVALID",
            "This arrival already has a booking, a booking link or a no-booking confirmation.",
        )
    decision = ArrivalDecision(
        arrival_id=arrival.pk,
        kind=ArrivalDecision.Kind.NO_BOOKING_CONFIRMED,
        reason_code=reason_code,
        evidence_id=evidence_id,
    )
    run.record(decision)
    head.revision += 1
    head.save(update_fields=["revision"])
    resolve_exceptions(
        run,
        kind="unbooked_arrival",
        subject_key=f"arrival:{arrival.pk}",
        reason_code="NO_BOOKING_CONFIRMED",
    )
    return decision


# ---------------------------------------------------------------------------
# Invoice claims (E114)
# ---------------------------------------------------------------------------

CLAIM_LINE_KEYS = frozenset(
    {
        "line_key",
        "style_code",
        "sku_id",
        "size_value_id",
        "description",
        "claimed_qty",
        "invoice_basic_paise",
        "invoice_mrp_paise",
        "evidence_line_ref",
        "remark",
    }
)


def parse_claim_lines(value: Any) -> list[dict[str, Any]]:
    rows = inp.object_list(value, "lines")
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        field = f"lines[{index}]"
        line = inp.closed(
            raw, CLAIM_LINE_KEYS, field, required=["line_key", "description", "claimed_qty"]
        )
        sku = inp.optional_uuid(line.get("sku_id"), f"{field}.sku_id")
        out.append(
            {
                "line_key": str(inp.uuid_value(line["line_key"], f"{field}.line_key")),
                "style_code": inp.text(line.get("style_code"), f"{field}.style_code", 120),
                "sku_id": str(sku) if sku else None,
                "size_value_id": inp.vocabulary_id(
                    line.get("size_value_id"), f"{field}.size_value_id"
                ),
                "description": inp.plain_text(line["description"], f"{field}.description", 240),
                "claimed_qty": inp.whole(line["claimed_qty"], f"{field}.claimed_qty", 0, 999_999),
                "invoice_basic_paise": inp.money(
                    line.get("invoice_basic_paise"), f"{field}.invoice_basic_paise"
                ),
                "invoice_mrp_paise": inp.money(
                    line.get("invoice_mrp_paise"), f"{field}.invoice_mrp_paise"
                ),
                "evidence_line_ref": inp.text(
                    line.get("evidence_line_ref"), f"{field}.evidence_line_ref", 100
                ),
                "remark": inp.text(line.get("remark"), f"{field}.remark", 500),
            }
        )
    return out


def record_invoice_claim(
    run: CommandRun,
    arrival: Arrival,
    *,
    invoice_number: str,
    invoice_date: date,
    evidence_id: uuid.UUID | None,
    lines: list[dict[str, Any]],
    reason_code: str | None,
    expected_revision: int | None,
    sees_cost: bool = True,
) -> InvoiceClaimVersion:
    """Append a claim version. Where the three-way match is on at the site, the
    invoice cost is carried and guarded (:func:`keep_claim_costs`); ``sees_cost``
    says whether the caller holds the cost field there."""
    head = lock_arrival_head(run, arrival.pk)
    inp.check_revision(expected_revision, head.revision)
    if is_feature_on(arrival.site_id, three_way_match.FEATURE_KEY):
        lines = keep_claim_costs(lines, latest_claim(arrival.pk), sees_cost=sees_cost)
    problems: list[dict[str, Any]] = []
    keys = [line["line_key"] for line in lines]
    if len(set(keys)) != len(keys):
        problems.append(
            issue("DUPLICATE_LINE", "Each claim line needs its own line_key.", field="lines")
        )
    skus = {line["sku_id"] for line in lines if line["sku_id"]}
    known = {
        str(s) for s in ProductSku.objects.filter(pk__in=list(skus)).values_list("pk", flat=True)
    }
    for line in lines:
        if line["sku_id"] and line["sku_id"] not in known:
            problems.append(
                issue(
                    "UNKNOWN_SKU", "No SKU has that ID.", field="sku_id", line_key=line["line_key"]
                )
            )
        if not line["sku_id"] and not line["description"].strip() and not line["style_code"]:
            problems.append(
                issue(
                    "IDENTITY_REQUIRED",
                    "A claim line needs a SKU, a style code or a description.",
                    line_key=line["line_key"],
                )
            )
    if evidence_id is not None and _evidence_missing([evidence_id]):
        problems.append(
            issue("UNKNOWN_EVIDENCE", "The invoice file was not found.", field="evidence_id")
        )
    grn = (
        GoodsGrn.objects.select_related("document", "arrival").filter(arrival_id=arrival.pk).first()
    )
    if grn is not None and not problems:
        # E114 step 10: a claim that arrives after the count is compared with the issued
        # GRN at once, and every quantity difference needs a remark on its claim line.
        remark_of = {line["line_key"]: (line["remark"] or "").strip() for line in lines}
        comparisons, _matched = compare_claim(lines, current_grn_lines(grn.document_id))
        problems += [
            issue(
                "REMARK_REQUIRED",
                "Every difference between the invoice and the issued GRN needs a remark.",
                line_key=c["claim_line_key"],
                quantity=c["difference"],
            )
            for c in comparisons
            if c["difference"] != 0 and not remark_of[c["claim_line_key"]]
        ]
    if problems:
        raise Refusal(
            "INVOICE_INVALID", "The invoice claim cannot be recorded.", status=422, issues=problems
        )
    revision = InvoiceClaimVersion.objects.filter(arrival_id=arrival.pk).count() + 1
    digest = content_hash(
        {
            "invoice_number": invoice_number,
            "invoice_date": invoice_date.isoformat(),
            "evidence_id": str(evidence_id) if evidence_id else None,
            "reason_code": reason_code,
            "lines": lines,
        }
    )
    claim = InvoiceClaimVersion(
        arrival_id=arrival.pk,
        revision=revision,
        evidence_id=evidence_id,
        lines=lines,
        content_hash=digest,
    )
    run.record(claim)
    head.revision += 1
    head.save(update_fields=["revision"])
    if grn is not None:
        sync_excess_holds(run, grn, trigger=claim.pk, claim_lines=lines)
        refresh_receipt_discrepancy(
            run,
            grn,
            trigger=claim.pk,
            resolved_reason="CLAIM_RECOMPARED",
            reopen=True,
            claim_lines=lines,
        )
        # Last: it takes the GRN's advisory lock at the highest rank (ticket 37).
        three_way_match.refresh(run, grn, claim_lines=lines)
    run.audit_after = {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date.isoformat(),
        "claim_revision": revision,
        "reason_code": reason_code,
    }
    return claim


#: The invoice's own cost on a claim line: a login without the ``cost`` field never
#: receives it (store operations ticket 37, "store roles see quantities, never cost").
CLAIM_COST_KEYS = frozenset({"invoice_basic_paise"})


def claim_lines_for(lines: list[dict[str, Any]], *, show_cost: bool) -> list[dict[str, Any]]:
    """Claim lines as this reader may see them: without cost, the key is absent."""
    if show_cost:
        return list(lines)
    return [{k: v for k, v in line.items() if k not in CLAIM_COST_KEYS} for line in lines]


def keep_claim_costs(
    lines: list[dict[str, Any]], previous: InvoiceClaimVersion | None, *, sees_cost: bool
) -> list[dict[str, Any]]:
    """``lines`` with their invoice cost carried from the previous claim version.

    A new version never silently wipes a recorded cost: a line that gives none keeps
    the one recorded for the same item. A login that is never shown cost cannot set
    one either - whatever it sends, the line keeps the previous version's cost (or
    none). Carried only where the item was on exactly one earlier line and is on
    exactly one line now: two lines of one item (two rates) are never guessed.
    """
    before: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for line in previous.lines if previous is not None else []:
        before[claim_identity(line)].append(line)
    now: dict[tuple[str, str], int] = defaultdict(int)
    for line in lines:
        now[claim_identity(line)] += 1
    out = []
    for line in lines:
        identity = claim_identity(line)
        earlier = before.get(identity, [])
        kept = (
            earlier[0].get("invoice_basic_paise")
            if len(earlier) == 1 and now[identity] == 1
            else None
        )
        if not sees_cost:
            line = {**line, "invoice_basic_paise": kept}
        elif line.get("invoice_basic_paise") is None and kept is not None:
            line = {**line, "invoice_basic_paise": kept}
        out.append(line)
    return out


def claim_data(claim: InvoiceClaimVersion, *, show_cost: bool = True) -> dict[str, Any]:
    return {
        "arrival_id": str(claim.arrival_id),
        "revision": claim.revision,
        "evidence_id": str(claim.evidence_id) if claim.evidence_id else None,
        "lines": claim_lines_for(claim.lines, show_cost=show_cost),
        "content_hash": claim.content_hash,
    }


# ---------------------------------------------------------------------------
# Count sessions and observations (E115, E116)
# ---------------------------------------------------------------------------

OBSERVATION_KEYS = frozenset(
    {
        "scan_key",
        "sku_id",
        "description",
        "alias_value",
        "alias_context",
        "attrs",
        "condition",
        "qty",
        "correction_of_id",
    }
)
ATTRIBUTE_KEYS = frozenset({"field_id", "vocabulary_value_id", "supplied_text", "unknown"})


def open_count_session(
    run: CommandRun,
    arrival: Arrival,
    *,
    counter_id: uuid.UUID,
    entry_user_id: uuid.UUID,
    expected_revision: int | None,
) -> CountSession:
    require_goods_site(run, arrival.site_id)
    head = lock_arrival_head(run, arrival.pk)
    inp.check_revision(expected_revision, head.revision)
    if GoodsGrn.objects.filter(arrival_id=arrival.pk).exists():
        raise Refusal(
            "COUNT_SESSION_INVALID",
            "This arrival's count is already issued as a GRN; correct it with a counter-GRN.",
            issues=[issue("GRN_ISSUED", "The arrival already has an issued GRN.")],
        )
    problems = [
        issue(
            "NOT_ASSIGNED",
            "This person is not an active receiver at the arrival's site.",
            field=field,
        )
        for field, human_id in (("counter_id", counter_id), ("entry_user_id", entry_user_id))
        if not person_can_receive(run.tenant_id, human_id, arrival.site_id)
    ]
    if problems:
        raise Refusal(
            "COUNT_SESSION_INVALID", "The count session cannot be opened.", issues=problems
        )
    session = CountSession.objects.create(
        tenant_id=run.tenant_id,
        arrival_id=arrival.pk,
        counter_id=counter_id,
        entry_user_id=entry_user_id,
        last_activity_at=run.now,
    )
    head.revision += 1
    head.save(update_fields=["revision"])
    return session


def hand_over_count(
    run: CommandRun,
    session_id: uuid.UUID,
    *,
    to_human_id: uuid.UUID,
    reason_code: str,
    reviewed_hash: str,
    expected_revision: int | None,
) -> CountHandover:
    """Pass an unfinished count to another authorised person at the site (E240).

    The count itself does not move: same session, same arrival, same acknowledged
    scans, same eventual GRN. Only who is holding it changes, and the change is
    appended as evidence rather than quietly overwriting the session, so the
    people who counted the earlier pieces remain readable (GSA-T05).

    Nothing here reclassifies goods or creates a quantity: a handover has no
    observation, no line and no value leg of its own.
    """
    current = CountSession.objects.select_related("arrival").get(pk=session_id)
    arrival = current.arrival
    require_goods_site(run, arrival.site_id)
    session: CountSession = run.lock(LockRank.DRAFT, CountSession.objects.filter(pk=session_id))[0]
    inp.check_revision(expected_revision, session.revision)
    observations = session_observations(session.pk)
    if reviewed_hash != session_hash(session, observations):
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The count changed after you reviewed it. Reload and review it again.",
        )
    if session.state != CountSession.State.OPEN or session.grn_id is not None:
        raise Refusal(
            "COUNT_SESSION_INVALID",
            "This count is finished; there is nothing left to hand over.",
            issues=[issue("SESSION_CLOSED", "The count session is no longer open.")],
        )
    from_human_id = run.principal.human_id
    # Only the person actually holding the count may pass it on. A receiver who
    # merely works at the site cannot take a colleague's unfinished count off
    # them, and a service principal holds no count at all.
    if from_human_id is None or from_human_id not in {session.counter_id, session.entry_user_id}:
        raise Refusal(
            "COUNT_SESSION_INVALID",
            "Only the person holding this count can hand it to someone else.",
            issues=[issue("NOT_THE_HOLDER", "You are not this count session's current owner.")],
        )
    if to_human_id == from_human_id:
        raise Refusal(
            "COUNT_SESSION_INVALID",
            "A count is handed to somebody else, not back to yourself.",
            issues=[issue("SAME_PERSON", "The new owner is the current one.", field="to_human_id")],
        )
    if not person_can_receive(run.tenant_id, to_human_id, arrival.site_id):
        raise Refusal(
            "COUNT_SESSION_INVALID",
            "That person is not an active receiver at this arrival's site.",
            issues=[
                issue(
                    "NOT_ASSIGNED",
                    "This person is not an active receiver at the arrival's site.",
                    field="to_human_id",
                )
            ],
        )
    handover = CountHandover(
        count_session_id=session.pk,
        from_human_id=from_human_id,
        to_human_id=to_human_id,
        reason_code=reason_code,
    )
    run.record(handover)
    run.audit_before = {
        "counter_id": str(session.counter_id),
        "entry_user_id": str(session.entry_user_id),
    }
    session.counter_id = to_human_id
    session.entry_user_id = to_human_id
    session.revision += 1
    session.last_activity_at = run.now
    session.save(update_fields=["counter", "entry_user", "revision", "last_activity_at"])
    # Both sides name the same keys, so the audit reads as a change rather than
    # two different shapes. Why it moved is on the handover row itself.
    run.audit_after = {
        "counter_id": str(to_human_id),
        "entry_user_id": str(to_human_id),
    }
    return handover


def session_handovers(session_id: uuid.UUID) -> list[CountHandover]:
    return list(
        CountHandover.objects.filter(count_session_id=session_id).order_by("recorded_at", "id")
    )


#: The AliasContext fields an observation keeps: enough to re-judge the same code
#: under the same scope it was read under, and nothing that is not a scope.
ALIAS_CONTEXT_KEYS = frozenset({"issuer_key", "alias_type", "profile_version_id"})


def _alias_context(value: Any, field: str) -> dict[str, Any] | None:
    """The scope a scanned code was read under, kept with the scan itself.

    Judging the same code twice under two different scopes is how a piece that
    the count screen resolved cleanly can be refused at GRN issue, or the other
    way round, with nobody able to see why. Keeping the context makes the second
    judgement the same judgement.
    """
    if value in (None, ""):
        return None
    item = inp.closed(value, ALIAS_CONTEXT_KEYS, field)
    return {
        "issuer_key": inp.text(item.get("issuer_key"), f"{field}.issuer_key", 100),
        "alias_type": inp.text(item.get("alias_type"), f"{field}.alias_type", 12),
        "profile_version_id": str(
            inp.optional_uuid(item.get("profile_version_id"), f"{field}.profile_version_id") or ""
        )
        or None,
    }


def parse_observations(value: Any) -> list[dict[str, Any]]:
    rows = inp.object_list(value, "observations", limit=5000)
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        field = f"observations[{index}]"
        obs = inp.closed(
            raw, OBSERVATION_KEYS, field, required=["scan_key", "description", "condition", "qty"]
        )
        condition = obs["condition"]
        if condition not in CONDITIONS:
            raise inp.bad(f"{field}.condition must be one of {', '.join(CONDITIONS)}.", field)
        attrs_raw = obs.get("attrs") or []
        if not isinstance(attrs_raw, list) or len(attrs_raw) > 100:
            raise inp.bad(f"{field}.attrs must be a list.", f"{field}.attrs")
        attrs = []
        for position, attr in enumerate(attrs_raw):
            item = inp.closed(
                attr, ATTRIBUTE_KEYS, f"{field}.attrs[{position}]", required=["field_id"]
            )
            attrs.append(
                {
                    "field_id": str(inp.vocabulary_id(item["field_id"], "field_id")),
                    "vocabulary_value_id": inp.vocabulary_id(
                        item.get("vocabulary_value_id"), "vocabulary_value_id"
                    ),
                    "supplied_text": inp.text(item.get("supplied_text"), "supplied_text", 240),
                    "unknown": bool(item.get("unknown", False)),
                }
            )
        qty = obs["qty"]
        if isinstance(qty, bool) or not isinstance(qty, int) or abs(qty) > 999_999:
            raise inp.bad(f"{field}.qty must be a whole number.", f"{field}.qty")
        sku = inp.optional_uuid(obs.get("sku_id"), f"{field}.sku_id")
        correction = inp.optional_uuid(obs.get("correction_of_id"), f"{field}.correction_of_id")
        out.append(
            {
                "scan_key": str(inp.uuid_value(obs["scan_key"], f"{field}.scan_key")),
                "sku_id": str(sku) if sku else None,
                "description": inp.plain_text(obs["description"], f"{field}.description", 240),
                "alias_value": inp.text(obs.get("alias_value"), f"{field}.alias_value", 128),
                "alias_context": _alias_context(obs.get("alias_context"), f"{field}.alias_context"),
                "attrs": attrs,
                "condition": condition,
                "qty": qty,
                "correction_of_id": str(correction) if correction else None,
            }
        )
    return out


def _observation_input(row: ScanObservation) -> dict[str, Any]:
    return {
        "scan_key": str(row.scan_key),
        "sku_id": str(row.sku_id) if row.sku_id else None,
        "description": row.description,
        "alias_value": row.alias_value,
        "alias_context": row.alias_context,
        "attrs": row.attrs,
        "condition": row.condition,
        "qty": row.qty,
        "correction_of_id": str(row.correction_of_id) if row.correction_of_id else None,
    }


def record_observations(
    run: CommandRun,
    session_id: uuid.UUID,
    observations: list[dict[str, Any]],
    *,
    expected_revision: int | None,
) -> list[str]:
    """Append durable, duplicate-safe observations; returns every acknowledged scan key."""
    current = CountSession.objects.select_related("arrival").get(pk=session_id)
    require_goods_site(run, current.arrival.site_id)
    session: CountSession = run.lock(LockRank.DRAFT, CountSession.objects.filter(pk=session_id))[0]
    inp.check_revision(expected_revision, session.revision)
    if session.state != CountSession.State.OPEN:
        raise Refusal("OBSERVATION_INVALID", "This count session is no longer open.", status=422)
    stored = list(ScanObservation.objects.filter(count_session_id=session_id))
    by_key = {str(row.scan_key): row for row in stored}
    by_id = {str(row.pk): row for row in stored}
    totals: dict[str, int] = defaultdict(int)
    for row in stored:
        totals[str(row.correction_of_id or row.pk)] += row.qty
    wanted_skus = {o["sku_id"] for o in observations if o["sku_id"]}
    known_skus = {
        str(pk)
        for pk in ProductSku.objects.filter(pk__in=list(wanted_skus)).values_list("pk", flat=True)
    }
    problems: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    new_rows: list[ScanObservation] = []
    for index, obs in enumerate(observations):
        field = f"observations[{index}]"
        key = obs["scan_key"]
        if key in seen:
            if seen[key] != obs:
                problems.append(
                    issue(
                        "SCAN_KEY_REUSED",
                        "One scan key was sent twice with different contents.",
                        field=field,
                    )
                )
            continue
        seen[key] = obs
        prior = by_key.get(key)
        if prior is not None:
            if _observation_input(prior) != obs:
                problems.append(
                    issue(
                        "SCAN_KEY_REUSED",
                        "This scan key is already recorded with different contents.",
                        field=field,
                    )
                )
            continue
        if obs["sku_id"] and obs["sku_id"] not in known_skus:
            problems.append(issue("UNKNOWN_SKU", "No SKU has that ID.", field=f"{field}.sku_id"))
        if obs["condition"] == "unidentified" and obs["sku_id"]:
            problems.append(
                issue(
                    "UNIDENTIFIED_WITH_SKU",
                    "Unidentified goods carry a description, not a SKU.",
                    field=field,
                )
            )
        if not obs["sku_id"] and not obs["description"].strip():
            problems.append(
                issue(
                    "DESCRIPTION_REQUIRED",
                    "Goods without a SKU need a description.",
                    field=f"{field}.description",
                )
            )
        target_id = obs["correction_of_id"]
        if target_id is None:
            if not 1 <= obs["qty"] <= 999_999:
                problems.append(
                    issue("QTY_INVALID", "A scan counts 1 to 999,999 pieces.", field=f"{field}.qty")
                )
        else:
            target = by_id.get(target_id)
            if target is None or target.correction_of_id is not None:
                problems.append(
                    issue(
                        "CORRECTION_TARGET",
                        "A correction must point at an original scan in this session.",
                        field=f"{field}.correction_of_id",
                    )
                )
            elif (
                obs["qty"] == 0
                or target.condition != obs["condition"]
                or str(target.sku_id or "") != (obs["sku_id"] or "")
            ):
                problems.append(
                    issue(
                        "CORRECTION_INVALID",
                        "A correction changes only the quantity of the scan it corrects.",
                        field=field,
                    )
                )
            elif totals[target_id] + obs["qty"] < 0:
                problems.append(
                    issue(
                        "CORRECTION_BELOW_ZERO",
                        "A correction cannot take a scan below zero.",
                        field=f"{field}.qty",
                    )
                )
            else:
                totals[target_id] += obs["qty"]
        new_rows.append(
            ScanObservation(
                count_session_id=session.pk,
                site_id=current.arrival.site_id,
                scan_key=uuid.UUID(key),
                sku_id=uuid.UUID(obs["sku_id"]) if obs["sku_id"] else None,
                description=obs["description"],
                alias_value=obs["alias_value"],
                alias_context=obs["alias_context"],
                attrs=obs["attrs"],
                condition=obs["condition"],
                qty=obs["qty"],
                correction_of_id=uuid.UUID(target_id) if target_id else None,
            )
        )
    if problems:
        raise Refusal(
            "OBSERVATION_INVALID", "Some scans could not be recorded.", status=422, issues=problems
        )
    for row in new_rows:
        run.record(row)
    if new_rows:
        session.revision += 1
        session.last_activity_at = run.now
        session.save(update_fields=["revision", "last_activity_at"])
        _open_identity_ambiguities(run, current.arrival.site_id, new_rows)
    return sorted(set(by_key) | set(seen))


def _open_identity_ambiguities(run: CommandRun, site_id: int, rows: list[ScanObservation]) -> None:
    """Own each counted code that matched more than one product (GSA-T04).

    The counter can record the scan and keep counting, but the piece cannot be
    frozen into custody until someone authorised chooses between the candidates
    (E117 step 13). That waiting is real work for the product master owner, so it
    is named here, from inside the command that discovered it.

    A code that matched exactly one product, and a piece that is honestly
    unidentified and carries its description instead, are not ambiguous and raise
    nothing. The scan's own key is the upsert key, so re-sending the same scan
    names the same work.
    """
    # Judged under the scope each scan was read under, the same rule E117 applies
    # at issue, so a code is never called ambiguous here and resolved there.
    # Resolved once per distinct (code, scope) rather than once per row, exactly
    # as `settled_identities` does: a request may carry thousands of scans, and a
    # carton of one unlucky barcode must not become thousands of lookups.
    pending = [row for row in rows if row.sku_id is None and (row.alias_value or "").strip()]
    if not pending:
        return
    families: dict[str, str | None] = {}
    results: dict[tuple[str, str, str, str], str] = {}
    for row in pending:
        context = row.alias_context or {}
        issuer = str(context.get("issuer_key") or "")
        alias_type = str(context.get("alias_type") or "")
        profile_id = str(context.get("profile_version_id") or "")
        key = (str(row.alias_value), issuer, alias_type, profile_id)
        if key not in results:
            if profile_id not in families:
                families[profile_id] = _profile_family(run.tenant_id, profile_id)
            results[key] = resolve_alias(
                run.tenant_id,
                value=str(row.alias_value),
                site_id=site_id,
                as_of=run.now,
                issuer_key=issuer or None,
                alias_type=alias_type or None,
                profile_family=families[profile_id],
            ).result
        if results[key] != "ambiguous":
            continue
        # One job per scan, because E117 needs one pick per scan: these are that
        # many real decisions, not one decision counted many times.
        open_exception(
            run,
            kind="identity_ambiguity",
            site_id=site_id,
            subject_key=f"identity:{row.pk}",
            reason_code="IDENTITY_AMBIGUOUS",
            source_event_key=row.scan_key,
            allowed_resolution_actions=["masters/identity-picks"],
            note=f"{row.alias_value} matches more than one product.",
        )


def _resolve_identity_ambiguities(run: CommandRun, rows: list[ScanObservation]) -> None:
    """Close every ambiguity job for these scans; called once the GRN may be issued."""
    for row in rows:
        if row.sku_id is None and (row.alias_value or "").strip():
            resolve_exceptions(
                run,
                kind="identity_ambiguity",
                subject_key=f"identity:{row.pk}",
                reason_code="IDENTITY_SETTLED",
                source_event_key=row.scan_key,
            )


def session_observations(session_id: uuid.UUID) -> list[ScanObservation]:
    return list(
        ScanObservation.objects.filter(count_session_id=session_id).order_by("recorded_at", "id")
    )


def handover_data(row: CountHandover) -> dict[str, Any]:
    return {
        "id": str(row.pk),
        "session_id": str(row.count_session_id),
        "from_human_id": str(row.from_human_id),
        "to_human_id": str(row.to_human_id),
        "actor_id": str(row.actor_id) if row.actor_id else None,
        "recorded_at": row.recorded_at.isoformat(),
        "reason_code": row.reason_code,
    }


def session_data(session: CountSession, observations: list[ScanObservation]) -> dict[str, Any]:
    return {
        "arrival_id": str(session.arrival_id),
        "counter_id": str(session.counter_id),
        "entry_user_id": str(session.entry_user_id),
        "state": session.state,
        "revision": session.revision,
        "grn_id": str(session.grn_id) if session.grn_id else None,
        "acknowledged_scan_keys": sorted(str(o.scan_key) for o in observations),
        # The durable rows themselves, so a reloaded count shows what was actually
        # recorded rather than whatever the screen still had in memory - and so a
        # scan whose code matched several products has an id an E091 pick can bind
        # to (design E091, E117 step 13).
        "observations": [
            {
                "id": str(row.pk),
                "scan_key": str(row.scan_key),
                "sku_id": str(row.sku_id) if row.sku_id else None,
                "alias_value": row.alias_value,
                "description": row.description,
                "condition": row.condition,
                "qty": row.qty,
                "correction_of_id": str(row.correction_of_id) if row.correction_of_id else None,
            }
            for row in observations
        ],
        # Who has held this count, and why it changed hands (E240). The session's
        # counter and entry user above are only the current owner; without this
        # chain the person who counted the first half of a delivery disappears
        # from the record the moment somebody else takes it on.
        "handovers": [handover_data(row) for row in session_handovers(session.pk)],
    }


def session_hash(session: CountSession, observations: list[ScanObservation] | None = None) -> str:
    rows = observations if observations is not None else session_observations(session.pk)
    return content_hash(
        {
            "session": str(session.pk),
            "state": session.state,
            "revision": session.revision,
            # Who holds the count is part of what a reviewer reviewed: a GRN
            # issued against a count that changed hands after it was read is
            # issued against something the issuer did not see.
            "counter": str(session.counter_id),
            "entry_user": str(session.entry_user_id),
            "observations": sorted(
                ([str(o.pk), *_observation_input(o).values()] for o in rows), key=str
            ),
        }
    )


# ---------------------------------------------------------------------------
# GRN issue (E117, P01)
# ---------------------------------------------------------------------------


def _profile_family(tenant_id: uuid.UUID, profile_version_id: str) -> str | None:
    """The product family an identity profile names, or ``None`` when unscoped."""
    if not profile_version_id:
        return None
    found = profile_context(tenant_id, uuid.UUID(profile_version_id))
    return (found[1] or None) if found else None


def settled_identities(
    site_id: int, observations: list[ScanObservation], now: datetime
) -> dict[uuid.UUID, uuid.UUID]:
    """The SKU each still-unidentified observation was settled to, and why it may be.

    Design E117 step 13: an observation whose scanned code matched more than one
    SKU cannot be frozen into custody until someone with authority chose between
    them, and that choice is an E091 IdentityPick bound to this exact scan. A code
    that matches nothing stays honestly unidentified. Re-resolved here rather than
    trusted from the count screen, once per distinct code.
    """
    tenant_id = require_tenant_id()
    pending = [
        row for row in observations if row.sku_id is None and (row.alias_value or "").strip()
    ]
    if not pending:
        return {}
    # Newest pick wins: nothing stops an authorised person changing their mind
    # before the GRN is issued, and an unordered read would freeze whichever row
    # the database happened to hand back.
    picks: dict[uuid.UUID, uuid.UUID] = {}
    for pick in IdentityPick.objects.filter(
        tenant_id=tenant_id, scan_event_id__in=[row.pk for row in pending]
    ).order_by("recorded_at", "pk"):
        if pick.scan_event_id is not None:
            picks[pick.scan_event_id] = pick.chosen_sku_id
    settled: dict[uuid.UUID, uuid.UUID] = {}
    ambiguous: list[ScanObservation] = []
    results: dict[tuple[str, str, str, str], str] = {}
    for row in pending:
        value = str(row.alias_value)
        chosen = picks.get(row.pk)
        if chosen is not None:
            settled[row.pk] = chosen
            continue
        # Re-judged under the scope the scan itself was read under, never a wider
        # one: a code that is unique to this vendor's issuer key and shared
        # tenant-wide must not read as ambiguous now when it read as resolved at
        # the counter, with no way left to choose.
        context = row.alias_context or {}
        issuer = str(context.get("issuer_key") or "")
        alias_type = str(context.get("alias_type") or "")
        profile_id = str(context.get("profile_version_id") or "")
        key = (value, issuer, alias_type, profile_id)
        if key not in results:
            results[key] = resolve_alias(
                tenant_id,
                value=value,
                site_id=site_id,
                as_of=now,
                issuer_key=issuer or None,
                alias_type=alias_type or None,
                profile_family=_profile_family(tenant_id, profile_id),
            ).result
        if results[key] == "ambiguous":
            ambiguous.append(row)
    if ambiguous:
        raise Refusal(
            "GRN_ISSUE_INVALID",
            "Some counted goods matched more than one product. Choose which one each is "
            "before issuing the GRN.",
            issues=[
                issue(
                    "IDENTITY_AMBIGUOUS",
                    f"{row.alias_value} matches more than one product.",
                    field="observations",
                )
                for row in ambiguous
            ],
        )
    return settled


def _grn_lines(
    session: CountSession,
    observations: list[ScanObservation],
    settled: dict[uuid.UUID, uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Accumulate observations by identity + condition; corrections join their target."""
    settled = settled or {}
    groups: dict[str, dict[str, Any]] = {}
    group_of: dict[str, str] = {}
    for row in observations:
        # An authorised E091 choice *is* this piece's identity from here on; the
        # observation itself stays exactly as the counter recorded it.
        sku_id = row.sku_id or settled.get(row.pk)
        if row.correction_of_id is not None:
            key = group_of.get(str(row.correction_of_id))
            if key is None:
                continue
        elif sku_id is not None:
            key = content_hash(["sku", str(sku_id), row.condition])
        else:
            # Different scanned barcodes are different physical identities until
            # someone resolves them, so they never share a line or a lot.
            key = content_hash(
                [
                    "described",
                    row.description.strip().casefold(),
                    row.attrs,
                    row.condition,
                    row.alias_value or "",
                ]
            )
        group_of[str(row.pk)] = key
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "line_key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"grn-line:{session.pk}:{key}")),
                "observation_ids": [],
                "identity": {
                    "sku_id": str(sku_id) if sku_id else None,
                    "description": row.description,
                    "attributes": row.attrs,
                    "raw_alias": row.alias_value,
                },
                "condition": row.condition,
                "qty": 0,
                "booking_line_key": None,
                "discrepancy_remark": None,
            }
        group["observation_ids"].append(str(row.pk))
        group["qty"] += row.qty
    return [g for g in groups.values() if g["qty"] > 0]


def claim_identity(claim: dict[str, Any]) -> tuple[str, str]:
    """The identity a claim line is compared on: its SKU, else its description."""
    if claim.get("sku_id"):
        return ("sku", str(claim["sku_id"]))
    wanted = str(claim.get("description") or "").strip().casefold()
    return ("description", wanted) if wanted else ("line", str(claim["line_key"]))


def compare_claim(
    claim_lines: list[dict[str, Any]], lines: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Claimed versus counted for this shipment only (P1-REC-03).

    Claim lines for the same identity are one group: the counted total of the group
    fills its lines in invoice order and only the last line carries any excess, so an
    invoice that splits one SKU across lines (for example at two rates) is not a
    false shortage on one line and excess on the other.
    """
    matched: dict[str, str] = {}
    for line in lines:
        identity = line["identity"]
        for claim in claim_lines:
            if claim.get("sku_id"):
                hit = identity.get("sku_id") == claim["sku_id"]
            else:
                wanted = str(claim.get("description") or "").strip().casefold()
                hit = (
                    bool(wanted)
                    and wanted == str(identity.get("description") or "").strip().casefold()
                )
            if hit:
                matched[line["line_key"]] = claim["line_key"]
                break
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in claim_lines:
        groups.setdefault(claim_identity(claim), []).append(claim)
    by_claim: dict[str, dict[str, Any]] = {}
    for members in groups.values():
        member_keys = {claim["line_key"] for claim in members}
        keys = [k for k, c in matched.items() if c in member_keys]
        remaining = sum(line["qty"] for line in lines if line["line_key"] in keys)
        for index, claim in enumerate(members):
            claimed = int(claim["claimed_qty"])
            counted = remaining if index == len(members) - 1 else min(claimed, remaining)
            remaining -= counted
            by_claim[claim["line_key"]] = {
                "claim_line_key": claim["line_key"],
                "claimed_qty": claimed,
                "counted_qty": counted,
                "difference": counted - claimed,
                "line_keys": keys,
            }
    comparisons = [by_claim[claim["line_key"]] for claim in claim_lines]
    return comparisons, matched


def discrepancies(
    lines: list[dict[str, Any]], claim_lines: list[dict[str, Any]] | None
) -> tuple[dict[str, int], set[str]]:
    """Quantity needing a decision, keyed by GRN line (physical) or claim line (shortage)."""
    out: dict[str, int] = defaultdict(int)
    reasons: set[str] = set()
    by_key = {line["line_key"]: line for line in lines}
    for line in lines:
        if line["condition"] != "good":
            out[line["line_key"]] += line["qty"]
            reasons.add(line["condition"].upper())
    if claim_lines is None:
        return dict(out), reasons
    comparisons, matched = compare_claim(claim_lines, lines)
    for line in lines:
        if line["line_key"] not in matched and line["condition"] == "good":
            reasons.add("EXCESS")
    for comparison in comparisons:
        difference = comparison["difference"]
        if difference < 0:
            out[comparison["claim_line_key"]] += -difference
            reasons.add("SHORTAGE")
        elif difference > 0:
            reasons.add("EXCESS")
    for line_key, qty in excess_by_line(lines, claim_lines, by_key=by_key).items():
        out[line_key] += qty
    return dict(out), reasons


def excess_by_line(
    lines: list[dict[str, Any]],
    claim_lines: list[dict[str, Any]],
    *,
    by_key: dict[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Good pieces counted above the claim, per GRN line.

    An unclaimed good line is excess whole. A claim group's excess (less its pieces counted
    in another condition) is spread over its good lines, latest first, never more than a
    line counted, so no line is charged excess it does not hold.
    """
    by_key = by_key or {line["line_key"]: line for line in lines}
    comparisons, matched = compare_claim(claim_lines, lines)
    out: dict[str, int] = defaultdict(int)
    for line in lines:
        if line["line_key"] not in matched and line["condition"] == "good":
            out[line["line_key"]] += line["qty"]
    for comparison in comparisons:
        keys = comparison["line_keys"]
        not_good = sum(by_key[k]["qty"] for k in keys if by_key[k]["condition"] != "good")
        excess = comparison["difference"] - not_good
        for key in reversed([k for k in keys if by_key[k]["condition"] == "good"]):
            if excess <= 0:
                break
            take = min(excess, by_key[key]["qty"] - out[key])
            out[key] += take
            excess -= take
    return {key: qty for key, qty in out.items() if qty > 0}


def issue_grn(
    run: CommandRun,
    session_id: uuid.UUID,
    *,
    reviewed_hash: str,
    remarks: dict[str, str],
    expected_revision: int | None,
) -> tuple[DocumentIdentity, str]:
    from vendors.goods_services import link_exact_matches, lock_booking_for_receipt

    current = CountSession.objects.select_related("arrival", "arrival__site__gstin").get(
        pk=session_id
    )
    arrival = current.arrival
    site_id = arrival.site_id
    require_goods_site(run, site_id)
    arrival_head = lock_arrival_head(run, arrival.pk)
    # Received against a booking: its head is locked now, beside the arrival's, so the
    # exact-match receipt links appended below keep the fixed lock order (PRD §5.1).
    booking_lock = lock_booking_for_receipt(run, arrival.booking_id)
    session: CountSession = run.lock(LockRank.DRAFT, CountSession.objects.filter(pk=session_id))[0]
    inp.check_revision(expected_revision, session.revision)
    observations = session_observations(session.pk)
    if reviewed_hash != session_hash(session, observations):
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The count changed after you reviewed it. Reload and review it again.",
        )
    if (
        session.state != CountSession.State.OPEN
        or GoodsGrn.objects.filter(count_session_id=session.pk).exists()
    ):
        raise Refusal("GRN_ISSUE_INVALID", "A GRN has already been issued from this count session.")
    if GoodsGrn.objects.filter(arrival_id=arrival.pk).exists():
        raise Refusal(
            "GRN_ISSUE_INVALID",
            "This arrival already has an issued GRN; correct it with a counter-GRN.",
        )
    lines = _grn_lines(session, observations, settled_identities(site_id, observations, run.now))
    if not lines:
        raise Refusal("GRN_ISSUE_INVALID", "A GRN needs at least one counted piece.")
    # GSA-T04: reaching here means no counted code is ambiguous any more - either
    # somebody picked, or the governed alias behind the clash was corrected or
    # retired so the code now means one thing. Either way the waiting is over, so
    # the owned work closes here too; without this, a clash fixed at the master
    # would leave a C-PMO job nobody could ever close.
    _resolve_identity_ambiguities(run, observations)
    claim = latest_claim(arrival.pk)
    claim_lines: list[dict[str, Any]] = list(claim.lines) if claim else []
    comparisons, matched = compare_claim(claim_lines, lines)
    claim_keys = {c["line_key"] for c in claim_lines}
    problems = [
        issue(
            "REMARK_REQUIRED",
            "Every difference between the invoice and the count needs a remark.",
            line_key=c["claim_line_key"],
            quantity=c["difference"],
        )
        for c in comparisons
        if c["difference"] != 0 and not (remarks.get(c["claim_line_key"]) or "").strip()
    ]
    problems += [
        issue(
            "UNKNOWN_CLAIM_LINE", "A remark names a claim line that does not exist.", line_key=key
        )
        for key in remarks
        if key not in claim_keys
    ]
    if problems:
        raise Refusal("GRN_ISSUE_INVALID", "The GRN cannot be issued yet.", issues=problems)
    identity_of = {claim["line_key"]: claim_identity(claim) for claim in claim_lines}
    for line in lines:
        claim_key = matched.get(line["line_key"])
        if claim_key is None:
            continue
        for member in claim_lines:
            if identity_of[member["line_key"]] == identity_of[claim_key] and remarks.get(
                member["line_key"]
            ):
                line["discrepancy_remark"] = remarks[member["line_key"]]
                break
    header = {
        "arrival_id": str(arrival.pk),
        "count_session_id": str(session.pk),
        "site_id": str(site_id),
        "counter_id": str(session.counter_id),
        "entry_user_id": str(session.entry_user_id),
        "claim_revision_id": str(claim.pk) if claim else None,
    }
    entity = arrival.site.gstin.legal_entity
    identity, head = new_document(
        run, kind="GRN", purpose="grn", entity_id=entity.pk, site_id=site_id
    )
    keyed = [(uuid.UUID(line["line_key"]), line) for line in lines]
    append_revision(run, head, header=header, replace_lines=[(k, v) for k, v in keyed])
    number = allocate(run, entity, "GRN")
    assert run.principal.human_id is not None
    version, official_lines = officialise(
        run,
        head,
        approved_by_id=run.principal.human_id,
        canonical_header=header,
        lines=[(k, v) for k, v in keyed],
        authority=run.authority,
        number=number,
    )
    goods_grn = GoodsGrn.objects.create(
        tenant_id=run.tenant_id,
        document_id=identity.pk,
        arrival_id=arrival.pk,
        count_session_id=session.pk,
        booking_id=arrival.booking_id,
    )
    goods_grn.document = identity
    session.state = CountSession.State.ISSUED
    session.grn_id = identity.pk
    session.revision += 1
    session.last_activity_at = run.now
    session.save(update_fields=["state", "grn", "revision", "last_activity_at"])
    arrival_head.grn_count += 1
    arrival_head.revision += 1
    arrival_head.save(update_fields=["grn_count", "revision"])

    plan = Plan("P01", version.pk, event_key("P01", version.pk))
    locations: dict[str, Location] = {}
    lot_rows: list[tuple[uuid.UUID, str]] = []
    counted_damage: list[tuple[uuid.UUID, CustodyLot, uuid.UUID | None]] = []
    for official in official_lines:
        payload = official.payload
        condition = str(payload["condition"])
        kind = "receiving" if condition == "good" else "quarantine"
        if kind not in locations:
            locations[kind] = system_location(site_id, kind)
        sku = payload["identity"].get("sku_id")
        opened = open_lot(
            run,
            plan,
            source_kind="grn",
            site_id=site_id,
            qty=int(payload["qty"]),
            # The lot keeps what was counted: condition, brand and the observations behind it,
            # so a later decision can never make the goods look as if they arrived otherwise.
            identity={
                **payload["identity"],
                "condition": condition,
                "brand_id": arrival.brand_id,
                "observation_ids": list(payload.get("observation_ids") or []),
            },
            source_time=arrival.actual_arrival_at,
            address=Address(
                boundary="physical",
                site_id=site_id,
                location_id=locations[kind].pk,
                condition=condition,
                sku_id=uuid.UUID(sku) if sku else None,
            ),
            source_line_id=official.pk,
        )
        lot_rows.append((opened.pk, str(official.stable_line_key)))
        if condition == "damaged":
            counted_damage.append(
                (official.stable_line_key, opened, uuid.UUID(sku) if sku else None)
            )
    post(run, version, plan)
    if claim is not None:
        # Lots and lines are this command's own evidence, so they are handed over directly.
        sync_excess_holds(
            run,
            goods_grn,
            trigger=version.pk,
            version=version,
            lines=lines,
            claim_lines=claim_lines,
            lot_rows=lot_rows,
            lock=False,
        )
    # Ticket 05C: counting a piece damaged is reporting it. It opened in quarantine
    # above; its damage hold and the pending report a different person decides
    # open now, in the same command (damaged-goods PRD §3.1, §4).
    damage_report = hold_counted_damage(
        run, goods_grn, version, grn_revision=head.revision, counted=counted_damage
    )

    # Store and warehouse operations PRD §5.1 (Anand, 23 September 2026): each counted
    # line matching exactly one booking line is linked now, through the receipt-link
    # command's own rules, dated now and recorded as this GRN issuer's. Anything else
    # waits for the buyer.
    booking_links = link_exact_matches(
        run,
        booking_lock,
        grn_document_id=identity.pk,
        arrival_id=arrival.pk,
        grn_lines=[(official.stable_line_key, official.payload) for official in official_lines],
    )

    subject = f"grn:{identity.pk}"
    open_exception(
        run,
        kind="grn_awaiting_pt",
        site_id=site_id,
        subject_key=subject,
        reason_code="GRN_ISSUED",
        source_event_key=event_key("grn_awaiting_pt", identity.pk),
        allowed_resolution_actions=["ptmapper/files/from-grn/{id}"],
    )
    needed, reasons = discrepancies(lines, claim_lines if claim else None)
    # Counted damage is already held and under review, so it is not waiting for a
    # receiving decision; the review is where it is owned.
    held_damage = {str(key) for key, _lot, _sku in counted_damage}
    needed = {key: qty for key, qty in needed.items() if key not in held_damage}
    reasons.discard("DAMAGED")
    if needed:
        open_exception(
            run,
            kind="receipt_discrepancy",
            site_id=site_id,
            subject_key=subject,
            reason_code="+".join(sorted(reasons))[:60] or "DISCREPANCY",
            source_event_key=event_key("receipt_discrepancy", identity.pk),
            allowed_resolution_actions=[
                "inbound/grns/{id}/dispositions",
                "inbound/grns/{id}/counter",
            ],
        )
    # Ticket 37: booked, invoiced and counted compared once the count is issued,
    # never before - counting is never stopped by it.
    three_way_match.refresh(run, goods_grn, lines=lines, claim_lines=claim_lines if claim else None)
    run.audit_after = {
        "grn_id": str(identity.pk),
        "number": number,
        "lines": len(lines),
        "booking_links": len(booking_links),
        **({"damage_report_id": str(damage_report.pk)} if damage_report is not None else {}),
    }
    return identity, number


# ---------------------------------------------------------------------------
# Custody read API (used by the PT slice)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrnLotView:
    lot_id: uuid.UUID
    line_key: uuid.UUID
    qty: int
    condition: str
    sku_id: uuid.UUID | None
    description: str
    covered: list[tuple[int, int]]  # portions under live coverage (stockledger.LiveCoverage)
    held_uncovered: list[tuple[int, int]]  # physical, not covered, not disposed
    disposed: list[tuple[int, int]]  # returned/disposed boundary
    held: list[tuple[int, int]]  # under an actual ActiveHold, whatever its coverage


def _counter_documents(grn_document_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        CounterGrnDraft.objects.filter(grn__document_id=grn_document_id).values_list(
            "document_id", flat=True
        )
    )


def grn_lots(grn_document_id: uuid.UUID) -> list[tuple[CustodyLot, uuid.UUID]]:
    """Every custody lot opened by the GRN or its approved counter-GRNs, with its line key."""
    documents = [grn_document_id, *_counter_documents(grn_document_id)]
    index = dict(
        OfficialLine.objects.filter(version__document_id__in=documents).values_list(
            "pk", "stable_line_key"
        )
    )
    lots = CustodyLot.objects.filter(source_line_id__in=list(index)).order_by("recorded_at", "id")
    return [(lot, index[lot.source_line_id]) for lot in lots if lot.source_line_id in index]


def grn_custody(grn_document_id: uuid.UUID) -> list[GrnLotView]:
    rows = grn_lots(grn_document_id)
    lot_ids = [lot.pk for lot, _ in rows]
    covered: dict[uuid.UUID, list[tuple[int, int]]] = defaultdict(list)
    for lot_id, stored in LiveCoverage.objects.filter(lot_id__in=lot_ids).values_list(
        "lot_id", "portion"
    ):
        covered[lot_id].append(bounds(stored))
    physical: dict[uuid.UUID, list[tuple[int, int]]] = defaultdict(list)
    disposed: dict[uuid.UUID, list[tuple[int, int]]] = defaultdict(list)
    conditions: dict[uuid.UUID, set[str]] = defaultdict(set)
    skus: dict[uuid.UUID, set[uuid.UUID | None]] = defaultdict(set)
    for position in Position.objects.filter(lot_id__in=lot_ids):
        interval = bounds(position.portion)
        if position.boundary == "physical":
            physical[position.lot_id].append(interval)
            conditions[position.lot_id].add(position.condition)
            skus[position.lot_id].add(position.sku_id)
        elif position.boundary in ("returned", "disposed"):
            disposed[position.lot_id].append(interval)
    # An *actual* hold, not "no PT covers it yet". GSA-T05 separates the two:
    # `held_qty` below counts only portions an ActiveHold row names, whatever
    # their coverage; `uncovered_qty` counts what no live coverage reaches.
    holds: dict[uuid.UUID, list[tuple[int, int]]] = defaultdict(list)
    for hold_lot_id, stored_hold in ActiveHold.objects.filter(lot_id__in=lot_ids).values_list(
        "lot_id", "portion"
    ):
        holds[hold_lot_id].append(bounds(stored_hold))
    views: list[GrnLotView] = []
    for lot, line_key in rows:
        identity = lot.initial_identity or {}
        live = ranges.normalise(covered[lot.pk])
        condition = (
            next(iter(conditions[lot.pk]))
            if len(conditions[lot.pk]) == 1
            else str(identity.get("condition") or "good")
        )
        if len(skus[lot.pk]) == 1:
            sku_id = next(iter(skus[lot.pk]))
        else:
            sku_id = uuid.UUID(identity["sku_id"]) if identity.get("sku_id") else None
        views.append(
            GrnLotView(
                lot_id=lot.pk,
                line_key=line_key,
                qty=lot.issued_qty,
                condition=condition,
                sku_id=sku_id,
                description=str(identity.get("description") or ""),
                covered=live,
                held_uncovered=ranges.subtract(ranges.normalise(physical[lot.pk]), live),
                disposed=ranges.normalise(disposed[lot.pk]),
                held=ranges.intersect(
                    ranges.normalise(holds[lot.pk]), ranges.normalise(physical[lot.pk])
                ),
            )
        )
    return views


def grn_line_state(
    grn_document_id: uuid.UUID,
    extra_counters: Iterable[tuple[uuid.UUID, dict[str, Any]]] = (),
) -> dict[uuid.UUID, dict[str, Any]]:
    """Counted quantity and condition per GRN line after approved counter-GRNs.

    ``extra_counters`` are counter lines officialised in the running command, which
    the kernel writes only when the command commits.
    """
    head = DocumentHead.objects.filter(document_id=grn_document_id).first()
    if head is None or head.live_version_id is None:
        return {}
    state: dict[uuid.UUID, dict[str, Any]] = {}
    for line in OfficialLine.objects.filter(version_id=head.live_version_id).order_by("line_no"):
        state[line.stable_line_key] = {
            "qty": int(line.payload["qty"]),
            "condition": str(line.payload["condition"]),
            "payload": line.payload,
        }
    counters = OfficialLine.objects.filter(
        version__document_id__in=_counter_documents(grn_document_id)
    ).order_by("version__recorded_at", "line_no")
    pending = [(line.stable_line_key, line.payload) for line in counters]
    for key, payload in [*pending, *extra_counters]:
        entry = state.get(key)
        if entry is None:
            continue
        entry["qty"] += int(payload["delta"])
        entry["condition"] = str(payload["new_condition"])
    return state


def current_grn_lines(
    grn_document_id: uuid.UUID,
    extra_counters: Iterable[tuple[uuid.UUID, dict[str, Any]]] = (),
) -> list[dict[str, Any]]:
    """The GRN's lines as they stand after approved counter-GRNs, for claim comparison."""
    return [
        {
            **entry["payload"],
            "line_key": str(key),
            "qty": int(entry["qty"]),
            "condition": str(entry["condition"]),
        }
        for key, entry in grn_line_state(grn_document_id, extra_counters).items()
    ]


def effective_count(grn_document_id: uuid.UUID) -> dict[uuid.UUID, int]:
    return {key: int(entry["qty"]) for key, entry in grn_line_state(grn_document_id).items()}


def goods_grn(grn_id: uuid.UUID) -> GoodsGrn | None:
    """A GRN by its document ID (the resource ID on the wire)."""
    return GoodsGrn.objects.select_related("document", "arrival").filter(document_id=grn_id).first()


def grn_hash(grn: GoodsGrn, head: DocumentHead | None = None) -> str:
    head = head or DocumentHead.objects.select_related("live_version").get(
        document_id=grn.document_id
    )
    live = head.live_version
    claim = latest_claim(grn.arrival_id)
    return content_hash(
        {
            "version": live.content_hash if live is not None else None,
            "revision": head.revision,
            "claim": str(claim.pk) if claim else None,
        }
    )


def grn_coverage(grn: GoodsGrn) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    head = DocumentHead.objects.select_related("live_version").get(document_id=grn.document_id)
    header = dict(head.live_version.canonical_payload) if head.live_version else {}
    views = grn_custody(grn.document_id)
    damage_held = _portions_of(
        ActiveHold.objects.filter(lot_id__in=[v.lot_id for v in views], kind__in=DAMAGE_HOLD_KINDS)
    )
    reportable = reportable_damage_qty(grn)
    accepted = accepted_wrong([v.lot_id for v in views])
    # Goods ticket 13E: damaged pre-PT custody may have moved to another site,
    # or be on the road there. It is still this GRN's goods, and says where.
    from outbound.pre_pt_custody import where_now

    moved = where_now([v.lot_id for v in views], grn.document.held_site_id)
    items = []
    for key, entry in grn_line_state(grn.document_id).items():
        mine = [v for v in views if v.line_key == key]
        payload = entry["payload"]
        items.append(
            {
                "line_key": str(key),
                "counted_qty": entry["qty"],
                "covered_qty": sum(ranges.total(v.covered) for v in mine),
                # Two different facts, never the same number (GSA-T05, design
                # §5.8): nothing has put these pieces on a PT yet, versus a
                # hold is actually on them. Good, uncovered goods are not held.
                "uncovered_qty": sum(ranges.total(v.held_uncovered) for v in mine),
                "held_qty": sum(ranges.total(v.held) for v in mine),
                # Ticket 07B: no PT covers these and no hold is on them - on a good
                # line, what a supplement PT may still cover, accepted excess
                # included (overall PRD §15.2.1 rule 2). Not an ordinary PT's
                # figure: a primary never takes excess, decided or not.
                "unheld_uncovered_qty": sum(
                    ranges.total(ranges.subtract(v.held_uncovered, v.held)) for v in mine
                ),
                # Ticket 05D: of a wrong or unidentified line, the pieces a different
                # person accepted once their identity was resolved that no PT covers and
                # no hold is on - what a primary or supplement PT may cover (overall
                # PRD §15.2.1 rule 2). Zero on every other line.
                "accepted_uncovered_qty": sum(
                    ranges.total(
                        ranges.intersect(
                            ranges.subtract(v.held_uncovered, v.held), accepted.get(v.lot_id, [])
                        )
                    )
                    for v in mine
                ),
                "disposed_qty": sum(ranges.total(v.disposed) for v in mine),
                # Ticket 05C: of the held pieces, those held because somebody
                # reported them damaged - each under a report a different person
                # decides (``damage_reports``) - and how many more pieces here a
                # damage report could still take.
                "damage_held_qty": sum(
                    ranges.total(ranges.intersect(v.held, damage_held.get(v.lot_id, [])))
                    for v in mine
                ),
                "damage_reportable_qty": reportable.get(str(key), 0),
                "in_transit_qty": sum(moved[v.lot_id]["in_transit"] for v in mine),
                "elsewhere_qty": sum(moved[v.lot_id]["elsewhere"] for v in mine),
                # What the pieces on this line actually are, and how they were
                # counted. Without them a screen can show a held quantity but
                # never say *why* it is held, and cannot put the invoice beside
                # the count; the numbers alone are not the truth a receiver reads.
                "identity": payload.get("identity") or {},
                "condition": entry["condition"],
                "discrepancy_remark": payload.get("discrepancy_remark"),
            }
        )
    return header, items


def grn_invoice(grn: GoodsGrn, *, show_cost: bool = True) -> dict[str, Any] | None:
    """The arrival's current invoice claim, as the GRN detail compares against it."""
    claim = latest_claim(grn.arrival_id)
    if claim is None:
        return None
    return {
        "claim_revision_id": str(claim.pk),
        "revision": claim.revision,
        "invoice_number": grn.arrival.invoice_number,
        "invoice_date": grn.arrival.invoice_date.isoformat() if grn.arrival.invoice_date else None,
        "evidence_id": str(claim.evidence_id) if claim.evidence_id else None,
        "lines": claim_lines_for(claim.lines, show_cost=show_cost),
    }


def grn_disposition_history(grn: GoodsGrn, *, show_value: bool) -> list[dict[str, Any]]:
    """Revision-frozen disposition requests shown in the GRN discrepancy panel.

    Value, tax and damage evidence stay separate on the wire so a checker never
    has to infer which attachment proves which fact.  The frozen value appears
    only after the approval succeeded; before that the same amount is explicitly
    the requested value.
    """
    rows: list[dict[str, Any]] = []
    drafts = list(
        ActionDraft.objects.filter(
            subject_kind="disposition", payload__grn_id=str(grn.document_id)
        ).order_by("created_at", "pk")
    )
    requests: dict[str, ApprovalRequest] = {}
    for candidate in ApprovalRequest.objects.filter(
        subject_key__in=[f"action_draft:{draft.pk}" for draft in drafts]
    ).order_by("created_at"):
        requests[candidate.subject_key] = candidate
    # The frozen value is the ledger's, not the draft's: once approved, read it back from
    # the origins the approval actually created.  A draft can span several lots, so several
    # origins; sum them.  Before approval there is no origin, and the requested value - the
    # draft's own arithmetic - is the honest thing to show.
    disposition_of: dict[str, uuid.UUID] = {}
    for draft in drafts:
        approval = requests.get(f"action_draft:{draft.pk}")
        if approval is None or approval.state != ApprovalRequest.State.APPROVED:
            continue
        decision = approval.decisions.order_by("-created_at").first()
        result = (decision.result if decision is not None else None) or {}
        for disposition_id in result.get("disposition_ids") or []:
            disposition_of[str(disposition_id)] = draft.pk
    frozen: dict[uuid.UUID, int] = defaultdict(int)
    for origin in Origin.objects.filter(
        value_damage_disposition_id__in=list(disposition_of)
    ).values("value_damage_disposition_id", "opening_qty", "mrp"):
        draft_pk = disposition_of[str(origin["value_damage_disposition_id"])]
        frozen[draft_pk] += int(origin["opening_qty"]) * int(origin["mrp"])

    for draft in drafts:
        request = requests.get(f"action_draft:{draft.pk}")
        payload = dict(draft.payload.get("payload") or {})
        mrp = payload.get("approved_mrp_paise")
        total = str(int(mrp) * int(payload.get("qty") or 0)) if mrp else None
        rows.append(
            {
                "id": str(draft.pk),
                "approval_request_id": str(request.pk) if request else None,
                "kind": payload.get("kind"),
                "state": request.state if request else "pending",
                "source_line_key": payload.get("source_line_key"),
                "lot_id": payload.get("lot_id"),
                "qty": payload.get("qty"),
                "reason_code": payload.get("reason_code"),
                "damage_description": payload.get("damage_description"),
                "damage_evidence_ids": list(payload.get("evidence_ids") or []),
                "resolved_sku_id": payload.get("resolved_sku_id"),
                "source_value_evidence": {
                    "evidence_id": payload.get("approved_cost_evidence_id") if show_value else None,
                    "cost_paise": payload.get("approved_cost_paise") if show_value else None,
                    "mrp_paise": mrp if show_value else None,
                },
                "tax_basis_evidence_id": (
                    payload.get("approved_tax_evidence_id") if show_value else None
                ),
                "requested_value_paise": total if show_value else None,
                "frozen_value_paise": str(frozen[draft.pk])
                if show_value and draft.pk in frozen
                else None,
                "maker_id": str(draft.maker_id),
            }
        )
    return rows


def grn_comparison(grn: GoodsGrn) -> list[dict[str, Any]]:
    """Claimed versus counted per claim line, paired the one authoritative way.

    ``compare_claim`` spreads a claim group's counted total over its lines in
    invoice order; a screen that re-paired them itself would disagree with the
    server about which line is short. Empty when no invoice has been recorded.
    """
    claim = latest_claim(grn.arrival_id)
    if claim is None:
        return []
    comparisons, _matched = compare_claim(list(claim.lines), current_grn_lines(grn.document_id))
    return comparisons


def grn_count_history(grn: GoodsGrn) -> list[dict[str, Any]]:
    """The issued count and every counter-GRN raised against it, oldest first.

    A pending counter-GRN has no official version yet; it is listed with its draft
    corrections so a reader sees the correction that is waiting, not a silent gap.
    """
    items: list[dict[str, Any]] = []
    head = DocumentHead.objects.select_related("live_version").get(document_id=grn.document_id)
    if head.live_version is not None:
        items.append(
            {
                "document_id": str(grn.document_id),
                "kind": "grn",
                "number": grn.document.official_number,
                "state": "issued",
                "recorded_at": head.live_version.recorded_at.isoformat(),
                "lines": [
                    {
                        "line_key": str(line.stable_line_key),
                        "qty": int(line.payload["qty"]),
                        "condition": str(line.payload["condition"]),
                    }
                    for line in OfficialLine.objects.filter(version=head.live_version).order_by(
                        "line_no"
                    )
                ],
            }
        )
    drafts = (
        CounterGrnDraft.objects.select_related("document")
        .filter(grn__document_id=grn.document_id)
        .order_by("created_at", "id")
    )
    for draft in drafts:
        counter_head = DocumentHead.objects.select_related("draft_revision", "live_version").get(
            document_id=draft.document_id
        )
        live = counter_head.live_version
        if live is not None:
            lines = [
                dict(line.payload)
                for line in OfficialLine.objects.filter(version=live).order_by("line_no")
            ]
            recorded_at = live.recorded_at.isoformat()
            state = "approved"
        else:
            revision = counter_head.draft_revision
            lines = (
                [dict(row.payload) for row in revision_lines(draft.document_id, revision.revision)]
                if revision is not None
                else []
            )
            recorded_at = draft.created_at.isoformat()
            state = "pending_approval"
        items.append(
            {
                "document_id": str(draft.document_id),
                "kind": "counter_grn",
                "number": draft.document.official_number,
                "state": state,
                "recorded_at": recorded_at,
                # A counter-GRN's approval request is bound to *this* document's
                # revision, not the GRN's, and `ApprovalDTO` carries no revision
                # of its own. Without this a screen could not quote the revision
                # its approve is required to quote, and would have to guess.
                "revision": counter_head.revision,
                "lines": lines,
            }
        )
    return items


# ---------------------------------------------------------------------------
# Counter-GRN (E118 + its approval, P02)
# ---------------------------------------------------------------------------

COUNTER_LINE_KEYS = frozenset({"line_key", "new_qty", "new_condition", "reason_code"})


def parse_counter_corrections(value: Any) -> list[dict[str, Any]]:
    rows = inp.object_list(value, "corrections", limit=50_000)
    if not rows:
        raise inp.bad("A counter-GRN needs at least one correction.", "corrections")
    out = []
    for index, raw in enumerate(rows):
        field = f"corrections[{index}]"
        row = inp.closed(raw, COUNTER_LINE_KEYS, field, required=list(COUNTER_LINE_KEYS))
        if row["new_condition"] not in CONDITIONS:
            raise inp.bad(f"{field}.new_condition is not a condition.", f"{field}.new_condition")
        out.append(
            {
                "line_key": inp.uuid_value(row["line_key"], f"{field}.line_key"),
                "new_qty": inp.whole(row["new_qty"], f"{field}.new_qty", 0, 999_999),
                "new_condition": row["new_condition"],
                "reason_code": inp.text(
                    row["reason_code"], f"{field}.reason_code", 60, required=True
                ),
            }
        )
    return out


def _counter_problems(
    grn_document_id: uuid.UUID, corrections: list[dict[str, Any]]
) -> tuple[list[tuple[uuid.UUID, dict[str, Any]]], list[dict[str, Any]]]:
    state = grn_line_state(grn_document_id)
    views = grn_custody(grn_document_id)
    lines: list[tuple[uuid.UUID, dict[str, Any]]] = []
    problems: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    for correction in corrections:
        key = correction["line_key"]
        if key in seen or key not in state:
            problems.append(
                issue("UNKNOWN_LINE", "Each correction names one GRN line once.", line_key=key)
            )
            continue
        seen.add(key)
        entry = state[key]
        mine = [v for v in views if v.line_key == key]
        covered = sum(ranges.total(v.covered) for v in mine)
        removable = sum(ranges.total(v.held_uncovered) for v in mine)
        delta = correction["new_qty"] - entry["qty"]
        if delta < 0 and -delta > removable:
            problems.append(
                issue(
                    "BELOW_LIVE_COVERAGE",
                    f"Only {removable} uncovered piece(s) can be removed; "
                    f"{covered} are covered by a live PT.",
                    line_key=key,
                    quantity=removable,
                )
            )
        if correction["new_condition"] != entry["condition"] and covered:
            problems.append(
                issue(
                    "COVERED_CONDITION",
                    "The condition of quantity already covered by a live PT cannot be recounted.",
                    line_key=key,
                )
            )
        if (
            correction["new_condition"] != entry["condition"]
            and ActiveHold.objects.filter(
                lot_id__in=[v.lot_id for v in mine], kind__in=DAMAGE_HOLD_KINDS
            ).exists()
        ):
            # Ticket 05C: damage leaves its hold only through its own routes - the
            # review's rejection, or the goods' actual return or disposal (overall
            # PRD §15.2.1 rule 3). Recounting the condition underneath a damage
            # report would move held goods out of quarantine behind its back.
            problems.append(
                issue(
                    "DAMAGE_HELD",
                    "Pieces on this line are held under a damage report. If the damage was "
                    "a mistake, have the report rejected first, then correct the count.",
                    line_key=key,
                )
            )
        if delta == 0 and correction["new_condition"] == entry["condition"]:
            problems.append(issue("NO_CHANGE", "This correction changes nothing.", line_key=key))
        lines.append(
            (
                key,
                {
                    "line_key": str(key),
                    "old_qty": entry["qty"],
                    "new_qty": correction["new_qty"],
                    "delta": delta,
                    "old_condition": entry["condition"],
                    "new_condition": correction["new_condition"],
                    "reason_code": correction["reason_code"],
                },
            )
        )
    return lines, problems


def request_counter_grn(
    run: CommandRun,
    grn: GoodsGrn,
    *,
    corrections: list[dict[str, Any]],
    evidence_ids: list[uuid.UUID],
    expected_revision: int | None,
) -> DocumentIdentity:
    head = lock_heads(run, [grn.document_id])[grn.document_id]
    inp.check_revision(expected_revision, head.revision)
    if _evidence_missing(evidence_ids):
        raise inp.bad("An evidence file was not found.", "evidence_ids")
    pending = ApprovalRequest.objects.filter(
        subject_key__in=[f"document:{d}" for d in _counter_documents(grn.document_id)],
        requested_action=COUNTER_ACTION,
        state=ApprovalRequest.State.PENDING,
    ).exists()
    lines, problems = _counter_problems(grn.document_id, corrections)
    if pending:
        problems.append(
            issue("COUNTER_PENDING", "Another counter-GRN for this GRN awaits approval.")
        )
    if problems:
        raise Refusal(
            "COUNTER_GRN_UNSAFE",
            "This correction would change quantity that is covered, used or being corrected.",
            issues=problems,
        )
    source = grn.document
    identity, counter_head = new_document(
        run, kind="CGRN", purpose="counter_grn", entity_id=source.entity_id, site_id=source.site_id
    )
    header = {
        "grn_id": str(grn.document_id),
        "grn_number": source.official_number,
        "grn_revision": head.revision,
        "site_id": str(source.site_id),
        "evidence_ids": [str(e) for e in evidence_ids],
    }
    revision = append_revision(
        run, counter_head, header=header, replace_lines=[(k, v) for k, v in lines]
    )
    CounterGrnDraft.objects.create(tenant_id=run.tenant_id, document_id=identity.pk, grn_id=grn.pk)
    policy = pin(
        run,
        action=COUNTER_ACTION,
        purpose="counter_grn",
        site_id=source.site_id,
        brand_ids=[grn.arrival.brand_id],
        # A count correction changes pieces, not value: its value is unknown, never zero.
        amounts=Amounts(sum(abs(int(line["delta"])) for _key, line in lines), None),
    )
    create_request(
        run,
        subject_kind="document",
        subject_key=f"document:{identity.pk}",
        revision=counter_head.revision,
        reviewed_hash=revision.content_hash,
        requested_action=COUNTER_ACTION,
        site_id=source.site_id,
        brand_id=grn.arrival.brand_id,
        title=f"Counter-GRN for {source.official_number}",
        policy=policy,
    )
    run.audit_after = {"counter_grn_id": str(identity.pk), "grn_id": str(grn.document_id)}
    return identity


def _hold_key(lot_id: uuid.UUID, kind: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"grn-hold:{lot_id}:{kind}")


def _release_holds(
    run: CommandRun,
    plan: Plan,
    lot_id: uuid.UUID,
    interval: tuple[int, int],
    *,
    site_id: int,
    version_id: uuid.UUID,
    kinds: Iterable[str] | None = None,
) -> None:
    wanted = set(kinds) if kinds is not None else None
    for hold in list(ActiveHold.objects.filter(lot_id=lot_id, portion__overlap=portion(*interval))):
        if wanted is not None and hold.kind not in wanted:
            continue
        for piece in ranges.intersect([bounds(hold.portion)], [interval]):
            release_hold(
                run,
                plan,
                lot_id=lot_id,
                interval=piece,
                hold_key=hold.hold_key,
                site_id=site_id,
                source_version_id=version_id,
            )


def _take_from_end(
    slices: list[tuple[uuid.UUID, tuple[int, int]]], qty: int
) -> list[tuple[uuid.UUID, tuple[int, int]]]:
    out: list[tuple[uuid.UUID, tuple[int, int]]] = []
    remaining = qty
    for lot_id, (lower, upper) in slices:
        if remaining <= 0:
            break
        size = min(remaining, upper - lower)
        out.append((lot_id, (upper - size, upper)))
        remaining -= size
    if remaining > 0:
        raise Refusal("DISPOSITION_STALE", "Not enough undecided quantity remains on this line.")
    return out


def _relocate(target: uuid.UUID, condition: str | None = None) -> Callable[[Address], Address]:
    def change(old: Address) -> Address:
        return replace(old, location_id=target, condition=condition or old.condition)

    return change


def decide_counter_grn(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    request = context.request
    document_id = _subject_uuid(request.subject_key, "document")
    draft = (
        CounterGrnDraft.objects.select_related("grn", "grn__document", "grn__arrival")
        .filter(document_id=document_id)
        .first()
    )
    if draft is None:
        raise Refusal("NOT_FOUND", "That counter-GRN was not found.")
    context.access.require(COUNTER_ACTION, site_id=request.site_id, brand_id=request.brand_id)
    context.access.require_step_up()
    grn = draft.grn
    heads = lock_heads(run, [grn.document_id, document_id])
    counter_head, grn_head = heads[document_id], heads[grn.document_id]
    if counter_head.live_version_id is not None or counter_head.draft_revision is None:
        raise Refusal("STATE_CONFLICT", "This counter-GRN has already been decided.")
    if counter_head.draft_revision.content_hash != request.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The counter-GRN changed after it was submitted.")
    context.enforce_policy(run)
    if context.decision == "reject":
        record_event(
            run,
            document_id,
            "rejected",
            reason_code=context.reason_code,
            revision_id=counter_head.draft_revision.pk,
            payload={"to_state": "rejected", "details": []},
        )
        return {"state": "rejected"}
    header = dict(counter_head.draft_revision.payload)
    if header.get("grn_revision") != grn_head.revision:
        raise Refusal("COUNTER_GRN_UNSAFE", "The GRN changed after this counter-GRN was drafted.")
    site_id = grn.document.held_site_id
    _check_not_frozen(site_id)
    lock_lots(run, [lot.pk for lot, _ in grn_lots(grn.document_id)])
    drafted = revision_lines(document_id, counter_head.draft_revision.revision)
    corrections = [
        {
            "line_key": s.line_key,
            "new_qty": int(s.payload["new_qty"]),
            "new_condition": s.payload["new_condition"],
            "reason_code": s.payload["reason_code"],
        }
        for s in drafted
    ]
    lines, problems = _counter_problems(grn.document_id, corrections)
    stale = [
        s.line_key
        for s, (_key, fresh) in zip(drafted, lines, strict=False)
        if s.payload["old_qty"] != fresh["old_qty"]
        or s.payload["old_condition"] != fresh["old_condition"]
    ]
    if problems or stale:
        raise Refusal(
            "COUNTER_GRN_UNSAFE",
            "Custody changed since this counter-GRN was drafted; it can no longer be applied.",
            issues=problems,
        )
    entity = LegalEntity.objects.get(pk=grn.document.entity_id)
    number = allocate(run, entity, "CGRN")
    version, official_lines = officialise(
        run,
        counter_head,
        approved_by_id=context.checker_id,
        canonical_header=header,
        lines=[(s.line_key, s.payload) for s in drafted],
        authority=run.authority,
        number=number,
    )
    plan = Plan("P02", version.pk, event_key("P02", version.pk))
    views = grn_custody(grn.document_id)
    rows = grn_lots(grn.document_id)
    added_rows: list[tuple[uuid.UUID, str]] = []
    for official in official_lines:
        payload = official.payload
        key = official.stable_line_key
        new_condition = str(payload["new_condition"])
        target = system_location(site_id, "receiving" if new_condition == "good" else "quarantine")
        mine = [v for v in views if v.line_key == key]
        if new_condition != payload["old_condition"]:
            for view in mine:
                for interval in view.held_uncovered:
                    change_address(
                        run, plan, view.lot_id, interval, _relocate(target.pk, new_condition)
                    )
        delta = int(payload["delta"])
        if delta < 0:
            recorded = {lot.pk: lot.recorded_at for lot, _ in rows}
            slices = sorted(
                ((v.lot_id, interval) for v in mine for interval in v.held_uncovered),
                key=lambda item: (recorded[item[0]], item[1][0]),
                reverse=True,
            )
            for lot_id, interval in _take_from_end(slices, -delta):
                _release_holds(run, plan, lot_id, interval, site_id=site_id, version_id=version.pk)
                end_positions(run, plan, lot_id, interval, "external", "count_correction")
        elif delta > 0:
            parent = next(lot for lot, line_key in rows if line_key == key)
            # The counter-GRN line is this lot's source evidence, not the parent's scans.
            identity = {
                "brand_id": grn.arrival.brand_id,
                **(parent.initial_identity or {}),
                "condition": new_condition,
                "observation_ids": [],
                "counter_grn_id": str(document_id),
            }
            sku = identity.get("sku_id")
            added = open_lot(
                run,
                plan,
                source_kind="grn",
                site_id=site_id,
                qty=delta,
                identity=identity,
                source_time=run.now,
                address=Address(
                    boundary="physical",
                    site_id=site_id,
                    location_id=target.pk,
                    condition=new_condition,
                    sku_id=uuid.UUID(sku) if sku else None,
                ),
                source_line_id=official.pk,
                parent_lot_id=parent.pk,
                from_boundary="count_correction",
            )
            added_rows.append((added.pk, str(key)))
    post(run, version, plan)
    inp.save_head_revision(grn_head)
    counter_lines = [(official.stable_line_key, official.payload) for official in official_lines]
    sync_excess_holds(
        run,
        grn,
        trigger=version.pk,
        version=grn_head.live_version,
        lines=current_grn_lines(grn.document_id, counter_lines),
        lot_rows=[(lot.pk, str(line_key)) for lot, line_key in rows] + added_rows,
        lock=False,
    )
    refresh_receipt_discrepancy(
        run,
        grn,
        trigger=version.pk,
        resolved_reason="COUNTER_GRN_APPROVED",
        reopen=True,
        extra_counters=counter_lines,
    )
    refresh_awaiting_pt(
        run,
        grn,
        trigger=version.pk,
        added_custody=any(int(payload["delta"]) > 0 for _key, payload in counter_lines),
    )
    record_event(
        run,
        grn.document_id,
        "countered",
        version_id=grn_head.live_version_id,
        payload={
            "related_document_id": str(document_id),
            "official_version_id": str(version.pk),
            "details": [],
        },
    )
    # Last: it takes the GRN's advisory lock at the highest rank (ticket 37).
    three_way_match.refresh(run, grn, extra_counters=counter_lines)
    return {"counter_grn_id": str(document_id), "number": number}


# ---------------------------------------------------------------------------
# Dispositions (E119 + its approval, P03)
# ---------------------------------------------------------------------------

DISPOSITION_KEYS = frozenset(
    {
        "kind",
        "source_document_id",
        "source_line_key",
        "lot_id",
        "qty",
        "reason_code",
        "evidence_ids",
        "reviewed_grn_hash",
        "reviewed_pt_hash",
        "followup_owner_id",
        "resolved_sku_id",
        "approved_cost_evidence_id",
        "approved_tax_evidence_id",
        "approved_cost_paise",
        "approved_mrp_paise",
        "damage_description",
    }
)


def parse_disposition(body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("kind")
    if kind == "hold_damage":
        raise inp.bad(
            "Damage is reported, not decided: report it on this GRN's damage-reports route "
            "and a different person reviews it.",
            "kind",
        )
    if kind not in DISPOSITION_KINDS:
        raise inp.bad(f"kind must be one of {', '.join(DISPOSITION_KINDS)}.", "kind")

    def optional(name: str) -> str | None:
        value = inp.optional_uuid(body.get(name), name)
        return str(value) if value else None

    reviewed_pt = body.get("reviewed_pt_hash")
    if reviewed_pt is not None and (not isinstance(reviewed_pt, str) or len(reviewed_pt) != 64):
        raise inp.bad("reviewed_pt_hash must be a 64-character hash.", "reviewed_pt_hash")
    reviewed_grn = body.get("reviewed_grn_hash")
    if not isinstance(reviewed_grn, str) or len(reviewed_grn) != 64:
        raise inp.bad("reviewed_grn_hash must be a 64-character hash.", "reviewed_grn_hash")
    return {
        "kind": kind,
        "source_document_id": str(
            inp.uuid_value(body.get("source_document_id"), "source_document_id")
        ),
        "source_line_key": str(inp.uuid_value(body.get("source_line_key"), "source_line_key")),
        "lot_id": optional("lot_id"),
        "qty": inp.quantity(body.get("qty"), "qty"),
        "reason_code": inp.text(body.get("reason_code"), "reason_code", 60, required=True),
        "evidence_ids": [str(e) for e in inp.id_list(body.get("evidence_ids"), "evidence_ids")],
        "reviewed_grn_hash": reviewed_grn,
        "reviewed_pt_hash": reviewed_pt,
        "followup_owner_id": optional("followup_owner_id"),
        "resolved_sku_id": optional("resolved_sku_id"),
        "approved_cost_evidence_id": optional("approved_cost_evidence_id"),
        "approved_tax_evidence_id": optional("approved_tax_evidence_id"),
        "approved_cost_paise": inp.money(body.get("approved_cost_paise"), "approved_cost_paise"),
        "approved_mrp_paise": inp.money(body.get("approved_mrp_paise"), "approved_mrp_paise"),
        "damage_description": inp.text(body.get("damage_description"), "damage_description", 1000),
    }


def disposition_policy(
    kind: str, at: datetime, *, site_id: int, brand_id: int | None
) -> ConfigVersion | None:
    """The one effective approval policy for this disposition kind here, or ``None``.
    More than one matching policy fails closed."""
    target = ConfigTarget.of(at, site_id=site_id, brand_ids=[brand_id], purpose=kind)
    try:
        return resolve(
            require_tenant_id(),
            "approval",
            target,
            match={"action": DISPOSITION_ACTION},
            code="APPROVAL_POLICY_BLOCKED",
            path="approval_policy",
        )
    except Refusal as refusal:
        if (refusal.issues or [{}])[0].get("code") != "CONFIG_MISSING":
            raise
        return None


def disposition_request_actions(kind: str) -> tuple[str, ...]:
    """The grants that may ask for a disposition of ``kind`` at a GRN's site (E119)."""
    return DISPOSITION_REQUEST_ACTIONS.get(kind, (DISPOSITION_ACTION,))


def disposition_needs_checker(
    kind: str, at: datetime, *, site_id: int, brand_id: int | None
) -> bool:
    """The effective approval configuration for this site, brand and kind decides; with none
    the Phase 1 default applies. ``value_damage`` and ``accept_wrong`` always need a distinct
    approver (overall PRD §15.2.1 rules 4 and 2), whatever a policy says."""
    if kind in ALWAYS_CHECKED_KINDS:
        return True
    policy = disposition_policy(kind, at, site_id=site_id, brand_id=brand_id)
    if policy is None:
        return kind in CHECKER_KINDS
    return bool(policy.payload.get("require_distinct", True))


_UNSET: Any = object()


def grn_discrepancies(
    grn: GoodsGrn,
    *,
    claim_lines: list[dict[str, Any]] | None = _UNSET,
    extra_counters: Iterable[tuple[uuid.UUID, dict[str, Any]]] = (),
) -> tuple[dict[str, int], set[str]]:
    """Quantity needing a decision against the arrival's current claim and the GRN's lines
    after approved counter-GRNs - never only the claim frozen when the GRN was issued."""
    if claim_lines is _UNSET:
        claim = latest_claim(grn.arrival_id)
        claim_lines = list(claim.lines) if claim else None
    return discrepancies(current_grn_lines(grn.document_id, extra_counters), claim_lines)


def grn_source_documents(grn: GoodsGrn) -> set[uuid.UUID]:
    """The GRN and its receipt PTs: the documents a decision on this GRN may cite."""
    goods_pt = apps.get_model("ptmapper", "GoodsPt")
    return {
        grn.document_id,
        *goods_pt.objects.filter(grn_id=grn.pk).values_list("document_id", flat=True),
    }


def _decided_portions(
    lot_ids: list[uuid.UUID],
    extra_rows: Iterable[Disposition] = (),
    kinds: frozenset[str] | None = None,
) -> dict[uuid.UUID, list[tuple[int, int]]]:
    """Portions already carrying a decision, per lot (each unit counts once).

    A damage hold whose report a different person rejected decides nothing any
    more (ticket 05C): the pieces went back to being ordinary goods, so whatever
    the count said about them is undecided again.
    """
    found: dict[uuid.UUID, list[tuple[int, int]]] = defaultdict(list)
    rows = Disposition.objects.filter(lot_id__in=lot_ids, portion__isnull=False)
    if kinds is not None:
        rows = rows.filter(kind__in=sorted(kinds))
    listed = list(rows.values_list("pk", "lot_id", "portion", "kind", "document_id"))
    withdrawn = _rejected_damage_holds(
        {document for _pk, _lot, _p, kind, document in listed if kind == "hold_damage"}
    )
    for pk, lot_id, stored, _kind, _document in listed:
        if pk not in withdrawn:
            found[lot_id].append(bounds(stored))
    wanted = set(lot_ids)
    for row in extra_rows:
        if row.lot_id in wanted and row.portion is not None:
            if kinds is None or row.kind in kinds:
                found[row.lot_id].append(bounds(row.portion))
    return {lot_id: ranges.normalise(pieces) for lot_id, pieces in found.items()}


def _rejected_damage_holds(document_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """The receipt damage-hold records whose report was rejected, on these documents."""
    if not document_ids:
        return set()
    from outbound.goods_models import DamageReport

    out: set[uuid.UUID] = set()
    for lines in DamageReport.objects.filter(
        state=DamageReport.State.REJECTED,
        disposition__document_id__in=sorted(document_ids, key=str),
    ).values_list("lines", flat=True):
        out |= {
            uuid.UUID(str(line["disposition_id"])) for line in lines if line.get("disposition_id")
        }
    return out


def undecided_discrepancies(
    grn: GoodsGrn,
    *,
    claim_lines: list[dict[str, Any]] | None = _UNSET,
    extra_rows: Iterable[Disposition] = (),
    extra_counters: Iterable[tuple[uuid.UUID, dict[str, Any]]] = (),
) -> tuple[dict[str, int], set[str]]:
    """Discrepant quantity no decision on THIS GRN covers yet, with the discrepancy reasons.

    Physical quantity is decided by distinct units of this GRN's own lots (units removed by
    a counter-GRN no longer count); a shortage is decided by accepted shortages that cite
    this GRN or its receipt PT.
    """
    extra = list(extra_rows)
    needed, reasons = grn_discrepancies(grn, claim_lines=claim_lines, extra_counters=extra_counters)
    if not needed:
        return {}, reasons
    lots_by_line: dict[str, list[uuid.UUID]] = defaultdict(list)
    for lot, line_key in grn_lots(grn.document_id):
        lots_by_line[str(line_key)].append(lot.pk)
    lot_ids = [lot_id for lots in lots_by_line.values() for lot_id in lots]
    decided = _decided_portions(lot_ids, extra)
    removed: dict[uuid.UUID, list[tuple[int, int]]] = defaultdict(list)
    for lot_id, stored in Position.objects.filter(
        lot_id__in=lot_ids, boundary="external"
    ).values_list("lot_id", "portion"):
        removed[lot_id].append(bounds(stored))
    documents = grn_source_documents(grn)
    shortage: dict[str, int] = defaultdict(int)
    for claim_key, accepted in Disposition.objects.filter(
        kind=Disposition.Kind.ACCEPT_SHORTAGE,
        lot__isnull=True,
        document_id__in=sorted(documents, key=str),
        source_line_key__in=[uuid.UUID(k) for k in needed if k not in lots_by_line],
    ).values_list("source_line_key", "qty"):
        shortage[str(claim_key)] += accepted
    for row in extra:
        if row.kind == Disposition.Kind.ACCEPT_SHORTAGE and row.document_id in documents:
            shortage[str(row.source_line_key)] += row.qty
    remaining: dict[str, int] = {}
    for needed_key, qty in needed.items():
        done: int
        if needed_key in lots_by_line:
            done = sum(
                ranges.total(
                    ranges.subtract(decided.get(lot_id, []), ranges.normalise(removed[lot_id]))
                )
                for lot_id in lots_by_line[needed_key]
            )
        else:
            done = shortage[needed_key]
        if done < qty:
            remaining[needed_key] = qty - done
    return remaining, reasons


Pieces = dict[uuid.UUID, list[tuple[int, int]]]


def _lot_rows(grn_document_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    return [(lot.pk, str(key)) for lot, key in grn_lots(grn_document_id)]


def _by_line(lot_rows: Iterable[tuple[uuid.UUID, str]]) -> dict[str, list[uuid.UUID]]:
    out: dict[str, list[uuid.UUID]] = defaultdict(list)
    for lot_id, line_key in lot_rows:
        out[str(line_key)].append(lot_id)
    return out


def _portions_of(queryset: Any) -> Pieces:
    out: Pieces = defaultdict(list)
    for lot_id, stored in queryset.values_list("lot_id", "portion"):
        out[lot_id].append(bounds(stored))
    return {lot_id: ranges.normalise(pieces) for lot_id, pieces in out.items()}


def _settled_units(
    lot_ids: list[uuid.UUID], kinds: frozenset[str], extra_rows: Iterable[Disposition] = ()
) -> Pieces:
    """Portions carrying a decision of ``kinds``, less units a counter-GRN removed."""
    decided = _decided_portions(lot_ids, extra_rows, kinds=kinds)
    removed = _portions_of(Position.objects.filter(lot_id__in=lot_ids, boundary="external"))
    return {
        lot_id: ranges.subtract(pieces, removed.get(lot_id, []))
        for lot_id, pieces in decided.items()
    }


def _total(pieces: Pieces, lot_ids: Iterable[uuid.UUID]) -> int:
    return sum(ranges.total(pieces.get(lot_id, [])) for lot_id in lot_ids)


def _claim_lines_of(grn: GoodsGrn, claim_lines: Any) -> list[dict[str, Any]] | None:
    if claim_lines is not _UNSET:
        return list(claim_lines) if claim_lines is not None else None
    claim = latest_claim(grn.arrival_id)
    return list(claim.lines) if claim else None


def counted_excess(
    grn: GoodsGrn,
    *,
    lines: list[dict[str, Any]] | None = None,
    claim_lines: list[dict[str, Any]] | None = _UNSET,
) -> dict[str, int]:
    """Good pieces counted above the arrival's current claim, per GRN line (``excess_by_line``).

    Without a claim nothing is excess. The GRN's lines are taken after approved counter-GRNs.
    """
    claimed = _claim_lines_of(grn, claim_lines)
    if claimed is None:
        return {}
    return excess_by_line(
        lines if lines is not None else current_grn_lines(grn.document_id), claimed
    )


def excess_room(grn: GoodsGrn, line_key: str, kinds: frozenset[str]) -> int:
    """Counted excess on one line that no decision of ``kinds`` has settled yet."""
    excess = counted_excess(grn).get(line_key, 0)
    if excess <= 0:
        return 0
    lot_ids = _by_line(_lot_rows(grn.document_id)).get(line_key, [])
    return max(excess - _total(_settled_units(lot_ids, kinds), lot_ids), 0)


def undecided_excess(grn: GoodsGrn) -> dict[str, int]:
    """Excess per GRN line that is neither settled by a decision nor under the excess hold.

    ``sync_excess_holds`` keeps undecided excess on hold, so this is normally empty; it stays
    as a quantity check for pieces that could not be held yet (overall PRD §15.2.1 rule 3).
    """
    excess = counted_excess(grn)
    if not excess:
        return {}
    by_line = _by_line(_lot_rows(grn.document_id))
    lot_ids = [lot_id for key in excess for lot_id in by_line.get(key, [])]
    settled = _settled_units(lot_ids, SETTLING_EXCESS)
    held = _portions_of(ActiveHold.objects.filter(lot_id__in=lot_ids, kind=EXCESS_HOLD))
    out: dict[str, int] = {}
    for key, qty in excess.items():
        done = sum(
            ranges.total(ranges.normalise([*settled.get(lot, []), *held.get(lot, [])]))
            for lot in by_line.get(key, [])
        )
        if qty > done:
            out[key] = qty - done
    return out


def accepted_excess(lot_ids: list[uuid.UUID]) -> Pieces:
    """Portions an approved ``accept_excess`` released: coverable by a supplement only."""
    return _decided_portions(lot_ids, kinds=frozenset({"accept_excess"}))


def sync_excess_holds(
    run: CommandRun,
    grn: GoodsGrn,
    *,
    trigger: Any,
    version: Any = None,
    lines: list[dict[str, Any]] | None = None,
    claim_lines: list[dict[str, Any]] | None = _UNSET,
    lot_rows: list[tuple[uuid.UUID, str]] | None = None,
    extra_rows: Iterable[Disposition] = (),
    lock: bool = True,
) -> None:
    """Keep every undecided excess piece under the receipt excess hold (§15.2.1 rule 3).

    Per GRN line the hold covers its counted excess less pieces already accepted, returned or
    disposed. Missing holds go on the line's latest uncovered good pieces; holds no longer
    needed (a larger invoice, a decision) come off pieces no ``hold_excess`` decision holds.
    Run whenever excess or uncovered custody changes: GRN issue, a new invoice claim, a
    counter-GRN, an excess decision and a PT reversal. Nothing is relocated or valued.
    """
    rows = lot_rows if lot_rows is not None else _lot_rows(grn.document_id)
    lot_ids = [lot_id for lot_id, _key in rows]
    if not lot_ids:
        return
    if lock:
        lock_lots(run, lot_ids)
    extra = list(extra_rows)
    excess = counted_excess(grn, lines=lines, claim_lines=claim_lines)
    holds = _portions_of(ActiveHold.objects.filter(lot_id__in=lot_ids, kind=EXCESS_HOLD))
    if not excess and not holds:
        return
    settled = _settled_units(lot_ids, SETTLING_EXCESS, extra)
    decided_holds = _decided_portions(lot_ids, extra, kinds=frozenset({"hold_excess"}))
    covered = _portions_of(LiveCoverage.objects.filter(lot_id__in=lot_ids))
    good = _portions_of(
        Position.objects.filter(lot_id__in=lot_ids, boundary="physical", condition="good")
    )
    if version is None:
        version = (
            DocumentHead.objects.select_related("live_version")
            .get(document_id=grn.document_id)
            .live_version
        )
    site_id = grn.document.held_site_id
    plan = Plan("P03", version.pk, event_key("P03", "excess_hold", grn.document_id, trigger))
    order = {lot_id: index for index, lot_id in enumerate(lot_ids)}
    for key, lots in _by_line(rows).items():
        target = max(excess.get(key, 0) - _total(settled, lots), 0)
        shortfall = target - _total(holds, lots)
        if shortfall > 0:
            free = [
                (lot, piece)
                for lot in lots
                for piece in ranges.subtract(
                    good.get(lot, []),
                    [*holds.get(lot, []), *covered.get(lot, []), *settled.get(lot, [])],
                )
            ]
            free.sort(key=lambda item: (order[item[0]], item[1][0]), reverse=True)
            for lot, piece in _take_up_to(free, shortfall, from_end=True):
                place_hold(
                    run,
                    plan,
                    lot_id=lot,
                    interval=piece,
                    hold_key=_hold_key(lot, "excess"),
                    kind=EXCESS_HOLD,
                    site_id=site_id,
                    source_version_id=version.pk,
                )
        elif shortfall < 0:
            spare = [
                (lot, piece)
                for lot in lots
                for piece in ranges.subtract(holds.get(lot, []), decided_holds.get(lot, []))
            ]
            spare.sort(key=lambda item: (order[item[0]], item[1][0]))
            for lot, piece in _take_up_to(spare, -shortfall, from_end=False):
                release_hold(
                    run,
                    plan,
                    lot_id=lot,
                    interval=piece,
                    hold_key=_hold_key(lot, "excess"),
                    site_id=site_id,
                    source_version_id=version.pk,
                )
    post(run, version, plan)


def _take_up_to(
    slices: list[tuple[uuid.UUID, tuple[int, int]]], qty: int, *, from_end: bool
) -> list[tuple[uuid.UUID, tuple[int, int]]]:
    """Up to ``qty`` pieces from ``slices`` in order, cutting each at its end or its start."""
    out: list[tuple[uuid.UUID, tuple[int, int]]] = []
    remaining = qty
    for lot_id, (lower, upper) in slices:
        if remaining <= 0:
            break
        size = min(remaining, upper - lower)
        out.append((lot_id, (upper - size, upper) if from_end else (lower, lower + size)))
        remaining -= size
    return out


def refresh_receipt_discrepancy(
    run: CommandRun,
    grn: GoodsGrn,
    *,
    trigger: Any,
    resolved_reason: str,
    reopen: bool,
    claim_lines: list[dict[str, Any]] | None = _UNSET,
    extra_rows: Iterable[Disposition] = (),
    extra_counters: Iterable[tuple[uuid.UUID, dict[str, Any]]] = (),
) -> None:
    """Close ``receipt_discrepancy`` once every discrepant unit is decided; reopen it when a
    later claim or counter-GRN leaves undecided quantity again."""
    remaining, reasons = undecided_discrepancies(
        grn, claim_lines=claim_lines, extra_rows=extra_rows, extra_counters=extra_counters
    )
    subject = f"grn:{grn.document_id}"
    if not remaining:
        resolve_exceptions(
            run, kind="receipt_discrepancy", subject_key=subject, reason_code=resolved_reason
        )
        return
    already_open = GoodsException.objects.filter(
        tenant_id=run.tenant_id, kind="receipt_discrepancy", subject_key=subject, state="open"
    ).exists()
    if reopen and not already_open:
        open_exception(
            run,
            kind="receipt_discrepancy",
            site_id=grn.document.site_id,
            subject_key=subject,
            reason_code="+".join(sorted(reasons))[:60] or "DISCREPANCY",
            source_event_key=event_key("receipt_discrepancy", grn.document_id, trigger),
            allowed_resolution_actions=[
                "inbound/grns/{id}/dispositions",
                "inbound/grns/{id}/counter",
            ],
        )


def refresh_awaiting_pt(
    run: CommandRun, grn: GoodsGrn, *, trigger: Any, added_custody: bool
) -> None:
    """``grn_awaiting_pt`` closes when no uncovered custody is left for a PT to cover, and
    opens again when a counter-GRN adds custody."""
    subject = f"grn:{grn.document_id}"
    already_open = GoodsException.objects.filter(
        tenant_id=run.tenant_id, kind="grn_awaiting_pt", subject_key=subject, state="open"
    ).exists()
    if added_custody:
        if not already_open:
            open_exception(
                run,
                kind="grn_awaiting_pt",
                site_id=grn.document.site_id,
                subject_key=subject,
                reason_code="COUNTER_GRN_ADDED",
                source_event_key=event_key("grn_awaiting_pt", grn.document_id, trigger),
                allowed_resolution_actions=["ptmapper/files/from-grn/{id}"],
            )
        return
    held = sum(ranges.total(view.held_uncovered) for view in grn_custody(grn.document_id))
    if already_open and held == 0:
        resolve_exceptions(
            run, kind="grn_awaiting_pt", subject_key=subject, reason_code="NO_UNCOVERED_CUSTODY"
        )


def _disposition_problems(grn: GoodsGrn, payload: dict[str, Any]) -> None:
    kind = payload["kind"]
    problems: list[dict[str, Any]] = []
    needs_identity = kind in ("accept_excess", "value_damage", "resolve_identity", "accept_wrong")
    # R-REC-005: accepting discrepant goods into inventory needs resolved identity and
    # approved cost/tax evidence - good excess and wrong or unidentified goods alike
    # (quarantine outcomes §6 gives both the same route).
    needs_value = kind in ("accept_excess", "value_damage", "accept_wrong")
    if needs_identity and not payload["resolved_sku_id"]:
        problems.append(
            issue("IDENTITY_REQUIRED", "A resolved SKU is required.", field="resolved_sku_id")
        )
    if needs_value and not payload["approved_cost_evidence_id"]:
        problems.append(
            issue(
                "COST_EVIDENCE_REQUIRED",
                "Approved source value evidence is required.",
                field="approved_cost_evidence_id",
            )
        )
    if needs_value and not payload["approved_tax_evidence_id"]:
        problems.append(
            issue(
                "TAX_BASIS_REQUIRED",
                "Approved tax-basis evidence is required.",
                field="approved_tax_evidence_id",
            )
        )
    if kind == "value_damage":
        cost = int(payload["approved_cost_paise"] or 0)
        mrp = int(payload["approved_mrp_paise"] or 0)
        if cost <= 0:
            problems.append(
                issue(
                    "COST_REQUIRED",
                    "A positive evidenced unit cost is required.",
                    field="approved_cost_paise",
                )
            )
        if mrp <= 0:
            problems.append(
                issue(
                    "MRP_REQUIRED",
                    "A positive evidenced ticket MRP is required.",
                    field="approved_mrp_paise",
                )
            )
        if cost > 0 and mrp > 0 and cost > mrp:
            problems.append(
                issue(
                    "COST_ABOVE_MRP",
                    "The evidenced cost cannot exceed ticket MRP.",
                    field="approved_cost_paise",
                )
            )
        if not payload["damage_description"]:
            problems.append(
                issue(
                    "DAMAGE_DESCRIPTION_REQUIRED",
                    "Describe the damage before asking to keep and value it.",
                    field="damage_description",
                )
            )
        if not payload["evidence_ids"]:
            problems.append(
                issue(
                    "DAMAGE_EVIDENCE_REQUIRED",
                    "Supporting damage evidence is required.",
                    field="evidence_ids",
                )
            )
    if problems:
        raise Refusal(
            "DISPOSITION_NOT_AUTHORISED",
            "This decision is missing required identity, value, tax or damage evidence.",
            status=403,
            issues=problems,
        )
    if (
        payload["resolved_sku_id"]
        and not ProductSku.objects.filter(pk=payload["resolved_sku_id"]).exists()
    ):
        raise inp.bad("No SKU has that ID.", "resolved_sku_id")
    if kind == "accept_wrong":
        unsettled = _unsettled_identity(uuid.UUID(payload["resolved_sku_id"]))
        if unsettled:
            raise Refusal(
                "DISPOSITION_NOT_AUTHORISED",
                "Wrong or unidentified goods are accepted only once their identity and "
                "profile-defining attributes are settled.",
                status=403,
                issues=unsettled,
            )
    if (
        payload["followup_owner_id"]
        and not HumanIdentity.objects.filter(pk=payload["followup_owner_id"]).exists()
    ):
        raise inp.bad("No person has that ID.", "followup_owner_id")
    # Damage, source value and tax are three separate proofs (GSA-T09), so each group
    # is resolved on its own: a file the checker cannot find, or one declared for other
    # goods, is named against the field that actually carries it.
    groups = [
        ("evidence_ids", "DAMAGE_EVIDENCE", [uuid.UUID(e) for e in payload["evidence_ids"] if e]),
        (
            "approved_cost_evidence_id",
            "COST_EVIDENCE",
            [uuid.UUID(payload["approved_cost_evidence_id"])]
            if payload["approved_cost_evidence_id"]
            else [],
        ),
        (
            "approved_tax_evidence_id",
            "TAX_EVIDENCE",
            [uuid.UUID(payload["approved_tax_evidence_id"])]
            if payload["approved_tax_evidence_id"]
            else [],
        ),
    ]
    for field, _code, ids in groups:
        if ids and _evidence_missing(ids):
            raise inp.bad("An evidence file was not found.", field)
    site_id = grn.document.site_id
    brand_id = grn.arrival.brand_id
    scopes = {
        row.pk: row.scope or {}
        for row in EvidenceObject.objects.filter(
            pk__in=[found for _f, _c, ids in groups for found in ids]
        )
    }
    unscoped = [
        issue(
            f"{code}_OUT_OF_SCOPE",
            "That evidence is not declared for the site and brand of these goods.",
            field=field,
        )
        for field, code, ids in groups
        if any(i in scopes and not covers_cell(scopes[i], site_id, brand_id) for i in ids)
    ]
    if unscoped:
        raise Refusal(
            "DISPOSITION_NOT_AUTHORISED",
            "Evidence must be declared for the site and brand of these goods.",
            status=403,
            issues=unscoped,
        )
    source = payload["source_document_id"]
    if (
        source != str(grn.document_id)
        and not DocumentIdentity.objects.filter(
            pk=source, purpose=DocumentIdentity.Purpose.RECEIPT, site_id=grn.document.site_id
        ).exists()
    ):
        raise inp.bad(
            "source_document_id must be this GRN or its receipt PT.", "source_document_id"
        )


def _unsettled_identity(sku_id: uuid.UUID) -> list[dict[str, Any]]:
    """Why this SKU is not yet a settled identity to accept goods as (overall PRD
    §15.2.1 rule 2): it must be an effective SKU, and every attribute its identity
    profile defines - size aside, as the identity rules allow - must be known."""
    from masters.goods_identity_services import profile_from_version

    sku = ProductSku.objects.select_related("identity_profile").get(pk=sku_id)
    if sku.governance_state != "effective":
        return [
            issue(
                "IDENTITY_NOT_SETTLED",
                "The SKU is not an effective item yet; it cannot be what the goods are.",
                field="resolved_sku_id",
            )
        ]
    if sku.identity_profile is None:
        return []
    profile = profile_from_version(sku.identity_profile)
    if profile is None:
        return []
    known = {
        str(entry.get("field_id"))
        for entry in sku.attrs or []
        if isinstance(entry, dict)
        and (entry.get("vocabulary_value_id") or entry.get("supplied_text") is not None)
    }
    return [
        issue(
            "DEFINING_ATTRIBUTE_UNKNOWN",
            f"The SKU's defining {dimension} is not known.",
            field="resolved_sku_id",
        )
        for dimension in profile.distinguishing
        if dimension != profile.size_dimension and dimension not in known
    ]


def _wrong_goods_portions(
    grn: GoodsGrn, payload: dict[str, Any], views: list[GrnLotView]
) -> list[tuple[uuid.UUID, tuple[int, int]]]:
    """The wrong or unidentified pieces ``accept_wrong`` takes (ticket 05D).

    Physically here, on no live PT, counted wrong or unidentified, already named as the
    resolved SKU (by the count or by ``resolve_identity`` - naming them is the
    precondition, not a decision this one overrides) and accepted by nothing yet.
    Damaged pieces, or pieces under a damage hold, are never taken: their damage
    hold stays in force and they never join an ordinary PT (quarantine outcomes §6).
    """
    lot_ids = [v.lot_id for v in views]
    covering = _decided_portions(lot_ids, kinds=COVERING_KINDS - {"resolve_identity"})
    damage_held = _portions_of(
        ActiveHold.objects.filter(lot_id__in=lot_ids, kind__in=DAMAGE_HOLD_KINDS)
    )
    recorded = dict(CustodyLot.objects.filter(pk__in=lot_ids).values_list("pk", "recorded_at"))
    unresolved = other_sku = damaged = 0
    slices: list[tuple[uuid.UUID, tuple[int, int]]] = []
    for view in views:
        for position in Position.objects.filter(lot_id=view.lot_id, boundary="physical"):
            free = ranges.intersect([bounds(position.portion)], view.held_uncovered)
            free = ranges.subtract(free, covering.get(view.lot_id, []))
            if not free:
                continue
            if position.condition == "damaged":
                damaged += ranges.total(free)
                continue
            if position.condition not in WRONG_CONDITIONS:
                continue
            damaged += ranges.total(ranges.intersect(free, damage_held.get(view.lot_id, [])))
            free = ranges.subtract(free, damage_held.get(view.lot_id, []))
            if position.sku_id is None:
                unresolved += ranges.total(free)
            elif str(position.sku_id) != payload["resolved_sku_id"]:
                other_sku += ranges.total(free)
            else:
                slices.extend((view.lot_id, piece) for piece in free)
    slices.sort(key=lambda item: (recorded[item[0]], item[1][0]), reverse=True)
    available = sum(ranges.length(piece) for _lot, piece in slices)
    if available < payload["qty"]:
        if unresolved:
            raise Refusal(
                "DISPOSITION_NOT_AUTHORISED",
                "Name the goods first: wrong or unidentified goods are accepted only after "
                "their identity is resolved.",
                status=403,
                issues=[
                    issue(
                        "IDENTITY_UNRESOLVED",
                        "These pieces have no resolved SKU yet.",
                        field="resolved_sku_id",
                        quantity=unresolved,
                    )
                ],
            )
        if other_sku:
            raise Refusal(
                "DISPOSITION_NOT_AUTHORISED",
                "The goods are accepted as the SKU they were named as; accepting them as "
                "another would relabel them.",
                status=403,
                issues=[
                    issue(
                        "SKU_MISMATCH",
                        "The pieces are named as a different SKU.",
                        field="resolved_sku_id",
                        quantity=other_sku,
                    )
                ],
            )
        if damaged:
            raise Refusal(
                "DISPOSITION_NOT_AUTHORISED",
                "Damaged goods keep their damage hold. Accepting a discrepancy never puts "
                "them on an ordinary PT; value_damage is their valuation route.",
                status=403,
                issues=[
                    issue(
                        "DAMAGE_STAYS_HELD",
                        "These pieces are damaged or under a damage hold.",
                        field="kind",
                        quantity=damaged,
                    )
                ],
            )
        raise Refusal(
            "DISPOSITION_STALE",
            f"Only {available} wrong or unidentified piece(s) on this line are still "
            "waiting to be accepted.",
            issues=[
                issue(
                    "WRONG_GOODS_NOT_AVAILABLE",
                    "Not enough wrong or unidentified pieces",
                    quantity=available,
                )
            ],
        )
    return _take_from_end(slices, payload["qty"])


def _accepted_as_good(location_id: uuid.UUID | None) -> Callable[[Address], Address]:
    """Accepted wrong goods stand as good goods, at ``location_id`` or where they are."""

    def change(old: Address) -> Address:
        return replace(old, condition="good", location_id=location_id or old.location_id)

    return change


def accepted_wrong(lot_ids: list[uuid.UUID]) -> Pieces:
    """Portions an approved ``accept_wrong`` made eligible for an ordinary receipt PT."""
    return _decided_portions(lot_ids, kinds=frozenset({"accept_wrong"}))


def _select_portions(
    grn: GoodsGrn, payload: dict[str, Any]
) -> list[tuple[uuid.UUID, tuple[int, int]]]:
    """The exact unvalued portions a physical decision affects, or DISPOSITION_STALE."""
    kind = payload["kind"]
    key = uuid.UUID(payload["source_line_key"])
    if kind == "accept_shortage":
        claim = latest_claim(grn.arrival_id)
        claim_keys = {line["line_key"] for line in (claim.lines if claim else [])}
        remaining, _reasons = undecided_discrepancies(grn)
        if (
            payload["source_line_key"] not in claim_keys
            or remaining.get(payload["source_line_key"], 0) < payload["qty"]
        ):
            raise Refusal(
                "DISPOSITION_STALE", "Not enough undecided shortage remains on this line."
            )
        return []
    views = [v for v in grn_custody(grn.document_id) if v.line_key == key]
    if not views:
        raise Refusal("DISPOSITION_STALE", "That GRN line has no custody to decide.")
    if payload["lot_id"]:
        views = [v for v in views if str(v.lot_id) == payload["lot_id"]]
        if not views:
            raise Refusal("DISPOSITION_STALE", "That lot does not belong to this GRN line.")
    if kind == "accept_wrong":
        return _wrong_goods_portions(grn, payload, views)
    # A receipt damage hold is placed only by reporting the damage (the GRN for damage it
    # counted, ``report_receipt_damage`` for damage found later); value_damage may only
    # value pieces already under that hold. The excess hold is also kept automatically
    # (``sync_excess_holds``), so hold_excess takes pieces no earlier hold_excess decided.
    hold_kind = "damage" if kind == "value_damage" else None
    recorded = dict(
        CustodyLot.objects.filter(pk__in=[v.lot_id for v in views]).values_list("pk", "recorded_at")
    )
    covering = _decided_portions(
        [v.lot_id for v in views],
        kinds=COVERING_KINDS | ({"hold_excess"} if kind == "hold_excess" else set()),
    )
    other_sku = 0
    unheld_damage = 0
    damaged_elsewhere = 0
    slices: list[tuple[uuid.UUID, tuple[int, int]]] = []
    for view in views:
        if not view.held_uncovered:
            continue
        held = (
            [
                bounds(h.portion)
                for h in ActiveHold.objects.filter(
                    lot_id=view.lot_id, hold_key=_hold_key(view.lot_id, hold_kind)
                )
            ]
            if hold_kind
            else []
        )
        for position in Position.objects.filter(lot_id=view.lot_id, boundary="physical"):
            interval = bounds(position.portion)
            if kind == "value_damage" and position.condition != "damaged":
                continue
            if kind in ("return", "dispose") and position.condition == "damaged":
                # Ticket 05C: damaged goods stay in quarantine under their damage
                # report. Actually returning or disposing of them is the physical
                # route of tickets 15G and 15E; a receiving decision that ended the
                # pieces here would only pretend it had happened.
                damaged_elsewhere += ranges.total(ranges.intersect([interval], view.held_uncovered))
                continue
            if kind in ("hold_excess", "accept_excess") and position.condition != "good":
                continue
            if kind == "resolve_identity" and not (
                position.condition in ("wrong", "unidentified") or position.sku_id is None
            ):
                continue
            available = ranges.intersect([interval], view.held_uncovered)
            if kind == "value_damage":
                # Only pieces already under the damage hold may be valued (overall PRD
                # §15.2.1 rule 4); only reporting the damage places that hold.
                free = ranges.intersect(available, held)
                unheld_damage += ranges.total(ranges.subtract(available, held))
            else:
                free = ranges.subtract(available, held)
            free = ranges.subtract(free, covering.get(view.lot_id, []))
            if (
                kind in ("accept_excess", "value_damage")
                and position.sku_id is not None
                and str(position.sku_id) != payload["resolved_sku_id"]
            ):
                other_sku += ranges.total(free)
                continue
            slices.extend((view.lot_id, piece) for piece in free)
    slices.sort(key=lambda item: (recorded[item[0]], item[1][0]), reverse=True)
    if other_sku and sum(ranges.length(piece) for _lot, piece in slices) < payload["qty"]:
        raise Refusal(
            "DISPOSITION_NOT_AUTHORISED",
            "The decision must use the SKU the goods were counted as; "
            "use resolve_identity for wrong goods.",
            status=403,
            issues=[
                issue(
                    "SKU_MISMATCH",
                    "The counted goods already carry a different SKU.",
                    field="resolved_sku_id",
                )
            ],
        )
    if damaged_elsewhere and sum(ranges.length(piece) for _lot, piece in slices) < payload["qty"]:
        raise Refusal(
            "DISPOSITION_NOT_AUTHORISED",
            "Damaged goods are not returned or disposed of from receiving. They stay in "
            "quarantine under their damage report until they are actually handed back to "
            "the vendor or disposed of through that route.",
            status=403,
            issues=[
                issue(
                    "DAMAGE_OUTCOME_ELSEWHERE",
                    "Damaged pieces leave through the return-to-vendor or disposal route.",
                    field="kind",
                    quantity=damaged_elsewhere,
                )
            ],
        )
    if unheld_damage and sum(ranges.length(piece) for _lot, piece in slices) < payload["qty"]:
        raise Refusal(
            "DISPOSITION_NOT_AUTHORISED",
            "Damaged goods must be placed under their damage hold before they can be "
            "valued; report the damage on this GRN first.",
            status=403,
            issues=[
                issue(
                    "DAMAGE_HOLD_REQUIRED",
                    "This portion is not yet under its damage hold.",
                    field="kind",
                )
            ],
        )
    if kind in ("hold_excess", "accept_excess"):
        # Only counted excess can be held or accepted as excess; accepting held excess is
        # still possible, so a hold does not use up the room to accept it.
        settled = EXCESS_DECISIONS - ({"hold_excess"} if kind == "accept_excess" else set())
        room = excess_room(grn, payload["source_line_key"], frozenset(settled))
        if payload["qty"] > room:
            raise Refusal(
                "DISPOSITION_STALE",
                f"Only {room} counted excess piece(s) on this line are still undecided.",
                issues=[
                    issue("EXCESS_NOT_AVAILABLE", "Not enough undecided excess", quantity=room)
                ],
            )
    return _take_from_end(slices, payload["qty"])


def apply_disposition(
    run: CommandRun,
    grn: GoodsGrn,
    head: DocumentHead,
    payload: dict[str, Any],
    *,
    maker_id: uuid.UUID | None = None,
) -> list[Disposition]:
    """Append the authorised decision and post its P03 branch; no SKU, origin or value invented."""
    kind = payload["kind"]
    site_id = grn.document.held_site_id
    chosen = _select_portions(grn, payload)
    version = head.live_version
    assert version is not None
    plan = Plan("P03", version.pk, event_key("P03", run.key_id, payload["source_line_key"], kind))
    resolved = uuid.UUID(payload["resolved_sku_id"]) if payload["resolved_sku_id"] else None
    # A value_damage origin freezes its maker, so a named person is a precondition of the
    # whole command - not something to discover part-way through the rows.
    if kind == "value_damage" and maker_id is None:
        raise Refusal("ACTION_DENIED", "A disposition is decided by a named person.")
    rows: list[Disposition] = []
    base = {
        "document_id": uuid.UUID(payload["source_document_id"]),
        "site_id": site_id,
        "source_line_key": uuid.UUID(payload["source_line_key"]),
        "kind": kind,
        "grn_revision": head.revision,
        "decision": payload,
    }
    if not chosen:
        rows.append(Disposition(**base, lot_id=None, portion=None, qty=payload["qty"]))
    for lot_id, interval in chosen:
        rows.append(
            Disposition(
                **base, lot_id=lot_id, portion=portion(*interval), qty=ranges.length(interval)
            )
        )
    for row in rows:
        run.record(row)

    for row in rows:
        if row.lot_id is None or row.portion is None:
            continue
        lot_id = row.lot_id
        interval = bounds(row.portion)
        if kind in ("return", "dispose"):
            _release_holds(run, plan, lot_id, interval, site_id=site_id, version_id=version.pk)
            end_positions(
                run, plan, lot_id, interval, "returned" if kind == "return" else "disposed", kind
            )
        elif kind == "hold_excess":
            quarantine = system_location(site_id, "quarantine")
            change_address(run, plan, lot_id, interval, _relocate(quarantine.pk))
            existing = [
                bounds(h.portion)
                for h in ActiveHold.objects.filter(
                    lot_id=lot_id, hold_key=_hold_key(lot_id, "excess")
                )
            ]
            for piece in ranges.subtract([interval], existing):
                place_hold(
                    run,
                    plan,
                    lot_id=lot_id,
                    interval=piece,
                    hold_key=_hold_key(lot_id, "excess"),
                    kind=EXCESS_HOLD,
                    site_id=site_id,
                    source_version_id=version.pk,
                )
        elif kind == "value_damage":
            mrp = int(payload["approved_mrp_paise"])
            basis = run.record(
                Origin(
                    lineage_key=uuid.uuid4(),
                    official_line_id=None,
                    value_damage_disposition_id=row.pk,
                    site_id=site_id,
                    source_time=run.now,
                    source_kind=Origin.SourceKind.VALUE_DAMAGE,
                    sku_id=uuid.UUID(payload["resolved_sku_id"]),
                    # Operational memo value is MRP.  The distinct evidenced cost is
                    # retained below, never promoted to a purchase/carrying value.
                    unit_cost=mrp,
                    mrp=mrp,
                    opening_qty=row.qty,
                    frozen_evidence={
                        "kind": "value_damage",
                        "grn_id": str(grn.document_id),
                        "source_line_key": payload["source_line_key"],
                        "lot_id": str(lot_id),
                        "portion": [interval[0], interval[1]],
                        "resolved_sku_id": payload["resolved_sku_id"],
                        "approved_cost_paise": payload["approved_cost_paise"],
                        "approved_mrp_paise": payload["approved_mrp_paise"],
                        "approved_cost_evidence_id": payload["approved_cost_evidence_id"],
                        "approved_tax_evidence_id": payload["approved_tax_evidence_id"],
                        "damage_evidence_ids": payload["evidence_ids"],
                        "damage_description": payload["damage_description"],
                        "reason_code": payload["reason_code"],
                        "maker_id": str(maker_id),
                    },
                )
            )

            LiveValueBasis.objects.create(
                tenant_id=run.tenant_id,
                lot_id=lot_id,
                portion=portion(*interval),
                origin_id=basis.pk,
            )
            plan.value.append(
                ValuePair(
                    origin_id=basis.pk,
                    amount=row.qty * mrp,
                    source_bucket="origin_evidence",
                    destination_bucket="stock",
                    source_site_id=None,
                    destination_site_id=site_id,
                    lot_id=lot_id,
                    lower=interval[0],
                    upper=interval[1],
                )
            )
        elif kind == "accept_excess":
            _release_holds(
                run,
                plan,
                lot_id,
                interval,
                site_id=site_id,
                version_id=version.pk,
                kinds=["receipt_excess"],
            )
            receiving = system_location(site_id, "receiving")

            def accept(
                old: Address, sku: uuid.UUID | None = resolved, to: uuid.UUID = receiving.pk
            ) -> Address:
                return replace(
                    old, sku_id=sku, location_id=to if old.condition == "good" else old.location_id
                )

            change_address(run, plan, lot_id, interval, accept)
        elif kind == "resolve_identity":
            # Naming the goods never erases how they were counted: unidentified or wrong goods
            # keep that condition, stay in quarantine and off ordinary PTs (§15.2.1 rule 3).

            def resolve(old: Address, sku: uuid.UUID | None = resolved) -> Address:
                return replace(old, sku_id=sku)

            change_address(run, plan, lot_id, interval, resolve)
        elif kind == "accept_wrong":
            # Ticket 05D (overall PRD §15.2.1 rule 2, CH-2026-09-24-01): the business keeps
            # these goods as the SKU they were named as. They become ordinary good goods
            # waiting for a PT; how they arrived stays on the GRN line, the lot's counted
            # identity and both decisions. No hold is released: a piece another hold
            # stands on keeps its place and stays off every PT until that hold's own route.
            receiving = system_location(site_id, "receiving")
            held = [
                bounds(h.portion)
                for h in ActiveHold.objects.filter(lot_id=lot_id, portion__overlap=row.portion)
            ]
            for piece in ranges.subtract([interval], held):
                change_address(run, plan, lot_id, piece, _accepted_as_good(receiving.pk))
            for piece in ranges.intersect([interval], held):
                change_address(run, plan, lot_id, piece, _accepted_as_good(None))
    batch_id = post(run, version, plan)
    for row in rows:
        row.journal_batch_id = batch_id
    refresh_receipt_discrepancy(
        run,
        grn,
        trigger=run.key_id,
        resolved_reason="DISPOSITION_RECORDED",
        reopen=False,
        extra_rows=rows,
    )
    if chosen and kind in EXCESS_DECISIONS:
        sync_excess_holds(
            run, grn, trigger=run.key_id, version=version, extra_rows=rows, lock=False
        )
    if chosen:
        refresh_awaiting_pt(run, grn, trigger=run.key_id, added_custody=False)
    if kind in EXCESS_DECISIONS:
        # Ticket 07B: extra pieces handed over from acceptance are owned until the
        # extra on this receipt is decided - the `acceptance_discrepancy` kind's
        # own closing rule ("disposition or movement recorded"). Only the handoff
        # is keyed to the receipt; wrong or damaged goods found at acceptance are
        # keyed to their PT and are not touched here.
        resolve_exceptions(
            run,
            kind="acceptance_discrepancy",
            subject_key=f"grn:{grn.document_id}",
            reason_code="EXCESS_DECIDED",
        )
    if kind == Disposition.Kind.ACCEPT_SHORTAGE:
        # Store operations ticket 38: an approved shortage drafts the vendor's debit
        # note where the switch is on. Last: it takes the GRN's advisory lock at the
        # highest rank.
        debit_notes.draft_for_shortage(run, grn, rows)
    run.audit_after = {"dispositions": [str(r.pk) for r in rows], "kind": kind}
    return rows


def record_disposition(
    run: CommandRun, grn: GoodsGrn, payload: dict[str, Any], *, expected_revision: int | None
) -> tuple[str, uuid.UUID]:
    """E119: decide now, or park the decision for a distinct checker when policy says so."""
    site_id = grn.document.held_site_id
    require_goods_site(run, site_id, physical=payload["kind"] != "accept_shortage")
    head = lock_heads(run, [grn.document_id])[grn.document_id]
    inp.check_revision(expected_revision, head.revision)
    if payload["reviewed_grn_hash"] != grn_hash(grn, head):
        raise Refusal("DISPOSITION_STALE", "The GRN changed after you reviewed it. Reload it.")
    _disposition_problems(grn, payload)
    _select_portions(grn, payload)
    if disposition_needs_checker(
        payload["kind"], run.now, site_id=site_id, brand_id=grn.arrival.brand_id
    ):
        assert run.principal.human_id is not None
        stored = {"grn_id": str(grn.document_id), "payload": payload}
        draft = ActionDraft.objects.create(
            tenant_id=run.tenant_id,
            subject_kind="disposition",
            subject_key=f"grn:{grn.document_id}:{run.spec.command_id}",
            revision=1,
            maker_id=run.principal.human_id,
            payload=stored,
            content_hash=content_hash(stored),
        )
        policy = pin(
            run,
            action=DISPOSITION_ACTION,
            purpose=payload["kind"],
            site_id=site_id,
            brand_ids=[grn.arrival.brand_id],
            amounts=Amounts(
                int(payload["qty"]),
                int(payload["approved_mrp_paise"]) * int(payload["qty"])
                if payload["kind"] == "value_damage"
                else None,
            ),
        )
        request = create_request(
            run,
            subject_kind="disposition",
            subject_key=f"action_draft:{draft.pk}",
            revision=head.revision,
            reviewed_hash=draft.content_hash,
            requested_action=DISPOSITION_ACTION,
            site_id=site_id,
            brand_id=grn.arrival.brand_id,
            title=f"{payload['kind']} on {grn.document.official_number}",
            policy=policy,
        )
        if payload["kind"] == "value_damage":
            open_exception(
                run,
                kind="approval_pending",
                site_id=site_id,
                subject_key=f"grn:{grn.document_id}",
                reason_code="VALUE_DAMAGE_APPROVAL",
                source_event_key=event_key("value_damage_approval", draft.pk),
                allowed_resolution_actions=["approvals/{id}/decide"],
                note=f"approval_request:{request.pk}",
            )
        run.audit_after = {"disposition_request": str(draft.pk), "kind": payload["kind"]}
        return "disposition_request", draft.pk
    # Decided at once, but still inside the policy's limits when a policy governs it.
    governing = disposition_policy(
        payload["kind"], run.now, site_id=site_id, brand_id=grn.arrival.brand_id
    )
    if governing is not None:
        failure = band_failure(governing.payload, Amounts(int(payload["qty"]), None))
        if failure is not None:
            raise Refusal(
                "APPROVAL_POLICY_BLOCKED",
                BAND_MESSAGES[failure],
                status=422,
                issues=[issue(failure, BAND_MESSAGES[failure], field="approval_policy")],
            )
    lock_lots(run, [lot.pk for lot, _ in grn_lots(grn.document_id)])
    rows = apply_disposition(run, grn, head, payload, maker_id=run.principal.human_id)
    return "disposition", rows[0].pk


def decide_disposition(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    request = context.request
    draft_id = _subject_uuid(request.subject_key, "action_draft")
    draft = ActionDraft.objects.filter(pk=draft_id, subject_kind="disposition").first()
    if draft is None:
        raise Refusal("NOT_FOUND", "That disposition request was not found.")
    context.access.require(DISPOSITION_ACTION, site_id=request.site_id, brand_id=request.brand_id)
    if draft.content_hash != request.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The disposition changed after it was requested.")
    grn_id = uuid.UUID(draft.payload["grn_id"])
    payload = dict(draft.payload["payload"])
    if context.decision == "reject":
        context.enforce_policy(run)
        if payload.get("kind") == "value_damage":
            resolve_exceptions(
                run,
                kind="approval_pending",
                subject_key=f"grn:{grn_id}",
                reason_code="VALUE_DAMAGE_REJECTED",
                source_event_key=event_key("value_damage_approval", draft.pk),
            )
        return {"state": "rejected"}
    grn = goods_grn(grn_id)
    if grn is None:
        raise Refusal("NOT_FOUND", "That GRN was not found.")
    if payload["kind"] != "accept_shortage":
        _check_not_frozen(grn.document.held_site_id)
    head = lock_heads(run, [grn.document_id])[grn.document_id]
    context.enforce_policy(run)
    if payload["reviewed_grn_hash"] != grn_hash(grn, head):
        raise Refusal("DISPOSITION_STALE", "The GRN changed after this decision was requested.")
    _disposition_problems(grn, payload)
    lock_lots(run, [lot.pk for lot, _ in grn_lots(grn.document_id)])
    rows = apply_disposition(run, grn, head, payload, maker_id=draft.maker_id)
    if payload["kind"] == "value_damage":
        resolve_exceptions(
            run,
            kind="approval_pending",
            subject_key=f"grn:{grn.document_id}",
            reason_code="VALUE_DAMAGE_APPROVED",
            source_event_key=event_key("value_damage_approval", draft.pk),
        )
    return {"disposition_ids": [str(r.pk) for r in rows]}


def disposition_request_state(draft: ActionDraft) -> str:
    request = (
        ApprovalRequest.objects.filter(subject_key=f"action_draft:{draft.pk}")
        .order_by("-created_at")
        .first()
    )
    return f"approval_{request.state}" if request is not None else "approval_pending"


def register_approval_handlers() -> None:
    register_subject_handler("document", COUNTER_ACTION, decide_counter_grn)
    register_subject_handler("disposition", DISPOSITION_ACTION, decide_disposition)


# ---------------------------------------------------------------------------
# Damage found while receiving (ticket 05C; design "Damage report and review", E254)
# ---------------------------------------------------------------------------
#
# Damage at receiving is the common damage report, entered from the receipt. The
# goods go to quarantine under the receipt's own damage hold the moment the
# damage is reported, and a pending report opens in the same transaction for a
# different authorised person to confirm or reject (``outbound.damage_review``).
#
# Two ways in, and nothing else changes between them:
#
# * the **count**: pieces counted damaged open in quarantine when the GRN is
#   issued, already under the hold, and the GRN's issuer is the reporter;
# * **after the GRN, before a PT**: E254 reports damage found on a line's good
#   pieces no PT covers yet (or on damaged pieces nothing holds yet).
#
# The report never touches the count - damage does not reduce it, and a missing
# piece is a shortage, not damage - and it invents no SKU, cost, tax, layer or
# value. Its outcome is the review's: confirmed damage may stay in quarantine
# indefinitely, ``value_damage`` stays its own separately approved route, and
# returning or disposing of the goods is the physical route of tickets 15G/15E.

DAMAGE_REPORT_KEYS = frozenset(
    {"source_line_key", "lot_id", "qty", "reason_code", "evidence_ids", "reviewed_grn_hash"}
)


def parse_damage_report(body: dict[str, Any]) -> dict[str, Any]:
    reviewed_grn = body.get("reviewed_grn_hash")
    if not isinstance(reviewed_grn, str) or len(reviewed_grn) != 64:
        raise inp.bad("reviewed_grn_hash must be a 64-character hash.", "reviewed_grn_hash")
    lot_id = inp.optional_uuid(body.get("lot_id"), "lot_id")
    return {
        "source_line_key": str(inp.uuid_value(body.get("source_line_key"), "source_line_key")),
        "lot_id": str(lot_id) if lot_id else None,
        "qty": inp.quantity(body.get("qty"), "qty"),
        "reason_code": inp.text(body.get("reason_code"), "reason_code", 60, required=True),
        "evidence_ids": [str(e) for e in inp.id_list(body.get("evidence_ids"), "evidence_ids")],
        "reviewed_grn_hash": reviewed_grn,
    }


@dataclass(frozen=True)
class _DamagedPiece:
    """One exact portion a receipt damage report covers, and the GRN line it is on."""

    line_key: uuid.UUID
    lot_id: uuid.UUID
    interval: tuple[int, int]
    sku_id: uuid.UUID | None


def receipt_damage_key(lot_id: uuid.UUID) -> uuid.UUID:
    """The receipt's own damage hold key on a lot - the key ``value_damage`` values under."""
    return _hold_key(lot_id, "damage")


def _hold_receipt_damage(
    run: CommandRun,
    plan: Plan,
    *,
    document_id: uuid.UUID,
    site_id: int,
    grn_revision: int,
    reason_code: str,
    evidence_ids: list[str],
    pieces: list[_DamagedPiece],
    relocate: bool,
) -> tuple[list[Disposition], list[dict[str, Any]]]:
    """Quarantine and hold the pieces; the receipt's record of it, and the report's lines.

    ``relocate`` is false for pieces the GRN has just opened in quarantine as
    damaged: they are already where damage stands, in the condition it has.
    """
    quarantine = system_location(site_id, "quarantine")
    receiving = system_location(site_id, "receiving")
    rows: list[Disposition] = []
    lines: list[dict[str, Any]] = []
    for piece in pieces:
        key = receipt_damage_key(piece.lot_id)
        if relocate:
            change_address(
                run, plan, piece.lot_id, piece.interval, _relocate(quarantine.pk, "damaged")
            )
        existing = [
            bounds(h.portion) for h in ActiveHold.objects.filter(lot_id=piece.lot_id, hold_key=key)
        ]
        for part in ranges.subtract([piece.interval], existing):
            place_hold(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=part,
                hold_key=key,
                kind=DAMAGE_HOLD,
                site_id=site_id,
                source_version_id=plan.version_id,
            )
        qty = ranges.length(piece.interval)
        row = run.record(
            Disposition(
                document_id=document_id,
                site_id=site_id,
                source_line_key=piece.line_key,
                kind=Disposition.Kind.HOLD_DAMAGE,
                grn_revision=grn_revision,
                decision={
                    "kind": "hold_damage",
                    "source_document_id": str(document_id),
                    "source_line_key": str(piece.line_key),
                    "lot_id": str(piece.lot_id),
                    "qty": qty,
                    "reason_code": reason_code,
                    "evidence_ids": list(evidence_ids),
                },
                lot_id=piece.lot_id,
                portion=portion(*piece.interval),
                qty=qty,
            )
        )
        rows.append(row)
        lines.append(
            {
                "line_key": str(piece.line_key),
                "disposition_id": str(row.pk),
                "lot_id": str(piece.lot_id),
                # The identity the count actually gave the goods, if any. A report
                # never makes one up.
                "sku_id": str(piece.sku_id) if piece.sku_id else None,
                "origin_id": None,
                "qty": qty,
                # Where a rejection gives the pieces back: the site's receiving
                # location, as good goods - where they would stand had nobody
                # called them damaged.
                "source_location_id": str(receiving.pk),
                "hold_keys": [str(key)],
                "portions": [
                    {
                        "lot_id": str(piece.lot_id),
                        "lower": piece.interval[0],
                        "upper": piece.interval[1],
                        "condition": "good",
                    }
                ],
            }
        )
    return rows, lines


def hold_counted_damage(
    run: CommandRun,
    grn: GoodsGrn,
    version: Any,
    *,
    grn_revision: int,
    counted: list[tuple[uuid.UUID, CustodyLot, uuid.UUID | None]],
) -> Any:
    """GRN issue: what the count found damaged is held and reported in the same command.

    ``counted`` is this command's own damaged lots, each with its GRN line key
    and counted SKU - they are not readable back until the command commits.
    """
    if not counted:
        return None
    from outbound import damage_review

    assert run.principal.human_id is not None
    site_id = grn.document.held_site_id
    plan = Plan("P03", version.pk, event_key("P03", "counted_damage", grn.document_id))
    rows, lines = _hold_receipt_damage(
        run,
        plan,
        document_id=grn.document_id,
        site_id=site_id,
        grn_revision=grn_revision,
        reason_code=COUNTED_DAMAGE_REASON,
        evidence_ids=[],
        pieces=[
            _DamagedPiece(line_key, lot.pk, (0, lot.issued_qty), sku)
            for line_key, lot, sku in counted
        ],
        relocate=False,
    )
    batch_id = post(run, version, plan)
    for row in rows:
        row.journal_batch_id = batch_id
    return damage_review.open_for_receiving(
        run,
        disposition=rows[0],
        site_id=site_id,
        reason_code=COUNTED_DAMAGE_REASON,
        reporter_id=run.principal.human_id,
        lines=lines,
    )


def reportable_damage(
    grn: GoodsGrn, line_key: uuid.UUID | None = None, lot_id: uuid.UUID | None = None
) -> list[_DamagedPiece]:
    """Pieces of this GRN damage may still be reported on, latest first.

    Physically here, on no live PT, carrying no covering decision, and either good
    and under no hold at all, or damaged and under no damage hold yet. Wrong and
    unidentified goods keep their own reason: calling them damaged would erase it.
    """
    views = grn_custody(grn.document_id)
    if line_key is not None:
        views = [v for v in views if v.line_key == line_key]
    if lot_id is not None:
        views = [v for v in views if v.lot_id == lot_id]
    lot_ids = [v.lot_id for v in views]
    if not lot_ids:
        return []
    covering = _decided_portions(lot_ids, kinds=COVERING_KINDS)
    damage_held = _portions_of(
        ActiveHold.objects.filter(lot_id__in=lot_ids, kind__in=DAMAGE_HOLD_KINDS)
    )
    recorded = dict(CustodyLot.objects.filter(pk__in=lot_ids).values_list("pk", "recorded_at"))
    out: list[tuple[Any, _DamagedPiece]] = []
    for view in views:
        for position in Position.objects.filter(lot_id=view.lot_id, boundary="physical"):
            if position.condition == "good":
                blocked = view.held
            elif position.condition == "damaged":
                blocked = damage_held.get(view.lot_id, [])
            else:
                continue
            free = ranges.intersect([bounds(position.portion)], view.held_uncovered)
            free = ranges.subtract(free, [*blocked, *covering.get(view.lot_id, [])])
            out.extend(
                (
                    (recorded[view.lot_id], piece[0]),
                    _DamagedPiece(view.line_key, view.lot_id, piece, position.sku_id),
                )
                for piece in free
            )
    out.sort(key=lambda item: item[0], reverse=True)
    return [piece for _order, piece in out]


def reportable_damage_qty(grn: GoodsGrn) -> dict[str, int]:
    """Per GRN line, how many pieces E254 would still accept - the screen's own limit."""
    out: dict[str, int] = defaultdict(int)
    for piece in reportable_damage(grn):
        out[str(piece.line_key)] += ranges.length(piece.interval)
    return dict(out)


def report_receipt_damage(
    run: CommandRun, grn: GoodsGrn, payload: dict[str, Any], *, expected_revision: int | None
) -> Any:
    """E254: damage found on a GRN's goods before their PT, quarantined and reported at once.

    A count freeze does not stop it (change PRD §14.10 GSA-R02): damage found
    while the site is being counted still goes to quarantine at once. Deciding
    the report waits for the count, as every damage decision does (E253).
    """
    from outbound import damage_review

    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "Damage is reported by a named person.")
    site_id = grn.document.held_site_id
    require_goods_site(run, site_id, physical=False)
    head = lock_heads(run, [grn.document_id])[grn.document_id]
    inp.check_revision(expected_revision, head.revision)
    if payload["reviewed_grn_hash"] != grn_hash(grn, head):
        raise Refusal("DISPOSITION_STALE", "The GRN changed after you reviewed it. Reload it.")
    evidence = [uuid.UUID(e) for e in payload["evidence_ids"]]
    if _evidence_missing(evidence):
        raise inp.bad("An evidence file was not found.", "evidence_ids")
    lock_lots(run, [lot.pk for lot, _ in grn_lots(grn.document_id)])
    candidates = reportable_damage(
        grn,
        uuid.UUID(payload["source_line_key"]),
        uuid.UUID(payload["lot_id"]) if payload["lot_id"] else None,
    )
    room = sum(ranges.length(piece.interval) for piece in candidates)
    if room < payload["qty"]:
        raise Refusal(
            "DISPOSITION_STALE",
            f"Only {room} piece(s) on this line can be reported damaged here. Damage is "
            "reported on pieces that are physically here and on no PT; a missing piece is "
            "a shortage, and wrong or unidentified goods keep their own reason.",
            issues=[
                issue(
                    "NOT_ENOUGH_TO_REPORT",
                    "Fewer reportable pieces than asked",
                    line_key=payload["source_line_key"],
                    quantity=room,
                )
            ],
        )
    chosen: list[_DamagedPiece] = []
    remaining = int(payload["qty"])
    for piece in candidates:
        if remaining <= 0:
            break
        size = min(remaining, ranges.length(piece.interval))
        lower, upper = piece.interval
        chosen.append(replace(piece, interval=(upper - size, upper)))
        remaining -= size
    version = head.live_version
    assert version is not None
    plan = Plan("P03", version.pk, event_key("P03", run.key_id, "damage_report"))
    rows, lines = _hold_receipt_damage(
        run,
        plan,
        document_id=grn.document_id,
        site_id=site_id,
        grn_revision=head.revision,
        reason_code=payload["reason_code"],
        evidence_ids=payload["evidence_ids"],
        pieces=chosen,
        relocate=True,
    )
    batch_id = post(run, version, plan)
    for row in rows:
        row.journal_batch_id = batch_id
    refresh_receipt_discrepancy(
        run,
        grn,
        trigger=run.key_id,
        resolved_reason="DAMAGE_REPORTED",
        reopen=False,
        extra_rows=rows,
    )
    report = damage_review.open_for_receiving(
        run,
        disposition=rows[0],
        site_id=site_id,
        reason_code=payload["reason_code"],
        reporter_id=run.principal.human_id,
        lines=lines,
        evidence_id=evidence[0] if evidence else None,
    )
    run.audit_subject_key = f"grn:{grn.document_id}"
    run.audit_site_id = site_id
    run.audit_after = {
        "damage_report_id": str(report.pk),
        "dispositions": [str(r.pk) for r in rows],
        "qty": int(payload["qty"]),
    }
    return report


def damage_report_rejected(run: CommandRun, report: Any) -> None:
    """A rejected receiving report: what the count called damaged is undecided again.

    The rejection gave the pieces back as good goods and lifted only its own
    hold; the count still says what it said. If that leaves counted damage
    without a decision, the receipt's discrepancy work opens again - correcting
    the count is a counter-GRN, with its own distinct approval.
    """
    grn = goods_grn(report.disposition.document_id)
    if grn is None:
        return
    refresh_receipt_discrepancy(
        run, grn, trigger=run.key_id, resolved_reason="DAMAGE_REPORT_REJECTED", reopen=True
    )


def grn_damage_reports(grn: GoodsGrn) -> list[dict[str, Any]]:
    """Every damage report over this GRN's goods, newest first, with the lines it covers.

    Receiving reports and a stock-screen report on the same pre-PT goods alike:
    the screen links each to the common review rather than restating it.
    """
    from django.db.models import Q

    from outbound.damage_review import source_of
    from outbound.goods_models import DamageReport

    rows = grn_lots(grn.document_id)
    line_of = {str(lot.pk): str(key) for lot, key in rows}
    if not line_of:
        return []
    match = Q()
    for lot_id in line_of:
        match |= Q(lines__contains=[{"lot_id": lot_id}])
    reports = list(
        DamageReport.objects.filter(site_id=grn.document.held_site_id)
        .filter(match)
        .order_by("-reported_at", "-id")
    )
    names = dict(
        HumanIdentity.objects.filter(
            pk__in=sorted(
                {r.reporter_id for r in reports}
                | {r.reviewer_id for r in reports if r.reviewer_id},
                key=str,
            )
        ).values_list("pk", "display_name")
    )
    out: list[dict[str, Any]] = []
    for report in reports:
        per_line: dict[str, int] = defaultdict(int)
        for line in report.lines:
            for piece in line["portions"]:
                key = line_of.get(str(piece["lot_id"]))
                if key is not None:
                    per_line[key] += int(piece["upper"]) - int(piece["lower"])
        out.append(
            {
                "id": str(report.pk),
                "source": source_of(report),
                "state": report.state,
                "quantity": report.quantity,
                "reason_code": report.reason_code,
                "reported_by": {
                    "id": str(report.reporter_id),
                    "name": names.get(report.reporter_id, ""),
                },
                "reported_at": report.reported_at.isoformat(),
                "reviewed_by": (
                    {"id": str(report.reviewer_id), "name": names.get(report.reviewer_id, "")}
                    if report.reviewer_id
                    else None
                ),
                "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None,
                "lines": [{"line_key": k, "qty": q} for k, q in sorted(per_line.items())],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Inbound work: pending and queue (E167, E168)
# ---------------------------------------------------------------------------

QUEUE_EXCEPTIONS = {
    "grn_awaiting_pt": "awaiting_pt",
    "unbooked_arrival": "unbooked",
    "receipt_discrepancy": "discrepancy",
}


def _summary(
    *,
    id: Any,
    kind: str,
    number: str | None,
    state: str,
    site_id: int,
    brand_id: int | None,
    created_at: datetime,
    updated_at: datetime,
    owner_role: str | None,
    due_at: datetime | None,
    purpose: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(id),
        "record_contract": "goods-v1",
        "kind": kind,
        "purpose": purpose,
        "number": number,
        "state": state,
        "site_id": str(site_id),
        "brand_id": str(brand_id) if brand_id is not None else None,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "owner_role": owner_role,
        "due_at": due_at.isoformat() if due_at else None,
    }


def inbound_work(access: AccessContext) -> list[dict[str, Any]]:
    """Arrivals awaiting count, GRNs awaiting PT, unbooked arrivals and open discrepancies."""
    items: list[dict[str, Any]] = []
    for head in ArrivalHead.objects.select_related("arrival").filter(grn_count=0):
        arrival = head.arrival
        if not can_read(access, arrival.site_id, arrival.brand_id):
            continue
        items.append(
            _summary(
                id=arrival.pk,
                kind="arrival",
                number=None,
                state="awaiting_count",
                site_id=arrival.site_id,
                brand_id=arrival.brand_id,
                created_at=arrival.recorded_at,
                updated_at=arrival.recorded_at,
                owner_role="M-STR",
                due_at=None,
            )
        )
    exceptions = list(
        GoodsException.objects.filter(state="open", kind__in=list(QUEUE_EXCEPTIONS)).order_by(
            "opened_at", "id"
        )
    )
    grn_ids = [
        e.subject_key.partition(":")[2] for e in exceptions if e.subject_key.startswith("grn:")
    ]
    arrival_ids = [
        e.subject_key.partition(":")[2] for e in exceptions if e.subject_key.startswith("arrival:")
    ]
    grns = {
        str(g.document_id): g
        for g in GoodsGrn.objects.select_related("document", "arrival").filter(
            document_id__in=[uuid.UUID(g) for g in grn_ids]
        )
    }
    arrivals = {str(a.pk): a for a in Arrival.objects.filter(pk__in=arrival_ids)}
    for exception in exceptions:
        prefix, _, subject = exception.subject_key.partition(":")
        brand_id: int | None
        if prefix == "grn" and subject in grns:
            grn = grns[subject]
            kind, number, subject_id, brand_id = (
                "grn",
                grn.document.official_number,
                grn.document_id,
                grn.arrival.brand_id,
            )
        elif prefix == "arrival" and subject in arrivals:
            kind, number, subject_id, brand_id = (
                "arrival",
                None,
                arrivals[subject].pk,
                arrivals[subject].brand_id,
            )
        else:
            continue
        # Every receiving-queue exception is about a GRN or an arrival, both of
        # which happen at a site. A site-less exception (GSA-T18 made the column
        # nullable for tenant-wide ones) is not a receiving queue row.
        if exception.site_id is None or not can_read(access, exception.site_id, brand_id):
            continue
        items.append(
            _summary(
                id=subject_id,
                kind=kind,
                number=number,
                state=QUEUE_EXCEPTIONS[exception.kind],
                site_id=exception.site_id,
                brand_id=brand_id,
                created_at=exception.opened_at,
                updated_at=exception.opened_at,
                owner_role=exception.owner_role,
                due_at=exception.due_at,
            )
        )
    items.sort(key=lambda item: (item["created_at"], item["id"]))
    return items


# ---------------------------------------------------------------------------
# The receiving inbox (OPS-04): one list of what is arriving at a site
# ---------------------------------------------------------------------------

#: The steps a delivery walks, in order. The inbox names the *next* one, so a
#: person opening the list is told what to do rather than being left to work it
#: out from a state word. `done` is the end: accepted, or closed for good.
INBOX_STEPS: tuple[str, ...] = (
    "arrival",
    "count",
    "grn",
    "discrepancies",
    "pt_prepare",
    "pt_approve",
    "labels",
    "accept",
    "done",
)

#: Who does the site's own receiving work: the warehouse's operator at a
#: warehouse, the store's own person at a store (store and warehouse operations
#: PRD §3.2). Anything else is a store, which is the safer default.
_SITE_WORKER = {"warehouse": "C-WHO", "store": "M-STR"}

#: Putting stock away, whatever brought it in. Spelled here rather than imported
#: from `outbound.transfers` because that module imports this one.
ACCEPT_STOCK_ACTION = "stock.accept"

#: The steps that are not the receiving site's own work. The buyer answers
#: whether a delivery was booked, the warehouse prepares a PT, the Owner
#: approves it and the inventory controller decides disputed goods - the same
#: separation the PRD's §3.2 table sets out.
_STEP_OWNER: dict[str, str | None] = {
    "arrival": "C-BUY",
    "discrepancies": "C-INV",
    "pt_prepare": "C-WHO",
    "pt_approve": "C-OWN",
    "done": None,
}


def _next_step_owner(step: str, store_type: str) -> str | None:
    """The role the next step waits on. ``None`` once there is nothing to wait for."""
    if step in _STEP_OWNER:
        return _STEP_OWNER[step]
    return _SITE_WORKER.get(store_type, "M-STR")


def incoming_dispatch_items(site_id: int | None, access: AccessContext) -> list[dict[str, Any]]:
    """Transfer dispatches on their way in, for the receiving inbox (OPS-06).

    A shipment is in this site's inbox from the moment it leaves the other site
    until its goods have been put away or it has gone back where it came from.
    Its next step is the same two words the rest of the inbox uses - *count*
    while it is still on the road or just arrived, *accept* once it has been
    counted and there are good pieces waiting - so a person reads one list and
    not two vocabularies.

    A returned shipment is done here: it never arrived, and the inbox says so by
    having nothing left to ask of this site.
    """
    from outbound.goods_models import TransferDispatch
    from outbound.transfers import ACCEPT_ACTION, MOVE_ACTION

    rows = TransferDispatch.objects.select_related(
        "transfer", "transfer__source_site", "transfer__destination_site"
    ).order_by("dispatched_at", "id")
    if site_id is not None:
        rows = rows.filter(transfer__destination_site_id=site_id)
    items: list[dict[str, Any]] = []
    for record in rows:
        destination_id = record.transfer.destination_site_id
        if not (
            access.can(MOVE_ACTION, site_id=destination_id)
            or access.can(ACCEPT_ACTION, site_id=destination_id)
        ):
            continue
        step = _dispatch_step(record)
        items.append(
            {
                "id": str(record.pk),
                "record_contract": "goods-v1",
                "kind": "transfer_dispatch",
                "site_id": str(destination_id),
                "brand_id": None,
                "reference": _dispatch_reference(record),
                "arrived_at": (record.arrived_at or record.dispatched_at).isoformat(),
                "updated_at": (
                    record.counted_at or record.arrived_at or record.dispatched_at
                ).isoformat(),
                "next_step": step,
                "next_step_owner_role": _next_step_owner(
                    step, record.transfer.destination_site.store_type
                ),
                "in_receiving_queue": False,
                "arrival_id": None,
                "grn_id": None,
                "grn_number": None,
                "pt_id": None,
                "pt_number": None,
                "pt_ids": [],
                "official_version_id": None,
                "acceptance_session_id": None,
                "transfer_id": str(record.transfer_id),
                "dispatch_id": str(record.pk),
                "sale_line_id": None,
                **NO_BOOKING,
            }
        )
    return items


def _dispatch_step(record: Any) -> str:
    """Which step this shipment is waiting on at the site receiving it."""
    from outbound.goods_models import TransferDispatch

    if record.state == TransferDispatch.State.IN_TRANSIT:
        return "count"
    if record.state == TransferDispatch.State.COUNTED:
        return "accept"
    return "done"


def _dispatch_reference(record: Any) -> str:
    """What a person calls this shipment: where it came from, and which one it is."""
    source = record.transfer.source_site
    return f"{source.name} ({source.code}) · shipment {record.sequence_no}"


def _arrival_reference(arrival: Arrival, vendor_names: dict[int, str]) -> str:
    """What a person calls this delivery: the vendor, and the invoice if there is one."""
    vendor = vendor_names.get(arrival.vendor_id) or f"Vendor {arrival.vendor_id}"
    if arrival.invoice_number:
        return f"{vendor} · {arrival.invoice_number}"
    return vendor


@dataclass(frozen=True)
class _DeliveryRecords:
    """Every record one vendor delivery has reached so far, read in bulk.

    ``pt`` is the PT the delivery is working on now: the primary, then each
    supplement in turn (ticket 07B). It is ``None`` both before any PT exists and
    while a new supplement is still to be started, which ``pt_ids`` - every
    receipt PT the delivery has had, primary first - tells apart.
    """

    grn: GoodsGrn | None
    pt: Any | None
    pt_head: DocumentHead | None
    pt_ids: tuple[str, ...]
    counted: bool
    unbooked: bool
    discrepancy: bool
    printed: bool
    remaining_to_accept: int | None
    acceptance_session_id: uuid.UUID | None
    official_version_id: uuid.UUID | None


def _delivery_step(records: _DeliveryRecords) -> str:
    """Which step this delivery is waiting on, from its own records alone.

    Read in the order the work actually happens, so the first thing that is not
    finished is the answer. Nothing here decides anything: every step keeps its
    own record, approval and refusals exactly as it already had them, and this
    only says which one a person should open next.
    """
    if records.grn is None:
        # The booking question belongs to the arrival itself and is asked while
        # the delivery is still sitting there unopened. Once somebody has begun
        # counting, the delivery has moved on: the unbooked work item stays
        # open and owned in the exceptions centre, but it is no longer what
        # this delivery is waiting on.
        if records.unbooked and not records.counted:
            return "arrival"
        # A count that has been started but has issued no GRN is at the GRN
        # step; nothing counted at all is still at the count step.
        return "grn" if records.counted else "count"
    if records.discrepancy:
        return "discrepancies"
    if records.pt_head is None:
        # No PT yet - or, once every earlier PT is official and put away, newly
        # eligible pieces (accepted excess) waiting for their supplement.
        return "pt_prepare"
    state = records.pt_head.state
    if state == DocumentHead.State.SUBMITTED:
        return "pt_approve"
    if state != DocumentHead.State.OFFICIAL:
        # A draft PT, or one that has been reversed and needs reissuing.
        return "pt_prepare"
    if records.remaining_to_accept is None or records.remaining_to_accept <= 0:
        return "done"
    # Labels are printed from the frozen official PT values and the acceptance
    # scan cites the tag (PRD §5.3), so printing comes before putting away.
    return "labels" if not records.printed else "accept"


def _current_pt(
    pts: list[Any], heads: dict[Any, DocumentHead], remaining: dict[Any, int], awaiting: bool
) -> Any | None:
    """The PT a delivery is working on now, primary first, then each supplement.

    The first one not yet official, or official with pieces still to put away.
    When every one is finished but the receipt still waits on a PT (the
    ``grn_awaiting_pt`` work a counter-GRN reopens), a new supplement is next and
    there is no current PT. Otherwise the last one: the delivery is done.
    """
    for pt in pts:
        head = heads.get(pt.document_id)
        if head is None or head.state != DocumentHead.State.OFFICIAL:
            return pt
        if remaining.get(head.live_version_id, 0) > 0:
            return pt
    if awaiting or not pts:
        return None
    return pts[-1]


def _delivery_records(
    arrivals: list[Arrival], access: AccessContext
) -> dict[Any, _DeliveryRecords]:
    """Read every delivery's records in bulk, one query per kind rather than per row."""
    # Imported here, not at the top: `ptmapper.goods_pt_services` imports this
    # module, and `stockledger.goods_acceptance` is read only for the remaining
    # quantity below. Models alone would be safe; keeping all three together
    # keeps the reason in one place.
    from ptmapper.goods_models import GoodsPt, PrintJob
    from stockledger.goods_acceptance import pending_acceptance
    from stockledger.goods_models import AcceptanceSession

    arrival_ids = [a.pk for a in arrivals]
    grns = {
        grn.arrival_id: grn
        for grn in GoodsGrn.objects.select_related("document")
        .filter(arrival_id__in=arrival_ids, counter_of__isnull=True)
        .order_by("id")
    }
    # Every receipt PT of each receipt, the primary first and then its
    # supplements in the order they were started (ticket 07B).
    pts_by_grn: dict[Any, list[Any]] = {}
    for receipt_pt in (
        GoodsPt.objects.select_related("document")
        .filter(grn_id__in=[grn.pk for grn in grns.values()])
        .order_by("id")
    ):
        pts_by_grn.setdefault(receipt_pt.grn_id, []).append(receipt_pt)
    for listed in pts_by_grn.values():
        listed.sort(key=lambda each: (each.receipt_kind != "primary", each.pk))
    pt_heads = {
        head.document_id: head
        for head in DocumentHead.objects.select_related("live_version").filter(
            document_id__in=[each.document_id for listed in pts_by_grn.values() for each in listed]
        )
    }
    counted = set(
        CountSession.objects.filter(arrival_id__in=arrival_ids).values_list("arrival_id", flat=True)
    )
    open_kinds = ("unbooked_arrival", "receipt_discrepancy", "grn_awaiting_pt")
    flags: dict[str, set[str]] = {kind: set() for kind in open_kinds}
    for kind, subject_key in GoodsException.objects.filter(
        state="open", kind__in=open_kinds
    ).values_list("kind", "subject_key"):
        flags[kind].add(subject_key)
    versions = [
        head.live_version_id for head in pt_heads.values() if head.live_version_id is not None
    ]
    printed = set(
        PrintJob.objects.filter(pt_version_id__in=versions).values_list("pt_version_id", flat=True)
    )
    sessions = {
        row.source_version_id: row.pk
        for row in AcceptanceSession.objects.filter(source_version_id__in=versions).order_by("id")
    }
    remaining = {
        row.official_version_id: row.remaining_qty
        for row in pending_acceptance(access.tenant_id, None)
    }

    records: dict[Any, _DeliveryRecords] = {}
    for arrival in arrivals:
        grn = grns.get(arrival.pk)
        listed = pts_by_grn.get(grn.pk, []) if grn else []
        awaiting = grn is not None and f"grn:{grn.document_id}" in flags["grn_awaiting_pt"]
        pt = _current_pt(listed, pt_heads, remaining, awaiting)
        pt_head = pt_heads.get(pt.document_id) if pt else None
        version_id = pt_head.live_version_id if pt_head else None
        records[arrival.pk] = _DeliveryRecords(
            grn=grn,
            pt=pt,
            pt_head=pt_head,
            pt_ids=tuple(str(each.document_id) for each in listed),
            counted=arrival.pk in counted,
            unbooked=f"arrival:{arrival.pk}" in flags["unbooked_arrival"],
            discrepancy=(
                grn is not None and f"grn:{grn.document_id}" in flags["receipt_discrepancy"]
            ),
            printed=version_id in printed if version_id else False,
            remaining_to_accept=remaining.get(version_id) if version_id else None,
            acceptance_session_id=sessions.get(version_id) if version_id else None,
            official_version_id=version_id,
        )
    return records


#: A row with no booking to report: an unbooked delivery, a dispatch, a return, or a
#: booking this reader may not read. Null rather than absent, so every row is one shape.
NO_BOOKING: dict[str, Any] = {
    "booking_id": None,
    "booking_number": None,
    "booking_booked_qty": None,
    "booking_received_qty": None,
}


def _booking_figures(access: AccessContext, arrivals: list[Arrival]) -> dict[Any, Any]:
    """Received-of-booked for the bookings these deliveries were received against.

    Store and warehouse operations PRD §5.1: the Pending row shows "Booking B-104: 80
    of 100 received". The figures are worked out from the goods-v1 receipt links on
    every read (``vendors.goods_services.booking_figures``) - never stored - and only
    for a booking this reader may read; any other booking is left out, not hinted at.
    """
    # Imported here: `vendors.goods_services` imports this module's package.
    from vendors.goods_services import booking_figures, booking_readable

    ids = {arrival.booking_id for arrival in arrivals if arrival.booking_id}
    if not ids:
        return {}
    readable = [
        booking
        for booking in GoodsBooking.objects.select_related("document").filter(pk__in=ids)
        if booking_readable(access, booking)
    ]
    return booking_figures(readable)


def _booking_fields(figures: Any) -> dict[str, Any]:
    if figures is None:
        return dict(NO_BOOKING)
    return {
        "booking_id": str(figures.document_id),
        "booking_number": figures.number,
        "booking_booked_qty": figures.booked,
        "booking_received_qty": figures.received,
    }


def inbox_items(
    access: AccessContext, *, site_id: int | None, work: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every delivery on its way in to this site, each with the step it waits on.

    ``work`` is the receiving queue the pending and queue reads already answer
    (E167/E168), passed in so this list is scoped by exactly the same rule rather
    than by a second one written beside it. Vendor deliveries come from the
    arrivals themselves, because the queue only carries work that is still early
    - an arrival already on an official PT is not "pending" there, but it is very
    much still in the inbox until somebody has put it away.
    """
    queued = {item["id"] for item in work}
    arrivals = [
        arrival
        for arrival in Arrival.objects.select_related("site").order_by("actual_arrival_at", "id")
        if (site_id is None or arrival.site_id == site_id)
        and can_read(access, arrival.site_id, arrival.brand_id)
    ]
    records = _delivery_records(arrivals, access)
    vendor_names = dict(
        Vendor.objects.filter(pk__in={a.vendor_id for a in arrivals}).values_list("pk", "name")
    )
    bookings = _booking_figures(access, arrivals)
    items: list[dict[str, Any]] = []
    for arrival in arrivals:
        found = records[arrival.pk]
        step = _delivery_step(found)
        grn = found.grn
        pt = found.pt
        items.append(
            {
                "id": str(arrival.pk),
                "record_contract": "goods-v1",
                "kind": "vendor_delivery",
                "site_id": str(arrival.site_id),
                "brand_id": str(arrival.brand_id),
                "reference": _arrival_reference(arrival, vendor_names),
                "arrived_at": arrival.actual_arrival_at.isoformat(),
                "updated_at": (
                    found.pt_head.updated_at.isoformat()
                    if found.pt_head is not None
                    else arrival.recorded_at.isoformat()
                ),
                "next_step": step,
                "next_step_owner_role": _next_step_owner(step, arrival.site.store_type),
                "in_receiving_queue": str(arrival.pk) in queued
                or (grn is not None and str(grn.document_id) in queued),
                "arrival_id": str(arrival.pk),
                "grn_id": str(grn.document_id) if grn is not None else None,
                "grn_number": grn.document.official_number if grn is not None else None,
                "pt_id": str(pt.document_id) if pt is not None else None,
                "pt_number": pt.document.official_number if pt is not None else None,
                "pt_ids": list(found.pt_ids),
                "official_version_id": (
                    str(found.official_version_id) if found.official_version_id else None
                ),
                "acceptance_session_id": (
                    str(found.acceptance_session_id) if found.acceptance_session_id else None
                ),
                **_booking_fields(bookings.get(arrival.booking_id) if arrival.booking_id else None),
                # A vendor delivery is nobody's transfer. Published as null
                # rather than omitted, so both kinds of row have one shape.
                "transfer_id": None,
                "dispatch_id": None,
                # A vendor delivery is nobody's customer return either.
                "sale_line_id": None,
            }
        )
    items.extend(incoming_dispatch_items(site_id, access))
    items.extend(customer_return_items(site_id, access))
    return items


def customer_return_items(site_id: int | None, access: AccessContext) -> list[dict[str, Any]]:
    """Pieces a customer brought back, waiting to be put away (OPS-09, PRD §10.4).

    The third thing that arrives at a store's receiving, beside a vendor's
    delivery and another site's shipment, and it belongs on the same list for the
    same reason: a returned piece is not sellable until somebody accepts it, and
    "not sellable and on nobody's list" is how stock disappears.

    Its next step is the one word the rest of the inbox uses - *accept* - because
    it is the same act. There is nothing before it: the bill counted the piece and
    the bill is the evidence, so there is no arrival to record, no count to take
    and no PT to prepare.
    """
    from sell.services.returned_pieces import pending_returns

    reach = access.site_reach(ACCEPT_STOCK_ACTION)
    if site_id is not None:
        reach = {site_id} if reach is None else (reach & {site_id})
    items: list[dict[str, Any]] = []
    for row in pending_returns(reach):
        if not access.can(ACCEPT_STOCK_ACTION, site_id=row.store_id):
            continue
        items.append(
            {
                "id": f"return:{row.sale_line_id}",
                "record_contract": "goods-v1",
                "kind": "customer_return",
                "site_id": str(row.store_id),
                "brand_id": None,
                "reference": row.reference,
                "arrived_at": row.returned_at.isoformat(),
                "updated_at": row.returned_at.isoformat(),
                "next_step": "accept",
                "next_step_owner_role": "M-STR",
                "in_receiving_queue": False,
                "arrival_id": None,
                "grn_id": None,
                "grn_number": None,
                "pt_id": None,
                "pt_number": None,
                "pt_ids": [],
                "official_version_id": None,
                "acceptance_session_id": None,
                "transfer_id": None,
                "dispatch_id": None,
                # The row the accept route names. Null on every other kind, so all
                # three shapes stay one shape.
                "sale_line_id": str(row.sale_line_id),
                **NO_BOOKING,
            }
        )
    return items

"""Goods-v1 vendors and bookings (design §5.2-§5.3, E021-E025, E092/E093, E108-E112, E238).

Vendors stay the legacy master rows; each goods-v1 change appends a
``MasterVersion``. A booking is a versioned document: an unnumbered draft, then one
C-BUY confirmation that numbers it and freezes its lines. After that it changes
only through appended, dated corrections, receipt links and counter links.
Progress (received, reversed, outstanding) is derived from those rows on every
read; there is no mutable received counter.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from accounts.principal import AccessContext
from alerts.goods_services import resolve_exceptions
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
from core.kernel_models import DocumentEvent, DocumentHead, DocumentIdentity, OfficialLine
from core.numbering import allocate
from core.refusals import Refusal, issue
from files.goods_models import EvidenceObject
from inbound import goods_input as inp
from inbound import three_way_match
from masters.goods_models import MasterVersion, SiteGuard
from masters.goods_sbu import require_active_sbu
from masters.models import Brand, LegalEntity, Season, Store
from vendors import open_to_buy
from vendors.goods_models import BookingCorrectionEvent, BookingReceiptLink, GoodsBooking
from vendors.models import Vendor

BOOKING_ACTION = "booking.manage"
VENDOR_ACTION = "vendor.manage"
#: Grants that may read bookings (document-family read, design E092/E093).
BOOKING_READ_ACTIONS = (BOOKING_ACTION, "receive.arrival", "pt.prepare", "pt.view")
CLOSE_EVENTS = ("short_closed", "cancelled")


# ---------------------------------------------------------------------------
# Vendors (E021-E025)
# ---------------------------------------------------------------------------

VENDOR_KEYS = frozenset({"code", "name", "gstin", "agent_ref"})
_VENDOR_CODE = re.compile(r"^[-a-zA-Z0-9_]{1,32}$")
_GSTIN = re.compile(r"^[0-9A-Z]{15}$")


def parse_vendor(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not partial or "code" in body:
        out["code"] = inp.text(body.get("code"), "code", 32, required=True)
    if not partial or "name" in body:
        out["name"] = inp.text(body.get("name"), "name", 160, required=True)
    if "gstin" in body:
        out["gstin"] = inp.text(body.get("gstin"), "gstin", 15)
    if "agent_ref" in body:
        out["agent_ref"] = inp.text(body.get("agent_ref"), "agent_ref", 160)
    if partial and not out:
        raise inp.bad("Send at least one vendor field to change.", "body", "REQUIRED")
    return out


def latest_vendor_versions(vendor_ids: list[int]) -> dict[int, MasterVersion]:
    latest: dict[int, MasterVersion] = {}
    rows = MasterVersion.objects.filter(
        kind=MasterVersion.Kind.VENDOR, target_key__in=[str(i) for i in vendor_ids]
    ).order_by("revision")
    for row in rows:
        latest[int(row.target_key)] = row
    return latest


def vendor_revision(latest: MasterVersion | None) -> int:
    return latest.revision if latest is not None else 1


def vendor_state(vendor: Vendor, latest: MasterVersion | None) -> str:
    return (
        "retired" if (latest is not None and latest.retired) or not vendor.is_active else "active"
    )


def vendor_data(vendor: Vendor, latest: MasterVersion | None) -> dict[str, Any]:
    payload = latest.payload if latest is not None else {}
    return {
        "code": vendor.code,
        "name": vendor.name,
        "gstin": vendor.gstin or None,
        "agent_ref": payload.get("agent_ref"),
    }


def _validate_vendor(payload: dict[str, Any], *, exclude_id: int | None) -> None:
    problems: list[dict[str, Any]] = []
    code = payload.get("code")
    if code is not None and not _VENDOR_CODE.match(code):
        problems.append(
            issue("CODE_FORMAT", "A vendor code uses letters, digits, - and _.", field="code")
        )
    gstin = payload.get("gstin")
    if gstin and not _GSTIN.match(gstin):
        problems.append(
            issue("GSTIN_FORMAT", "A GSTIN is 15 capital letters and digits.", field="gstin")
        )
    if problems:
        raise Refusal(
            "MASTER_INVALID", "The vendor details are not valid.", status=422, issues=problems
        )
    if code is not None:
        clash = Vendor.objects.filter(code__iexact=code)
        if exclude_id is not None:
            clash = clash.exclude(pk=exclude_id)
        if clash.exists():
            raise Refusal("MASTER_CONFLICT", "Another vendor already uses that code.")
    if gstin:
        # A nonblank GSTIN identifies one vendor in the tenant (change PRD §14.2).
        taken = Vendor.objects.filter(gstin=gstin)
        if exclude_id is not None:
            taken = taken.exclude(pk=exclude_id)
        if taken.exists():
            raise Refusal("MASTER_CONFLICT", "Another vendor already uses that GSTIN.")


def create_vendor(run: CommandRun, payload: dict[str, Any]) -> Vendor:
    _validate_vendor(payload, exclude_id=None)
    vendor = Vendor.objects.create(
        code=payload["code"], name=payload["name"], gstin=payload.get("gstin") or ""
    )
    run.record(
        MasterVersion(
            kind=MasterVersion.Kind.VENDOR,
            target_key=str(vendor.pk),
            revision=1,
            payload=_vendor_version_payload(vendor, payload.get("agent_ref")),
            effective_from=run.now,
        )
    )
    run.audit_after = {"vendor_id": vendor.pk, "code": vendor.code}
    return vendor


def _vendor_version_payload(vendor: Vendor, agent_ref: str | None) -> dict[str, Any]:
    return {
        "code": vendor.code,
        "name": vendor.name,
        "gstin": vendor.gstin or None,
        "agent_ref": agent_ref,
    }


def _lock_vendor(run: CommandRun, vendor_id: int) -> Vendor:
    rows = run.lock(LockRank.DOCUMENT, Vendor.objects.filter(pk=vendor_id))
    if not rows:
        raise Refusal("NOT_FOUND", "That vendor was not found.")
    vendor: Vendor = rows[0]
    return vendor


def update_vendor(
    run: CommandRun, vendor_id: int, payload: dict[str, Any], *, expected_revision: int | None
) -> Vendor:
    vendor = _lock_vendor(run, vendor_id)
    latest = latest_vendor_versions([vendor.pk]).get(vendor.pk)
    inp.check_revision(expected_revision, vendor_revision(latest))
    if vendor_state(vendor, latest) == "retired":
        raise Refusal("MASTER_INVALID", "A retired vendor cannot be changed.", status=422)
    _validate_vendor(payload, exclude_id=vendor.pk)
    run.audit_before = vendor_data(vendor, latest)
    for field in ("code", "name"):
        if field in payload:
            setattr(vendor, field, payload[field])
    if "gstin" in payload:
        vendor.gstin = payload["gstin"] or ""
    vendor.save()
    agent_ref = (
        payload["agent_ref"] if "agent_ref" in payload else vendor_data(vendor, latest)["agent_ref"]
    )
    run.record(
        MasterVersion(
            kind=MasterVersion.Kind.VENDOR,
            target_key=str(vendor.pk),
            revision=vendor_revision(latest) + 1,
            payload=_vendor_version_payload(vendor, agent_ref),
            reason_code="CORRECTION",
            effective_from=run.now,
        )
    )
    run.audit_after = _vendor_version_payload(vendor, agent_ref)
    return vendor


def retire_vendor(
    run: CommandRun,
    vendor_id: int,
    *,
    reason_code: str,
    effective_at: datetime,
    expected_revision: int | None,
) -> Vendor:
    vendor = _lock_vendor(run, vendor_id)
    latest = latest_vendor_versions([vendor.pk]).get(vendor.pk)
    inp.check_revision(expected_revision, vendor_revision(latest))
    if vendor_state(vendor, latest) == "retired":
        raise Refusal("RETIREMENT_BLOCKED", "This vendor is already retired.")
    open_bookings = [
        booking
        for booking in GoodsBooking.objects.filter(vendor_id=vendor.pk, closed_at__isnull=True)
        if DocumentHead.objects.filter(
            document_id=booking.document_id, live_version__isnull=False
        ).exists()
    ]
    if open_bookings:
        raise Refusal(
            "RETIREMENT_BLOCKED",
            f"{len(open_bookings)} open booking(s) still name this vendor; close them first.",
        )
    vendor.is_active = False
    vendor.save()
    run.record(
        MasterVersion(
            kind=MasterVersion.Kind.VENDOR,
            target_key=str(vendor.pk),
            revision=vendor_revision(latest) + 1,
            payload=_vendor_version_payload(vendor, vendor_data(vendor, latest)["agent_ref"]),
            retired=True,
            reason_code=reason_code,
            effective_from=effective_at,
        )
    )
    run.audit_after = {"vendor_id": vendor.pk, "retired": True, "reason_code": reason_code}
    return vendor


# ---------------------------------------------------------------------------
# Booking payloads
# ---------------------------------------------------------------------------

HEADER_KEYS = frozenset(
    {
        "vendor_id",
        "brand_id",
        "season_id",
        "entity_id",
        "destination_site_id",
        "commercial_label",
        # The vendor's own reference for the order (their PO or order number).
        "vendor_ref",
        "agreement_evidence_id",
        "expected_date",
        "source_evidence_id",
        "lines",
        "notes",
    }
)
LINE_KEYS = frozenset(
    {
        "line_key",
        "style_code",
        "description",
        # Either the size as a person writes it ("M", "Medium") or the governed
        # value's id. The text is resolved to that id before anything is saved.
        "size",
        "size_value_id",
        "colour_value_id",
        "qty",
        "destination_site_id",
        "mrp_paise",
        # Indicative, never binding: the PT holds the cost that counts (overall
        # PRD R-BUY-001). Read back only with the ``cost`` field grant.
        "cost_paise",
    }
)
#: The governed vocabulary a booking line's size text is looked up in.
SIZE_DIMENSION = "size"


@dataclass
class BookingInput:
    header: dict[str, Any]
    lines: list[tuple[uuid.UUID, dict[str, Any]]]
    vendor_id: int
    brand_id: int
    entity_id: int


def parse_booking(body: dict[str, Any]) -> BookingInput:
    header: dict[str, Any] = {
        "vendor_id": str(inp.legacy_id(body.get("vendor_id"), "vendor_id")),
        "brand_id": str(inp.legacy_id(body.get("brand_id"), "brand_id")),
        "season_id": str(inp.legacy_id(body.get("season_id"), "season_id")),
        "entity_id": str(inp.legacy_id(body.get("entity_id"), "entity_id")),
        "destination_site_id": _opt_str(
            inp.optional_legacy_id(body.get("destination_site_id"), "destination_site_id")
        ),
        "commercial_label": inp.text(body.get("commercial_label"), "commercial_label", 160),
        "vendor_ref": inp.text(body.get("vendor_ref"), "vendor_ref", 80),
        "agreement_evidence_id": _opt_str(
            inp.optional_uuid(body.get("agreement_evidence_id"), "agreement_evidence_id")
        ),
        "expected_date": _opt_str(inp.optional_day(body.get("expected_date"), "expected_date")),
        "source_evidence_id": _opt_str(
            inp.optional_uuid(body.get("source_evidence_id"), "source_evidence_id")
        ),
        "notes": inp.text(body.get("notes"), "notes", 1000),
    }
    raw_lines = inp.object_list(body.get("lines", []), "lines")
    lines: list[tuple[uuid.UUID, dict[str, Any]]] = []
    for index, raw in enumerate(raw_lines):
        field = f"lines[{index}]"
        line = inp.closed(raw, LINE_KEYS, field, required=["line_key", "style_code", "qty"])
        key = inp.uuid_value(line["line_key"], f"{field}.line_key")
        lines.append(
            (
                key,
                {
                    "line_key": str(key),
                    "style_code": inp.text(
                        line["style_code"], f"{field}.style_code", 120, required=True
                    ),
                    "description": inp.text(line.get("description"), f"{field}.description", 200),
                    "size_value_id": inp.vocabulary_id(
                        line.get("size_value_id"), f"{field}.size_value_id"
                    ),
                    "colour_value_id": inp.vocabulary_id(
                        line.get("colour_value_id"), f"{field}.colour_value_id"
                    ),
                    "qty": inp.quantity(line["qty"], f"{field}.qty"),
                    "destination_site_id": _opt_str(
                        inp.optional_legacy_id(
                            line.get("destination_site_id"), f"{field}.destination_site_id"
                        )
                    ),
                    "mrp_paise": inp.money(line.get("mrp_paise"), f"{field}.mrp_paise"),
                    "cost_paise": inp.money(line.get("cost_paise"), f"{field}.cost_paise"),
                    # Transient: `resolve_sizes` turns it into ``size_value_id``
                    # and removes it before the line is stored.
                    **(
                        {"size": inp.text(line["size"], f"{field}.size", 40)}
                        if line.get("size") is not None
                        else {}
                    ),
                },
            )
        )
    return BookingInput(
        header=header,
        lines=lines,
        vendor_id=int(header["vendor_id"]),
        brand_id=int(header["brand_id"]),
        entity_id=int(header["entity_id"]),
    )


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def resolve_sizes(tenant_id: uuid.UUID, lines: list[dict[str, Any]], now: datetime) -> None:
    """Turn each line's size text into the governed size value it names, in place.

    A size is matched on its key or its label, case ignored ("m", "M" and
    "Medium" are one value). Retired values are no choice for a new booking. A
    size that matches nothing is refused - never stored as free text beside the
    governed id - and a blank one leaves the size not given."""
    from masters.goods_identity_services import vocabulary

    if not any("size" in line for line in lines):
        return
    values = [
        value
        for value in vocabulary(tenant_id, now, SIZE_DIMENSION).get(SIZE_DIMENSION, [])
        if not value.retired
    ]
    by_text: dict[str, str] = {}
    for value in values:
        by_text.setdefault(value.value_key.casefold(), str(value.id))
        by_text.setdefault(value.label.casefold(), str(value.id))
    problems: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if "size" not in line:
            continue
        typed = line.pop("size")
        if typed is None:
            continue
        field = f"lines[{index}].size"
        if line.get("size_value_id"):
            problems.append(
                issue("SIZE_TWICE", "Give the size as text or as an id, not both.", field=field)
            )
            continue
        found = by_text.get(typed.strip().casefold())
        if found is None:
            message = f"Size '{typed.strip()}' is not in the size list."
            problems.append(issue("UNKNOWN_SIZE", message, field=field))
            continue
        line["size_value_id"] = found
    if problems:
        raise Refusal(
            "BOOKING_INVALID",
            problems[0]["message"] if len(problems) == 1 else "Some sizes are not in the list.",
            status=422,
            issues=problems,
        )


def booking_home_site(header: dict[str, Any], lines: list[dict[str, Any]]) -> int | None:
    """The document's home site: the header destination, else the first line destination."""
    if header.get("destination_site_id"):
        return int(header["destination_site_id"])
    for line in lines:
        if line.get("destination_site_id"):
            return int(line["destination_site_id"])
    return None


def season_retired(season_id: int) -> bool:
    """A season has no ``is_active``; its retirement is its latest master version's."""
    latest = (
        MasterVersion.objects.filter(kind=MasterVersion.Kind.SEASON, target_key=str(season_id))
        .order_by("-revision")
        .first()
    )
    return latest is not None and latest.retired


def check_destinations(sites: set[int], brand_id: int, now: datetime) -> None:
    """Every named destination uses goods-v1 bookings and has no retired unit for the brand.

    A destination whose SBU for the booking's brand is retired refuses the booking
    (SBU_RETIRED): no new brand work starts at a retired unit."""
    fenced = SiteGuard.objects.filter(site_id__in=sites).exclude(
        stock_contract=SiteGuard.StockContract.GOODS_V1
    )
    if sites and (
        fenced.exists() or SiteGuard.objects.filter(site_id__in=sites).count() != len(sites)
    ):
        raise Refusal("CONTRACT_DISABLED", "A destination site does not use goods-v1 bookings.")
    for site in sorted(sites):
        require_active_sbu(site, brand_id, now)


def validate_booking(
    header: dict[str, Any], lines: list[dict[str, Any]], *, confirming: bool, now: datetime
) -> int | None:
    """Reference checks (BOOKING_INVALID); returns the document's home site, if one is named.

    A booking may name no destination at all and keep only its legal entity (GSA-T05).
    Retired vendors, brands, seasons and legal entities are refused here, whatever
    an older picker option offered (ticket 02D)."""
    problems: list[dict[str, Any]] = []
    vendor = Vendor.objects.filter(pk=int(header["vendor_id"])).first()
    if vendor is None or not vendor.is_active:
        problems.append(issue("UNKNOWN_VENDOR", "No active vendor has that ID.", field="vendor_id"))
    if not Brand.objects.filter(pk=int(header["brand_id"]), is_active=True).exists():
        problems.append(issue("UNKNOWN_BRAND", "No active brand has that ID.", field="brand_id"))
    season_id = int(header["season_id"])
    if not Season.objects.filter(pk=season_id).exists() or season_retired(season_id):
        problems.append(issue("UNKNOWN_SEASON", "No active season has that ID.", field="season_id"))
    entity_id = int(header["entity_id"])
    if not LegalEntity.objects.filter(pk=entity_id, is_active=True).exists():
        problems.append(
            issue("UNKNOWN_ENTITY", "No active legal entity has that ID.", field="entity_id")
        )
    sites = {int(header["destination_site_id"])} if header.get("destination_site_id") else set()
    sites |= {int(line["destination_site_id"]) for line in lines if line.get("destination_site_id")}
    entity_sites = set(
        Store.objects.filter(gstin__legal_entity_id=entity_id, pk__in=sites).values_list(
            "pk", flat=True
        )
    )
    for site in sorted(sites - entity_sites):
        problems.append(
            issue(
                "DESTINATION_OUTSIDE_ENTITY",
                f"Site {site} is not a site of this legal entity.",
                field="destination_site_id",
            )
        )
    evidence = [header.get("agreement_evidence_id"), header.get("source_evidence_id")]
    wanted = {e for e in evidence if e}
    if wanted and EvidenceObject.objects.filter(pk__in=list(wanted)).count() != len(wanted):
        problems.append(
            issue(
                "UNKNOWN_EVIDENCE", "An evidence file was not found.", field="agreement_evidence_id"
            )
        )
    keys = [line["line_key"] for line in lines]
    if len(keys) != len(set(keys)):
        problems.append(
            issue("DUPLICATE_LINE", "Each booking line needs its own line_key.", field="lines")
        )
    if confirming and not lines:
        problems.append(
            issue("NO_LINES", "A booking needs at least one line to be confirmed.", field="lines")
        )
    if problems:
        raise Refusal(
            "BOOKING_INVALID",
            "The booking cannot be saved as entered.",
            status=422,
            issues=problems,
        )
    check_destinations(sites, int(header["brand_id"]), now)
    return booking_home_site(header, lines)


def place_booking(booking: GoodsBooking, site_id: int) -> None:
    """Set the site of a booking saved with none, once (GSA-T05).

    The identity guard allows ``site_id`` to go from empty to a site exactly once,
    and only on a booking; a booking that already has a site keeps it."""
    assert booking.document.site_id is None
    DocumentIdentity.objects.filter(pk=booking.document_id, site__isnull=True).update(
        site_id=site_id
    )
    booking.document.site_id = site_id


def booking_readable(access: AccessContext, booking: GoodsBooking) -> bool:
    """Whether ``access`` may read ``booking``: its site and brand, or - for a booking
    with no site yet - a grant that reaches its legal entity without a site
    (tenant, brand, or an entity grant for that entity; GSA-T05)."""
    return any(
        access.can(
            action,
            site_id=booking.document.site_id,
            brand_id=booking.brand_id,
            entity_id=booking.document.entity_id,
        )
        for action in BOOKING_READ_ACTIONS
    )


# ---------------------------------------------------------------------------
# Booking reads: state, lines, progress
# ---------------------------------------------------------------------------


def booking_by_document(document_id: uuid.UUID) -> GoodsBooking | None:
    return GoodsBooking.objects.select_related("document").filter(document_id=document_id).first()


def booking_head(booking: GoodsBooking) -> DocumentHead:
    return DocumentHead.objects.select_related("draft_revision", "live_version").get(
        document_id=booking.document_id
    )


def booking_close_kind(booking: GoodsBooking) -> str | None:
    if booking.closed_at is None:
        return None
    event = (
        DocumentEvent.objects.filter(document_id=booking.document_id, event_kind__in=CLOSE_EVENTS)
        .order_by("-recorded_at")
        .first()
    )
    return event.event_kind if event is not None else "short_closed"


def booking_close_kinds(bookings: list[GoodsBooking]) -> dict[uuid.UUID, str]:
    """``booking_close_kind`` for many closed bookings in one query."""
    closed = [b.document_id for b in bookings if b.closed_at is not None]
    kinds: dict[uuid.UUID, str] = {}
    for document_id, kind in (
        DocumentEvent.objects.filter(document_id__in=closed, event_kind__in=CLOSE_EVENTS)
        .order_by("document_id", "-recorded_at")
        .values_list("document_id", "event_kind")
    ):
        kinds.setdefault(document_id, kind)
    return {document_id: kinds.get(document_id, "short_closed") for document_id in closed}


def booking_state(booking: GoodsBooking, head: DocumentHead) -> str:
    if head.live_version_id is None:
        return "draft"
    return booking_close_kind(booking) or "confirmed"


def _corrections(
    booking: GoodsBooking, run: CommandRun | None = None
) -> list[BookingCorrectionEvent]:
    """The booking's corrections in order; with ``run``, its own unsealed ones last."""
    rows = list(
        BookingCorrectionEvent.objects.filter(booking_id=booking.pk).order_by(
            "effective_at", "recorded_at", "id"
        )
    )
    if run is not None:
        rows += [
            row
            for row in run.evidence.pending
            if isinstance(row, BookingCorrectionEvent) and row.booking_id == booking.pk
        ]
    return rows


#: What a dated correction may change on a confirmed line.
CORRECTABLE_LINE_FIELDS = (
    "qty",
    "destination_site_id",
    "size_value_id",
    "colour_value_id",
    "description",
    "cost_paise",
)


def booking_lines(
    booking: GoodsBooking, head: DocumentHead, run: CommandRun | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """Current header and lines after corrections, and each line key's original stable key.

    ``run`` also applies a correction recorded in that command and not yet sealed.
    """
    if head.live_version_id is None:
        revision = head.draft_revision
        if revision is None:
            return {}, [], {}
        lines = [dict(s.payload) for s in revision_lines(booking.document_id, revision.revision)]
        return dict(revision.payload), lines, {line["line_key"]: line["line_key"] for line in lines}
    assert head.live_version is not None
    header = dict(head.live_version.canonical_payload)
    lines = [
        dict(line.payload)
        for line in OfficialLine.objects.filter(version_id=head.live_version_id).order_by("line_no")
    ]
    root = {line["line_key"]: line["line_key"] for line in lines}
    for correction in _corrections(booking, run):
        payload = correction.payload
        header.update(payload.get("header_changes") or {})
        for change in payload.get("line_changes") or []:
            original = change["original_line_key"]
            index = next((i for i, line in enumerate(lines) if line["line_key"] == original), None)
            if index is None:
                continue
            replaced = dict(lines[index])
            replaced["line_key"] = change["replacement_line_key"]
            for field in CORRECTABLE_LINE_FIELDS:
                if change.get(field) is not None:
                    replaced[field] = change[field]
            lines[index] = replaced
            root[change["replacement_line_key"]] = root[original]
    return header, lines, root


def _linked_totals(
    booking: GoodsBooking, root: dict[str, str]
) -> tuple[dict[str, int], dict[str, int]]:
    received: dict[str, int] = defaultdict(int)
    reversed_: dict[str, int] = defaultdict(int)
    for link in BookingReceiptLink.objects.filter(booking_id=booking.pk):
        key = str(link.booking_line_key)
        stable = root.get(key, key)
        if link.counter_of_id is None:
            received[stable] += link.linked_qty
        else:
            reversed_[stable] += link.linked_qty
    return received, reversed_


def booking_progress(
    booking: GoodsBooking, head: DocumentHead
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``BookingProgressDTO``: outstanding = booked - valid received + reversed (P1-BUY-02)."""
    from masters.goods_identity_services import value_labels

    header, lines, root = booking_lines(booking, head)
    received, reversed_ = _linked_totals(booking, root)
    labels = value_labels(booking.tenant_id)
    items = []
    for line in lines:
        stable = root.get(line["line_key"], line["line_key"])
        ordered = int(line["qty"])
        got = received[stable]
        back = reversed_[stable]
        items.append(
            {
                "line_key": line["line_key"],
                "ordered_qty": ordered,
                "received_qty": got,
                "reversed_qty": back,
                "outstanding_qty": max(0, ordered - got + back),
                # What the line is for, beside how much of it is left. Size,
                # colour and MRP are optional on a booking line; each stays
                # null when it was not given, so a reader is told "not given"
                # rather than shown a guess.
                "style_code": line.get("style_code"),
                "description": line.get("description"),
                "size_value_id": line.get("size_value_id"),
                # The words a person reads for the governed value, kept even
                # after the value is retired; null when no size was given.
                "size_label": labels.get(str(line.get("size_value_id") or "")),
                "colour_value_id": line.get("colour_value_id"),
                "colour_label": labels.get(str(line.get("colour_value_id") or "")),
                "mrp_paise": line.get("mrp_paise"),
                "cost_paise": line.get("cost_paise"),
                "destination_site_id": line.get("destination_site_id"),
            }
        )
    return header, items


@dataclass(frozen=True)
class BookingFigures:
    """How much of one booking has been received: "Booking B-104: 80 of 100 received".

    ``booked`` is the pieces on the booking's current lines (corrections applied);
    ``received`` is every receipt link less the counters that reversed one - the same
    sum the booking list's ``arrived_qty`` is. Worked out from the links on every
    read, never kept as a counter (store and warehouse operations PRD §5.1).
    """

    document_id: uuid.UUID
    number: str | None
    booked: int
    received: int


def booking_figures(bookings: Iterable[GoodsBooking]) -> dict[uuid.UUID, BookingFigures]:
    """Each booking's received-of-booked figures, keyed by the booking row's own id.

    Every receipt link of every booking asked about is read in one query rather than
    one per booking; each booking's lines are read through :func:`booking_lines`, so a
    corrected quantity counts as corrected.
    """
    rows = {booking.pk: booking for booking in bookings}
    if not rows:
        return {}
    received: dict[uuid.UUID, int] = defaultdict(int)
    for booking_pk, qty, counter in BookingReceiptLink.objects.filter(
        booking_id__in=list(rows)
    ).values_list("booking_id", "linked_qty", "counter_of_id"):
        received[booking_pk] += -qty if counter else qty
    out: dict[uuid.UUID, BookingFigures] = {}
    for pk, booking in rows.items():
        _header, lines, _root = booking_lines(booking, booking_head(booking))
        out[pk] = BookingFigures(
            document_id=booking.document_id,
            number=booking.document.official_number,
            booked=sum(int(line["qty"]) for line in lines),
            received=max(0, received[pk]),
        )
    return out


def booking_content_hash(booking: GoodsBooking, head: DocumentHead) -> str:
    if head.live_version_id is None:
        return (
            head.draft_revision.content_hash
            if head.draft_revision is not None
            else content_hash({})
        )
    assert head.live_version is not None
    return content_hash(
        {
            "version": head.live_version.content_hash,
            "revision": head.revision,
            "corrections": [str(c.pk) for c in _corrections(booking)],
            "closed": booking.close_reason,
        }
    )


# ---------------------------------------------------------------------------
# Booking commands (E108-E112, E238)
# ---------------------------------------------------------------------------


def create_booking(run: CommandRun, data: BookingInput) -> GoodsBooking:
    resolve_sizes(run.tenant_id, [line for _, line in data.lines], run.now)
    home = validate_booking(
        data.header, [line for _, line in data.lines], confirming=False, now=run.now
    )
    identity, head = new_document(
        run, kind="BKG", purpose="booking", entity_id=data.entity_id, site_id=home
    )
    append_revision(
        run, head, header=data.header, replace_lines=[(k, line) for k, line in data.lines]
    )
    booking = GoodsBooking.objects.create(
        tenant_id=run.tenant_id,
        document_id=identity.pk,
        vendor_id=data.vendor_id,
        brand_id=data.brand_id,
    )
    run.audit_after = {"booking_id": str(identity.pk), "lines": len(data.lines)}
    return booking


def _locked_booking(run: CommandRun, booking: GoodsBooking) -> DocumentHead:
    return lock_heads(run, [booking.document_id])[booking.document_id]


def update_booking(
    run: CommandRun, booking: GoodsBooking, data: BookingInput, *, expected_revision: int | None
) -> GoodsBooking:
    head = _locked_booking(run, booking)
    inp.check_revision(expected_revision, head.revision)
    if head.live_version_id is not None:
        raise Refusal(
            "STATE_CONFLICT",
            "A confirmed booking changes only through dated corrections.",
        )
    if data.entity_id != booking.document.entity_id:
        raise Refusal(
            "BOOKING_INVALID",
            "A draft booking cannot move to another legal entity; start a new booking.",
            status=422,
        )
    # The document's home site is fixed once named - at creation, or by the first
    # edit that names a destination (GSA-T05); the destination itself may still
    # change on the draft payload, which is what receiving reads.
    resolve_sizes(run.tenant_id, [line for _, line in data.lines], run.now)
    home = validate_booking(
        data.header, [line for _, line in data.lines], confirming=False, now=run.now
    )
    if booking.document.site_id is None and home is not None:
        place_booking(booking, home)
    append_revision(
        run, head, header=data.header, replace_lines=[(k, line) for k, line in data.lines]
    )
    booking.vendor_id = data.vendor_id
    booking.brand_id = data.brand_id
    booking.save(update_fields=["vendor", "brand"])
    return booking


def confirm_booking(
    run: CommandRun, booking: GoodsBooking, *, reviewed_hash: str, expected_revision: int | None
) -> str:
    """C-BUY confirmation (single actor): number the booking and freeze its lines."""
    head = _locked_booking(run, booking)
    inp.check_revision(expected_revision, head.revision)
    if head.live_version_id is not None or head.draft_revision is None:
        raise Refusal("BOOKING_INVALID", "This booking is already confirmed.", status=422)
    if reviewed_hash != head.draft_revision.content_hash:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "What you reviewed is no longer the current draft. Reload and review it again.",
        )
    header = dict(head.draft_revision.payload)
    states = revision_lines(booking.document_id, head.draft_revision.revision)
    validate_booking(header, [s.payload for s in states], confirming=True, now=run.now)
    entity = LegalEntity.objects.get(pk=booking.document.entity_id)
    number = allocate(run, entity, "BKG")
    # Ticket 39: a booking over open-to-buy needs the Owner's approval of this very
    # draft. Checked after the number's series lock, under open-to-buy's own
    # advisory key (the highest rank); a refusal rolls the number back.
    otb_ask = open_to_buy.guard_confirmation(
        run, booking, head, header, [s.payload for s in states]
    )
    assert run.principal.human_id is not None
    officialise(
        run,
        head,
        approved_by_id=run.principal.human_id,
        canonical_header=header,
        lines=[(s.line_key, s.payload) for s in states],
        authority=run.authority,
        number=number,
    )
    run.audit_after = {"booking_id": str(booking.document_id), "number": number}
    if otb_ask is not None:
        run.audit_after["open_to_buy_ask_id"] = otb_ask.pk
    return number


def close_booking(
    run: CommandRun,
    booking: GoodsBooking,
    *,
    action: str,
    reason_code: str,
    note: str | None,
    expected_revision: int | None,
) -> GoodsBooking:
    head = _locked_booking(run, booking)
    inp.check_revision(expected_revision, head.revision)
    if head.live_version_id is None or booking.closed_at is not None:
        raise Refusal("BOOKING_CLOSE_INVALID", "Only an open, confirmed booking can be closed.")
    if action == "cancel":
        _header, items = booking_progress(booking, head)
        if any(item["received_qty"] - item["reversed_qty"] > 0 for item in items):
            raise Refusal(
                "BOOKING_CLOSE_INVALID",
                "Goods have been received against this booking; short-close it instead.",
            )
    booking.closed_at = run.now
    booking.close_reason = reason_code
    booking.save(update_fields=["closed_at", "close_reason"])
    kind = "short_closed" if action == "short_close" else "cancelled"
    record_event(
        run,
        booking.document_id,
        kind,
        reason_code=reason_code,
        version_id=head.live_version_id,
        payload={
            "from_state": "confirmed",
            "to_state": kind,
            "reason_code": reason_code,
            "details": [issue("NOTE", note)] if note else [],
        },
    )
    inp.save_head_revision(head)
    return booking


def link_receipts(
    run: CommandRun,
    booking: GoodsBooking,
    *,
    grn_document_id: uuid.UUID,
    links: list[dict[str, Any]],
    reason_code: str,
    effective_at: datetime,
    expected_revision: int | None,
) -> list[BookingReceiptLink]:
    """Append positive receipt links or exact counters; progress is re-derived, nothing edited."""
    from inbound.goods_models import GoodsGrn
    from inbound.goods_services import effective_count

    heads = lock_heads(run, [booking.document_id, grn_document_id])
    head = heads[booking.document_id]
    inp.check_revision(expected_revision, head.revision)
    grn = (
        GoodsGrn.objects.select_related("document", "arrival")
        .filter(document_id=grn_document_id)
        .first()
    )
    if grn is None:
        raise Refusal("NOT_FOUND", "That GRN was not found.")
    rows = _append_links(
        run,
        booking,
        head,
        grn_document_id=grn_document_id,
        arrival_id=grn.arrival_id,
        counts=effective_count(grn_document_id),
        links=links,
        reason_code=reason_code,
        effective_at=effective_at,
    )
    # Ticket 37: a link names the booked figure of the lines it joins, and of every
    # later delivery of this booking, whose open quantity it changes.
    for received in _with(three_way_match.grns_of_booking(booking), grn):
        three_way_match.refresh(run, received)
    return rows


def _with(grns: list[Any], grn: Any) -> list[Any]:
    """``grns`` with ``grn`` in it once."""
    return grns if any(g.pk == grn.pk for g in grns) else [*grns, grn]


def _append_links(
    run: CommandRun,
    booking: GoodsBooking,
    head: DocumentHead,
    *,
    grn_document_id: uuid.UUID,
    arrival_id: uuid.UUID,
    counts: dict[uuid.UUID, int],
    links: list[dict[str, Any]],
    reason_code: str,
    effective_at: datetime,
) -> list[BookingReceiptLink]:
    """The receipt-link command's validation and append, on a booking head already locked.

    Shared by the buyer's own command (E112) and the exact-match link made when a GRN
    is issued (store and warehouse operations PRD §5.1), so both keep one set of
    rules, one date and the actor of the command that runs them. ``counts`` is each
    GRN line's counted quantity as it stands.
    """
    if head.live_version_id is None or booking_close_kind(booking) == "cancelled":
        raise Refusal(
            "BOOKING_LINK_INVALID",
            "Receipts link only to a confirmed booking that is not cancelled.",
        )
    # GSA-T05: goods are received against a booking only once it names a destination,
    # whoever the caller is.
    if booking.document.site_id is None:
        raise Refusal(
            "BOOKING_LINK_INVALID", "Name the booking's destination before linking receipts."
        )
    problems: list[dict[str, Any]] = []
    if effective_at > run.now:
        problems.append(
            issue("FUTURE", "A link cannot be dated in the future.", field="effective_at")
        )
    _header, _lines, root = booking_lines(booking, head)
    stored = list(BookingReceiptLink.objects.filter(grn_id=grn_document_id))
    countered = {link.counter_of_id for link in stored if link.counter_of_id is not None}
    by_id = {link.pk: link for link in stored}
    on_grn_line: dict[uuid.UUID, int] = defaultdict(int)
    active_pairs: set[tuple[str, uuid.UUID]] = set()
    for link in stored:
        sign = -1 if link.counter_of_id else 1
        on_grn_line[link.grn_line_key] += sign * link.linked_qty
        key = str(link.booking_line_key)
        if (
            link.booking_id == booking.pk
            and link.counter_of_id is None
            and link.pk not in countered
        ):
            active_pairs.add((root.get(key, key), link.grn_line_key))
    rows: list[BookingReceiptLink] = []
    for index, entry in enumerate(links):
        field = f"links[{index}]"
        booking_key = str(entry["booking_line_key"])
        grn_key = entry["grn_line_key"]
        qty = entry["qty"]
        stable = root.get(booking_key)
        if stable is None:
            problems.append(
                issue("UNKNOWN_BOOKING_LINE", "That booking line does not exist.", field=field)
            )
            continue
        if grn_key not in counts:
            problems.append(issue("UNKNOWN_GRN_LINE", "That GRN line does not exist.", field=field))
            continue
        counter_of = entry["counter_of_id"]
        if counter_of is not None:
            target = by_id.get(counter_of)
            target_key = str(target.booking_line_key) if target is not None else ""
            if (
                target is None
                or target.booking_id != booking.pk
                or target.counter_of_id is not None
                or target.pk in countered
                or target.grn_line_key != grn_key
                or root.get(target_key, target_key) != stable
                or target.linked_qty != qty
            ):
                problems.append(
                    issue(
                        "COUNTER_INVALID",
                        "A counter reverses exactly one live link of the same lines and quantity.",
                        field=field,
                    )
                )
                continue
            countered.add(target.pk)
            on_grn_line[grn_key] -= qty
            active_pairs.discard((stable, grn_key))
        else:
            if (stable, grn_key) in active_pairs:
                problems.append(
                    issue("DUPLICATE_LINK", "These lines are already linked.", field=field)
                )
                continue
            if on_grn_line[grn_key] + qty > counts[grn_key]:
                problems.append(
                    issue(
                        "EXCEEDS_RECEIVED",
                        f"{counts[grn_key] - on_grn_line[grn_key]} received piece(s) are unlinked.",
                        field=field,
                        quantity=counts[grn_key] - on_grn_line[grn_key],
                    )
                )
                continue
            on_grn_line[grn_key] += qty
            active_pairs.add((stable, grn_key))
        rows.append(
            BookingReceiptLink(
                booking_id=booking.pk,
                booking_line_key=uuid.UUID(booking_key),
                grn_line_key=grn_key,
                grn_id=grn_document_id,
                linked_qty=qty,
                counter_of_id=counter_of,
            )
        )
    if problems:
        raise Refusal(
            "BOOKING_LINK_INVALID", "The receipt links cannot be recorded.", issues=problems
        )
    for row in rows:
        run.record(row, event_at=effective_at)
    record_event(
        run,
        booking.document_id,
        "receipt_linked",
        reason_code=reason_code,
        version_id=head.live_version_id,
        payload={
            "related_document_id": str(grn_document_id),
            "reason_code": reason_code,
            "details": [],
        },
    )
    inp.save_head_revision(head)
    _close_unbooked_arrival(run, arrival_id, rows)
    run.audit_after = {"links": len(rows), "grn_id": str(grn_document_id)}
    return rows


#: The reason a receipt link made at GRN issue carries (PRD §5.1, Anand 23 September 2026).
EXACT_MATCH_REASON = "GRN_EXACT_MATCH"


def lock_booking_for_receipt(
    run: CommandRun, booking_pk: uuid.UUID | None
) -> tuple[GoodsBooking, DocumentHead] | None:
    """Lock the booking an arrival was received against, before a GRN is issued for it.

    Taken at DOCUMENT rank beside the arrival's own head, so the exact-match links the
    GRN issue appends later in the same command respect the fixed lock order.
    """
    if booking_pk is None:
        return None
    document_id = (
        GoodsBooking.objects.filter(pk=booking_pk).values_list("document_id", flat=True).first()
    )
    if document_id is None:
        return None
    head = lock_heads(run, [document_id]).get(document_id)
    # Read after the lock, so a close recorded meanwhile is seen.
    booking = GoodsBooking.objects.select_related("document").filter(pk=booking_pk).first()
    return (booking, head) if head is not None and booking is not None else None


def exact_booking_line(
    lines: list[dict[str, Any]],
    *,
    style_code: str,
    size_value_id: str | None,
    colour_value_id: str | None,
) -> dict[str, Any] | None:
    """The one booking line an item of this style, size and colour is exactly, or None.

    The same style code and the same size, on exactly one line; a booking line with
    no size given matches nothing. That one line is still no match when its colour is
    given and differs from the item's known colour. Several candidates are None:
    nothing is guessed.
    """
    from masters.goods_identity_services import normalise_text

    if not size_value_id:
        return None
    same = [
        line
        for line in lines
        if normalise_text(str(line.get("style_code") or "")) == normalise_text(style_code)
        and line.get("size_value_id")
        and str(line["size_value_id"]) == str(size_value_id)
    ]
    if len(same) != 1:
        return None
    colour = same[0].get("colour_value_id")
    if colour and colour_value_id and str(colour) != str(colour_value_id):
        return None
    return same[0]


def exact_booking_matches(
    booking: GoodsBooking, head: DocumentHead, grn_lines: list[tuple[uuid.UUID, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Receipt links for the counted lines that match exactly one booking line.

    A match is the same style (same brand, same style code) and the same size - a
    booking line with no size given matches nothing, and neither does a counted line
    with no identified item or no known size. A booking line whose colour is given and
    differs from the item's known colour is no match either. A counted line matching
    no booking line, or several, is left for the buyer: nothing is guessed.
    """
    from masters.goods_identity_services import sku_booking_facts

    _header, lines, _root = booking_lines(booking, head)
    sku_ids = {
        str(payload["identity"]["sku_id"])
        for _key, payload in grn_lines
        if (payload.get("identity") or {}).get("sku_id")
    }
    facts = sku_booking_facts(booking.tenant_id, sku_ids)
    links: list[dict[str, Any]] = []
    for key, payload in grn_lines:
        qty = int(payload.get("qty") or 0)
        sku = facts.get(str((payload.get("identity") or {}).get("sku_id") or ""))
        if qty <= 0 or sku is None or sku.size_value_id is None:
            continue
        if sku.brand_id != booking.brand_id:
            continue
        match = exact_booking_line(
            lines,
            style_code=sku.style_code,
            size_value_id=sku.size_value_id,
            colour_value_id=sku.colour_value_id,
        )
        if match is None:
            continue
        links.append(
            {
                "booking_line_key": uuid.UUID(str(match["line_key"])),
                "grn_line_key": key,
                "qty": qty,
                "counter_of_id": None,
            }
        )
    return links


def link_exact_matches(
    run: CommandRun,
    locked: tuple[GoodsBooking, DocumentHead] | None,
    *,
    grn_document_id: uuid.UUID,
    arrival_id: uuid.UUID,
    grn_lines: list[tuple[uuid.UUID, dict[str, Any]]],
) -> list[BookingReceiptLink]:
    """At GRN issue, link each counted line that matches exactly one booking line.

    Through the receipt-link command's own validation and append (:func:`_append_links`),
    dated now and recorded as the GRN issuer's. A booking that could not take a link
    from the buyer either - not confirmed, cancelled, no destination - takes none here,
    and the GRN is issued all the same; its lines wait for the buyer.
    """
    if locked is None:
        return []
    booking, head = locked
    if (
        head.live_version_id is None
        or booking_close_kind(booking) == "cancelled"
        or booking.document.site_id is None
    ):
        return []
    links = exact_booking_matches(booking, head, grn_lines)
    if not links:
        return []
    return _append_links(
        run,
        booking,
        head,
        grn_document_id=grn_document_id,
        arrival_id=arrival_id,
        counts={key: int(payload.get("qty") or 0) for key, payload in grn_lines},
        links=links,
        reason_code=EXACT_MATCH_REASON,
        effective_at=run.now,
    )


def _close_unbooked_arrival(
    run: CommandRun, arrival_id: uuid.UUID, new_rows: list[BookingReceiptLink]
) -> None:
    from inbound.goods_models import GoodsGrn

    documents = list(
        GoodsGrn.objects.filter(arrival_id=arrival_id).values_list("document_id", flat=True)
    )
    if not documents:
        return
    active: dict[uuid.UUID, int] = defaultdict(int)
    for link in [*BookingReceiptLink.objects.filter(grn_id__in=documents), *new_rows]:
        active[link.grn_id] += -link.linked_qty if link.counter_of_id else link.linked_qty
    if all(active[document] > 0 for document in documents):
        resolve_exceptions(
            run,
            kind="unbooked_arrival",
            subject_key=f"arrival:{arrival_id}",
            reason_code="BOOKING_LINKED",
        )


CORRECTION_KEYS = frozenset(
    {"reason_code", "effective_at", "evidence_ids", "header_changes", "line_changes"}
)
HEADER_CHANGE_KEYS = frozenset({"expected_date", "destination_site_id", "notes"})
LINE_CHANGE_KEYS = frozenset(
    {
        "original_line_key",
        "replacement_line_key",
        "qty",
        "destination_site_id",
        "size_value_id",
        "colour_value_id",
        "description",
        "cost_paise",
    }
)


def parse_correction(body: dict[str, Any]) -> dict[str, Any]:
    header_raw = inp.closed(body.get("header_changes") or {}, HEADER_CHANGE_KEYS, "header_changes")
    header: dict[str, Any] = {}
    if "expected_date" in header_raw:
        header["expected_date"] = _opt_str(
            inp.optional_day(header_raw["expected_date"], "expected_date")
        )
    if "destination_site_id" in header_raw:
        header["destination_site_id"] = str(
            inp.legacy_id(header_raw["destination_site_id"], "destination_site_id")
        )
    if "notes" in header_raw:
        header["notes"] = inp.text(header_raw["notes"], "notes", 1000)
    changes = []
    for index, raw in enumerate(inp.object_list(body.get("line_changes") or [], "line_changes")):
        field = f"line_changes[{index}]"
        change = inp.closed(
            raw, LINE_CHANGE_KEYS, field, required=["original_line_key", "replacement_line_key"]
        )
        entry: dict[str, Any] = {
            "original_line_key": str(
                inp.uuid_value(change["original_line_key"], f"{field}.original_line_key")
            ),
            "replacement_line_key": str(
                inp.uuid_value(change["replacement_line_key"], f"{field}.replacement_line_key")
            ),
        }
        if change.get("qty") is not None:
            entry["qty"] = inp.quantity(change["qty"], f"{field}.qty")
        if change.get("destination_site_id") is not None:
            entry["destination_site_id"] = str(
                inp.legacy_id(change["destination_site_id"], f"{field}.destination_site_id")
            )
        for vocabulary in ("size_value_id", "colour_value_id"):
            if change.get(vocabulary) is not None:
                entry[vocabulary] = inp.vocabulary_id(change[vocabulary], f"{field}.{vocabulary}")
        if change.get("description") is not None:
            entry["description"] = inp.text(change["description"], f"{field}.description", 200)
        if change.get("cost_paise") is not None:
            entry["cost_paise"] = inp.money(change["cost_paise"], f"{field}.cost_paise")
        changes.append(entry)
    return {
        "reason_code": inp.text(body.get("reason_code"), "reason_code", 60, required=True),
        "effective_at": inp.timestamp(body.get("effective_at"), "effective_at").isoformat(),
        "evidence_ids": [str(e) for e in inp.id_list(body.get("evidence_ids"), "evidence_ids")],
        "header_changes": header,
        "line_changes": changes,
    }


def correction_home(payload: dict[str, Any]) -> int | None:
    """The site a correction names first: its header destination, else a line's."""
    named = [payload["header_changes"].get("destination_site_id")] + [
        change.get("destination_site_id") for change in payload["line_changes"]
    ]
    return next((int(site) for site in named if site), None)


def correct_booking(
    run: CommandRun,
    booking: GoodsBooking,
    payload: dict[str, Any],
    *,
    expected_revision: int | None,
) -> BookingCorrectionEvent:
    head = _locked_booking(run, booking)
    inp.check_revision(expected_revision, head.revision)
    if head.live_version_id is None or booking_close_kind(booking) == "cancelled":
        raise Refusal(
            "BOOKING_CORRECTION_INVALID", "Only a confirmed, uncancelled booking can be corrected."
        )
    effective_at = inp.timestamp(payload["effective_at"], "effective_at")
    before = booking_lines(booking, head)
    _header, lines, root = before
    current = {line["line_key"] for line in lines}
    received, reversed_ = _linked_totals(booking, root)
    problems: list[dict[str, Any]] = []
    previous = _corrections(booking)
    if previous and effective_at < previous[-1].effective_at:
        problems.append(
            issue(
                "EFFECTIVE_BEFORE_PREVIOUS",
                "A correction cannot be dated before the last one.",
                field="effective_at",
            )
        )
    if not payload["header_changes"] and not payload["line_changes"]:
        problems.append(issue("NO_CHANGE", "A correction must change the header or a line."))
    replacements: set[str] = set()
    for index, change in enumerate(payload["line_changes"]):
        field = f"line_changes[{index}]"
        original = change["original_line_key"]
        replacement = change["replacement_line_key"]
        if original not in current:
            problems.append(
                issue("NOT_CURRENT_LINE", "Correct the current version of a line.", field=field)
            )
            continue
        if replacement in root or replacement in replacements:
            problems.append(
                issue("REPLACEMENT_KEY_USED", "Each replacement needs a new line key.", field=field)
            )
        replacements.add(replacement)
        stable = root[original]
        linked = received[stable] - reversed_[stable]
        if change.get("qty") is not None and change["qty"] < linked:
            problems.append(
                issue(
                    "BELOW_RECEIVED",
                    f"{linked} piece(s) are already linked as received on this line.",
                    field=field,
                    quantity=linked,
                )
            )
    sites = [payload["header_changes"].get("destination_site_id")] + [
        c.get("destination_site_id") for c in payload["line_changes"]
    ]
    wanted_sites = {int(s) for s in sites if s}
    if wanted_sites and Store.objects.filter(
        pk__in=wanted_sites, gstin__legal_entity_id=booking.document.entity_id
    ).count() != len(wanted_sites):
        problems.append(
            issue("DESTINATION_OUTSIDE_ENTITY", "A destination is not a site of this legal entity.")
        )
    home = correction_home(payload)
    evidence = set(payload["evidence_ids"])
    if evidence and EvidenceObject.objects.filter(pk__in=list(evidence)).count() != len(evidence):
        problems.append(
            issue("UNKNOWN_EVIDENCE", "An evidence file was not found.", field="evidence_ids")
        )
    if problems:
        raise Refusal(
            "BOOKING_CORRECTION_INVALID", "The correction cannot be recorded.", issues=problems
        )
    assert head.live_version_id is not None
    if booking.document.site_id is None and home is not None:
        # The first destination named on a booking confirmed without one sets its
        # site, and the goods-v1 contract and retired-SBU checks run now (GSA-T05).
        check_destinations(wanted_sites, booking.brand_id, run.now)
        place_booking(booking, home)
    event = BookingCorrectionEvent(
        booking_id=booking.pk,
        prior_version_id=head.live_version_id,
        reason_code=payload["reason_code"],
        effective_at=effective_at,
        payload=payload,
        evidence_ids=payload["evidence_ids"],
    )
    run.record(event, event_at=effective_at)
    # Ticket 39: a correction that uses more of a budget than the booking did is
    # judged against open-to-buy, with this correction applied.
    open_to_buy.guard_correction(run, booking, before, booking_lines(booking, head, run))
    inp.save_head_revision(head)
    # Ticket 37: a corrected quantity or cost is a new booked figure for every
    # delivery already received against this booking.
    for grn in three_way_match.grns_of_booking(booking):
        three_way_match.refresh(run, grn)
    run.audit_after = {"correction_id": str(event.pk), "reason_code": payload["reason_code"]}
    return event

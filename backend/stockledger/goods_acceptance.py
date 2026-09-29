"""Physical acceptance and putaway against an official source version (design §3.3, E139-E142).

Approval makes goods valued; only acceptance makes them usable. A receiver scans
the physical tag, the scan is compared with the frozen official line (alias and
ticket MRP) and the tag's own label evidence, and only then are exact covered
portions moved into an eligible location with acceptance evidence (P09). Damage
found at acceptance goes straight to quarantine under a damage hold (P10).
Completing a session never turns unscanned quantity into a shortage.

Receipt and opening PT versions are accepted here. Their goods were already
counted into custody at the GRN or manifest, so:

* ``checked_good`` records that the tag matched and the pieces are good, and moves
  nothing - the checked portions are taken first by the later putaway;
* ``wrong`` records an observation and an exception, never a second physical lot
  (that would count the same goods twice).

Lock order: SITE (the site guard) -> DOCUMENT (session, then source head) -> LOT.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils.dateparse import parse_datetime

from alerts.goods_models import GoodsException
from alerts.goods_services import add_exception_event, open_exception, resolve_exceptions
from core.commands import CommandRun, LockRank
from core.goods_fields import bounds, portion
from core.goods_money import MoneyInvalid, paise_from_json
from core.kernel_models import DocumentHead, DocumentIdentity, OfficialLine, OfficialVersion
from core.refusals import Refusal, issue
from inbound.goods_models import ScanObservation
from masters.goods_identity_models import ProductSku, SkuAlias
from masters.goods_models import Location, SiteGuard, Tenant
from ptmapper.goods_models import GoodsPt, PrintJob
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import (
    AcceptanceEvent,
    AcceptanceSession,
    ActiveHold,
    CustodyLot,
    LiveCoverage,
    Position,
)

#: Document kinds and purposes of the sources accepted in this stage: receipt and
#: opening PTs, and (goods ticket 15A, design §7.2 P09) an approved new-found
#: adjustment, whose valued found goods are put away against its own lines.
SOURCE_PT_KINDS = frozenset({"RPT", "OPT", "ADJ"})
SOURCE_PURPOSES = frozenset({"receipt", "opening", "adjustment_up"})
FOUND_PURPOSE = "adjustment_up"
#: Non-system location kinds accepted goods may be put away into.
PUTAWAY_KINDS = engine.TRANSFERABLE_KINDS
OUTCOMES = ("checked_good", "accepted_good", "damaged", "wrong")
CONDITIONS = ("good", "damaged", "wrong")
OUTCOME_CONDITION = {"checked_good": "good", "accepted_good": "good", "damaged": "damaged"}
#: A print job whose labels may be on goods: sent to a printer, confirmed, or
#: confirmed for only some of its lines (GSA-T11) - a partial outcome still means
#: some of this job's labels really printed.
PRINTED = ("attempted", "confirmed", "partial")
MAX_OBSERVATIONS = 500
MAX_QTY = 999_999
REMAINING_KIND = "acceptance_remaining"
DISCREPANCY_KIND = "acceptance_discrepancy"
#: Ticket 07B: extra pieces handed over from acceptance to whoever owns the receipt's
#: correction. Keyed to the goods receipt, not the PT: the fix is on the receipt.
EXTRA_REASON = "EXTRA_AT_ACCEPTANCE"
EXTRA_FIELDS = frozenset({"alias_value", "qty", "official_line_id", "note"})
MAX_NOTE = 500
SCAN_FIELDS = frozenset(
    {
        "scan_key",
        "official_line_id",
        "alias_value",
        "observed_ticket_mrp_paise",
        "label_evidence_id",
        "chosen_sku_id",
        "candidate_hash",
        "qty",
        "condition",
        "location_id",
        "outcome",
        "actual_at",
    }
)
HEX = frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# Input: AcceptanceScanInput[]
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scan:
    key: uuid.UUID
    outcome: str
    condition: str
    qty: int
    alias_value: str
    observed_mrp: str | None
    line_id: str | None
    label_evidence_id: uuid.UUID | None
    chosen_sku_id: uuid.UUID | None
    location_id: uuid.UUID | None
    actual_at: datetime


def _invalid(message: str, field: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)])


def _optional_uuid(value: Any, field: str) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise _invalid(f"{field} must be an ID.", field) from None


def _timestamp(value: Any, field: str) -> datetime:
    try:
        moment = parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise _invalid("actual_at must be a timestamp with an offset.", field)
    return moment


def parse_scans(raw: Any) -> list[Scan]:
    """Validate ``observations`` against the closed AcceptanceScanInput schema."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OBSERVATIONS:
        raise _invalid(f"observations must list 1 to {MAX_OBSERVATIONS} scans.", "observations")
    scans: list[Scan] = []
    seen: set[uuid.UUID] = set()
    for index, item in enumerate(raw):
        at = f"observations[{index}]"
        parsed = _parse_scan(item, at)
        if parsed.key in seen:
            raise _invalid("A scan_key appears twice in this request.", f"{at}.scan_key")
        seen.add(parsed.key)
        scans.append(parsed)
    return scans


def _parse_scan(item: Any, at: str) -> Scan:
    if not isinstance(item, dict):
        raise _invalid("Each scan must be an object.", at)
    unknown = sorted(set(item) - SCAN_FIELDS)
    if unknown:
        raise _invalid(f"Unknown scan field(s): {', '.join(unknown)}.", at)
    key = _optional_uuid(item.get("scan_key"), f"{at}.scan_key")
    if key is None:
        raise _invalid("Every scan needs a scan_key.", f"{at}.scan_key")
    outcome = item.get("outcome")
    if outcome not in OUTCOMES:
        raise _invalid(
            "outcome must be checked_good, accepted_good, damaged or wrong.", f"{at}.outcome"
        )
    condition = item.get("condition")
    if condition not in CONDITIONS:
        raise _invalid("condition must be good, damaged or wrong.", f"{at}.condition")
    qty = item.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
        raise _invalid("qty must be a whole number from 1 to 999999.", f"{at}.qty")
    alias, mrp, label = _tag_fields(item, at, str(outcome))
    line_id = _optional_uuid(item.get("official_line_id"), f"{at}.official_line_id")
    return Scan(
        key=key,
        outcome=str(outcome),
        condition=str(condition),
        qty=qty,
        alias_value=alias,
        observed_mrp=mrp,
        line_id=str(line_id) if line_id else None,
        label_evidence_id=label,
        chosen_sku_id=_optional_uuid(item.get("chosen_sku_id"), f"{at}.chosen_sku_id"),
        location_id=_optional_uuid(item.get("location_id"), f"{at}.location_id"),
        actual_at=_timestamp(item.get("actual_at"), f"{at}.actual_at"),
    )


def _tag_fields(
    item: dict[str, Any], at: str, outcome: str
) -> tuple[str, str | None, uuid.UUID | None]:
    """What the tag says: its text, its printed MRP and the tag's own label record."""
    alias = item.get("alias_value")
    if not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 128:
        raise _invalid("alias_value must be the scanned tag text.", f"{at}.alias_value")
    try:
        paise = paise_from_json(item.get("observed_ticket_mrp_paise"))
    except MoneyInvalid:
        raise _invalid(
            "observed_ticket_mrp_paise must be integer paise.", f"{at}.observed_ticket_mrp_paise"
        ) from None
    mrp = None if paise is None else str(paise)
    # GSA-T07: a physical Code 128 carries only the frozen printable alias, so a
    # receiver has no opaque evidence ID to type and the screen has none to send.
    # Omitting it means "derive it from the official line's own frozen source or
    # print job" (below). A caller that does send one is still checked against
    # that same source, so a wrong id is still a TAG_MISMATCH.
    label = _optional_uuid(item.get("label_evidence_id"), f"{at}.label_evidence_id")
    if outcome != "wrong" and mrp is None:
        raise _invalid("A scan of PT goods needs the ticket MRP off the tag.", at)
    candidate = item.get("candidate_hash")
    if candidate is not None and (
        not isinstance(candidate, str) or len(candidate) != 64 or not set(candidate) <= HEX
    ):
        raise _invalid("candidate_hash must be 64 hex characters.", f"{at}.candidate_hash")
    return alias.strip(), mrp, label


# ---------------------------------------------------------------------------
# Fences
# ---------------------------------------------------------------------------


def _guard(locked: list[Any]) -> SiteGuard:
    guard: SiteGuard | None = locked[0] if locked else None
    if guard is None or guard.stock_contract != SiteGuard.StockContract.GOODS_V1:
        raise Refusal("CONTRACT_DISABLED", "This site is not on the goods-v1 stock contract.")
    return guard


def _check_ready(guard: SiteGuard, purpose: str) -> None:
    """Only opening-source acceptance may run on an opening-setup site.

    GSA-T10: opening acceptance also stays blocked by OQ-54 on a real tenant,
    defence in depth alongside manifest creation, opening PT creation and
    opening PT approval - never a route a real tenant's site setup could open.
    """
    if purpose == "opening":
        tenant = Tenant.objects.filter(pk=guard.tenant_id).first()
        if tenant is None or not tenant.synthetic:
            raise Refusal(
                "OPENING_NOT_READY", "Real opening stock stays blocked until OQ-54 is resolved."
            )
    if not (guard.goods_ready or (purpose == "opening" and guard.opening_setup_ready)):
        raise Refusal("SITE_NOT_READY", "This site is not approved for goods acceptance.")
    if guard.freeze_id:
        raise Refusal("UNDER_COUNT", "A stock count freezes this site.")


def _check_live_source(version: OfficialVersion, site_id: int, head: DocumentHead | None) -> None:
    document = version.document
    if (
        document.kind not in SOURCE_PT_KINDS
        or document.purpose not in SOURCE_PURPOSES
        or document.site_id != site_id
        or head is None
        or head.state != DocumentHead.State.OFFICIAL
        or head.live_version_id != version.pk
    ):
        raise Refusal(
            "ACCEPTANCE_NOT_READY",
            "There is no live official receipt or opening PT to accept at this site.",
        )


def _lock_session(
    run: CommandRun,
    session_id: uuid.UUID,
    expected_revision: int | None,
    *,
    check_revision: bool = True,
) -> tuple[AcceptanceSession, SiteGuard, OfficialVersion, DocumentHead | None]:
    site_id = (
        AcceptanceSession.objects.filter(pk=session_id).values_list("site_id", flat=True).first()
    )
    if site_id is None:
        raise Refusal("NOT_FOUND", "That acceptance session was not found.")
    guards = run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=site_id))
    sessions = run.lock(LockRank.DOCUMENT, AcceptanceSession.objects.filter(pk=session_id))
    session: AcceptanceSession = sessions[0]
    version = OfficialVersion.objects.select_related("document").get(pk=session.source_version_id)
    heads = run.lock(
        LockRank.DOCUMENT, DocumentHead.objects.filter(document_id=version.document_id)
    )
    guard = _guard(guards)
    if check_revision and expected_revision != session.revision:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Someone scanned into this session after you loaded it. Reload and scan again.",
        )
    return session, guard, version, heads[0] if heads else None


def version_is_live(version: OfficialVersion) -> bool:
    """Whether ``version`` is still its PT's live official version (an unlocked read)."""
    return _is_live(version, DocumentHead.objects.filter(document_id=version.document_id).first())


def _is_live(version: OfficialVersion, head: DocumentHead | None) -> bool:
    return (
        head is not None
        and head.state == DocumentHead.State.OFFICIAL
        and head.live_version_id == version.pk
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def open_session(
    run: CommandRun, *, site_id: int, version_id: uuid.UUID
) -> tuple[AcceptanceSession, bool]:
    """E139: a resumable session against a live receipt or opening PT version.

    Returns the session and whether it was created (an open session is resumed).
    """
    human_id = run.principal.human_id
    if human_id is None:
        raise Refusal("ACTION_DENIED", "Only a person can accept goods.")
    guard = _guard(run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=site_id)))
    version = OfficialVersion.objects.select_related("document").filter(pk=version_id).first()
    if version is None:
        raise Refusal("NOT_FOUND", "That PT version was not found.")
    _check_ready(guard, version.document.purpose)
    heads = run.lock(
        LockRank.DOCUMENT, DocumentHead.objects.filter(document_id=version.document_id)
    )
    _check_live_source(version, site_id, heads[0] if heads else None)
    existing = AcceptanceSession.objects.filter(
        site_id=site_id, source_version_id=version.pk, state=AcceptanceSession.State.OPEN
    ).first()
    run.audit_subject_key = f"document:{version.document_id}"
    run.audit_site_id = site_id
    if existing is not None:
        run.audit_after = {"session_id": str(existing.pk), "resumed": True}
        return existing, False
    session = AcceptanceSession.objects.create(
        tenant_id=run.tenant_id,
        site_id=site_id,
        source_version_id=version.pk,
        opened_by_id=human_id,
        last_activity_at=run.now,
    )
    run.audit_after = {"session_id": str(session.pk), "source_version_id": str(version.pk)}
    return session, True


def scan(
    run: CommandRun, session_id: uuid.UUID, scans: list[Scan], expected_revision: int | None
) -> AcceptanceSession:
    """E141: compare every tag first, then post each new scan exactly once."""
    session, guard, version, head = _lock_session(run, session_id, expected_revision)
    _check_ready(guard, version.document.purpose)
    if session.state != AcceptanceSession.State.OPEN:
        # E141 lists no state code: a closed session is not a valid scan target (step 15).
        raise Refusal(
            "ACCEPTANCE_INVALID",
            "This acceptance session is closed. Open a session to keep scanning.",
            status=422,
            issues=[issue("SESSION_CLOSED", "The session is closed")],
        )
    _check_live_source(version, session.site_id, head)
    fresh = _fresh(session, scans)
    run.audit_subject_key = f"document:{version.document_id}"
    run.audit_site_id = session.site_id
    if fresh:
        source = _Source.load(version, session.site_id, run.now)
        resolved = _resolve_all(source, fresh)
        touched = [item.line.pk for item in resolved if item.line is not None]
        engine.lock_lots(run, _source_lot_ids(version, touched))
        for item in resolved:
            _apply(run, session, source, item)
        session.revision += 1
        session.last_activity_at = run.now
        session.save(update_fields=["revision", "last_activity_at"])
        remaining = sum(
            row["remaining_qty"] for row in line_progress(version, _events(run, source))
        )
        if remaining == 0:
            resolve_exceptions(
                run,
                kind=REMAINING_KIND,
                subject_key=f"document:{version.document_id}",
                source_event_key=version.pk,
            )
    run.audit_after = {
        "session_id": str(session.pk),
        "acknowledged": sorted(str(item.key) for item in scans),
        "posted": sorted(str(item.key) for item in fresh),
    }
    return session


def complete(
    run: CommandRun,
    session_id: uuid.UUID,
    *,
    confirm_complete: bool,
    reason_code: str | None,
    expected_revision: int | None,
) -> AcceptanceSession:
    """E142: close the session; remaining quantity stays visible and is never written off.

    Completing an already closed session repeats nothing and changes nothing. The
    remaining-work exception belongs to one official version: only a session of
    the version that is still live may keep it open or resolve it, so a stale
    session of a reversed version never touches its reissue's work.
    """
    session, _guard_row, version, head = _lock_session(run, session_id, expected_revision)
    subject = f"document:{version.document_id}"
    run.audit_subject_key = subject
    run.audit_site_id = session.site_id
    if session.state != AcceptanceSession.State.OPEN:
        run.audit_after = {"state": session.state, "already_closed": True}
        return session
    remaining = sum(row["remaining_qty"] for row in line_progress(version))
    if remaining and not confirm_complete:
        raise Refusal(
            "SESSION_INCOMPLETE",
            f"{remaining} piece(s) are still not accepted. Confirm to close and leave them open.",
        )
    session.state = AcceptanceSession.State.COMPLETED
    session.revision += 1
    session.last_activity_at = run.now
    session.save(update_fields=["state", "revision", "last_activity_at"])
    run.audit_before = {"state": "open"}
    run.audit_after = {
        "state": "completed",
        "remaining_qty": remaining,
        "reason_code": reason_code,
    }
    if not _is_live(version, head):
        # The reversal already resolved this version's work; nothing of it remains to track.
        return session
    if remaining == 0:
        resolve_exceptions(
            run, kind=REMAINING_KIND, subject_key=subject, source_event_key=version.pk
        )
    else:
        # The approval opened this exception; keep (or restore) the remaining work visible.
        open_exception(
            run,
            kind=REMAINING_KIND,
            site_id=session.site_id,
            subject_key=subject,
            reason_code="AWAITING_ACCEPTANCE",
            source_event_key=version.pk,
            allowed_resolution_actions=["stockledger/acceptance-sessions"],
        )
    return session


# ---------------------------------------------------------------------------
# Ticket 07B: extra pieces - the way back, and the handoff (E251)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtraReport:
    """What a person found beyond the official PT: the tag, how many, and why."""

    alias_value: str
    qty: int
    line_id: str | None
    note: str | None


def parse_extra_report(body: dict[str, Any]) -> ExtraReport:
    """Validate E251's closed body. Nothing here is an acceptance scan."""
    alias = body.get("alias_value")
    if not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 128:
        raise _invalid("alias_value must be the scanned tag text.", "alias_value")
    qty = body.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
        raise _invalid("qty must be a whole number from 1 to 999999.", "qty")
    line_id = _optional_uuid(body.get("official_line_id"), "official_line_id")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise _invalid(f"note is at most {MAX_NOTE} characters.", "note")
    return ExtraReport(
        alias_value=alias.strip(),
        qty=qty,
        line_id=str(line_id) if line_id else None,
        note=(note or "").strip() or None,
    )


def receipt_source(version: OfficialVersion) -> GoodsPt | None:
    """The receipt PT behind a session's version, with the goods receipt it covers.

    ``None`` for an opening PT (or anything else without a GRN): the way back
    for extra pieces is the goods receipt's count, and only a receipt PT has one.
    """
    if version.document.purpose != "receipt":
        return None
    pt: GoodsPt | None = (
        GoodsPt.objects.select_related("grn__document", "grn__arrival")
        .filter(document_id=version.document_id, grn__isnull=False)
        .first()
    )
    return pt


def handoff_key(grn_document_id: uuid.UUID) -> uuid.UUID:
    """One handoff per goods receipt: a second report adds to it, never a second owner."""
    return uuid.uuid5(grn_document_id, EXTRA_REASON)


def correction_route(
    version: OfficialVersion,
    source: GoodsPt | None,
    *,
    can_read_grn: bool,
    can_correct: bool,
) -> dict[str, Any] | None:
    """Where extra pieces go back to, for the screen to guide the person there.

    The source documents are named, never changed. The goods receipt is named
    only to someone who may read it - a link is not a grant - and ``can_correct``
    says whether this person may start the correction themselves (raise the
    counter-GRN). Everyone else hands the pieces over (E251) and sees whether
    that handoff is still open.
    """
    if source is None or source.grn is None:
        return None
    grn = source.grn
    handoff = (
        GoodsException.objects.filter(
            tenant_id=version.tenant_id,
            kind=DISCREPANCY_KIND,
            source_event_key=handoff_key(grn.document_id),
        )
        .order_by("-opened_at")
        .first()
    )
    return {
        "grn_id": str(grn.document_id) if can_read_grn else None,
        "grn_number": grn.document.official_number if can_read_grn else None,
        "pt_id": str(version.document_id),
        "pt_number": version.document.official_number,
        "receipt_kind": source.receipt_kind,
        "can_correct": can_correct,
        "handoff": None
        if handoff is None
        else {
            "exception_id": str(handoff.pk),
            "state": handoff.state,
            "owner_role": handoff.owner_role,
            "opened_at": handoff.opened_at.isoformat(),
        },
    }


def report_extra(run: CommandRun, session_id: uuid.UUID, report: ExtraReport) -> AcceptanceSession:
    """E251: hand extra pieces over to the owner of the receipt's correction.

    Nothing is accepted, counted or posted, and the official PT is untouched:
    the report opens (or adds a note to) one owned ``acceptance_discrepancy``
    for the goods receipt, owned by the role the exception kind already names.
    The reporter gains no authority by it - the counter-GRN and the excess
    decision stay with the people who hold them.
    """
    session, guard, version, head = _lock_session(run, session_id, None, check_revision=False)
    del guard
    source = receipt_source(version)
    if source is None or source.grn is None or not _is_live(version, head):
        raise Refusal(
            "ACCEPTANCE_NOT_READY",
            "Extra goods are handed over against a live receipt PT and its goods receipt.",
        )
    if (
        report.line_id is not None
        and not OfficialLine.objects.filter(pk=report.line_id, version_id=version.pk).exists()
    ):
        raise Refusal("IDENTITY_UNRESOLVED", "That line is not on this PT version.")
    grn = source.grn
    subject = f"grn:{grn.document_id}"
    key = handoff_key(grn.document_id)
    # Two people reporting extra on one receipt at once must meet one handoff, not
    # race to open two: serialise on the handoff itself (append-only, so advisory).
    run.advisory_lock(LockRank.DOCUMENT, [f"extra-handoff:{grn.document_id}"])
    note = (
        f"{report.qty} extra piece(s) with tag {report.alias_value} found while accepting "
        f"PT {version.document.official_number or version.document_id}"
        + (f": {report.note}" if report.note else ".")
    )
    already_open = GoodsException.objects.filter(
        tenant_id=run.tenant_id, kind=DISCREPANCY_KIND, source_event_key=key, state="open"
    ).first()
    if already_open is not None:
        exception = add_exception_event(
            run, already_open.pk, event_kind="note", owner_human_id=None, note=note
        )
    else:
        exception = open_exception(
            run,
            kind=DISCREPANCY_KIND,
            site_id=session.site_id,
            subject_key=subject,
            reason_code=EXTRA_REASON,
            source_event_key=key,
            allowed_resolution_actions=[
                "inbound/grns/{id}/counter",
                "inbound/grns/{id}/dispositions",
            ],
            note=note,
            reopen=True,
        )
    run.audit_subject_key = subject
    run.audit_site_id = session.site_id
    run.audit_after = {
        "session_id": str(session.pk),
        "exception_id": str(exception.pk),
        "qty": report.qty,
        "alias_value": report.alias_value,
    }
    return session


# ---------------------------------------------------------------------------
# Scan resolution
# ---------------------------------------------------------------------------


@dataclass
class _Source:
    """The frozen official version a session accepts, with what its tags may carry."""

    version: OfficialVersion
    site_id: int
    lines: dict[str, OfficialLine]
    aliases: dict[str, set[str]]
    jobs: list[PrintJob]
    evidence_ids: set[str]

    @classmethod
    def load(cls, version: OfficialVersion, site_id: int, now: datetime) -> _Source:
        lines = {
            str(line.pk): line
            for line in OfficialLine.objects.filter(version_id=version.pk).order_by("line_no")
            if _acceptable_qty(line)
        }
        jobs = list(PrintJob.objects.filter(pt_version_id=version.pk, status__in=PRINTED))
        sources = (version.canonical_payload or {}).get("source_evidence_ids") or []
        return cls(
            version=version,
            site_id=site_id,
            lines=lines,
            aliases=_aliases(lines, jobs, site_id, now),
            jobs=jobs,
            evidence_ids={str(e) for e in sources},
        )


@dataclass(frozen=True)
class _Resolved:
    scan: Scan
    line: OfficialLine | None
    label_ref: str | None
    destination_id: uuid.UUID | None
    #: The scan's place in the request, so a refusal names the scan that caused it.
    index: int


def _fresh(session: AcceptanceSession, scans: list[Scan]) -> list[Scan]:
    """Scans not yet acknowledged in this session; a reused key must be the same scan."""
    prior = {
        o.scan_key: o
        for o in ScanObservation.objects.filter(
            acceptance_id=session.pk, scan_key__in=[s.key for s in scans]
        )
    }
    recorded: dict[uuid.UUID, list[AcceptanceEvent]] = defaultdict(list)
    for event in AcceptanceEvent.objects.filter(session_id=session.pk, scan_key__in=list(prior)):
        recorded[event.scan_key].append(event)
    fresh: list[Scan] = []
    for item in scans:
        earlier = prior.get(item.key)
        if earlier is None:
            fresh.append(item)
        elif not _same_scan(item, earlier, recorded.get(item.key, [])):
            raise Refusal(
                "ACCEPTANCE_INVALID",
                "A scan_key was already used for a different scan in this session.",
                status=422,
                issues=[issue("SCAN_KEY_REUSED", "Use a new scan_key", field=str(item.key))],
            )
    return fresh


def _same_scan(item: Scan, earlier: ScanObservation, events: list[AcceptanceEvent]) -> bool:
    """Does a replayed scan repeat the acknowledged one in every stored respect?

    The observation keeps what was seen (quantity, tag text, condition, location,
    SKU); the acceptance events keep the outcome, the official line, the ticket
    MRP and the label record. A wrong-goods observation has no events.
    """
    if (
        earlier.qty != item.qty
        or (earlier.alias_value or "") != item.alias_value
        or earlier.condition != item.condition
        or (item.location_id is not None and earlier.location_id != item.location_id)
    ):
        return False
    if not events:
        return item.outcome == "wrong" and earlier.sku_id == item.chosen_sku_id
    labels = {str(e.label_evidence_ref or "").split(":", 1)[-1] for e in events}
    return (
        {e.outcome for e in events} == {item.outcome}
        and (item.line_id is None or {str(e.official_line_id) for e in events} == {item.line_id})
        and (item.chosen_sku_id is None or earlier.sku_id == item.chosen_sku_id)
        and {_paise_or_none(e.observed_ticket_mrp) for e in events}
        == {_paise_or_none(item.observed_mrp)}
        and (item.label_evidence_id is None or labels == {str(item.label_evidence_id)})
    )


def _paise_or_none(value: Any) -> int | None:
    """An observed tag MRP: no MRP read stays unknown, never a zero MRP."""
    return None if value in (None, "") else int(value)


def _printed_lines(job: PrintJob) -> list[dict[str, Any]]:
    return [entry for entry in (job.lines or []) if isinstance(entry, dict)]


def _aliases(
    lines: dict[str, OfficialLine], jobs: list[PrintJob], site_id: int, now: datetime
) -> dict[str, set[str]]:
    """Per line: the frozen alias, aliases printed for it, and effective aliases of its SKU."""
    out: dict[str, set[str]] = {
        key: {str(line.payload.get("alias_as_used") or "")} - {""} for key, line in lines.items()
    }
    for job in jobs:
        for entry in _printed_lines(job):
            key = str(entry.get("official_line_id"))
            alias = entry.get("alias_as_used") or entry.get("alias_value")
            if key in out and alias:
                out[key].add(str(alias))
    by_sku: dict[str, list[str]] = defaultdict(list)
    for key, line in lines.items():
        if line.payload.get("sku_id"):
            by_sku[str(line.payload["sku_id"])].append(key)
    if by_sku:
        effective = (
            SkuAlias.objects.filter(
                sku_id__in=list(by_sku), governance_state="effective", effective_from__lte=now
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=now))
            .filter(Q(site__isnull=True) | Q(site_id=site_id))
            .values_list("sku_id", "value")
        )
        for sku_id, value in effective:
            for key in by_sku.get(str(sku_id), []):
                out[key].add(value)
    return out


def _frozen_mrp(line: OfficialLine) -> str:
    payload = line.payload
    calculated = payload.get("calculated") or {}
    supplied = payload.get("supplied") or {}
    # A found adjustment line freezes the MRP of its cost-evidence origin itself.
    return str(
        calculated.get("mrp_paise") or supplied.get("mrp_paise") or payload.get("mrp_paise") or ""
    )


def _acceptable_qty(line: OfficialLine) -> int:
    """How many pieces of a line acceptance puts away.

    A found adjustment line nobody valued stays in quarantine (P14's missing-
    evidence branch): it is not acceptance work, so it counts for nothing here.
    """
    if line.payload.get("value_basis") == "unvalued":
        return 0
    return int(line.payload.get("qty") or 0)


def _source_lot_ids(version: OfficialVersion, line_ids: Iterable[Any]) -> list[uuid.UUID]:
    """The custody lots a source version's lines put away: covered lots, or found lots."""
    wanted = list(line_ids)
    if version.document.purpose == FOUND_PURPOSE:
        return list(
            CustodyLot.objects.filter(adjustment_line_id__in=wanted).values_list("pk", flat=True)
        )
    return list(
        LiveCoverage.objects.filter(
            cover_event__pt_version_id=version.pk, cover_event__pt_line_id__in=wanted
        ).values_list("lot_id", flat=True)
    )


def _resolve_all(source: _Source, scans: list[Scan]) -> list[_Resolved]:
    """Identity (step 13), then every tag comparison (step 14), then locations (step 15)."""
    progress = {
        row["official_line_id"]: row["remaining_qty"] for row in line_progress(source.version)
    }
    lines: list[tuple[Scan, OfficialLine | None]] = []
    for index, item in enumerate(scans):
        if item.outcome == "wrong":
            if item.chosen_sku_id and not ProductSku.objects.filter(pk=item.chosen_sku_id).exists():
                raise Refusal("IDENTITY_UNRESOLVED", "The chosen SKU was not found.")
            lines.append((item, None))
            continue
        lines.append((item, _resolve_line(source, item, progress, index)))
    labels: dict[uuid.UUID, str | None] = {}
    mismatches: list[dict[str, Any]] = []
    for index, (item, line) in enumerate(lines):
        if line is None:
            continue
        problem, label_ref = _tag_problem(source, line, item)
        if problem:
            mismatches.append(
                issue(
                    "TAG_MISMATCH",
                    problem,
                    field=f"observations[{index}]",
                    line_key=str(line.stable_line_key),
                )
            )
        labels[item.key] = label_ref
    if mismatches:
        raise Refusal(
            "TAG_MISMATCH",
            "Some tags do not match the approved PT line.",
            status=422,
            issues=mismatches,
        )
    for index, (item, line) in enumerate(lines):
        if line is not None and OUTCOME_CONDITION[item.outcome] != item.condition:
            raise Refusal(
                "ACCEPTANCE_INVALID",
                f"A {item.outcome} scan records {OUTCOME_CONDITION[item.outcome]} condition.",
                status=422,
                issues=[issue("CONDITION", "condition", field=f"observations[{index}].condition")],
            )
    return [
        _Resolved(item, line, labels.get(item.key), _destination(source, item), index)
        for index, (item, line) in enumerate(lines)
    ]


def _resolve_line(
    source: _Source, item: Scan, remaining: dict[str, int], index: int
) -> OfficialLine:
    """The official line a scan is for (step 13).

    A tag the PT carries always resolves to its line, even when that line has
    nothing left: more pieces than the official line is a quantity question
    (step 15, ``EXCEEDS_REMAINING``), answered by the line itself, not an
    identity one. Only a tag no line of this PT carries is ``NOT_ON_PT`` (GSA-T07:
    both go back to the count, never to a new acceptance outcome).
    """
    if item.line_id:
        chosen = source.lines.get(item.line_id)
        if chosen is None:
            raise Refusal("IDENTITY_UNRESOLVED", "That line is not on this PT version.")
        return chosen
    known = [
        line
        for key, line in source.lines.items()
        if item.alias_value in source.aliases.get(key, set())
    ]
    if item.chosen_sku_id is not None:
        known = [
            line
            for line in known
            if str(line.payload.get("sku_id") or "") == str(item.chosen_sku_id)
        ]
    candidates = [line for line in known if remaining.get(str(line.pk), 0) > 0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates and len(known) == 1:
        # Its one line is already fully accepted: an over-scan. The line's own
        # quantity rule refuses it, by name, once tags are compared.
        return known[0]
    if not candidates and known:
        # Several exhausted lines carry the tag: which one is over-scanned is
        # the person's choice, the same as any other ambiguous tag.
        candidates = known
    if not candidates:
        raise Refusal(
            "IDENTITY_UNRESOLVED",
            f"No line of this PT carries tag {item.alias_value}.",
            issues=[
                issue(
                    "NOT_ON_PT",
                    "This tag is not on this PT. Extra goods go back to the goods receipt.",
                    field=f"observations[{index}]",
                )
            ],
        )
    raise Refusal(
        "IDENTITY_UNRESOLVED",
        "This tag matches more than one line. Choose the line.",
        issues=[
            issue("AMBIGUOUS", f"Line {line.line_no} matches.", line_key=str(line.stable_line_key))
            for line in candidates
        ],
    )


def _tag_problem(source: _Source, line: OfficialLine, item: Scan) -> tuple[str | None, str | None]:
    if item.alias_value not in source.aliases.get(str(line.pk), set()):
        return "The tag's barcode is not the one approved for this line.", None
    if item.observed_mrp != _frozen_mrp(line):
        return "The tag's MRP differs from the approved line.", None
    target = item.label_evidence_id or _derived_label(source, line)
    label_ref = _label_ref(source, line, target)
    if label_ref is None:
        return (
            "The label evidence is not a print job for this line, the PT's source file "
            "or the typed PT's official record.",
            None,
        )
    return None, label_ref


def _derived_label(source: _Source, line: OfficialLine) -> uuid.UUID | None:
    """The label record this line's tags must have come from, chosen by the server.

    In order of how the tag was actually made: a print job that printed this exact
    line at its frozen MRP; then the PT's own frozen source file when it has exactly
    one; then, for a typed PT with no source file, the official version itself. More
    than one source file is genuinely ambiguous evidence, so nothing is derived and
    the scan is refused rather than attributed to a guess.
    """
    for job in source.jobs:
        printed = [e for e in _printed_lines(job) if str(e.get("official_line_id")) == str(line.pk)]
        if printed and all(
            str(e.get("mrp_paise") or _frozen_mrp(line)) == _frozen_mrp(line) for e in printed
        ):
            return job.pk
    if len(source.evidence_ids) == 1:
        return uuid.UUID(next(iter(source.evidence_ids)))
    if not source.evidence_ids:
        return uuid.UUID(str(source.version.pk))
    return None


def _label_ref(source: _Source, line: OfficialLine, target: uuid.UUID | None) -> str | None:
    """The tag's own record: a print job for generated tags, PT source evidence for vendor tags."""
    if target is None:
        return None
    if str(target) in source.evidence_ids:
        return f"evidence:{target}"
    for job in source.jobs:
        if job.pk == target:
            printed = [
                e for e in _printed_lines(job) if str(e.get("official_line_id")) == str(line.pk)
            ]
            if printed and all(
                str(e.get("mrp_paise") or _frozen_mrp(line)) == _frozen_mrp(line) for e in printed
            ):
                return f"print_job:{target}"
            return None
    if target == source.version.pk and not source.evidence_ids:
        return f"pt_version:{target}"
    return None


def _destination(source: _Source, item: Scan) -> uuid.UUID | None:
    if item.outcome == "accepted_good":
        location = (
            Location.objects.filter(
                pk=item.location_id, site_id=source.site_id, retired_at__isnull=True
            ).first()
            if item.location_id
            else None
        )
        if location is None or location.system or location.kind not in PUTAWAY_KINDS:
            raise Refusal(
                "ACCEPTANCE_INVALID",
                "Accepted goods go onto a floor, backstore, bin, zone or fixture location "
                "at this site.",
                status=422,
            )
        return location.pk
    if item.outcome == "damaged":
        return uuid.UUID(str(engine.system_location(source.site_id, "quarantine").pk))
    if item.location_id is not None:
        if not Location.objects.filter(pk=item.location_id, site_id=source.site_id).exists():
            raise Refusal("ACCEPTANCE_INVALID", "That location is not at this site.", status=422)
        return item.location_id
    return None


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Slice:
    lot_id: uuid.UUID
    interval: ranges.Interval
    location_id: uuid.UUID


def _events(run: CommandRun | None, source: _Source) -> list[AcceptanceEvent]:
    """Acceptance evidence of the version, including rows this command has not yet sealed."""
    rows = list(AcceptanceEvent.objects.filter(official_line__version_id=source.version.pk))
    if run is not None:
        rows.extend(
            row
            for row in run.evidence.pending
            if isinstance(row, AcceptanceEvent) and str(row.official_line_id) in source.lines
        )
    return rows


def _portions(
    events: Iterable[AcceptanceEvent], line_id: uuid.UUID, outcomes: Iterable[str]
) -> dict[uuid.UUID, list[ranges.Interval]]:
    wanted = set(outcomes)
    out: dict[uuid.UUID, list[ranges.Interval]] = defaultdict(list)
    for event in events:
        if event.official_line_id == line_id and event.outcome in wanted:
            out[event.lot_id].append(bounds(event.portion))
    return out


def _open_slices(source: _Source, line: OfficialLine, *, condition: str = "good") -> list[_Slice]:
    """Covered portions of the line still physically here, in ``condition``, not yet accepted.

    ``condition="damaged"`` finds pieces this version covers that are already
    damaged - counted damaged at the GRN, or damaged under a reversed earlier
    version - so a damaged scan can acknowledge them without moving good stock.
    """
    covered: dict[uuid.UUID, list[ranges.Interval]] = defaultdict(list)
    if source.version.document.purpose == FOUND_PURPOSE:
        # A found lot is this line's own custody from its first piece to its last.
        for lot in CustodyLot.objects.filter(adjustment_line_id=line.pk):
            covered[lot.pk].append((0, int(lot.issued_qty)))
    for coverage in LiveCoverage.objects.filter(
        cover_event__pt_version_id=source.version.pk, cover_event__pt_line_id=line.pk
    ):
        covered[coverage.lot_id].append(bounds(coverage.portion))
    out: list[_Slice] = []
    for position in Position.objects.filter(
        lot_id__in=list(covered),
        boundary="physical",
        site_id=source.site_id,
        condition=condition,
        accepted_event__isnull=True,
    ):
        if position.location_id is None:
            continue
        for lower, upper in ranges.intersect([bounds(position.portion)], covered[position.lot_id]):
            out.append(_Slice(position.lot_id, (lower, upper), position.location_id))
    return sorted(out, key=lambda s: (str(s.lot_id), s.interval[0]))


def _cut(
    slices: list[_Slice], by_lot: dict[uuid.UUID, list[ranges.Interval]], *, inside: bool
) -> list[_Slice]:
    out: list[_Slice] = []
    for piece in slices:
        within, outside = ranges.split(piece.interval, by_lot.get(piece.lot_id, []))
        for lower, upper in within if inside else outside:
            out.append(replace(piece, interval=(lower, upper)))
    return out


def _take(pool: list[_Slice], qty: int, line: OfficialLine, verb: str, index: int) -> list[_Slice]:
    chosen: list[_Slice] = []
    remaining = qty
    for piece in pool:
        if remaining <= 0:
            break
        size = min(ranges.length(piece.interval), remaining)
        lower = piece.interval[0]
        chosen.append(replace(piece, interval=(lower, lower + size)))
        remaining -= size
    if remaining > 0:
        # GSA-T07: more than the official line is never accepted here and never
        # changes the PT. The issue names the scan, the line and how many pieces
        # of it are still open, so the screen can send the person back to the
        # count (counter-GRN) and the excess decision instead.
        still_open = qty - remaining
        raise Refusal(
            "ACCEPTANCE_INVALID",
            f"Only {still_open} piece(s) of line {line.line_no} are still to {verb}. "
            "Extra pieces are not accepted here: they go back to the goods receipt.",
            status=422,
            issues=[
                issue(
                    "EXCEEDS_REMAINING",
                    f"More than line {line.line_no} of this PT still has to {verb}.",
                    field=f"observations[{index}]",
                    line_key=str(line.stable_line_key),
                    quantity=still_open,
                )
            ],
        )
    return chosen


def _mover(
    outcome: str, location_id: uuid.UUID, event_id: uuid.UUID
) -> Callable[[engine.Address], engine.Address]:
    if outcome == "accepted_good":
        return lambda old: replace(old, location_id=location_id, accepted_event_id=event_id)
    return lambda old: replace(old, location_id=location_id, condition="damaged")


def _apply(run: CommandRun, session: AcceptanceSession, source: _Source, item: _Resolved) -> None:
    scan_ = item.scan
    subject_key = f"document:{source.version.document_id}"
    if item.line is None:
        _observe(run, session, scan_, sku_id=scan_.chosen_sku_id, location_id=item.destination_id)
        open_exception(
            run,
            kind=DISCREPANCY_KIND,
            site_id=session.site_id,
            subject_key=subject_key,
            reason_code="WRONG_GOODS",
            source_event_key=scan_.key,
        )
        return
    line = item.line
    slices = _open_slices(source, line)
    known = _events(run, source)
    checked = _portions(known, line.pk, ["checked_good"])
    if scan_.outcome == "checked_good":
        pool = _cut(slices, checked, inside=False)
        verb = "check"
    else:
        # Putaway takes the pieces already checked first, so a check then a putaway
        # describes the same goods.
        pool = [*_cut(slices, checked, inside=True), *_cut(slices, checked, inside=False)]
        verb = "accept"
    if scan_.outcome == "damaged":
        # Covered pieces already damaged, and not yet acknowledged under this version,
        # are taken before any good piece is moved to quarantine.
        acknowledged = _portions(known, line.pk, ["damaged"])
        already = _cut(_open_slices(source, line, condition="damaged"), acknowledged, inside=False)
        pool = [*already, *pool]
    chosen = _take(pool, scan_.qty, line, verb, item.index)
    sku_id = uuid.UUID(str(line.payload["sku_id"])) if line.payload.get("sku_id") else None
    plan = engine.Plan(
        "P09" if scan_.outcome != "damaged" else "P10",
        source.version.pk,
        engine.event_key("acceptance", session.pk, scan_.key),
    )
    events: list[AcceptanceEvent] = []
    for piece in chosen:
        destination = item.destination_id or piece.location_id
        event = AcceptanceEvent(
            session_id=session.pk,
            site_id=session.site_id,
            scan_key=scan_.key,
            official_line_id=line.pk,
            lot_id=piece.lot_id,
            portion=portion(*piece.interval),
            destination_location_id=destination,
            observed_alias=scan_.alias_value,
            observed_ticket_mrp=_paise_or_none(scan_.observed_mrp),
            label_evidence_ref=item.label_ref,
            tag_verdict="matched",
            outcome=scan_.outcome,
        )
        run.record(event, event_at=scan_.actual_at)
        events.append(event)
        if scan_.outcome == "checked_good":
            continue
        engine.change_address(
            run, plan, piece.lot_id, piece.interval, _mover(scan_.outcome, destination, event.pk)
        )
        if scan_.outcome == "damaged":
            _hold_damage(run, plan, session, source, piece, event.pk)
    location_id = item.destination_id or (chosen[0].location_id if chosen else None)
    _observe(run, session, scan_, sku_id=sku_id, location_id=location_id, description=line)
    batch_id = engine.post(run, source.version, plan)
    for event in events:
        event.journal_batch_id = batch_id
    if scan_.outcome == "damaged":
        open_exception(
            run,
            kind=DISCREPANCY_KIND,
            site_id=session.site_id,
            subject_key=subject_key,
            reason_code="DAMAGED_AT_ACCEPTANCE",
            source_event_key=scan_.key,
        )


def _hold_damage(
    run: CommandRun,
    plan: engine.Plan,
    session: AcceptanceSession,
    source: _Source,
    piece: _Slice,
    event_id: uuid.UUID,
) -> None:
    """P10: a damage hold on the damaged portion, never a second one where one is active."""
    active = [
        bounds(hold.portion)
        for hold in ActiveHold.objects.filter(lot_id=piece.lot_id, kind="damage")
    ]
    for index, interval in enumerate(ranges.subtract([piece.interval], active)):
        engine.place_hold(
            run,
            plan,
            lot_id=piece.lot_id,
            interval=interval,
            hold_key=uuid.uuid5(event_id, "damage" if index == 0 else f"damage:{interval[0]}"),
            kind="damage",
            site_id=session.site_id,
            source_version_id=source.version.pk,
        )


def _observe(
    run: CommandRun,
    session: AcceptanceSession,
    item: Scan,
    *,
    sku_id: uuid.UUID | None,
    location_id: uuid.UUID | None,
    description: OfficialLine | None = None,
) -> None:
    """The durable scan acknowledgement; its key makes a replay a no-op."""
    if description is None:
        text = f"Unexpected goods at acceptance: {item.alias_value}"
    else:
        text = str(description.payload.get("source_ref") or item.alias_value)
    run.record(
        ScanObservation(
            acceptance_id=session.pk,
            site_id=session.site_id,
            scan_key=item.key,
            sku_id=sku_id,
            description=text[:240],
            alias_value=item.alias_value,
            condition=item.condition,
            qty=item.qty,
            location_id=location_id,
        ),
        event_at=item.actual_at,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def line_progress(
    version: OfficialVersion, events: Iterable[AcceptanceEvent] | None = None
) -> list[dict[str, Any]]:
    """Per-line progress from acceptance evidence, never inferred from unfinished scans."""
    rows_events = (
        list(events)
        if events is not None
        else list(AcceptanceEvent.objects.filter(official_line__version_id=version.pk))
    )
    by_line: dict[str, list[AcceptanceEvent]] = defaultdict(list)
    for event in rows_events:
        by_line[str(event.official_line_id)].append(event)
    rows: list[dict[str, Any]] = []
    for line in OfficialLine.objects.filter(version_id=version.pk).order_by("line_no"):
        key = str(line.pk)
        line_events = by_line.get(key, [])
        accepted = sum(
            ranges.length(bounds(e.portion)) for e in line_events if e.outcome == "accepted_good"
        )
        damaged = sum(
            ranges.length(bounds(e.portion)) for e in line_events if e.outcome == "damaged"
        )
        checked = sum(
            ranges.total(intervals)
            for intervals in _portions(line_events, line.pk, OUTCOME_CONDITION).values()
        )
        expected = _acceptable_qty(line)
        remaining = max(expected - accepted - damaged, 0)
        reasons: set[str] = set()
        if remaining:
            reasons.add("NOT_ACCEPTED")
        if damaged:
            reasons.update({"CONDITION_NOT_GOOD", "HOLD_ACTIVE"})
        rows.append(
            {
                "official_line_id": key,
                "line_key": str(line.stable_line_key),
                "alias_as_used": line.payload.get("alias_as_used"),
                "mrp_paise": _frozen_mrp(line) or None,
                "expected_qty": expected,
                "checked_qty": min(checked, expected) if expected else checked,
                "accepted_qty": accepted,
                "damaged_qty": damaged,
                "remaining_qty": remaining,
                "eligibility_reasons": [r for r in engine.ELIGIBILITY_REASONS if r in reasons],
                "label_evidence_ids": sorted(
                    {
                        str(e.label_evidence_ref).split(":", 1)[1]
                        for e in line_events
                        if e.label_evidence_ref
                    }
                ),
            }
        )
    return rows


def acknowledged_scan_keys(session: AcceptanceSession) -> list[str]:
    return [
        str(key)
        for key in ScanObservation.objects.filter(acceptance_id=session.pk)
        .order_by("recorded_at", "scan_key")
        .values_list("scan_key", flat=True)
    ]


# ---------------------------------------------------------------------------
# E248: what is still waiting to be accepted
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingAcceptance:
    """One official version with acceptance work still open, and nothing else.

    GSA-T07: a person holding only ``stock.accept`` must be able to find their own
    work without ``pt.view`` or ``exception.view``. So this carries the official
    reference needed to open or resume E139 and nothing more - no rows, no costs,
    no exception detail, no money.
    """

    official_version_id: uuid.UUID
    pt_id: uuid.UUID
    pt_number: str | None
    site_id: int
    remaining_qty: int
    updated_at: datetime


def pending_acceptance(tenant_id: uuid.UUID, site_ids: set[int] | None) -> list[PendingAcceptance]:
    """Every live receipt/opening version at those sites with remaining quantity.

    ``site_ids`` of ``None`` means "no site prefilter"; the caller still checks each
    row's own site and brand grant. Read in bulk - one query for the candidate
    heads, one for their lines, one for their acceptance evidence - because a
    per-version ``line_progress`` call would cost a query per PT on every load.
    """
    heads = DocumentHead.objects.select_related("document", "live_version").filter(
        tenant_id=tenant_id,
        state=DocumentHead.State.OFFICIAL,
        live_version__isnull=False,
        document__purpose__in=(
            DocumentIdentity.Purpose.RECEIPT,
            DocumentIdentity.Purpose.OPENING,
            FOUND_PURPOSE,
        ),
    )
    if site_ids is not None:
        if not site_ids:
            return []
        heads = heads.filter(document__site_id__in=sorted(site_ids))
    # ``live_version__isnull=False`` filters the rows but not the declared type.
    candidates = [(head, head.live_version) for head in heads if head.live_version is not None]
    if not candidates:
        return []
    version_ids = [version.pk for _head, version in candidates]
    expected: dict[uuid.UUID, int] = defaultdict(int)
    line_version: dict[uuid.UUID, uuid.UUID] = {}
    for version_id, line_id, payload in OfficialLine.objects.filter(
        version_id__in=version_ids
    ).values_list("version_id", "pk", "payload"):
        payload = payload or {}
        if payload.get("value_basis") != "unvalued":
            expected[version_id] += int(payload.get("qty") or 0)
        line_version[line_id] = version_id
    settled: dict[uuid.UUID, int] = defaultdict(int)
    for line_id, stored, outcome in AcceptanceEvent.objects.filter(
        official_line_id__in=list(line_version)
    ).values_list("official_line_id", "portion", "outcome"):
        if outcome in (AcceptanceEvent.Outcome.ACCEPTED_GOOD, AcceptanceEvent.Outcome.DAMAGED):
            settled[line_version[line_id]] += ranges.length(bounds(stored))
    # Oldest actionable work first (design E248) means oldest *by when the work
    # appeared* - when the version became official - not by when its head was last
    # touched. The document id breaks a timestamp tie so a cursor stays stable
    # across two reads of the same page.
    candidates.sort(key=lambda pair: (pair[1].recorded_at, str(pair[0].document_id)))
    return [
        PendingAcceptance(
            official_version_id=version.pk,
            pt_id=head.document_id,
            pt_number=head.document.official_number,
            site_id=head.document.held_site_id,
            remaining_qty=remaining,
            updated_at=head.updated_at,
        )
        for head, version in candidates
        if (remaining := max(expected[version.pk] - settled[version.pk], 0)) > 0
    ]

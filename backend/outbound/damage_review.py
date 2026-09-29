"""Damage reports and the independent review of one (OPS-05; goods tickets 12A, 12B, 05C).

Reporting damage takes effect at once: ``mark_damaged`` quarantines the pieces
and records the condition, a GRN quarantines and holds what was counted damaged
the moment it is issued, and damage found on a GRN's goods before their PT
(``inbound.goods_services.report_receipt_damage``) does the same. Availability
drops in that transaction, before anybody reviews anything.

What this module adds is the review. Every report opens ``pending``, and one
decision by a **different** authorised person closes it:

* **Confirm** writes down who decided, when and why. It posts no second stock
  movement, because the pieces were already quarantined when they were reported
  (damaged-goods-quarantine PRD §4.5).
* **Reject** says the report was a mistake. The original report is kept exactly
  as it was written; a linked release movement gives each portion back the
  address it had before it was reported and lifts only this report's own damage
  hold (PRD §5, ticket 12B). Any other hold over the same pieces stays in force,
  and goods it still covers stay in quarantine.

What a rejection leaves standing (ticket 12B): another hold keeps the pieces
in quarantine; a transfer reservation keeps them reserved; pieces never accepted
go back unaccepted, and a transfer-arrival report's shipment is opened for
acceptance again. A rejection is refused when the report's hold is no longer
over every piece (stale) or when the pieces have left the site's quarantine
since (moved) - where a rejection would put moved pieces back is not settled
and waits for Anand's ruling. The decision names no quantity.

A receiving report (ticket 05C) is decided the same way. Rejecting one lifts
only its own receipt damage hold and gives the pieces back to the site's
receiving location as good goods - where they would have stood had nobody
called them damaged (overall PRD §15.2.1 rule 3, CH-2026-09-24-01). The GRN's
count is not touched: pieces the count itself recorded as damaged still need a
counter-GRN before an ordinary PT may cover them. A report whose pieces were
since valued as damaged (``value_damage``) cannot be rejected.

A report can also be **closed** without a review (goods ticket 15B, Anand's
decision 3): once every piece it covers has gone back to the vendor on an RTV
pickup, the pickup closes it with a "Returned to vendor" note. Closing names
no reviewer and posts nothing; the pickup already ended the pieces' holds.
While any of its pieces is still here the report stays pending, and can be
confirmed as ever (rejecting it is refused: some pieces have moved).

Three more rules from goods ticket 12A:

* **Confirmation is only that decision.** It lifts no other hold, cancels no
  reservation, grants no acceptance and establishes no value; ``value_damage``
  is its own, separately approved route (goods PRD §14.9.2).
* **A count freeze does not stop damage reporting** (goods PRD §14.10
  GSA-R02): reporting goes ahead during a count. Deciding a report - confirm
  or reject - still waits for the count; nothing settles otherwise.
* **Reserved pieces.** Damage reported on reserved pieces leaves the
  reservation standing and opens owned work for its transfer (design E209 step
  13); dispatching the held pieces is refused until the hold is gone. A
  rejection gives the pieces back to that same reservation and closes the work.

Lock order, as everywhere else: SITE (the site guard) → DOCUMENT (the report row
and the release's head) → LOT → SERIES.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from django.utils import timezone

from alerts.goods_services import resolve_exceptions
from core.commands import CommandRun, LockRank
from core.goods_documents import append_revision, new_document, officialise, record_event
from core.goods_fields import bounds, portion
from core.kernel_models import DocumentIdentity
from core.numbering import allocate
from core.refusals import Refusal
from outbound.goods_models import DamageReport, GoodsMovement
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import ActiveHold

#: Deciding a damage report needs the same authority a release decision needs,
#: and sits under the same two-person floor: ``movement.approve`` is one of
#: ``accounts.actions.DISTINCT_IDENTITY_ACTIONS``.
REVIEW_ACTION = "movement.approve"
#: The two decisions. Anything else is a malformed request, not a refusal about stock.
DECISIONS = ("confirm", "reject")
#: The document a rejection posts and the posting it makes - the same pair a
#: release has always used, because a rejection *is* a release of that one hold.
RELEASE_DOC_TYPE = "REL"
RELEASE_POSTING = "P12"
#: The purpose written on that document, so a reader can tell it from a release
#: somebody drafted and sent for approval: this one carries the reviewer's own
#: governed decision as its authority and is official the moment it is written.
RELEASE_PURPOSE = "damage_rejected"

#: One frozen line of a report, as ``lines`` stores it.
ReportLine = dict[str, Any]


def _refuse(message: str) -> Refusal:
    return Refusal("MOVEMENT_INVALID", message, status=422)


# ---------------------------------------------------------------------------
# Opening a report
# ---------------------------------------------------------------------------


def snapshot_movement_lines(
    resolved: Sequence[tuple[Any, Sequence[Any], dict[str, Any]]],
) -> list[ReportLine]:
    """What the report has to remember to be able to give the goods back.

    Per line: the frozen movement line as it was written, plus - per portion -
    the address the pieces stood at *before* the hold moved them to quarantine.
    The condition matters as much as the location, because rejecting a report
    takes the damage condition off again and "what was it before" cannot be
    guessed after the fact.
    """
    return [
        {
            "line_key": body["line_key"],
            "lot_id": body["lot_id"],
            "sku_id": body["sku_id"],
            "origin_id": body["origin_id"],
            "qty": line.qty,
            "source_location_id": body["source_location_id"],
            "hold_keys": list(body["hold_keys"]),
            "portions": [
                {
                    "lot_id": str(piece.lot_id),
                    "lower": piece.interval[0],
                    "upper": piece.interval[1],
                    "condition": piece.address.condition,
                }
                for piece in slices
            ],
        }
        for line, slices, body in resolved
    ]


def open_for_movement(
    run: CommandRun,
    *,
    document: DocumentIdentity,
    site_id: int,
    reason_code: str,
    evidence_ids: Sequence[uuid.UUID],
    resolved: Sequence[tuple[Any, Sequence[Any], dict[str, Any]]],
) -> DamageReport:
    """One pending report per mark-damaged command, in that command's transaction."""
    assert run.principal.human_id is not None
    lines = snapshot_movement_lines(resolved)
    return DamageReport.objects.create(
        tenant_id=run.tenant_id,
        site_id=site_id,
        movement=document,
        reporter_id=run.principal.human_id,
        reported_at=run.now,
        quantity=sum(int(line["qty"]) for line in lines),
        lines=lines,
        reason_code=reason_code[:60],
        evidence_id=evidence_ids[0] if evidence_ids else None,
    )


def open_for_receiving(
    run: CommandRun,
    *,
    disposition: Any,
    site_id: int,
    reason_code: str,
    reporter_id: uuid.UUID,
    lines: Sequence[ReportLine],
    evidence_id: uuid.UUID | None = None,
) -> DamageReport:
    """The same pending report for damage found while receiving (ticket 05C).

    It hangs off the receipt's own damage-hold record: counting a piece damaged
    and issuing the GRN, or finding damage on a GRN's goods before their PT, is
    what quarantined it. ``lines`` say where a rejection gives each piece back -
    the site's receiving location, as good goods.
    """
    return DamageReport.objects.create(
        tenant_id=run.tenant_id,
        site_id=site_id,
        disposition=disposition,
        reporter_id=reporter_id,
        reported_at=run.now,
        quantity=sum(int(line["qty"]) for line in lines),
        lines=list(lines),
        reason_code=reason_code[:60],
        evidence_id=evidence_id,
    )


def open_for_dispatch(
    run: CommandRun,
    *,
    dispatch: Any,
    site_id: int,
    reason_code: str,
    lines: Sequence[ReportLine],
) -> DamageReport:
    """The same pending report for damage found counting an arriving shipment (OPS-06).

    Damage at arrival is damage: it quarantines the pieces at once and waits for
    a different authorised person, exactly as damage reported on the stock screen
    does. What differs is only where the rejection puts them back - the
    destination's receiving location, which is where those pieces would have been
    standing had nobody called them damaged.
    """
    assert run.principal.human_id is not None
    return DamageReport.objects.create(
        tenant_id=run.tenant_id,
        site_id=site_id,
        dispatch=dispatch,
        reporter_id=run.principal.human_id,
        reported_at=run.now,
        quantity=sum(int(line["qty"]) for line in lines),
        lines=list(lines),
        reason_code=reason_code[:60],
    )


def open_for_rtv_return(
    run: CommandRun,
    *,
    document: DocumentIdentity,
    site_id: int,
    reason_code: str,
    lines: Sequence[ReportLine],
) -> DamageReport:
    """The same pending report for damage found on RTV goods that came back (goods ticket 15F).

    A shipment to the vendor that failed and came back is received at the
    source like any goods: pieces found damaged are quarantined at once and
    wait for a different authorised person. The report hangs off the RTV
    itself; ``lines`` say where a rejection gives the pieces back - the
    source's receiving location, as good goods still to be put away.
    """
    assert run.principal.human_id is not None
    return DamageReport.objects.create(
        tenant_id=run.tenant_id,
        site_id=site_id,
        movement=document,
        reporter_id=run.principal.human_id,
        reported_at=run.now,
        quantity=sum(int(line["qty"]) for line in lines),
        lines=list(lines),
        reason_code=reason_code[:60],
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def visible_reports(
    tenant_id: uuid.UUID,
    site_ids: Iterable[int] | None,
    *,
    site_id: int | None = None,
    state: str | None = None,
) -> list[DamageReport]:
    """Every report the caller may read, newest first, inside their own sites."""
    # ``source_of`` reads each report's shipment and its transfer (ticket 13C).
    queryset = DamageReport.objects.select_related("dispatch__transfer").filter(tenant_id=tenant_id)
    if site_ids is not None:
        queryset = queryset.filter(site_id__in=sorted(site_ids))
    if site_id is not None:
        queryset = queryset.filter(site_id=site_id)
    if state:
        queryset = queryset.filter(state=state)
    return list(queryset.order_by("-reported_at", "-id"))


def report_dto(report: DamageReport, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    """One report in the words the screens use. No cost, no value, no valuation."""
    return {
        "id": str(report.pk),
        "record_contract": "goods-v1",
        "site_id": str(report.site_id),
        "source": source_of(report),
        "movement_id": str(report.movement_id) if report.movement_id else None,
        "disposition_id": str(report.disposition_id) if report.disposition_id else None,
        "dispatch_id": str(report.dispatch_id) if report.dispatch_id else None,
        "reported_by": {"id": str(report.reporter_id), "name": names.get(report.reporter_id, "")},
        "reported_at": report.reported_at.isoformat(),
        "quantity": report.quantity,
        "reason_code": report.reason_code,
        "evidence_id": str(report.evidence_id) if report.evidence_id else None,
        "state": report.state,
        "reviewed_by": (
            {"id": str(report.reviewer_id), "name": names.get(report.reviewer_id, "")}
            if report.reviewer_id
            else None
        ),
        "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None,
        "review_reason": report.review_reason,
        "release_movement_id": (
            str(report.release_movement_id) if report.release_movement_id else None
        ),
        "lines": report.lines,
        # A receiving report whose pieces were since valued as damaged cannot be
        # rejected (ticket 05C). The reader is told, rather than finding out by
        # pressing a button that refuses.
        "can_reject": _rejectable(report),
    }


def source_of(report: DamageReport) -> str:
    if report.disposition_id:
        return "receiving"
    if report.dispatch is not None:
        # Damage found as a failed delivery came back is raised at the source
        # (goods ticket 13C); damage found counting an arrival, at the destination.
        if report.site_id == report.dispatch.transfer.source_site_id:
            return "transfer_return"
        return "transfer_arrival"
    return "movement"


def reporter_names(reports: Sequence[DamageReport]) -> dict[uuid.UUID, str]:
    from accounts.goods_models import HumanIdentity

    wanted = {report.reporter_id for report in reports}
    wanted |= {report.reviewer_id for report in reports if report.reviewer_id}
    return dict(
        HumanIdentity.objects.filter(pk__in=sorted(wanted, key=str)).values_list(
            "pk", "display_name"
        )
    )


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def decide(run: CommandRun, report_id: uuid.UUID, *, decision: str, reason: str) -> DamageReport:
    """Confirm or reject one pending report, as a different person from the reporter."""
    from outbound.goods_movements import require_movement_site

    if decision not in DECISIONS:
        raise Refusal("INVALID_REQUEST", "decision is confirm or reject.", status=400)
    # The site guard first, because SITE is the rank above DOCUMENT and the
    # report's own row is locked under DOCUMENT. Which site it is comes from an
    # unlocked read; the locked row below is re-read before anything is decided.
    known = DamageReport.objects.filter(pk=report_id).first()
    if known is None:
        raise Refusal("NOT_FOUND", "That damage report was not found.")
    # GSA-R02 exempts damage *reporting* from a count freeze, and nothing more.
    # Whether a decision may be taken during a count is not settled, so both
    # confirming and rejecting still wait for the count, as they always have.
    require_movement_site(run, known.site_id)
    report = _locked(run, report_id)
    _refuse_second_decision(report)
    _refuse_self_review(run, report)
    arrival = None
    if decision == "reject":
        _refuse_unrejectable(report)
        # A transfer-arrival report's shipment may have to be opened for
        # acceptance again (ticket 12B). Its row is locked now, at the same rank
        # as the report and before any lot, so the lock order holds. Within
        # DOCUMENT the order is report, then dispatch; nothing locks a
        # dispatch and then a damage report, and nothing may start to.
        arrival = _locked_arrival(run, report)
    report.reviewer_id = run.principal.human_id
    report.reviewed_at = timezone.now()
    report.review_reason = reason
    fields = ["state", "reviewer", "reviewed_at", "review_reason"]
    if decision == "confirm":
        # Damage PRD §4.5: the quantity was already quarantined when it was
        # reported, so confirming it makes no second stock movement. It is also
        # nothing more than that decision (goods PRD §14.9.2): every other hold,
        # every reservation and every acceptance requirement stays exactly as it
        # was, and no value is established - `value_damage` is its own, separately
        # approved route and is neither required nor implied here.
        report.state = DamageReport.State.CONFIRMED
    else:
        report.state = DamageReport.State.REJECTED
        report.release_movement = _post_release(run, report, reason)
        fields.append("release_movement")
    report.save(update_fields=fields)
    if report.state == DamageReport.State.REJECTED and report.disposition_id is not None:
        # The receipt's own record of what is still undecided reads this
        # rejection: pieces the count called damaged are a discrepancy again.
        from inbound.goods_services import damage_report_rejected

        damage_report_rejected(run, report)
    if arrival is not None:
        # Pieces found damaged at a transfer's arrival go back to the
        # destination's receiving location unaccepted; acceptance is still the
        # only way from there to the shelf, so the shipment takes it again.
        from outbound.transfers import damage_report_rejected as arrival_rejected

        arrival_rejected(run, report, arrival)
    run.audit_subject_key = f"damage_report:{report.pk}"
    run.audit_site_id = report.site_id
    run.audit_after = {
        "damage_report_id": str(report.pk),
        "state": report.state,
        "release_movement_id": (
            str(report.release_movement_id) if report.release_movement_id else None
        ),
    }
    return report


def _locked(run: CommandRun, report_id: uuid.UUID) -> DamageReport:
    """The report row itself, locked before anything is read off it.

    Rank DOCUMENT: a damage report is the review side of a document, and the
    release a rejection posts locks nothing below that rank before it.
    """
    rows: list[DamageReport] = run.lock(
        LockRank.DOCUMENT, DamageReport.objects.filter(pk=report_id)
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That damage report was not found.")
    return rows[0]


def _locked_arrival(run: CommandRun, report: DamageReport) -> Any:
    """The shipment a transfer-arrival report came from, locked; None for any other report."""
    if report.dispatch_id is None:
        return None
    from outbound.goods_models import TransferDispatch

    rows: list[TransferDispatch] = run.lock(
        LockRank.DOCUMENT, TransferDispatch.objects.filter(pk=report.dispatch_id)
    )
    return rows[0] if rows else None


def _refuse_second_decision(report: DamageReport) -> None:
    if report.state != DamageReport.State.PENDING:
        raise Refusal(
            "STATE_CONFLICT",
            f"This damage report was already {report.state}. It is decided once.",
        )


def _refuse_self_review(run: CommandRun, report: DamageReport) -> None:
    """Damage PRD §4.3: the reporter cannot confirm or reject their own report."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A damage report is decided by a named person.")
    if str(run.principal.human_id) == str(report.reporter_id):
        raise Refusal(
            "SELF_APPROVAL",
            "Whoever reported this damage cannot also decide it. "
            "A different authorised person confirms or rejects it.",
        )


def _refuse_unrejectable(report: DamageReport) -> None:
    if not all(line.get("hold_keys") for line in report.lines):
        raise _refuse(
            "This report was recorded without the hold it placed, so a rejection has "
            "nothing it could lift. Confirm it, or correct the goods through their own route."
        )
    if _valued_as_damaged(report):
        # Pending Anand's ruling (ticket 12B): what becomes of the damaged memo
        # value if the damage under it was a mistake is not settled anywhere.
        raise _refuse(
            "These pieces were valued as damaged after this report, so the report can no "
            "longer be rejected. The damage stays in quarantine."
        )


def _rejectable(report: DamageReport) -> bool:
    """Whether a rejection could go ahead, as far as a read can tell.

    A receiving report written before ticket 05C froze no hold key and no place
    to give the pieces back to, so there is nothing a rejection could lift. A
    report whose pieces were since valued as damaged, whose hold is gone, or
    whose pieces have left the site's quarantine cannot be rejected either
    (ticket 12B). Only a pending report is asked the last two: a decided one
    cannot be rejected anyway, and the answer would cost a read per report.
    """
    if not all(line.get("hold_keys") for line in report.lines):
        return False
    if _valued_as_damaged(report):
        return False
    if report.state != DamageReport.State.PENDING:
        return True
    return _standing_problem(report) is None


def _valued_as_damaged(report: DamageReport) -> bool:
    """Whether a ``value_damage`` decision now covers any piece of a receiving report.

    Only a receipt's own damage hold can be valued, so only a receiving report
    can be. Its pieces carry a damaged memo value from then on; lifting the
    damage under them would leave that value standing on goods called good.
    """
    if report.disposition_id is None:
        return False
    from inbound.goods_models import Disposition

    wanted = {
        (str(piece["lot_id"]), int(piece["lower"]), int(piece["upper"]))
        for line in report.lines
        for piece in line["portions"]
    }
    lots = sorted({uuid.UUID(lot) for lot, _lower, _upper in wanted}, key=str)
    for lot_id, stored in Disposition.objects.filter(
        kind=Disposition.Kind.VALUE_DAMAGE, lot_id__in=lots, portion__isnull=False
    ).values_list("lot_id", "portion"):
        lower, upper = bounds(stored)
        if any(lot == str(lot_id) and lower < high and low < upper for lot, low, high in wanted):
            return True
    return False


# ---------------------------------------------------------------------------
# The linked release a rejection posts (ticket 12B)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Piece:
    """One frozen portion, where it goes back to, and in what condition."""

    lot_id: uuid.UUID
    interval: ranges.Interval
    destination_id: uuid.UUID
    condition: str


def _plan_line(
    line: ReportLine, site_id: int, quarantine_id: uuid.UUID
) -> tuple[list[uuid.UUID], list[_Piece]]:
    """This line's hold keys and the exact reversal each of its portions gets."""
    keys = [uuid.UUID(str(k)) for k in line["hold_keys"]]
    back_to = line.get("source_location_id")
    pieces: list[_Piece] = []
    for piece in line["portions"]:
        lot_id = uuid.UUID(str(piece["lot_id"]))
        interval: ranges.Interval = (int(piece["lower"]), int(piece["upper"]))
        problem = _portion_problem(lot_id, interval, keys, site_id, quarantine_id)
        if problem is not None:
            raise _refuse(STANDING_PROBLEMS[problem])
        # Ticket 12B: a rejection corrects *this report's* damage and nothing
        # else. While another hold still covers the pieces the damage condition
        # comes off but the goods stay in quarantine - moving held goods into an
        # ordinary location is exactly what a release approval exists to stop.
        blocked = (
            ActiveHold.objects.filter(lot_id=lot_id, portion__overlap=portion(*interval))
            .exclude(hold_key__in=keys)
            .exists()
        )
        destination = quarantine_id if (blocked or not back_to) else uuid.UUID(str(back_to))
        pieces.append(_Piece(lot_id, interval, destination, str(piece.get("condition") or "good")))
    return keys, pieces


#: Why a rejection cannot go ahead over goods that no longer stand as the report
#: left them (ticket 12B), in the words the refusal uses.
STANDING_PROBLEMS = {
    # Stale: the report's own hold is no longer over every piece.
    "stale": (
        "These goods are no longer held as this report left them. "
        "Reload the report and look at where they are now."
    ),
    # Moved: the pieces have left this site's quarantine since they were
    # reported - moved, sent or taken away. Where a rejection would put them back
    # is not settled, so it waits for Anand's ruling; confirming is still open.
    "moved": (
        "Some of these pieces have moved out of this site's quarantine since they were "
        "reported, so this report cannot be rejected for now. It can still be confirmed."
    ),
}


def _portion_problem(
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    keys: Sequence[uuid.UUID],
    site_id: int,
    quarantine_id: uuid.UUID,
) -> str | None:
    """``stale``, ``moved`` or None: does this portion still stand as the report left it?

    Still under this report's hold keys, and still physically in the report's
    site's quarantine - the only place a report ever put it.
    """
    active = ranges.normalise(
        [
            bounds(hold.portion)
            for hold in ActiveHold.objects.filter(lot_id=lot_id, hold_key__in=list(keys))
        ]
    )
    if not ranges.contains(active, [interval]):
        return "stale"
    standing = ranges.normalise(
        [
            bounds(position.portion)
            for position in engine.positions_of(lot_id, interval)
            if position.boundary == "physical"
            and position.site_id == site_id
            and position.location_id == quarantine_id
        ]
    )
    if not ranges.contains(standing, [interval]):
        return "moved"
    return None


def _standing_problem(report: DamageReport) -> str | None:
    """The first portion of a report that no longer stands as it left it, if any."""
    from outbound.goods_movements import quarantine_of

    quarantine_id = quarantine_of(report.site_id).pk
    for line in report.lines:
        keys = [uuid.UUID(str(k)) for k in line["hold_keys"]]
        for piece in line["portions"]:
            interval: ranges.Interval = (int(piece["lower"]), int(piece["upper"]))
            problem = _portion_problem(
                uuid.UUID(str(piece["lot_id"])), interval, keys, report.site_id, quarantine_id
            )
            if problem is not None:
                return problem
    return None


def _release_line_body(line: ReportLine, keys: Sequence[uuid.UUID], pieces: list[_Piece]) -> Any:
    first = pieces[0]
    return {
        "line_key": str(line["line_key"]),
        "lot_id": line["lot_id"],
        "qty": int(line["qty"]),
        "sku_id": line["sku_id"],
        "origin_id": line["origin_id"],
        # What the pieces go back to being. A rejection takes the damage
        # condition off; it invents nothing else about them.
        "condition": first.condition,
        "source_location_id": str(_quarantine_source(pieces)),
        "destination_location_id": str(first.destination_id),
        "hold_keys": [str(k) for k in keys],
        "portions": [
            {"lot_id": str(p.lot_id), "lower": p.interval[0], "upper": p.interval[1]}
            for p in pieces
        ],
    }


def _quarantine_source(pieces: list[_Piece]) -> uuid.UUID:
    """Where the held goods are standing now, read from the ledger, never assumed."""
    positions = engine.positions_of(pieces[0].lot_id, pieces[0].interval)
    for position in positions:
        if position.location_id is not None:
            return uuid.UUID(str(position.location_id))
    return pieces[0].destination_id


def _post_release(run: CommandRun, report: DamageReport, reason: str) -> DocumentIdentity:
    """One official release, linked to the report, giving the frozen portions back."""
    from outbound.goods_movements import (
        HOLD_EXCEPTION,
        quarantine_of,
        reserved_versions_over,
        settle_reservation_blocks,
        site_of,
    )

    site = site_of(report.site_id)
    identity, head = new_document(
        run,
        kind=RELEASE_DOC_TYPE,
        purpose=RELEASE_PURPOSE,
        entity_id=site.gstin.legal_entity_id,
        site_id=report.site_id,
    )
    lines: list[ReportLine] = list(report.lines)
    engine.lock_lots(
        run,
        sorted({uuid.UUID(str(p["lot_id"])) for line in lines for p in line["portions"]}, key=str),
    )
    quarantine_id = quarantine_of(report.site_id).pk
    planned = [(line, *_plan_line(line, report.site_id, quarantine_id)) for line in lines]
    bodies = [
        (uuid.UUID(str(line["line_key"])), _release_line_body(line, keys, pieces))
        for line, keys, pieces in planned
    ]
    header: dict[str, Any] = {
        "kind": GoodsMovement.Kind.RELEASE.value,
        "site_id": str(report.site_id),
        "reason_code": reason[:60],
        "evidence_ids": [],
        "source_document_id": _source_document(report),
        "count_id": None,
    }
    append_revision(run, head, header=header, replace_lines=bodies)
    assert run.principal.human_id is not None
    version, _official = officialise(
        run,
        head,
        approved_by_id=run.principal.human_id,
        canonical_header=header,
        lines=bodies,
        authority=run.authority,
        number=allocate(run, identity.entity, RELEASE_DOC_TYPE),
    )
    plan = engine.Plan(RELEASE_POSTING, version.pk, engine.event_key(RELEASE_PURPOSE, report.pk))
    for _line, keys, pieces in planned:
        _give_back(run, plan, report.site_id, version.pk, keys, pieces)
    engine.post(run, version, plan)
    GoodsMovement.objects.create(
        tenant_id=run.tenant_id,
        document_id=identity.pk,
        kind=GoodsMovement.Kind.RELEASE,
        source_document_id=header["source_document_id"],
    )
    _close_hold_exceptions(run, HOLD_EXCEPTION, {k for _l, keys, _p in planned for k in keys})
    # A reservation over these pieces was never touched (design E209 step 13); the
    # goods go back to it. Its owner's work closes once no hold is left over it.
    settle_reservation_blocks(
        run,
        reserved_versions_over(
            (piece.lot_id, piece.interval) for _l, _k, pieces in planned for piece in pieces
        ),
    )
    record_event(run, identity.pk, "released", version_id=version.pk)
    return identity


def _source_document(report: DamageReport) -> str | None:
    """The document a rejection's release links back to: the movement, the GRN or the transfer."""
    if report.movement_id:
        return str(report.movement_id)
    if report.disposition is not None:
        return str(report.disposition.document_id)
    if report.dispatch is not None:
        return str(report.dispatch.transfer.document_id)
    return None


def _give_back(
    run: CommandRun,
    plan: engine.Plan,
    site_id: int,
    version_id: uuid.UUID,
    keys: Sequence[uuid.UUID],
    pieces: Sequence[_Piece],
) -> None:
    for piece in pieces:
        engine.change_address(
            run,
            plan,
            piece.lot_id,
            piece.interval,
            lambda old, to=piece.destination_id, cond=piece.condition: replace(
                old, location_id=to, condition=cond
            ),
        )
        for key in keys:
            engine.release_hold(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=piece.interval,
                hold_key=key,
                site_id=site_id,
                source_version_id=version_id,
            )


def _close_hold_exceptions(run: CommandRun, kind: str, keys: set[uuid.UUID]) -> None:
    """A hold's owned work is finished only once nothing stands under its key."""
    for key in sorted(keys, key=str):
        if ActiveHold.objects.filter(hold_key=key).exists():
            continue
        resolve_exceptions(
            run, kind=kind, subject_key=f"hold:{key}", reason_code="DAMAGE_REPORT_REJECTED"
        )


# ---------------------------------------------------------------------------
# Closing a report whose goods went back to the vendor (goods ticket 15B)
# ---------------------------------------------------------------------------


def lock_pending_over(run: CommandRun, lot_ids: Iterable[uuid.UUID]) -> list[DamageReport]:
    """Every pending report naming a portion of these lots, locked at rank DOCUMENT.

    A command that may close such a report takes this lock before any lot, so it
    waits behind a decision already under way instead of racing it - the same
    order ``decide`` keeps (report, then lots). The report's own site does not
    matter: pieces moved by a quarantine transfer carry their report with them.
    """
    from django.db.models import Q

    wanted = sorted({str(lot) for lot in lot_ids})
    if not wanted:
        return []
    match = Q()
    for lot_id in wanted:
        match |= Q(lines__contains=[{"portions": [{"lot_id": lot_id}]}])
    return list(
        run.lock(
            LockRank.DOCUMENT,
            DamageReport.objects.filter(
                tenant_id=run.tenant_id, state=DamageReport.State.PENDING
            ).filter(match),
        )
    )


def report_pieces(report: DamageReport) -> dict[uuid.UUID, list[ranges.Interval]]:
    """The exact portions a report covers, per lot."""
    out: dict[uuid.UUID, list[ranges.Interval]] = {}
    for line in report.lines:
        for piece in line["portions"]:
            out.setdefault(uuid.UUID(str(piece["lot_id"])), []).append(
                (int(piece["lower"]), int(piece["upper"]))
            )
    return {lot: ranges.normalise(spans) for lot, spans in out.items()}


def all_at_boundary(report: DamageReport, boundary: str) -> bool:
    """Whether every piece the report covers now stands at ``boundary`` - gone from custody."""
    for lot_id, spans in report_pieces(report).items():
        for interval in spans:
            there = [
                bounds(p.portion)
                for p in engine.positions_of(lot_id, interval)
                if p.boundary == boundary
            ]
            if ranges.total(ranges.intersect(there, [interval])) != ranges.length(interval):
                return False
    return True


def close_departed(run: CommandRun, report: DamageReport, *, note: str) -> None:
    """Close a pending report whose goods have left through their exit route.

    Anand's 15B decision 3: when the vendor collects the pieces a pending report
    covers, the pickup closes the report with a "returned to vendor" note. It
    is not a review: nobody confirmed or rejected the damage, so no reviewer is
    written and no two-person rule is claimed. It posts nothing - the pickup
    that carried the goods away already ended their holds and hold work. The
    caller holds the report's lock (``lock_pending_over``).
    """
    if report.state != DamageReport.State.PENDING:
        raise Refusal("STATE_CONFLICT", f"This damage report was already {report.state}.")
    report.state = DamageReport.State.CLOSED
    report.reviewed_at = run.now
    report.review_reason = note[:500]
    report.save(update_fields=["state", "reviewed_at", "review_reason"])

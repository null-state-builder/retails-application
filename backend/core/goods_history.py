"""Paged document history (design §5.8, GSA-T01).

A document's history is the append-only ``DocumentEvent`` stream: draft
corrections, submission and withdrawal, approval and refusal, official versions
and reissues, reversals and the postings they caused. It is read newest first,
50 events to a page, through a cursor of its own so that advancing the history
never disturbs the line pages beside it.

The cursor pins two things: the **watermark** — the recorded time of the newest
event on the first page — and the **last key** of the page just read. Pinning the
watermark is what keeps a long history readable while work continues: events
recorded after the reader started are simply not in the pinned window, so page
two holds what it would have held had the whole history been read at once,
instead of sliding a page's worth of older events out of reach.

Only registered event kinds appear. A writer that records a new kind registers it
here, so history is a named contract rather than whatever happened to be written.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from django.db.models import Q
from django.utils.dateparse import parse_datetime

from core.kernel_models import DocumentEvent
from core.refusals import Refusal

#: Every event kind a document history may show, with the source that records it.
#: Adding a ``record_event`` kind without adding it here hides it from history;
#: ``tests/test_goods_pt_history.py`` fails when the two drift apart.
HISTORY_KINDS: Final[frozenset[str]] = frozenset(
    {
        # Draft life (ptmapper, inbound)
        "draft_created",
        "draft_corrected",
        "repriced",
        "submitted",
        "withdrawn",
        "rejected",
        # Officialisation and the postings it causes
        "officialised",
        "posted",
        # Movement life (outbound). A release officialises on approval rather than
        # on submit, so "released" is the event that says the goods actually moved.
        "released",
        # An evidenced adjustment or shrinkage, approved and posted (15A).
        "adjusted",
        # A write-off approved: the loss recognised, nothing moved (15C).
        "written_off",
        # A disposal approved: the pieces left custody for the disposed boundary,
        # linked to any earlier write-off without recognising its loss again (15D).
        "disposed",
        # Return to vendor (15B): the Owner's approval reserved the goods; the RTV
        # closed once nothing was reserved to it any more (``goods_rtv._settle``
        # writes ``rtv_{state}``, which the scan in test_goods_pt_history cannot
        # read, so test_goods_rtv reads every kind back through this history).
        "rtv_approved",
        "rtv_completed",
        "rtv_closed_partially_returned",
        "rtv_cancelled",
        # A persistent vendor acknowledgement shortfall on an RTV shipment (15H,
        # GSA-R07): the site prepared its closure, and the Owner approved it (the
        # unacknowledged pieces recognised as a shortfall) or turned it down.
        "rtv_shortfall_proposed",
        "rtv_shortfall_closed",
        "rtv_shortfall_rejected",
        # An RTV pickup took back the last piece a pending damage report covered,
        # and closed that report as returned to vendor (Anand's 15B decision 3); a
        # disposal (15D) closes such a report the same way, as disposed of.
        "damage_report_closed",
        # A transfer's excess-observed GAP decision matched to the observed goods
        # by its corrective transfer's confirmation (16, design P17).
        "excess_matched",
        # A non-trading stock count (17): started under its freeze and snapshot,
        # each blind pass opened and submitted with its GSA-T17 affirmation, a
        # scoped recount asked for, a pass gone stale and resumed, and the two
        # endings that post nothing - verified zero variance and cancellation.
        "count_started",
        "count_pass_opened",
        "count_pass_submitted",
        "count_recount_requested",
        "count_pass_stale",
        "count_pass_resumed",
        "count_closed",
        "count_cancelled",
        # Correction routes
        "reversal_requested",
        "reversal_evidence",
        "reversal_rejected",
        "reversed",
        "reissue_started",
        "countered",
        # Booking life (vendors)
        "receipt_linked",
        "short_closed",
        "cancelled",
    }
)

#: Events per history page (design §5.8). Bounded, so a long history is reachable
#: page by page instead of arriving whole or not at all.
HISTORY_PAGE: Final[int] = 50

_CURSOR_MAX: Final[int] = 200


@dataclass(frozen=True)
class HistoryPage:
    """One page of history, newest first, and the cursor that reads the next one."""

    items: list[dict[str, Any]]
    next_cursor: str | None

    def as_dto(self) -> dict[str, Any]:
        return {"items": self.items, "next_history_cursor": self.next_cursor}


def encode_history_cursor(
    watermark: datetime, last_recorded_at: datetime, last_id: uuid.UUID
) -> str:
    raw = json.dumps(
        {"w": watermark.isoformat(), "r": last_recorded_at.isoformat(), "i": str(last_id)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_history_cursor(cursor: str | None) -> tuple[datetime, datetime, uuid.UUID] | None:
    """The pinned watermark and last key, or ``None`` for the first page."""
    if not cursor:
        return None
    if len(cursor) > _CURSOR_MAX:
        raise Refusal("INVALID_REQUEST", "history_cursor is not valid.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
        watermark = parse_datetime(str(value["w"]))
        recorded_at = parse_datetime(str(value["r"]))
        last_id = uuid.UUID(str(value["i"]))
    except (ValueError, KeyError, TypeError):
        raise Refusal("INVALID_REQUEST", "history_cursor is not valid.") from None
    if watermark is None or recorded_at is None:
        raise Refusal("INVALID_REQUEST", "history_cursor is not valid.")
    return watermark, recorded_at, last_id


def _event_dto(event: DocumentEvent) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    related = payload.get("related_document_id")
    return {
        "id": str(event.pk),
        "kind": event.event_kind,
        "actor_id": str(event.actor_id) if event.actor_id else None,
        "recorded_at": event.recorded_at.isoformat(),
        "revision": event.revision.revision if event.revision is not None else None,
        "outcome": payload.get("to_state") or None,
        "reason_code": event.reason_code,
        "evidence_ids": [str(event.evidence_id)] if event.evidence_id else [],
        "related_document_id": str(related) if related else None,
    }


def document_history(document_id: uuid.UUID, cursor: str | None = None) -> HistoryPage:
    """One page of ``document_id``'s registered history, newest first."""
    pinned = decode_history_cursor(cursor)
    events = DocumentEvent.objects.filter(
        document_id=document_id, event_kind__in=HISTORY_KINDS
    ).select_related("revision")
    if pinned is not None:
        watermark, last_recorded_at, last_id = pinned
        # Inside the pinned window, and strictly after the last key in descending
        # order: an older instant, or the same instant with a smaller id. Ties are
        # broken by id, never by luck.
        events = events.filter(recorded_at__lte=watermark).filter(
            Q(recorded_at__lt=last_recorded_at) | Q(recorded_at=last_recorded_at, id__lt=last_id)
        )
    window = list(events.order_by("-recorded_at", "-id")[: HISTORY_PAGE + 1])
    if not window:
        return HistoryPage(items=[], next_cursor=None)
    more = len(window) > HISTORY_PAGE
    page = window[:HISTORY_PAGE]
    watermark = pinned[0] if pinned is not None else page[0].recorded_at
    last = page[-1]
    return HistoryPage(
        items=[_event_dto(event) for event in page],
        next_cursor=(
            encode_history_cursor(watermark, last.recorded_at, uuid.UUID(str(last.pk)))
            if more
            else None
        ),
    )

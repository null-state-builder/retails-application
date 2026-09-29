"""Three-way match at receiving (store operations PRD §10 ST-REC-1, ticket 37).

Each received line puts three figures side by side: what the booking asked for,
what the vendor's invoice claims and what was counted, with the booked and
invoiced cost beside them. A line is a **match**, or it is **short**, **excess**
or its **cost differs** (it can be more than one), within the tolerance held as
settings - 0 pieces and Rs 1 a line (``KDPS_THREE_WAY_QTY_TOLERANCE``,
``KDPS_THREE_WAY_COST_TOLERANCE_PAISE``).

It is a reading of records that already exist, never a step of its own: the
count is never stopped or refused because of it. After a command changes one of
the three figures - a GRN issued, an invoice claim recorded, a counter-GRN
approved, a receipt linked or a booking corrected - :func:`refresh` compares
again, opens the buyer's owned ``three_way_mismatch`` exception when a line
disagrees and closes it when every line agrees, and leaves an audit record of
the state before and after.

The unit is the invoice line: the invoice is the document being matched. Its
counted figure is paired the one way the GRN already pairs it (``compare_claim``);
a counted line on no invoice line is its own row. The booked figure is the booking
line the counted pieces are linked to (the receipt links made at GRN issue or by
the buyer), or for an invoice line nothing was counted against, the one booking
line its item is exactly. Booking fulfilment stays separate (overall PRD
R-REC-003): the booked figure is what the line still had open before this
delivery, and a delivery smaller than the booking is not short against it.

All of it is switched per store (``three-way-match``, ticket 01). Off, nothing
here runs and the read is refused. Cost is shown only to a login holding the
``cost`` field at the site; everyone else sees quantities.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import models

from accounts.principal import AccessContext
from alerts.goods_models import GoodsException
from alerts.goods_services import open_exception, resolve_exceptions
from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.kernel_models import AuditEvent, DocumentHead
from inbound.goods_models import GoodsGrn
from masters.store_feature_registry import THREE_WAY_MATCH
from masters.store_features import is_feature_on
from stockledger.goods_engine import event_key
from vendors.goods_models import BookingReceiptLink, GoodsBooking

FEATURE_KEY = THREE_WAY_MATCH
EXCEPTION_KIND = "three_way_mismatch"
AUDIT_ACTION = "inbound.three_way_match"
#: Named in an arrival's ``allowed_actions`` where the switch is on at its site.
ALLOWED_ACTION = "three_way_match"

SHORT = "short"
EXCESS = "excess"
COST_DIFFERS = "cost_differs"

#: Where a row's booked figure came from.
LINKED = "linked"  # one booking line
NOT_BOOKED = "not_booked"  # the delivery has a booking, but no line of it is this item
SEVERAL = "several"  # more than one booking line could be it; the buyer links it
NO_BOOKING = "no_booking"  # the delivery was received without a booking

#: Every key that says anything about cost. A viewer without the ``cost`` field
#: gets rows without them - absent, not blanked - and no ``cost_differs`` result.
COST_KEYS = frozenset(
    {"booked_cost_paise", "invoiced_cost_paise", "cost_difference_paise", "cost_tolerance_paise"}
)

_UNSET: Any = object()


@dataclass(frozen=True)
class Tolerance:
    qty: int
    cost_paise: int


def tolerance() -> Tolerance:
    """The tolerance in force, from settings (ST-REC-1: 0 pieces and Rs 1 a line)."""
    return Tolerance(
        qty=int(getattr(settings, "KDPS_THREE_WAY_QTY_TOLERANCE", 0)),
        cost_paise=int(getattr(settings, "KDPS_THREE_WAY_COST_TOLERANCE_PAISE", 100)),
    )


@dataclass(frozen=True)
class BookedLine:
    """One booking line as this delivery is compared with it."""

    line_key: str
    #: The line's quantity less what other deliveries have already been linked to it.
    open_qty: int
    #: Booked cost per piece, or None when the booking gave none.
    cost_paise: int | None


def _paise(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _money(value: int | None) -> str | None:
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# The comparison itself: pure, so the rules are tested without a database
# ---------------------------------------------------------------------------


def match_rows(
    *,
    claim_lines: list[dict[str, Any]] | None,
    grn_lines: list[dict[str, Any]],
    booked: dict[str, BookedLine] | None,
    link_of: dict[str, set[str]],
    claim_booking: dict[str, set[str]],
    limits: Tolerance,
) -> list[dict[str, Any]]:
    """One row per invoice line, then one per counted line on no invoice line.

    ``claim_lines`` is None when no invoice has been recorded. ``booked`` is None
    when the delivery has no booking; otherwise it holds every current booking line.
    ``link_of`` names, per GRN line, the booking lines its pieces are linked to;
    ``claim_booking`` the booking lines an invoice line's own item exactly is.
    """
    rows = _paired(claim_lines, grn_lines, link_of, claim_booking)
    _share_booked(rows, booked)
    return [
        {
            "row_key": entry["row_key"],
            "invoice_line_key": entry["invoice_line_key"],
            "grn_line_keys": entry["grn_line_keys"],
            "booking_line_key": entry["booking_line_key"],
            "booking": entry["booking"],
            "description": entry["description"],
            "booked_qty": entry["booked_qty"],
            "invoiced_qty": entry["invoiced_qty"],
            "counted_qty": entry["counted_qty"],
            "booked_cost_paise": _money(entry["booked_cost"]),
            "invoiced_cost_paise": _money(entry["invoiced_cost"]),
            "cost_difference_paise": _money(cost_difference(entry)),
            "results": judge(entry, invoiced=claim_lines is not None, limits=limits),
        }
        for entry in rows
    ]


def _row(
    *,
    invoice_line_key: str | None,
    grn_line_keys: list[str],
    description: str,
    invoiced_qty: int | None,
    invoiced_cost: int | None,
    counted_qty: int,
    candidates: set[str],
) -> dict[str, Any]:
    return {
        "row_key": invoice_line_key or grn_line_keys[0],
        "invoice_line_key": invoice_line_key,
        "grn_line_keys": grn_line_keys,
        "description": description,
        "invoiced_qty": invoiced_qty,
        "counted_qty": counted_qty,
        "invoiced_cost": invoiced_cost,
        "candidates": candidates,
        "booking": NO_BOOKING,
        "booking_line_key": None,
        "booked_qty": None,
        "booked_cost": None,
    }


def _paired(
    claim_lines: list[dict[str, Any]] | None,
    grn_lines: list[dict[str, Any]],
    link_of: dict[str, set[str]],
    claim_booking: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Invoice lines with their counted figure, paired the way the GRN pairs them,
    then every counted line on no invoice line; each with its candidate booking lines."""
    from inbound.goods_services import compare_claim

    rows: list[dict[str, Any]] = []
    unmatched = grn_lines
    if claim_lines is not None:
        comparisons, matched = compare_claim(claim_lines, grn_lines)
        claims = {claim["line_key"]: claim for claim in claim_lines}
        for comparison in comparisons:
            claim = claims[comparison["claim_line_key"]]
            linked: set[str] = set()
            for key in comparison["line_keys"]:
                linked |= link_of.get(key, set())
            rows.append(
                _row(
                    invoice_line_key=claim["line_key"],
                    grn_line_keys=list(comparison["line_keys"]),
                    description=str(claim.get("description") or ""),
                    invoiced_qty=int(comparison["claimed_qty"]),
                    invoiced_cost=_paise(claim.get("invoice_basic_paise")),
                    counted_qty=int(comparison["counted_qty"]),
                    candidates=linked or claim_booking.get(claim["line_key"], set()),
                )
            )
        unmatched = [line for line in grn_lines if line["line_key"] not in matched]
    for line in unmatched:
        rows.append(
            _row(
                invoice_line_key=None,
                grn_line_keys=[line["line_key"]],
                description=str((line.get("identity") or {}).get("description") or ""),
                invoiced_qty=None,
                invoiced_cost=None,
                counted_qty=int(line["qty"]),
                candidates=set(link_of.get(line["line_key"], set())),
            )
        )
    return rows


def _share_booked(rows: list[dict[str, Any]], booked: dict[str, BookedLine] | None) -> None:
    """Give each row its booking line and booked figure.

    Several rows on one booking line (an invoice that splits one item over two
    lines) share its open quantity in row order, the last row taking what is left -
    the same way ``compare_claim`` shares a count.
    """
    if booked is None:
        return
    on_line: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in rows:
        candidates = entry["candidates"]
        if len(candidates) == 1 and next(iter(candidates)) in booked:
            entry["booking"] = LINKED
            entry["booking_line_key"] = next(iter(candidates))
            on_line[entry["booking_line_key"]].append(entry)
        else:
            entry["booking"] = SEVERAL if len(candidates) > 1 else NOT_BOOKED
    for key, members in on_line.items():
        remaining = max(booked[key].open_qty, 0)
        for index, entry in enumerate(members):
            wanted = entry["invoiced_qty"]
            if wanted is None:
                wanted = entry["counted_qty"]
            share = remaining if index == len(members) - 1 else min(wanted, remaining)
            remaining -= share
            entry["booked_qty"] = share
            entry["booked_cost"] = booked[key].cost_paise


def cost_difference(entry: dict[str, Any]) -> int | None:
    """Invoiced less booked cost over the line's invoiced pieces, or None if unknown."""
    if entry["invoiced_cost"] is None or entry["booked_cost"] is None:
        return None
    if not entry["invoiced_qty"]:
        return None
    return int(entry["invoiced_qty"]) * (int(entry["invoiced_cost"]) - int(entry["booked_cost"]))


def judge(entry: dict[str, Any], *, invoiced: bool, limits: Tolerance) -> list[str]:
    """``short``, ``excess`` and ``cost_differs`` as they apply; empty is a match.

    Short: fewer counted than invoiced. Excess: more counted than invoiced, counted
    and on no invoice line, or more invoiced or counted than the booking line still
    had open (a line on no booking line of a booked delivery had none). A line two
    booking lines could be is left for the buyer to link, not judged against either.
    Cost differs: the invoiced cost is off the booked cost by more than the
    tolerance over the line. A cost not given on either side is not judged.
    """
    results: list[str] = []
    counted = int(entry["counted_qty"])
    claimed = entry["invoiced_qty"]
    if claimed is not None:
        if counted - claimed < -limits.qty:
            results.append(SHORT)
        elif counted - claimed > limits.qty:
            results.append(EXCESS)
    elif invoiced and counted > limits.qty:
        results.append(EXCESS)
    most = max(claimed or 0, counted)
    over_booking = (
        entry["booking"] == LINKED and most - int(entry["booked_qty"]) > limits.qty
    ) or (entry["booking"] == NOT_BOOKED and most > limits.qty)
    if over_booking and EXCESS not in results:
        results.append(EXCESS)
    difference = cost_difference(entry)
    if difference is not None and abs(difference) > limits.cost_paise:
        results.append(COST_DIFFERS)
    return results


def for_viewer(rows: list[dict[str, Any]], *, sees_cost: bool) -> list[dict[str, Any]]:
    """Rows as this viewer may read them: without cost, a store role sees quantities."""
    out = []
    for entry in rows:
        shown = dict(entry)
        if not sees_cost:
            shown = {key: value for key, value in shown.items() if key not in COST_KEYS}
            shown["results"] = [r for r in entry["results"] if r != COST_DIFFERS]
        shown["status"] = "match" if not shown["results"] else "mismatch"
        out.append(shown)
    return out


def sees_cost(access: AccessContext, site_id: int, brand_id: int | None) -> bool:
    """The goods-v1 cost field at this site, as the GRN detail already asks it."""
    from inbound.goods_services import READ_ACTIONS

    return "cost" in access.field_grants(
        site_id=site_id, brand_id=brand_id, actions=set(READ_ACTIONS)
    )


# ---------------------------------------------------------------------------
# Gathering the three figures of a GRN
# ---------------------------------------------------------------------------


def booking_of(grn: GoodsGrn, run: CommandRun | None = None) -> GoodsBooking | None:
    """The booking ``grn`` was received against: the arrival's, or else the one
    booking the buyer has linked its lines to since (an arrival recorded unbooked)."""
    booking_pk = grn.booking_id or grn.arrival.booking_id
    if booking_pk is None:
        linked = {
            link.booking_id
            for link in BookingReceiptLink.objects.filter(grn_id=grn.document_id).only("booking_id")
        }
        if run is not None:
            linked |= {
                row.booking_id
                for row in run.evidence.pending
                if isinstance(row, BookingReceiptLink) and row.grn_id == grn.document_id
            }
        booking_pk = next(iter(linked)) if len(linked) == 1 else None
    return GoodsBooking.objects.filter(pk=booking_pk).first() if booking_pk else None


def grns_of_booking(booking: GoodsBooking) -> list[GoodsGrn]:
    """Every GRN received against ``booking`` or linked to it."""
    linked = BookingReceiptLink.objects.filter(booking_id=booking.pk).values_list(
        "grn_id", flat=True
    )
    return list(
        GoodsGrn.objects.select_related("document", "arrival")
        .filter(models.Q(booking_id=booking.pk) | models.Q(document_id__in=linked))
        .filter(counter_of__isnull=True)
        .order_by("created_at", "document_id")
    )


def _earlier_grns(grn: GoodsGrn, candidates: set[str]) -> set[str]:
    """Of ``candidates`` (GRN document ids), those issued before ``grn``: only an
    earlier delivery has already used up part of a booking line."""
    others = [uuid.UUID(c) for c in candidates if c != str(grn.document_id)]
    if not others:
        return set()
    mine = GoodsGrn.objects.filter(pk=grn.pk).values_list("created_at", "document_id").first()
    if mine is None:
        return set()
    at, document_id = mine
    return {
        str(pk)
        for pk in GoodsGrn.objects.filter(document_id__in=others)
        .filter(models.Q(created_at__lt=at) | models.Q(created_at=at, document_id__lt=document_id))
        .values_list("document_id", flat=True)
    }


def _booked(
    grn: GoodsGrn, run: CommandRun | None
) -> tuple[dict[str, BookedLine] | None, dict[str, set[str]], list[dict[str, Any]], Any]:
    """The booking's open lines, each GRN line's linked booking lines, the booking's
    current lines and the booking, or ``(None, {}, [], None)`` with no booking."""
    from vendors.goods_services import booking_lines

    booking = booking_of(grn, run)
    head = (
        DocumentHead.objects.filter(document_id=booking.document_id).first()
        if booking is not None
        else None
    )
    if booking is None or head is None or head.live_version_id is None:
        return None, {}, [], None
    _header, lines, root = booking_lines(booking, head, run)
    current_of = {stable: key for key, stable in root.items()}
    links = list(BookingReceiptLink.objects.filter(booking_id=booking.pk))
    if run is not None:
        links += [
            row
            for row in run.evidence.pending
            if isinstance(row, BookingReceiptLink) and row.booking_id == booking.pk
        ]
    # Net linked pieces per (stable booking line, GRN, GRN line): a counter link
    # reverses the one it names.
    net: dict[tuple[str, str, str], int] = defaultdict(int)
    for link in links:
        key = str(link.booking_line_key)
        stable = root.get(key, key)
        sign = -1 if link.counter_of_id is not None else 1
        net[(stable, str(link.grn_id), str(link.grn_line_key))] += sign * link.linked_qty
    earlier = _earlier_grns(grn, {grn_id for (_stable, grn_id, _line) in net})
    elsewhere: dict[str, int] = defaultdict(int)
    link_of: dict[str, set[str]] = defaultdict(set)
    for (stable, grn_id, grn_line_key), qty in net.items():
        if grn_id == str(grn.document_id):
            if qty > 0 and stable in current_of:
                link_of[grn_line_key].add(current_of[stable])
        elif grn_id in earlier:
            elsewhere[stable] += qty
    booked = {
        str(line["line_key"]): BookedLine(
            line_key=str(line["line_key"]),
            open_qty=int(line["qty"])
            - elsewhere[root.get(str(line["line_key"]), line["line_key"])],
            cost_paise=_paise(line.get("cost_paise")),
        )
        for line in lines
    }
    return booked, dict(link_of), lines, booking


def _claim_booking(
    claim_lines: list[dict[str, Any]], lines: list[dict[str, Any]], booking: Any
) -> dict[str, set[str]]:
    """Per invoice line, the one booking line its own item exactly is (if any)."""
    from masters.goods_identity_services import sku_booking_facts
    from vendors.goods_services import exact_booking_line

    facts = sku_booking_facts(
        booking.tenant_id, [c["sku_id"] for c in claim_lines if c.get("sku_id")]
    )
    out: dict[str, set[str]] = {}
    for claim in claim_lines:
        sku = facts.get(str(claim.get("sku_id") or ""))
        if sku is not None:
            if sku.brand_id != booking.brand_id:
                continue
            style, size, colour = sku.style_code, sku.size_value_id, sku.colour_value_id
        elif claim.get("style_code"):
            style, size, colour = str(claim["style_code"]), claim.get("size_value_id"), None
        else:
            continue
        match = exact_booking_line(
            lines, style_code=style, size_value_id=size, colour_value_id=colour
        )
        if match is not None:
            out[claim["line_key"]] = {str(match["line_key"])}
    return out


def grn_rows(
    grn: GoodsGrn,
    *,
    run: CommandRun | None = None,
    lines: list[dict[str, Any]] | None = None,
    claim_lines: list[dict[str, Any]] | None = _UNSET,
    extra_counters: Any = (),
) -> list[dict[str, Any]]:
    """The three-way rows of ``grn`` as they stand, with every cost figure.

    Inside a command, pass what it changed and has not sealed yet: the GRN's own
    ``lines``, the new ``claim_lines`` or the counter-GRN's ``extra_counters``.
    Receipt links and booking corrections of the command are read from ``run``.
    """
    from inbound.goods_services import current_grn_lines, latest_claim

    if lines is None:
        lines = current_grn_lines(grn.document_id, extra_counters)
    if claim_lines is _UNSET:
        claim = latest_claim(grn.arrival_id)
        claim_lines = list(claim.lines) if claim is not None else None
    booked, link_of, booking_current, booking = _booked(grn, run)
    claim_booking = (
        _claim_booking(claim_lines, booking_current, booking)
        if claim_lines and booking is not None
        else {}
    )
    return match_rows(
        claim_lines=claim_lines,
        grn_lines=lines,
        booked=booked,
        link_of=link_of,
        claim_booking=claim_booking,
        limits=tolerance(),
    )


# ---------------------------------------------------------------------------
# After a command: compare again, own the mismatch, audit the change
# ---------------------------------------------------------------------------


#: The state before the first comparison of a GRN.
NO_STATE: dict[str, Any] = {
    "mismatch": False,
    "reason_code": None,
    "lines_mismatched": 0,
    "cost_differs_lines": 0,
    "figures": None,
}


def _state(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What the audit records and the exception says, before and after.

    The reason code names only quantity results (``SHORT``, ``EXCESS``), or plain
    ``MISMATCH`` when only cost differs: the exception and the audit entry are read
    by store roles, who must not learn that cost differs. How many lines differ in
    cost is kept under a cost key, which the audit log strips for them (ticket 02).
    ``figures`` fingerprints every line's figures, so a changed figure is audited
    even when the verdict stays the same.
    """
    wrong = [entry for entry in rows if entry["results"]]
    reasons = sorted(
        {result.upper() for entry in wrong for result in entry["results"] if result != COST_DIFFERS}
    )
    return {
        "mismatch": bool(wrong),
        "reason_code": ("+".join(reasons)[:60] or "MISMATCH") if wrong else None,
        "lines_mismatched": len(wrong),
        "cost_differs_lines": sum(1 for entry in rows if COST_DIFFERS in entry["results"]),
        "figures": content_hash(rows) if rows else None,
    }


def _previous_state(run: CommandRun, subject: str) -> dict[str, Any]:
    """The state the last audit entry of ``subject`` left, this command's own included."""
    pending = [
        row
        for row in run.evidence.pending
        if isinstance(row, AuditEvent) and row.action == AUDIT_ACTION and row.subject_key == subject
    ]
    if pending:
        return {**NO_STATE, **(pending[-1].after or {})}
    previous = (
        AuditEvent.objects.filter(
            tenant_id=run.tenant_id, action=AUDIT_ACTION, subject_key=subject, outcome="recorded"
        )
        .order_by("-recorded_at", "-id")
        .first()
    )
    return {**NO_STATE, **((previous.after if previous is not None else None) or {})}


def refresh(
    run: CommandRun,
    grn: GoodsGrn,
    *,
    lines: list[dict[str, Any]] | None = None,
    claim_lines: list[dict[str, Any]] | None = _UNSET,
    extra_counters: Any = (),
) -> None:
    """Compare ``grn`` again after this command, where the switch is on at its site.

    A line that disagrees opens the buyer's ``three_way_mismatch`` exception (a
    new occurrence each time, as ``receipt_discrepancy`` does, so no row is
    re-locked late in the caller's lock order); every line agreeing closes it.
    When the state changes, an audit record carries it before and after. It never
    refuses: counting goes on.

    Call it last in the command: it takes the GRN's advisory lock at the highest
    rank, so two commands on one GRN compare, open and audit one after the other.
    """
    site_id = grn.arrival.site_id
    if not is_feature_on(site_id, FEATURE_KEY):
        return
    subject = f"grn:{grn.document_id}"
    run.advisory_lock(LockRank.CHAIN, [f"three-way:{subject}"])
    rows = grn_rows(
        grn, run=run, lines=lines, claim_lines=claim_lines, extra_counters=extra_counters
    )
    after = _state(rows)
    before = _previous_state(run, subject)
    is_open = GoodsException.objects.filter(
        tenant_id=run.tenant_id, kind=EXCEPTION_KIND, subject_key=subject, state="open"
    ).exists()
    if after["mismatch"] and not is_open:
        open_exception(
            run,
            kind=EXCEPTION_KIND,
            site_id=site_id,
            subject_key=subject,
            reason_code=after["reason_code"] or "MISMATCH",
            source_event_key=event_key(EXCEPTION_KIND, grn.document_id, run.key_id),
            allowed_resolution_actions=[
                "bookings/{id}/corrections",
                "bookings/{id}/receipt-links",
                "inbound/arrivals/{id}/invoice",
                "inbound/grns/{id}/counter",
            ],
        )
    elif not after["mismatch"] and is_open:
        resolve_exceptions(
            run, kind=EXCEPTION_KIND, subject_key=subject, reason_code="THREE_WAY_AGREES"
        )
    if after != before:
        run.record(
            AuditEvent(
                action=AUDIT_ACTION,
                subject_key=subject,
                site_id=site_id,
                outcome="recorded",
                reason_code=after["reason_code"],
                before=before,
                after=after,
                authority=run.authority,
            )
        )

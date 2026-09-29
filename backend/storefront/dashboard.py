"""What one store's Home is made of (D10 §2, ticket #174).

A read-only aggregator: it imports other apps' models, writes nothing, and owns
no table of its own. Living in its own app keeps every one of those cross-app
imports on one side of a seam, so no domain app grows a dependency on another
just to draw a card (ADR-0002) - the same shape `search` already has.

**Every count here is a count of something that exists today.** The nine
`action_queue` keys in `api-contract.md` include three that read `sell_heldbill`,
`sell_deferredcosting` and `sell_continuityflag`, and those tables arrived one
ticket at a time. A key was *absent* rather than reported as nought until its
table was there: a row saying "0 bills on hold" would be a sentence about a
store's morning, and what would actually be true is that nothing could be put on
hold yet. `held_bills` joined the queue with the hold list itself (#185),
`uncosted_sale_lines` with the costing sweep (#186) and `continuity_flags` with
the daily check (#188), which completed the contract's nine. `grn_pt_pending`
counted legacy GRNs still owed a PT and left with legacy receiving (OPS-18);
goods waiting on receiving work are Receive Goods' own Pending list.

The money tiles were noughts for the same reason and are not any more (#188).
`sales_live` stays in the payload and is now true wherever a bill can exist: the
screen still needs to tell "no bills yet today" from "billing is not live here",
and a store the POS has not reached is the second. The arithmetic itself is
`storefront.day`, shared with the Money section's cash summary, so Home and Money
cannot read one day two ways.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.db.models import F, Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from accounts.role_assignments import effective_assignments
from accounts.sections import CAP_APPROVE, CAP_VIEW, meets
from accounts.unified_policy import role_capability, role_fields
from alerts.models import Alert, AlertKind, AlertStatus
from approvals.services import inbox_for
from core.documents import DocStatus, VoucherSeries
from core.fiscal import financial_year
from masters.models import Store, StoreTarget
from masters.scoping import active_store_ids, is_brand_scoped
from masters.store_features import is_feature_on
from offers.models import Offer
from offers.serializers import one_liner
from outbound import count_schedules
from outbound.models import CountStatus, MarkDamaged, Stocktake, StoreTransfer
from sell.models import SALE_DOC_TYPE, ContinuityFlag, DeferredCosting, HeldBill
from storefront import checklists
from storefront.day import DayMoney, money_for, net_sales_by_day

#: How many in-transit cartons the "live in store" card names before it stops
#: listing and the action-queue row carries the rest. Four fits the card; the
#: count above it is never truncated.
IN_TRANSIT_SHOWN = 4

#: Days on the sparkline, today included.
SPARKLINE_DAYS = 7


@dataclass(frozen=True)
class InTransitTransfer:
    """One carton on the road to this store, and how many pieces are on it."""

    #: The transfer's own id, so the card can open the document where the store
    #: actually scans it in. Beyond the contract's sketch, and the reason is a
    #: QA failure: the queue row alone landed the receiving store on the
    #: in-transit screen, which is scoped to the *sending* store, so a store
    #: reading "1 carton to receive" clicked through to "nothing on the road".
    id: int
    doc_number: str
    pieces: int
    expected: str


@dataclass(frozen=True)
class StorePick:
    """Which store this dashboard is about, or the sentence saying why none is.

    The refusal travels with the answer rather than being reconstructed from the
    query string afterwards: there are three different ways to end up without a
    store and only the code that ruled each one out knows which happened.
    """

    store: Store | None
    refusal: str = ""


def resolve_store(user: Any, requested_code: str) -> StorePick:
    """The one store this dashboard is about.

    **One gate, and every row on the screen obeys it.** Scope narrowed by the
    top-bar switcher (`active_store_ids`) decides the store, and every count in
    `build()` is then filtered to that one store id. Resolving the store
    switcher-blind and counting switcher-obeying is the defect class #171 shipped
    and #174 must not: the approvals row reads its rows through `inbox_for`,
    which obeys the switcher, so a store picked past the switcher would show a
    real count as nought and nobody would know a card was lying.

    That is also why `?store=` narrows *within* the active unit instead of
    overriding it. Naming a store the switcher has filtered out is a
    contradiction, and the honest answer to a contradiction is a refusal.

    A network person who has picked no unit gets no store either: "the store's
    Home" is a question about a store, and with fifty in view there is no honest
    answer, only an arbitrary one.

    **A brand-scoped person gets no store at all**, and that is a fail-closed
    branch rather than an omission. `visible_store_ids` answers `None` for a
    brand manager, and `None` there means "stores are the wrong question" - not
    "every store". Reading it as the second is the read-scope fail-open class
    this codebase has shipped before, and it bites harder now that the tiles
    carry real money: a brand manager naming any shop in `?store=` would have
    read that shop's takings, bill count and tender split, none of which is a
    fact about their brands. `actionable_store_ids` is the existing spelling of
    the same refusal, and it is the one used here.
    """
    # Asked before anything else, because it is the one refusal that does not
    # depend on what was asked for: "which store is this screen about" is a claim
    # about a *place*, and a boundary made of brands cannot support one.
    if is_brand_scoped(user):
        return StorePick(
            None,
            "This screen is one store's day. Your access is by brand, so there is "
            "no store to open here.",
        )
    try:
        ids = active_store_ids(user, section="home", minimum="view")
    except PermissionDenied as exc:
        # An unknown or out-of-scope `X-KDPS-Unit`. Caught here, before any count
        # runs, so the caller gets this endpoint's own refusal shape: the
        # contract says it answers `{"error", "code"}` and nothing else, and the
        # scoping layer's own DRF `{"detail": ...}` must not leak past it.
        return StorePick(None, str(exc.detail))
    if requested_code:
        rows = Store.objects.filter(code__iexact=requested_code, is_active=True)
        if ids is not None:
            rows = rows.filter(id__in=ids)
        store = rows.first()
        if store is None:
            # One sentence for "no such store" and for "not yours / not the unit
            # you have picked", deliberately: the contract allows this endpoint
            # only `SCOPE_DENIED`, and telling an outsider which of the three it
            # was would confirm the store exists.
            return StorePick(
                None,
                f"'{requested_code}' is not a store you can open. Check the code, "
                "or the unit picked in the top bar.",
            )
        return StorePick(store)
    if ids is None or len(ids) != 1:
        return StorePick(
            None, "Pick a store first - this screen is one store's day, not the network's."
        )
    return StorePick(Store.objects.filter(pk=ids[0], is_active=True).first())


# --- the action queue -----------------------------------------------------


def _approvals_pending(user: Any, store: Store) -> int:
    """Rows in this person's own approvals inbox that belong to this store.

    Deliberately `inbox_for`, not a hand-rolled count of pending approvals: the
    inbox already refuses one's own requests and matches the approver role, and a
    Home card promising "2 waiting for you" that opens onto an empty screen is
    worse than no card. Network-level approvals (no store) are not this store's.
    """
    return int(inbox_for(user).filter(store_id=store.id).count())


def inbound_in_transit(store: Store) -> list[InTransitTransfer]:
    """Cartons dispatched to this store and not yet scanned in, newest first.

    A transfer with a receipt is done; one whose gap has been closed has nothing
    left on the road. Pieces are summed in SQL from the same three columns
    `StoreTransferLine.qty_in_transit` derives from, so the card and the line
    cannot disagree.
    """
    rows = (
        StoreTransfer.objects.filter(
            docstatus=DocStatus.SUBMITTED,
            destination_store_id=store.id,
            receipt__isnull=True,
        )
        .exclude(dispatch_date__isnull=True)
        .annotate(
            pieces=Sum(
                F("lines__qty_dispatched") - F("lines__qty_received") - F("lines__qty_resolved")
            )
        )
        .filter(pieces__gt=0)
        .order_by("-dispatch_date")
    )
    return [
        InTransitTransfer(
            id=transfer.id,
            doc_number=transfer.doc_number or "",
            pieces=int(transfer.pieces or 0),
            # There is no expected-arrival *date* on a transfer, only the
            # dispatcher's note ("by Friday", "bus 7pm"). The card shows what the
            # store was actually told rather than inventing a date from it.
            expected=transfer.expected_arrival_note,
        )
        for transfer in rows
    ]


def _quarantine_to_confirm(store: Store) -> int:
    """Damage reported at this store and still a draft (#138): the pieces are
    sellable until somebody senior confirms, which is the work this row is for."""
    return int(MarkDamaged.objects.filter(store_id=store.id, docstatus=DocStatus.DRAFT).count())


def _rtb_windows_closing(store: Store) -> int:
    """Open return-window countdowns at this store.

    Read off `alerts_alert` rather than recomputed from the returnable pool: the
    nightly check already walks every brand's holdings and applies the configured
    thresholds, and a dashboard that re-derived it would drift from the Alerts
    screen the row sends you to.
    """
    return int(
        Alert.objects.filter(
            kind=AlertKind.RETURN_WINDOW, status=AlertStatus.OPEN, store_id=store.id
        ).count()
    )


def _open_count_session(store: Store) -> int:
    """Counts open at this store - the exercise, not the counter's pass, because
    that is the row `/stock-count` lists and clicking through must match."""
    return int(Stocktake.objects.filter(store_id=store.id, status=CountStatus.OPEN).count())


def _held_bills(store: Store) -> int:
    """Carts the counter has parked (#185, grill Q13).

    The till's own mirror, not a document: a hold holds no stock and no money, so
    this is the one row on the queue that is a count of *unfinished conversations*
    rather than of work the books are waiting on. It is on the queue because the
    store chooses at day close whether each one is kept or let go, and a manager
    should not have to walk to the counter to find out how many there are.
    """
    return int(HeldBill.objects.filter(store_id=store.id).count())


def _uncosted_sale_lines(store: Store) -> int:
    """Lines this store sold before the paperwork could price them (#186).

    Counted per *line* rather than per bill, because a line is what the sweep
    releases and what the daily check ages - a bill with two such lines is two
    pieces the books cannot value, not one problem. Rows already posted drop out
    the moment their PT lands, which is what makes this a queue rather than a
    tally: it is meant to sit at nought.
    """
    return int(
        DeferredCosting.objects.filter(
            store_id=store.id, status=DeferredCosting.Status.WAITING
        ).count()
    )


def _continuity_flags(store: Store) -> int:
    """Counter exceptions nobody has cleared (#188).

    Open only. `ignored` is the store having looked and decided this one is fine,
    and a queue that kept counting it would make the third state mean nothing.

    Not date-scoped, unlike the Money section's own figure: this is a to-do list,
    and a flag raised on Tuesday is still work on Thursday. The day summary asks
    the narrower question ("what did *this* day leave open") because that is the
    one somebody counting a drawer is asking.
    """
    return int(
        ContinuityFlag.objects.filter(store_id=store.id, status=ContinuityFlag.Status.OPEN).count()
    )


def action_queue(user: Any, store: Store, in_transit_count: int) -> list[dict[str, Any]]:
    """The needs-your-action rows, in the contract's order.

    Every row ships with its count even when that count is nought: these all read
    a table that exists, so nought is a fact about the store and not about the
    build. The screen greys a nought row rather than hiding it - a queue that
    changes length as work is cleared is one you stop trusting to be the whole
    list. Eight keys: the ninth, `grn_pt_pending`, counted legacy receiving and
    went with it (OPS-18).
    """
    rows = [
        {"key": "approvals_pending", "count": _approvals_pending(user, store)},
        {"key": "transfers_to_receive", "count": in_transit_count},
        {"key": "quarantine_to_confirm", "count": _quarantine_to_confirm(store)},
        {"key": "rtb_windows_closing", "count": _rtb_windows_closing(store)},
        {"key": "open_count_session", "count": _open_count_session(store)},
        {"key": "held_bills", "count": _held_bills(store)},
        {"key": "uncosted_sale_lines", "count": _uncosted_sale_lines(store)},
        {"key": "continuity_flags", "count": _continuity_flags(store)},
    ]
    # Store operations ticket 35 (ST-INV-3): the scheduled counts due today and
    # not yet done. A ninth row only where the scheduled-counts switch is on, so a
    # store with it off keeps exactly the queue it had.
    if is_feature_on(store, count_schedules.FEATURE_KEY):
        due = count_schedules.due_today([store], timezone.localdate())
        rows.append({"key": "counts_due_today", "count": len(due)})
    # Store operations ticket 49 (ST-OPS-4): checklist items whose time has passed
    # and are still not ticked, today's and the last few days'. Only where the
    # store-checklists switch is on, for the same reason.
    if is_feature_on(store, checklists.FEATURE_KEY):
        rows.append({"key": "checklist_missed", "count": checklists.today_at(store).missed_items})
    return rows


# --- the rest of the payload ----------------------------------------------


def sales_live(store: Store) -> bool:
    """Can a bill exist at this location at all?

    The year's `SAL` series row, which is the same reading the till's own boot
    call takes (`register_state.series_open`) - a store without one cannot have a
    number minted for it, so every bill it printed would fail to sync. A warehouse
    has no counter and never gets the row.

    It matters because four noughts say "this store sold nothing today" and the
    truth at a location the POS has not reached is "this store cannot bill yet",
    and a store manager reading the first when the second is true will go looking
    for the trade rather than for the rollout.
    """
    return VoucherSeries.objects.filter(
        fy=financial_year(), store_code=store.code, doc_type=SALE_DOC_TYPE
    ).exists()


def today_block(money: DayMoney, yesterday_paise: int) -> dict[str, Any]:
    """The four money tiles, as the contract fixes them (§Step 1, item 2).

    `vs_yesterday_pct` is `None` rather than nought when yesterday took nothing:
    "up 0%" against a day with no trade is arithmetic nobody can read, and there
    is no percentage change from nothing.
    """
    return {
        "net_sales_paise": money.net_sales_paise,
        "bills": money.bills,
        "avg_bill_paise": money.avg_bill_paise,
        "pieces": money.pieces,
        "collections": dict(money.collections),
        "vs_yesterday_pct": (
            round((money.net_sales_paise - yesterday_paise) * 100 / yesterday_paise)
            if yesterday_paise > 0
            else None
        ),
    }


def last7(store: Store, today: date) -> list[dict[str, Any]]:
    """Seven dated points, oldest first, so the sparkline has an x-axis whatever
    the values come to - a day with no trade is a nought on the strip, not a gap
    in it."""
    days = [today - timedelta(days=offset) for offset in range(SPARKLINE_DAYS - 1, -1, -1)]
    takings = net_sales_by_day(store, days)
    return [{"date": day.isoformat(), "net_sales_paise": takings[day]} for day in days]


def manager_block(user: Any, store: Store, today: date) -> dict[str, Any] | None:
    """Month-to-date against the store's target, or ``None`` for a cashier.

    ``None`` means the key is absent from the payload entirely, not present and
    empty: the screen decides whether to draw the row by asking whether the row
    is there, so a cashier's browser is never handed a number it must remember
    not to render.

    **Two rungs, not one.** The contract names `sell >= approve` - that is who
    the row is *for*. But what the row carries is a store's target and its
    month-to-date, and `StoreTarget` is Money data: `/api/masters/store-targets`
    serves the very same rows behind `money: view`. Gating on Sell alone let a
    seat with `sell: manage` and `money: none` read the target through this
    endpoint - the IT Admin, whose "Admin = no Money" cell is a ratified one. A
    second door onto data with a lock on the first is not a second view of it, it
    is a way round the lock.

    Both gates read the stored matrix, never a role name - which role holds
    either rung is admin-editable data (#173).
    """
    if store.tenant_id != getattr(user, "tenant_id", None):
        return None
    if not any(
        row.all_brands
        and (row.all_sites or store.pk in row.site_ids)
        and meets(role_capability(row.role, "sell"), CAP_APPROVE)
        and meets(role_capability(row.role, "money"), CAP_VIEW)
        and "financial" in role_fields(row.role)
        for row in effective_assignments(user.human_id) if user.human_id
    ):
        return None
    month_start = today.replace(day=1)
    target = StoreTarget.objects.filter(store_id=store.id, month=month_start).first()
    mtd = net_sales_by_day(
        store, [month_start + timedelta(days=n) for n in range((today - month_start).days + 1)]
    )
    return {
        # Day close (I3, store open/close) is its own designed flow, sequenced
        # after this one. The key is here with an honest state rather than
        # missing, so the row has its shape from the first day.
        "day_close": {"date": today.isoformat(), "state": "not_built"},
        # The month so far, first of the month to today inclusive - and not a
        # month-to-*yesterday*, because the bar is read beside a target somebody
        # is still trading towards.
        "mtd_net_paise": sum(mtd.values()),
        "target_paise": int(target.target_paise) if target else 0,
    }


def live_offers(store: Store, today: date) -> list[dict[str, Any]]:
    """What is running at this counter this morning (contract §Step 1, item 4).

    A card the store reads before opening, so it says the rule in a phrase rather
    than in its dials - and the phrase is generated from the rule (`one_liner`)
    rather than typed beside it, because a hand-written summary drifts away from
    the thing it summarises and the summary is what the shop floor believes.
    """
    rows = (Offer.objects.for_tenant(store.tenant_id).live_on(today).for_store(store.code).select_related("brand")).order_by(
        "priority", "id"
    )
    return [
        {"id": offer.id, "brand": offer.brand_name, "one_liner": one_liner(offer)} for offer in rows
    ]


def build(user: Any, store: Store) -> dict[str, Any]:
    """One store's whole Home, in the contract's shape."""
    today = timezone.localdate()
    in_transit = inbound_in_transit(store)
    week = last7(store, today)
    payload: dict[str, Any] = {
        "store": store.code,
        # Not in the contract's sketch, which was written as if `sell` existed.
        # Without it four noughts cannot say whether this store had a quiet day
        # or has no counter at all.
        "sales_live": sales_live(store),
        # Yesterday comes off the strip that is already loaded rather than from a
        # query of its own: it is the second-to-last point on it.
        "today": today_block(money_for(store, today), week[-2]["net_sales_paise"]),
        "action_queue": action_queue(user, store, len(in_transit)),
        "live": {
            "offers": live_offers(store, today),
            "in_transit": [
                {
                    "id": row.id,
                    "doc_number": row.doc_number,
                    "pieces": row.pieces,
                    "expected": row.expected,
                }
                for row in in_transit[:IN_TRANSIT_SHOWN]
            ],
        },
        "last7": week,
    }
    manager = manager_block(user, store, today)
    if manager is not None:
        payload["manager"] = manager
    return payload

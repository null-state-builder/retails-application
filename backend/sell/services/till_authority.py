"""Which device is this store's counter, for how long, and what it took with it.

Three questions that look like one and are not (PRD §10.1 and §10.2, OPS-09):

  · **Which device?** One active `RegisteredTill` per store, owning the display
    series `{StoreCode}-{CounterID}-{Seq}`. A second registration for a store
    that already has one is refused (`TILL_TAKEN`) rather than allowed to share
    the series, because two machines numbering one series is how two customers'
    purchases end up under one key. Replacing a device is a deliberate act with a
    reason on it: the old row is switched off, a `RegisterHandover` is written,
    and the new device takes the next counter id - so no bill printed on the old
    machine can ever read the same as one printed on the new.

  · **For how long?** Twenty-four hours from the last online renewal. Renewal
    needs a live session, which is the whole point: the window is evidence that
    this device was talking to us recently, and a device that can mint its own
    window is evidence of nothing. Expiry stops *new* bills and nothing else -
    it never deletes, hides or releases a thing (§10.1).

  · **What did it take?** The snapshot it was handed, protected until somebody
    says otherwise. See `TillAllocation` for why that protection is whole-snapshot
    rather than per piece, and why a newer snapshot supersedes an older one
    instead of releasing it.

The server's clock is the only clock here. A till compares its own against the
moment it is handed at renewal; see `src/till/authority.ts` for what the device
does with a clock it cannot trust.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, NoReturn

from django.db import models, transaction
from django.utils import timezone

from core.fiscal import financial_year, previous_financial_year
from masters.goods_models import SiteGuard
from masters.models import Store
from sell.models import RegisteredTill, Sale, TillAllocation, TillPause
from sell.services.register import register_state
from sell.services.working_set import current_version

#: How long one online renewal licenses a till to keep billing offline (§10.1).
AUTHORITY_HOURS = 24


class TillError(Exception):
    """A refusal the till or the manager in front of it has to read."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def lock_site_for_till(store: Store, *, allow_frozen: bool = False) -> SiteGuard | None:
    """The shared site boundary precedes device, bill and allocation locks.

    A missing guard remains a legacy site's existing condition. A goods freeze
    cannot be escaped by renewing or replacing its registered device.
    Call only inside the operation's transaction; the lock lasts through commit.
    """
    guard = SiteGuard.objects.select_for_update().filter(
        tenant_id=store.tenant_id, site=store,
    ).first()
    if guard is not None and guard.freeze_id and not allow_frozen:
        raise TillError("UNDER_COUNT", "This store's inventory review freezes new counter work.", 409)
    return guard


# --- which device ----------------------------------------------------------


def active_till(store: Store) -> RegisteredTill | None:
    """The store's counter now, or `None` for a store that has never registered one."""
    return RegisteredTill.objects.filter(store=store, active=True).first()


def series_prefix(store_code: str, counter_id: str) -> str:
    return f"{store_code}-{counter_id}"


def render_till_number(prefix: str, seq: int) -> str:
    """How a registered till's bill reads on the shop floor: `DEO-T1-74`."""
    return f"{prefix}-{seq}"


def _next_counter_id(store: Store) -> str:
    """`T1`, then `T2`, and never a number this store has used before.

    Read off the history rather than off a count of the live rows, because the
    live rows are exactly what a replacement retires: counting them would hand the
    new device the retired one's name back.
    """
    used = set(RegisteredTill.objects.filter(store=store).values_list("counter_id", flat=True))
    n = 1
    while f"T{n}" in used:
        n += 1
    return f"T{n}"


@dataclass(frozen=True)
class TillState:
    """What the store's counter is, as the server has it."""

    registered: bool
    counter_id: str
    series_prefix: str
    authority_until: str | None
    authority_hours: int
    working_set_version: int | None
    #: The server's own clock at the moment of the answer. The till anchors its
    #: expiry arithmetic to this rather than to the device's clock - see §10.1's
    #: clock-uncertainty note and `src/till/authority.ts`.
    server_time: str
    allocation_version: int | None
    #: The counter is in its transfer pause (change PRD §10.2): no new bill until
    #: it resumes and takes a fresh dataset. Sent on every answer so a till that
    #: lost its own copy of the pause learns it back from here.
    paused: bool = False
    pause_fy: str | None = None
    pause_next_seq: int | None = None
    pause_reason: str = ""
    paused_at: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "registered": self.registered,
            "counter_id": self.counter_id,
            "series_prefix": self.series_prefix,
            "authority_until": self.authority_until,
            "authority_hours": self.authority_hours,
            "working_set_version": self.working_set_version,
            "server_time": self.server_time,
            "allocation_version": self.allocation_version,
            "paused": self.paused,
            "pause_fy": self.pause_fy,
            "pause_next_seq": self.pause_next_seq,
            "pause_reason": self.pause_reason,
            "paused_at": self.paused_at,
        }


def till_state(store: Store, till: RegisteredTill | None = None) -> TillState:
    """Read the store's till, its window and the version its shelf is on."""
    found = till if till is not None else active_till(store)
    live = live_allocation(found) if found is not None else None
    pause = live_pause(found)
    return TillState(
        registered=found is not None,
        counter_id=found.counter_id if found else "",
        series_prefix=found.series_prefix if found else "",
        authority_until=(
            found.authority_until.isoformat() if found and found.authority_until else None
        ),
        authority_hours=AUTHORITY_HOURS,
        working_set_version=current_version(store.pk),
        server_time=timezone.now().isoformat(),
        allocation_version=live.version if live is not None else None,
        paused=pause is not None,
        pause_fy=pause.fy if pause is not None else None,
        pause_next_seq=pause.next_seq if pause is not None else None,
        pause_reason=pause.reason if pause is not None else "",
        paused_at=pause.paused_at.isoformat() if pause is not None else None,
    )


@transaction.atomic
def register_till(
    store: Store, actor: Any, *, replace: bool = False, reason: str = ""
) -> tuple[RegisteredTill, str]:
    """Make this store's counter live on a device. Answers the till and its token.

    A store with an active till is refused unless the caller says out loud that it
    is replacing it and why. Replacement retires the old row, writes the
    `RegisterHandover` the store then works down from paper, and issues a fresh
    counter id - which is what "a fresh series" means for a device that shares the
    store's gap-free `till_seq` with the machine before it.
    """
    lock_site_for_till(store)
    from sell.services.online import prepare_online_sale_series
    prepare_online_sale_series(store)
    existing = RegisteredTill.objects.select_for_update().filter(store=store, active=True).first()
    if existing is not None and not replace:
        raise TillError(
            "TILL_TAKEN",
            f"{store.code} already bills from counter {existing.counter_id}. Replacing it "
            "is a separate decision, and it needs a reason.",
        )
    if existing is not None:
        if not reason.strip():
            raise TillError(
                "VALIDATION", "Say why this store's counter is moving to another device.", 400
            )
        # The old machine's unsynced bills are receipts in a drawer. Recording the
        # handover is what tells the store which ones to key back in, and it is the
        # same record a manager's handover writes - one kind of row, not two.
        from sell.services.register import record_handover

        record_handover(store, actor, reason.strip())
        # A retired device's transfer pause ends with it: the new counter shares
        # the store's series, and a pause left open would flag every bill it
        # numbers from the pause point on (change PRD §10.2).
        TillPause.objects.filter(till=existing, resumed_at__isnull=True).update(
            resumed_at=timezone.now(),
            resumed_by=actor,
            resumed_fy=models.F("fy"),
            resumed_next_seq=models.F("next_seq"),
        )
        existing.active = False
        existing.retired_at = timezone.now()
        existing.save(update_fields=["active", "retired_at"])
    counter_id = _next_counter_id(store)
    token = secrets.token_hex(16)
    till = RegisteredTill.objects.create(
        store=store,
        counter_id=counter_id,
        registered_by=actor,
        series_prefix=series_prefix(store.code, counter_id),
        active=True,
        device_token=token,
    )
    return till, token


@transaction.atomic
def renew_authority(store: Store) -> TillState:
    """Push this till's window 24 hours out from now.

    Only reachable with a live session, which is the evidence the window stands
    on. Nothing else about the till changes: a renewal is not a re-registration,
    and it neither moves the series nor touches what the device is holding.
    """
    lock_site_for_till(store)
    from sell.services.online import prepare_online_sale_series
    prepare_online_sale_series(store)
    till = RegisteredTill.objects.select_for_update().filter(store=store, active=True).first()
    if till is None:
        raise TillError(
            "TILL_NOT_REGISTERED",
            f"No counter is registered for {store.code}. A manager registers the device "
            "before it can bill.",
            404,
        )
    till.authority_until = timezone.now() + timedelta(hours=AUTHORITY_HOURS)
    till.save(update_fields=["authority_until"])
    return till_state(store, till)


# --- what it took ----------------------------------------------------------


def live_allocation(till: RegisteredTill | None) -> TillAllocation | None:
    """The protection standing over this till's snapshot, if there is one."""
    if till is None:
        return None
    return TillAllocation.objects.filter(till=till, released_at__isnull=True).first()


def store_allocation(store_id: int) -> TillAllocation | None:
    """The unreleased allocation at this site, whichever till holds it."""
    return (
        TillAllocation.objects.filter(
            till__store_id=store_id, till__active=True, released_at__isnull=True
        )
        .select_related("till", "till__store")
        .first()
    )


@transaction.atomic
def issue_allocation(till: RegisteredTill, version: int, summary: dict[str, Any]) -> TillAllocation:
    """Protect what this sync handed the till, at the version it read the shelf at.

    Re-issuing at the same version is a no-op: a till that syncs three times in a
    minute took one snapshot, not three. A *newer* version supersedes the older
    row rather than releasing it, so there is no instant at which the store's
    stock is unprotected while the counter is still holding it.
    """
    lock_site_for_till(till.store)
    current = RegisteredTill.objects.select_for_update().filter(pk=till.pk, active=True).first()
    if current is None:
        raise TillError("TILL_CHANGED", "This counter was replaced. Refresh its current identity.", 409)
    if live_pause(current) is not None:
        raise TillError("TILL_PAUSED", "This counter is paused; its released stock remains free.", 409)
    live = (
        TillAllocation.objects.select_for_update()
        .filter(till=till, released_at__isnull=True)
        .first()
    )
    if live is not None and live.version == version:
        return live
    if live is not None:
        live.released_at = timezone.now()
        live.release_reason = "superseded"
        live.save(update_fields=["released_at", "release_reason"])
    return TillAllocation.objects.create(till=till, version=version, summary=summary)


@transaction.atomic
def release_allocation(
    store: Store, version: int, actor: Any, reason: str, fy: str, next_seq: int
) -> tuple[TillAllocation, TillPause]:
    """Pause the counter and let the store's stock go, in one step (§10.2).

    Anand, 25 September 2026: the store's own person (`sell: operate`) does this,
    and releasing is the second half of pausing. The till has already written its
    own pause before it asks; this records the server's half in the same
    transaction as the release, so there is no instant at which the stock is free
    and the counter is not paused.

    The reconciliation test is "every bill this counter numbered has arrived".
    `(fy, next_seq)` is the position the counter says it would bill next, in the
    financial year the counter is counting in, so that means every number below
    it in that year has landed - not just "no hole below the highest one that
    did", which could not see a bill printed after the last upload. The year is
    the counter's and never head office's: the two clocks straddle 1 April, and
    judging a counter still on bill 3 of the old year against head office's
    empty new one would refuse an honest release (or, the other way round, call
    an honest counter out of step). The year before the counter's is checked for
    holes too, so crossing 1 April leaves no missing bill behind.

    A counter naming a position at or before a bill head office already holds -
    in its year or any later one - has lost its place and cannot vouch for
    anything (`TILL_OUT_OF_STEP`).
    """
    lock_site_for_till(store)
    till = RegisteredTill.objects.select_for_update().filter(store=store, active=True).first()
    if till is None:
        raise TillError("TILL_NOT_REGISTERED", f"No counter is registered for {store.code}.", 404)
    allocation = (
        TillAllocation.objects.select_for_update()
        .filter(till=till, version=version, released_at__isnull=True)
        .first()
    )
    if allocation is None:
        # Asking again after an answer was lost is not a second release: the
        # pause this counter is already in, over this very allocation, is the
        # answer the first request would have given.
        repeat = (
            TillPause.objects.select_related("allocation")
            .filter(till=till, resumed_at__isnull=True, allocation__version=version)
            .first()
        )
        if repeat is not None:
            return repeat.allocation, repeat
        raise TillError(
            "NOT_FOUND",
            f"Counter {till.counter_id} is not holding an unreleased allocation at "
            f"version {version}.",
            404,
        )
    if not reason.strip():
        raise TillError("VALIDATION", "Say why this allocation is being released.", 400)
    if previous_financial_year(fy) > financial_year():
        # Head office opens next year's series before 1 April, so a counter can be
        # a year ahead of it for a few minutes - never two.
        raise TillError(
            "VALIDATION",
            f"The counter says it is billing in {fy}, which has not begun. Check its date.",
            400,
        )
    for year in (previous_financial_year(fy), fy):
        state = register_state(store, year)  # ends on the counter's own year
        if state.hole_count:
            missing = ", ".join(str(seq) for seq in state.holes[:10]) or "some"
            raise TillError(
                "UNSYNCED_BILLS",
                f"{state.hole_count} bill(s) this counter numbered in {year} have not "
                f"arrived ({missing}). They sync or are keyed in from paper before the "
                "stock is let go.",
                409,
            )
    landed = Sale.objects.filter(store=store, doc_number__isnull=False)
    ahead = (
        landed.filter(models.Q(fy=fy, till_seq__gte=next_seq) | models.Q(fy__gt=fy))
        .order_by("-fy", "-till_seq")
        .first()
    )
    if ahead is not None:
        raise TillError(
            "TILL_OUT_OF_STEP",
            f"Head office already holds bill {ahead.till_seq} of {ahead.fy} from this "
            f"counter, but the counter says its next bill is {next_seq} of {fy}. Sync "
            "the counter before releasing its stock.",
            409,
        )
    if state.last_accepted_seq < next_seq - 1:
        first = state.last_accepted_seq + 1
        raise TillError(
            "UNSYNCED_BILLS",
            f"The counter has numbered up to bill {next_seq - 1} of {fy}, but head office "
            f"has only up to {state.last_accepted_seq}. Bill {first} onwards sync first.",
            409,
        )
    allocation.released_at = timezone.now()
    allocation.released_by = actor
    allocation.release_reason = reason.strip()
    allocation.save(update_fields=["released_at", "released_by", "release_reason"])
    pause = TillPause.objects.create(
        till=till,
        allocation=allocation,
        fy=fy,
        next_seq=next_seq,
        paused_by=actor,
        reason=reason.strip(),
    )
    return allocation, pause


def live_pause(till: RegisteredTill | None) -> TillPause | None:
    """The transfer pause this counter is in, if it is in one."""
    if till is None:
        return None
    return TillPause.objects.filter(till=till, resumed_at__isnull=True).first()


def snapshot_pause_evidence(run: Any, site: Store) -> dict[str, Any]:
    """Pin a reconciled sole online counter for a trading stock snapshot.

    The caller first holds SiteGuard at rank SITE. This contract is deliberately
    narrower than offline cutover: historical/unknown devices, allocations and
    unresolved numbered work cannot be called synced by a boolean. Unused
    current online invoice reservations stay reserved and are pinned; none is
    silently cancelled or reissued by counting stock.
    """
    from core.canonical import content_hash, normalise
    from core.commands import LockRank
    from core.refusals import Refusal
    from masters.document_series import render
    from masters.document_series_models import DocumentSeries, DocumentSeriesCounter, IssuedDocumentNumber
    from sell.models import HeldBill, OnlineSaleSubmission, TillNumberBlock
    from sell.services.invoice_numbers import month_of, next_month

    def refuse(code: str, message: str) -> NoReturn:
        raise Refusal(code, message, status=409)

    if run.tenant_id != site.tenant_id:
        refuse("SCOPE_DENIED", "That inventory pause belongs to another tenant.")
    guard = SiteGuard.objects.filter(tenant_id=run.tenant_id, site=site).first()
    if guard is None or guard.selling_mode != SiteGuard.SellingMode.ONLINE_ALPHA:
        refuse("ONLINE_STORE_REQUIRED", "Trading snapshot reconciliation supports a proven online counter only.")
    tills = run.lock(LockRank.DOCUMENT, RegisteredTill.objects.filter(store=site))
    active = [till for till in tills if till.active]
    if len(active) != 1:
        refuse("TILL_RECONCILIATION_REQUIRED", "Reconcile the store's exact registered counter before this snapshot.")
    till = active[0]
    if TillAllocation.objects.filter(till__in=tills, released_at__isnull=True).exists():
        refuse("TILL_RECONCILIATION_REQUIRED", "An active or retired counter still holds unreleased stock.")
    pauses = run.lock(LockRank.DOCUMENT, TillPause.objects.filter(till=till, resumed_at__isnull=True))
    if len(pauses) != 1:
        refuse("TILL_PAUSE_REQUIRED", "Pause and reconcile the registered counter before reviewing this snapshot.")
    pause = pauses[0]
    if pause.allocation.released_at is None or pause.next_seq < 1 or pause.fy != financial_year():
        refuse("TILL_RECONCILIATION_REQUIRED", "The persisted pause has no exact current-year released frontier.")
    if HeldBill.objects.filter(store=site).exists() or Sale.objects.filter(store=site, doc_number__isnull=True).exists():
        refuse("TILL_RECONCILIATION_REQUIRED", "Resolve the store's held or unissued bills before this snapshot.")
    submissions = list(OnlineSaleSubmission.objects.filter(tenant_id=run.tenant_id, store=site).values(
        "id", "idempotency_uuid", "status", "sale_id", "payload_fingerprint",
    ).order_by("pk"))
    if any(row["status"] not in {"accepted", "rejected"} or (row["status"] == "accepted" and row["sale_id"] is None)
           or (row["status"] == "rejected" and row["sale_id"] is not None) for row in submissions):
        refuse("SALE_OUTCOME_REQUIRED", "Resolve every pending online sale outcome before this snapshot.")
    sales = list(Sale.objects.filter(store=site).values("id", "idempotency_uuid", "origin", "tax_invoice_number", "fy").order_by("pk"))
    known_sales = {(row["id"], str(row["idempotency_uuid"])) for row in sales}
    accepted_intents = {(row["sale_id"], str(row["idempotency_uuid"])) for row in submissions if row["status"] == "accepted"}
    if accepted_intents != known_sales or any(row["origin"] != "online" for row in sales):
        refuse("TILL_RECONCILIATION_REQUIRED", "This store has earlier offline or manual selling history requiring separate reconciliation.")
    accepted_numbers = {(str(row["id"]), row["tax_invoice_number"]) for row in sales}
    frontiers: list[dict[str, Any]] = []
    years = sorted(set(Sale.objects.filter(store=site).values_list("fy", flat=True)) | {previous_financial_year(pause.fy), pause.fy})
    for year in years:
        state = register_state(site, year)
        if state.hole_count or (year == pause.fy and state.last_accepted_seq + 1 != pause.next_seq) or year > pause.fy:
            refuse("UNSYNCED_BILLS", "The persisted pause does not match every arrived bill number.")
        frontiers.append({"fy": year, "last_accepted_seq": state.last_accepted_seq, "hole_count": state.hole_count})
    blocks = run.lock(LockRank.DOCUMENT, TillNumberBlock.objects.filter(till__in=tills, closed_at__isnull=True))
    permitted_months = {month_of(timezone.localdate(run.now)), next_month(month_of(timezone.localdate(run.now)))}
    block_evidence: list[dict[str, Any]] = []
    ranges_by_series: dict[tuple[int, str], list[tuple[int, int]]] = {}
    for block in blocks:
        prefix = block.prefix
        if (block.till_id != till.pk or prefix.tenant_id != run.tenant_id or prefix.site_id != site.pk
                or block.prefix_code != prefix.code or block.month not in permitted_months
                or block.fy != financial_year(block.month) or block.cancelled_count):
            refuse("NUMBER_RECONCILIATION_REQUIRED", "A retired or unresolved invoice-number block prevents this snapshot.")
        key = (prefix.pk, block.fy)
        previous = ranges_by_series.setdefault(key, [])
        if any(block.first_n <= upper and lower <= block.last_n for lower, upper in previous):
            refuse("NUMBER_RECONCILIATION_REQUIRED", "Invoice-number reservations overlap and require reconciliation.")
        previous.append((block.first_n, block.last_n))
        counter = DocumentSeriesCounter.objects.filter(tenant_id=run.tenant_id, prefix=prefix,
            series=DocumentSeries.TAX_INVOICE, fy=block.fy).first()
        if counter is None or counter.next_n <= block.last_n:
            refuse("NUMBER_RECONCILIATION_REQUIRED", "The retained invoice series does not own this block's complete range.")
        used = run.lock(LockRank.DOCUMENT, IssuedDocumentNumber.objects.filter(tenant_id=run.tenant_id,
            prefix=prefix, series=DocumentSeries.TAX_INVOICE, fy=block.fy, n__gte=block.first_n, n__lte=block.last_n))
        rows: list[dict[str, Any]] = []
        for number in used:
            if (number.status != IssuedDocumentNumber.Status.ISSUED or number.document_type != "sale"
                    or number.number != render(DocumentSeries.TAX_INVOICE, prefix.code, block.fy, number.n)
                    or (number.document_ref, number.number) not in accepted_numbers):
                refuse("NUMBER_RECONCILIATION_REQUIRED", "An invoice number lacks its exact accepted online bill.")
            rows.append({"id": str(number.pk), "number": number.number, "sale_id": number.document_ref})
        block_evidence.append({"id": block.pk, "till_id": till.pk, "prefix_id": prefix.pk,
            "prefix": prefix.code, "fy": block.fy, "month": block.month.isoformat(),
            "first": block.first_n, "last": block.last_n, "reserved_next_n": counter.next_n, "used": rows})
    result: dict[str, Any] = {"till_id": till.pk, "pause_id": pause.pk, "paused_by_id": pause.paused_by_id,
        "paused_at": pause.paused_at.isoformat(), "fy": pause.fy, "next_seq": pause.next_seq,
        "allocation_id": pause.allocation_id, "allocation_version": pause.allocation.version,
        "frontiers": frontiers, "open_number_blocks": block_evidence,
        # New definitive refusals while frozen consume no bill/stock frontier.
        # Keep those durable receipts, but do not stale a physical snapshot for
        # an attempted sale that this very freeze refused. Every recheck above
        # still independently refuses any pending or unknown outcome.
        "online_outcomes_hash": content_hash([row for row in submissions if row["status"] == "accepted"])}
    result["hash"] = content_hash(result)
    return dict(normalise(result))


@transaction.atomic
def resume_till(
    store: Store, actor: Any, fy: str | None = None, next_seq: int | None = None
) -> TillPause | None:
    """End the counter's transfer pause. Answers the pause it ended, or None.

    Ending the pause here does **not** put the counter back to work: the till
    stays shut until it has taken a fresh dataset, which is also the sync that
    protects its new shelf again. Resuming while a transfer out of the store is
    still waiting is allowed (Anand, 25 September 2026) - the fresh hold then
    stops that approval until the store pauses again, which the screen warns of.

    `(fy, next_seq)` is where the counter would bill next as it resumes; the
    window ends there, and never before where it began. Resuming a counter that
    is not paused changes nothing, so a retry after a lost answer is harmless.
    """
    lock_site_for_till(store)
    till = RegisteredTill.objects.select_for_update().filter(store=store, active=True).first()
    if till is None:
        raise TillError("TILL_NOT_REGISTERED", f"No counter is registered for {store.code}.", 404)
    pause = TillPause.objects.select_for_update().filter(till=till, resumed_at__isnull=True).first()
    if pause is None:
        return None
    start = (pause.fy, pause.next_seq)
    pause.resumed_at = timezone.now()
    pause.resumed_by = actor
    pause.resumed_fy, pause.resumed_next_seq = (
        max(start, (fy, next_seq)) if fy is not None and next_seq is not None else start
    )
    pause.save(update_fields=["resumed_at", "resumed_by", "resumed_fy", "resumed_next_seq"])
    return pause


def billed_while_paused(store_id: int, fy: str, till_seq: int) -> TillPause | None:
    """The pause a bill at this position was billed inside, if any (§10.2).

    By position in the bill series rather than by the till's clock: the series is
    the one thing the counter cannot print out of order, and its clock is a
    setting. A position is the year, then the number, so a window that opened on
    bill 3 of one year and closed on bill 2 of the next holds bill 1 of the next.
    """
    started = models.Q(fy__lt=fy) | models.Q(fy=fy, next_seq__lte=till_seq)
    not_ended = (
        models.Q(resumed_at__isnull=True)
        | models.Q(resumed_fy__gt=fy)
        | models.Q(resumed_fy=fy, resumed_next_seq__gt=till_seq)
    )
    return TillPause.objects.filter(started & not_ended, till__store_id=store_id).first()


def refuse_if_allocated(site_id: int) -> None:
    """Stop a reservation that would take a till's protected stock away (§10.2).

    Called from the transfer approval path, which is where stock at a store stops
    being sellable. Deliberately the *whole* site rather than the exact pieces: the
    allocation protects a snapshot, and the honest reading of "some of this may
    already be sold on a device we have not heard from" is that the store's stock
    is not free to promise elsewhere until the counter has reconciled.
    """
    from core.refusals import Refusal

    allocation = store_allocation(site_id)
    if allocation is None:
        return
    raise Refusal(
        "ALLOCATED_TO_TILL",
        f"{allocation.till.store.code}'s counter is holding this stock offline "
        f"(working set {allocation.version}). Release the counter's allocation after it "
        "has reconciled, then approve this transfer.",
    )

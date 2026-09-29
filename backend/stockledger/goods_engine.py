"""The goods-v1 operational inventory engine (design §3.1-§3.3, §7).

The only writer of custody positions, coverage, holds and reservations. Every
change is expressed as paired journal legs and goes through
``core.posting.post_entries(ledger="operational_inventory")``; callers never touch
the projections directly.

Vocabulary used below:

* **portion** - ``(lower, upper)``, a half-open range of one custody lot.
* **address** - where a portion is and what is true about it there: site,
  location, boundary, transfer, condition, SKU, origin, value basis, acceptance.
* **eligible** - good, officially valued, physically accepted, in an allowed
  location, not held, not reserved, at a goods-ready site that is not frozen.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.goods_fields import bounds, portion
from core.operational import (
    DISPOSAL_POSTING,
    SALE_POSTING,
    WRITE_OFF_POSTING,
    EncumbrancePair,
    OperationalBatch,
    QuantityPair,
    ValuePair,
)
from core.posting import OPERATIONAL_LEDGER, post_entries, register_operational_adapter
from core.refusals import Refusal
from stockledger import ranges
from stockledger.goods_models import (
    ActiveHold,
    ActiveReservation,
    AllocationGuard,
    CoverageEvent,
    CustodyLot,
    EncumbranceLeg,
    HoldEvent,
    JournalBatch,
    LiveCoverage,
    LiveValueBasis,
    Origin,
    Position,
    QuantityLeg,
    ReservationEvent,
    ValueLeg,
)

#: Location kinds where store stock is available to sell.
SELLING_KINDS = frozenset({"floor", "backstore"})
#: Location kinds from which accepted stock may be allocated to a transfer.
TRANSFERABLE_KINDS = frozenset({"floor", "backstore", "bin", "zone", "fixture"})
#: Location kinds that hold unaccepted custody; a P06 reversal leaves portions there.
HELD_KINDS = frozenset({"receiving", "quarantine", "excess_hold", "rtv_hold"})

ELIGIBILITY_REASONS = (
    "IDENTITY_UNRESOLVED",
    "VALUE_MISSING",
    "NOT_OFFICIAL",
    "NOT_ACCEPTED",
    "CONDITION_NOT_GOOD",
    "LOCATION_NOT_ELIGIBLE",
    "HOLD_ACTIVE",
    "RESERVED",
    "SITE_NOT_GOODS_READY",
    "COUNT_FROZEN",
    "IN_TRANSIT",
    "WRONG_SITE",
    "OUTSIDE_EFFECTIVE_PERIOD",
)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Address:
    boundary: str
    site_id: int | None = None
    location_id: uuid.UUID | None = None
    transfer_id: uuid.UUID | None = None
    condition: str = "good"
    sku_id: uuid.UUID | None = None
    origin_id: uuid.UUID | None = None
    value_basis_origin_id: uuid.UUID | None = None
    accepted_event_id: uuid.UUID | None = None
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            k: (str(v) if isinstance(v, uuid.UUID) else v)
            for k, v in asdict(self).items()
            if v is not None
        }

    @classmethod
    def of(cls, position: Position) -> Address:
        return cls(
            boundary=position.boundary,
            site_id=position.site_id,
            location_id=position.location_id,
            transfer_id=position.transfer_id,
            condition=position.condition,
            sku_id=position.sku_id,
            origin_id=position.origin_id,
            value_basis_origin_id=position.value_basis_origin_id,
            accepted_event_id=position.accepted_event_id,
        )


def external(reason: str) -> Address:
    return Address(boundary="external", condition="good", reason=reason)


# ---------------------------------------------------------------------------
# Posting plan: what one command changes, collected then written once
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    """The paired legs of one business event, built while the projections change."""

    posting_kind: str
    version_id: uuid.UUID
    event_key: uuid.UUID
    quantity: list[QuantityPair] = field(default_factory=list)
    value: list[ValuePair] = field(default_factory=list)
    encumbrance: list[EncumbrancePair] = field(default_factory=list)

    def batch(self) -> OperationalBatch:
        return OperationalBatch(
            event_key=self.event_key,
            posting_kind=self.posting_kind,
            version_id=self.version_id,
            quantity_pairs=tuple(self.quantity),
            value_pairs=tuple(self.value),
            encumbrance_pairs=tuple(self.encumbrance),
        )

    def empty(self) -> bool:
        return not (self.quantity or self.value or self.encumbrance)


def post(run: CommandRun, version: Any, plan: Plan) -> uuid.UUID | None:
    """Post ``plan`` through the kernel; returns the journal batch id (None if nothing moved).

    The id is read back from this command's own evidence (or the committed journal,
    for a replayed event key); the module keeps no per-command state.
    """
    if plan.empty():
        return None
    post_entries(version, ledger=OPERATIONAL_LEDGER, batch=plan.batch(), run=run)
    # Every till whose store this posting touched now holds a stale shelf. Raised
    # here rather than by each service, because this is the one door every goods
    # stock posting goes through and a service that had to remember would one day
    # not (OPS-07).
    from sell.services.working_set import bump_for_posting

    bump_for_posting(plan.quantity)
    batch = _journal_batch(run, plan.event_key)
    return uuid.UUID(str(batch.pk)) if batch is not None else None


def _journal_batch(run: CommandRun, key: uuid.UUID) -> JournalBatch | None:
    """The batch already written for ``key``: committed, or queued by this command."""
    existing = JournalBatch.objects.filter(tenant_id=run.tenant_id, event_key=key).first()
    if existing is not None:
        return existing
    for row in run.evidence.pending:
        if isinstance(row, JournalBatch) and row.event_key == key:
            return row
    return None


class StockLedgerAdapter:
    """Domain validation and journal append for ``post_entries`` (registered at start-up)."""

    def write(self, run: CommandRun | None, doc: Any, batch: OperationalBatch) -> uuid.UUID:
        if run is None:
            raise Refusal("POSTING_EVENT_CONFLICT", "An operational posting runs inside a command.")
        digest = batch.content_hash()
        prior = _journal_batch(run, batch.event_key)
        if prior is not None:
            if prior.content_hash != digest:
                raise Refusal(
                    "POSTING_EVENT_CONFLICT",
                    "This business event was already posted with different content.",
                )
            return uuid.UUID(str(prior.pk))
        for pair in batch.quantity_pairs:
            if pair.source.get("boundary") == "physical" and not pair.source.get("site_id"):
                raise Refusal("POSTING_EVENT_CONFLICT", "A physical source leg needs its site.")
        journal = JournalBatch(
            version_id=doc.pk,
            event_key=batch.event_key,
            posting_kind=batch.posting_kind,
            content_hash=digest,
        )
        run.record(journal)
        for pair in batch.quantity_pairs:
            for side, sign, address in (
                ("source", -1, pair.source),
                ("destination", 1, pair.destination),
            ):
                run.record(
                    QuantityLeg(
                        batch_id=journal.pk,
                        pair_key=pair.pair_key,
                        side=side,
                        lot_id=pair.lot_id,
                        portion=portion(pair.lower, pair.upper),
                        position=address,
                        qty=sign * pair.qty,
                    )
                )
        for vpair in batch.value_pairs:
            for side, sign, bucket, site in (
                ("source", -1, vpair.source_bucket, vpair.source_site_id),
                ("destination", 1, vpair.destination_bucket, vpair.destination_site_id),
            ):
                run.record(
                    ValueLeg(
                        batch_id=journal.pk,
                        pair_key=vpair.pair_key,
                        side=side,
                        origin_id=vpair.origin_id,
                        lot_id=vpair.lot_id,
                        portion=portion(vpair.lower, vpair.upper)
                        if vpair.lower is not None and vpair.upper is not None
                        else None,
                        bucket=bucket,
                        leg_site_id=site,
                        amount=sign * vpair.amount,
                    )
                )
        for epair in batch.encumbrance_pairs:
            for side, sign, gate in (
                ("source", -1, epair.source_gate),
                ("destination", 1, epair.destination_gate),
            ):
                run.record(
                    EncumbranceLeg(
                        batch_id=journal.pk,
                        pair_key=epair.pair_key,
                        side=side,
                        axis=epair.axis,
                        control_key=epair.control_key,
                        lot_id=epair.lot_id,
                        portion=portion(epair.lower, epair.upper),
                        gate=gate,
                        qty=sign * epair.qty,
                    )
                )
        return uuid.UUID(str(journal.pk))


def install() -> None:
    register_operational_adapter(StockLedgerAdapter())


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def lock_lots(run: CommandRun, lot_ids: Iterable[uuid.UUID]) -> None:
    """Rank LOT: custody lots are append-only, so they are locked by advisory key."""
    run.advisory_lock(
        LockRank.LOT, [f"lot:{lot_id}" for lot_id in sorted({str(i) for i in lot_ids})]
    )


def lock_allocation(run: CommandRun, site_id: int, sku_ids: Iterable[uuid.UUID]) -> None:
    keys = sorted({uuid.UUID(str(s)) for s in sku_ids}, key=str)
    for sku_id in keys:
        AllocationGuard.objects.get_or_create(
            tenant_id=run.tenant_id, site_id=site_id, sku_id=sku_id
        )
    run.lock(
        LockRank.ALLOCATION,
        AllocationGuard.objects.filter(tenant_id=run.tenant_id, site_id=site_id, sku_id__in=keys),
    )


# ---------------------------------------------------------------------------
# Lots and positions
# ---------------------------------------------------------------------------


def open_lot(
    run: CommandRun,
    plan: Plan,
    *,
    source_kind: str,
    site_id: int,
    qty: int,
    identity: dict[str, Any],
    source_time: datetime,
    address: Address,
    source_line_id: uuid.UUID | None = None,
    source_manifest_row_id: uuid.UUID | None = None,
    adjustment_line_id: uuid.UUID | None = None,
    source_observation_id: uuid.UUID | None = None,
    parent_lot_id: uuid.UUID | None = None,
    from_boundary: str = "receipt",
) -> CustodyLot:
    """A new counted lot and its first position, posted from an external boundary."""
    if not 1 <= qty <= 999_999:
        raise Refusal("INVALID_REQUEST", "A lot holds 1 to 999,999 pieces.")
    lot = CustodyLot(
        source_kind=source_kind,
        source_line_id=source_line_id,
        source_manifest_row_id=source_manifest_row_id,
        adjustment_line_id=adjustment_line_id,
        source_observation_id=source_observation_id,
        parent_lot_id=parent_lot_id,
        issued_qty=qty,
        initial_site_id=site_id,
        initial_identity=identity,
        source_time=source_time,
    )
    run.record(lot)
    Position.objects.create(
        tenant_id=run.tenant_id,
        lot_id=lot.pk,
        portion=portion(0, qty),
        description=str(identity.get("description") or "")[:240],
        **_address_columns(address),
    )
    plan.quantity.append(
        QuantityPair(
            lot_id=lot.pk,
            lower=0,
            upper=qty,
            source=external(from_boundary).as_json(),
            destination=address.as_json(),
        )
    )
    return lot


def _address_columns(address: Address) -> dict[str, Any]:
    return {
        "site_id": address.site_id,
        "location_id": address.location_id,
        "boundary": address.boundary,
        "transfer_id": address.transfer_id,
        "condition": address.condition,
        "sku_id": address.sku_id,
        "origin_id": address.origin_id,
        "value_basis_origin_id": address.value_basis_origin_id,
        "accepted_event_id": address.accepted_event_id,
    }


def positions_of(lot_id: uuid.UUID, interval: ranges.Interval | None = None) -> list[Position]:
    queryset = Position.objects.filter(lot_id=lot_id)
    if interval is not None:
        queryset = queryset.filter(portion__overlap=portion(*interval))
    return sorted(queryset, key=lambda p: bounds(p.portion)[0])


def change_address(
    run: CommandRun,
    plan: Plan,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    change: Any,
    *,
    require_same: bool = True,
) -> list[tuple[ranges.Interval, Address, Address]]:
    """Give ``interval`` of a lot a new address, splitting positions at its edges.

    ``change`` maps an old address to the new one (so a move keeps the origin and
    condition it did not change). Every moved slice is one quantity pair. The
    interval must be wholly held by positions; nothing is invented.
    """
    held = positions_of(lot_id, interval)
    covered = ranges.intersect([bounds(p.portion) for p in held], [interval])
    if ranges.total(covered) != ranges.length(interval):
        raise Refusal(
            "ACCEPTANCE_INVALID", "Part of that quantity is not where the command expects it."
        )
    moved: list[tuple[ranges.Interval, Address, Address]] = []
    for position in held:
        current = bounds(position.portion)
        inside, outside = ranges.split(current, [interval])
        if not inside:
            continue
        old = Address.of(position)
        new = change(old)
        description = position.description
        position.delete()
        for lower, upper in outside:
            Position.objects.create(
                tenant_id=run.tenant_id,
                lot_id=lot_id,
                portion=portion(lower, upper),
                description=description,
                **_address_columns(old),
            )
        for lower, upper in inside:
            Position.objects.create(
                tenant_id=run.tenant_id,
                lot_id=lot_id,
                portion=portion(lower, upper),
                description=description,
                **_address_columns(new),
            )
            if new != old:
                plan.quantity.append(
                    QuantityPair(
                        lot_id=lot_id,
                        lower=lower,
                        upper=upper,
                        source=old.as_json(),
                        destination=new.as_json(),
                    )
                )
            moved.append(((lower, upper), old, new))
    return moved


def end_positions(
    run: CommandRun,
    plan: Plan,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    boundary: str,
    reason: str,
) -> list[tuple[ranges.Interval, Address]]:
    """Move a portion to a non-physical boundary (disposed, returned, consumed, external)."""
    moved = change_address(
        run,
        plan,
        lot_id,
        interval,
        lambda old: replace(
            old,
            boundary=boundary,
            site_id=None,
            location_id=None,
            transfer_id=None,
            accepted_event_id=None,
            reason=reason,
        ),
    )
    return [(slice_, old) for slice_, old, _new in moved]


# ---------------------------------------------------------------------------
# Coverage and value
# ---------------------------------------------------------------------------


def cover(
    run: CommandRun,
    plan: Plan,
    *,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    origin: Origin,
    pt_version_id: uuid.UUID,
    pt_line_id: uuid.UUID,
    site_id: int,
) -> CoverageEvent:
    """P04/P05 coverage: an unvalued custody portion becomes valued by ``origin``."""
    existing = LiveCoverage.objects.filter(
        lot_id=lot_id, portion__overlap=portion(*interval)
    ).exists()
    if existing:
        raise Refusal("COVERAGE_CONFLICT", "Part of this quantity is already covered by a live PT.")
    event = CoverageEvent(
        pt_version_id=pt_version_id,
        pt_line_id=pt_line_id,
        lot_id=lot_id,
        site_id=site_id,
        portion=portion(*interval),
        origin_id=origin.pk,
        effect=CoverageEvent.Effect.COVER,
    )
    run.record(event)
    LiveCoverage.objects.create(
        tenant_id=run.tenant_id,
        lot_id=lot_id,
        portion=portion(*interval),
        cover_event_id=event.pk,
        origin_id=origin.pk,
    )

    def valued(old: Address) -> Address:
        # The deepest guard: ordinary coverage values only good goods of the origin's own
        # SKU - never damaged goods, even ones a reversal uncovered (change PRD §14.5 J5:
        # damaged value uses only value_damage), and never goods relabelled as another SKU.
        if old.sku_id != origin.sku_id or old.condition != "good":
            raise Refusal(
                "COVERAGE_CONFLICT",
                "Only good goods counted as the line's own SKU can be covered.",
            )
        return replace(old, origin_id=origin.pk, value_basis_origin_id=None)

    change_address(run, plan, lot_id, interval, valued)
    plan.value.append(
        ValuePair(
            origin_id=origin.pk,
            amount=ranges.length(interval) * int(origin.unit_cost),
            source_bucket="origin_evidence",
            destination_bucket="stock",
            source_site_id=None,
            destination_site_id=site_id,
            lot_id=lot_id,
            lower=interval[0],
            upper=interval[1],
        )
    )
    return event


def counter_coverage(run: CommandRun, plan: Plan, *, pt_version_id: uuid.UUID) -> int:
    """P06: remove every live cover of a reversed version, keeping physical custody.

    The countered portions become unvalued, unaccepted custody in the site's held
    locations (design §7.2 P06): a portion already in receiving or a quarantine-kind
    location stays where it is; one that was put away goes back to receiving when
    good, or to quarantine otherwise. Independent holds are untouched.
    """
    covers = list(
        CoverageEvent.objects.filter(
            pt_version_id=pt_version_id, effect=CoverageEvent.Effect.COVER
        ).select_related("origin")
    )
    kinds: dict[uuid.UUID, str] = {}
    held: dict[tuple[int, str], uuid.UUID] = {}

    def to_custody(old: Address) -> Address:
        location = old.location_id
        if old.boundary == "physical" and old.site_id is not None:
            if location is not None and location not in kinds:
                kinds.update(location_kinds([location]))
            if location is None or kinds.get(location) not in HELD_KINDS:
                target = "receiving" if old.condition == "good" else "quarantine"
                if (old.site_id, target) not in held:
                    held[(old.site_id, target)] = system_location(old.site_id, target).pk
                location = held[(old.site_id, target)]
        return replace(old, origin_id=None, accepted_event_id=None, location_id=location)

    countered = 0
    for event in covers:
        interval = bounds(event.portion)
        live = LiveCoverage.objects.filter(cover_event_id=event.pk)
        if not live.exists():
            continue
        live.delete()
        run.record(
            CoverageEvent(
                pt_version_id=pt_version_id,
                pt_line_id=event.pt_line_id,
                lot_id=event.lot_id,
                site_id=event.site_id,
                portion=event.portion,
                origin_id=event.origin_id,
                effect=CoverageEvent.Effect.COUNTER,
                counter_of_id=event.pk,
            )
        )
        change_address(run, plan, event.lot_id, interval, to_custody)
        plan.value.append(
            ValuePair(
                origin_id=event.origin_id,
                amount=ranges.length(interval) * int(event.origin.unit_cost),
                source_bucket="stock",
                destination_bucket="origin_evidence",
                source_site_id=event.site_id,
                destination_site_id=None,
                lot_id=event.lot_id,
                lower=interval[0],
                upper=interval[1],
            )
        )
        countered += 1
    return countered


# ---------------------------------------------------------------------------
# Holds and reservations
# ---------------------------------------------------------------------------


def place_hold(
    run: CommandRun,
    plan: Plan,
    *,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    hold_key: uuid.UUID,
    kind: str,
    site_id: int,
    source_version_id: uuid.UUID | None,
) -> HoldEvent:
    event = HoldEvent(
        lot_id=lot_id,
        site_id=site_id,
        portion=portion(*interval),
        hold_key=hold_key,
        kind=kind[:60],
        effect=HoldEvent.Effect.PLACE,
        source_version_id=source_version_id,
        journal_batch_id=None,
    )
    run.record(event)
    ActiveHold.objects.create(
        tenant_id=run.tenant_id,
        lot_id=lot_id,
        portion=portion(*interval),
        hold_key=hold_key,
        kind=kind[:60],
        event_id=event.pk,
    )
    plan.encumbrance.append(
        EncumbrancePair(
            axis="hold",
            control_key=hold_key,
            lot_id=lot_id,
            lower=interval[0],
            upper=interval[1],
            source_gate="inactive",
            destination_gate="active",
        )
    )
    return event


def release_hold(
    run: CommandRun,
    plan: Plan,
    *,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    hold_key: uuid.UUID,
    site_id: int,
    source_version_id: uuid.UUID | None,
) -> None:
    active = list(
        ActiveHold.objects.filter(
            lot_id=lot_id, hold_key=hold_key, portion__overlap=portion(*interval)
        )
    )
    held = ranges.normalise([bounds(h.portion) for h in active])
    if not ranges.contains(held, [interval]):
        raise Refusal("HOLD_NOT_ACTIVE", "That quantity is not under this hold.")
    kind = active[0].kind if active else "hold"
    for hold in active:
        remaining = ranges.subtract([bounds(hold.portion)], [interval])
        hold.delete()
        for lower, upper in remaining:
            ActiveHold.objects.create(
                tenant_id=run.tenant_id,
                lot_id=lot_id,
                portion=portion(lower, upper),
                hold_key=hold_key,
                kind=hold.kind,
                event_id=hold.event_id,
            )
    run.record(
        HoldEvent(
            lot_id=lot_id,
            site_id=site_id,
            portion=portion(*interval),
            hold_key=hold_key,
            kind=kind,
            effect=HoldEvent.Effect.RELEASE,
            source_version_id=source_version_id,
        )
    )
    plan.encumbrance.append(
        EncumbrancePair(
            axis="hold",
            control_key=hold_key,
            lot_id=lot_id,
            lower=interval[0],
            upper=interval[1],
            source_gate="active",
            destination_gate="inactive",
        )
    )


def reserve(
    run: CommandRun,
    plan: Plan,
    *,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    transfer_version_id: uuid.UUID,
    site_id: int,
) -> ReservationEvent:
    if ActiveReservation.objects.filter(
        lot_id=lot_id, portion__overlap=portion(*interval)
    ).exists():
        raise Refusal("INSUFFICIENT_ELIGIBLE_STOCK", "Part of that stock is already reserved.")
    event = ReservationEvent(
        lot_id=lot_id,
        site_id=site_id,
        portion=portion(*interval),
        transfer_version_id=transfer_version_id,
        effect=ReservationEvent.Effect.RESERVE,
    )
    run.record(event)
    ActiveReservation.objects.create(
        tenant_id=run.tenant_id,
        lot_id=lot_id,
        portion=portion(*interval),
        event_id=event.pk,
        transfer_version_id=transfer_version_id,
    )
    plan.encumbrance.append(
        EncumbrancePair(
            axis="reservation",
            control_key=transfer_version_id,
            lot_id=lot_id,
            lower=interval[0],
            upper=interval[1],
            source_gate="inactive",
            destination_gate="active",
        )
    )
    return event


def end_reservations(
    run: CommandRun, plan: Plan, *, transfer_version_id: uuid.UUID, effect: str, site_id: int
) -> list[tuple[uuid.UUID, ranges.Interval]]:
    """Release (cancel) or consume (dispatch) every active reservation of a transfer version."""
    ended: list[tuple[uuid.UUID, ranges.Interval]] = []
    for reservation in ActiveReservation.objects.filter(
        transfer_version_id=transfer_version_id
    ).order_by("lot_id"):
        interval = bounds(reservation.portion)
        reservation_event_id = reservation.event_id
        lot_id = reservation.lot_id
        reservation.delete()
        _record_reservation_end(
            run,
            plan,
            lot_id=lot_id,
            interval=interval,
            transfer_version_id=transfer_version_id,
            effect=effect,
            site_id=site_id,
            prior_event_id=reservation_event_id,
        )
        ended.append((lot_id, interval))
    return ended


def end_reservation(
    run: CommandRun,
    plan: Plan,
    *,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    transfer_version_id: uuid.UUID,
    effect: str,
    site_id: int,
) -> None:
    """End exactly ``interval`` of one transfer version's reservation, leaving the rest.

    A transfer may be fulfilled by several dispatches, so the reservation a
    departure consumes is the part that actually left - never the whole approved
    balance (transfers PRD §3: "a smaller dispatch leaves the remaining quantity
    reserved"). Whatever of the reserved row falls outside ``interval`` is
    written back under the same reservation event, so the balance keeps its
    history rather than being re-reserved as something new.
    """
    active = list(
        ActiveReservation.objects.filter(
            lot_id=lot_id,
            transfer_version_id=transfer_version_id,
            portion__overlap=portion(*interval),
        )
    )
    reserved = ranges.normalise([bounds(row.portion) for row in active])
    if not ranges.contains(reserved, [interval]):
        raise Refusal(
            "RESERVATION_BLOCKED", "That quantity is not reserved for this transfer any more."
        )
    prior_event_id = active[0].event_id
    for row in active:
        remaining = ranges.subtract([bounds(row.portion)], [interval])
        event_id = row.event_id
        row.delete()
        for lower, upper in remaining:
            ActiveReservation.objects.create(
                tenant_id=run.tenant_id,
                lot_id=lot_id,
                portion=portion(lower, upper),
                event_id=event_id,
                transfer_version_id=transfer_version_id,
            )
    _record_reservation_end(
        run,
        plan,
        lot_id=lot_id,
        interval=interval,
        transfer_version_id=transfer_version_id,
        effect=effect,
        site_id=site_id,
        prior_event_id=prior_event_id,
    )


def _record_reservation_end(
    run: CommandRun,
    plan: Plan,
    *,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    transfer_version_id: uuid.UUID,
    effect: str,
    site_id: int,
    prior_event_id: uuid.UUID | None,
) -> None:
    run.record(
        ReservationEvent(
            lot_id=lot_id,
            site_id=site_id,
            portion=portion(*interval),
            transfer_version_id=transfer_version_id,
            effect=effect,
            prior_event_id=prior_event_id,
        )
    )
    plan.encumbrance.append(
        EncumbrancePair(
            axis="reservation",
            control_key=transfer_version_id,
            lot_id=lot_id,
            lower=interval[0],
            upper=interval[1],
            source_gate="active",
            destination_gate="inactive",
        )
    )


# ---------------------------------------------------------------------------
# Eligibility and availability
# ---------------------------------------------------------------------------


@dataclass
class Portion:
    lot_id: uuid.UUID
    interval: ranges.Interval
    address: Address
    location_kind: str | None
    source_time: datetime
    lineage_key: str
    unit_cost: int | None
    mrp: int | None
    description: str
    #: ``receipt``/``opening``/``value_damage`` from the valuing basis; ``None`` when unvalued.
    source_kind: str | None = None


def active_encumbrances(
    lot_ids: Sequence[uuid.UUID],
) -> tuple[dict[uuid.UUID, list[ranges.Interval]], dict[uuid.UUID, list[ranges.Interval]]]:
    """Live hold and reservation portions per lot, from the projections."""
    holds: dict[uuid.UUID, list[ranges.Interval]] = defaultdict(list)
    for hold in ActiveHold.objects.filter(lot_id__in=lot_ids):
        holds[hold.lot_id].append(bounds(hold.portion))
    reservations: dict[uuid.UUID, list[ranges.Interval]] = defaultdict(list)
    for reservation in ActiveReservation.objects.filter(lot_id__in=lot_ids):
        reservations[reservation.lot_id].append(bounds(reservation.portion))
    return holds, reservations


def site_frozen(site_id: int) -> bool:
    from masters.goods_models import SiteGuard

    guard = SiteGuard.objects.filter(site_id=site_id).first()
    return bool(guard and guard.freeze_id)


def physical_portions(site_id: int, *, sku_ids: Iterable[uuid.UUID] | None = None) -> list[Portion]:
    queryset = Position.objects.filter(site_id=site_id, boundary="physical")
    if sku_ids is not None:
        queryset = queryset.filter(sku_id__in=list(sku_ids))
    return portions_from(queryset)


def portions_from(queryset: Any) -> list[Portion]:
    """``Portion`` views of live ``Position`` rows (any boundary)."""
    out: list[Portion] = []
    positions = list(queryset.select_related("location", "origin", "value_basis_origin", "lot"))
    live: dict[uuid.UUID, list[tuple[ranges.Interval, Origin]]] = defaultdict(list)
    for row in LiveValueBasis.objects.filter(
        lot_id__in=[position.lot_id for position in positions]
    ).select_related("origin"):
        live[row.lot_id].append((bounds(row.portion), row.origin))

    def append(
        position: Position, interval: ranges.Interval, overlay: Origin | None = None
    ) -> None:
        basis = position.origin or position.value_basis_origin or overlay
        address = Address.of(position)
        if overlay is not None:
            address = replace(
                address,
                sku_id=address.sku_id or overlay.sku_id,
                value_basis_origin_id=overlay.pk,
            )
        source_time = position.origin.source_time if position.origin else position.lot.source_time
        lineage = str(position.origin.lineage_key) if position.origin else str(position.lot_id)
        out.append(
            Portion(
                lot_id=position.lot_id,
                interval=interval,
                address=address,
                location_kind=position.location.kind if position.location else None,
                source_time=source_time,
                lineage_key=lineage,
                unit_cost=int(basis.unit_cost) if basis else None,
                mrp=int(basis.mrp) if basis else None,
                description=position.description,
                source_kind=basis.source_kind if basis else None,
            )
        )

    for position in positions:
        interval = bounds(position.portion)
        overlays = (
            [
                (piece, origin)
                for assigned, origin in live.get(position.lot_id, [])
                for piece in ranges.intersect([interval], [assigned])
            ]
            if position.origin_id is None and position.value_basis_origin_id is None
            else []
        )
        for piece, origin in overlays:
            append(position, piece, origin)
        for piece in ranges.subtract([interval], [piece for piece, _origin in overlays]):
            append(position, piece)
    return out


def eligible_portions(
    site_id: int, sku_id: uuid.UUID, *, purpose: str = "transfer"
) -> list[Portion]:
    """Portions of one SKU at one site that may be reserved (transfer) or sold (sell)."""
    from masters.goods_models import SiteGuard

    guard = SiteGuard.objects.filter(site_id=site_id).first()
    if guard is None or not guard.goods_ready or guard.freeze_id:
        return []
    kinds = SELLING_KINDS if purpose == "sell" else TRANSFERABLE_KINDS
    candidates = [
        p
        for p in physical_portions(site_id, sku_ids=[sku_id])
        if p.address.condition == "good"
        and p.address.accepted_event_id is not None
        and (p.address.origin_id is not None or p.address.value_basis_origin_id is not None)
        and p.location_kind in kinds
    ]
    holds, reservations = active_encumbrances([p.lot_id for p in candidates])
    out: list[Portion] = []
    for candidate in candidates:
        free = ranges.subtract(
            [candidate.interval],
            [*holds.get(candidate.lot_id, []), *reservations.get(candidate.lot_id, [])],
        )
        for lower, upper in free:
            out.append(replace(candidate, interval=(lower, upper)))
    return out


def reasons_for(
    portion_: Portion,
    *,
    guard: Any,
    holds: list[ranges.Interval],
    reservations: list[ranges.Interval],
    purpose: str,
) -> list[str]:
    reasons: list[str] = []
    address = portion_.address
    if guard is None or not guard.goods_ready:
        reasons.append("SITE_NOT_GOODS_READY")
    if guard is not None and guard.freeze_id:
        reasons.append("COUNT_FROZEN")
    if address.sku_id is None:
        reasons.append("IDENTITY_UNRESOLVED")
    if address.origin_id is None and address.value_basis_origin_id is None:
        reasons.append("NOT_OFFICIAL")
        reasons.append("VALUE_MISSING")
    if address.accepted_event_id is None:
        reasons.append("NOT_ACCEPTED")
    if address.condition != "good":
        reasons.append("CONDITION_NOT_GOOD")
    kinds = SELLING_KINDS if purpose == "sell" else TRANSFERABLE_KINDS
    if portion_.location_kind not in kinds:
        reasons.append("LOCATION_NOT_ELIGIBLE")
    if ranges.intersect([portion_.interval], holds):
        reasons.append("HOLD_ACTIVE")
    if ranges.intersect([portion_.interval], reservations):
        reasons.append("RESERVED")
    return [r for r in ELIGIBILITY_REASONS if r in reasons]


def select_fifo(
    site_id: int,
    sku_id: uuid.UUID,
    quantity: int,
    *,
    origin_ids: Iterable[uuid.UUID] | None = None,
    purpose: str = "transfer",
) -> list[Portion]:
    """The oldest eligible pieces, by the origin's own time. ``purpose`` picks the question.

    A transfer may take accepted stock out of any storage location; a sale may only
    take it off the shop floor or out of the backstore. Same selection, same
    eligibility rules, one narrower set of locations - so a counter can never sell
    a piece standing in a bin nobody has put on the floor.
    """
    return _fifo(eligible_portions(site_id, sku_id, purpose=purpose), quantity, origin_ids)


def quarantined_portions(site_id: int, sku_id: uuid.UUID | None = None) -> list[Portion]:
    """Recorded stock standing in this site's quarantine under a hold, and reserved to nobody.

    The one source pool of the controlled custody transfer (overall PRD
    §15.2.1 rule 10, goods ticket 13D). *Recorded* means it has a resolved SKU
    and an origin or value basis, so a PT registered it; pre-PT custody has
    neither and is ticket 13E's. *Quarantined* means it stands in the site's
    quarantine location and a hold covers it - only the held part is offered.
    Acceptance and condition are not asked about: quarantined goods are, by
    definition, not the ordinary eligible stock ``eligible_portions`` answers,
    and nothing here makes them so. Reserved pieces are never offered twice.
    """
    from masters.goods_models import SiteGuard

    guard = SiteGuard.objects.filter(site_id=site_id).first()
    if guard is None or not guard.goods_ready or guard.freeze_id:
        return []
    candidates = [
        p
        for p in physical_portions(site_id, sku_ids=[sku_id] if sku_id is not None else None)
        if p.location_kind == "quarantine"
        and p.address.sku_id is not None
        and (p.address.origin_id is not None or p.address.value_basis_origin_id is not None)
    ]
    holds, reservations = active_encumbrances([p.lot_id for p in candidates])
    # Goods ticket 15C: a written-off piece's value has already left stock, and
    # moving it would carry that value again. It waits for its disposal route.
    gone = written_off([p.lot_id for p in candidates])
    out: list[Portion] = []
    for candidate in candidates:
        held = ranges.intersect([candidate.interval], holds.get(candidate.lot_id, []))
        blocked = [*reservations.get(candidate.lot_id, []), *gone.get(candidate.lot_id, [])]
        for lower, upper in ranges.subtract(held, blocked):
            out.append(replace(candidate, interval=(lower, upper)))
    return out


def select_quarantined(
    site_id: int,
    sku_id: uuid.UUID,
    quantity: int,
    *,
    origin_ids: Iterable[uuid.UUID] | None = None,
) -> list[Portion]:
    """The oldest quarantined recorded pieces, by the same FIFO rule ``select_fifo`` uses."""
    return _fifo(quarantined_portions(site_id, sku_id), quantity, origin_ids)


def _fifo(
    eligible: list[Portion], quantity: int, origin_ids: Iterable[uuid.UUID] | None
) -> list[Portion]:
    wanted = {str(o) for o in origin_ids} if origin_ids else None
    if wanted is not None:
        eligible = [p for p in eligible if str(p.address.origin_id) in wanted]
    candidates = [
        ranges.Candidate(
            p.source_time.timestamp(), p.lineage_key, str(p.lot_id), p.interval[0], p.interval[1]
        )
        for p in eligible
    ]
    try:
        chosen = ranges.fifo_select(candidates, quantity)
    except ValueError as exc:
        raise Refusal(
            "INSUFFICIENT_ELIGIBLE_STOCK",
            f"Only {ranges.total([p.interval for p in eligible])} eligible piece(s) are available.",
        ) from exc
    by_key = {(str(p.lot_id), p.interval[0]): p for p in eligible}
    out: list[Portion] = []
    for choice in chosen:
        base = next(
            p
            for (lot, lower), p in by_key.items()
            if lot == choice.lot_id and p.interval[0] <= choice.lower < p.interval[1]
        )
        out.append(replace(base, interval=(choice.lower, choice.upper)))
    return out


#: Money properties a ``StockDTO`` row may carry: omitted without the grant, and null
#: (unknown, never zero) when any of the row's pieces has no operational value.
COST_VALUE = "cost_value_paise"
TICKET_VALUE = "ticket_value_paise"


def site_purposes(site_types: Mapping[int, str]) -> dict[int, str]:
    """Which eligibility question a site answers: a warehouse transfers, a store sells."""
    return {
        site_id: "transfer" if kind in ("warehouse", "transfer") else "sell"
        for site_id, kind in site_types.items()
    }


def source_kind_label(item: Portion, address: Address) -> str:
    """The stock-row ``source_kind`` a valued position reports, or ``unvalued``."""
    if not (address.origin_id or address.value_basis_origin_id):
        return "unvalued"
    return item.source_kind or ("receipt" if address.origin_id else "adjustment")


def build_stock_rows(
    portions: Iterable[Portion],
    *,
    holds: Mapping[uuid.UUID, list[ranges.Interval]],
    reservations: Mapping[uuid.UUID, list[ranges.Interval]],
    guards: Mapping[int, Any],
    purposes: Mapping[int, str] | None = None,
    values: Iterable[str] = (),
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """``StockDTO`` rows grouped by site, location, SKU, origin and condition.

    The one aggregation behind every goods stock read. ``portions`` may come from
    the live projections or from a journal replay at a past watermark, so the
    quantity, eligibility and money rules cannot drift between the two. Holds and
    reservations are subtracted as one union, never twice.
    """
    wanted = set(values)
    stamp = (as_of or timezone.now()).isoformat()
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in portions:
        address = item.address
        site_id = address.site_id
        guard = guards.get(site_id) if site_id is not None else None
        purpose = (purposes or {}).get(site_id or 0, "sell")
        if guard is not None and guard.non_trading_confirmed:
            # Affirmed non-trading: its stock can move on, but nothing there is sell-ready.
            purpose = "transfer"
        key = (
            site_id,
            address.location_id,
            address.sku_id,
            address.origin_id or address.value_basis_origin_id,
            address.condition,
        )
        row = rows.setdefault(
            key,
            {
                "site_id": str(site_id) if site_id is not None else None,
                "location_id": str(address.location_id) if address.location_id else None,
                "sku_id": str(address.sku_id) if address.sku_id else None,
                "description": item.description,
                "origin_id": str(address.origin_id) if address.origin_id else None,
                "source_kind": source_kind_label(item, address),
                "condition": address.condition,
                "physical_qty": 0,
                "valued_qty": 0,
                "accepted_qty": 0,
                "held_qty": 0,
                "reserved_qty": 0,
                "ats_qty": 0,
                "transferable_qty": 0,
                "eligibility_reasons": [],
                "as_of": stamp,
                "record_contract": "goods-v1",
                "_cost": 0,
                "_ticket": 0,
            },
        )
        size = ranges.length(item.interval)
        lot_holds = holds.get(item.lot_id, [])
        lot_reservations = reservations.get(item.lot_id, [])
        held = ranges.total(ranges.intersect([item.interval], lot_holds))
        reserved = ranges.total(ranges.intersect([item.interval], lot_reservations))
        blocked = ranges.total(ranges.intersect([item.interval], [*lot_holds, *lot_reservations]))
        valued = address.origin_id is not None or address.value_basis_origin_id is not None
        row["physical_qty"] += size
        row["valued_qty"] += size if valued else 0
        row["accepted_qty"] += size if address.accepted_event_id else 0
        row["held_qty"] += held
        row["reserved_qty"] += reserved
        base_ok = (
            valued
            and address.sku_id is not None
            and address.accepted_event_id is not None
            and address.condition == "good"
            and guard is not None
            and guard.goods_ready
            and not guard.freeze_id
        )
        free = size - blocked
        # Only a selling site has ATS; a warehouse's availability is transferable only.
        if base_ok and purpose == "sell" and item.location_kind in SELLING_KINDS:
            row["ats_qty"] += free
        if base_ok and item.location_kind in TRANSFERABLE_KINDS:
            row["transferable_qty"] += free
        if item.unit_cost is not None:
            row["_cost"] += size * item.unit_cost
        if item.mrp is not None:
            row["_ticket"] += size * item.mrp
        reasons = reasons_for(
            item, guard=guard, holds=lot_holds, reservations=lot_reservations, purpose=purpose
        )
        row["eligibility_reasons"] = sorted(
            set(row["eligibility_reasons"]) | set(reasons), key=ELIGIBILITY_REASONS.index
        )
    out: list[dict[str, Any]] = []
    for _key, row in sorted(rows.items(), key=lambda kv: tuple(_sortable(v) for v in kv[0])):
        cost = row.pop("_cost")
        ticket = row.pop("_ticket")
        known = row["valued_qty"] == row["physical_qty"]
        if COST_VALUE in wanted:
            row[COST_VALUE] = str(cost) if known else None
        if TICKET_VALUE in wanted:
            row[TICKET_VALUE] = str(ticket) if known else None
        out.append(row)
    return out


def _sortable(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return f"{value:020d}"
    return str(value)


def stock_rows(
    site_ids: Iterable[int], *, show_value: bool, purpose_site_types: dict[int, str] | None = None
) -> list[dict[str, Any]]:
    """Live ``StockDTO`` rows for whole sites (delegates to ``build_stock_rows``)."""
    from masters.goods_models import SiteGuard

    sites = list(site_ids)
    guards = {g.site_id: g for g in SiteGuard.objects.filter(site_id__in=sites)}
    portions = [item for site_id in sites for item in physical_portions(site_id)]
    holds, reservations = active_encumbrances([p.lot_id for p in portions])
    return build_stock_rows(
        portions,
        holds=holds,
        reservations=reservations,
        guards=guards,
        purposes=site_purposes(purpose_site_types) if purpose_site_types is not None else None,
        values=(COST_VALUE, TICKET_VALUE) if show_value else (),
    )


def transit_rows(transfer_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    totals: dict[uuid.UUID, int] = defaultdict(int)
    for position in Position.objects.filter(boundary="transit", transfer_id__in=list(transfer_ids)):
        totals[position.transfer_id] += ranges.length(bounds(position.portion))  # type: ignore[index]
    return totals


def location_kinds(location_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    from masters.goods_models import Location

    return dict(Location.objects.filter(pk__in=list(location_ids)).values_list("pk", "kind"))


def system_location(site_id: int, kind: str) -> Any:
    from masters.goods_models import Location

    location = Location.objects.filter(site_id=site_id, kind=kind, system=True).first()
    if location is None:
        raise Refusal(
            "SITE_NOT_READY", f"This site has no {kind.replace('_', ' ')} location set up."
        )
    return location


def event_key(*parts: Any) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, content_hash([str(p) for p in parts]))


def outbound_consumed(
    lot_ids: Iterable[uuid.UUID], intervals: dict[uuid.UUID, list[ranges.Interval]]
) -> bool:
    """Has any portion ever left by dispatch, RTV, adjustment, write-off, shrinkage or disposal?"""
    lot_ids = list(lot_ids)
    # A sold piece has left the shop as surely as a dispatched or written-off one,
    # so a PT covering it can no longer be reversed (OPS-07). A disposed one
    # (P21, goods ticket 15D) has left custody altogether.
    consuming = {"P08", "P13", "P15", SALE_POSTING, DISPOSAL_POSTING}
    legs = QuantityLeg.objects.filter(
        lot_id__in=list(lot_ids), side="source", batch__posting_kind__in=consuming
    ).values_list("lot_id", "portion")
    for lot_id, stored in legs:
        if ranges.intersect([bounds(stored)], intervals.get(lot_id, [])):
            return True
    # A write-off (P20, goods ticket 15C) moves no piece, but the layer's value
    # has left stock: reversing the PT would take it out a second time.
    for lot_id, gone in written_off(lot_ids).items():
        if ranges.intersect(gone, intervals.get(lot_id, [])):
            return True
    return False


def written_off(lot_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[ranges.Interval]]:
    """Portions whose loss a write-off (P20) has recognised, per lot. Permanent evidence.

    Read from the value journal rather than the write-off's hold, so it still
    answers after a later disposal ends that hold: the same loss is never
    recognised twice (quarantine outcomes PRD §7.3).
    """
    out: dict[uuid.UUID, list[ranges.Interval]] = defaultdict(list)
    for lot_id, stored in ValueLeg.objects.filter(
        lot_id__in=list(lot_ids), side="source", batch__posting_kind=WRITE_OFF_POSTING
    ).values_list("lot_id", "portion"):
        if stored is not None:
            out[lot_id].append(bounds(stored))
    return {lot_id: ranges.normalise(parts) for lot_id, parts in out.items()}


def positions_query(**filters: Any) -> Any:
    return Position.objects.filter(Q(**filters))

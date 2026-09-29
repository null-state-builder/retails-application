"""Claims for brand-funded discounts (store operations PRD ST-BRD-3, ST-BRD-6; ticket 26).

The flow, and who does each step (PRD §4, R-FIN-013):

1. **Raised** by Accounts once a month has ended: for each brand and store, the
   brand-funded share on the month's discounted lines (ticket 25's
   ``SaleLineFunding``), sold lines less pieces given back. A part whose share is
   unknown is never claimed; it is listed beside the claim. A part is claimed once,
   ever (``BrandClaimPart``), so a bill that reaches head office after the month's
   claim is taken in by a second claim for the same month.
2. **Accepted**: Accounts records that the brand agreed the claim.
3. **Settled** through the brand's commercial credit note, with no GST effect on
   our bills (Circular 251/08/2025): the note's number, date and amount. A note
   for less than the claim settles it short, and the difference is kept with its
   reason.

A brand whose promotion-services agreement is Yes on the month's last day has
every claim flagged for Accounts, with an alert (ST-BRD-6): money paid under such
an agreement may be payment for a service, not a discount.

Nothing here touches a bill, issues a tax document or posts to the accounts.
Each step is switched per store with ticket 01's switch and leaves an audit
record with the claim before and after. The rules (``tally``, the month) are
pure; the steps take a running command, so a claim and its audit record are
written together or not at all.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.utils import timezone

from accounts.permissions import user_can
from accounts.principal import access_for_user
from accounts.sections import CAP_MANAGE
from alerts.models import Alert, AlertKind, AlertStatus
from core.commands import CommandRun, LockRank
from core.documents import DocStatus
from core.goods_money import MoneyInvalid, paise_from_json
from core.kernel_models import AuditEvent
from core.refusals import Refusal, issue
from masters.brand_terms import promotion_for
from masters.models import Brand, Store
from masters.scoping import actionable_store_ids
from masters.store_feature_registry import BRAND_DISCOUNT_CLAIMS
from masters.store_features import is_feature_on, require_feature
from sell.claim_models import BrandClaim, BrandClaimPart
from sell.models import SaleLine, SaleLineFunding

FEATURE_KEY = BRAND_DISCOUNT_CLAIMS

#: Audit actions, one per step; each claim's subject is ``brand_claim:<id>``.
RAISE_ACTION = "sell.brand_claim.raise"
ACCEPT_ACTION = "sell.brand_claim.accept"
SETTLE_ACTION = "sell.brand_claim.settle"

RAISED = BrandClaim.Status.RAISED.value
ACCEPTED = BrandClaim.Status.ACCEPTED.value
SETTLED = BrandClaim.Status.SETTLED.value
SETTLED_SHORT = BrandClaim.Status.SETTLED_SHORT.value

NOTE_LENGTH = 240
CREDIT_NOTE_LENGTH = 60
MAX_PAISE = 100_000_000_000  # Rs 100 crore
#: Months outside these years are refused, not worked out.
MIN_YEAR = 2000
MAX_YEAR = 2999


# ---------------------------------------------------------------------------
# The rules, without a database
# ---------------------------------------------------------------------------


def parse_month(value: Any) -> date:
    """``"2026-08"`` -> 1 August 2026."""
    text = str(value or "").strip()
    try:
        year, month = (int(part) for part in text.split("-"))
        if not MIN_YEAR <= year <= MAX_YEAR:
            raise ValueError(year)
        return date(year, month, 1)
    except ValueError:
        raise Refusal(
            "INVALID_REQUEST",
            "Pick a month, written like 2026-08.",
            issues=[issue("INVALID", "month is YYYY-MM", field="month")],
        ) from None


def month_end(month: date) -> date:
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def next_month(month: date) -> date:
    return month_end(month) + timedelta(days=1)


def month_bounds(month: date) -> tuple[datetime, datetime]:
    """The month's first and next month's first midnight, India time."""
    zone = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(month, time.min), zone),
        timezone.make_aware(datetime.combine(next_month(month), time.min), zone),
    )


def is_ended(month: date, today: date) -> bool:
    """A month is claimed once it is over (India): "at month end"."""
    return next_month(month) <= today


def reference(store_code: str, brand_code: str, month: date, sequence: int) -> str:
    """A reference the brand can quote back; not a tax document number."""
    return f"{store_code}-{brand_code}-{month:%Y%m}-{sequence}".upper()[:60]


@dataclass(frozen=True)
class FundedPart:
    """One funded part of one bill line, as a claim reads it."""

    funding_id: int
    sale_id: int
    line_id: int
    store_id: int
    #: The brand the line was matched to; None where it is not one brand in the list.
    brand_id: int | None
    brand_name: str
    #: 1 for a sold line, -1 for a piece given back.
    sign: int
    funder: str
    #: Magnitudes, as ``SaleLineFunding`` keeps them; None where the split is unknown.
    brand_paise: int | None
    discount_paise: int
    qty: int


@dataclass
class Tally:
    """What one brand is owed at one store for a month."""

    store_id: int
    brand_id: int | None
    brand_name: str
    #: The brand's share, sold less given back, of the parts in ``claimable``.
    amount_paise: int = 0
    claimable: list[FundedPart] = field(default_factory=list)
    #: Discount whose split is unknown (signed): never claimed, listed.
    unknown_paise: int = 0
    unknown_parts: int = 0

    @property
    def pieces(self) -> int:
        """Pieces on the claimed lines, sold less given back, each line once."""
        lines = {part.line_id: part.sign * part.qty for part in self.claimable}
        return sum(lines.values())


def is_claimable(part: FundedPart) -> bool:
    """A brand-funded part with a known share the brand pays some of."""
    return (
        part.funder == SaleLineFunding.Funder.BRAND
        and part.brand_id is not None
        and part.brand_paise is not None
        and part.brand_paise > 0
    )


def tally(parts: Iterable[FundedPart]) -> dict[tuple[int, int | None], Tally]:
    """The month's parts grouped by store and brand.

    A part with an unknown split (both shares empty) whose offer the brand funds,
    or whose offer is not on the books, counts its discount as unknown; a KDPS
    part is KDPS's alone and is not listed at all.
    """
    out: dict[tuple[int, int | None], Tally] = {}
    for part in parts:
        unknown = part.brand_paise is None
        if unknown and part.funder == SaleLineFunding.Funder.KDPS:
            continue
        if not unknown and not is_claimable(part):
            continue
        key = (part.store_id, part.brand_id)
        row = out.get(key)
        if row is None:
            row = out[key] = Tally(part.store_id, part.brand_id, part.brand_name)
        if unknown:
            row.unknown_paise += part.sign * part.discount_paise
            row.unknown_parts += 1
        else:
            row.amount_paise += part.sign * int(part.brand_paise or 0)
            row.claimable.append(part)
    return out


def snapshot(claim: BrandClaim) -> dict[str, Any]:
    """The claim as its audit records hold it. Money is whole paise, as text."""
    return {
        "reference": claim.reference,
        "status": claim.status,
        "brand_id": claim.brand_id,
        "store_id": claim.store_id,
        "month": claim.month.isoformat(),
        "amount_paise": str(claim.amount_paise),
        "parts": claim.parts,
        "pieces": claim.pieces,
        "unknown_paise": str(claim.unknown_paise),
        "promotion_flag": claim.promotion_flag,
        "accepted_on": claim.accepted_on.isoformat() if claim.accepted_on else None,
        "accepted_note": claim.accepted_note,
        "credit_note_number": claim.credit_note_number,
        "credit_note_date": (
            claim.credit_note_date.isoformat() if claim.credit_note_date else None
        ),
        "settled_paise": None if claim.settled_paise is None else str(claim.settled_paise),
        "difference_paise": (
            None if claim.difference_paise is None else str(claim.difference_paise)
        ),
        "difference_reason": claim.difference_reason,
    }


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


def may_read(user: Any) -> bool:
    """Owner and Accounts: ``money: manage``. Store roles never see a claim."""
    return user_can(user, "money", CAP_MANAGE)


def may_edit(user: Any, site_id: int | None = None, brand_id: int | None = None) -> bool:
    """Accounts (``money: manage`` narrowed to the declared editors)."""
    return access_for_user(user).covers_all({'brand_claim.manage'}, [(site_id, brand_id)], ['financial'])


def readable_stores(user: Any, tenant_id: Any) -> list[Store]:
    """The stores whose claims this person reads, in their own company."""
    if not may_read(user):
        return []
    ids = actionable_store_ids(user, section="money", minimum=CAP_MANAGE)
    rows = Store.objects.filter(tenant_id=tenant_id).order_by("code")
    return list(rows if ids is None else rows.filter(pk__in=ids))


def readable_claims(user: Any, tenant_id: Any) -> Any:
    ids = [store.pk for store in readable_stores(user, tenant_id)]
    return (
        BrandClaim.objects.filter(tenant_id=tenant_id, store_id__in=ids)
        .select_related("brand", "store", "raised_by", "accepted_by", "settled_by")
        .order_by("-month", "store__code", "brand__name", "sequence")
    )


# ---------------------------------------------------------------------------
# Reading the month's funded parts
# ---------------------------------------------------------------------------


def _on_bills(store_ids: list[int]) -> Any:
    """Funded parts on accepted, numbered, not-cancelled bills at these stores."""
    return SaleLineFunding.objects.filter(
        line__sale__store_id__in=store_ids, line__sale__doc_number__isnull=False
    ).exclude(line__sale__docstatus=DocStatus.CANCELLED)


def funded_parts(store_ids: Iterable[int], month: date) -> list[FundedPart]:
    """Every funded part on accepted, numbered, not-cancelled bills of the month.

    A bill belongs to the month of its business day (India), as the funding
    report counts it; a piece given back belongs to the month it came back.
    """
    ids = list(store_ids)
    if not ids:
        return []
    start, end = month_bounds(month)
    return _read(
        _on_bills(ids).filter(line__sale__billed_at__gte=start, line__sale__billed_at__lt=end)
    )


def carried_returns(store_ids: Iterable[int], month: date) -> list[FundedPart]:
    """Brand shares given back before ``month`` that no claim has taken in yet.

    A month that gave back more than it sold, or a return that reached head
    office after its month was claimed, raises no claim of its own; its share is
    carried into the store's next claim for that brand, so the brand is never
    left overclaimed.
    """
    ids = list(store_ids)
    if not ids:
        return []
    start, _ = month_bounds(month)
    return _read(
        _on_bills(ids)
        .filter(
            line__sale__billed_at__lt=start,
            line__direction=SaleLine.Direction.RETURN,
            funder=SaleLineFunding.Funder.BRAND,
            brand_ref__isnull=False,
            brand_paise__gt=0,
        )
        .exclude(pk__in=BrandClaimPart.objects.values("funding_id"))
    )


def _read(rows: Any) -> list[FundedPart]:
    rows = rows.values(
        "id",
        "line_id",
        "line__sale_id",
        "line__sale__store_id",
        "line__direction",
        "line__brand",
        "line__qty",
        "brand_ref_id",
        "funder",
        "brand_paise",
        "discount_paise",
    ).order_by("id")
    return [
        FundedPart(
            funding_id=row["id"],
            sale_id=row["line__sale_id"],
            line_id=row["line_id"],
            store_id=row["line__sale__store_id"],
            brand_id=row["brand_ref_id"],
            brand_name=row["line__brand"] or "",
            sign=1 if row["line__direction"] == SaleLine.Direction.SALE else -1,
            funder=row["funder"] or "",
            brand_paise=None if row["brand_paise"] is None else int(row["brand_paise"]),
            discount_paise=int(row["discount_paise"]),
            qty=abs(int(row["line__qty"])),
        )
        for row in rows
    ]


def claimed_ids(funding_ids: Iterable[int]) -> set[int]:
    ids = list(funding_ids)
    if not ids:
        return set()
    return set(
        BrandClaimPart.objects.filter(funding_id__in=ids).values_list("funding_id", flat=True)
    )


def unclaimed(parts: list[FundedPart]) -> list[FundedPart]:
    """The parts no claim has taken in yet; an unknown part is never taken in."""
    taken = claimed_ids(part.funding_id for part in parts)
    return [part for part in parts if part.funding_id not in taken]


def flag_for(tenant_id: Any, brand_id: int, month: date, at: datetime) -> Any:
    """The promotion-services agreement in force on the month's last day, if Yes."""
    found = promotion_for(tenant_id, brand_id, month_end(month), at)
    return found if found is not None and found.agreement else None


# ---------------------------------------------------------------------------
# Locks and audit
# ---------------------------------------------------------------------------


def _guard(run: CommandRun, store_ids: Iterable[int]) -> None:
    """Every change to a store's claims is taken in turn under one advisory key."""
    run.advisory_lock(LockRank.DOCUMENT, [f"brand-claims:store:{sid}" for sid in sorted(store_ids)])


def _lock(run: CommandRun, claim_id: int) -> BrandClaim:
    store_id = BrandClaim.objects.filter(pk=claim_id).values_list("store_id", flat=True).first()
    if store_id is None:
        raise Refusal("NOT_FOUND", "That claim was not found.")
    _guard(run, [store_id])
    claim: BrandClaim = BrandClaim.objects.select_for_update().get(pk=claim_id)
    return claim


def _check_revision(claim: BrandClaim, expected_revision: int | None) -> None:
    if expected_revision is not None and expected_revision != claim.revision:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Someone changed this claim after you loaded it. Reload it and try again.",
        )


def _set_command_audit(
    run: CommandRun, claim: BrandClaim, before: dict[str, Any], after: dict[str, Any]
) -> None:
    run.audit_subject_key = f"brand_claim:{claim.pk}"
    run.audit_site_id = claim.store_id
    run.audit_before = before
    run.audit_after = after


def _rupees(paise: int) -> str:
    rupees, rest = divmod(abs(paise), 100)
    return f"Rs {rupees:,}" + (f".{rest:02d}" if rest else "")


def _raise_flag(claim: BrandClaim) -> None:
    """The alert for Accounts, at the claim's store so it stays within the
    company and the reader's stores. It names no brand, so a brand manager never
    reads it, and the inbox shows this kind only to ``money: manage``
    (``alerts.views.KIND_NEEDS``), so store roles and Admin never do."""
    Alert.objects.create(
        kind=AlertKind.BRAND_CLAIM_FLAGGED,
        kind_label=AlertKind.BRAND_CLAIM_FLAGGED.label,
        title=(
            f"{claim.reference}: {claim.brand.name} has a promotion-services agreement. "
            "Check whether this is a discount or payment for a service."
        )[:240],
        dedupe_key=f"brand_claim:{claim.pk}",
        store_id=claim.store_id,
        brand=claim.brand.name,
        brand_ref=claim.brand,
        object_id=claim.pk,
        due_date=None,
        status=AlertStatus.OPEN,
    )


def _resolve_flag(claim: BrandClaim, now: datetime) -> None:
    Alert.objects.filter(
        kind=AlertKind.BRAND_CLAIM_FLAGGED,
        dedupe_key=f"brand_claim:{claim.pk}",
        status=AlertStatus.OPEN,
    ).update(status=AlertStatus.RESOLVED, resolved_at=now)


# ---------------------------------------------------------------------------
# 1. Raised once the month has ended
# ---------------------------------------------------------------------------


def _acting_stores(user: Any, tenant_id: Any, store_ids: list[int] | None) -> list[Store]:
    """The stores a raise covers: those asked for (each must be switched on), or
    every store in reach where the switch is on."""
    reach = {store.pk: store for store in readable_stores(user, tenant_id)}
    if store_ids is None:
        return [store for store in reach.values() if is_feature_on(store, FEATURE_KEY)]
    stores: list[Store] = []
    for sid in store_ids:
        store = reach.get(sid)
        if store is None:
            raise Refusal("NOT_FOUND", "That store was not found.")
        require_feature(store, FEATURE_KEY)
        stores.append(store)
    return stores


def raise_claims(
    run: CommandRun,
    *,
    user: Any,
    tenant_id: Any,
    month: date,
    today: date,
    store_ids: list[int] | None = None,
    brand_ids: list[int] | None = None,
) -> list[BrandClaim]:
    """Raise the ended month's claims: one per brand and store with something owed.

    Every part not yet claimed goes in, so a bill that reached head office after
    the month's first claim is claimed by a second one. A brand whose share nets
    to nothing or less (more given back than sold) gets no claim; its parts wait.
    """
    if not is_ended(month, today):
        raise Refusal(
            "STATE_CONFLICT",
            f"{month:%B %Y} has not ended yet. Claims are raised at month end.",
        )
    stores = _acting_stores(user, tenant_id, store_ids)
    if not stores:
        raise Refusal(
            "FEATURE_OFF",
            "Brand discount claims are switched off at every store you work at. Admin "
            "can switch them on in Setup, Feature Switches.",
            status=403,
        )
    _guard(run, [store.pk for store in stores])
    by_id = {store.pk: store for store in stores}
    wanted = None if brand_ids is None else set(brand_ids)
    all_parts = funded_parts(by_id, month)
    unknown = tally(all_parts)
    owed = tally(unclaimed(all_parts) + carried_returns(by_id, month))
    brands = Brand.objects.in_bulk([bid for (_, bid) in owed if bid is not None])
    raised: list[BrandClaim] = []
    for (store_id, brand_id), row in sorted(owed.items(), key=lambda item: str(item[0])):
        if brand_id is None or row.amount_paise <= 0:
            continue
        if wanted is not None and brand_id not in wanted:
            continue
        if not may_edit(user, store_id, brand_id):
            raise Refusal("NOT_FOUND", "That store and brand are not in your claim scope.")
        store, brand = by_id[store_id], brands[brand_id]
        earlier = BrandClaim.objects.filter(
            tenant_id=tenant_id, brand_id=brand_id, store_id=store_id, month=month
        )
        sequence = earlier.count() + 1
        # Unknown discount the month's earlier claims already listed is not listed again.
        listed = sum(earlier.values_list("unknown_paise", flat=True))
        month_unknown = (
            unknown[(store_id, brand_id)].unknown_paise if (store_id, brand_id) in unknown else 0
        )
        flag = flag_for(tenant_id, brand_id, month, run.now)
        claim = BrandClaim.objects.create(
            brand=brand,
            store=store,
            month=month,
            sequence=sequence,
            reference=reference(store.code, brand.code, month, sequence),
            amount_paise=row.amount_paise,
            parts=len(row.claimable),
            pieces=row.pieces,
            unknown_paise=month_unknown - listed,
            promotion_flag=flag is not None,
            promotion_version_id=None if flag is None else flag.id,
            raised_by=user,
        )
        BrandClaimPart.objects.bulk_create(
            [
                BrandClaimPart(
                    claim=claim,
                    funding_id=part.funding_id,
                    sale_id=part.sale_id,
                    line_id=part.line_id,
                    brand_paise=part.sign * int(part.brand_paise or 0),
                    pieces=part.sign * part.qty,
                )
                for part in row.claimable
            ]
        )
        if flag is not None:
            _raise_flag(claim)
        run.record(
            AuditEvent(
                action=RAISE_ACTION,
                subject_key=f"brand_claim:{claim.pk}",
                site_id=claim.store_id,
                outcome="recorded",
                reason_code="PROMOTION_FLAGGED" if flag is not None else "MONTH_END",
                before=None,
                after=snapshot(claim),
                authority=run.authority,
            )
        )
        raised.append(claim)
    if not raised:
        raise Refusal(
            "STATE_CONFLICT",
            f"Nothing is left to claim for {month:%B %Y} at the stores chosen: every "
            "brand-funded share there is claimed already, unknown, or given back.",
        )
    run.audit_subject_key = f"brand_claims:{month:%Y-%m}"
    run.audit_site_id = raised[0].store_id if len(by_id) == 1 else None
    run.audit_before = {"month": month.isoformat(), "claims": []}
    run.audit_after = {
        "month": month.isoformat(),
        "claims": [
            {"id": claim.pk, "reference": claim.reference, "amount_paise": str(claim.amount_paise)}
            for claim in raised
        ],
    }
    return raised


# ---------------------------------------------------------------------------
# 2. Accepted by the brand
# ---------------------------------------------------------------------------


def _note(value: Any, field_name: str, problems: list[dict[str, Any]], *, required: bool) -> str:
    text = str(value or "").strip()
    if required and not text:
        problems.append(issue("REQUIRED", "Say in a few words.", field=field_name))
    if len(text) > NOTE_LENGTH:
        problems.append(issue("TOO_LONG", f"At most {NOTE_LENGTH} characters.", field=field_name))
    return text


def _refuse(problems: list[dict[str, Any]]) -> None:
    if problems:
        raise Refusal(
            "INVALID_REQUEST",
            " ".join(str(p["message"]) for p in problems),
            issues=problems,
        )


def accept(
    run: CommandRun,
    claim_id: int,
    *,
    user: Any,
    note: Any,
    today: date,
    expected_revision: int | None,
) -> BrandClaim:
    """Accounts records that the brand agreed the claim."""
    claim = _lock(run, claim_id)
    _check_revision(claim, expected_revision)
    require_feature(claim.store_id, FEATURE_KEY)
    if claim.status != RAISED:
        raise Refusal(
            "STATE_CONFLICT", f"This claim is {claim.get_status_display().lower()} already."
        )
    problems: list[dict[str, Any]] = []
    text = _note(note, "note", problems, required=False)
    _refuse(problems)
    before = snapshot(claim)
    claim.status = ACCEPTED
    claim.accepted_on = today
    claim.accepted_by = user
    claim.accepted_note = text
    claim.revision += 1
    claim.save()
    _set_command_audit(run, claim, before, snapshot(claim))
    return claim


# ---------------------------------------------------------------------------
# 3. Settled by the brand's commercial credit note
# ---------------------------------------------------------------------------


def _paise(value: Any, problems: list[dict[str, Any]]) -> int | None:
    try:
        paise = paise_from_json(value, maximum=MAX_PAISE)
    except MoneyInvalid:
        paise = 0
    if not paise:
        problems.append(
            issue(
                "INVALID",
                "The credit note's amount is whole paise written as text, more than 0.",
                field="settled_paise",
            )
        )
        return None
    return int(paise)


def _day(value: Any, problems: list[dict[str, Any]]) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        problems.append(
            issue("INVALID", "The credit note's date must be a date.", field="credit_note_date")
        )
        return None


def settle(
    run: CommandRun,
    claim_id: int,
    *,
    user: Any,
    credit_note_number: Any,
    credit_note_date: Any,
    settled_paise: Any,
    difference_reason: Any,
    today: date,
    expected_revision: int | None,
) -> BrandClaim:
    """Record the brand's commercial credit note against an accepted claim.

    The full amount settles it; less settles it short, and the difference is kept
    with Accounts' reason. More than the claim is refused. A commercial credit
    note carries no GST, so nothing on any bill changes.
    """
    claim = _lock(run, claim_id)
    _check_revision(claim, expected_revision)
    require_feature(claim.store_id, FEATURE_KEY)
    if claim.status != ACCEPTED:
        raise Refusal(
            "STATE_CONFLICT",
            "Only a claim the brand has accepted is settled. Record the brand accepting it first."
            if claim.status == RAISED
            else f"This claim is {claim.get_status_display().lower()} already.",
        )
    problems: list[dict[str, Any]] = []
    number = str(credit_note_number or "").strip()
    if not number or len(number) > CREDIT_NOTE_LENGTH:
        problems.append(
            issue(
                "REQUIRED",
                f"Type the brand's credit note number (up to {CREDIT_NOTE_LENGTH} characters).",
                field="credit_note_number",
            )
        )
    day = _day(credit_note_date, problems)
    if day is not None and day > today:
        problems.append(
            issue(
                "FUTURE", "The credit note's date cannot be after today.", field="credit_note_date"
            )
        )
    if day is not None and day < claim.month:
        problems.append(
            issue(
                "BEFORE_MONTH",
                "The credit note's date cannot be before the month it settles.",
                field="credit_note_date",
            )
        )
    paise = _paise(settled_paise, problems)
    if paise is not None and paise > claim.amount_paise:
        problems.append(
            issue(
                "MORE_THAN_CLAIMED",
                f"The credit note is for more than the claim ({_rupees(claim.amount_paise)}).",
                field="settled_paise",
            )
        )
    short = paise is not None and paise < claim.amount_paise
    reason = _note(difference_reason, "difference_reason", problems, required=short)
    _refuse(problems)
    assert paise is not None and day is not None
    before = snapshot(claim)
    claim.status = SETTLED_SHORT if short else SETTLED
    claim.credit_note_number = number
    claim.credit_note_date = day
    claim.settled_paise = paise
    claim.difference_paise = claim.amount_paise - paise
    claim.difference_reason = reason if short else ""
    claim.settled_by = user
    claim.revision += 1
    claim.save()
    _resolve_flag(claim, run.now)
    _set_command_audit(run, claim, before, snapshot(claim))
    return claim


# ---------------------------------------------------------------------------
# What is left to raise
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToRaise:
    store: Store
    brand_id: int | None
    brand_name: str
    #: Owed and not yet claimed; zero or less raises nothing.
    amount_paise: int
    parts: int
    pieces: int
    unknown_paise: int
    unknown_parts: int
    raised: int
    promotion_flag: bool
    switched_on: bool


def to_raise(user: Any, tenant_id: Any, month: date, at: datetime) -> list[ToRaise]:
    """For each store in reach and brand: what the month still owes (with shares
    given back earlier and carried in), what is unknown, and how many claims are
    raised already. An amount of nought or less raises nothing yet."""
    stores = {store.pk: store for store in readable_stores(user, tenant_id)}
    all_parts = funded_parts(stores, month)
    known = tally(all_parts)
    owed = tally(unclaimed(all_parts) + carried_returns(stores, month))
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for claim_store, claim_brand in BrandClaim.objects.filter(
        tenant_id=tenant_id, store_id__in=list(stores), month=month
    ).values_list("store_id", "brand_id"):
        counts[(claim_store, claim_brand)] += 1
    keys = set(known) | set(owed)
    brands = Brand.objects.in_bulk([bid for (_, bid) in keys if bid is not None])
    flags = {bid: flag_for(tenant_id, bid, month, at) is not None for bid in brands}
    switches = {sid: is_feature_on(store, FEATURE_KEY) for sid, store in stores.items()}
    rows: list[ToRaise] = []
    for store_id, brand_id in keys:
        whole = known.get((store_id, brand_id))
        left = owed.get((store_id, brand_id))
        brand = brands.get(brand_id) if brand_id is not None else None
        either = whole or left
        named = either.brand_name if either is not None else ""
        rows.append(
            ToRaise(
                store=stores[store_id],
                brand_id=brand_id,
                brand_name=brand.name if brand is not None else named,
                amount_paise=left.amount_paise if left else 0,
                parts=len(left.claimable) if left else 0,
                pieces=left.pieces if left else 0,
                unknown_paise=whole.unknown_paise if whole else 0,
                unknown_parts=whole.unknown_parts if whole else 0,
                raised=counts[(store_id, brand_id)] if brand_id is not None else 0,
                promotion_flag=brand_id is not None and flags.get(brand_id, False),
                switched_on=switches[store_id],
            )
        )
    rows.sort(key=lambda row: (row.store.code, row.brand_name.lower()))
    return rows

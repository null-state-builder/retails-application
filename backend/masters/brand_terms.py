"""Brand terms: what is in force, what is unknown, and who may change them (ticket 23).

The questions every later ticket asks (SOR ageing, funding split, claims, margin
share, payables), answered in one place:

* ``terms_for(tenant_id, brand_id, season_id, day, at)`` - the approved terms in
  force for a brand and season on ``day`` (India), as they stood at the moment
  ``at``. ``None`` means the model is **unknown** (D9): nothing is assumed, and
  the caller says so rather than guessing. A caller that stores a result stores
  the ``Terms.id`` it used, so a later version can never change it.
* ``promotion_for(tenant_id, brand_id, day, at)`` - the approved
  "promotion-services agreement" in force, or ``None``, which means **No**
  (ST-BRD-6's default).
* ``in_force(versions, day, at)`` - the pure rule under both: the newest
  approved version whose date has come *and* that had been approved by ``at``.
  A version approved this afternoon never claims a result worked out this
  morning, even when it applies from today.

A change is always a new version. It counts only once the Owner approves it, by
a different person from the one who proposed it (overall PRD R-BUY-006, R-CTL-004),
and it may not apply from a past date (the same rule as tax settings, B25), so a
result already worked out for an earlier day stays exactly as it was.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from accounts.principal import access_for_user
from core.refusals import Refusal, issue
from masters.brand_terms_models import (
    BrandPromotionVersion,
    BrandTermsDecision,
    BrandTermsVersion,
    CommercialModel,
)
from masters.models import Brand, Store
from core.tenancy import require_tenant_id
from masters.store_feature_registry import BRAND_TERMS
from masters.store_features import feature, switch_states

#: The store feature (ticket 01's registry) that switches brand terms on.
FEATURE_KEY = BRAND_TERMS

TERMS = BrandTermsDecision.Kind.TERMS.value
PROMOTION = BrandTermsDecision.Kind.PROMOTION.value
APPROVED = BrandTermsDecision.Outcome.APPROVED.value
REJECTED = BrandTermsDecision.Outcome.REJECTED.value
WITHDRAWN = BrandTermsDecision.Outcome.WITHDRAWN.value
#: A proposed row nobody has decided yet.
WAITING = "waiting"

_MAX_PERCENT = Decimal("100")
_MAX_PAYMENT_DAYS = 365
_NOTE_LENGTH = 240


@dataclass(frozen=True)
class Terms:
    """One approved version of a brand's terms for a season."""

    id: uuid.UUID
    brand_id: int
    season_id: int
    version: int
    applies_from: date
    approved_at: datetime
    model: str
    #: Each figure is None when it was left unknown.
    margin_percent: Decimal | None
    return_allowance_percent: Decimal | None
    discount_funding_percent: Decimal | None
    payment_days: int | None


@dataclass(frozen=True)
class Promotion:
    """One approved version of a brand's promotion-services agreement."""

    id: uuid.UUID
    brand_id: int
    version: int
    applies_from: date
    approved_at: datetime
    agreement: bool


class _Dated(Protocol):
    @property
    def applies_from(self) -> date: ...

    @property
    def approved_at(self) -> datetime: ...

    @property
    def version(self) -> int: ...


def in_force[D: _Dated](versions: Iterable[D], day: date, at: datetime | None = None) -> D | None:
    """The approved version in force on ``day``, as things stood at ``at``."""
    candidates = [
        v for v in versions if v.applies_from <= day and (at is None or v.approved_at <= at)
    ]
    return max(candidates, key=lambda v: (v.applies_from, v.version), default=None)


# -- reading ----------------------------------------------------------------


def decisions(tenant_id: Any, kind: str, subject_ids: Iterable[Any]) -> dict[Any, Any]:
    """``{row id: BrandTermsDecision}`` for the rows given."""
    ids = list(subject_ids)
    if not ids:
        return {}
    rows = BrandTermsDecision.objects.filter(
        tenant_id=tenant_id, subject_kind=kind, subject_id__in=ids
    )
    return {row.subject_id: row for row in rows}


def _approved_at(tenant_id: Any, kind: str) -> dict[Any, datetime]:
    rows = BrandTermsDecision.objects.filter(
        tenant_id=tenant_id, subject_kind=kind, outcome=APPROVED
    ).values_list("subject_id", "created_at")
    return dict(rows)


def as_terms(row: BrandTermsVersion, approved_at: datetime) -> Terms:
    return Terms(
        id=row.pk,
        brand_id=row.brand_id,
        season_id=row.season_id,
        version=row.version,
        applies_from=row.applies_from,
        approved_at=approved_at,
        model=row.model,
        margin_percent=row.margin_percent,
        return_allowance_percent=row.return_allowance_percent,
        discount_funding_percent=row.discount_funding_percent,
        payment_days=row.payment_days,
    )


def approved_terms(
    tenant_id: Any,
    *,
    brand_ids: Iterable[int] | None = None,
    season_ids: Iterable[int] | None = None,
) -> list[Terms]:
    approved = _approved_at(tenant_id, TERMS)
    rows = BrandTermsVersion.objects.filter(tenant_id=tenant_id, pk__in=list(approved))
    if brand_ids is not None:
        rows = rows.filter(brand_id__in=list(brand_ids))
    if season_ids is not None:
        rows = rows.filter(season_id__in=list(season_ids))
    return [as_terms(row, approved[row.pk]) for row in rows]


def approved_promotions(
    tenant_id: Any, *, brand_ids: Iterable[int] | None = None
) -> list[Promotion]:
    approved = _approved_at(tenant_id, PROMOTION)
    rows = BrandPromotionVersion.objects.filter(tenant_id=tenant_id, pk__in=list(approved))
    if brand_ids is not None:
        rows = rows.filter(brand_id__in=list(brand_ids))
    return [
        Promotion(
            id=row.pk,
            brand_id=row.brand_id,
            version=row.version,
            applies_from=row.applies_from,
            approved_at=approved[row.pk],
            agreement=row.agreement,
        )
        for row in rows
    ]


def terms_for(
    tenant_id: Any, brand_id: int, season_id: int, day: date, at: datetime | None = None
) -> Terms | None:
    """The approved terms in force, or None: the model is unknown (D9)."""
    return in_force(
        approved_terms(tenant_id, brand_ids=[brand_id], season_ids=[season_id]), day, at
    )


def promotion_for(
    tenant_id: Any, brand_id: int, day: date, at: datetime | None = None
) -> Promotion | None:
    """The approved agreement in force, or None: No, the default (ST-BRD-6)."""
    return in_force(approved_promotions(tenant_id, brand_ids=[brand_id]), day, at)


def has_agreement(tenant_id: Any, brand_id: int, day: date, at: datetime | None = None) -> bool:
    found = promotion_for(tenant_id, brand_id, day, at)
    return bool(found and found.agreement)


# -- who may do what ---------------------------------------------------------


def _brand_ids(user: Any, action: str) -> list[int]:
    """A term is company-wide for one brand, including its protected fields."""
    return [brand_id for brand_id in Brand.objects.filter(tenant_id=require_tenant_id()).values_list("id", flat=True)
            if access_for_user(user).covers_all({action}, [(None, brand_id)], {"cost", "margin"})]


def _may_use_terms(user: Any, action: str, brand_id: int | None) -> bool:
    if brand_id is None:
        return bool(_brand_ids(user, action))
    return access_for_user(user).covers_all({action}, [(None, brand_id)], {"cost", "margin"})


def may_read(user: Any, brand_id: int | None = None) -> bool:
    return _may_use_terms(user, "brand_terms.view", brand_id)


def may_propose(user: Any, brand_id: int | None = None) -> bool:
    return _may_use_terms(user, "brand_terms.propose", brand_id)


def may_approve(user: Any, brand_id: int | None = None) -> bool:
    return _may_use_terms(user, "brand_terms.approve", brand_id)


def readable_brands(user: Any) -> Any:
    """Only brands covered by a qualifying all-sites assignment."""
    return Brand.objects.filter(
        tenant_id=require_tenant_id(),
        pk__in=_brand_ids(user, "brand_terms.view"),
    ).order_by("name")


def changeable_brands(user: Any) -> Any:
    """The brands this person may propose or decide for.

    Terms apply company-wide, so a person whose scope is some stores (not every
    store, not a set of brands) changes none of them.
    """
    # A retired brand keeps its history to read, but nothing new is agreed for it.
    editable = set(_brand_ids(user, "brand_terms.propose"))
    editable.update(_brand_ids(user, "brand_terms.approve"))
    return Brand.objects.filter(
        tenant_id=require_tenant_id(), pk__in=editable, is_active=True
    ).order_by("name")


def switch_stores(user: Any) -> list[Store]:
    """The stores whose ticket-01 switch decides whether this person may write.

    A brand manager's work spans every store, so every active store counts; for
    everyone else, the stores they may act at.
    """
    if not (may_read(user) or may_propose(user) or may_approve(user)):
        return []
    return list(
        Store.objects.filter(tenant_id=require_tenant_id(), is_active=True).order_by("code")
    )


def brand_terms_sites(user: Any) -> list[str]:
    """The stores (as id strings) where brand terms are on, of those that count
    for this person - what their session tells the menu."""
    try:
        registered = feature(FEATURE_KEY)
    except LookupError:  # a test that swapped the registry out
        return []
    states = switch_states(switch_stores(user), [registered])
    return [str(state.site_id) for state in states if state.enabled]


def switched_on(user: Any) -> bool:
    stores = switch_stores(user)
    return any(state.enabled for state in switch_states(stores, [feature(FEATURE_KEY)]))


def require_switched_on(user: Any) -> None:
    """Refuse new work unless brand terms are on at one of this person's stores."""
    if not switched_on(user):
        raise Refusal(
            "FEATURE_OFF",
            "Brand terms are switched off at every store you work at. "
            "Admin can switch them on in Setup, Feature Switches.",
            status=403,
        )


# -- reading a proposal ------------------------------------------------------


class Problems:
    """Every problem with a proposal, named at once."""

    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.sentences: list[str] = []

    def add(self, field: str, code: str, sentence: str) -> None:
        self.issues.append(issue(code, sentence, field=field))
        self.sentences.append(sentence)

    def raise_any(self) -> None:
        if self.issues:
            raise Refusal("INVALID_REQUEST", " ".join(self.sentences), issues=self.issues)


def parse_model(value: Any, problems: Problems) -> str:
    text = str(value or "").strip().lower()
    if text not in CommercialModel.values:
        problems.add(
            "model",
            "INVALID",
            "Pick the commercial model: SOR, outright, consignment or concession.",
        )
    return text


def parse_percent(value: Any, field: str, label: str, problems: Problems) -> Decimal | None:
    """A percentage, 0 to 100, at most two decimals. Blank is unknown, never 0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        problems.add(field, "INVALID", f"The {label} must be a number from 0 to 100.")
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        problems.add(field, "INVALID", f"The {label} must be a number from 0 to 100.")
        return None
    if not number.is_finite() or number < 0 or number > _MAX_PERCENT:
        problems.add(field, "INVALID", f"The {label} must be a number from 0 to 100.")
        return None
    if number != number.quantize(Decimal("0.01")):
        problems.add(field, "INVALID", f"The {label} can have at most two decimals.")
        return None
    return number.quantize(Decimal("0.01"))


def parse_days(value: Any, problems: Problems) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        days = int(str(value).strip()) if not isinstance(value, bool) else -1
    except ValueError:
        days = -1
    if days < 0 or days > _MAX_PAYMENT_DAYS:
        problems.add(
            "payment_days",
            "INVALID",
            f"Payment days must be a whole number from 0 to {_MAX_PAYMENT_DAYS}.",
        )
        return None
    return days


def parse_note(value: Any, problems: Problems, *, required: bool = True) -> str:
    note = str(value or "").strip()
    if (required and not note) or len(note) > _NOTE_LENGTH:
        problems.add("note", "REQUIRED", "Say in a few words why (up to 240 characters).")
    return note[:_NOTE_LENGTH]


def parse_day(value: Any, problems: Problems) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        problems.add("applies_from", "INVALID", "The date it applies from must be a date.")
        return None


def check_applies_from(applies: date, today: date, newest: date | None) -> None:
    """Never a past date, and never before the newest approved version's date.

    A past date could change a result already worked out for that day, which a
    change of terms must never do; the overall PRD's backdating approval (§8.4)
    is not built, so it is refused, as for tax settings (B25).
    """
    if applies < today:
        raise Refusal(
            "INVALID_REQUEST",
            "New terms cannot apply from a past date. Pick today or a later date.",
            issues=[issue("BACKDATED", "applies_from is before today", field="applies_from")],
        )
    if newest is not None and applies < newest:
        raise Refusal(
            "INVALID_REQUEST",
            f"New terms must apply from {newest.isoformat()} or later, the date the terms "
            "in force now apply from.",
            issues=[
                issue(
                    "OUT_OF_ORDER",
                    "applies_from is before the newest approved version's",
                    field="applies_from",
                )
            ],
        )


def newest_approved[D: _Dated](versions: Sequence[D]) -> D | None:
    return max(versions, key=lambda v: (v.applies_from, v.version), default=None)


def percent_text(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"

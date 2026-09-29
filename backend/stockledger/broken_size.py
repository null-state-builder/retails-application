"""Broken sizes at a store (store operations ticket 32, ST-INV-1).

A style-colour at a store is **broken** when it still has stock there but is
missing a set share of its category's core sizes: 40% or more to start (store
operations PRD ST-INV-1 and §23, Anand's B2 there). The core sizes and the share
are set per category (``SizeRule``); a category with no rule is never checked,
since nothing is guessed.

The category is the item's ITEM value (the Master Sheet's "item", shown as the
item's grade in the goods identity), the same word the sales report groups by.
An item with none is in "No category", and it is checked only once a steward
sets a rule for that too.

Only goods-v1 stores have positions to read, so only they can be checked; a
legacy store is reported as such. The counted stock is the same as Stock
Ageing's: good, accepted pieces standing at the store (held or reserved
included). No cost is in any row. The daily alerts job
(``alerts.checks.check_broken_sizes``) turns what ``store_sizes`` finds into
``BrokenSizeAlert`` rows (``refresh_store``, the only writer here), and those are
what size balancing (ticket 34) and markdown suggestions read, through
``open_broken_sizes``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction

from masters.models import Store

#: The rule's word for "the item has no ITEM value".
NO_CATEGORY = ""
#: The success measure's window: broken-size alerts acted on within 7 days (§26).
ACT_WITHIN = timedelta(days=7)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def size_key(size: str) -> str:
    """A size as compared: its words, whatever their case or spacing."""
    return " ".join(str(size).split()).upper()


def same_size(a: str, b: str) -> bool:
    return size_key(a) == size_key(b)


@dataclass(frozen=True)
class SizeVerdict:
    broken: bool
    #: The core sizes with no piece here, in the rule's order.
    missing: tuple[str, ...]
    #: The share of core sizes missing, rounded down to a whole percent (for showing;
    #: the rule itself compares exactly).
    missing_percent: int


def judge(core_sizes: Sequence[str], missing_percent: int, held: Mapping[str, int]) -> SizeVerdict:
    """Is a style-colour holding ``held`` (pieces per size) broken under this rule?"""
    pieces_by_size: dict[str, int] = defaultdict(int)
    for size, qty in held.items():
        pieces_by_size[size_key(size)] += qty
    core = [size for size in core_sizes if size_key(size)]
    missing = tuple(size.strip() for size in core if pieces_by_size.get(size_key(size), 0) <= 0)
    has_stock = sum(qty for qty in pieces_by_size.values() if qty > 0) > 0
    if not core:
        return SizeVerdict(broken=False, missing=(), missing_percent=0)
    broken = has_stock and len(missing) * 100 >= missing_percent * len(core)
    return SizeVerdict(
        broken=broken, missing=missing, missing_percent=len(missing) * 100 // len(core)
    )


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    category: str
    core_sizes: tuple[str, ...]
    missing_percent: int


def rules_in_force() -> dict[str, Rule]:
    """Each category's rule, keyed by its size key (so "Shirt" and "SHIRT" are one)."""
    from core.tenancy import require_tenant_id
    from stockledger.broken_size_models import SizeRule

    return {
        row.category_key: Rule(
            category=row.category,
            core_sizes=tuple(row.core_sizes),
            missing_percent=row.missing_percent,
        )
        for row in SizeRule.objects.filter(tenant_id=require_tenant_id(), active=True)
    }


@dataclass(frozen=True)
class StyleColour:
    """One style in one colour at one store, as it stands today."""

    style_id: uuid.UUID
    brand: str
    style_code: str
    #: Blank where the item's identity has no colour.
    colour: str
    category: str
    #: Pieces per size, in the order the sizes were first met.
    held: dict[str, int]
    rule: Rule | None
    verdict: SizeVerdict | None
    #: Its items name more than one category, so no one rule is theirs.
    mixed: bool = False

    @property
    def pieces(self) -> int:
        return sum(self.held.values())

    @property
    def broken(self) -> bool:
        return self.verdict is not None and self.verdict.broken


@dataclass(frozen=True)
class StoreSizes:
    store: Store
    #: False for a legacy store: it keeps no positions, so nothing can be checked.
    goods_records: bool
    style_colours: list[StyleColour] = field(default_factory=list)

    @property
    def broken(self) -> list[StyleColour]:
        return [row for row in self.style_colours if row.broken]

    @property
    def unruled_categories(self) -> list[str]:
        """Categories held here that no rule covers, so none of their stock is checked."""
        return sorted(
            {row.category for row in self.style_colours if row.rule is None and not row.mixed}
        )

    @property
    def mixed(self) -> list[StyleColour]:
        """Style-colours whose items name two categories: not checked, whatever the rules."""
        return [row for row in self.style_colours if row.mixed]


def _held(store: Store) -> dict[uuid.UUID, int]:
    """Good, accepted pieces standing at this store, per item."""
    from core.goods_fields import bounds
    from stockledger.goods_models import Position

    pieces: dict[uuid.UUID, int] = defaultdict(int)
    for portion, sku_id in Position.objects.filter(
        site_id=store.pk,
        boundary="physical",
        condition="good",
        accepted_event__isnull=False,
        sku__isnull=False,
    ).values_list("portion", "sku_id"):
        lower, upper = bounds(portion)
        pieces[sku_id] += upper - lower
    return {sku_id: qty for sku_id, qty in pieces.items() if qty > 0}


def store_sizes(store: Store, rules: Mapping[str, Rule] | None = None) -> StoreSizes:
    """Every style-colour standing at this store, each checked against its category's rule."""
    from core.tenancy import require_tenant_id
    from masters.goods_identity_models import ProductSku
    from masters.goods_identity_services import candidates_for
    from sell.services.goods_stock import is_goods_site

    if not is_goods_site(store):
        return StoreSizes(store=store, goods_records=False)
    held = _held(store)
    if not held:
        return StoreSizes(store=store, goods_records=True)
    rules = rules_in_force() if rules is None else rules
    style_of = dict(
        ProductSku.objects.filter(pk__in=sorted(held, key=str)).values_list("pk", "style_id")
    )
    described = {
        row["sku_id"]: row for row in candidates_for(require_tenant_id(), {str(s) for s in held})
    }
    grouped: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
    for sku_id in sorted(held, key=str):
        identity = described.get(str(sku_id), {})
        colour = str(identity.get("colour") or "")
        key = (style_of[sku_id], size_key(colour))
        entry = grouped.setdefault(
            key,
            {
                "brand": str(identity.get("brand") or ""),
                "style_code": str(identity.get("style") or ""),
                "colour": colour,
                "categories": set(),
                "held": defaultdict(int),
            },
        )
        entry["categories"].add(str(identity.get("grade") or NO_CATEGORY))
        entry["held"][str(identity.get("size") or "unknown")] += held[sku_id]

    rows: list[StyleColour] = []
    for (style_id, _colour), entry in grouped.items():
        categories = {size_key(c): c for c in entry["categories"]}
        # One style-colour, one category. Two items of it naming two different
        # categories cannot say which rule is theirs, so it is not checked.
        category = next(iter(categories.values())) if len(categories) == 1 else None
        rule = rules.get(size_key(category)) if category is not None else None
        rows.append(
            StyleColour(
                style_id=style_id,
                brand=entry["brand"],
                style_code=entry["style_code"],
                colour=entry["colour"],
                category=category
                if category is not None
                else " / ".join(sorted(categories.values())),
                mixed=category is None,
                held=dict(entry["held"]),
                rule=rule,
                verdict=judge(rule.core_sizes, rule.missing_percent, entry["held"])
                if rule
                else None,
            )
        )
    rows.sort(key=lambda r: (r.brand, r.style_code, r.colour))
    return StoreSizes(store=store, goods_records=True, style_colours=rows)


# ---------------------------------------------------------------------------
# The daily check: open, refresh and close the alerts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreCount:
    #: Broken-size alerts open at the store after the check.
    open: int
    #: Of those, the ones nobody has acted on yet.
    not_acted: int


def _colour_key(style_id: uuid.UUID, colour: str) -> tuple[uuid.UUID, str]:
    return (style_id, size_key(colour))


def _lock_store(store: Store) -> None:
    """One refresh of a store's alerts at a time (a scheduled run and a hand run may overlap)."""
    from django.db import connection

    from core.canonical import content_hash

    number = int(content_hash([str(store.tenant_id), f"broken-size-store:{store.pk}"])[:15], 16)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [number])


@transaction.atomic
def refresh_store(
    store: Store,
    *,
    on: bool,
    now: datetime,
    rules: Mapping[str, Rule] | None = None,
    closing: str | None = None,
) -> StoreCount:
    """Make the store's open alerts match what is true now.

    A style-colour newly broken opens an alert; one still broken is refreshed
    (sizes, pieces, the rule's share) and keeps its opening time, so its 7 days
    are not restarted; one no longer broken is closed, saying why. With the
    switch off every open alert at the store closes; ``closing`` names another
    reason to close them all (the store is no longer checked). An action already
    recorded is never touched here.

    The stock is read before anything is locked, so a person recording an action
    waits only for the writes, not the read.
    """
    from stockledger.broken_size_models import BrokenSizeAlert

    sizes = store_sizes(store, rules) if on and not closing else None
    if sizes is not None and not sizes.goods_records:
        closing = BrokenSizeAlert.Closed.NOT_CHECKED
    _lock_store(store)
    open_alerts = {
        _colour_key(alert.style_id, alert.colour): alert
        for alert in BrokenSizeAlert.objects.select_for_update()
        .filter(site=store, closed_at__isnull=True)
        .order_by("id")
    }
    if sizes is None or closing:
        everything = closing or BrokenSizeAlert.Closed.SWITCHED_OFF
        for alert in open_alerts.values():
            _close(alert, everything, now)
        return StoreCount(open=0, not_acted=0)

    found = {_colour_key(row.style_id, row.colour): row for row in sizes.style_colours}
    for key, alert in open_alerts.items():
        reason = _closed_because(alert, found.get(key))
        if reason:
            _close(alert, reason, now)
    still = [
        _open_or_refresh(store, row, open_alerts.get(key), now)
        for key, row in found.items()
        if row.broken
    ]
    return StoreCount(open=len(still), not_acted=sum(1 for a in still if a.acted_at is None))


def _closed_because(alert: Any, row: StyleColour | None) -> str | None:
    """Why an open alert closes now, or ``None`` while it is still broken."""
    from stockledger.broken_size_models import BrokenSizeAlert

    if row is None:
        return BrokenSizeAlert.Closed.SOLD_OUT
    if row.rule is None:
        return BrokenSizeAlert.Closed.NO_RULE
    if row.broken:
        return None
    changed = (
        list(row.rule.core_sizes) != list(alert.core_sizes)
        or row.rule.missing_percent != alert.rule_percent
    )
    return BrokenSizeAlert.Closed.RULE_CHANGED if changed else BrokenSizeAlert.Closed.FIXED


def _open_or_refresh(store: Store, row: StyleColour, alert: Any, now: datetime) -> Any:
    from core.tenancy import require_tenant_id
    from stockledger.broken_size_models import BrokenSizeAlert

    assert row.rule is not None and row.verdict is not None  # a broken row has both
    facts = {
        "colour": row.colour,
        "brand": row.brand,
        "style_code": row.style_code,
        "category": row.category,
        "core_sizes": list(row.rule.core_sizes),
        "missing_sizes": list(row.verdict.missing),
        "held": row.held,
        "pieces": row.pieces,
        "rule_percent": row.rule.missing_percent,
        "missing_percent": row.verdict.missing_percent,
        "checked_at": now,
    }
    if alert is None:
        return BrokenSizeAlert.objects.create(
            tenant_id=require_tenant_id(), site=store, style_id=row.style_id, opened_at=now, **facts
        )
    for name, value in facts.items():
        setattr(alert, name, value)
    alert.save(update_fields=list(facts))
    return alert


def _close(alert: Any, reason: str, now: datetime) -> None:
    alert.closed_at = now
    alert.closed_reason = reason
    alert.save(update_fields=["closed_at", "closed_reason"])


# ---------------------------------------------------------------------------
# The success measure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Measure:
    """Broken-size alerts acted on within 7 days of opening (§26, target 80% or more).

    Counted: every alert acted on within its 7 days (on time), and every alert
    whose 7 days ran out with no action (missed). Left out: an alert still inside
    its 7 days with no action yet, and one that closed by itself inside its 7 days
    before anybody acted, since there was nothing left to act on.
    """

    counted: int
    on_time: int
    #: Still inside their 7 days, not acted on yet.
    waiting: int
    #: Closed by themselves inside their 7 days, before anybody acted.
    closed_first: int

    @property
    def percent(self) -> int | None:
        return self.on_time * 100 // self.counted if self.counted else None

    def as_json(self) -> dict[str, Any]:
        return {
            "counted": self.counted,
            "on_time": self.on_time,
            "waiting": self.waiting,
            "closed_first": self.closed_first,
            "percent": self.percent,
        }


def outcome(
    opened_at: datetime, acted_at: datetime | None, closed_at: datetime | None, now: datetime
) -> str:
    """ "on_time", "missed", "waiting" or "closed_first" for one alert."""
    deadline = opened_at + ACT_WITHIN
    if acted_at is not None and acted_at <= deadline:
        return "on_time"
    if acted_at is None and closed_at is not None and closed_at <= deadline:
        return "closed_first"
    if now >= deadline or acted_at is not None:
        return "missed"
    return "waiting"


def measure(alerts: Iterable[Any], now: datetime) -> Measure:
    tally: dict[str, int] = defaultdict(int)
    for alert in alerts:
        tally[outcome(alert.opened_at, alert.acted_at, alert.closed_at, now)] += 1
    return Measure(
        counted=tally["on_time"] + tally["missed"],
        on_time=tally["on_time"],
        waiting=tally["waiting"],
        closed_first=tally["closed_first"],
    )


# ---------------------------------------------------------------------------
# For size balancing and markdown suggestions
# ---------------------------------------------------------------------------


def checked_stores() -> list[Store]:
    """The stores whose broken sizes are checked: active selling stores with the switch on."""
    from masters.store_feature_registry import BROKEN_SIZE
    from masters.store_features import feature, switch_states

    stores = list(Store.objects.filter(is_active=True, store_type=Store.StoreType.STORE))
    states = switch_states(stores, [feature(BROKEN_SIZE)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


def open_broken_sizes(store_ids: Iterable[int] | None = None) -> list[Any]:
    """The broken-size alerts open now, oldest first, for the stores asked about.

    Each is a ``BrokenSizeAlert``: the store, style, colour, category, the core
    sizes it was checked against, the ones missing and the pieces per size held.
    Size balancing (ticket 34) and markdown suggestions read these rather than
    working the rule out again, so what they act on is what the store was told.
    Only stores still checked (active, selling, switch on) are read: a store
    switched off since the last daily check hands nothing on.
    """
    from stockledger.broken_size_models import BrokenSizeAlert

    wanted = {store.pk for store in checked_stores()}
    if store_ids is not None:
        wanted &= set(store_ids)
    rows = BrokenSizeAlert.objects.filter(closed_at__isnull=True, site_id__in=sorted(wanted))
    return list(rows.select_related("site").order_by("opened_at", "id"))

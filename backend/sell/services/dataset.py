"""What the counter knows: the till's bootstrap and its delta feed (#179, D10).

The till bills **offline**. Everything it needs to price a scan, name a piece,
tax it and credit a salesman has to be sitting on the device before the network goes away - so this
module is the whole of the till's knowledge, and a row missing from it is a
counter that cannot sell a shirt it can see on the shelf.

Three shapes are worth reading before the code.

**The payload has no cost in it, by construction.** H2: a store person may know
the ticket price of everything and the cost of nothing. The hazard is not
carelessness, it is convenience - `StockOnHand` carries `net_value_paise` on the
very row the `stock` section is built from, and `Cohort` carries
`unit_cost_paise` on the row the `items` section is built from. So neither
section is built by serialising a model: both go through `values`/`values_list`
naming exactly the columns that may leave, which means a cost cannot ride along by
having been on the object. A test walks the finished payload and fails on any
cost-shaped key or number, and that test is the belt to this braces.

**Big sections are deltaed; small ones are sent whole.** Items and stock are
20,000 rows and get a watermark. The store's own registration, its tax slabs, its
salespeople, its manager PINs, the season master's ordering and the shop floor's
money dials are a handful of rows each: a delta over five rows saves nothing and
would need a deletion channel the contract does not give it, so they are replaced
wholesale on every response. `deleted` therefore names the two sections
that can lose a row invisibly - items and offers.

**The cursor deliberately laps backwards, and even so it is not the whole
guarantee.** `updated_at` is stamped when a row is written, not when its
transaction commits, so a cursor set to the request's own instant can step over a
row that was stamped before the request and landed after it - the classic
watermark hole, and the missed row is missed for ever. The till upserts every
section by key, so re-sending rows costs nothing and losing one costs a barcode
the counter cannot scan; the cursor is therefore held a long lap behind the clock,
sized against the longest write transaction this system has (see `CURSOR_LAP`).

That lap makes the hole very unlikely rather than impossible, so it is not what
the correctness rests on: **a bootstrap cannot miss anything by construction**, and
the till takes one at store open each day. Any row a delta could still lose is
recovered within the day, without anybody having to notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import User
from accounts.role_assignments import effective_assignments
from accounts.till_pin import may_hold_till_pin
from accounts.unified_policy import role_actions
from core.tenancy import require_tenant_id
from core.documents import DocStatus
from masters.consent_wording import current_wording
from masters.hsn import hsn_digits
from masters.models import Cohort, Customer, GstSlab, Season, Sku, Store
from masters.store_feature_registry import (
    ALTERATIONS,
    CUSTOMER_CONSENT,
    CUSTOMER_DISPLAY,
    CUSTOMER_RESERVATION,
    GIFT_VOUCHERS,
    MANAGER_PIN_OVERRIDES,
    ONLINE_ONLY_REFUSALS,
    SAVED_SIZES,
    SPECIAL_ORDERS,
    SPLIT_SALE,
)
from masters.store_features import is_feature_on
from masters.tax_settings import saved_versions, store_uses_settings
from offers.models import Offer
from sell.alteration_models import ALTERATION_DESCRIPTION, ALTERATION_GST_RATE, ALTERATION_SAC
from sell.models import CustomerErasure, Sale, SaleLine, SaleTender, SellPolicy
from sell.services.after_discount_check import after_discount_on
from sell.services.exchange_tax import return_tax_on
from sell.services.goods_stock import Shelf, is_goods_site, read_shelf
from sell.services.refunds import RETURNED_BANK_PAISE, RETURNED_PAISE, RETURNED_QTY
from sell.services.salespeople import active_salespeople
from sell.services.working_set import current_version
from stockledger.models import StockOnHand

#: How far behind the clock the returned cursor sits. See the module docstring for
#: why it laps at all; this is why it laps *this far*.
#:
#: The lap has to outlast the longest write transaction in the system, because a row
#: stamped at the start of one and committed at the end of it is invisible to any
#: cursor issued in between. The longest was the legacy PT inward posting (deleted
#: in OPS-18), a single `@transaction.atomic` walking a PT row by row and calling
#: `update_or_create` on `Sku` and `Cohort` for each - a 20,000-line PT is tens of
#: thousands of round trips in one transaction, minutes rather than seconds. A
#: quarter of an hour has real headroom over that.
#:
#: The cost of the lap is a delta re-sending a quarter-hour of edits, which is a
#: handful of rows the till upserts. That is the cheap side of an unfair trade.
CURSOR_LAP = timedelta(minutes=15)


class TillScopeError(Exception):
    """This login is not a till. Carries the sentence the person should read."""


def resolve_till_store(user: Any) -> Store:
    """The one store this dataset is about, or a refusal.

    Deliberately the caller's *scope* rather than the top-bar switcher. The till
    is a store login by construction (contract, step 3): the payload names one
    GSTIN, one shelf and one set of override PIN hashes, so a person who can see
    two stores has no honest dataset - and letting the switcher pick would hand
    any multi-store login a store's PIN hashes by choosing a unit in a dropdown.
    """
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or getattr(user, "human_id", None) is None
        or getattr(user, "tenant_id", None) != require_tenant_id()
    ):
        raise TillScopeError("This login has no active counter assignment.")
    rows = [
        row for row in effective_assignments(user.human_id)
        if row.role.code == "store_person"
        and row.all_brands
        and "section.sell.operate" in role_actions(row.role)
    ]
    if any(row.all_sites for row in rows):
        raise TillScopeError(
            "This login can operate every store, so it cannot be a till. A counter "
            "signs in for its own store."
        )
    ids = sorted({int(site_id) for row in rows for site_id in row.site_ids})
    if len(ids) != 1:
        where = "no store" if not ids else f"{len(ids)} stores"
        raise TillScopeError(
            f"This login is scoped to {where}, and a till is one store. Ask an "
            "administrator for a counter login at this store."
        )
    store = Store.objects.filter(
        tenant_id=user.tenant_id, id=ids[0], is_active=True
    ).select_related("gstin").first()
    if store is None:
        raise TillScopeError("This login's store is closed. A closed store has no counter.")
    return store


@dataclass(frozen=True)
class Sync:
    """One request for the till's world: whose counter, and how far back.

    These three travelled as loose arguments to every section builder, and the
    middle one carried its meaning in a null check - `if since is not None` reads
    as a missing value where what it means is "this is a delta, not a bootstrap".
    `is_bootstrap` says that, and `today` rides along because every section that
    judges a deadline has to judge it against the *same* day.
    """

    store: Store
    #: The caller's cursor; `None` is a bootstrap (see `_read_cursor`).
    since: datetime | None
    #: The store-local day this whole answer is judged against.
    today: date

    @property
    def is_bootstrap(self) -> bool:
        return self.since is None

    @property
    def from_moment(self) -> datetime:
        """The cursor, for the delta arms that only run when there is one.

        A property rather than a cast at each site: mypy has to be told that a
        delta has a cursor, and telling it eight times is eight chances to tell it
        wrongly.
        """
        assert self.since is not None, "a bootstrap has no cursor to read from"
        return self.since


def build_dataset(store: Store, since_raw: str) -> dict[str, Any]:
    """The till's whole world, or what changed in it since `since_raw`.

    One `timezone.now()` for the whole answer, taken before any query: it is both
    the day the deadline sections are judged against and the base of the cursor
    handed back.

    An unreadable `since` is a bootstrap, not a refusal - see `_read_cursor`.
    """
    from sell.services.online import (
        commercial_marks,
        commercial_revision,
        online_alpha,
        remember_commercial_revision,
        selling_policy,
    )
    marks = None
    if online_alpha(store):
        from masters.goods_services import require_sell_ready
        require_sell_ready(store)
        # Taken before the reads, so a change during the build moves the marks
        # and the next check rebuilds rather than trusting this revision.
        marks = commercial_marks(store)
    started = timezone.now()
    sync = Sync(store=store, since=_read_cursor("" if online_alpha(store) else since_raw), today=timezone.localdate(started))
    shelf = read_shelf(store, started) if is_goods_site(store) else None

    offers_live, offers_withdrawn = _offers(sync)
    # Asked once: the flag and the rows sent under it must agree, or a till would
    # replace its whole list with a delta.
    customers_whole = _customers_whole(sync)
    payload = {
        "selling_mode": "online_alpha" if online_alpha(store) else "historical",
        "cursor": _stamp(started - CURSOR_LAP),
        "full": sync.is_bootstrap,
        # The number this shelf was read at, for a goods-v1 store. A till quoting
        # a different one is sent the stock section whole (see `sell.services.
        # working_set`); a legacy store has no such number and says so with a null.
        "working_set_version": (current_version(store.pk) if shelf is not None else None),
        "store": {
            "tenant_id": str(store.tenant_id),
            "site_id": str(store.pk),
            "code": store.code,
            "gstin": store.gstin.gstin,
            "state_code": store.gstin.state_code,
        },
        "items": _goods_items(shelf) if shelf is not None else _items(sync),
        "stock": _goods_stock(shelf) if shelf is not None else _stock(sync),
        # The bills an exchange may be taken against offline (§10.3, §10.4). Sent
        # whole every time, like the other small sections: a bill's returnable
        # quantity changes when *another* bill gives a piece back, and a watermark
        # over the sold bill would never notice. Only where the store's shelf is a
        # goods one - a legacy till's exchange path is unchanged by this ticket.
        "bills": _bills(store, sync.today) if shelf is not None else [],
        "gst_slabs": _gst_slabs(),
        # Ticket 03: every saved tax settings version, future-dated ones too, so
        # an offline counter reaches a new version on its date with the one it
        # holds. `rules_on` is this store's switch; off, the till keeps
        # version 1 (the slabs above) and records that on each bill (B5).
        "tax_settings": _tax_settings(store),
        # Ticket 05: whether this counter refuses online-only work while offline
        # (credit notes as payment, B2B bills and exchanges against them). On for
        # every store unless Admin switches it off (B4); the till holds the answer
        # so it can refuse with no network.
        "online_only_refusals": is_feature_on(store, ONLINE_ONLY_REFUSALS),
        # Ticket 06: whether a manager's PIN must come from somebody other than
        # the cashier. Only a yes or no - the till already holds the PIN hashes
        # it checks against (`managers` below), and nothing more leaves for it.
        "manager_pin_rules": is_feature_on(store, MANAGER_PIN_OVERRIDES),
        # Ticket 08: whether this counter may split a line between two
        # salespeople. Held by the till so it works offline.
        "split_sale": is_feature_on(store, SPLIT_SALE),
        # Ticket 09: whether this counter offers a customer display. Held by the
        # till so the button follows the switch without asking the server.
        "customer_display": is_feature_on(store, CUSTOMER_DISPLAY),
        # Ticket 15: whether this counter asks for consent, and the wording it
        # asks with. Held by the till so it can ask, and record the wording
        # version, with no network.
        "customer_consent": is_feature_on(store, CUSTOMER_CONSENT),
        # Ticket 20: whether this counter offers "Reservation pickup". The pickup
        # itself is online only and asks head office for the reservation.
        "customer_reservation": is_feature_on(store, CUSTOMER_RESERVATION),
        # Ticket 18: whether this counter shows the customer's saved sizes. The
        # sizes themselves are read online when a number is on the bill.
        "saved_sizes": is_feature_on(store, SAVED_SIZES),
        # Ticket 21: whether this counter offers "Special order collection". The
        # collection itself is online only and asks head office for the order.
        "special_orders": is_feature_on(store, SPECIAL_ORDERS),
        # Ticket 19: whether this counter sells and takes gift vouchers. Both are
        # online only and ask head office for the voucher.
        "gift_vouchers": is_feature_on(store, GIFT_VOUCHERS),
        # Ticket 22: whether this counter offers "Alteration charge", and the
        # line it makes - the code and rate come from here, never the till's own
        # copy. Null when off. The charge bills offline like any line.
        "alteration_charge": (
            {
                "sac": ALTERATION_SAC,
                "gst_rate": f"{ALTERATION_GST_RATE}",
                "description": ALTERATION_DESCRIPTION,
            }
            if is_feature_on(store, ALTERATIONS)
            else None
        ),
        "consent_wording": current_wording(store.tenant_id).as_json(),
        # Each row carries its own `starts_on`/`ends_on`, so the counter starts
        # and stops an offer on its own clock while offline (grill Q3) - the
        # dates ride inside the data rather than being applied by this query.
        "offers": offers_live,
        # Ticket 07: the staff list is the only salesperson list, at every store.
        "salespeople": _salespeople(store),
        # A till built before ticket 07 reads this key and would send a staff id
        # where it expects a number, so it is sent empty: such a till can no
        # longer pick anybody new. It may still default a line to the old id it
        # remembers; the server takes that onto the old row's frozen copy
        # (`SellerBook`), which is matched to the person, so the bill is kept
        # and credited. Drop once no such till is left.
        "salesmen": [],
        "managers": _managers(store),
        "seasons": _seasons(),
        "policy": selling_policy(store).as_till_policy() if online_alpha(store) else _policy(),
        "customers": _customers(sync, whole=customers_whole),
        # Ticket 16: an erasure since the cursor sends the customer list whole,
        # and the till replaces its copy - the one way a row leaves it.
        "customers_whole": customers_whole,
        "deleted": {
            "items": _withdrawn_items(sync),
            "offers": offers_withdrawn,
        },
    }

    payload["commercial_revision"] = commercial_revision(payload)
    if online_alpha(store) and marks is not None:
        remember_commercial_revision(store, marks, payload["commercial_revision"])
    return payload


def _stamp(moment: datetime) -> str:
    """A cursor, in the one format this endpoint reads back."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_cursor(raw: str) -> datetime | None:
    """The caller's cursor, or `None` meaning "start from nothing".

    **Anything unreadable is a bootstrap, never a refusal.** The cursor is opaque
    and ours; a till holding a damaged one cannot repair it, and a GET whose 400
    the queue retries for ever is worse than re-sending 20,000 rows once.

    Both failure shapes have to be caught, and only one of them looks like a
    failure: `parse_datetime` answers `None` for something that is not a timestamp
    at all ("yesterday-ish") but *raises* `ValueError` for something correctly
    shaped and impossible ("2026-02-30T00:00:00Z"). Catching only the first is how
    the self-heal this docstring promises would be true of one damaged cursor and a
    500 for another.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        moment = parse_datetime(text)
    except ValueError:
        return None
    if moment is None:
        return None
    # A cursor is always one we minted, so it always carries `Z`. A naive one can
    # only be a hand-typed or mangled cursor; UTC is the reading that cannot make
    # the delta *skip* rows, whatever the server's local zone is.
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment, UTC)


def working_set_items(store: Store, day: date) -> list[dict[str, Any]]:
    """Every piece this site's counter can scan and price, in the till's own shape.

    The one statement of "the working set", published because a second screen now
    has to ask what it contains: Running Offers lists the pieces each live rule
    reaches (PRD §11), and that list is only worth anything if it is drawn over
    the very rows the counter prices with. A screen that read the shelf its own
    way would be a second answer waiting to disagree with a bill.

    Which records answer depends on the site's stock contract and on nothing
    else: a goods-v1 site's accepted shelf, a legacy site's cohort registry over
    what the ledger says it holds. Both come back as the same item row, because
    the counter is the same counter either way.

    A bootstrap read, never a delta: the caller is asking what is there now.
    """
    if is_goods_site(store):
        return _goods_items(read_shelf(store))
    return _items(Sync(store=store, since=None, today=day))


def _at_store(store: Store) -> QuerySet[StockOnHand]:
    """The shelf: every barcode this store has ever held, whatever it holds now.

    A row at nought is not a row to drop. The piece can walk back in as an
    exchange, and the till still has to name and price it when it does.
    """
    return StockOnHand.objects.filter(store=store)


def _items(sync: Sync) -> list[dict[str, Any]]:
    """One row per sellable (barcode, season) this store has held.

    A season is a buying cohort, so one barcode bought twice is two rows with two
    prices, and the till resolves the scan to the older one (A2). Inactive SKUs
    are absent: head office withdrawing a piece must mean the counter stops
    offering it, and a delta says so out loud through `deleted.items`.
    """
    rows = Cohort.objects.filter(
        barcode__in=_at_store(sync.store).values("sku_code"), sku__is_active=True
    )
    if not sync.is_bootstrap:
        # Three ways an item row can be new *to this till*, and the third is the
        # one a naive delta misses: a cohort bought a season ago at another store,
        # arriving here today, moves no master row at all - only the stock
        # projection. Without it the till would receive a quantity for a barcode
        # it cannot describe or price.
        arrived_here = _at_store(sync.store).filter(updated_at__gt=sync.from_moment)
        rows = rows.filter(
            Q(updated_at__gt=sync.from_moment)
            | Q(sku__updated_at__gt=sync.from_moment)
            | Q(barcode__in=arrived_here.values("sku_code"))
        )
    # `values`, not the model: `Cohort.unit_cost_paise` is the piece's cost (the
    # PT's P RATE) and must never be fetched onto an object this function then
    # serialises. Naming the columns is what makes H2 structural.
    fields = (
        "barcode",
        "season",
        "sku__design",
        "sku__brand",
        "sku__item",
        "sku__size",
        "sku__color",
        "sku__hsn",
        "mrp_paise",
        "sku__mrp_paise",
        "sku__no_discount",
    )
    return [
        {
            "barcode": row["barcode"],
            "season": row["season"],
            "design": row["sku__design"],
            "brand": row["sku__brand"],
            "item": row["sku__item"],
            "size": row["sku__size"],
            "color": row["sku__color"],
            "hsn": row["sku__hsn"],
            "mrp_paise": _ticket_price(row["mrp_paise"], row["sku__mrp_paise"]),
            "no_discount": row["sku__no_discount"],
        }
        for row in rows.order_by("barcode", "season").values(*fields)
    ]


def _ticket_price(cohort_mrp: int | None, sku_mrp: int | None) -> int | None:
    """The MRP printed on this buying lot's tag, or `null` if nobody knows one.

    **Null, never nought.** A PT that quotes no MRP registers the SKU with none
    (`_register_identity` stores `None` deliberately), so an unpriced piece is a
    real thing that reaches a shelf - and a zero here would be a till pricing that
    scan at ₹0, billing it at ₹0, and posting ₹0 of revenue and tax against a
    garment that walked out of the shop. `null` says "this needs a price from a
    human" in a way no number can.

    The fallback is `or` rather than a null test on purpose: a stored nought on the
    cohort is not knowledge either, and must not shadow a good price on the SKU.
    """
    return cohort_mrp or sku_mrp or None


def _withdrawn_items(sync: Sync) -> list[str]:
    """Barcodes this till must stop offering, by barcode.

    Only a delta answers anything here: a fresh bootstrap has nothing cached to
    remove, and an inactive piece is simply absent from `items` instead.

    Deactivating a SKU is the only withdrawal that exists. A cohort is a record of
    a purchase and is never unmade, and a stock row falls to nought rather than
    vanishing - so the barcode, not the (barcode, season) pair, is the identity a
    removal travels under, and it takes every season of that piece with it.
    """
    if sync.is_bootstrap:
        return []
    return list(
        Sku.objects.filter(
            barcode__in=_at_store(sync.store).values("sku_code"),
            is_active=False,
            updated_at__gt=sync.from_moment,
        )
        .order_by("barcode")
        .values_list("barcode", flat=True)
    )


def _stock(sync: Sync) -> list[dict[str, int | str]]:
    """Quantity per barcode. Quantity, and nothing else (H2).

    `net_value_paise` sits on this very row, which is why the two columns that may
    leave are named rather than a serialiser being pointed at the model.
    """
    rows = _at_store(sync.store)
    if not sync.is_bootstrap:
        rows = rows.filter(updated_at__gt=sync.from_moment)
    return [
        {"barcode": sku_code, "qty": net_qty}
        for sku_code, net_qty in rows.order_by("sku_code").values_list("sku_code", "net_qty")
    ]


def _goods_items(shelf: Shelf) -> list[dict[str, Any]]:
    """The same row the legacy `items` section sends, read off the goods records.

    Field for field the legacy shape, because the counter is the same counter: it
    scans a barcode, resolves it to a season, prices it off the ticket and taxes it
    off the HSN. What changed underneath is only where those four facts come from.

    The SKU's governed `no_discount` veto travels with its stable identity.
    Approved origin MRP and HSN remain frozen business inputs; labels do not
    establish either identity or price.
    """
    return [
        {
            "sku_id": str(piece.sku_id),
            "brand_id": piece.brand_id,
            "barcode": piece.barcode,
            "season": piece.season,
            "design": piece.dims["design"],
            "brand": piece.dims["brand"],
            "item": piece.dims["item"],
            "size": piece.dims["size"],
            "color": piece.dims["color"],
            "hsn": piece.hsn,
            "mrp_paise": piece.mrp_paise,
            "no_discount": piece.no_discount,
            # OPS-03's explicit unknown historical season, carried so the counter
            # can label it rather than showing a customer-facing cashier a code.
            "season_unknown_historical": piece.season_unknown_historical,
        }
        for piece in shelf.pieces
    ]


def _goods_stock(shelf: Shelf) -> list[dict[str, int | str]]:
    """Sellable quantity per (barcode, season). Quantity, and nothing else (H2).

    Sent whole every time, never deltaed. Goods stock has no `updated_at` to
    watermark - a piece becomes sellable when an acceptance event names it, which
    moves no row this section reads - so the honest answer is the whole section
    plus the store's `working_set_version`, which tells a till whether its copy is
    the current one. One store's sellable shelf is a small list; a watermark over
    it would buy nothing and could lose a barcode.
    """
    return [
        {"barcode": barcode, "season": season, "qty": qty}
        for (barcode, season), qty in sorted(shelf.quantities.items())
    ]


def _gst_slabs() -> list[dict[str, Any]]:
    """Every dated slab, always whole - the till taxes on its own clock.

    Sent in full even on a delta, and deliberately including slabs whose date has
    not arrived: a rate change announced today and effective in October has to be
    on the device before the counter reaches October offline (Rule 11, dates ride
    inside the data).
    """
    fields = ("hsn_prefix", "threshold_paise", "rate_below", "rate_above", "effective_from")
    return [
        {
            "hsn_prefix": hsn_prefix,
            "threshold_paise": int(threshold_paise),
            "rate_below": _rate(rate_below),
            "rate_above": _rate(rate_above),
            "effective_from": effective_from.isoformat(),
        }
        for hsn_prefix, threshold_paise, rate_below, rate_above, effective_from in (
            GstSlab.objects.order_by("effective_from", "hsn_prefix").values_list(*fields)
        )
    ]


def _tax_settings(store: Store) -> dict[str, Any]:
    """The saved tax versions and whether this store is taxed by them (ticket 03)."""
    return {
        "rules_on": store_uses_settings(store),
        "versions": [version.as_json() for version in saved_versions(store.tenant_id)],
        # Ticket 11: whether this counter prices with GST after discount. Held
        # with the tax settings so an offline till keeps pricing the same way.
        "after_discount": after_discount_on(store),
        # Ticket 12: what an HSN looks like (digits, of these lengths). Any
        # other code is "no HSN" and matches no rule, on the till as here.
        "hsn_digits": hsn_digits(),
        # Ticket 13: whether this counter takes pieces back by the exchange and
        # return tax rules. Held with the tax settings (whose versions carry the
        # cross-GSTIN choice and the filing dates) so an offline till keeps them.
        "return_tax": return_tax_on(store),
    }


def _rate(rate: Decimal) -> str:
    """A tax rate as the two-decimal string the contract quotes.

    A string rather than a float on purpose: the till back-calculates tax out of
    an MRP-inclusive price, and a rate that arrived as 4.999999999 would put the
    counter and the server a paise apart on every line.
    """
    return f"{Decimal(rate):.2f}"


def _seasons() -> list[dict[str, Any]]:
    """The season master's own ordering, so the counter can pick the oldest.

    A barcode is a scan-alias, not an identity (A2): the same tag under two buying
    cohorts is two lots at two ticket prices, and a scan that does not name a
    season resolves to the **oldest live** one with stock here. `resolve_piece`
    makes that choice server-side by ranking `(is_closed, sort_order)` from this
    master - and the till has to make the identical choice offline, because the
    season it picks is the season it writes on the line and the accept pipeline
    honours an exact `(barcode, season)` outright. Without the ordering on the
    device the till would fall back to sorting names, and "FW25 before SS26" is
    true only by the accident of the alphabet.

    Whole on every response, like the slabs and the manager list: it is a handful
    of rows, a season closing is a fact the counter must not miss, and `deleted`
    has no channel for it.
    """
    # `historical_unknown` rides along so the counter can *label* the one
    # explicit unknown historical cohort (OPS-03). The offer engine does not
    # read it - both twins name the sentinel themselves, because neither reads
    # a master - but a screen showing "UNKNOWN-HIST" to a customer-facing
    # cashier would be showing them a code, not a fact.
    fields = ("code", "name", "status", "sort_order", "historical_unknown")
    return [
        {
            "code": code,
            "name": name,
            "status": status,
            "sort_order": sort_order,
            "historical_unknown": historical_unknown,
        }
        for code, name, status, sort_order, historical_unknown in Season.objects.order_by(
            "sort_order", "code"
        ).values_list(*fields)
    ]


def _policy() -> dict[str, str | bool | int]:
    """The dials the counter has to hold offline.

    The cap is B2: below it a manual discount is the cashier's to give, above it
    the bill will not close without a manager. Both ends of that rule have to
    agree, and only one of them is online. A till that did not know the number
    would let a cashier key in a discount the accept pipeline refuses - days later,
    when the bill is printed, paid for and in a customer's hand.

    So it rides down whole on every response, and as a two-decimal **string** for
    the reason the tax rates are: the till multiplies by it, and a rate that
    arrived as 7.499999 would put the counter and the server on opposite sides of
    a cap.
    """
    policy = SellPolicy.current()
    return {
        **policy.as_till_policy(),
        "return_window_days": policy.return_window_days,
        "cached_bill_days": policy.cached_bill_days,
    }


def _bills(store: Store, today: date) -> list[dict[str, Any]]:
    """This store's recent bills, so an exchange can be taken with no network (§10.3).

    The window is a policy dial (`cached_bill_days`, 30 by default), and what
    rides down is exactly what an exchange needs: which pieces the bill sold, what
    the customer actually paid for each, the tax inside that, what has already
    come back off it, and how it was paid. **Never cost, margin or origin value** -
    the same H2 rule the shelf sections keep, and the same reason: a store person
    knows the ticket price of everything and the cost of nothing.

    Built by naming columns rather than by serialising a `SaleLine`, for the
    reason the module docstring gives about `Cohort.unit_cost_paise`: this model
    carries `unit_cost_paise`, `goods_allocations` and a cost book on the very row
    the lines come from, and a serialiser is how one of them leaves the building.

    Return legs are sent as the lines they are but are not returnable themselves -
    they are pieces that have already come back - so the till filters them exactly
    as it filters a server bill's legs today (`original.fromServer`).
    """
    from sell.services.refunds import with_returned

    # The window opens at midnight of the store's business day (IST, §9.2), as
    # one aware instant: `today` is a local date, so the edge must be too, never
    # a date the database works out in whatever zone its session happens to use.
    from sell.services.online import online_alpha
    since = today - timedelta(days=30 if online_alpha(store) else max(0, SellPolicy.current().cached_bill_days))
    opens_at = timezone.make_aware(datetime.combine(since, time.min))
    sales = (
        Sale.objects.filter(store=store, billed_at__gte=opens_at)
        .exclude(lines__kind=SaleLine.Kind.GOODS, lines__brand_ref_id__isnull=True)
        .exclude(docstatus=DocStatus.CANCELLED)
        .order_by("-billed_at", "-id")
    )
    lines_by_sale: dict[int, list[dict[str, Any]]] = {}
    line_rows = with_returned(SaleLine.objects.filter(sale__in=sales)).values(
        "sale_id",
        "line_no",
        "direction",
        "kind",
        "barcode",
        "season",
        "design",
        "color",
        "size",
        "brand",
        "item",
        "hsn",
        "qty",
        "mrp_paise",
        "net_paise",
        "gst_rate",
        "gst_paise",
        "manual_desc",
        RETURNED_QTY,
        RETURNED_PAISE,
        RETURNED_BANK_PAISE,
    )
    for row in line_rows:
        lines_by_sale.setdefault(int(row["sale_id"]), []).append(
            {
                "line_no": int(row["line_no"]),
                "direction": row["direction"],
                # Ticket 22: so an offline till never offers a charge back.
                "kind": row["kind"],
                "barcode": row["barcode"],
                "season": row["season"] or "",
                "design": row["design"] or "",
                "color": row["color"] or "",
                "size": row["size"] or "",
                "brand": row["brand"] or "",
                "item": row["item"] or "",
                "hsn": row["hsn"] or "",
                "qty": int(row["qty"]),
                "mrp_paise": int(row["mrp_paise"] or 0),
                "net_paise": int(row["net_paise"] or 0),
                "gst_rate": _rate(row["gst_rate"] or Decimal("0")),
                "gst_paise": int(row["gst_paise"] or 0),
                "manual_desc": row["manual_desc"] or "",
                "returned_qty": int(row[RETURNED_QTY] or 0),
                "returned_paise": int(row[RETURNED_PAISE] or 0),
                # Ticket 13 (B60): what earlier returns took off the line's
                # bank-offer share; the share itself is spread from `tenders`.
                "returned_bank_paise": int(row[RETURNED_BANK_PAISE] or 0),
            }
        )
    tenders_by_sale: dict[int, list[dict[str, Any]]] = {}
    for row in SaleTender.objects.filter(sale__in=sales).values("sale_id", "mode", "amount_paise"):
        tenders_by_sale.setdefault(int(row["sale_id"]), []).append(
            {"mode": row["mode"], "amount_paise": int(row["amount_paise"])}
        )
    bills: list[dict[str, Any]] = []
    for row in sales.values(
        "id",
        "fy",
        "till_seq",
        "doc_number",
        "till_number",
        "billed_at",
        "customer_name",
        "customer_mobile",
        "buyer_gstin",
        "net_paise",
    ):
        bills.append(
            {
                "fy": row["fy"],
                "till_seq": int(row["till_seq"]),
                "doc_number": row["doc_number"] or "",
                "till_number": row["till_number"] or "",
                "billed_at": row["billed_at"].isoformat(),
                "customer_name": row["customer_name"] or "",
                "customer_mobile": row["customer_mobile"] or "",
                # Ticket 05: an offline exchange against a B2B bill is refused,
                # so the counter has to know which cached bills are B2B.
                "buyer_gstin": row["buyer_gstin"] or "",
                "net_paise": int(row["net_paise"] or 0),
                "lines": sorted(
                    lines_by_sale.get(int(row["id"]), []), key=lambda line: line["line_no"]
                ),
                "tenders": tenders_by_sale.get(int(row["id"]), []),
            }
        )
    return bills


def _offer_is_for(offer: Offer, store_code: str, today: date) -> bool:
    """Should this counter be holding this rule at all?

    Three ways a rule stops being this till's business, and the till can only see
    the last of them for itself: head office stopped it, head office took this
    store off it, or its end date passed. The dates are still sent down and still
    judged at the counter (grill Q3) - this is about what is worth sending, not
    about who decides when an offer stops.
    """
    if offer.status != Offer.Status.LIVE:
        return False
    # Upper-cased on both sides: `validate_store_scope` normalises what it
    # stores, `Store.code` is a slug with no normalisation of its own, and a
    # lower-case store code would otherwise send this counter an empty rulebook
    # while the dashboard and the server still found its offers.
    if store_code.upper() not in ((offer.store_scope or {}).get("stores") or []):
        return False
    return offer.ends_on is None or offer.ends_on >= today


def _offers(sync: Sync) -> tuple[list[dict[str, Any]], list[int]]:
    """The rulebook this counter prices with, and the rules it must forget.

    Two things make this awkward for the same underlying reason - a rule can stop
    mattering without anybody writing to its row:

      · **A rule dies of a date.** `ends_on` passes and nothing is stamped, so a
        delta also asks which rules crossed their own end date between the cursor
        and today. A till that was offline over a weekend would otherwise go on
        discounting under a promotion that finished on the Saturday.
      · **A rule can be taken off *this store* rather than stopped.** That edit
        does stamp `updated_at`, but it also drops the row out of any query
        narrowed by store - so the delta would never mention it again and the
        till would keep it for ever. The delta therefore scans the changed rules
        across the network and decides store membership per row, which is the
        only ordering that can report a withdrawal at all.

    A bootstrap has nothing cached, so it reports nothing withdrawn and simply
    omits what it will not send.
    """
    code = sync.store.code.upper()
    rows = Offer.objects.for_tenant(sync.store.tenant_id).select_related("brand")
    if sync.is_bootstrap:
        live = [
            offer
            for offer in rows.filter(status=Offer.Status.LIVE, store_scope__stores__contains=[code])
            .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=sync.today))
            .order_by("priority", "id")
        ]
        return [offer.as_rule_payload() for offer in live], []

    changed = rows.filter(
        Q(updated_at__gt=sync.from_moment)
        | Q(ends_on__gte=timezone.localdate(sync.from_moment), ends_on__lt=sync.today)
    ).order_by("priority", "id")

    sending: list[dict[str, Any]] = []
    withdrawn: list[int] = []
    for offer in changed:
        if _offer_is_for(offer, code, sync.today):
            sending.append(offer.as_rule_payload())
        else:
            # Sent even for a rule this store never held: an id the till does not
            # know is a delete that does nothing, and the alternative - guessing
            # which store scope a rule used to have - cannot be done from here.
            withdrawn.append(offer.id)
    return sending, withdrawn


def _salespeople(store: Store) -> list[dict[str, Any]]:
    """This store's salespeople from the staff list - the per-line picker (ticket 07).

    Staff marked as salespeople and active at this store now, by name. Sent whole
    on every response, so somebody who moves or leaves drops out of the picker
    on the next sync. Only the id and the name: the picker shows a name and the
    bill sends the id back, and nothing else about a person leaves for a shop
    floor device (overall PRD §10.4).
    """
    return [
        {"id": str(row.staff_id), "name": row.display_name} for row in active_salespeople(store)
    ]


def _customers(sync: Sync, *, whole: bool) -> list[dict[str, Any]]:
    """Everybody KDPS has ever billed, by mobile - the counter's phone book (#245).

    **Deliberately not narrowed to this store**, and it is the only section here
    that is not. Every other store-owned list is scoped because shipping another
    shop's shelf or its override PINs would be wrong; a customer is the opposite
    case - a Deoghar regular walking into Ranchi has to be recognised there, so
    the list is all-KDPS (grill Q6). Scoping it would look consistent with the
    sections above it and quietly make the typeahead useless at the second store
    somebody visits.

    Three fields and no fourth: a mobile to find them by, a name to print, and a
    GSTIN so a business bill does not have to be keyed in twice. No purchase
    history rides down - the till is a device on a shop floor, and what a
    customer has ever spent is not a question it should be able to answer.

    Deltaed like items rather than sent whole like the salespeople, because this list
    is the one that mostly grows: it is the whole business's customers, not a
    handful of rows, and re-sending it every five minutes would cost every till
    the entire book to learn about one new shopper.

    A row leaves only when a customer is erased at their request (ticket 16), and
    a watermark cannot say a row went. So there is no `deleted` channel naming
    customers - it would have to keep the number the customer asked to be rid
    of - and instead the list is sent whole whenever an erasure happened since
    the cursor (`_customers_whole`), for the till to replace its copy.
    """
    rows = Customer.objects.all()
    if not whole:
        rows = rows.filter(updated_at__gt=sync.from_moment)
    return [
        {"mobile": mobile, "name": name, "gstin": gstin}
        for mobile, name, gstin in rows.order_by("mobile").values_list("mobile", "name", "gstin")
    ]


def _customers_whole(sync: Sync) -> bool:
    """Is the customer list sent whole: a bootstrap, or an erasure since the cursor."""
    if sync.is_bootstrap:
        return True
    return CustomerErasure.objects.filter(erased_at__gt=sync.from_moment).exists()


def _managers(store: Store) -> list[dict[str, Any]]:
    """Who may authorise an over-cap discount at this counter with the line cut.

    Only a current Store Person assignment at this site, covering every brand
    and holding `sell >= approve`, may put a PIN hash on this till. A blank hash
    is not a credential and cannot be sent.

    That sentence lives in `accounts.till_pin` rather than here, because the
    endpoint a manager sets their PIN through has to refuse exactly the people
    this list would refuse to ship.

    Sent whole every time and never deltaed: it is a handful of rows, and the one
    thing that must never happen is a till holding a stale copy - a rung
    withdrawn at head office is withdrawn on the next request (Anand's ruling, 30
    Jul), and the counter's copy has to follow.

    Note what the seeded matrix means for this list today: the ratified sheet
    predates the POS and gives both store seats `sell: operate`, so out of the box
    no store person reaches the rung and this list is empty until an administrator
    grants it in the editor. That is a decision about the sheet, not about this
    code.
    """
    candidates = (
        User.objects.filter(tenant_id=store.tenant_id, is_active=True)
        .exclude(till_pin_hash="")
        .order_by("id")
    )
    return [
        {
            "user_id": user.id,
            "name": user.full_name or user.username,
            "till_pin_hash": user.till_pin_hash,
        }
        for user in candidates
        if may_hold_till_pin(user, site_id=store.pk)
    ]

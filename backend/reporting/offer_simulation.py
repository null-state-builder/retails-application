"""Offer simulation: what a draft offer would have cost (ticket 30, ST-OFR-3, §16).

Before an offer is approved, it is priced against real bills from **the last 4
weeks** and from **the same 4 weeks last year**. Each past bill is put through
the counter's own offer engine (``offers.resolution.resolve``) with this offer
alone, and the answer is the estimated discount, the pieces it would touch and,
for viewers who may see cost, the margin effect. Every figure is an estimate and
says so.

**Never slows a bill.** It reads only the reporting copy of sold lines
(``report_offer_sim_line_fact``, brought up to date by ``offer_sim_facts`` on
the worker's clock), never a billing table, and takes no lock. A period with
more bills than ``KDPS_OFFER_SIM_MAX_BILLS`` is refused rather than cut short.

**Scope on the server.** A run covers the offer's stores that are in the
runner's scope and where the switch is on. The result is kept per store, so a
later reader is shown only their own stores' figures (``read``), and cost and
margin only if ``sees_cost`` allows it.

**Audited.** Each run is one ``offers.simulation.run`` audit entry with the
previous run's headline as *before* and this run's as *after* (no margin in
either: the audit log is read by people who may not see cost).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal
from masters.models import Sku, Store
from masters.store_feature_registry import OFFER_SIMULATION
from masters.store_features import feature, switch_states
from offers.models import Offer
from offers.resolution import (
    ADD_ON_LAYERS,
    Cart,
    CartLine,
    normalise,
    resolve,
)
from ptmapper.goods_workbook import Amount, workbook_bytes
from reporting.base import (
    Missing,
    ReportScope,
    freshness,
    percent,
    principal_for,
    record_export,
    sees_cost,
    viewer_stores,
)
from reporting.models import OfferSimLineFact, OfferSimulation
from reporting.offer_sim_facts import KEY as FACTS_KEY

FEATURE_KEY = OFFER_SIMULATION
FORMULA_VERSION = "offer-simulation/1"
AUDIT_ACTION = "offers.simulation.run"

#: 4 weeks, and a year back as 52 weeks so the same weekdays are compared.
WINDOW_DAYS = 28
YEAR_BACK = timedelta(weeks=52)

#: Figures kept per store per period; all integers, summed on read.
FIGURES = (
    "bills",
    "bills_affected",
    "pieces",
    "mrp_paise",
    "discount_paise",
    "actual_disc_paise",
    "gifts",
    # Cost and margin: only ever sent to a viewer ``sees_cost`` allows.
    "cost_paise",
    "costed_taxable_paise",
    "actual_costed_taxable_paise",
    "uncosted_pieces",
)
COST_FIGURES = frozenset(
    {"cost_paise", "costed_taxable_paise", "actual_costed_taxable_paise", "uncosted_pieces"}
)

BASIS = [
    "An estimate, not a forecast. The offer as it now stands is priced by the counter's own "
    "offer engine on each past bill, on its own, as if no other offer had been running.",
    "Last 4 weeks = the 28 days up to yesterday. Same 4 weeks last year = the same 28 days "
    "52 weeks earlier, so the same weekdays are compared.",
    "Only pieces sold are read; returns and exchanges are not taken off. A piece marked "
    "'no discount' is never discounted.",
    "Pieces affected = pieces the offer would discount. Estimated discount = what it would "
    "take off them. Discount actually given = what those same pieces were really discounted.",
    "Customers may buy differently when an offer runs; the estimate does not guess at that.",
    "Offers are priced as at a store with 'GST after discount' switched off. Where it is on, "
    "a buy 2 get 1 may be spread over the pieces differently, and a bank offer is recorded "
    "as a payment rather than a discount.",
]
COST_BASIS = (
    "Margin = the price after the estimated discount, less GST at the rate on the bill, less "
    "the cost frozen on the line at billing, over pieces whose cost is known. Margin effect = "
    "that margin less the margin the same pieces actually made."
)


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    date_from: date
    date_to: date

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
        }


def periods(today: date) -> list[Period]:
    """The two periods an offer is judged on (Anand, B9)."""
    last_to = today - timedelta(days=1)
    last_from = today - timedelta(days=WINDOW_DAYS)
    return [
        Period("last_4_weeks", "Last 4 weeks", last_from, last_to),
        Period("last_year", "Same 4 weeks last year", last_from - YEAR_BACK, last_to - YEAR_BACK),
    ]


# -- which stores ----------------------------------------------------------------


@dataclass(frozen=True)
class SimStores:
    #: The offer's stores in the caller's scope where the switch is on.
    stores: list[Store]
    #: The offer's stores in the caller's scope where it is switched off.
    switched_off: list[Store]
    #: How many of the offer's stores are outside the caller's scope.
    outside: int


def _offer_codes(offer: Offer) -> set[str]:
    return {str(code).upper() for code in (offer.store_scope or {}).get("stores") or []}


def sim_stores(user: Any, offer: Offer, feature_key: str = FEATURE_KEY) -> SimStores:
    """The offer's stores as ``user`` sees them, split by ``feature_key``'s switch
    (ticket 31's return on each offer asks the same question of its own switch)."""
    codes = _offer_codes(offer)
    mine = [store for store in viewer_stores(user) if store.code.upper() in codes]
    on = {state.site_id for state in switch_states(mine, [feature(feature_key)]) if state.enabled}
    return SimStores(
        stores=[store for store in mine if store.pk in on],
        switched_off=[store for store in mine if store.pk not in on],
        outside=len(codes) - len(mine),
    )


def _refuse_scope(found: SimStores) -> None:
    if not found.stores and not found.switched_off:
        raise Refusal(
            "SCOPE_DENIED",
            "None of this offer's stores is in your scope, so there are no bills to "
            "simulate it on.",
            status=403,
        )
    if not found.stores:
        raise Refusal(
            "FEATURE_OFF",
            f"{feature(FEATURE_KEY).name} is switched off at every store of this offer you "
            "can see. Admin can switch it on in Setup, Feature Switches.",
            status=403,
        )


# -- the run ----------------------------------------------------------------------


def run(user: Any, offer: Offer, *, today: date | None = None) -> OfferSimulation:
    """Simulate a draft offer on past bills, keep the result, and audit it."""
    if offer.status != Offer.Status.DRAFT:
        raise Refusal(
            "NOT_DRAFT",
            "An offer is simulated before it is approved; this one is "
            f"{offer.get_status_display().lower()}.",
            status=409,
        )
    found = sim_stores(user, offer)
    _refuse_scope(found)
    clock = time.monotonic()
    judged = periods(today or timezone.localdate())
    store_ids = [store.pk for store in found.stores]
    # Both sizes first, so a refused second period never wastes the first.
    for period in judged:
        _check_size(period, store_ids)
    # How fresh the copy was, kept with the run so a reader is told later.
    notes = Missing()
    as_of = freshness(FACTS_KEY, notes)
    never = _never_discounted()
    result = {
        "periods": [
            {**period.as_dict(), "stores": _simulate(offer, period, store_ids, never)}
            for period in judged
        ],
        "stores": [_store(store) for store in found.stores],
        "rule": rule_key(offer),
        "notes": notes.items,
    }
    took = int((time.monotonic() - clock) * 1000)
    simulation = OfferSimulation(
        offer=offer,
        offer_updated_at=offer.updated_at,
        run_by=user if getattr(user, "pk", None) else None,
        as_of=as_of,
        formula_version=FORMULA_VERSION,
        took_ms=took,
        result=result,
    )
    _save_audited(user, offer, simulation, found.stores)
    return simulation


def rule_key(offer: Offer) -> str:
    """A fingerprint of what the estimate depends on: the rule and its stores.

    Not ``updated_at``: approving an offer saves it without changing what it
    gives, and an estimate must not turn stale for that.
    """
    payload = offer.as_rule_payload()
    for key in ("id", "name", "starts_on", "ends_on"):
        payload.pop(key, None)
    payload["stores"] = sorted(_offer_codes(offer))
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _never_discounted() -> frozenset[str]:
    """Pieces flagged 'no discount' as they stand now (a masters read, not billing)."""
    return frozenset(Sku.objects.filter(no_discount=True).values_list("barcode", flat=True))


def _check_size(period: Period, store_ids: list[int]) -> None:
    bills = (
        OfferSimLineFact.objects.filter(
            store_id__in=store_ids, day__range=(period.date_from, period.date_to)
        )
        .values("bill_id")
        .distinct()
        .count()
    )
    most = int(settings.KDPS_OFFER_SIM_MAX_BILLS)
    if bills > most:
        raise Refusal(
            "PERIOD_TOO_LARGE",
            f"{period.label} holds {bills:,} bills at these stores, more than the "
            f"{most:,} a simulation reads. Narrow the offer's stores, or ask Admin to "
            "raise the limit.",
            status=400,
        )


def _blank() -> dict[str, int]:
    return dict.fromkeys(FIGURES, 0)


def _simulate(
    offer: Offer, period: Period, store_ids: list[int], never: frozenset[str]
) -> dict[str, dict[str, int]]:
    """Each store's figures for one period, keyed by store id (as text, for JSON)."""
    figures: dict[int, dict[str, int]] = defaultdict(_blank)
    base = OfferSimLineFact.objects.filter(
        store_id__in=store_ids, day__range=(period.date_from, period.date_to)
    )
    for row in base.values("store_id").annotate(bills=Count("bill_id", distinct=True)):
        figures[row["store_id"]]["bills"] = int(row["bills"])
    # The rule, open on every day: the question is what it would have given on
    # that bill, not whether it was running then.
    rule = replace(offer.as_rule(), starts_on=date.min, ends_on=None)
    lines = base
    if offer.brand is not None:
        # Exact, not a shortcut: a brand offer covers only its own brand's lines,
        # and its slabs are measured on the lines it covers.
        lines = lines.filter(brand_key=normalise(offer.brand.name))
    for bill in _bills(lines):
        _price(bill, rule, figures, never)
    return {str(store_id): dict(values) for store_id, values in figures.items()}


#: What one line of the copy the engine and the sums need; read as plain rows.
LINE_FIELDS = (
    "bill_id",
    "store_id",
    "day",
    "line_no",
    "brand",
    "item",
    "design",
    "size",
    "color",
    "barcode",
    "season",
    "qty",
    "mrp_paise",
    "disc_paise",
    "net_paise",
    "gst_paise",
    "gst_rate",
    "cost_paise",
)


def _bills(lines: Any) -> Iterator[list[dict[str, Any]]]:
    """The lines, one bill at a time, as plain rows (no model instances)."""
    bill: list[dict[str, Any]] = []
    rows = lines.order_by("bill_id", "line_no").values(*LINE_FIELDS)
    for line in rows.iterator(chunk_size=5000):
        if bill and line["bill_id"] != bill[0]["bill_id"]:
            yield bill
            bill = []
        bill.append(line)
    if bill:
        yield bill


def _price(
    bill: list[dict[str, Any]],
    rule: Any,
    figures: dict[int, dict[str, int]],
    never: frozenset[str],
) -> None:
    head = bill[0]
    cart = Cart(
        lines=tuple(
            CartLine(
                line_no=line["line_no"],
                brand=line["brand"],
                item=line["item"],
                design=line["design"],
                size=line["size"],
                color=line["color"],
                barcode=line["barcode"],
                season=line["season"],
                qty=line["qty"],
                mrp_paise=int(line["mrp_paise"]),
                no_discount=line["barcode"] in never,
            )
            for line in bill
        ),
        day=head["day"],
    )
    outcome = resolve(cart, [rule])
    by_line = outcome.by_line()
    store = figures[head["store_id"]]
    touched = False
    for line in bill:
        found = by_line.get(line["line_no"])
        discount = found.discount_paise if found else 0
        if discount <= 0:
            continue
        touched = True
        qty = int(line["qty"])
        gross = int(line["mrp_paise"]) * qty
        store["pieces"] += qty
        store["mrp_paise"] += gross
        store["discount_paise"] += discount
        store["actual_disc_paise"] += int(line["disc_paise"])
        if line["cost_paise"] is None:
            store["uncosted_pieces"] += qty
            continue
        store["cost_paise"] += int(line["cost_paise"])
        store["costed_taxable_paise"] += _taxable(gross - discount, line["gst_rate"])
        store["actual_costed_taxable_paise"] += int(line["net_paise"]) - int(line["gst_paise"])
    if outcome.entitlements:
        touched = True
        store["gifts"] += len(outcome.entitlements)
    if touched:
        store["bills_affected"] += 1


def _taxable(inclusive_paise: int, rate: Any) -> int:
    """The value inside a GST-inclusive amount at ``rate`` percent, half up."""
    base = Decimal(inclusive_paise) * 100 / (100 + Decimal(str(rate or 0)))
    return int(base.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _store(store: Store) -> dict[str, Any]:
    return {"id": store.pk, "code": store.code, "name": store.name}


# -- the audit entry ----------------------------------------------------------------


def headline(simulation: OfferSimulation | None) -> dict[str, Any] | None:
    """What an audit entry says about a run: no cost, no margin."""
    if simulation is None:
        return None
    result = simulation.result or {}
    out: dict[str, Any] = {
        "offer": simulation.offer_id,
        "stores": [store["code"] for store in result.get("stores") or []],
        "formula_version": simulation.formula_version,
    }
    for period in result.get("periods") or []:
        total = _sum(period["stores"].values())
        out[period["key"]] = {
            "date_from": period["date_from"],
            "date_to": period["date_to"],
            "bills": total["bills"],
            "bills_affected": total["bills_affected"],
            "pieces": total["pieces"],
            "discount_paise": total["discount_paise"],
        }
    return out


def _save_audited(
    user: Any, offer: Offer, simulation: OfferSimulation, stores: list[Store]
) -> None:
    """Keep the run and its audit entry together.

    Held at no one store (``site_id`` None): *before* is the previous run, which
    may have covered other stores, so a store-scoped audit reader must not get it.
    """
    after = headline(simulation)

    def handler(run_: CommandRun) -> CommandResult:
        # Read inside the command, so two runs at once each quote the one before.
        previous = OfferSimulation.objects.filter(offer=offer).first()
        simulation.save()
        run_.audit_before = headline(previous)
        run_.audit_after = after
        return CommandResult(resource_type="offer_simulation", resource_id=str(simulation.pk))

    execute_command(
        principal_for(user, stores[0].tenant_id),
        CommandSpec(
            AUDIT_ACTION,
            uuid.uuid4(),
            after,
            resource_ids=[f"offer:{offer.pk}"],
            subject_key=f"offer_simulation:{offer.pk}",
        ),
        handler,
    )


# -- the read -------------------------------------------------------------------------


def _sum(rows: Iterable[dict[str, int]]) -> dict[str, int]:
    total = _blank()
    for row in rows:
        for key in FIGURES:
            total[key] += int(row.get(key) or 0)
    return total


def _row(period: dict[str, Any], figures: dict[str, int], show_cost: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": period["key"],
        "label": period["label"],
        "date_from": period["date_from"],
        "date_to": period["date_to"],
        "bills": figures["bills"],
        "bills_affected": figures["bills_affected"],
        "pieces": figures["pieces"],
        "mrp_paise": figures["mrp_paise"],
        "discount_paise": figures["discount_paise"],
        "discount_pct": percent(figures["discount_paise"], figures["mrp_paise"]),
        "actual_disc_paise": figures["actual_disc_paise"],
        "gifts": figures["gifts"],
    }
    if show_cost:
        costed = figures["pieces"] - figures["uncosted_pieces"]
        cost = figures["cost_paise"]
        margin = figures["costed_taxable_paise"] - cost if costed else None
        actual = figures["actual_costed_taxable_paise"] - cost if costed else None
        row.update(
            {
                "cost_paise": cost if costed else None,
                "margin_paise": margin,
                "margin_pct": (
                    None if margin is None else percent(margin, figures["costed_taxable_paise"])
                ),
                "actual_margin_paise": actual,
                "margin_effect_paise": (
                    None if margin is None or actual is None else margin - actual
                ),
                "uncosted_pieces": figures["uncosted_pieces"],
            }
        )
    return row


def _note_stores(missing: Missing, found: SimStores, ran_at: set[int], shown: list[Store]) -> None:
    """Say which of the viewer's stores the estimate leaves out, and why."""
    if not shown:
        missing.add(
            "NOT_YOUR_STORES",
            "This estimate was made on none of your stores. Run it again to include them.",
        )
    elif len(shown) < len(found.stores):
        names = ", ".join(f"{s.name} ({s.code})" for s in found.stores if s.pk not in ran_at)
        missing.add("NOT_RUN_AT", f"Not included: {names}; run it again to include them.")
    if found.switched_off:
        names = ", ".join(f"{s.name} ({s.code})" for s in found.switched_off)
        missing.add(
            "SWITCHED_OFF",
            f"Not included: {names}, where offer simulation is switched off.",
        )
    if found.outside:
        missing.add(
            "OUTSIDE_SCOPE",
            f"{found.outside} of this offer's stores are outside your scope and not shown.",
        )


def _note_run(missing: Missing, offer: Offer, simulation: OfferSimulation, stale: bool) -> None:
    """Say what about the offer or the run makes the estimate less than it looks."""
    if stale:
        missing.add(
            "STALE",
            "The offer was changed after this estimate was made. Run it again to see the "
            "offer as it now stands.",
        )
    if offer.layer in ADD_ON_LAYERS and not offer.combinable:
        missing.add(
            "NOT_STACKING",
            f"This {offer.get_layer_display().lower()} is not set to stack on other offers, "
            "and the counter applies an add-on only when it stacks, so it would give nothing.",
        )


def _period_rows(
    result: dict[str, Any], mine: set[int], show_cost: bool, missing: Missing
) -> list[dict[str, Any]]:
    """Each period's figures over the viewer's stores only."""
    rows = []
    for period in result.get("periods") or []:
        figures = _sum(
            values for key, values in (period.get("stores") or {}).items() if int(key) in mine
        )
        rows.append(_row(period, figures, show_cost))
        if not figures["bills"]:
            missing.add(
                f"NO_BILLS_{period['key'].upper()}",
                f"{period['label']}: no bills at these stores are in the system for "
                f"{period['date_from']} to {period['date_to']}.",
            )
        if show_cost and figures["uncosted_pieces"]:
            missing.add(
                f"UNCOSTED_{period['key'].upper()}",
                f"{period['label']}: {figures['uncosted_pieces']} affected piece(s) have no "
                "cost yet; margin leaves them out.",
            )
        if figures["gifts"]:
            missing.add("GIFTS", "Gift pieces are counted, not valued.")
    return rows


def _never_run(offer: Offer, found: SimStores, show_cost: bool) -> dict[str, Any]:
    """The answer before the first run: nothing estimated yet, said plainly."""
    missing = Missing()
    missing.add("NOT_RUN", "This offer has not been simulated yet.")
    _note_stores(missing, found, set(), found.stores)
    return {
        "report": "offer-simulation",
        "title": "Offer simulation (estimate)",
        "estimate": True,
        "formula_version": FORMULA_VERSION,
        "offer": offer.pk,
        "simulation": None,
        "run_at": None,
        "run_by": "",
        "as_of": None,
        "stale": False,
        "stores": [_store(store) for store in found.stores],
        "basis": [*BASIS, COST_BASIS] if show_cost else list(BASIS),
        "missing": missing.items,
        "shows_cost": show_cost,
        "periods": [],
    }


def read(user: Any, offer: Offer) -> dict[str, Any]:
    """The newest simulation of ``offer``, narrowed to the viewer's own stores."""
    found = sim_stores(user, offer)
    _refuse_scope(found)
    simulation = OfferSimulation.objects.filter(offer=offer).select_related("run_by").first()
    show_cost = sees_cost(user)
    if simulation is None:
        return _never_run(offer, found, show_cost)
    result = simulation.result or {}
    ran_at = {int(store["id"]) for store in result.get("stores") or []}
    shown = [store for store in found.stores if store.pk in ran_at]
    stale = result.get("rule") != rule_key(offer)
    missing = Missing()
    for item in result.get("notes") or []:
        missing.add(item["code"], item["text"])
    _note_run(missing, offer, simulation, stale)
    _note_stores(missing, found, ran_at, shown)
    rows = _period_rows(result, {store.pk for store in shown}, show_cost, missing)
    run_by = simulation.run_by
    return {
        "report": "offer-simulation",
        "title": "Offer simulation (estimate)",
        "estimate": True,
        "formula_version": simulation.formula_version,
        "offer": offer.pk,
        "simulation": simulation.pk,
        "run_at": simulation.run_at,
        "run_by": (getattr(run_by, "full_name", "") or run_by.get_username()) if run_by else "",
        "as_of": simulation.as_of,
        "stale": stale,
        "stores": [_store(store) for store in shown],
        "basis": [*BASIS, COST_BASIS] if show_cost else list(BASIS),
        "missing": missing.items,
        "shows_cost": show_cost,
        "periods": rows,
    }


# -- the spreadsheet (§17: every report exports to .xlsx) ------------------------------

#: The rows of the sheet, as the offer page draws them; ``cost`` rows only for
#: viewers who were sent cost.
SHEET_ROWS: tuple[tuple[str, str, str, bool], ...] = (
    ("bills", "Bills looked at", "number", False),
    ("bills_affected", "Bills the offer would touch", "number", False),
    ("pieces", "Pieces affected", "number", False),
    ("mrp_paise", "MRP of those pieces", "money", False),
    ("discount_paise", "Estimated discount", "money", False),
    ("discount_pct", "Estimated discount, % of MRP", "number", False),
    ("actual_disc_paise", "Discount actually given on them", "money", False),
    ("gifts", "Gifts earned", "number", False),
    ("margin_paise", "Margin with the offer (estimate)", "money", True),
    ("margin_pct", "Margin with the offer, %", "number", True),
    ("actual_margin_paise", "Margin those pieces made", "money", True),
    ("margin_effect_paise", "Margin effect", "money", True),
)


def export(user: Any, offer: Offer) -> bytes:
    """The newest estimate as one sheet: what it is, its basis and gaps, then the figures.

    Built from ``read``, so it carries exactly what this viewer may see, and the
    export is recorded in the audit log as every report export is.
    """
    body = read(user, offer)
    if body["simulation"] is None:
        raise Refusal("NOT_FOUND", "This offer has not been simulated yet.", status=404)
    stamp = body["as_of"]
    head: list[list[Any]] = [
        [f"{body['title']}: {offer.name}"],
        ["Estimate", "Not a forecast; see the basis below."],
        ["Stores", ", ".join(f"{s['name']} ({s['code']})" for s in body["stores"])],
        ["Run", timezone.localtime(body["run_at"]).strftime("%Y-%m-%d %H:%M")],
        [
            "Bills as of",
            timezone.localtime(stamp).strftime("%Y-%m-%d %H:%M") if stamp else "Not built yet",
        ],
        ["Formula version", body["formula_version"]],
    ]
    head += [["Basis", line] for line in body["basis"]]
    head += [["Missing data", item["text"]] for item in body["missing"]] or [
        ["Missing data", "None"]
    ]
    table: list[list[Any]] = [
        [
            "Estimate",
            *(f"{p['label']} ({p['date_from']} to {p['date_to']})" for p in body["periods"]),
        ]
    ]
    for key, label, kind, cost in SHEET_ROWS:
        if cost and not body["shows_cost"]:
            continue
        cells = [period.get(key) for period in body["periods"]]
        table.append([label, *(_cell(value, kind) for value in cells)])
    content = workbook_bytes("Offer simulation", [*head, [], *table])
    stores = [
        store for store in viewer_stores(user) if store.pk in {s["id"] for s in body["stores"]}
    ]
    record_export(
        user,
        report="offer-simulation",
        scope=ReportScope(
            user=user,
            options=stores,
            stores=stores,
            date_from=min(date.fromisoformat(p["date_from"]) for p in body["periods"]),
            date_to=max(date.fromisoformat(p["date_to"]) for p in body["periods"]),
        ),
        detail={"offer": offer.pk, "simulation": body["simulation"]},
    )
    return content


def _cell(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind != "money":
        return value
    # ``Amount`` holds exact non-negative paise; a loss (a negative margin effect)
    # is written as exact rupees instead.
    paise = int(value)
    return Amount(paise) if paise >= 0 else Decimal(paise) / 100

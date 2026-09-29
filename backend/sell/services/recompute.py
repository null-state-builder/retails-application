"""What the rulebook says a bill should have cost - the server's own answer.

Two callers, and they want the same number for different reasons.

The **discount cap** (contract step 6) has to know how much of a line's discount
the rulebook is answerable for, because the rest is a cashier's own and B2 caps
that. Before this module existed `_rulebook_saving` returned nought and said why:
the obvious source is the till's `offer_evidence.saved_paise`, and the till is
the party the cap exists to constrain. A cap the capped party can lift by
describing its own discount as an offer is not a cap. So the server resolves the
cart itself, and the number it gets is the only one that counts.

The **daily applied-vs-rulebook check** (D5 Q10, B3) wants the same resolution to
compare against what was actually charged, and to raise `offer_mismatch` where
the two disagree by more than a rupee on a line. That one never blocks: the bill
is printed and the customer has gone.

**The rulebook is read as of the bill's own day, not today.** This is the whole
reason the two uses can share one function without the first becoming a hazard.
A counter bills offline under the rules it holds; by the time the bill syncs, an
offer may have ended and another started. Resolving against *today's* rulebook
would refuse a bill the store priced honestly - `DISCOUNT_OVER_CAP` on a receipt
already in a customer's hand, with the whole queue stopped behind it. So an
`ended` rule is still consulted for a bill printed inside its dates: it was
running when that bill was printed, and that is the only question being asked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import date
from typing import Any
import uuid

from django.db.models import Q

from core.tenancy import require_tenant_id
from masters.models import Sku
from offers.models import Offer
from offers.resolution import Cart, CartLine, Resolution, Rule, resolve
from sell.models import SaleLine
from sell.services.tax_rulebook import TaxRulebook

#: A rupee a line, matching the GST half of the same advisory step (B3). Below
#: this the two engines are agreeing and the difference is a rounding artefact
#: nobody can act on.
OFFER_TOLERANCE_PAISE = 100

#: How far a bill's tax may sit from the dated slab before it is worth a human's
#: time - the same rupee a line, for the same reason (B3, D5 Q10).
GST_TOLERANCE_PAISE = 100

#: Statuses a bill could have been priced under. A draft or a merely-approved
#: rule never reached a till, so it cannot explain a discount; an ended one very
#: well might have been running on the day.
BILLABLE_STATUSES = (Offer.Status.LIVE, Offer.Status.ENDED)


@dataclass(frozen=True)
class BillLine:
    """The little a bill line has to say for the rulebook to price it.

    Deliberately a plain record rather than `_PreparedLine` or `SaleLine`: this
    module sits below the accept pipeline, and taking its type would make the
    rulebook a dependency of the counter in the wrong direction.
    """

    line_no: int
    barcode: str
    season: str
    qty: int
    mrp_paise: int
    dims: Mapping[str, str]
    no_discount: bool = False


def _running_on(store_code: str, day: date, tenant_id: uuid.UUID | None = None) -> Any:
    """Rows a bill printed at this store on this day could have been priced under."""
    return (
        Offer.objects.for_tenant(tenant_id if tenant_id is not None else require_tenant_id())
        .filter(status__in=BILLABLE_STATUSES, starts_on__lte=day)
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=day))
        .filter(store_scope__stores__contains=[store_code.upper()])
        .select_related("brand")
    )


def rulebook_for(store_code: str, day: date, *, tenant_id: uuid.UUID | None = None) -> list[Rule]:
    """The rules that were running at this store on this day, in engine terms."""
    return [offer.as_rule() for offer in _running_on(store_code, day, tenant_id).order_by("priority", "id")]


def _cart_line(line: BillLine) -> CartLine:
    return CartLine(
        line_no=line.line_no,
        brand=line.dims.get("brand", ""),
        item=line.dims.get("item", ""),
        design=line.dims.get("design", ""),
        size=line.dims.get("size", ""),
        color=line.dims.get("color", ""),
        barcode=line.barcode,
        season=line.season,
        qty=line.qty,
        mrp_paise=line.mrp_paise,
        no_discount=line.no_discount,
    )


def resolve_bill(
    store_code: str,
    day: date,
    lines: Sequence[BillLine],
    rules: Sequence[Rule] | None = None,
    *,
    after_discount: bool = False,
    tenant_id: uuid.UUID | None = None,
) -> Resolution:
    """Price these lines against the store's rulebook as it stood on `day`.

    `rules` is the same rulebook, already fetched. The nightly check walks every
    bill a shop rang up on a day and they are all read against one book, so
    fetching it per bill would be a thousand identical queries on a busy Saturday.
    """
    cart = Cart(lines=tuple(_cart_line(line) for line in lines), day=day)
    return resolve(
        cart,
        list(rules) if rules is not None else rulebook_for(store_code, day, tenant_id=tenant_id),
        after_discount=after_discount,
    )


def credit_from_cited_rule(
    offer_id: Any,
    store_code: str,
    day: date,
    lines: Sequence[BillLine],
    line_no: int,
    *,
    after_discount: bool = False,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """What the rule this line *names* gives it - the second opinion the cap takes.

    The narrow question that keeps the discount cap from stopping a store's whole
    queue, without opening it to a till that simply says "an offer did this".

    The server reads its rulebook live; the till read its copy whenever it last
    synced. Between the two, head office can flip a piece's no-discount flag or
    edit the master data a rule aims by - and a bill priced honestly under the
    till's copy then looks, to the server, like a manual discount.
    Refusing it would be `DISCOUNT_OVER_CAP` on a receipt already in a customer's
    hand, with every bill behind it stuck in the queue: exactly the "block what
    the business has already absorbed" this pipeline exists not to do.

    So the cited rule is fetched and **re-run on its own**, and what it works out
    is the credit. That is an *amount*, not a permission: a till naming a real
    rule cannot thereby claim any discount it likes, only the one that rule
    actually produces. A rule's dials cannot have moved underneath it either -
    a live rule is frozen, and changing one authors a successor (`offers.views`).

    A fabricated citation earns nothing: a till cannot mint a rule, put its own
    store on one, or make one cover a brand it never named.

    Two deliberate narrowings. `no_discount` is forced off, because the question
    is whether the *rule* was about this piece and the AMM/NOD flag is a fact
    about the piece today, with no history - a piece flagged after a bill printed
    would otherwise turn that bill into a refusal, the same retrospective trap
    through a different column. And `offer_id` is the brand-layer winner by
    construction, so a saving that came only from a storewide or bank add-on
    earns no credit here; those are small percentages on top of a brand offer,
    and one large enough to blow a cap on its own is a bill for a manager.
    """
    if not offer_id:
        return 0
    offer = _running_on(store_code, day, tenant_id).filter(pk=offer_id).first()
    if offer is None:
        return 0
    cart = Cart(
        lines=tuple(replace(_cart_line(line), no_discount=False) for line in lines), day=day
    )
    outcome = resolve(cart, [offer.as_rule()], after_discount=after_discount).by_line().get(line_no)
    return outcome.discount_paise if outcome else 0


# --- the two advisory findings, shared by the accept and the nightly pass ----
#
# Both are computed off *written* `SaleLine` rows rather than off whatever the
# caller happens to hold, which is what lets the accept pipeline and the daily
# check (#188) ask the identical question. If they diverged, a bill would be
# passed at the counter and reported at midnight - or reported at the counter and
# forgotten at midnight - and neither is a check anybody would trust.


def sold_lines(rows: Sequence[SaleLine]) -> list[SaleLine]:
    """The lines these checks are about: pieces going out, never coming back.

    A return leg is priced at what the customer actually paid on the original bill
    (D2), tax and rule and all, so it deliberately carries the *old* answer.
    Measuring it against today's slab or today's rulebook would raise a finding on
    every return taken after a rate change - the daily check crying wolf about the
    one thing the design asked it to watch.
    """
    # Ticket 22: nor an alteration charge - a service at its own fixed rate that
    # no slab, rule or offer covers. The accept pipeline checks its shape.
    return [
        row
        for row in rows
        if row.direction != SaleLine.Direction.RETURN
        and getattr(row, "kind", SaleLine.Kind.GOODS) != SaleLine.Kind.ALTERATION
    ]


def gst_offenders(rows: Sequence[SaleLine], rulebook: TaxRulebook) -> list[dict[str, Any]]:
    """Lines whose tax is more than a rupee from what the bill's tax version says.

    The version is the one in force for that bill at that store (ticket 03): the
    dated slab (version 1) where the store's tax-settings switch is off, which is
    exactly the check this was before, and the saved per-HSN rule where it is on.
    """
    offenders = []
    for row in sold_lines(rows):
        expected = rulebook.line_tax(row.hsn, int(row.net_paise), int(row.qty)).split
        if abs(expected.gst_paise - int(row.gst_paise)) > GST_TOLERANCE_PAISE:
            offenders.append(
                {
                    "line_no": row.line_no,
                    "charged_paise": int(row.gst_paise),
                    "slab_paise": expected.gst_paise,
                    "slab_rate": str(expected.rate),
                    "tax_setting_version": rulebook.version,
                }
            )
    return offenders


def rule_missing_lines(rows: Sequence[SaleLine], rulebook: TaxRulebook) -> list[dict[str, Any]]:
    """Sold lines whose HSN no rule in the bill's saved tax version covers.

    Always empty on version 1, which has its own fallback and no rules to miss.
    Such a line was taxed at the version's "no rule" rate; this is the flag that
    says so (ticket 03: flagged, never guessed silently, never blocked).
    """
    return [
        {"line_no": row.line_no, "hsn": row.hsn, "tax_setting_version": rulebook.version}
        for row in sold_lines(rows)
        if rulebook.line_tax(row.hsn, int(row.net_paise), int(row.qty)).rule_missing
    ]


def offer_offenders(
    rows: Sequence[SaleLine],
    savings: Mapping[int, int],
    drift: Set[int] = frozenset(),
) -> list[dict[str, Any]]:
    """Lines the counter did not charge what the rulebook says it should have.

    The comparison is the *whole* discount against the rulebook's, not the till's
    `offer_evidence` against the rulebook's, and that is deliberate. A line where
    the counter gave nothing and the rulebook says the customer was owed ₹600 is
    exactly the finding this exists for, and comparing evidence with evidence
    would miss it - a till that applied no offer also writes no evidence, so the
    two would agree about nothing and report nothing.

    A cashier's own keyed-in discount on top is the one thing that would make this
    cry wolf, so it is subtracted out. Note the asymmetry with the accept
    pipeline's *cap*, which is the point rather than an inconsistency: the cap
    refuses to credit the till's own `saved_paise`, because a cap the capped party
    can lift is not a cap; this only decides whether a human should read the bill,
    and the till's account of itself is evidence towards that.
    """
    offenders = []
    for row in sold_lines(rows):
        expected = int(savings.get(row.line_no, 0))
        claimed = int((row.offer_evidence or {}).get("saved_paise") or 0)
        given = int(row.disc_paise or 0)
        keyed_in = max(given - max(claimed, expected), 0)
        charged = given - keyed_in
        if abs(charged - expected) > OFFER_TOLERANCE_PAISE or row.line_no in drift:
            offenders.append(
                {
                    "line_no": row.line_no,
                    "charged_paise": charged,
                    "rulebook_paise": expected,
                    "offer_id": row.offer_id,
                }
            )
    return offenders


def savings_for(
    store_code: str,
    day: date,
    rows: Sequence[SaleLine],
    rules: Sequence[Rule] | None = None,
    *,
    after_discount: bool = False,
) -> dict[int, int]:
    """What the rulebook says each of these written lines was owed.

    The nightly counterpart of the accept pipeline's `_server_resolution`, reading
    the bill back off its own rows instead of off a payload. `no_discount` is
    fetched live from the SKU master for the same reason it is there: it is the
    one input to the rulebook that does not live on the bill.
    """
    sold = sold_lines(rows)
    if not sold:
        return {}
    barcodes = [row.barcode for row in sold]
    never = set(
        Sku.objects.filter(barcode__in=barcodes, no_discount=True).values_list("barcode", flat=True)
    )
    lines = [
        BillLine(
            line_no=row.line_no,
            barcode=row.barcode,
            season=row.season,
            qty=int(row.qty),
            mrp_paise=int(row.mrp_paise),
            dims={
                "brand": row.brand,
                "item": row.item,
                "design": row.design,
                "size": row.size,
                "color": row.color,
            },
            no_discount=row.barcode in never,
        )
        for row in sold
    ]
    return {
        line_no: outcome.discount_paise
        for line_no, outcome in resolve_bill(
            store_code, day, lines, rules, after_discount=after_discount
        )
        .by_line()
        .items()
    }

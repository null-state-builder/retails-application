"""Demo bills, so the discount screens have something true to show.

Not a fixture: every bill goes through ``accept_sale`` — the same pipeline the
till uses — and every discount is **the engine's own answer**, asked for by
``resolve_bill`` before the bill is written. Nothing here invents a number the
rulebook would disagree with, which is the only way a seeded month can be read
as evidence rather than as decoration.

Four shapes, because those are the four the Discounts screen has to tell apart:

* the **offer applied** — a live rule priced the line, with the evidence a till
  would have written;
* the **offer missed** — an eligible line charged at full price, which is exactly
  what D11 §8's finder exists to surface;
* the **keyed-in** discount — money a person decided, inside the counter's cap,
  with the salesperson named;
* the **plain** bill — no discount at all, so the totals are not all discount.

Idempotent: the key is derived from store, slot and day, so a second run replays
instead of doubling the month.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.fiscal import financial_year
from masters.models import Cohort, Sku, Store
from sell.pricing import split_line
from sell.serializers import SaleWriteSerializer
from sell.services.accept import AcceptError, accept_sale
from sell.services.recompute import BillLine, resolve_bill, rulebook_for
from sell.services.resolve import slab_for
from sell.services.salespeople import active_salespeople
from stockledger.models import StockOnHand

#: A demo namespace, so these keys can never collide with a real till's.
NS = uuid.UUID("d11d11d1-0000-4000-8000-000000000011")

#: Well inside the head-office cap, and a number a person would actually key.
KEYED_IN_PCT = 5


class Command(BaseCommand):
    help = "Seed demo bills through the real sale pipeline (idempotent)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--days", type=int, default=24)

    def handle(self, *args: Any, **options: Any) -> None:
        today = timezone.localdate()
        span = max(int(options["days"]), 1)
        made, skipped = 0, 0
        for store in Store.objects.exclude(code__endswith="-WH").order_by("code"):
            actor = self._cashier(store)
            sellers = [row.staff_id for row in active_salespeople(store)]
            if actor is None or not sellers:
                self.stdout.write(f"  {store.code}: no till user or salesperson, skipped")
                continue
            for slot, plan in enumerate(self._plan(store, span, today)):
                payload = self._payload(store, sellers[slot % len(sellers)], slot, **plan)
                form = SaleWriteSerializer(data=payload)
                if not form.is_valid():
                    self.stderr.write(f"  {store.code} #{slot}: {form.errors}")
                    skipped += 1
                    continue
                try:
                    accept_sale(dict(form.validated_data), actor)
                    made += 1
                except AcceptError as exc:
                    self.stderr.write(f"  {store.code} #{slot}: {exc.message}")
                    skipped += 1
        self.stdout.write(self.style.SUCCESS(f"Demo bills: {made} accepted, {skipped} skipped."))
        self.stdout.write("  Now run: manage.py sell_daily_check --days 30")

    # -- what the rulebook says -----------------------------------------------
    def _priced(self, store: Store, day: date, limit: int = 40) -> list[dict[str, Any]]:
        """Every piece in the shop, with what the rulebook would give it today."""
        rules = rulebook_for(store.code, day)
        rows = list(StockOnHand.objects.filter(store=store, net_qty__gt=1).order_by("-net_qty"))
        flags = dict(
            Sku.objects.filter(barcode__in=[row.sku_code for row in rows]).values_list(
                "barcode", "no_discount"
            )
        )
        out: list[dict[str, Any]] = []
        for row in rows[:limit]:
            mrp = self._mrp(row)
            if mrp <= 0:
                continue
            line = BillLine(
                line_no=1,
                barcode=row.sku_code,
                season=row.season,
                qty=1,
                mrp_paise=mrp,
                dims={
                    "brand": row.brand,
                    "item": row.item,
                    "design": row.design,
                    "size": row.size,
                    "color": row.color,
                },
                no_discount=bool(flags.get(row.sku_code)),
            )
            outcome = resolve_bill(store.code, day, [line], rules).by_line().get(1)
            out.append(
                {
                    "row": row,
                    "mrp": mrp,
                    "disc": outcome.discount_paise if outcome else 0,
                    "offer_id": outcome.offer_id if outcome else None,
                    "evidence": dict(outcome.evidence) if outcome else {},
                }
            )
        return out

    def _plan(self, store: Store, span: int, today: date) -> list[dict[str, Any]]:
        """The month's four shapes, drawn from what this shop actually holds."""
        priced = self._priced(store, today)
        offered = [p for p in priced if p["disc"] > 0 and p["offer_id"]]
        plain = [p for p in priced if p["disc"] == 0]
        plan: list[dict[str, Any]] = []

        def when(step: int) -> date:
            return today - timedelta(days=(step * 3) % span)

        for index, piece in enumerate(offered[:4]):  # the rule did its job
            plan.append({**piece, "day": when(index), "note": ""})
        for index, piece in enumerate(offered[:2]):  # ...and twice it did not
            plan.append(
                {
                    **piece,
                    "disc": 0,
                    "offer_id": None,
                    "evidence": {},
                    "day": when(index + 4),
                    "note": "",
                }
            )
        for index, piece in enumerate(plain[:2]):  # a person's own latitude
            plan.append(
                {
                    **piece,
                    "disc": piece["mrp"] * KEYED_IN_PCT // 100,
                    "day": when(index + 6),
                    "note": "Regular customer",
                }
            )
        for index, piece in enumerate(plain[2:4]):  # and an ordinary full-price bill
            plan.append({**piece, "day": when(index + 8), "note": ""})
        return plan

    # -- the bill -------------------------------------------------------------
    def _payload(
        self,
        store: Store,
        seller: uuid.UUID,
        slot: int,
        *,
        row: Any,
        mrp: int,
        disc: int,
        offer_id: int | None,
        evidence: dict[str, Any],
        day: date,
        note: str,
    ) -> dict[str, Any]:
        net = mrp - disc
        split = split_line(net, 1, slab_for(row.hsn or "", day))
        at = timezone.make_aware(datetime.combine(day, time(hour=12, minute=(slot * 7) % 60)))
        line: dict[str, Any] = {
            "line_no": 1,
            "direction": "sale",
            "barcode": row.sku_code,
            "season": row.season,
            "qty": 1,
            "mrp_paise": mrp,
            "disc_paise": disc,
            "net_paise": net,
            "gst_rate": str(split.rate),
            "gst_paise": split.gst_paise,
            "salesperson": str(seller),
            "offer_evidence": {**evidence, "saved_paise": disc} if offer_id else {},
        }
        if offer_id:
            line["offer_id"] = offer_id
        if note:
            line["manual_desc"] = note
        tender = (
            {"mode": "upi", "amount_paise": net, "upi_state": "manual"}
            if slot % 2
            else {"mode": "cash", "amount_paise": net}
        )
        return {
            "idempotency_uuid": str(uuid.uuid5(NS, f"{store.code}-{slot}-{day}")),
            "store": store.code,
            "fy": financial_year(day),
            "till_seq": 9000 + slot,
            "origin": "offline",
            "billed_at": at.isoformat(),
            "customer": {"name": "", "mobile": "", "gstin": ""},
            "lines": [line],
            "tenders": [tender],
            "totals": {
                "gross_paise": mrp,
                "discount_paise": disc,
                "net_paise": net,
                "gst_paise": split.gst_paise,
                "round_paise": 0,
            },
        }

    # -- small answers --------------------------------------------------------
    def _cashier(self, store: Store) -> Any:
        """Somebody who could actually have been standing at this counter."""
        from accounts.models import User

        people = User.objects.filter(stores=store, is_active=True).order_by("id")
        return people.filter(role__code="store_person").first() or people.first()

    def _mrp(self, row: Any) -> int:
        cohort = Cohort.objects.filter(barcode=row.sku_code).order_by("-id").first()
        return int((cohort.mrp_paise if cohort else 0) or 0)

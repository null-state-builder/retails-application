"""Whole-system demo data seed: every module, every store, real code paths.

Builds on ``seed_foundation`` (roles/masters/users) and complements
``seed_outbound_demo`` (which stocks DEO + BANKA). This command:

1. Cleans test junk left behind by API test runs (ZZTEST* stores, ZZS*/ZZB*
   seasons/brands, orphan GSTINs, test.auto.* users and their roles).
2. Lays legacy opening stock in at ALL six locations across 10 brands, both
   commercial models (owned + SOR) and two seasons - without a receipt, the
   way the legacy ledger takes in stock that came on none (``_opening_stock``):
   an ``adjustment`` leg per piece, its ``Sku``/``Cohort`` masters and one
   balanced value voucher per batch. Until 23 September 2026 this stock came
   through the legacy receiving pipeline (booking → GRN → PT file →
   PT inward posting), which OPS-18 deleted; the pieces and quantities are the
   ones those receipts brought in, and no booking, GRN, PT file, vendor bill or
   payable is made for them any more (OPS-13). One booking still awaiting goods
   is seeded for the Bookings screen.
3. Seeds the outbound document zoo in every lifecycle state: transfers
   (received / shortfall + gap closure / in transit / cross-state / with
   damaged arrivals), damage flags (confirmed and pending), RTVs (defective,
   seasonal, draft), stock adjustments (auto-cleared, approved, pending),
   a V-flip, and stocktakes (applied variance + one mid-count).
4. Seeds money: vendor payments and cash/bank/UPI movements through
   ``finledger.posting`` so the GL stays balanced.

Everything goes through the same service functions the API uses, and the one
stock writer every legacy posting shares — no raw ledger writes — so ledgers,
projections and the trial balance stay honest.

Idempotent via a sentinel on the seeded booking's notes - the open order's,
which a database seeded by the receipt-based version carries too, so neither
stocks it twice. Safe to run on a dev or preview database; do NOT run against
live books.

Usage::

    python manage.py seed_demo_data
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError, OutputWrapper
from django.db import transaction

from accounts.models import Role, User
from approvals.models import ApprovalStatus
from approvals.services import approval_for, decide
from core.documents import VoucherSeries
from finledger.posting import post_cash_movement, post_vendor_payment, rupees_to_paise
from masters.models import Brand, Customer, Gstin, Season, Store
from offers.models import Offer
from outbound.counting import (
    apply_variance,
    open_session,
    open_stocktake,
    record_scans,
    submit_session,
)
from outbound.maker_checker import request_document_approval
from outbound.management.commands._opening_stock import lay_in_opening_stock
from outbound.models import (
    AdjustmentReason,
    GapReason,
    LogisticsRoute,
    ReturnToVendor,
    ReturnToVendorLine,
    ReturnType,
    StockAdjustment,
    StockAdjustmentLine,
    StockRequest,
    StockRequestLine,
    StockRequestSource,
    StoreTransfer,
    TransferReason,
    TransferType,
    TransportMode,
    VFlip,
    VFlipLine,
    WriteOff,
    WriteOffLine,
)
from outbound.posting import (
    fulfil_stock_request,
    mark_damaged,
    post_adjustment,
    post_gap_closure,
    post_rtv,
    post_transfer_dispatch,
    post_transfer_receipt,
    post_vflip,
    post_writeoff,
    raise_gap_closure,
    resolve_line_identity,
)
from stockledger.models import StockOnHand
from vendors.models import Booking, BookingLine, Vendor

SEED_TAG = "demo-wave2"
DOC_TYPES = (
    "GRN",
    "PT",
    "STO",
    "DMG",
    "GAP",
    "RTV",
    "ADJ",
    "WRO",
    "VFL",
    "SRQ",
    "SAL",
    "CRN",
    "SRT",  # selling (D10)
)
REAL_GSTINS = {"10AAACK1234M1Z5", "20AAACK1234M1Z3"}


def _fy() -> str:
    d = date.today()
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def _sku(
    barcode: str,
    design: str,
    size: str,
    color: str,
    brand: str,
    season: str,
    item: str,
    hsn: str,
    cost: float,
    mrp: float,
    qty: int,
) -> dict[str, Any]:
    return {
        "BARCODE": barcode,
        "DESIGN": design,
        "SIZE": size,
        "COLOR": color,
        "BRAND": brand,
        "SEASON": season,
        "ITEM": item,
        "HSN": hsn,
        "P RATE": f"{cost:.2f}",
        "BASIC": f"{cost:.2f}",
        "MRP": f"{mrp:.2f}",
        "NAG": qty,
    }


SS26 = "Spring/Summer 2026"
AW25 = "Autumn/Winter 2025"

# One batch per brand per location, laid in as legacy opening stock (OPS-13).
# Until 23 September 2026 each batch was a goods receipt - booking → GRN → PT →
# posted stock - and the quantities are what those receipts brought in, so
# every later step, and every spec written against this seed, finds the same
# pieces where it always did.
OPENING_STOCK: list[dict[str, Any]] = [
    {
        "tag": "lp-wh",
        "brand": "Louis Philippe",
        "store": "RAN-WH",
        "skus": [
            _sku(
                "LP-OXF-BLU-40",
                "Oxford Shirt",
                "40",
                "Blue",
                "Louis Philippe",
                SS26,
                "Shirt",
                "6205",
                1650,
                3999,
                30,
            ),
            _sku(
                "LP-CHN-KHA-34",
                "Slim Chino",
                "34",
                "Khaki",
                "Louis Philippe",
                SS26,
                "Trouser",
                "6203",
                1750,
                4299,
                24,
            ),
            _sku(
                "LP-TEE-NVY-M",
                "Crew Tee",
                "M",
                "Navy",
                "Louis Philippe",
                SS26,
                "T-Shirt",
                "6109",
                900,
                2199,
                36,
            ),
        ],
    },
    {
        "tag": "pe-wh",
        "brand": "Peter England",
        "store": "RAN-WH",
        "skus": [
            _sku(
                "PE-PLO-GRN-L",
                "Pique Polo",
                "L",
                "Green",
                "Peter England",
                SS26,
                "T-Shirt",
                "6109",
                520,
                1099,
                30,
            ),
            _sku(
                "PE-DNM-IND-32",
                "Slim Jeans",
                "32",
                "Indigo",
                "Peter England",
                SS26,
                "Jeans",
                "6203",
                980,
                2299,
                24,
            ),
            _sku(
                "PE-FRM-LAV-42",
                "Formal Shirt",
                "42",
                "Lavender",
                "Peter England",
                SS26,
                "Shirt",
                "6205",
                760,
                1699,
                30,
            ),
        ],
    },
    {
        "tag": "bb-wh",
        "brand": "Blackberrys",
        "store": "RAN-WH",
        "skus": [
            _sku(
                "BB-SUIT-CHR-42",
                "Two-Piece Suit",
                "42",
                "Charcoal",
                "Blackberrys",
                SS26,
                "Suit",
                "6203",
                5200,
                12999,
                12,
            ),
            _sku(
                "BB-SHRT-WHT-40",
                "Formal Shirt",
                "40",
                "White",
                "Blackberrys",
                SS26,
                "Shirt",
                "6205",
                950,
                2499,
                24,
            ),
        ],
    },
    {
        "tag": "mf-deo",
        "brand": "Mufti",
        "store": "DEO",
        "skus": [
            _sku(
                "MF-JEAN-BLK-32",
                "Slim Jeans",
                "32",
                "Black",
                "Mufti",
                SS26,
                "Jeans",
                "6203",
                1150,
                2799,
                16,
            ),
            _sku(
                "MF-TEE-OLV-L",
                "Graphic Tee",
                "L",
                "Olive",
                "Mufti",
                SS26,
                "T-Shirt",
                "6109",
                480,
                1199,
                20,
            ),
        ],
    },
    {
        "tag": "vh-deo",
        "brand": "Van Heusen",
        "store": "DEO",
        "skus": [
            _sku(
                "VH-BLZR-GRY-40",
                "Formal Blazer",
                "40",
                "Grey",
                "Van Heusen",
                SS26,
                "Blazer",
                "6203",
                2800,
                6999,
                8,
            ),
            _sku(
                "VH-SHRT-PNK-39",
                "Formal Shirt",
                "39",
                "Pink",
                "Van Heusen",
                SS26,
                "Shirt",
                "6205",
                820,
                1999,
                16,
            ),
        ],
    },
    {
        "tag": "as-bkr",
        "brand": "Allen Solly",
        "store": "BKR",
        "skus": [
            _sku(
                "AS-POLO-YLW-M",
                "Solly Polo",
                "M",
                "Yellow",
                "Allen Solly",
                SS26,
                "T-Shirt",
                "6109",
                640,
                1499,
                18,
            ),
            _sku(
                "AS-CHNO-BEI-34",
                "Casual Chino",
                "34",
                "Beige",
                "Allen Solly",
                SS26,
                "Trouser",
                "6203",
                1050,
                2599,
                14,
            ),
        ],
    },
    {
        "tag": "kl-bkr",
        "brand": "Killer",
        "store": "BKR",
        "skus": [
            _sku(
                "KL-JEAN-DKB-30",
                "Torn Jeans",
                "30",
                "Dark Blue",
                "Killer",
                AW25,
                "Jeans",
                "6203",
                1180,
                2899,
                16,
            ),
            _sku(
                "KL-JKT-BLK-L",
                "Denim Jacket",
                "L",
                "Black",
                "Killer",
                AW25,
                "Jacket",
                "6201",
                1500,
                3699,
                10,
            ),
        ],
    },
    {
        "tag": "usp-hzb",
        "brand": "U.S. Polo Assn.",
        "store": "HZB",
        "skus": [
            _sku(
                "USP-TEE-WHT-L",
                "Logo Tee",
                "L",
                "White",
                "U.S. Polo Assn.",
                SS26,
                "T-Shirt",
                "6109",
                520,
                1299,
                20,
            ),
            _sku(
                "USP-SHRT-CHK-42",
                "Check Shirt",
                "42",
                "Red Check",
                "U.S. Polo Assn.",
                SS26,
                "Shirt",
                "6205",
                900,
                2199,
                14,
            ),
        ],
    },
    {
        "tag": "spy-hzb",
        "brand": "Spykar",
        "store": "HZB",
        "skus": [
            _sku(
                "SPY-JEAN-GRY-32",
                "Skinny Jeans",
                "32",
                "Grey",
                "Spykar",
                AW25,
                "Jeans",
                "6203",
                1100,
                2699,
                15,
            ),
            _sku(
                "SPY-TEE-RED-M",
                "Round Neck Tee",
                "M",
                "Red",
                "Spykar",
                AW25,
                "T-Shirt",
                "6109",
                380,
                999,
                18,
            ),
        ],
    },
    {
        "tag": "jky-dum",
        "brand": "Jockey",
        "store": "DUM",
        "skus": [
            _sku(
                "JKY-TRK-NVY-L",
                "Track Pant",
                "L",
                "Navy",
                "Jockey",
                SS26,
                "Trouser",
                "6104",
                520,
                1299,
                12,  # 12 of the 24 booked arrived; the booking is not seeded any more
            ),
            _sku(
                "JKY-TEE-GRY-M",
                "Essential Tee",
                "M",
                "Grey",
                "Jockey",
                SS26,
                "T-Shirt",
                "6109",
                300,
                799,
                24,
            ),
        ],
    },
    {
        "tag": "pe-dum",
        "brand": "Peter England",
        "store": "DUM",
        "skus": [
            _sku(
                "PE-SHRT-WHT-39",
                "Formal Shirt",
                "39",
                "White",
                "Peter England",
                SS26,
                "Shirt",
                "6205",
                720,
                1599,
                14,
            ),
            _sku(
                "PE-TRSR-GRY-34",
                "Formal Trouser",
                "34",
                "Grey",
                "Peter England",
                SS26,
                "Trouser",
                "6203",
                1050,
                2499,
                12,
            ),
        ],
    },
    {
        "tag": "vh-banka",
        "brand": "Van Heusen",
        "store": "BANKA",
        "skus": [
            _sku(
                "VH-TRSR-NVY-34",
                "Formal Trouser",
                "34",
                "Navy",
                "Van Heusen",
                SS26,
                "Trouser",
                "6203",
                1150,
                2799,
                12,
            ),
            _sku(
                "VH-SHRT-WHT-42",
                "Formal Shirt",
                "42",
                "White",
                "Van Heusen",
                SS26,
                "Shirt",
                "6205",
                820,
                1999,
                16,
            ),
        ],
    },
    {
        "tag": "mf-banka",
        "brand": "Mufti",
        "store": "BANKA",
        "skus": [
            _sku(
                "MF-SHKT-DNM-40",
                "Denim Shacket",
                "40",
                "Indigo",
                "Mufti",
                AW25,
                "Jacket",
                "6201",
                1350,
                3299,
                10,
            ),
            _sku(
                "MF-CRGO-OLV-34",
                "Cargo Pant",
                "34",
                "Olive",
                "Mufti",
                AW25,
                "Trouser",
                "6203",
                1100,
                2699,
                14,
            ),
        ],
    },
]


class Command(BaseCommand):
    help = "Seed rich demo data across every module and store (idempotent)."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        if Booking.objects.filter(notes__contains=SEED_TAG).exists():
            self.stdout.write(
                self.style.WARNING("Demo wave already seeded (sentinel bookings found). Skipping.")
            )
            return

        # Every progress line below is held back until the whole run has
        # succeeded. The command is one atomic transaction - if a later step
        # fails, every line already printed would claim progress the rollback
        # just undid (#251/#252/#256: a traceback a dozen lines in, nothing on
        # screen saying the stock those lines described was gone again). That
        # includes `seed_foundation` and `check_alerts` below: each is its own
        # Command instance, so its output is captured only because it is
        # handed this same buffer, not our wrapper around it. A failure below
        # prints nothing but the one sentence naming what broke; success
        # flushes every buffered line, unchanged from before.
        buffer = io.StringIO()
        real_stdout, self.stdout = self.stdout, OutputWrapper(buffer)
        current_step = "seeding foundation data"
        try:
            call_command("seed_foundation", stdout=buffer)
            current_step = "cleaning test junk"
            self._clean_test_junk()
            current_step = "ensuring voucher series"
            self._ensure_series()

            current_step = "loading seed users and masters"
            self.users = {
                u.username: u
                for u in User.objects.filter(
                    username__in=[
                        "superadmin",
                        "owner",
                        "ops1",
                        "accounts1",
                        "brand1",
                        "wh.patna",
                        "wh.ranchi",
                        "deo.manager",
                        "deo.cashier",
                        "bkr.manager",
                        "bkr.cashier",
                        "hzb.manager",
                        "dum.manager",
                        "banka.manager",
                    ]
                )
            }
            self.stores = {s.code: s for s in Store.objects.all()}
            self.vendors = {v.code: v for v in Vendor.objects.all()}
            self.brands = {b.name: b for b in Brand.objects.all()}
            self.seasons = {s.code: s for s in Season.objects.all()}

            steps: list[tuple[str, Any]] = [
                ("opening stock", self._seed_opening_stock),
                ("the open/booked order", self._seed_open_and_booked_orders),
                ("transfers", self._seed_transfers),
                ("damage flags", self._seed_damage),
                ("RTVs", self._seed_rtvs),
                ("adjustments", self._seed_adjustments),
                ("the V-flip", self._seed_vflip),
                ("stocktakes", self._seed_stocktakes),
                ("stock requests", self._seed_stock_requests),
                ("write-offs", self._seed_writeoffs),
                ("money movements", self._seed_money),
                ("offers", self._seed_offers),
                ("customers", self._seed_customers),
            ]
            for current_step, step in steps:  # noqa: B007 - read by the except clause below
                step()

            current_step = "alerts"
            call_command("check_alerts", stdout=buffer)
        except Exception as exc:
            raise CommandError(f"Demo seed failed while seeding {current_step}: {exc}") from exc
        finally:
            self.stdout = real_stdout

        self.stdout.write(buffer.getvalue(), ending="")

        self.stdout.write(self.style.SUCCESS("\nDemo data seeded across all modules."))
        self.stdout.write("  Verify: GET /api/stockledger/on-hand, /api/finledger/health")

    # ------------------------------------------------------------------
    # 0. cleanup + series
    # ------------------------------------------------------------------
    def _clean_test_junk(self) -> None:
        """Remove masters/users left behind by API test runs against the dev DB.

        Only rows matching the test fixtures' unmistakable naming patterns are
        touched, and PROTECT FKs guarantee anything referenced survives.
        """
        n_users, _ = User.objects.filter(username__startswith="test.auto.").delete()
        n_roles, _ = Role.objects.filter(code__startswith="test_auto_role_").delete()
        n_stores, _ = Store.objects.filter(code__startswith="ZZTEST").delete()
        n_seasons, _ = Season.objects.filter(code__startswith="ZZS").delete()
        n_brands, _ = Brand.objects.filter(code__startswith="ZZB").delete()
        n_gstins, _ = (
            Gstin.objects.exclude(gstin__in=REAL_GSTINS).filter(stores__isnull=True).delete()
        )
        cleaned = n_users + n_roles + n_stores + n_seasons + n_brands + n_gstins
        if cleaned:
            self.stdout.write(
                f"Cleaned test junk: {n_users} users, {n_roles} roles, {n_stores} stores, "
                f"{n_seasons} seasons, {n_brands} brands, {n_gstins} GSTINs"
            )

    def _ensure_series(self) -> None:
        fy = _fy()
        for store in Store.objects.all():
            for dt in DOC_TYPES:
                VoucherSeries.objects.get_or_create(fy=fy, store_code=store.code, doc_type=dt)

    # ------------------------------------------------------------------
    # 1. opening stock (no receipt), and one booking still awaiting goods
    # ------------------------------------------------------------------
    def _make_booking(
        self,
        vendor: Vendor,
        brand: Brand,
        season: Season,
        store: Store,
        skus: list[dict[str, Any]],
        tag: str,
        user: User,
    ) -> Booking:
        fy = _fy()
        scope = season.code[:16]
        VoucherSeries.objects.get_or_create(fy=fy, store_code=scope, doc_type="BK")
        _, number = VoucherSeries.allocate(fy=fy, store_code=scope, doc_type="BK")
        booking = Booking.objects.create(
            number=number,
            vendor=vendor,
            brand=brand,
            season=season,
            destination_store=store,
            status=Booking.Status.BOOKED,
            ownership=brand.ownership,
            return_terms=brand.return_terms,
            notes=f"{SEED_TAG}:{tag}",
            created_by=user,
        )
        for sku in skus:
            BookingLine.objects.create(
                booking=booking,
                store=store,
                style_code=sku["DESIGN"],
                size=sku["SIZE"],
                booked_qty=sku["NAG"],
                mrp_paise=int(round(float(sku["MRP"]) * 100)),
            )
        booking.estimated_value_paise = sum(
            line.booked_qty * (line.mrp_paise or 0) for line in booking.lines.all()
        )
        booking.save(update_fields=["estimated_value_paise"])
        return booking

    def _seed_opening_stock(self) -> None:
        # Value goes into the books under a named head-office person (Accounts),
        # never a store's own login - the posting floor refuses that (#158/#205).
        accounts = self.users["accounts1"]
        for spec in OPENING_STOCK:
            store = self.stores[spec["store"]]
            laid = lay_in_opening_stock(
                store=store,
                brand=spec["brand"],
                tag=spec["tag"],
                skus=spec["skus"],
                user=accounts,
            )
            model = "owned" if laid.owned else "SOR"
            self.stdout.write(
                f"  Opening stock {spec['tag']}: {laid.doc_number} @ {store.code} "
                f"model={model} ₹{laid.value_paise / 100:,.0f}"
            )

    def _seed_open_and_booked_orders(self) -> None:
        """One booking still awaiting goods, so the Bookings screen shows an open order."""
        self._make_booking(
            self.vendors["abfrl"],
            self.brands["Van Heusen"],
            self.seasons["SS26"],
            self.stores["DEO"],
            [
                _sku(
                    "VH-POLO-BLU-M",
                    "Luxe Polo",
                    "M",
                    "Blue",
                    "Van Heusen",
                    SS26,
                    "T-Shirt",
                    "6109",
                    750,
                    1799,
                    24,
                ),
                _sku(
                    "VH-TEE-WHT-L",
                    "Crew Tee",
                    "L",
                    "White",
                    "Van Heusen",
                    SS26,
                    "T-Shirt",
                    "6109",
                    540,
                    1299,
                    36,
                ),
            ],
            "vh-open-order",
            self.users["ops1"],
        )
        self.stdout.write("  Open order: VH booking awaiting goods @ DEO")

    # ------------------------------------------------------------------
    # 2. transfers
    # ------------------------------------------------------------------
    def _draft_transfer(
        self,
        source: Store,
        dest: Store,
        ttype: str,
        reason: str,
        user: User,
        eway: str = "",
    ) -> StoreTransfer:
        """A transfer waiting in the Operations Head's inbox (#137)."""
        t = StoreTransfer.objects.create(
            source_store=source,
            destination_store=dest,
            transfer_type=ttype,
            reason=reason,
            transport_mode=TransportMode.OWN_VEHICLE,
            transport_ref=f"KDPS-VAN-{dest.code}",
            eway_bill_number=eway,
            created_by=user,
        )
        request_document_approval(t, requested_by=user)
        return t

    def _transfer(
        self,
        source: Store,
        dest: Store,
        ttype: str,
        reason: str,
        scans: dict[str, int],
        user: User,
        eway: str = "",
    ) -> StoreTransfer:
        """A dispatched transfer — approved by the Operations Head first (#137)."""
        t = self._draft_transfer(source, dest, ttype, reason, user, eway)
        approval = approval_for(t)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=self.users["ops1"], action="approve")
        post_transfer_dispatch(t, scans, user)
        return t

    def _seed_transfers(self) -> None:
        wh = self.stores["RAN-WH"]
        ops1 = self.users["ops1"]
        wh_user = self.users["wh.ranchi"]

        # T1: warehouse → DEO, dispatched and fully received.
        t1 = self._transfer(
            wh,
            self.stores["DEO"],
            TransferType.STORE_SPLIT,
            TransferReason.SISTER_STORE_REQUEST,
            {"LP-OXF-BLU-40": 6, "PE-PLO-GRN-L": 8},
            wh_user,
        )
        post_transfer_receipt(
            t1, {"LP-OXF-BLU-40": 6, "PE-PLO-GRN-L": 8}, self.users["deo.manager"]
        )
        self.stdout.write(f"  Transfer {t1.doc_number}: RAN-WH → DEO, received in full")

        # T2: warehouse → BKR, received short → gap closed as lost in transit.
        t2 = self._transfer(
            wh,
            self.stores["BKR"],
            TransferType.STORE_SPLIT,
            TransferReason.SLOW_MOVER,
            {"BB-SUIT-CHR-42": 5, "BB-SHRT-WHT-40": 6},
            wh_user,
        )
        post_transfer_receipt(
            t2,
            {"BB-SUIT-CHR-42": 3, "BB-SHRT-WHT-40": 6},
            self.users["bkr.manager"],
            notes="Carton arrived retaped; two suits missing.",
        )
        closure = raise_gap_closure(
            t2,
            reason=GapReason.LOST_IN_TRANSIT,
            note="Transporter confirms one bundle lost between Ranchi and Bokaro.",
            user=ops1,
        )
        approval = approval_for(closure)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=self.users["owner"], action="approve")
        post_gap_closure(closure, user=ops1)
        self.stdout.write(f"  Transfer {t2.doc_number}: RAN-WH → BKR, shortfall + gap closed")

        # T3: warehouse → HZB, still on the truck.
        t3 = self._transfer(
            wh,
            self.stores["HZB"],
            TransferType.STORE_SPLIT,
            TransferReason.SEASONAL_SWAP,
            {"PE-DNM-IND-32": 6, "LP-TEE-NVY-M": 6},
            wh_user,
        )
        self.stdout.write(f"  Transfer {t3.doc_number}: RAN-WH → HZB, in transit")

        # T4: DEO → DUM inter-store; one piece arrives damaged (store flags it).
        t4 = self._transfer(
            self.stores["DEO"],
            self.stores["DUM"],
            TransferType.INTER_STORE,
            TransferReason.CUSTOMER_WAITING,
            {"MF-TEE-OLV-L": 4},
            self.users["deo.manager"],
        )
        post_transfer_receipt(
            t4,
            {"MF-TEE-OLV-L": 3},
            self.users["dum.manager"],
            damaged={"MF-TEE-OLV-L": 1},
            notes="One tee crushed under the strap.",
        )
        self.stdout.write(f"  Transfer {t4.doc_number}: DEO → DUM, 1 damaged arrival flagged")

        # T5: warehouse (Jharkhand) → BANKA (Bihar) — a cross-state, IGST-relevant move.
        t5 = self._transfer(
            wh,
            self.stores["BANKA"],
            TransferType.STORE_SPLIT,
            TransferReason.SISTER_STORE_REQUEST,
            {"LP-CHN-KHA-34": 5, "PE-FRM-LAV-42": 6},
            wh_user,
            eway="EWB-2026-551204",
        )
        post_transfer_receipt(
            t5, {"LP-CHN-KHA-34": 5, "PE-FRM-LAV-42": 6}, self.users["banka.manager"]
        )
        state = "cross-state" if t5.is_cross_state else "intra-state"
        self.stdout.write(f"  Transfer {t5.doc_number}: RAN-WH → BANKA, received ({state})")

        # T6: a draft still sitting in the Operations Head's inbox — the state
        # every transfer now starts in, and the one the demo had no example of.
        self._draft_transfer(
            wh,
            self.stores["DUM"],
            TransferType.STORE_SPLIT,
            TransferReason.FREE_FLOOR_SPACE,
            wh_user,
        )
        self.stdout.write("  Transfer draft: RAN-WH → DUM, awaiting the Operations Head")

    # ------------------------------------------------------------------
    # 3. damage
    # ------------------------------------------------------------------
    def _seed_damage(self) -> None:
        d1 = mark_damaged(
            self.stores["DEO"],
            {"MF-JEAN-BLK-32": 1},
            user=self.users["wh.patna"],
            note="Water stain on the left leg — unsellable.",
        )
        self.stdout.write(f"  Damage {d1.doc_number or '(draft)'} @ DEO: confirmed by warehouse")
        d2 = mark_damaged(
            self.stores["BKR"],
            {"KL-JKT-BLK-L": 1},
            user=self.users["bkr.cashier"],
            note="Zip runner snapped during trial.",
        )
        state = "posted" if d2.doc_number else "pending confirmation"
        self.stdout.write(f"  Damage flag @ BKR: {state}")

    # ------------------------------------------------------------------
    # 4. RTVs
    # ------------------------------------------------------------------
    def _rtv_line(
        self, rtv: ReturnToVendor, store: Store, sku: str, qty: int, season: str = ""
    ) -> None:
        identity = resolve_line_identity(store.id, sku, season)
        ReturnToVendorLine.objects.create(rtv=rtv, sku_code=sku, qty=qty, **identity)

    def _seed_rtvs(self) -> None:
        ops1 = self.users["ops1"]
        deo = self.stores["DEO"]
        rtv1 = ReturnToVendor.objects.create(
            store=deo,
            vendor=self.vendors["abfrl"],
            brand=self.brands["Peter England"],
            return_type=ReturnType.DEFECTIVE,
            logistics_route=LogisticsRoute.STORE_PICKUP,
            notes=f"{SEED_TAG}: stitching defects reported at billing",
            created_by=ops1,
        )
        self._rtv_line(rtv1, deo, "PE-PLO-GRN-L", 2)
        request_document_approval(rtv1, requested_by=ops1)
        approval = approval_for(rtv1)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=self.users["brand1"], action="approve")
        post_rtv(rtv1, ops1)
        self.stdout.write(f"  RTV {rtv1.doc_number} @ DEO: defective, posted")

        banka = self.stores["BANKA"]
        rtv2 = ReturnToVendor.objects.create(
            store=banka,
            vendor=self.vendors["abfrl"],
            brand=self.brands["Van Heusen"],
            return_type=ReturnType.SEASONAL,
            logistics_route=LogisticsRoute.WAREHOUSE_CONSOLIDATION,
            season=SS26,
            return_window_date=date.today() + timedelta(days=30),
            notes=f"{SEED_TAG}: season-end return within window",
            created_by=ops1,
        )
        self._rtv_line(rtv2, banka, "VH-TRSR-NVY-34", 3)
        request_document_approval(rtv2, requested_by=ops1)
        approval = approval_for(rtv2)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=self.users["owner"], action="approve")
        post_rtv(rtv2, ops1)
        self.stdout.write(f"  RTV {rtv2.doc_number} @ BANKA: seasonal, posted")

        hzb = self.stores["HZB"]
        rtv3 = ReturnToVendor.objects.create(
            store=hzb,
            vendor=self.vendors["arvind"],
            brand=self.brands["Spykar"],
            return_type=ReturnType.DEFECTIVE,
            notes=f"{SEED_TAG}: draft awaiting pickup confirmation",
            created_by=ops1,
        )
        self._rtv_line(rtv3, hzb, "SPY-JEAN-GRY-32", 2)
        request_document_approval(rtv3, requested_by=ops1)
        self.stdout.write("  RTV draft @ HZB: awaiting approval, unposted")

    # ------------------------------------------------------------------
    # 5. adjustments
    # ------------------------------------------------------------------
    def _adjustment(
        self, store: Store, reason: str, deltas: dict[str, int], maker: User, note: str
    ) -> StockAdjustment:
        adj = StockAdjustment.objects.create(
            store=store, reason=reason, notes=note, created_by=maker
        )
        for sku, delta in deltas.items():
            identity = resolve_line_identity(store.id, sku)
            on_hand = StockOnHand.objects.filter(store=store, sku_code=sku).first()
            book = on_hand.net_qty if on_hand else 0
            StockAdjustmentLine.objects.create(
                adjustment=adj,
                sku_code=sku,
                book_qty=book,
                counted_qty=book + delta,
                adj_qty=delta,
                **identity,
            )
        request_document_approval(adj, requested_by=maker)
        return adj

    def _seed_adjustments(self) -> None:
        ops1 = self.users["ops1"]

        # Small surplus at BKR — inside tolerance, clears itself and posts.
        adj1 = self._adjustment(
            self.stores["BKR"],
            AdjustmentReason.SURPLUS_FOUND,
            {"AS-POLO-YLW-M": 2},
            ops1,
            "Two polos found in the trial-room return bin.",
        )
        approval = approval_for(adj1)
        if approval is not None and approval.status in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.NOT_REQUIRED,
        ):
            post_adjustment(adj1, user=ops1)
            self.stdout.write(f"  Adjustment {adj1.doc_number} @ BKR: surplus, auto-cleared")

        # Big shrinkage at DEO — left sitting in the approvals inbox.
        self._adjustment(
            self.stores["DEO"],
            AdjustmentReason.SHRINKAGE,
            {"VH-BLZR-GRY-40": -2},
            ops1,
            "Two blazers unaccounted for after the weekend rush.",
        )
        self.stdout.write("  Adjustment @ DEO: shrinkage, awaiting approval")

        # Shrinkage at DUM — approved by the store manager, then posted.
        adj3 = self._adjustment(
            self.stores["DUM"],
            AdjustmentReason.SHRINKAGE,
            {"PE-SHRT-WHT-39": -3},
            ops1,
            "Shortfall confirmed against the rack count.",
        )
        approval = approval_for(adj3)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=self.users["dum.manager"], action="approve")
        post_adjustment(adj3, user=ops1)
        self.stdout.write(f"  Adjustment {adj3.doc_number} @ DUM: approved and posted")

    # ------------------------------------------------------------------
    # 6. V-flip
    # ------------------------------------------------------------------
    def _seed_vflip(self) -> None:
        ops1 = self.users["ops1"]
        deo = self.stores["DEO"]
        vf = VFlip.objects.create(
            store=deo,
            original_brand=self.brands["Louis Philippe"],
            season=SS26,
            created_by=ops1,
        )
        identity = resolve_line_identity(deo.id, "LP-OXF-BLU-40", SS26)
        VFlipLine.objects.create(vflip=vf, sku_code="LP-OXF-BLU-40", qty=2, **identity)
        request_document_approval(vf, requested_by=ops1)
        approval = approval_for(vf)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=self.users["owner"], action="approve")
        post_vflip(vf, user=self.users["owner"])
        self.stdout.write(f"  V-flip {vf.doc_number} @ DEO: LP stock flipped to owned")

    # ------------------------------------------------------------------
    # 7. stocktakes
    # ------------------------------------------------------------------
    def _seed_stocktakes(self) -> None:
        # HZB: a full blind count with two small variances, applied.
        hzb = self.stores["HZB"]
        st = open_stocktake(hzb, user=self.users["hzb.manager"])
        session = open_session(st, scope="store", user=self.users["hzb.manager"])
        scans = {
            row.sku_code: row.net_qty
            for row in StockOnHand.objects.filter(store=hzb, net_qty__gt=0)
        }
        scans["USP-TEE-WHT-L"] -= 1  # one tee missing
        scans["SPY-TEE-RED-M"] += 1  # one tee extra
        record_scans(session, {k: v for k, v in scans.items() if v > 0})
        submit_session(session)
        apply_variance(st, user=self.users["ops1"])
        self.stdout.write("  Stocktake @ HZB: counted, variance applied")

        # DUM: a brand count still in progress.
        st2 = open_stocktake(self.stores["DUM"], user=self.users["dum.manager"])
        s2 = open_session(st2, scope="brand", scope_value="Jockey", user=self.users["dum.manager"])
        record_scans(s2, {"JKY-TRK-NVY-L": 10})
        self.stdout.write("  Stocktake @ DUM: Jockey count in progress")

    # ------------------------------------------------------------------
    # 8. stock requests (the cross-store search's ask, #74/#175)
    # ------------------------------------------------------------------
    def _stock_request(
        self,
        requesting: Store,
        fulfilling: Store,
        lines: list[tuple[str, int]],
        user: User,
        source: str = StockRequestSource.MANUAL,
    ) -> StockRequest:
        req = StockRequest.objects.create(
            requesting_store=requesting,
            fulfilling_store=fulfilling,
            source=source,
            created_by=user,
        )
        for sku, qty in lines:
            on_hand = StockOnHand.objects.filter(store=fulfilling, sku_code=sku).first()
            StockRequestLine.objects.create(
                request=req,
                sku_code=sku,
                design=getattr(on_hand, "design", "") or "",
                color=getattr(on_hand, "color", "") or "",
                size=getattr(on_hand, "size", "") or "",
                brand=getattr(on_hand, "brand", "") or "",
                season=getattr(on_hand, "season", "") or "",
                item=getattr(on_hand, "item", "") or "",
                hsn=getattr(on_hand, "hsn", "") or "",
                qty=qty,
            )
        request_document_approval(req, requested_by=user)
        return req

    def _seed_stock_requests(self) -> None:
        ops1 = self.users["ops1"]
        wh = self.stores["RAN-WH"]

        # SR1: DUM asking the warehouse — still waiting on the approvals inbox.
        sr1 = self._stock_request(
            self.stores["DUM"],
            wh,
            [("PE-DNM-IND-32", 4)],
            self.users["dum.manager"],
            source=StockRequestSource.CROSS_STORE_SEARCH,
        )
        self.stdout.write(f"  Stock request {sr1.doc_number or '(draft)'}: DUM ← RAN-WH, pending")

        # SR2: HZB asking the warehouse — approved, nothing fulfilled yet.
        sr2 = self._stock_request(
            self.stores["HZB"],
            wh,
            [("LP-TEE-NVY-M", 3)],
            self.users["hzb.manager"],
        )
        approval = approval_for(sr2)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=ops1, action="approve")
        self.stdout.write(f"  Stock request {sr2.doc_number}: HZB ← RAN-WH, approved")

        # SR3: BKR asking the warehouse — declined by the Operations Head.
        sr3 = self._stock_request(
            self.stores["BKR"],
            wh,
            [("BB-SHRT-WHT-40", 3)],
            self.users["bkr.manager"],
        )
        approval = approval_for(sr3)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(
                approval,
                actor=ops1,
                action="reject",
                reason="Warehouse cover for BB-SHRT-WHT-40 is thin — holding it back this week.",
            )
        self.stdout.write(f"  Stock request {sr3.doc_number}: BKR ← RAN-WH, declined")

        # SR4: DUM asking the warehouse again — approved and a fulfilling
        # transfer drafted (still awaiting the Operations Head's own gate, #137).
        sr4 = self._stock_request(
            self.stores["DUM"],
            wh,
            [("LP-CHN-KHA-34", 4)],
            self.users["dum.manager"],
            source=StockRequestSource.CROSS_STORE_SEARCH,
        )
        approval = approval_for(sr4)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=ops1, action="approve")
        line = sr4.lines.first()
        assert line is not None  # the ask above was created with exactly one line
        fulfil_stock_request(
            sr4, [{"line_id": line.id, "qty": line.qty}], user=self.users["wh.ranchi"]
        )
        self.stdout.write(f"  Stock request {sr4.doc_number}: DUM ← RAN-WH, being fulfilled")

        # SR5: BANKA asking the warehouse — approved, fulfilled and received in
        # full, so the ask closes itself.
        sr5 = self._stock_request(
            self.stores["BANKA"],
            wh,
            [("PE-PLO-GRN-L", 3)],
            self.users["banka.manager"],
        )
        approval = approval_for(sr5)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=ops1, action="approve")
        line = sr5.lines.first()
        assert line is not None  # the ask above was created with exactly one line
        transfer = fulfil_stock_request(
            sr5, [{"line_id": line.id, "qty": line.qty}], user=self.users["wh.ranchi"]
        )
        t_approval = approval_for(transfer)
        if t_approval is not None and t_approval.status == ApprovalStatus.PENDING:
            decide(t_approval, actor=ops1, action="approve")
        post_transfer_dispatch(transfer, {"PE-PLO-GRN-L": line.qty}, self.users["wh.ranchi"])
        post_transfer_receipt(transfer, {"PE-PLO-GRN-L": line.qty}, self.users["banka.manager"])
        self.stdout.write(f"  Stock request {sr5.doc_number}: BANKA ← RAN-WH, closed")

    # ------------------------------------------------------------------
    # 9. write-offs (dead stock exit)
    # ------------------------------------------------------------------
    def _seed_writeoffs(self) -> None:
        ops1 = self.users["ops1"]

        # WRO1: DEO — approved and posted, dead stock leaves the books.
        deo = self.stores["DEO"]
        wro1 = WriteOff.objects.create(
            store=deo,
            reason="Two blazers past a full season with no movement — write off as dead stock.",
            created_by=self.users["deo.manager"],
        )
        identity = resolve_line_identity(deo.id, "VH-BLZR-GRY-40")
        WriteOffLine.objects.create(writeoff=wro1, sku_code="VH-BLZR-GRY-40", qty=1, **identity)
        request_document_approval(wro1, requested_by=self.users["deo.manager"])
        approval = approval_for(wro1)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            decide(approval, actor=self.users["owner"], action="approve")
        post_writeoff(wro1, user=ops1)
        self.stdout.write(f"  Write-off {wro1.doc_number} @ DEO: posted")

        # WRO2: BKR — sitting in the approvals inbox.
        bkr = self.stores["BKR"]
        wro2 = WriteOff.objects.create(
            store=bkr,
            reason="Jacket zip runner beyond repair — refused by the customer twice.",
            created_by=self.users["bkr.manager"],
        )
        identity = resolve_line_identity(bkr.id, "KL-JKT-BLK-L")
        WriteOffLine.objects.create(writeoff=wro2, sku_code="KL-JKT-BLK-L", qty=1, **identity)
        request_document_approval(wro2, requested_by=self.users["bkr.manager"])
        self.stdout.write("  Write-off @ BKR: awaiting approval")

    # ------------------------------------------------------------------
    # 11. money
    # ------------------------------------------------------------------
    def _seed_money(self) -> None:
        acct = self.users["ops1"]
        payments = [
            ("abfrl", 50_000, "Part payment on the Madura account", "neft", "BANK"),
            ("blackberrys", 40_000, "Payment for suit consignment", "neft", "BANK"),
            ("credo", 25_000, "Mufti account settlement", "upi", "UPI"),
            ("page", 8_000, "Jockey monthly settlement", "cash", "CASH"),
            ("kewalkiran", 15_000, "Killer AW25 part payment", "neft", "BANK"),
        ]
        for code, rupees, desc, mode, account in payments:
            post_vendor_payment(
                self.vendors[code],
                rupees_to_paise(rupees),
                desc,
                acct,
                mode=mode,
                account=account,
            )
        self.stdout.write(f"  Vendor payments: {len(payments)} posted")

        movements = [
            ("in", 45_230, "Cash sales deposit — DEO counter", "CASH", "cash"),
            ("in", 23_410, "UPI collections — BKR", "UPI", "upi"),
            ("in", 500_000, "Owner capital infusion", "BANK", "neft"),
            ("out", 8_750, "Store housekeeping & supplies — DEO", "CASH", "cash"),
            ("out", 12_400, "Freight — Ranchi to Deoghar cartons", "CASH", "cash"),
            ("out", 18_000, "Electricity bill — HZB", "BANK", "netbanking"),
        ]
        for direction, rupees, desc, account, mode in movements:
            post_cash_movement(
                direction, rupees_to_paise(rupees), desc, acct, account=account, mode=mode
            )
        self.stdout.write(f"  Cash/bank movements: {len(movements)} posted")

    # ------------------------------------------------------------------
    # 12. offers & customers
    # ------------------------------------------------------------------
    def _seed_offers(self) -> None:
        ops1 = self.users["ops1"]
        owner = self.users["owner"]
        pe = self.brands.get("Peter England")
        lp = self.brands.get("Louis Philippe")
        bb = self.brands.get("Blackberrys")
        store_codes = [s.code for s in Store.objects.all()]

        if pe:
            Offer.objects.get_or_create(
                name="Peter England SS26 - Flat 20% Off",
                defaults={
                    "brand": pe,
                    "funder": Offer.Funder.BRAND,
                    "layer": Offer.Layer.BRAND,
                    "trigger_type": Offer.Trigger.NONE,
                    "trigger_config": {},
                    "reward_type": Offer.Reward.PCT_OFF,
                    "reward_config": {"percent": "20.00"},
                    "item_scope": {"brands": ["Peter England"]},
                    "store_scope": {"kind": "all", "stores": store_codes},
                    "starts_on": date.today() - timedelta(days=15),
                    "ends_on": date.today() + timedelta(days=45),
                    "status": Offer.Status.LIVE,
                    "approved_by": owner,
                    "created_by": ops1,
                },
            )

        if lp:
            Offer.objects.get_or_create(
                name="Louis Philippe - Buy 2 Get 1 Free",
                defaults={
                    "brand": lp,
                    "funder": Offer.Funder.BRAND,
                    "layer": Offer.Layer.BRAND,
                    "trigger_type": Offer.Trigger.QTY,
                    "trigger_config": {"slabs": [{"min_qty": 3}]},
                    "reward_type": Offer.Reward.ITEM_FREE,
                    "reward_config": {"free_qty": 1},
                    "item_scope": {"brands": ["Louis Philippe"]},
                    "store_scope": {"kind": "all", "stores": store_codes},
                    "starts_on": date.today() - timedelta(days=10),
                    "ends_on": date.today() + timedelta(days=30),
                    "status": Offer.Status.LIVE,
                    "approved_by": owner,
                    "created_by": ops1,
                },
            )

        if bb:
            Offer.objects.get_or_create(
                name="Blackberrys Suit Fest - ₹1000 Instant Off",
                defaults={
                    "brand": bb,
                    "funder": Offer.Funder.BRAND,
                    "layer": Offer.Layer.BRAND,
                    "trigger_type": Offer.Trigger.SPEND,
                    "trigger_config": {"slabs": [{"min_paise": 499900}]},
                    "reward_type": Offer.Reward.AMT_OFF,
                    "reward_config": {"amount_paise": 100000},
                    "item_scope": {"brands": ["Blackberrys"]},
                    "store_scope": {"kind": "all", "stores": store_codes},
                    "starts_on": date.today() - timedelta(days=5),
                    "ends_on": date.today() + timedelta(days=60),
                    "status": Offer.Status.LIVE,
                    "approved_by": owner,
                    "created_by": ops1,
                },
            )

        Offer.objects.get_or_create(
            name="End of Season Sale - Storewide 15% Off",
            defaults={
                "brand": None,
                "funder": Offer.Funder.KDPS,
                "layer": Offer.Layer.STOREWIDE,
                "trigger_type": Offer.Trigger.SPEND,
                "trigger_config": {"slabs": [{"min_paise": 299900}]},
                "reward_type": Offer.Reward.PCT_OFF,
                "reward_config": {"percent": "15.00"},
                "item_scope": {},
                "store_scope": {"kind": "all", "stores": store_codes},
                "starts_on": date.today() - timedelta(days=2),
                "ends_on": date.today() + timedelta(days=20),
                "status": Offer.Status.LIVE,
                "approved_by": owner,
                "created_by": ops1,
            },
        )
        self.stdout.write("  Offers: 4 live promotional rules seeded")

    def _seed_customers(self) -> None:
        customers = [
            ("9835012345", "Rajesh Kumar", "10ABCDE1234F1Z1"),
            ("9431098765", "Priya Sharma", ""),
            ("9934123456", "Amit Verma", "20XYZAB5678C1Z9"),
            ("9835999888", "Sanjay Gupta", ""),
            ("9709876543", "Ananya Roy", ""),
        ]
        for mobile, name, gstin in customers:
            Customer.objects.get_or_create(
                mobile=mobile,
                defaults={"name": name, "gstin": gstin},
            )
        self.stdout.write(f"  Customers: {len(customers)} registered customers seeded")

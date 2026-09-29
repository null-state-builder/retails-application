"""Idempotent foundation seed: roles, the masters spine, and demo users.

Run: `python manage.py seed_foundation`. Safe to re-run - it upserts, except for
the three things a live operator owns: a user's password, a role's access grid
and whether a role is active (see `_seed_roles`). It also (re)writes
`app/test_credentials.md` in the checkout (override with
`SEED_CREDENTIALS_PATH`) so the testing/fork agents always have current logins.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.floors import clamp_to_floors, describe_floors
from accounts.goods_demo import credentials_section, seed_goods_demo
from accounts.matrix import seeded_row
from accounts.models import NAV_GROUPS, Role, User
from accounts.rbac_matrix import section_access_for
from core.documents import VoucherSeries
from core.fiscal import financial_year, next_financial_year
from masters.models import Brand, Gstin, LegalEntity, Season, Store
from vendors.models import Booking, BookingLine, Vendor

#: The selling document types, seeded per store per FY: the sale (till-assigned),
#: the credit note it can issue, and the plain return that issues one.
SELL_DOC_TYPES = ("SAL", "CRN", "SRT")

# Demo logins (documented credentials) must NOT be (re)created on a production
# deploy. Gate them behind SEED_DEMO (default on, so preview/CI keep working);
# set SEED_DEMO=0 in production.
SEED_DEMO = os.environ.get("SEED_DEMO", "1") == "1"

ROLES: list[dict[str, Any]] = [
    {
        "code": "owner",
        "name": "Owner / Director",
        "landing_page": "owner",
        "nav_groups": list(NAV_GROUPS),
        "description": "Sees the whole business; dashboards, approvals, sign-offs.",
    },
    {
        # The sheet's one "Store Person". Two codes (store_manager, store_staff)
        # shared this row until 22 Sep 2026; migration 0020 merged them.
        "code": "store_person",
        "name": "Store Person",
        "landing_page": "store",
        "nav_groups": ["home", "store_ops", "documents", "ledgers", "controls", "outbound"],
        "description": "One store's floor: bills, returns, receives, counts, own expenses.",
    },
    {
        "code": "warehouse",
        "name": "Warehouse / Inward Operator",
        "landing_page": "warehouse",
        "nav_groups": ["home", "documents", "ledgers", "store_ops", "outbound"],
        "description": "Receives goods, builds PTs, fills barcodes, proposes splits.",
    },
    {
        "code": "accounts",
        "name": "Accounts / Finance",
        "landing_page": "finance",
        "nav_groups": ["home", "documents", "ledgers", "controls", "intelligence", "outbound"],
        "description": "Owns money — payables, payments, collection & bank audit, Tally.",
    },
    {
        "code": "brand_manager",
        "name": "Brand Manager",
        "landing_page": "home",
        "nav_groups": ["home", "documents", "intelligence"],
        "description": "Owns assigned brands across stores — bookings, offers, brand reports.",
    },
    {
        "code": "ho_ops",
        "name": "HO Operations / Buyer",
        "landing_page": "ops",
        "nav_groups": [
            "home",
            "master_data",
            "documents",
            "ledgers",
            "controls",
            "intelligence",
            "outbound",
        ],
        "description": "HO operating core — bookings, transfers, offers, intelligence.",
    },
    {
        "code": "data_steward",
        "name": "HO Data Steward",
        "landing_page": "masters",
        "nav_groups": ["home", "master_data"],
        "description": "Single steward of master data — vendors/brands/SKU/season/taxonomy.",
    },
    {
        "code": "promo",
        "name": "Promo / Marketing",
        "landing_page": "home",
        "nav_groups": ["home", "intelligence"],
        "description": "Writes the offers and publishes them to the shops; no money, no stock.",
    },
    {
        "code": "it_admin",
        "name": "System / IT Admin",
        "landing_page": "owner",
        "nav_groups": list(NAV_GROUPS),
        "description": "Owns users, RBAC, and all integration/adapter/config plumbing.",
    },
]

# Usernames granted Django-superuser break-glass. A superuser bypasses the whole
# RBAC matrix (manage on every section) and reaches Django `/admin`, so it is
# kept as its own account, separate from every business persona — including
# Owner. That way the matrix is genuinely enforced for real people (Owner sees
# only what the sheet grants, Admin has no Money) and stays auditable; god-mode
# lives in one clearly-labelled break-glass login instead of a daily persona.
SUPERUSERS = {"superadmin"}

# (username, password, role_code, scope_type, store_codes, full_name, brand_codes)
# `brand_codes` only applies to the `brand` scope — a brand manager works across
# every store but only inside their own brands, so the top bar gives them a brand
# filter instead of a unit list (#88).
USERS: list[tuple[str, str, str, str, list[str], str, list[str]]] = [
    ("superadmin", "Super@123", "it_admin", "all", [], "System Superadmin (break-glass)", []),
    ("admin", "Admin@123", "it_admin", "all", [], "IT Admin", []),
    ("owner", "Owner@123", "owner", "all", [], "K. D. Proprietor", []),
    ("ops1", "Ops@123", "ho_ops", "all", [], "Head-Office Ops", []),
    ("promo1", "Promo@123", "promo", "all", [], "Marketing / Promo", []),
    ("accounts1", "Acct@123", "accounts", "all", [], "Patna Accountant", []),
    (
        "brand1",
        "Brand@123",
        "brand_manager",
        "brand",
        [],
        "Madura Brand Manager",
        ["louis-philippe", "van-heusen", "allen-solly", "peter-england"],
    ),
    ("wh.patna", "Wh@123", "warehouse", "all", [], "Patna Warehouse", []),
    ("steward", "Steward@123", "data_steward", "all", [], "Data Steward", []),
    # "Manager" and "cashier" are the people's job titles, kept so the e2e
    # logins stay stable; both hold the one store_person role.
    ("deo.manager", "Store@123", "store_person", "store", ["DEO"], "Deoghar Manager", []),
    ("deo.cashier", "Store@123", "store_person", "store", ["DEO"], "Deoghar Cashier", []),
    ("bkr.manager", "Store@123", "store_person", "store", ["BKR"], "Bokaro Manager", []),
    ("bkr.cashier", "Store@123", "store_person", "store", ["BKR"], "Bokaro Cashier", []),
    ("hzb.manager", "Store@123", "store_person", "store", ["HZB"], "Hazaribagh Manager", []),
    ("dum.manager", "Store@123", "store_person", "store", ["DUM"], "Dumka Manager", []),
    ("banka.manager", "Store@123", "store_person", "store", ["BANKA"], "Banka Manager", []),
    ("banka.cashier", "Store@123", "store_person", "store", ["BANKA"], "Banka Cashier", []),
    ("wh.ranchi", "Wh@123", "warehouse", "store", ["RAN-WH"], "Ranchi Warehouse", []),
]


#: Demo logins sign in by email since goods-v1 (design §4.2).
DEMO_EMAIL_DOMAIN = "kdps.demo"


def demo_email(username: str) -> str:
    return f"{username}@{DEMO_EMAIL_DOMAIN}"


def demo_grants(
    role_code: str,
    store_codes: list[str],
    brand_codes: list[str],
    stores: dict[str, Store],
    warehouses: list[Store],
) -> list[Any]:
    """The Phase 1 role grants a legacy demo persona holds for goods work."""
    from accounts.goods_setup import GrantRequest

    def at_sites(code: str, sites: list[Store]) -> list[Any]:
        return [GrantRequest(role_code=code, scope_kind="site", site_id=site.pk) for site in sites]

    own_sites = [stores[c] for c in store_codes if c in stores]
    if role_code == "it_admin":
        return [GrantRequest(role_code="X-PLT")]
    if role_code == "owner":
        return [GrantRequest(role_code="C-OWN")]
    if role_code == "ho_ops":
        return [GrantRequest(role_code="C-INV"), GrantRequest(role_code="C-BUY")]
    if role_code == "accounts":
        return [GrantRequest(role_code="C-CAO")]
    if role_code == "data_steward":
        return [GrantRequest(role_code="C-PMO")]
    if role_code == "brand_manager":
        return [
            GrantRequest(role_code="C-BUY", scope_kind="brand", brand_id=brand.pk)
            for brand in Brand.objects.filter(code__in=brand_codes)
        ]
    if role_code == "warehouse":
        return at_sites("C-WHO", own_sites or warehouses)
    if role_code == "store_person":
        # The merged store role takes the wider of the two old grants.
        return at_sites("M-STR", own_sites)
    return []


def deployment_tenant() -> Any:
    """The demo deployment's tenant, created on the first seed."""
    import uuid

    from masters.goods_models import Tenant

    tenant, _ = Tenant.objects.get_or_create(
        deployment_key=uuid.UUID(str(settings.KDPS_DEPLOYMENT_KEY)),
        defaults={
            "code": "KDPS-DEMO",
            "name": "KDPS Lifestyle (demo data)",
            "timezone": "Asia/Kolkata",
            "currency": "INR",
            "locale": "en-IN",
            # Demo data, never a real tenant: real opening stock stays blocked (OQ-54).
            "synthetic": True,
        },
    )
    return tenant


class Command(BaseCommand):
    help = "Seed foundation roles, masters and demo users (idempotent)."

    def handle(self, *args: Any, **options: Any) -> None:
        from core.tenancy import tenant_context

        # Roles, entities, registrations, stores, brands and vendors belong to a
        # tenant (change PRD §14.2), so the deployment's tenant exists and is
        # bound before any of them is written.
        tenant = deployment_tenant()
        with tenant_context(tenant.pk):
            self._seed_rows()
        if SEED_DEMO:
            # The synthetic goods-v1 fixture: its own warehouse/store pair and one
            # login per goods role, beside the legacy demo data, never instead of
            # it (ticket 01, GSA-T01). Setup only - the pair stays planned and
            # goods-unapproved: on the goods-v1 stock contract, an empty scope.
            seed_goods_demo(tenant)
            # Outside the transaction on purpose: this is filesystem IO, and the
            # seed holds a row lock on every Role while it runs. Writing a file
            # under those locks makes a concurrent access-change apply wait on a
            # disk, and the file is derived from a constant - it needs no
            # database consistency at all.
            self._write_credentials()
            self.stdout.write(self.style.SUCCESS("Foundation seed complete (incl. demo users)."))
        else:
            self.stdout.write(
                self.style.SUCCESS("Foundation seed complete (SEED_DEMO=0: demo users skipped).")
            )

    @transaction.atomic
    def _seed_rows(self) -> None:
        self._seed_roles()
        entity, gstins = self._seed_entity_gstins()
        stores = self._seed_stores(gstins)
        self._seed_sell_series(stores)
        self._seed_salespeople(stores)
        self._seed_seasons()
        self._seed_brands()
        self._seed_gst_slab()
        vendors = self._seed_vendors()
        if SEED_DEMO:
            self._seed_users(entity, stores)
            self._seed_sample_booking(vendors, stores)

    def _seed_roles(self) -> None:
        """Roles, with the access grid seeded **additively** (#224).

        The grid is the one column here that two administrators maintain between
        releases (#173, floor rule 4), so the sheet is starting content for it
        and nothing more - `accounts.matrix.seeded_row` says why and how. What
        this method adds on top is the floor: a cell kept from the stored row is
        pulled down if a release has since locked that rung, and the cells it
        moved are named on stdout rather than corrected in silence.

        Where the row already exists it is locked for the read-then-write, so a
        re-seed racing an access change being applied cannot read the row before
        and write it after. On a first seed there is no row to lock, and the
        unique `code` settles the race instead.
        """
        for r in ROLES:
            existing = Role.objects.select_for_update().filter(code=r["code"]).first()
            access, corrected = clamp_to_floors(
                r["code"],
                seeded_row(
                    existing.section_access if existing else None,
                    default=section_access_for(r["code"]),
                ),
            )
            for line in describe_floors(corrected):
                self.stdout.write(self.style.WARNING(f"{r['code']} narrowed to the floor: {line}"))
            Role.objects.update_or_create(
                code=r["code"],
                defaults={
                    "name": r["name"],
                    "landing_page": r["landing_page"],
                    "nav_groups": r["nav_groups"],
                    "section_access": access,
                    "description": r["description"],
                    "is_system": True,
                    # Only on creation. Deactivating a role is an access change
                    # two administrators agree (#224 again, from the other side):
                    # switching it back on at the next deploy would re-open a
                    # seat they closed, and this one fails open.
                    **({} if existing else {"is_active": True}),
                },
            )

    def _seed_entity_gstins(self) -> tuple[LegalEntity, dict[str, Gstin]]:
        entity, _ = LegalEntity.objects.update_or_create(
            code="kdps",
            defaults={"name": "KDPS Lifestyle Pvt Ltd", "pan": "AAACK1234M"},
        )
        bihar, _ = Gstin.objects.update_or_create(
            gstin="10AAACK1234M1Z5",
            defaults={"legal_entity": entity, "state_code": "10", "state_name": "Bihar"},
        )
        jhk, _ = Gstin.objects.update_or_create(
            gstin="20AAACK1234M1Z3",
            defaults={"legal_entity": entity, "state_code": "20", "state_name": "Jharkhand"},
        )
        return entity, {"bihar": bihar, "jharkhand": jhk}

    def _seed_stores(self, gstins: dict[str, Gstin]) -> dict[str, Store]:
        rows = [
            ("DEO", "Deoghar", "store", "jharkhand", "Deoghar"),
            ("BKR", "Bokaro", "store", "jharkhand", "Bokaro"),
            ("HZB", "Hazaribagh", "store", "jharkhand", "Hazaribagh"),
            ("DUM", "Dumka", "store", "jharkhand", "Dumka"),
            ("BANKA", "Banka", "store", "bihar", "Banka"),
            ("RAN-WH", "Ranchi Central Warehouse", "warehouse", "jharkhand", "Ranchi"),
        ]
        out: dict[str, Store] = {}
        for code, name, stype, state, city in rows:
            store, _ = Store.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "store_type": stype,
                    "gstin": gstins[state],
                    "city": city,
                },
            )
            out[code] = store
        return out

    def _seed_salespeople(self, stores: dict[str, Store]) -> None:
        """Two salespeople on every selling store's staff list.

        Every sold line carries the name of who sold it (Rule 10, and D10 puts the
        picker on the line), so the counter refuses a bill that names nobody. A
        store seeded without a single salesperson therefore cannot sell at all -
        the first bill of a fresh install would be refused for a reason that
        reads like a defect and is a missing person. The till's picker reads the
        staff list (ticket 07), so these are staff records assigned to the store.
        Warehouses get none: they do not sell.
        """
        from sell.services.salespeople import ensure_salesperson

        for store in stores.values():
            if store.store_type != Store.StoreType.STORE:
                continue
            for code, name in (("S1", "Counter One"), ("S2", "Counter Two")):
                ensure_salesperson(store, f"{store.code}-{code}", f"{name} ({store.code})")

    def _seed_sell_series(self, stores: dict[str, Store]) -> None:
        """The selling counters: a sale, credit-note and return series per store.

        These are seeded rather than created on first use like the other document
        types, because the sale's counter is not the server's to create lazily —
        the till already holds a number when it calls, and a missing series row
        would turn a printed bill into a sync failure the store cannot fix. The
        credit-note and return series are ordinary server-allocated counters and
        ride along so the whole selling set exists together.

        Next year's rows are seeded alongside this year's, because the failure
        this is here to prevent lands precisely at midnight on 1 April: the till
        rolls its own financial year on its own clock and starts at bill 1, and
        nobody is deploying at that moment to create the row it needs.

        Idempotent, and only ever creates: `get_or_create` never touches
        `next_seq` on a series that is already counting.
        """
        for fy in (financial_year(), next_financial_year()):
            for store in stores.values():
                for doc_type in SELL_DOC_TYPES:
                    VoucherSeries.objects.get_or_create(
                        fy=fy, store_code=store.code, doc_type=doc_type
                    )

    def _seed_seasons(self) -> None:
        # The last row is the one explicit "unknown historical season" (store and
        # warehouse operations PRD §4): a real, named season somebody chooses on
        # an opening row when the buying cohort genuinely cannot be established.
        # Closed, so the counter never resolves a bare scan to it, and sorted
        # oldest so it never wins a "pick the oldest live cohort" choice either.
        for code, name, status, order, unknown in [
            ("SS26", "Spring/Summer 2026", "open", 3, False),
            ("AW25", "Autumn/Winter 2025", "eoss", 2, False),
            ("SS25", "Spring/Summer 2025", "closed", 1, False),
            ("UNKNOWN-HIST", "Unknown historical season", "closed", 0, True),
        ]:
            Season.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "status": status,
                    "sort_order": order,
                    "historical_unknown": unknown,
                },
            )

    def _seed_brands(self) -> None:
        rows = [
            ("louis-philippe", "Louis Philippe", "brand_owned", "uncapped"),
            ("van-heusen", "Van Heusen", "brand_owned", "uncapped"),
            ("allen-solly", "Allen Solly", "brand_owned", "rolling"),
            ("peter-england", "Peter England", "owned", "capped"),
            ("mufti", "Mufti", "owned", "none"),
            ("blackberry", "Blackberrys", "owned", "capped"),
            ("jockey", "Jockey", "owned", "none"),
            ("us-polo", "U.S. Polo Assn.", "brand_owned", "uncapped"),
            ("spykar", "Spykar", "owned", "none"),
            ("killer", "Killer", "owned", "none"),
        ]
        for code, name, ownership, terms in rows:
            Brand.objects.update_or_create(
                code=code,
                defaults={"name": name, "ownership": ownership, "return_terms": terms},
            )

    def _seed_gst_slab(self) -> None:
        from masters.models import GstSlab

        GstSlab.objects.update_or_create(
            name="Apparel (GST 2.0)",
            defaults={
                "threshold_paise": 250000,
                "rate_below": 5,
                "rate_above": 18,
                "effective_from": datetime.date(2025, 9, 22),
            },
        )

    def _seed_vendors(self) -> dict[str, Vendor]:
        rows = [
            (
                "abfrl",
                "Aditya Birla Fashion (Madura)",
                "Bengaluru",
                "29AAACX1234M1Z1",
                "29",
                "Karnataka",
                ["louis-philippe", "van-heusen", "allen-solly", "peter-england"],
            ),
            (
                "arvind",
                "Arvind Fashions",
                "Bengaluru",
                "29AAACA5678M1Z2",
                "29",
                "Karnataka",
                ["us-polo", "spykar"],
            ),
            (
                "credo",
                "Credo Brands (Mufti)",
                "Mumbai",
                "27AAACC9012M1Z3",
                "27",
                "Maharashtra",
                ["mufti"],
            ),
            (
                "blackberrys",
                "Blackberrys Menswear",
                "New Delhi",
                "07AAACB3456M1Z4",
                "07",
                "Delhi",
                ["blackberry"],
            ),
            (
                "page",
                "Page Industries (Jockey)",
                "Bengaluru",
                "29AAACP7890M1Z5",
                "29",
                "Karnataka",
                ["jockey"],
            ),
            (
                "kewalkiran",
                "Kewal Kiran (Killer)",
                "Mumbai",
                "27AAACK2345M1Z6",
                "27",
                "Maharashtra",
                ["killer"],
            ),
        ]
        out: dict[str, Vendor] = {}
        for code, name, city, gstin, sc, sn, brand_codes in rows:
            vendor, _ = Vendor.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "city": city,
                    "gstin": gstin,
                    "state_code": sc,
                    "state_name": sn,
                    "payment_terms": "Net 30",
                },
            )
            vendor.brands.set(Brand.objects.filter(code__in=brand_codes))
            out[code] = vendor
        return out

    def _seed_sample_booking(self, vendors: dict[str, Vendor], stores: dict[str, Store]) -> None:
        season = Season.objects.filter(code="SS26").first()
        brand = Brand.objects.filter(code="peter-england").first()
        vendor = vendors.get("abfrl")
        if not (season and brand and vendor):
            return
        booking, created = Booking.objects.get_or_create(
            number="BK-SS26-0001",
            defaults={
                "vendor": vendor,
                "brand": brand,
                "season": season,
                "destination_store": stores.get("DEO"),
                "status": Booking.Status.BOOKED,
                "vendor_ref": "PE/ORD/2026/118",
                "ownership": brand.ownership,
                "return_terms": brand.return_terms,
            },
        )
        if created:
            # A multi-store booking: shirts inherit the booking default (DEO), the
            # trousers are explicitly routed to a second store (BKR) — demonstrating
            # the "default destination, override per line" rule.
            for style, size, qty, mrp, store_code in [
                ("PE-FSHIRT-001", "39", 12, 1799_00, None),
                ("PE-FSHIRT-001", "40", 18, 1799_00, None),
                ("PE-TROUSER-100", "32", 10, 2499_00, "BKR"),
                ("PE-TROUSER-100", "34", 8, 2499_00, "BKR"),
            ]:
                BookingLine.objects.create(
                    booking=booking,
                    store=stores.get(store_code) if store_code else None,
                    style_code=style,
                    size=size,
                    booked_qty=qty,
                    mrp_paise=mrp,
                )
            booking.estimated_value_paise = sum(
                line.booked_qty * (line.mrp_paise or 0) for line in booking.lines.all()
            )
            booking.save(update_fields=["estimated_value_paise"])

    def _seed_users(self, entity: LegalEntity, stores: dict[str, Store]) -> None:
        for username, password, role_code, scope, store_codes, full_name, brand_codes in USERS:
            role = Role.objects.filter(code=role_code).first()
            # Only the dedicated break-glass account is a Django superuser (see
            # SUPERUSERS). Every business persona — `admin`/it_admin and `owner`
            # included — is a normal user enforced by `Role.section_access`, so
            # the ratified matrix (e.g. Admin = no Money) actually applies.
            is_super = username in SUPERUSERS
            user, created = User.objects.update_or_create(
                username=username,
                defaults={
                    "full_name": full_name,
                    "role": role,
                    "scope_type": scope,
                    "entity": entity if scope != "all" else None,
                    "is_active": True,
                    "is_staff": is_super,
                    "is_superuser": is_super,
                },
            )
            # Set the password only when the user is first created — never overwrite
            # an operator-changed password on a re-seed / redeploy.
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
            user.stores.set([stores[c] for c in store_codes if c in stores])
            user.brands.set(Brand.objects.filter(code__in=brand_codes))
        self._seed_goods_people(stores)

    def _seed_goods_people(self, stores: dict[str, Store]) -> None:
        """Give every demo login an email, a person and its Phase 1 role grants.

        Login is by email since goods-v1 (design §4.2); a demo login is
        `<username>@kdps.demo`. Grants are appended once per person through the
        command kernel, like any other access change, and never re-granted on a
        re-seed.
        """
        import uuid as _uuid

        from accounts.goods_models import HumanIdentity, RoleGrant, SecurityGuard
        from accounts.goods_setup import (
            add_grant,
            ensure_goods_roles,
            is_tenant_staff,
            service_principal,
            sync_tenant_staff,
            top_up_person_grants,
        )
        from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
        from core.tenancy import tenant_context

        tenant = deployment_tenant()
        with tenant_context(tenant.pk):
            ensure_goods_roles()
        warehouses = [s for s in stores.values() if s.store_type == Store.StoreType.WAREHOUSE]
        with tenant_context(tenant.pk):
            for (
                username,
                _password,
                role_code,
                _scope,
                store_codes,
                full_name,
                brand_codes,
            ) in USERS:
                user = User.objects.get(username=username)
                human = (
                    user.human
                    or HumanIdentity.objects.filter(tenant=tenant, staff_code=username).first()
                )
                if human is None:
                    human = HumanIdentity.objects.create(
                        tenant=tenant, staff_code=username, display_name=full_name
                    )
                SecurityGuard.objects.get_or_create(tenant=tenant, human=human)
                requests = demo_grants(role_code, store_codes, brand_codes, stores, warehouses)
                # The legacy IT-admin logins hold X-PLT alone, so they are not
                # tenant staff and stay off the People screen (GSA-T03). A demo
                # login with no goods grant yet (`promo1`) is an ordinary
                # employee and keeps its Staff row. This converges on a re-seed
                # too, so a DB seeded before the rule does not keep a stale row.
                sync_tenant_staff(
                    tenant_id=tenant.pk,
                    human_id=human.pk,
                    tenant_staff=is_tenant_staff([r.role_code for r in requests]),
                )
                User.objects.filter(pk=user.pk).update(
                    tenant=tenant, human=human, email=demo_email(username)
                )
                if RoleGrant.objects.filter(human=human).exists():
                    # Already granted - but a grant keeps the actions it was
                    # written with, so a login granted before a role template
                    # widened is short of the new ones. Append the difference
                    # (PRD §3.2 widened the owner's role on 22 September 2026).
                    top_up_person_grants(tenant, human, username)
                    continue
                if not requests:
                    continue

                def handler(
                    run: CommandRun, human: Any = human, requests: Any = requests
                ) -> CommandResult:
                    for request in requests:
                        add_grant(run, human, request)
                    return CommandResult(resource_type="human", resource_id=str(human.pk))

                execute_command(
                    service_principal(tenant.pk, "seed"),
                    CommandSpec(
                        action="seed.demo_grants",
                        command_id=_uuid.uuid5(tenant.deployment_key, f"demo-grants:{username}"),
                        business_input={"username": username},
                    ),
                    handler,
                )

    def _write_credentials(self) -> None:
        lines = [
            "# KDPS — Test Credentials",
            "",
            "Every login below is **synthetic demo data**, seeded by",
            "`python manage.py seed_foundation`. None of it describes a real person.",
            "Sign in with the email at the app root; API at `/api/auth/login` "
            "(email + password + CSRF token).",
            "",
            "## Legacy KDPS-DEMO logins",
            "",
            "| Email | Username | Password | Role | Scope |",
            "|---|---|---|---|---|",
        ]
        for username, password, role_code, scope, store_codes, _, brand_codes in USERS:
            units = store_codes or brand_codes
            scope_txt = f"{scope} ({','.join(units)})" if units else scope
            lines.append(
                f"| {demo_email(username)} | {username} | {password} | {role_code} | {scope_txt} |"
            )
        lines += [
            "",
            "`superadmin` is the only Django superuser (break-glass: bypasses the RBAC "
            "matrix, reaches `/admin`). Every other login — including `admin` (it_admin) "
            "and `owner` — is a normal user enforced by the section-access matrix.",
            "",
        ]
        lines += credentials_section()
        # Best-effort convenience dump of the demo logins. The data is already in
        # the DB, so never let a read-only or absent filesystem (e.g. Render's
        # build container) fail the seed.
        #
        # Resolved from the checkout, not hardcoded: the old "/app/memory/..."
        # default was the Emergent container's layout, so on a dev machine every
        # seed skipped the write against read-only "/app".
        default_path = Path(settings.BASE_DIR).parent / "test_credentials.md"
        path = Path(os.environ.get("SEED_CREDENTIALS_PATH", str(default_path)))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            self.stdout.write(self.style.WARNING(f"Skipped writing {path}: {exc}"))

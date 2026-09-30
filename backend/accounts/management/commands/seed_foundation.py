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
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.floors import clamp_to_floors, describe_floors
from accounts.goods_demo import credentials_section, seed_goods_demo
from accounts.models import NAV_GROUPS, Role, User
from accounts.rbac_matrix import section_access_for
from accounts.role_assignments import INITIAL_FIELD_ACCESS
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
        "code": "it_admin",
        "name": "Admin",
        "landing_page": "owner",
        "nav_groups": list(NAV_GROUPS),
        "description": "Owns users, RBAC, and all integration/adapter/config plumbing.",
    },
]

# The dedicated Django administration login is a separate platform identity.
# Its Django superuser flag never creates a tenant role assignment or business
# access; `admin` below is the explicit tenant Admin persona.
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
        if not tenant.synthetic:
            raise CommandError("Foundation fixture seeding is refused on a registered real company.")
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
        from accounts.goods_setup import LEGACY_SEEDED_ROLE_CODES
        from accounts.unified_policy import initial_step_actions
        from core.tenancy import require_tenant_id

        tenant_id = require_tenant_id()
        for r in ROLES:
            existing = Role.objects.select_for_update().filter(
                tenant_id=tenant_id, code=r["code"]
            ).first()
            # Existing policies are governed records. A seed must not fill in
            # removed cells, narrow a tuned policy, or restore revoked authority.
            if existing is not None:
                continue
            access, corrected = clamp_to_floors(
                r["code"], section_access_for(r["code"]),
            )
            for line in describe_floors(corrected):
                self.stdout.write(self.style.WARNING(f"{r['code']} narrowed to the floor: {line}"))
            Role.objects.update_or_create(
                tenant_id=tenant_id, code=r["code"],
                defaults={
                    "name": r["name"],
                    "landing_page": r["landing_page"],
                    "nav_groups": r["nav_groups"],
                    "section_access": access,
                    "field_access": existing.field_access if existing else INITIAL_FIELD_ACCESS.get(r["code"], []),
                    "permissions_map": existing.permissions_map if existing else {
                        "step_actions": initial_step_actions(r["code"])
                    },
                    "description": r["description"],
                    "is_system": True,
                    # Only on creation. Deactivating a role is an access change
                    # two administrators agree (#224 again, from the other side):
                    # switching it back on at the next deploy would re-open a
                    # seat they closed, and this one fails open.
                    **({} if existing else {"is_active": True}),
                },
            )
        Role.objects.filter(
            tenant_id=tenant_id, code__in=LEGACY_SEEDED_ROLE_CODES, is_system=True
        ).update(is_active=False)

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
        created_usernames: set[str] = set()
        for username, password, _role_code, _scope, _store_codes, full_name, _brand_codes in USERS:
            # The dedicated platform login is not a tenant Admin assignment.
            is_super = username in SUPERUSERS
            user, created = User.objects.get_or_create(
                username=username, tenant_id=entity.tenant_id,
                defaults={
                    "tenant_id": entity.tenant_id,
                    "full_name": full_name,
                    "is_active": True,
                    "is_staff": is_super,
                    "is_superuser": is_super,
                },
            )
            # Set the password only when the user is first created — never overwrite
            # an operator-changed password on a re-seed / redeploy.
            if created:
                created_usernames.add(username)
                user.set_password(password)
                user.save(update_fields=["password"])
        self._seed_goods_people(stores, created_usernames)

    def _seed_goods_people(
        self, stores: dict[str, Store], created_usernames: set[str]
    ) -> None:
        """Attach demo identities and explicit six-role assignments once.

        Unsupported personas remain usable test identities with no authority;
        their responsibilities await OQ-28 instead of being mapped to Owner.
        Existing RoleGrant rows remain historical evidence and are not topped up.
        """
        from accounts.goods_models import HumanIdentity, SecurityGuard
        from accounts.goods_setup import ensure_goods_roles, seed_role_assignment, sync_tenant_staff
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
                # Existing identities need the explicit migration report. A
                # seed may neither fill a missing identity link nor restore a
                # removed assignment by trusting a legacy username or role.
                if username not in created_usernames:
                    continue
                user = User.objects.get(username=username, tenant_id=tenant.pk)
                if HumanIdentity.objects.filter(tenant=tenant, staff_code=username).exists():
                    continue  # An orphaned identity needs explicit reconciliation.
                human = HumanIdentity.objects.create(
                    tenant=tenant, staff_code=username, display_name=full_name
                )
                SecurityGuard.objects.get_or_create(tenant=tenant, human=human)
                sync_tenant_staff(
                    tenant_id=tenant.pk,
                    human_id=human.pk,
                    tenant_staff=username != "superadmin",
                )
                User.objects.filter(pk=user.pk).update(
                    tenant=tenant, human=human, email=demo_email(username)
                )
                if username == "superadmin":
                    continue  # Platform support is never a tenant Admin assignment.
                if role_code in {"owner", "accounts", "it_admin"}:
                    seed_role_assignment(
                        tenant, human, role_code, source_key=f"demo:{username}",
                        all_sites=True, all_brands=True,
                    )
                elif role_code == "brand_manager":
                    brands = Brand.objects.filter(
                        tenant_id=tenant.pk, code__in=brand_codes
                    ).values_list("pk", flat=True)
                    seed_role_assignment(
                        tenant, human, role_code, source_key=f"demo:{username}",
                        all_sites=True, brand_ids=brands,
                    )
                elif role_code in {"store_person", "warehouse"}:
                    selected = [stores[code] for code in store_codes if code in stores]
                    if role_code == "warehouse" and not selected:
                        selected = warehouses
                    seed_role_assignment(
                        tenant, human, role_code, source_key=f"demo:{username}",
                        site_ids=[site.pk for site in selected], all_brands=True,
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
            "The role and scope columns above are historical seed labels. New demo",
            "users receive explicit SO-03 assignments only for the six mapped roles;",
            "existing users require the migration report before access is enabled.",
            "`ops1`, `promo1` and `steward` remain gated pending OQ-28.",
            "",
            "`superadmin` is a separate Django administration identity and has no "
            "tenant business assignment. `admin` and `owner` hold explicit "
            "scoped assignments governed by the six-role policy.",
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

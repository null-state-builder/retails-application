"""Fictional numbering inputs and read-only evidence for the damage sibling.

No origin, stock, bill, journal, damage report or decision is seeded. Every
business effect belongs to the canonical browser commands.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / ".local/first-store-damage-credentials.json"


def initialise() -> tuple[Any, dict[str, Any]]:
    if (os.environ.get("KDPS_PROOF_MODE") != "1"
            or not os.environ.get("KDPS_REHEARSAL_DB", "").startswith("kdps_rehearsal_damage_")):
        raise RuntimeError("Use the owned damage proof wrapper.")
    if stat.S_IMODE(RECORD.stat().st_mode) != 0o600:
        raise RuntimeError("The damage proof credential record must be private.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from accounts.registration_models import InstallationRegistration
    from accounts.registration_services import deployment_key
    from django.db import connection
    from masters.goods_models import Tenant

    record = json.loads(RECORD.read_text())
    expected = record["damage_rehearsal"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, system_identifier::text FROM pg_control_system()")
        actual = cursor.fetchone()
    if (actual != (expected["database"], "kdps_proof", expected["system_identifier"])
            or expected["database"] != os.environ["KDPS_REHEARSAL_DB"]
            or str(connection.settings_dict["HOST"]) != "127.0.0.1"
            or str(connection.settings_dict["PORT"]) != "55433"):
        raise RuntimeError("The owned damage database identity changed; preserved.")
    claim = InstallationRegistration.objects.select_related("tenant").get(
        deployment_key=deployment_key(), completed_at__isnull=False)
    if (Tenant.objects.count() != 1 or claim.tenant is None
            or not claim.tenant.synthetic or claim.tenant.code != "ALPHA"):
        raise RuntimeError("Only the sole fictional ALPHA tenant is permitted.")
    for who in ("manager", "owner", "admin", "count_checker"):
        if not record[who]["email"].endswith(".example.test"):
            raise RuntimeError("Only generated fictional proof people are permitted.")
    return claim, record


def snapshot(claim: Any, record: dict[str, Any]) -> dict[str, Any]:
    from core.canonical import content_hash, normalise
    from core.gl import GLEntry
    from django.db import connection, transaction
    from finledger.models import CashLedgerEntry
    from masters.goods_models import SiteGuard
    from outbound.goods_models import DamageReport
    from sell.cash_models import CashCount, CashCountBill, CashMovement
    from sell.models import OnlineSaleSubmission, Sale, SaleLine, SaleTender
    from sell.services.goods_stock import read_shelf
    from stockledger.goods_models import (
        ActiveHold,
        ActiveReservation,
        JournalBatch,
        Origin,
        Position,
        QuantityLeg,
        ValueLeg,
    )

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        guard = SiteGuard.objects.get(site_id=claim.first_store_id)
        positions = [{"id": str(row.pk), "lot_id": str(row.lot_id),
                      "origin_id": str(row.origin_id) if row.origin_id else None,
                      "accepted_event_id": str(row.accepted_event_id) if row.accepted_event_id else None,
                      "lower": row.portion.lower, "upper": row.portion.upper,
                      "condition": row.condition, "boundary": row.boundary,
                      "site_id": str(row.site_id) if row.site_id else None,
                      "location_id": str(row.location_id) if row.location_id else None,
                      "unit_cost_paise": str(row.origin.unit_cost) if row.origin else None}
                     for row in Position.objects.select_related("origin").order_by("pk")]
        money_models = (Sale, SaleLine, SaleTender, GLEntry, CashLedgerEntry,
                        CashCount, CashCountBill, CashMovement)
        money = {model._meta.label: list(model.objects.order_by("pk").values())
                 for model in money_models}
        data = {"synthetic_only": True, "identity": {key: value for key, value in record["damage_rehearsal"].items() if key != "deployment_key"},
                "site_id": str(claim.first_store_id),
                "freeze_id": str(guard.freeze_id) if guard.freeze_id else None,
                "sellable_qty": sum(read_shelf(guard.site).quantities.values()),
                "positions": positions,
                "stock_cost_paise": str(sum((row["upper"] - row["lower"]) * int(row["unit_cost_paise"] or 0)
                                            for row in positions if row["boundary"] == "physical")),
                "origins": list(Origin.objects.values("id", "opening_qty", "unit_cost", "mrp").order_by("pk")),
                "origin_hash": content_hash(list(Origin.objects.order_by("pk").values())),
                "journals": list(JournalBatch.objects.order_by("pk").values("id", "posting_kind", "version_id")),
                "quantity_legs": list(QuantityLeg.objects.order_by("pk").values("id", "qty", "batch_id")),
                "value_legs": list(ValueLeg.objects.order_by("pk").values("id", "amount", "batch_id")),
                "holds": list(ActiveHold.objects.order_by("pk").values("id", "hold_key", "lot_id", "portion", "kind")),
                "reservations": list(ActiveReservation.objects.order_by("pk").values()),
                "money_hash": content_hash(money), "bills": Sale.objects.count(),
                "accepted_intents": OnlineSaleSubmission.objects.filter(status="accepted").count(),
                "rejected_intents": OnlineSaleSubmission.objects.filter(status="rejected").count(),
                "damage_reports": list(DamageReport.objects.order_by("pk").values(
                    "id", "state", "quantity", "reporter_id", "reviewer_id", "release_movement_id"))}
        return normalise(data)


def prepare(claim: Any, record: dict[str, Any]) -> dict[str, Any]:
    from core.numbering import prepare_series
    from masters.goods_models import Location
    from masters.models import Store
    from stockledger.goods_models import Origin

    before = snapshot(claim, record)
    if before["freeze_id"] or before["sellable_qty"] != 2 or before["bills"] != 1:
        raise RuntimeError("Prepare only the untouched completed two-piece source clone; existing proof preserved.")
    site = Store.objects.get(pk=claim.first_store_id, code="FIRST")
    for kind in ("HLD", "REL"):
        prepare_series(claim.tenant_id, site.gstin.legal_entity, kind)
    origin = Origin.objects.get()
    record["damage_fixture"] = {"synthetic_only": True, "site_id": str(site.pk),
        "sku_id": str(origin.sku_id), "origin_id": str(origin.pk),
        "floor_id": str(Location.objects.get(site=site, kind="floor").pk), "barcode": "ALPHA000123"}
    descriptor, temporary = tempfile.mkstemp(prefix="damage-inputs-", dir=RECORD.parent)
    with os.fdopen(descriptor, "w") as output:
        json.dump(record, output)
    os.replace(temporary, RECORD)
    assert snapshot(claim, record) == before
    return record["damage_fixture"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "snapshot"])
    args = parser.parse_args()
    claim, record = initialise()
    from core.tenancy import tenant_context
    with tenant_context(claim.tenant_id):
        result = prepare(claim, record) if args.action == "prepare" else snapshot(claim, record)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

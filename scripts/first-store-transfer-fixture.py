"""Governed fictional setup and read-only reconciliation for transfer browser proof.

Every site, person, assignment, policy and document-series setup uses canonical
APIs with separate review. This helper never writes stock, transfers or journals.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / ".local/first-store-transfer-credentials.json"


def initialise() -> tuple[Any, Any, dict[str, Any]]:
    if (os.environ.get("KDPS_PROOF_MODE") != "1"
            or not os.environ.get("KDPS_REHEARSAL_DB", "").startswith("kdps_rehearsal_transfer_")):
        raise RuntimeError("Use the positively verified transfer wrapper.")
    if stat.S_IMODE(RECORD.stat().st_mode) != 0o600:
        raise RuntimeError("Transfer credentials must remain private.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from accounts.registration_models import InstallationRegistration
    from accounts.registration_services import deployment_key
    from django.db import connection

    record = json.loads(RECORD.read_text())
    expected = record["transfer_rehearsal"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, system_identifier::text FROM pg_control_system()")
        actual = cursor.fetchone()
    if (actual != (expected["database"], "kdps_proof", expected["system_identifier"])
            or connection.settings_dict["HOST"] != "127.0.0.1"
            or str(connection.settings_dict["PORT"]) != "55433"):
        raise RuntimeError("Exact owned transfer endpoint identity changed; no work was done.")
    claim = InstallationRegistration.objects.select_related("tenant").get(deployment_key=deployment_key(), completed_at__isnull=False)
    if claim.tenant is None or not claim.tenant.synthetic or claim.tenant.code != "ALPHA":
        raise RuntimeError("Only the synthetic ALPHA sibling is permitted.")
    spec = importlib.util.spec_from_file_location("transfer_api_helper", ROOT / "scripts/first-store-onboarding-proof.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The canonical API proof helper is unavailable.")
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    proof.RECORD = RECORD
    for who in ("owner", "admin", "manager"):
        if not record[who]["email"].endswith(".example.test"):
            raise RuntimeError("Only generated fictional humans are accepted.")
    for who in ("owner", "admin"):
        if claim.summary[who]["email"] != record[who]["email"]:
            raise RuntimeError("The copied fictional authority does not match the signed registration.")
    return proof, claim, record


def snapshot(claim: Any, record: dict[str, Any]) -> dict[str, Any]:
    from core.canonical import content_hash, normalise
    from finledger.models import CashLedgerEntry
    from masters.goods_models import SiteGuard
    from masters.models import Store
    from sell.models import Sale, SaleTender
    from sell.services.goods_stock import read_shelf
    from stockledger.goods_models import (
        ActiveReservation,
        JournalBatch,
        Origin,
        Position,
        ValueLeg,
    )

    fixture = record["transfer_fixture"]
    source = Store.objects.get(pk=claim.first_store_id)
    destination = Store.objects.get(pk=fixture["destination_site_id"])
    positions = [{"site_id": str(row.site_id), "origin_id": str(row.origin_id) if row.origin_id else None,
                  "qty": row.portion.upper - row.portion.lower, "boundary": row.boundary,
                  "accepted": row.accepted_event_id is not None, "condition": row.condition,
                  "unit_cost_paise": str(row.origin.unit_cost) if row.origin else None}
                 for row in Position.objects.select_related("origin").order_by("site_id", "lot_id", "portion")]
    originals = list(Origin.objects.values("id", "sku_id", "opening_qty", "unit_cost", "mrp").order_by("pk"))
    sales = list(Sale.objects.values("id", "doc_number", "net_paise", "idempotency_uuid").order_by("pk"))
    tenders = list(SaleTender.objects.values("id", "amount_paise").order_by("pk"))
    data = {"synthetic_only": True, "source_shelf_qty": sum(read_shelf(source).quantities.values()),
            "destination_shelf_qty": sum(read_shelf(destination).quantities.values()),
            "positions": positions, "origins_hash": content_hash(normalise(originals)),
            "sales_hash": content_hash(normalise(sales)), "tenders_hash": content_hash(normalise(tenders)),
            "cash_hash": content_hash(normalise(list(CashLedgerEntry.objects.values().order_by("pk")))),
            "value_hash": content_hash(normalise(list(ValueLeg.objects.values().order_by("pk")))),
            "reservations": ActiveReservation.objects.count(),
            "journals": list(JournalBatch.objects.values("id", "posting_kind").order_by("pk")),
            "freeze_ids": list(SiteGuard.objects.values_list("freeze_id", flat=True))}
    return normalise(data)


def prepare(proof: Any, claim: Any, record: dict[str, Any]) -> dict[str, Any]:
    from accounts.models import User
    from approvals.goods_models import ApprovalRequest
    from core.documents import VoucherSeries
    from core.fiscal import financial_year
    from core.numbering import publish_ceiling, series_store_code
    from django.utils import timezone
    from masters.goods_models import ConfigVersion
    from masters.models import Store
    from stockledger.goods_models import Origin

    fixture = record.setdefault("transfer_fixture", {})
    fixture.setdefault("effective_from", timezone.now().isoformat())
    person = record.setdefault("receiver", {"email": f"receiver-{uuid.uuid4().hex[:12]}@transfer.example.test",
        "temporary_password": secrets.token_urlsafe(24) + "A1!", "password": secrets.token_urlsafe(24) + "A1!"})
    proof.save(record)

    def actor(who: str) -> Any:
        client = proof.login(record, who)
        proof.request(client, "post", "/api/auth/step-up", {"password": record[who]["password"]})
        return client

    def publish(key: str, kind: str, payload: dict[str, Any], scope: dict[str, Any] | None = None) -> ConfigVersion:
        admin = actor("admin")
        draft = proof.request(admin, "post", "/api/goods-v1/masters/configurations", proof.command(record, f"transfer-{key}-create",
            kind=kind, scope=scope or {"scope_kind": "tenant"}, effective_from=fixture["effective_from"], payload=payload))
        existing = ConfigVersion.objects.filter(draft_id=draft["id"]).first()
        if existing is not None:
            return existing
        proof.request(admin, "post", f"/api/goods-v1/masters/configurations/{draft['id']}/submit", proof.command(record,
            f"transfer-{key}-submit", expected_revision=draft["revision"], reviewed_hash=draft["content_hash"]))
        approval = ApprovalRequest.objects.get(subject_kind="configuration", subject_key=f"configdraft:{draft['id']}")
        proof.request(actor("owner"), "post", f"/api/goods-v1/approvals/{approval.pk}/decide", proof.command(record,
            f"transfer-{key}-approve", expected_revision=approval.revision, reviewed_hash=approval.reviewed_hash, decision="approve"))
        return ConfigVersion.objects.get(draft_id=draft["id"])

    source = Store.objects.select_related("gstin").get(pk=claim.first_store_id, code="FIRST")
    origin = Origin.objects.get(sku__aliases__value="ALPHA000123")
    owner = actor("owner")
    created = proof.request(owner, "post", "/api/goods-v1/masters/stores", proof.command(record, "transfer-destination-site",
        code="TRANSFER-PROOF", name="Fictional transfer destination", type="store", entity_id=str(source.gstin.legal_entity_id),
        registration_id=str(source.gstin_id), city="Fictional city", brand_ids=[str(origin.sku.style.brand_id)],
        permitted_operations=["receive", "transfer"]))
    destination_id = str(created["id"])
    site_path = f"/api/goods-v1/masters/stores/{destination_id}"
    locations = proof.request(owner, "get", f"{site_path}/locations")
    floor = next((row for row in locations["items"] if row["data"]["kind"] == "floor"), None)
    if floor is None:
        current = proof.request(owner, "get", site_path)
        floor = proof.request(owner, "post", f"{site_path}/locations", proof.command(record, "transfer-destination-floor",
            name="Fictional destination sales floor", kind="floor", expected_revision=current["revision"]))
    staff = proof.request(owner, "post", "/api/auth/admin/staff", proof.command(record, "transfer-receiver-staff",
        staff_code="TRANSFER-PROOF", display_name="Fictional receiving store person", site_id=int(destination_id),
        effective_from=fixture["effective_from"], salesperson=False))
    user = User.objects.filter(email=person["email"]).first()
    if user is None:
        created_user = proof.request(owner, "post", "/api/auth/admin/users", proof.command(record, "transfer-receiver-user",
            human_id=staff["data"]["human_id"], email=person["email"], display_name="Fictional receiving store person",
            password=person["temporary_password"], active=True, identity_email_confirmed=True))
        user = User.objects.get(pk=created_user["id"])
    # Creating access can invalidate the acting session; each independent
    # command obtains current authority without retrying a failed mutation.
    owner = actor("owner")
    if str(user.human_id) != staff["data"]["human_id"]:
        raise RuntimeError("Existing receiver has another stable human identity; preserved.")
    assignment_path = f"/api/auth/admin/users/{user.pk}/assignments"
    assignments = proof.request(owner, "get", assignment_path)
    desired = {"role_code": "store_person", "all_sites": False, "site_ids": [int(destination_id)], "all_brands": True, "brand_ids": []}
    if not any(row["role_code"] == "store_person" and row["site_ids"] == [int(destination_id)] and row["all_brands"] for row in assignments["items"]):
        body = proof.command(record, "transfer-receiver-assignment", expected_revision=assignments["revision"],
            assignments=[*assignments["items"], desired])
        body["current_password"] = record["owner"]["password"]
        proof.request(owner, "put", assignment_path, body)
    admin = actor("admin")
    events = proof.request(admin, "get", "/api/auth/admin/privileged-changes", {"limit": "100"})
    owned = {record["commands"][key]["command_id"] for key in ("transfer-receiver-user", "transfer-receiver-assignment")}
    from core.kernel_models import AuditEvent, PrivilegedReview
    audits = list(AuditEvent.objects.filter(command_key__command_id__in=owned, outcome="succeeded"))
    for event in events["items"]:
        if str(event["id"]) in {str(row.pk) for row in audits} and "review" in event.get("allowed_actions", []):
            proof.request(admin, "post", f"/api/auth/admin/privileged-changes/{event['id']}/review", proof.command(record,
                f"transfer-review-{event['id']}", note="Independent review of fictional destination-scoped receiver for synthetic transfer proof."))
    if len(audits) != 2 or any(not PrivilegedReview.objects.filter(audit_event=event, reviewer_id=User.objects.get(email=record["admin"]["email"]).human_id).exists() for event in audits):
        raise RuntimeError("Receiver privileged setup lacks separate Admin review evidence.")
    if user.must_change_password:
        client = proof.login(record, "receiver", temporary=True)
        proof.request(client, "post", "/api/auth/change-password", {"current_password": person["temporary_password"], "new_password": person["password"]})
        user.refresh_from_db()
    if user.must_change_password or not user.check_password(person["password"]):
        raise RuntimeError("Receiver password replacement was not verified.")
    person.pop("temporary_password", None)
    publish("approval", "approval", {"action": "pt.approve.transfer", "roles": ["owner"], "site_ids": [], "brand_ids": [],
        "require_distinct": True, "qty_max": 100, "value_max": "10000000", "step_up": True, "unknown_value": "refuse"})
    entity_id = source.gstin.legal_entity_id
    publish("series-TPT", "series", {"entity_id": str(entity_id), "type": "TPT", "fy": financial_year(), "ceiling_block_size": 100},
        {"scope_kind": "entity", "entity_id": str(entity_id)})
    series = VoucherSeries.objects.get(store_code=series_store_code(entity_id), fy=financial_year(), doc_type="TPT", scope_version="entity_v1")
    publish_ceiling(claim.tenant_id, series.pk, target=series.ceiling_block_size)
    fixture.update(synthetic_only=True, source_site_id=str(source.pk), destination_site_id=destination_id,
        destination_floor_id=str(floor["id"]), sku_id=str(origin.sku_id), origin_id=str(origin.pk), barcode="ALPHA000123")
    proof.save(record)
    return {**fixture, "snapshot": snapshot(claim, record)}


def expire_receiver_session(proof: Any, claim: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Force expiry of one exact fictional browser session; never business data."""
    from datetime import timedelta

    from accounts.goods_models import ServerSession
    from accounts.models import User
    from core.commands import database_now
    from django.db import transaction

    body = json.load(sys.stdin)
    digest = body.get("session_hash")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise RuntimeError("An exact browser session hash is required for forced test expiry.")
    person = record["receiver"]
    if not person["email"].endswith("@transfer.example.test"):
        raise RuntimeError("Only the generated transfer receiver may be expired.")
    with transaction.atomic():
        receiver = User.objects.get(tenant_id=claim.tenant_id, email=person["email"], is_active=True, must_change_password=False)
        if receiver.human_id is None or not receiver.check_password(person["password"]):
            raise RuntimeError("The exact fictional receiver identity changed.")
        session = ServerSession.objects.select_for_update().get(tenant_id=claim.tenant_id, user=receiver, token_hash=digest)
        now = database_now()
        if session.revoked_at is not None or session.expires_at <= now:
            raise RuntimeError("Forced expiry requires the exact live, unrevoked browser session.")
        before = snapshot(claim, record)
        prior_expiry = session.expires_at
        session.expires_at = now - timedelta(seconds=1)
        session.save(update_fields=["expires_at"])
        if snapshot(claim, record) != before:
            raise RuntimeError("Forced session expiry unexpectedly changed business evidence.")
        evidence = {"synthetic_only": True, "forced_test_expiry": True, "session_id": str(session.pk),
                    "previous_expires_at": prior_expiry.isoformat(), "expires_at": session.expires_at.isoformat(),
                    "session_row_preserved": True, "business_unchanged": True}
    record.setdefault("transfer_session_expiry", []).append(evidence)
    proof.save(record)
    return evidence


def reconcile(proof: Any, claim: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Verify the retained movement and conservation through canonical reads."""
    from outbound.goods_models import TransferEvent
    from sell.models import TillPause

    fixture = record["transfer_fixture"]
    browser = fixture["browser"]
    before, after = browser["baseline"], snapshot(claim, record)
    transfer_id = browser["transfer_id"]
    client = proof.login(record, "owner")
    detail = proof.request(client, "get", f"/api/goods-v1/outbound/transfers/{transfer_id}")
    if (detail["state"] != "completed" or not detail["reconciliation"]["balanced"]
            or detail["reserved_qty"] != 0 or detail["in_transit_qty"] != 0
            or len(detail["dispatches"]) != 1 or detail["dispatches"][0]["state"] != "accepted"
            or after["source_shelf_qty"] != 1 or after["destination_shelf_qty"] != 1
            or after["reservations"] != before["reservations"]
            or any(after[key] != before[key] for key in ("origins_hash", "sales_hash", "tenders_hash", "cash_hash", "value_hash"))
            or any(after["freeze_ids"])
            or TillPause.objects.filter(till__store_id=claim.first_store_id, resumed_at__isnull=True).exists()):
        raise RuntimeError("The completed fictional transfer did not reconcile; all history was preserved.")
    physical = [row for row in after["positions"] if row["boundary"] == "physical"]
    if (sum(row["qty"] for row in physical) != 2
            or any(row["origin_id"] != fixture["origin_id"] or row["unit_cost_paise"] != "50000" or not row["accepted"] for row in physical)
            or sum(row["qty"] for row in after["positions"] if row["boundary"] == "transit") != 0):
        raise RuntimeError("Original quantity, origin or valuation was not conserved; preserved without reset.")
    if snapshot(claim, record) != after:
        raise RuntimeError("A canonical reconciliation read changed business state.")
    return {"synthetic_only": True, "reconciled": True, "transfer_id": transfer_id,
            "source_shelf_qty": 1, "destination_shelf_qty": 1, "physical_qty": 2, "transit_qty": 0,
            "original_origin_preserved": True, "unit_cost_paise": "50000", "financial_hashes_unchanged": True,
            "source_till_resumed": True, "canonical_reconciliation": detail["reconciliation"],
            "events": list(TransferEvent.objects.filter(transfer_id=transfer_id).order_by("recorded_at", "pk").values("id", "kind", "recorded_at"))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "snapshot", "expire-receiver-session", "reconcile"])
    args = parser.parse_args()
    proof, claim, record = initialise()
    from core.tenancy import tenant_context
    with tenant_context(claim.tenant_id):
        action = {"prepare": lambda: prepare(proof, claim, record), "snapshot": lambda: snapshot(claim, record),
                  "expire-receiver-session": lambda: expire_receiver_session(proof, claim, record),
                  "reconcile": lambda: reconcile(proof, claim, record)}
        print(json.dumps(action[args.action](), sort_keys=True, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- do not expose generated identities or credentials in tracebacks.
        print(str(error) if isinstance(error, RuntimeError) else f"Transfer proof failed ({type(error).__name__}); preserved for inspection.")
        raise SystemExit(1) from None

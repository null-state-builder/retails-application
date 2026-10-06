"""Advance the owned browser rehearsal through ordinary first-party commands.

Run only via ``first-store-proof.py run``. This never seeds, bypasses session
guards, edits model rows or prints credentials. Its private credential record
contains only generated example.test people from the browser signup.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / ".local/first-store-browser-credentials.json"


def load() -> dict[str, Any]:
    if stat.S_IMODE(RECORD.stat().st_mode) != 0o600:
        raise RuntimeError("The proof credential record must be private (0600).")
    result = json.loads(RECORD.read_text())
    if not isinstance(result, dict):
        raise TypeError("The proof credential record is invalid.")
    for who in ("owner", "admin"):
        if not str(result.get(who, {}).get("email", "")).endswith(".example.test"):
            raise RuntimeError("Only generated example.test proof people are accepted.")
    return result


def save(record: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="first-store-credentials-", dir=RECORD.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, RECORD)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialise() -> Any:
    if os.environ.get("KDPS_PROOF_MODE") != "1" or not os.environ.get("KDPS_REHEARSAL_DB", "").startswith("kdps_rehearsal_first_store_"):
        raise RuntimeError("Use the positively identified first-store proof wrapper.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from accounts.registration_models import InstallationRegistration
    from accounts.registration_services import deployment_key
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, system_identifier::text FROM pg_control_system()")
        row = cursor.fetchone()
    rehearsal = json.loads((ROOT / ".local/first-store-proof.json").read_text())
    if row != (rehearsal["database"], "kdps_proof", rehearsal["system_identifier"]):
        raise RuntimeError("The rehearsal identity does not match; nothing was run.")
    claim = InstallationRegistration.objects.select_related("tenant").get(deployment_key=deployment_key(), completed_at__isnull=False)
    if claim.tenant is None or claim.summary["company"]["code"] != "ALPHA":
        raise RuntimeError("This fixture only owns the browser's ALPHA proof registration.")
    for who in ("owner", "admin"):
        if not claim.summary[who]["email"].endswith(".example.test"):
            raise RuntimeError("This fixture cannot operate real company identities.")
    return claim


def request(client: Any, method: str, path: str, body: Any = None) -> Any:
    headers = {"HTTP_HOST": "127.0.0.1"}
    if method != "get":
        csrf = client.cookies.get("kdps_csrf")
        if csrf is None:
            raise RuntimeError("A proof session has no CSRF token.")
        headers["HTTP_X_CSRF_TOKEN"] = csrf.value
    response = getattr(client, method)(path, data=body, format="json", **headers)
    if response.status_code >= 400:
        data = getattr(response, "data", {})
        code = data.get("code", "HTTP_ERROR") if isinstance(data, dict) else "HTTP_ERROR"
        # Never include a body, password, cookie, token or raw exception repr.
        raise RuntimeError(f"{method.upper()} {path}: {response.status_code} {code}")
    return response.data


def login(record: dict[str, Any], who: str, *, temporary: bool = False) -> Any:
    from rest_framework.test import APIClient

    client = APIClient()
    request(client, "get", "/api/auth/csrf")
    person = record[who]
    request(client, "post", "/api/auth/login", {"email": person["email"], "password": person["temporary_password" if temporary else "password"]})
    return client


def command(record: dict[str, Any], key: str, **body: Any) -> dict[str, Any]:
    commands = record.setdefault("commands", {})
    if key not in commands:
        commands[key] = {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1", **body}
        save(record)
    return dict(commands[key])


def passwords(record: dict[str, Any], claim: Any) -> None:
    from accounts.models import User
    from core.tenancy import tenant_context

    with tenant_context(claim.tenant_id):
        for who in ("owner", "admin", "manager"):
            if who not in record:
                continue
            person = record[who]
            person.setdefault("password", secrets.token_urlsafe(24) + "A1!")
            save(record)  # A lost reply can always retry the generated replacement.
            user = User.objects.get(tenant_id=claim.tenant_id, email=person["email"])
            if user.must_change_password:
                if user.check_password(person["password"]):
                    raise RuntimeError("Unexpected temporary credential state; manual proof review required.")
                client = login(record, who, temporary=True)
                request(client, "post", "/api/auth/change-password", {"current_password": person["temporary_password"], "new_password": person["password"]})
                user.refresh_from_db()
            if not user.check_password(person["password"]) or user.must_change_password:
                raise RuntimeError("The proof person's password change was not verified.")
            login(record, who)
            person.pop("temporary_password", None)
            person["password_changed"] = True
            save(record)
    print("Proof people's first password changes completed through the actual API; sessions were invalidated and fresh logins verified.")


def calendar(record: dict[str, Any], claim: Any) -> None:
    from approvals.goods_models import ApprovalRequest
    from core.tenancy import tenant_context
    from django.utils import timezone
    from masters.goods_models import ConfigVersion

    with tenant_context(claim.tenant_id):
        if ConfigVersion.objects.filter(kind="working_calendar").exists():
            print("Existing proof calendar preserved; no version was replaced.")
            return
        admin = login(record, "admin")
        draft = request(admin, "post", "/api/goods-v1/masters/configurations", command(record, "calendar-create", kind="working_calendar", scope={"scope_kind": "tenant"}, effective_from=timezone.now().isoformat(), payload={"timezone": "Asia/Kolkata", "working_weekdays": list(range(1, 8)), "excluded_dates": []}))
        request(admin, "post", f"/api/goods-v1/masters/configurations/{draft['id']}/submit", command(record, "calendar-submit", expected_revision=draft["revision"], reviewed_hash=draft["content_hash"]))
        approval = ApprovalRequest.objects.get(subject_kind="configuration", subject_key=f"configdraft:{draft['id']}")
        owner = login(record, "owner")
        request(owner, "post", "/api/auth/step-up", {"password": record["owner"]["password"]})
        request(owner, "post", f"/api/goods-v1/approvals/{approval.pk}/decide", command(record, "calendar-approve", expected_revision=approval.revision, reviewed_hash=approval.reviewed_hash, decision="approve"))
        if not ConfigVersion.objects.filter(kind="working_calendar").exists():
            raise RuntimeError("The independently approved proof calendar was not published.")
    print("An explicit proof-only calendar was drafted by Admin and approved by the different Owner through normal commands.")


def activate(record: dict[str, Any], claim: Any) -> None:
    from accounts.models import User
    from accounts.role_assignments import effective_assignments
    from core.kernel_models import AuditEvent, PrivilegedReview
    from core.tenancy import tenant_context
    from django.utils import timezone

    with tenant_context(claim.tenant_id):
        people = claim.summary["proposed_team"]
        if len(people) != 1 or people[0]["role_code"] != "store_person" or not people[0]["email"].endswith(".example.test"):
            raise RuntimeError("This proof expects its one proposed example.test manager.")
        proposed = people[0]
        record.setdefault("manager", {"email": proposed["email"], "temporary_password": secrets.token_urlsafe(24) + "A1!"})
        save(record)
        owner = login(record, "owner")
        staff = request(owner, "post", "/api/auth/admin/staff", command(record, "manager-staff-reserved-code-v2", staff_code=proposed["staff_code"], registration_email=proposed["email"], display_name=proposed["name"], site_id=claim.first_store_id, effective_from=timezone.now().isoformat(), salesperson=True))
        request(owner, "post", "/api/auth/step-up", {"password": record["owner"]["password"]})
        user_command = command(record, "manager-user", human_id=staff["data"]["human_id"], email=proposed["email"], display_name=proposed["name"], active=True, identity_email_confirmed=True)
        existing_manager = User.objects.filter(tenant_id=claim.tenant_id, email=proposed["email"]).first()
        if existing_manager is None:
            user_command["password"] = record["manager"]["temporary_password"]
            manager_id = request(owner, "post", "/api/auth/admin/users", user_command)["id"]
        elif str(existing_manager.human_id) == staff["data"]["human_id"]:
            manager_id = existing_manager.pk
        else:
            raise RuntimeError("An existing proof manager has a different staff identity; nothing was relinked.")
        admin_user = User.objects.get(tenant_id=claim.tenant_id, email=record["admin"]["email"])
        for who, target_id, role_code in (("manager", manager_id, "store_person"), ("admin", admin_user.pk, "warehouse")):
            path = f"/api/auth/admin/users/{target_id}/assignments"
            current = request(owner, "get", path)
            already = any(item["role_code"] == role_code and item["site_ids"] == [claim.first_store_id] and item["all_brands"] for item in current["items"])
            if already:
                continue
            body = command(record, f"{who}-assignments", expected_revision=current["revision"], assignments=[*current["items"], {"role_code": role_code, "all_sites": False, "site_ids": [claim.first_store_id], "all_brands": True, "brand_ids": []}])
            body["current_password"] = record["owner"]["password"]
            request(owner, "put", path, body)
        for target_id, role_code in ((manager_id, "store_person"), (admin_user.pk, "warehouse")):
            target = User.objects.get(tenant_id=claim.tenant_id, pk=target_id)
            if not any(row.role.code == role_code and not row.all_sites and row.site_ids == [claim.first_store_id] and row.all_brands for row in effective_assignments(target.human_id)):
                raise RuntimeError("Existing proof access was independently edited or revoked; its state was preserved and no activation is claimed.")
        # Admin's assignment update deliberately ended its older sessions.
        admin = login(record, "admin")
        request(admin, "post", "/api/auth/step-up", {"password": record["admin"]["password"]})
        events = request(admin, "get", "/api/auth/admin/privileged-changes", {"limit": "100"})
        for event in events["items"]:
            if "review" in event.get("allowed_actions", []) and event["data"]["action"] in {"access.user.create", "access.assignment.replace"}:
                request(admin, "post", f"/api/auth/admin/privileged-changes/{event['id']}/review", command(record, f"review-{event['id']}", note="Independent review of generated first-store alpha proof setup; no real business authority or data."))
        owned_keys = [record["commands"][key]["command_id"] for key in ("manager-user", "manager-assignments", "admin-assignments")]
        changes = list(AuditEvent.objects.filter(command_key__command_id__in=owned_keys, outcome="succeeded"))
        if len(changes) != 3 or any(not PrivilegedReview.objects.filter(audit_event=event, reviewer_id=admin_user.human_id).exists() for event in changes):
            raise RuntimeError("The proof setup's three privileged changes do not all have independent Admin review evidence.")
    passwords(record, claim)
    print("The proposed proof manager and scoped Warehouse preparation assignment were activated and independently reviewed through normal administration.")


def personal_pin(record: dict[str, Any], claim: Any) -> None:
    from accounts.models import User
    from accounts.till_pin import (
        may_hold_till_pin,
        may_set_personal_till_pin,
        verify_till_pin,
    )
    from core.tenancy import tenant_context

    with tenant_context(claim.tenant_id):
        user = User.objects.get(tenant_id=claim.tenant_id, email=record["manager"]["email"])
        if not may_set_personal_till_pin(user, site_id=claim.first_store_id) or may_hold_till_pin(user, site_id=claim.first_store_id):
            raise RuntimeError("Proof manager must have personal credential eligibility and no exception approval authority.")
        person = record["manager"]
        person.setdefault("personal_pin", "".join(secrets.choice("0123456789") for _ in range(6)))
        while len(set(person["personal_pin"])) == 1:
            person["personal_pin"] = "".join(secrets.choice("0123456789") for _ in range(6))
        save(record)
        if not verify_till_pin(person["personal_pin"], user.till_pin_hash):
            client = login(record, "manager")
            request(client, "put", "/api/auth/me/till-pin", {"pin": person["personal_pin"], "current_password": person["password"]})
            user.refresh_from_db()
        if not verify_till_pin(person["personal_pin"], user.till_pin_hash) or may_hold_till_pin(user, site_id=claim.first_store_id):
            raise RuntimeError("The personal proof credential changed business approval authority.")
    print("Manager's own personal PIN was set through password-confirmed API; separate counter exception approval remains denied.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["password-change", "calendar", "activate-team", "personal-pin"])
    args = parser.parse_args()
    claim = initialise()
    record = load()
    for who in ("owner", "admin"):
        if claim.summary[who]["email"] != record[who]["email"]:
            raise RuntimeError("Private proof credentials do not match the owned registration.")
    {"password-change": passwords, "calendar": calendar, "activate-team": activate, "personal-pin": personal_pin}[args.action](record, claim)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- Do not leak fixture credentials through framework tracebacks.
        # API failures already carry safe status/code. Avoid raw framework
        # tracebacks which could disclose generated fixture credentials.
        print(str(error) if isinstance(error, RuntimeError) else f"Proof command failed ({type(error).__name__}); inspect the governed resource before retrying.")
        raise SystemExit(1) from None

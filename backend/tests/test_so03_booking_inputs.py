"""SO-03: canonical booking inputs govern OTB scope and protected delivery."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import ServerSession
from accounts.models import Role
from accounts.principal import AccessContext
from accounts.sessions import revoke_session
from approvals.unified_authority import source
from core.commands import CommandRun
from core.goods_documents import append_revision, new_document
from core.kernel_models import DocumentIdentity
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.models import Season, Store
from tests.first_store_goods import command, live_access
from tests.test_so03_denials import TenantWorld, _assign, _person
from tests.test_so03_denials import worlds as worlds
from vendors import goods_views, open_to_buy_views
from vendors.goods_models import GoodsBooking
from vendors.goods_services import booking_content_hash, booking_head, parse_booking
from vendors.goods_views import GoodsBookingAccessPreviewView, GoodsBookingDetailView
from vendors.models import Vendor
from vendors.open_to_buy_models import OpenToBuyAsk
from vendors.open_to_buy_views import GoodsBookingOpenToBuyView, GoodsOpenToBuyAskDetailView


@dataclass
class BookingInputs:
    access: AccessContext
    booking: GoodsBooking
    season: Season
    home: Store
    header_site: Store
    line_site: Store
    header: dict[str, Any]
    lines: list[tuple[uuid.UUID, Mapping[str, Any]]]


def _booking(world: TenantWorld, *, conflict: str = "", other: TenantWorld | None = None) -> BookingInputs:
    user, human = _person(world, "canonical-booking-owner")
    _assign(world, human, "owner", all_sites=True, all_brands=True)
    access = live_access(user)
    home, header_site = world.sites
    line_site = Store.objects.create(tenant=world.tenant, gstin=home.gstin,
                                     code=f"third-{uuid.uuid4().hex[:6]}", name="Third proof destination")
    assert other is not None or not conflict
    if conflict in {"home_site", "header_site", "line_site"}:
        assert other is not None
        if conflict == "home_site":
            home = other.sites[0]
        elif conflict == "header_site":
            header_site = other.sites[0]
        else:
            line_site = other.sites[0]
    vendor = Vendor.objects.create(tenant=world.tenant, code=f"proof-{uuid.uuid4().hex[:8]}", name="Proof vendor")
    season = Season.objects.create(code=f"proof-{uuid.uuid4().hex[:8]}", name="Proof season")
    data = parse_booking({
        "vendor_id": vendor.pk, "brand_id": world.brands[1 if conflict == "header_brand" else 0].pk,
        "season_id": season.pk, "entity_id": world.sites[0].gstin.legal_entity_id,
        "destination_site_id": header_site.pk,
        "lines": [{"line_key": str(uuid.uuid4()), "style_code": "PROOF-SHIRT", "qty": qty,
                   "destination_site_id": destination.pk, "cost_paise": "12000", "mrp_paise": "18000"}
                  for qty, destination in ((2, line_site), (3, world.sites[0]))],
    })
    canonical_lines: list[tuple[uuid.UUID, Mapping[str, Any]]] = [(key, payload) for key, payload in data.lines]

    def create(run: CommandRun) -> GoodsBooking:
        identity, head = new_document(run, kind="booking", purpose=DocumentIdentity.Purpose.BOOKING,
                                     entity_id=data.entity_id, site_id=home.pk)
        append_revision(run, head, header=data.header, replace_lines=canonical_lines)
        return GoodsBooking.objects.create(tenant_id=run.tenant_id, document=identity,
                                           vendor=vendor, brand=world.brands[0])

    booking = cast(GoodsBooking, command(access, "proof.booking.inputs", create))
    return BookingInputs(access, booking, season, home, header_site, line_site, data.header, canonical_lines)


def _ask(proof: BookingInputs, *, brand_id: int | None = None, season_id: int | None = None,
         access: AccessContext | None = None, site_id: int | None = None,
         draft_hash: str | None = None) -> OpenToBuyAsk:
    actor = access or proof.access

    def create(run: CommandRun) -> OpenToBuyAsk:
        return OpenToBuyAsk.objects.create(
            tenant_id=run.tenant_id, booking=proof.booking,
            draft_hash=draft_hash or booking_content_hash(proof.booking, booking_head(proof.booking)),
            brand_id=brand_id or proof.booking.brand_id, season_id=season_id or proof.season.pk,
            site_id=(site_id or proof.home.pk) if run.tenant_id == proof.booking.tenant_id else None,
            figures=[{"budget_id": 1, "site_id": proof.home.pk, "open_to_buy_paise": "0",
                      "booking_paise": "60000", "over_paise": "60000", "cost_missing_pieces": 0}],
            over_paise=60000, asked_by=actor.user,
        )

    return cast(OpenToBuyAsk, command(actor, "proof.otb.inputs", create))


def _request(access: AccessContext, params: dict[str, Any] | None = None) -> Any:
    request = APIRequestFactory().get("/booking-proof", params or {})
    force_authenticate(request, access.user, access.session)
    return request


def test_otb_source_reads_actual_draft_complete_scope_quantity_and_pinned_hash(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = _booking(world)
        ask = _ask(proof)
        result = source(ask)
        assert result.kind == "open_to_buy" and result.fields == frozenset({"cost"})
        assert set(result.cells) == {(site.pk, world.brands[0].pk)
                                     for site in (proof.home, proof.header_site, proof.line_site)}
        assert result.qty == 5 and result.value == 60000 and len(result.digest) == 64
        assert source(ask, lock=True) == result


def test_otb_source_refuses_changed_revision_and_preserves_original_request(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = _booking(world)
        ask = _ask(proof)
        original_hash = ask.draft_hash
        command(proof.access, "proof.booking.revise", lambda run: append_revision(
            run, booking_head(proof.booking), header={**proof.header, "notes": "Changed exact review inputs"},
        ))
        with pytest.raises(Refusal) as caught:
            source(ask)
        assert caught.value.code == "APPROVAL_STALE"
        ask.refresh_from_db()
        assert ask.draft_hash == original_hash
        assert booking_content_hash(proof.booking, booking_head(proof.booking)) != original_hash


@pytest.mark.parametrize("conflict", ["ask_brand", "ask_season", "booking_tenant",
                                      "header_brand", "header_site", "line_site"])
def test_otb_source_refuses_foreign_or_conflicting_stable_ownership(
    worlds: tuple[TenantWorld, TenantWorld], conflict: str,
) -> None:
    world, other = worlds
    if conflict == "booking_tenant":
        with tenant_context(other.tenant.pk):
            foreign = _booking(other)
            foreign_hash = booking_content_hash(foreign.booking, booking_head(foreign.booking))
    with tenant_context(world.tenant.pk):
        proof = _booking(world, conflict=conflict if conflict in {
            "header_brand", "header_site", "line_site"} else "", other=other)
        if conflict == "booking_tenant":
            local_access = proof.access
            proof = foreign
            ask = _ask(proof, access=local_access, brand_id=world.brands[0].pk, draft_hash=foreign_hash)
        else:
            wrong_season = Season.objects.create(code=f"other-{uuid.uuid4().hex[:8]}", name="Other season")
            ask = _ask(proof, brand_id=world.brands[1].pk if conflict == "ask_brand" else None,
                       season_id=wrong_season.pk if conflict == "ask_season" else None,
                       site_id=world.sites[0].pk)
        with pytest.raises(Refusal) as caught:
            source(ask)
        assert caught.value.code == "APPROVAL_SOURCE_INACTIVE"


@pytest.mark.parametrize("link", ["document_tenant", "home_site"])
def test_booking_tenant_wall_refuses_foreign_identity_links_without_persisting(
    worlds: tuple[TenantWorld, TenantWorld], link: str,
) -> None:
    world, other = worlds
    if link == "document_tenant":
        with tenant_context(other.tenant.pk):
            foreign = _booking(other)

            def foreign_identity(run: CommandRun) -> DocumentIdentity:
                identity, head = new_document(
                    run, kind="booking", purpose=DocumentIdentity.Purpose.BOOKING,
                    entity_id=foreign.home.gstin.legal_entity_id, site_id=foreign.home.pk,
                )
                append_revision(run, head, header=foreign.header, replace_lines=foreign.lines)
                return identity

            foreign_document = cast(DocumentIdentity, command(foreign.access, "proof.foreign.identity", foreign_identity))
    with tenant_context(world.tenant.pk):
        proof = _booking(world)
        before = (DocumentIdentity.objects.count(), GoodsBooking.objects.count(), OpenToBuyAsk.objects.count())
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                if link == "document_tenant":
                    GoodsBooking.objects.create(tenant=world.tenant, document=foreign_document,
                                                vendor=proof.booking.vendor, brand=world.brands[0])
                else:
                    _booking(world, conflict="home_site", other=other)
                # These tenant FKs are deferred. Force their real commit check
                # inside this rollback savepoint; never disable a constraint.
                connection.check_constraints()
        assert (DocumentIdentity.objects.count(), GoodsBooking.objects.count(), OpenToBuyAsk.objects.count()) == before


@pytest.mark.parametrize("scope", ["complete", "missing_header", "split_action_field", "split_scope"])
def test_booking_cost_projection_requires_action_fields_and_every_destination_together(
    worlds: tuple[TenantWorld, TenantWorld], scope: str,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = _booking(world)
        ask = _ask(proof)
        user, human = _person(world, f"booking-{scope}")
        sites = (proof.home, proof.header_site, proof.line_site)
        if scope == "complete":
            _assign(world, human, "owner", sites=sites, brands=(world.brands[0],))
        elif scope == "missing_header":
            _assign(world, human, "owner", sites=(proof.home, proof.line_site), brands=(world.brands[0],))
        else:
            field_role = Role.objects.create(tenant=world.tenant, code=f"cost-only-{scope}", name="Proof field only",
                                             section_access={}, field_access=["cost"], permissions_map={})
            world.roles[field_role.code] = field_role
            _assign(world, human, field_role.code, sites=sites, brands=(world.brands[0],))
            _assign(world, human, "warehouse", sites=sites, brands=(world.brands[0],))
            if scope == "split_scope":
                _assign(world, human, "owner", sites=sites, brands=(world.brands[1],))
        access = live_access(user)
        response = GoodsBookingDetailView.as_view()(_request(access), pk=proof.booking.document_id)
        assert response.status_code == 200
        allowed = scope == "complete"
        fields = ["cost"] if allowed else []
        assert response.data["field_access"]["readable_fields"] == fields
        assert response.data["field_access"]["writable_fields"] == fields
        assert all(("cost_paise" in line) == allowed for line in response.data["data"]["lines"]["items"])
        preview = GoodsBookingAccessPreviewView.as_view()(_request(access, {
            "site_id": proof.home.pk, "brand_id": world.brands[0].pk,
            "line_site_ids": f"{proof.header_site.pk},{proof.line_site.pk}",
        }))
        assert preview.status_code == 200
        assert preview.data == {"readable_fields": fields, "writable_fields": fields}
        assert preview["Cache-Control"] == "no-store, private"
        otb_booking = GoodsBookingOpenToBuyView.as_view()(_request(access), pk=proof.booking.document_id)
        otb_ask = GoodsOpenToBuyAskDetailView.as_view()(_request(access), pk=ask.pk)
        if allowed:
            assert otb_booking.status_code == 200 and otb_ask.status_code == 200
            assert otb_ask.data["over_paise"] == "60000"
            assert otb_booking["Cache-Control"] == otb_ask["Cache-Control"] == "no-store, private"
        else:
            for denied in (otb_booking, otb_ask):
                assert denied.status_code in {403, 404}
                assert "overs" not in denied.data and "over_paise" not in denied.data


@pytest.mark.parametrize("surface", ["preview", "booking", "otb_booking", "otb_ask"])
@pytest.mark.parametrize("boundary", ["logout", "expiry"])
def test_booking_and_otb_reads_refuse_delivery_after_session_ends(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
    surface: str, boundary: str,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = _booking(world)
        ask = _ask(proof)
        assert proof.access.session is not None
        session = proof.access.session
        module: Any = goods_views if surface in {"preview", "booking"} else open_to_buy_views
        name = {"preview": "_booking_field_access", "booking": "_booking_resource",
                "otb_booking": "booking_check", "otb_ask": "ask_json"}[surface]
        original = getattr(module, name)

        def delayed_data(*args: Any, **kwargs: Any) -> Any:
            data = original(*args, **kwargs)
            if boundary == "logout":
                revoke_session(session)
            else:
                ServerSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
            return data

        monkeypatch.setattr(module, name, delayed_data)
        request = _request(proof.access, {
            "site_id": proof.home.pk, "brand_id": world.brands[0].pk,
            "line_site_ids": f"{proof.header_site.pk},{proof.line_site.pk}",
        } if surface == "preview" else {})
        if surface == "preview":
            response = GoodsBookingAccessPreviewView.as_view()(request)
        elif surface == "booking":
            response = GoodsBookingDetailView.as_view()(request, pk=proof.booking.document_id)
        elif surface == "otb_booking":
            response = GoodsBookingOpenToBuyView.as_view()(request, pk=proof.booking.document_id)
        else:
            response = GoodsOpenToBuyAskDetailView.as_view()(request, pk=ask.pk)
        assert response.status_code == 401
        assert response.data["code"] == ("AUTH_REQUIRED" if boundary == "logout" else "SESSION_EXPIRED")
        assert "data" not in response.data and "overs" not in response.data and "readable_fields" not in response.data

"""Historical blob downloads, resolved through the owning business record.

``StoredFile`` predates tenant and scope columns. Its integer ID is not an
authority: only a known booking receipt or bank-statement relation gives it a
tenant, and the current role assignment must cover that record and its fields.
Unlinked/unknown blobs fail closed until their owning workflow migrates them.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request

from accounts.goods_api import GoodsAPIView
from accounts.principal import AccessContext
from accounts.role_assignments import effective_assignments
from accounts.sections import CAP_MANAGE, CAP_VIEW, meets
from accounts.unified_policy import role_capability
from core.refusals import Refusal
from files.models import StoredFile


def _readable(access: AccessContext, stored: StoredFile) -> bool:
    """Require the owning record, tenant and field policy on the same assignment."""
    from finledger.models import BankStatementImport
    from vendors.models import Booking

    roles = {row.pk: row.role for row in effective_assignments(access.human_id)}

    if stored.kind == StoredFile.Kind.BOOKING_RECEIPT:
        bookings = Booking.objects.filter(
            source_file_id=stored.pk, vendor__tenant_id=access.tenant_id
        ).values_list("destination_store_id", "brand_id")
        cells = list(bookings)
        return bool(cells) and all(
            any(
                (role := roles.get(grant.id)) is not None
                and meets(role_capability(role, "booking"), CAP_VIEW)
                and "cost" in grant.fields
                and access.grant_covers(grant, site_id, brand_id)
                for grant in access.grants
            )
            for site_id, brand_id in cells
        )

    if stored.kind == StoredFile.Kind.BANK_STATEMENT:
        owned = BankStatementImport.objects.filter(
            file_id=stored.pk, uploaded_by__tenant_id=access.tenant_id
        ).exists()
        return owned and any(
            (role := roles.get(grant.id)) is not None
            and meets(role_capability(role, "money"), CAP_MANAGE)
            and "financial" in grant.fields
            and grant.all_sites and grant.all_brands
            for grant in access.grants
        )

    return False


class LegacyFileDownloadView(GoodsAPIView):
    """Read an old blob only while its current assignment covers its owner."""

    http_method_names = ["get", "options"]

    @extend_schema(
        responses={(200, "*/*"): {"type": "string", "format": "binary", "description": "Stored file bytes."}}
    )
    def get(self, request: Request, pk: int) -> HttpResponse:
        access = self.access(request)
        stored: Any = StoredFile.objects.filter(pk=pk).first()
        if stored is None or not _readable(access, stored):
            raise Refusal("NOT_FOUND", "That file was not found.")
        # A download can be large. Refresh once more immediately before its
        # bytes leave, after both the owning record and its field policy resolve.
        if not access.refresh() or not _readable(access, stored):
            raise Refusal("NOT_FOUND", "That file was not found.")
        response = HttpResponse(bytes(stored.content), content_type=stored.content_type)
        safe_name = stored.filename.replace('"', "")
        response["Content-Disposition"] = f'inline; filename="{safe_name}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response

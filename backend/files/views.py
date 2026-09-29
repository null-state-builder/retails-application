from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from files.models import StoredFile


@extend_schema(
    responses={(200, "*/*"): {"type": "string", "format": "binary", "description": "Stored file bytes."}}
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download(request: Request, pk: int) -> HttpResponse:
    f = StoredFile.objects.get(pk=pk)
    resp = HttpResponse(bytes(f.content), content_type=f.content_type)
    resp["Content-Disposition"] = f'inline; filename="{f.filename}"'
    return resp

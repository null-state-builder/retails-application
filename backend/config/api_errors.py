"""The project's DRF exception handler (design §6.1 error envelope).

Goods-v1 code raises ``core.refusals.Refusal``; this renders it as
``{code, error, details?, command_id?, retryable}`` with its status. Everything
else keeps DRF's default rendering, so legacy endpoints answer exactly as before.
An unexpected exception inside a goods-v1 view becomes ``INTERNAL_ERROR`` with a
support reference instead of a traceback.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.urls import reverse
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from core.refusals import Refusal

logger = logging.getLogger("kdps.goods")


def goods_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, Refusal):
        view = context.get("view")
        command_id = getattr(view, "goods_command_id", None)
        body = exc.body(command_id)
        if exc.code == "OUTCOME_UNKNOWN" and command_id is not None:
            # E191: where to learn whether it committed before retrying the same identity.
            body.setdefault("details", {})["status_url"] = reverse(
                "goods-command-status", args=[command_id]
            )
        refused = Response(body, status=exc.status)
        if exc.status == 401:
            refused["WWW-Authenticate"] = 'Session realm="kdps"'
        return refused
    handled = drf_exception_handler(exc, context)
    if handled is not None:
        return handled
    view = context.get("view")
    if getattr(view, "goods_contract", False):
        reference = uuid.uuid4().hex[:12]
        logger.exception("goods-v1 internal error %s", reference, exc_info=exc)
        return Response(
            {
                "code": "INTERNAL_ERROR",
                "error": f"Something went wrong on our side. Support reference {reference}.",
                "retryable": False,
            },
            status=500,
        )
    return None

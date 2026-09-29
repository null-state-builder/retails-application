"""The D10 refusal body: a sentence for the person, a code for the caller.

Deliberately not DRF's ``{"detail": ...}``. The till replays writes from a
durable queue and has to tell "retry forever" from "this bill needs a human",
which it does on the code, never on the prose (`api-contract.md`, conventions).

Lives in `core` because it is a wire convention rather than any one app's rule,
and the two endpoints that share it today - the store-target grid in `masters`
and the dashboard in `storefront` - have no nearer place they both already
import. It was private to `masters.views` while that was the only caller, with a
note saying it would move here when the second one landed; this is that move.

**Nothing here imports a web framework**, and that is the whole reason it is a
body-builder rather than a response-builder. `core` is the kernel: it may not
learn about HTTP, and this module would otherwise be the first thing in it to
import DRF. So the *shape* lives here, once, and each view does its own
`Response(refusal_body(...), status=...)` - which also keeps the status beside
the branch that chose it, where each contract's error table puts it.
"""

from __future__ import annotations

import uuid
from typing import Any

#: Default HTTP status for the shared goods-v1 refusal codes (design §6.1). A
#: domain code not listed here states its own status where it is raised.
DEFAULT_STATUS: dict[str, int] = {
    "AUTH_REQUIRED": 401,
    "SESSION_EXPIRED": 401,
    "INVALID_CREDENTIALS": 401,
    "CSRF_FAILED": 403,
    "ACTION_DENIED": 403,
    "STEP_UP_REQUIRED": 403,
    "SELF_APPROVAL": 403,
    "PASSWORD_CHANGE_REQUIRED": 403,
    "EXPORT_SCOPE_INVALID": 403,
    "CONFIG_INVALID": 422,
    "INVALID_REQUEST": 400,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "REQUEST_TOO_LARGE": 413,
    "FILE_TOO_LARGE": 413,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "LOGIN_LOCKED": 429,
    "COMMAND_CONFLICT": 409,
    "CONTRACT_DISABLED": 409,
    "REVISION_SUPERSEDED": 409,
    "STATE_CONFLICT": 409,
    "SITE_NOT_READY": 409,
    "UNDER_COUNT": 409,
    "SERIES_NOT_READY": 409,
    "POSTING_EVENT_CONFLICT": 409,
    "RETRY_EXHAUSTED": 503,
    "OUTCOME_UNKNOWN": 503,
    "SERVICE_UNAVAILABLE": 503,
    "EVIDENCE_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}

#: Codes a client may retry with the same command identity.
RETRYABLE = frozenset({"RETRY_EXHAUSTED", "OUTCOME_UNKNOWN", "SERVICE_UNAVAILABLE"})


class Refusal(Exception):  # noqa: N818 - a refusal is an outcome, not a crash
    """A typed business refusal: code for the caller, sentence for the person.

    Raised anywhere below a goods-v1 view; the project exception handler renders
    it with ``body()``. The command kernel catches it inside its savepoint so a
    refused command leaves no business effect but keeps its audit evidence.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: int | None = None,
        issues: list[dict[str, Any]] | None = None,
        domain_code: str | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code.replace("_", " ").capitalize() + "."
        self.status = status if status is not None else DEFAULT_STATUS.get(code, 409)
        self.issues = issues or []
        self.domain_code = domain_code

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def body(self, command_id: uuid.UUID | str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "error": self.message}
        details: dict[str, Any] = {}
        if self.issues:
            details["issues"] = self.issues
        if self.domain_code:
            details["domain_code"] = self.domain_code
        if details:
            body["details"] = details
        if command_id is not None:
            body["command_id"] = str(command_id)
        body["retryable"] = self.retryable
        return body

    @classmethod
    def from_body(cls, body: dict[str, Any], status: int) -> Refusal:
        details = body.get("details") or {}
        return cls(
            str(body.get("code", "INTERNAL_ERROR")),
            str(body.get("error", "")),
            status=status,
            issues=list(details.get("issues") or []),
            domain_code=details.get("domain_code"),
        )


def issue(
    code: str,
    message: str,
    *,
    field: str | None = None,
    line_key: uuid.UUID | str | None = None,
    quantity: int | None = None,
) -> dict[str, Any]:
    """One closed ``Issue`` object (design §5.3)."""
    out: dict[str, Any] = {"code": code, "message": message[:500]}
    if field is not None:
        out["field"] = field[:100]
    if line_key is not None:
        out["line_key"] = str(line_key)
    if quantity is not None:
        out["quantity"] = quantity
    return out


def refusal_body(code: str, message: str) -> dict[str, str]:
    """One refusal, in the shape every D10 endpoint answers with.

    ``error`` is first so that a client walking the body in order meets the
    sentence before the code; no caller may *rely* on that order, and the PWA's
    `apiErrorMessage` reads `error` by name.
    """
    return {"error": message, "code": code}


def first_message(errors: Any) -> str:
    """The first sentence out of a DRF error tree, flattened.

    A refusal body carries one message, and a serializer's is keyed by field. The
    first one is the one the person needs: these forms are three fields wide, and
    a screen that fixes the named field re-submits and hears about the next.
    """
    if isinstance(errors, dict):
        for value in errors.values():
            found = first_message(value)
            if found:
                return found
        return ""
    if isinstance(errors, list):
        for item in errors:
            found = first_message(item)
            if found:
                return found
        return ""
    return str(errors).strip()

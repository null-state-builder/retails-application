"""Approval policy: pinned at submission, locked and rechecked at decision (change PRD §14.4-14.6).

Submission works out the quantity and monetary value the approval is about, finds the one
effective ``approval`` configuration for the requested action, the subject's site, every
brand it involves and its purpose, checks the amounts against that policy's limits and pins
the version and the amounts on the request.

A decision locks the pinned policy's effective period, then rechecks everything the pin
relied on inside the committing transaction: the checker holds one of the policy's roles
with the action over every cell of the subject, is a different human where the policy or
the fixed two-person floor needs it, has confirmed their password where the policy asks,
and the pinned version is still the one in force - not superseded, expired or withdrawn -
with the pinned amounts inside its limits. A changed policy never silently applies: the
decision is ``APPROVAL_STALE`` and the maker submits again.

Limits are inclusive. An unknown monetary value is never zero: it matches no finite value
limit and follows the policy's ``unknown_value`` branch (``refuse`` by default, or
``quantity_only`` to judge by quantity alone). A policy with no value limit does not judge
value, so an unknown value does not matter there.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from core.commands import CommandRun, LockRank
from core.refusals import Refusal, issue

BLOCKED = "APPROVAL_POLICY_BLOCKED"
STALE = "APPROVAL_STALE"


@dataclass(frozen=True)
class Amounts:
    qty: int
    #: ``None`` is an unknown value, never zero.
    value_paise: int | None


@dataclass(frozen=True)
class PolicyPin:
    version_id: uuid.UUID
    roles: list[str]
    require_distinct: bool
    basis: dict[str, Any]


def _target(basis: dict[str, Any], at: Any) -> Any:
    from masters.goods_config import ConfigTarget

    brands = [int(b) for b in basis.get("brand_ids") or []]
    site = basis.get("site_id")
    return ConfigTarget.of(
        at,
        site_id=int(site) if site is not None else None,
        brand_ids=brands or [None],
        purpose=basis.get("purpose"),
    )


def thresholds(payload: dict[str, Any]) -> dict[str, Any]:
    qty_max = payload.get("qty_max")
    value_max = payload.get("value_max")
    return {
        "qty": int(qty_max) if qty_max is not None else None,
        "value_paise": str(value_max) if value_max is not None else None,
    }


def band_failure(payload: dict[str, Any], amounts: Amounts) -> str | None:
    """Why ``amounts`` fall outside the policy's limits, or ``None``. Limits are inclusive."""
    qty_max = payload.get("qty_max")
    if qty_max is not None and amounts.qty > int(qty_max):
        return "QTY_ABOVE_LIMIT"
    value_max = payload.get("value_max")
    if value_max is None:
        return None
    if amounts.value_paise is None:
        return None if payload.get("unknown_value") == "quantity_only" else "VALUE_UNKNOWN"
    if amounts.value_paise > int(str(value_max)):
        return "VALUE_ABOVE_LIMIT"
    return None


BAND_MESSAGES = {
    "QTY_ABOVE_LIMIT": "The quantity is above this approval policy's limit.",
    "VALUE_ABOVE_LIMIT": "The value is above this approval policy's limit.",
    "VALUE_UNKNOWN": "The value is unknown, and this approval policy does not decide without it.",
}


def _record(run: CommandRun, version: Any, basis: dict[str, Any]) -> None:
    """The authority snapshot names the policy, its limits and the scope it was judged on."""
    run.authority["policy_version_ids"] = [str(version.pk)] if version is not None else []
    run.authority["thresholds"] = (
        thresholds(version.payload) if version is not None else {"qty": None, "value_paise": None}
    )
    run.authority["scope"] = {
        "site_ids": [str(basis["site_id"])] if basis.get("site_id") is not None else [],
        "brand_ids": [str(b) for b in basis.get("brand_ids") or []],
        "purpose": basis.get("purpose"),
        "policy_scope": version.scope if version is not None else None,
        "qty": basis.get("qty"),
        "value_paise": basis.get("value_paise"),
    }


def pin(
    run: CommandRun,
    *,
    action: str,
    purpose: str | None,
    site_id: int | None,
    brand_ids: list[int | None],
    amounts: Amounts,
) -> PolicyPin:
    """The effective policy for this submission, with the amounts inside its limits."""
    from masters.goods_config import resolve

    basis: dict[str, Any] = {
        "action": action,
        "site_id": site_id,
        "brand_ids": sorted({int(b) for b in brand_ids if b is not None}),
        "purpose": purpose,
        "qty": amounts.qty,
        "value_paise": str(amounts.value_paise) if amounts.value_paise is not None else None,
        "pinned_at": run.now.isoformat(),
    }
    _record(run, None, basis)
    version = resolve(
        run.tenant_id,
        "approval",
        _target(basis, run.now),
        match={"action": action},
        code=BLOCKED,
        path="approval_policy",
    )
    payload = version.payload if isinstance(version.payload, dict) else {}
    _record(run, version, basis)
    failure = band_failure(payload, amounts)
    if failure is not None:
        raise Refusal(
            BLOCKED,
            BAND_MESSAGES[failure],
            status=422,
            issues=[issue(failure, BAND_MESSAGES[failure], field="approval_policy")],
        )
    basis["thresholds"] = thresholds(payload)
    basis["unknown_value"] = payload.get("unknown_value") or "refuse"
    basis["step_up"] = bool(payload.get("step_up"))
    return PolicyPin(
        version_id=version.pk,
        roles=sorted({str(r) for r in payload.get("roles") or []}),
        require_distinct=bool(payload.get("require_distinct", True)),
        basis=basis,
    )


def _reason_of(refusal: Refusal) -> str | None:
    issues = refusal.issues or []
    return str(issues[0].get("code")) if issues else None


def _stale(reason: str, message: str) -> Refusal:
    return Refusal(STALE, message, status=409, issues=[issue(reason, message, field="policy")])


def _check_approver(
    run: CommandRun,
    request: Any,
    payload: dict[str, Any],
    basis: dict[str, Any],
    *,
    checker_id: uuid.UUID,
    access: Any,
) -> None:
    """The checker holds a policy role with the action over every cell, and is not the maker."""
    if access is None:
        raise Refusal("ACTION_DENIED", "An approval is decided by a signed-in person.")
    brands = basis.get("brand_ids") or [None]
    cells = {(basis.get("site_id"), brand) for brand in brands}
    roles = {str(r) for r in payload.get("roles") or []}
    grants = access.grants_with_roles(request.requested_action, cells, roles)
    if not grants:
        raise Refusal(
            "ACTION_DENIED",
            "The approval policy needs an approver role you do not hold here.",
            status=403,
        )
    run.authority["role_grant_ids"] = grants
    distinct = payload.get("require_distinct", True) or request.require_distinct
    if distinct and str(checker_id) == str(request.maker_id):
        raise Refusal("SELF_APPROVAL", "The person who prepared this cannot also approve it.")


def _check_in_force(run: CommandRun, request: Any, version: Any, basis: dict[str, Any]) -> None:
    """The pinned version is still the one policy in force, with the amounts inside it."""
    from masters.goods_config import candidate, resolve

    target = _target(basis, run.now)
    lapsed = candidate(version).failure(target)
    if lapsed == "CONFIG_WITHDRAWN":
        raise _stale(
            "POLICY_WITHDRAWN",
            "The approval policy this was submitted under was withdrawn; submit again.",
        )
    try:
        current = resolve(
            run.tenant_id,
            "approval",
            target,
            match={"action": request.requested_action},
            code=STALE,
            path="policy",
            status=409,
        )
    except Refusal as refusal:
        if _reason_of(refusal) == "CONFIG_AMBIGUOUS":
            raise _stale(
                "POLICY_AMBIGUOUS",
                "More than one approval policy now applies here; submit again once one applies.",
            ) from None
        current = None
    if current is not None and current.pk != version.pk:
        raise _stale(
            "POLICY_SUPERSEDED",
            "A different approval policy applies since this was submitted; submit again.",
        )
    if lapsed is not None or current is None:
        raise _stale(
            "POLICY_EXPIRED" if lapsed in (None, "CONFIG_EXPIRED") else "POLICY_OUT_OF_SCOPE",
            "The approval policy this was submitted under is no longer in force; submit again.",
        )
    payload = version.payload if isinstance(version.payload, dict) else {}
    value = basis.get("value_paise")
    amounts = Amounts(int(basis.get("qty") or 0), int(value) if value is not None else None)
    failure = band_failure(payload, amounts)
    if failure is not None:
        raise _stale(failure, BAND_MESSAGES[failure])


def enforce(
    run: CommandRun,
    request: Any,
    *,
    decision: str,
    checker_id: uuid.UUID,
    access: Any,
) -> None:
    """Lock and recheck the pinned policy inside the deciding transaction.

    Called by each registered subject handler after it has locked its subject documents
    (rank DOCUMENT) and before it touches custody (rank LOT), so the policy guard sits at
    its own rank in the fixed order. A rejection needs a policy approver too, but does not
    depend on the policy still being in force.
    """
    from masters.goods_config import CONFIGURATION
    from masters.goods_models import ConfigVersion, EffectiveVersionPeriod

    basis = dict(request.policy_basis or {})
    if (request.policy_version_id is None or not basis) and decision != "approve":
        # Made before policies were pinned: it can still be turned down, never approved.
        return
    if request.policy_version_id is None or not basis:
        raise _stale("POLICY_NOT_PINNED", "This request pinned no approval policy; submit again.")
    run.lock(
        LockRank.DRAFT,
        EffectiveVersionPeriod.objects.filter(
            target_kind=CONFIGURATION, target_id=request.policy_version_id
        ),
    )
    version = ConfigVersion.objects.get(pk=request.policy_version_id)
    payload = version.payload if isinstance(version.payload, dict) else {}
    _record(run, version, basis)
    _check_approver(run, request, payload, basis, checker_id=checker_id, access=access)
    if decision != "approve":
        return
    if payload.get("step_up"):
        access.require_step_up(run.now)
    _check_in_force(run, request, version, basis)

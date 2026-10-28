"""A test store's opening needs no second person while its policy says so (store test run).

Owner decision (29 Oct 2026, The Wardrobe): no approvals during alpha. That is
configuration, not removed code: the ``pt.approve.opening`` approval policy in force
at the store carries :data:`FLAG` (published from ``setup.json``
``"opening": {"approval": false}``). Then the opening preparer - the maker of every
request in the opening - confirms each of them with
``approvals.goods_services.decide_without_second_person``:

* the SOH/PT source (``soh_import`` / ``opening.manifest.approve``),
* each batch's opening manifest (``manifest`` / ``opening.manifest.approve``),
* each opening PT (``document`` / ``pt.approve.opening``, whose pinned policy must be
  this same version).

Never on a real store, never without the flag, never as a fallback: otherwise each
stays pending for its normal approver. Republishing the policy without the flag turns
approvals back on; the decision then refuses ``AUTO_APPROVAL_NOT_ALLOWED``.
"""

from __future__ import annotations

import uuid

from approvals.goods_models import ApprovalRequest
from approvals.goods_services import register_auto_rule
from core.commands import CommandRun
from core.refusals import Refusal

#: The approval-policy payload key that lets a test store open without approvals.
FLAG = "auto_approve_test_store_opening"


def policy_version(run: CommandRun, site_id: int | None) -> uuid.UUID | None:
    """The flagged ``pt.approve.opening`` policy in force at this test store now, or ``None``."""
    from masters.goods_config import resolve
    from masters.models import Store
    from masters.store_features import is_real_store
    from ptmapper.goods_manifest_services import APPROVE_OPENING, manifest_target

    if site_id is None:
        return None
    store = Store.objects.select_related("tenant").filter(tenant_id=run.tenant_id, pk=site_id).first()
    if store is None or is_real_store(store):
        return None
    try:
        # The same target an opening PT pins its policy against (``_pin_opening``):
        # the store, purpose ``opening``, no brand.
        version = resolve(run.tenant_id, "approval", manifest_target(run.now, site_id),
                          match={"action": APPROVE_OPENING}, code="APPROVAL_POLICY_BLOCKED", path="approval_policy")
    except Refusal:
        return None
    payload = version.payload if isinstance(version.payload, dict) else {}
    return version.pk if payload.get(FLAG) is True else None


def rule(run: CommandRun, request: ApprovalRequest) -> uuid.UUID | None:
    return policy_version(run, request.site_id)


def register() -> None:
    from ptmapper.goods_manifest_services import APPROVE_OPENING, MANIFEST_ACTION
    from ptmapper.soh_services import APPROVE as SOURCE_ACTION

    for subject_kind, action in (("soh_import", SOURCE_ACTION),
                                 (ApprovalRequest.SubjectKind.MANIFEST, MANIFEST_ACTION),
                                 ("document", APPROVE_OPENING)):
        register_auto_rule(str(subject_kind), action, rule, flag=FLAG)

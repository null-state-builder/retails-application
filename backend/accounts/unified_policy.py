"""The single role policy used by section and goods action decisions.

An action is attached to one section rung.  Exceptional workflow steps may be
added to a role's versioned policy through ``permissions_map.step_actions``;
the physical-person and sensitive-data floors below still apply.  The policy is
evaluated *inside* one assignment, before that assignment's site/brand scope is
checked, so separate assignments can never exchange privileges or scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from accounts.actions import ACTIONS, SECTION_ACTIONS
from accounts.sections import (
    CAP_APPROVE, CAP_MANAGE, CAP_OPERATE, CAP_VIEW, CAPABILITY_RANK,
    SECTION_CODES, meets,
)


ACTION_LEVELS: dict[str, tuple[str, str]] = {}


def _register(section: str, level: str, names: str) -> None:
    for name in names.split():
        if name in ACTION_LEVELS:
            raise ValueError(f"Duplicate action policy: {name}")
        ACTION_LEVELS[name] = (section, level)


_register("home", CAP_VIEW, "org.site.route exception.view approvals.view")
_register("home", CAP_APPROVE, "approval.decide")
_register("home", CAP_OPERATE, "exception.manage org.site.lifecycle.run")
_register("setup", CAP_MANAGE, "org.tenant.manage org.entity.manage org.site.manage org.site.lifecycle.approve org.location.manage org.location.store.manage org.location.warehouse_bin.manage access.manage access.review staff.manage staff.retire config.approve master.retire")
_register("setup", CAP_OPERATE, "config.draft product.master.manage product.master.propose crosswalk.manage crosswalk.propose identity.resolve vendor.manage")
_register("booking", CAP_OPERATE, "booking.manage arrival.no_booking.confirm")
_register("receive_goods", CAP_VIEW, "pt.view")
_register("receive_goods", CAP_OPERATE, "opening.import.stage")
_register("receive_goods", CAP_OPERATE, "receive.arrival label.print stock.accept pt.reversal.request")
_register("receive_goods", CAP_APPROVE, "receipt.counter_grn.approve receipt.disposition.decide pt.prepare pt.prepare.opening pt.approve.receipt pt.approve.opening pt.approve.transfer pt.reversal.approve opening.manifest.approve opening.variance.approve")
_register("stock", CAP_VIEW, "stock.view")
_register("transfer", CAP_OPERATE, "transfer.allocate transfer.move movement.draft")
_register("transfer", CAP_APPROVE, "movement.approve")
_register("return_to_brand", CAP_OPERATE, "rtv.execute")
_register("stock_count", CAP_OPERATE, "count.run")
_register("stock_count", CAP_APPROVE, "count.review")
_register("reports", CAP_VIEW, "audit.view")
_register("setup", CAP_MANAGE, "export.run recovery.run ops.health.view")
_register("hrms", CAP_VIEW, "payroll.view")
_register("booking", CAP_VIEW, "brand_terms.view")
_register("money", CAP_VIEW, "billing.identity.view")
_register('setup', 'manage', 'store.feature.manage')
_register('setup', 'manage', 'tax.settings.manage')
_register('setup', 'manage', 'document.series.manage')
_register('setup', 'manage', 'consent.wording.manage')
_register('stock_count', 'approve', 'count.schedule.manage')
_register('setup', 'manage', 'staff.salesperson.resolve')
_register('money', 'manage', 'debit_note.manage')
_register('money', 'manage', 'brand_claim.manage')
_register('money', 'manage', 'payable.manage')
_register('money', 'manage', 'sor.settlement.manage')
_register('booking', 'operate', 'booking.budget.manage')
_register('setup', 'manage', 'checklist.template.manage')
_register('reports', 'view', 'brand_report.layout.manage')
_register('setup', 'manage', 'till.pin.admin')
_register('setup', 'operate', 'brand_terms.propose')
_register('setup', 'manage', 'brand_terms.approve')
for (section, level), action in SECTION_ACTIONS.items():
    _register(section, level, action)

if set(ACTION_LEVELS) != set(ACTIONS):
    raise ValueError(f"Incomplete action policy: {sorted(set(ACTIONS) ^ set(ACTION_LEVELS))}")

WORKFLOW_TARGET_KEY = "access-workflow-policy"


def pending_workflow_actions(tenant_id: Any) -> dict[str, dict[str, str]]:
    """Offer new, disabled actions for an explicit reviewed policy upgrade."""
    from masters.goods_models import MasterVersion

    row = MasterVersion.objects.filter(
        tenant_id=tenant_id, kind="tenant", target_key=WORKFLOW_TARGET_KEY,
    ).order_by("-revision").values_list("payload", flat=True).first()
    if row is None or not isinstance(row, dict) or not isinstance(row.get("action_levels"), dict):
        return {}
    return serialise_workflow_levels({key: value for key, value in ACTION_LEVELS.items() if key not in row["action_levels"]})


def workflow_levels(tenant_id: Any) -> dict[str, tuple[str, str]]:
    """Latest tenant workflow thresholds, with the shipped grid as first version.

    A malformed stored version fails closed instead of silently re-enabling the
    shipped rules. Changes are written only through the stepped-up editor.
    """
    if tenant_id is None:
        return dict(ACTION_LEVELS)
    from masters.goods_models import MasterVersion

    row = (
        MasterVersion.objects.filter(
            tenant_id=tenant_id, kind="tenant", target_key=WORKFLOW_TARGET_KEY
        ).order_by("-revision").values_list("payload", flat=True).first()
    )
    if row is None:
        return dict(ACTION_LEVELS)
    raw = row.get("action_levels") if isinstance(row, dict) else None
    if not isinstance(raw, dict) or set(raw) - set(ACTIONS):
        return {}
    # A release may introduce actions, but it must not grant them to an existing
    # policy or invalidate every previously approved action. Missing actions are
    # explicitly disabled until an administrator saves a new policy version.
    levels: dict[str, tuple[str, str]] = {
        action: (section, "none") for action, (section, _level) in ACTION_LEVELS.items()
    }
    for action, value in raw.items():
        if not isinstance(value, dict):
            return {}
        section, minimum = value.get("section"), value.get("minimum")
        if section not in SECTION_CODES or minimum not in CAPABILITY_RANK:
            return {}
        levels[action] = (section, minimum)
    return levels


def workflow_floor_violation(action: str, section: str, minimum: str) -> bool:
    expected_section, floor = ACTION_LEVELS[action]
    if section != expected_section:
        return True
    if minimum == "none":
        return action in {"access.manage", "access.review"}
    return not meets(minimum, floor)


def serialise_workflow_levels(levels: Mapping[str, tuple[str, str]]) -> dict[str, dict[str, str]]:
    return {
        action: {"section": section, "minimum": minimum}
        for action, (section, minimum) in sorted(levels.items())
    }

# These steps name *who is physically at the site* or whose responsibility the
# approved six-role baseline assigns.  A broader section rung cannot turn a
# central administrator into the receiver at a store or a PT preparer.
INITIAL_STEP_ROLES: dict[str, frozenset[str]] = {
    "opening.import.stage": frozenset({"owner", "store_person", "warehouse"}),
    "receive.arrival": frozenset({"store_person", "warehouse"}),
    "stock.accept": frozenset({"store_person", "warehouse"}),
    "pt.prepare": frozenset({"warehouse"}),
    "pt.prepare.opening": frozenset({"warehouse"}),
    "label.print": frozenset({"store_person", "warehouse"}),
    "transfer.move": frozenset({"store_person", "warehouse"}),
    "count.run": frozenset({"store_person", "warehouse"}),
    "rtv.execute": frozenset({"warehouse"}),
    "booking.manage": frozenset({"owner", "brand_manager", "warehouse"}),
    "product.master.manage": frozenset({"owner", "brand_manager", "it_admin"}),
    "vendor.manage": frozenset({"owner", "brand_manager", "it_admin"}),
    "staff.manage": frozenset({"owner", "it_admin"}),
    "staff.retire": frozenset({"owner"}),
    "access.manage": frozenset({"owner", "it_admin"}),
    "access.review": frozenset({"owner", "it_admin"}),
    "export.run": frozenset({"owner", "it_admin"}),
    "recovery.run": frozenset({"owner", "it_admin"}),
    "ops.health.view": frozenset({"owner", "it_admin"}),
}

INITIAL_STEP_ROLES.update({'store.feature.manage': frozenset({'it_admin'}), 'tax.settings.manage': frozenset({'it_admin'}), 'document.series.manage': frozenset({'it_admin'}), 'consent.wording.manage': frozenset({'it_admin'}), 'count.schedule.manage': frozenset({'owner'}), 'staff.salesperson.resolve': frozenset({'it_admin'}), 'debit_note.manage': frozenset({'accounts'}), 'brand_claim.manage': frozenset({'accounts'}), 'payable.manage': frozenset({'accounts'}), 'sor.settlement.manage': frozenset({'accounts'}), 'booking.budget.manage': frozenset({'owner'}), 'checklist.template.manage': frozenset({'it_admin'}), 'brand_report.layout.manage': frozenset({'accounts'}), 'till.pin.admin': frozenset({'it_admin'}), 'brand_terms.propose': frozenset({'brand_manager'}), 'brand_terms.approve': frozenset({'owner'})})

STEP_FIELDS = {'debit_note.manage': frozenset({'financial'}), 'brand_claim.manage': frozenset({'financial'}), 'payable.manage': frozenset({'financial'}), 'sor.settlement.manage': frozenset({'financial'}), 'booking.budget.manage': frozenset({'cost'}), 'brand_report.layout.manage': frozenset({'financial'}), 'brand_terms.propose': frozenset({'cost', 'margin'}), 'brand_terms.approve': frozenset({'cost', 'margin'})}

INITIAL_STEP_ROLES["approval.decide"] = frozenset({"owner", "accounts", "brand_manager"})

OWNER_APPROVAL_STEPS = frozenset(
    {
        "pt.approve.receipt",
        "pt.approve.opening",
        "pt.approve.transfer",
        "pt.reversal.approve",
        "receipt.counter_grn.approve",
        "receipt.disposition.decide",
        "opening.manifest.approve",
        "opening.variance.approve",
        "count.review",
        "movement.approve",
    }
)

CONFIGURED_APPROVAL_STEPS = frozenset({
    "pt.approve.receipt", "pt.approve.opening", "pt.approve.transfer",
    "pt.reversal.approve",
})

# Eligibility is persisted in each role policy. These defaults are never read
# as runtime grants, and an upgrade must be explicitly reviewed and versioned.
EXPLICIT_STEP_ACTIONS = (frozenset(INITIAL_STEP_ROLES) | OWNER_APPROVAL_STEPS) - {"access.manage", "access.review"}


def initial_step_actions(code: str) -> list[str]:
    steps = {action for action, roles in INITIAL_STEP_ROLES.items() if code in roles}
    if code == "owner":
        steps |= OWNER_APPROVAL_STEPS
    return sorted(steps)


# The approved field baseline is a ceiling for these initial role codes.  A
# configurable role may narrow its fields, but a role edit cannot give Admin
# business Money data or give a shop-floor role general cost or private payroll.
FIELD_CEILINGS: dict[str, frozenset[str]] = {
    "owner": frozenset({"financial", "employee_private", "customer", "cost", "margin", "layer_value", "personal"}),
    "accounts": frozenset({"financial", "employee_private", "billing_identity", "cost", "margin", "layer_value", "personal"}),
    "brand_manager": frozenset({"cost", "margin"}),
    "warehouse": frozenset({"cost_own_pt"}),
    "store_person": frozenset({"customer"}),
    "it_admin": frozenset(),
}

# A tenant may define a new role in the policy editor without a release. The
# six initial roles retain their explicit sensitive-data safeguards above;
# a new role begins empty and receives only fields named in an approved policy
# version. The own-PT exception is meaningful only for Warehouse.
PROTECTED_FIELDS = frozenset().union(*FIELD_CEILINGS.values())


def allowed_fields_for_role(code: str) -> frozenset[str]:
    return FIELD_CEILINGS.get(code, PROTECTED_FIELDS - {"cost_own_pt"})


def role_capability(role: Any, section: str) -> str:
    entry = (role.section_access or {}).get(section)
    if not isinstance(entry, dict):
        return "none"
    value = entry.get("capability", "none")
    return value if isinstance(value, str) else "none"


def role_actions(
    role: Any, *, action_levels: Mapping[str, tuple[str, str]] | None = None
) -> frozenset[str]:
    if not getattr(role, "is_active", False):
        return frozenset()
    code = str(role.code)
    levels = action_levels if action_levels is not None else workflow_levels(getattr(role, "tenant_id", None))
    overrides = (role.permissions_map or {}).get("step_actions", [])
    added = set(overrides) & ACTIONS.keys() if isinstance(overrides, list) else set()
    actions: set[str] = set()
    for action, (section, level) in levels.items():
        if level == "none" or workflow_floor_violation(action, section, level):
            continue
        required_fields = {
            "payroll.view": {"employee_private"},
            "brand_terms.view": {"cost", "margin"},
            "billing.identity.view": {"billing_identity", "customer"},
        }.get(action)
        if required_fields is not None and not required_fields.intersection(role_fields(role)):
            continue
        if action in {"access.manage", "access.review"} and code not in {"owner", "it_admin"}:
            continue
        if not STEP_FIELDS.get(action, frozenset()) <= role_fields(role):
            continue
        by_section = action not in EXPLICIT_STEP_ACTIONS and meets(role_capability(role, section), level)
        if by_section or action in added:
            actions.add(action)
    return frozenset(actions)


def role_fields(role: Any) -> frozenset[str]:
    raw = getattr(role, "field_access", [])
    configured = frozenset(raw) if isinstance(raw, list) else frozenset()
    configured &= allowed_fields_for_role(str(role.code))
    # Goods services use `personal` for staff-private data.  The field is
    # derived from the same role policy, never from an old RoleGrant.field_set.
    if "employee_private" in configured:
        configured |= {"personal"}
    return frozenset(configured)

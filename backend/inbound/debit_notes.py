"""Debit note draft for shortages (store operations PRD ST-REC-3; ticket 38).

The flow, and who does each step (PRD §4):

1. **Drafted** by the system when a shortage is approved - an ``accept_shortage``
   disposition on a GRN (the vendor invoiced pieces that never came). The draft
   is to the vendor, at the invoice's cost per piece before tax, linked to the
   GRN and the vendor's claim (the invoice claim version), one line per accepted
   shortage. A later shortage on the same GRN and claim joins a draft that is
   still with Accounts.
2. **Reviewed** by Accounts: the GST rate of each line, typed from the vendor's
   invoice (the claim does not record one), and a cost per piece where the
   invoice line had none. The invoice's own cost is kept as it is.
3. **Approved** by the Owner, a different person, in the approvals inbox
   (``approvals.Approval``, kind ``debit_note``). A rejection sends it back.
4. **Issued** by Accounts: head office's ``XXX/DN/2627/n`` number for the GSTIN
   (``masters.document_series``). An issued note is never edited.

Posting through the accounting export (R-FIN-007) waits for OQ-47: nothing here
posts. Each step is switched per site with ticket 01's switch, and each leaves an
audit record with the note before and after.

The rules (``Line``, money, stage, split) are pure; the steps take a running
command, so the note and its audit record are written together or not at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from accounts.permissions import user_can
from accounts.principal import access_for_user
from accounts.role_lists import DEBIT_NOTE_APPROVER_ROLES
from accounts.sections import CAP_MANAGE
from approvals.models import Approval, ApprovalStatus
from approvals.services import AlreadyPendingError, ApprovalError, request_approval
from core.canonical import content_hash
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.goods_money import MoneyInvalid, paise_from_json
from core.kernel_models import AuditEvent
from core.refusals import Refusal, issue
from core.tenancy import current_tenant_id
from inbound.debit_note_models import DebitNote, DebitNoteSource
from inbound.goods_models import Disposition, GoodsGrn, InvoiceClaimVersion
from masters.document_series import DocumentSeries, issue_number
from masters.models import Store
from masters.scoping import actionable_store_ids
from masters.store_feature_registry import DEBIT_NOTE_DRAFT
from masters.store_features import is_feature_on, require_feature

FEATURE_KEY = DEBIT_NOTE_DRAFT

#: The approvals-spine family (``Approval.kind``) and how the inbox names it.
APPROVAL_KIND = "debit_note"
APPROVAL_LABEL = "Debit note"

#: Audit actions, one per step; the subject is ``debit_note:<id>``.
DRAFT_ACTION = "inbound.debit_note.draft"
REVIEW_ACTION = "inbound.debit_note.review"
REQUEST_ACTION = "inbound.debit_note.request_approval"
APPROVE_ACTION = "inbound.debit_note.approve"
REJECT_ACTION = "inbound.debit_note.reject"
ISSUE_ACTION = "inbound.debit_note.issue"
CANCEL_ACTION = "inbound.debit_note.cancel"

#: Where a note stands. ``status`` is stored; the stage adds the Owner's approval.
DRAFT = "draft"
WAITING = "waiting"
APPROVED = "approved"
REJECTED = "rejected"
ISSUED = "issued"
CANCELLED = "cancelled"
#: Stages Accounts may still change, and a new shortage may still join.
OPEN_TO_CHANGE = frozenset({DRAFT, REJECTED})

#: Where a line's cost per piece came from.
COST_FROM_INVOICE = "invoice"
COST_FROM_ACCOUNTS = "accounts"

#: GST rates a line may carry (B99). A setting, so a new rate is not a code change.
DEFAULT_GST_RATES = ("0", "0.25", "3", "5", "12", "18", "28", "40")
MAX_UNIT_COST_PAISE = 100_000_000  # Rs 10 lakh a piece
NOTE_LENGTH = 240


def gst_rates() -> tuple[Decimal, ...]:
    raw = getattr(settings, "KDPS_DEBIT_NOTE_GST_RATES", DEFAULT_GST_RATES)
    return tuple(Decimal(str(rate)) for rate in raw)


def rate_text(rate: Decimal) -> str:
    """``Decimal("5.00")`` -> ``"5"``; ``Decimal("0.25")`` -> ``"0.25"``."""
    text = format(rate.normalize(), "f")
    return text


# ---------------------------------------------------------------------------
# The rules, without a database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Money:
    taxable_paise: int | None
    tax_paise: int | None


def line_money(qty: int, unit_cost_paise: int | None, rate: Decimal | None) -> Money:
    """A line's taxable value (pieces x cost before tax) and its tax, to the paisa.

    Tax is rounded half up per line, as a debit note prints it. A missing cost
    leaves both unknown; a missing rate leaves the tax unknown - never zero.
    """
    if unit_cost_paise is None:
        return Money(None, None)
    taxable = qty * unit_cost_paise
    if rate is None:
        return Money(taxable, None)
    tax = (Decimal(taxable) * rate / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return Money(taxable, int(tax))


def _int_or_none(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _rate_or_none(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def priced(lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """``lines`` with each line's money worked out, and the note's taxable value and tax.

    The totals count only what is known; ``missing`` says what is not.
    """
    out: list[dict[str, Any]] = []
    taxable_total = 0
    tax_total = 0
    for line in lines:
        money = line_money(
            int(line["qty"]),
            _int_or_none(line.get("unit_cost_paise")),
            _rate_or_none(line.get("gst_rate")),
        )
        out.append(
            {
                **line,
                "taxable_paise": None if money.taxable_paise is None else str(money.taxable_paise),
                "tax_paise": None if money.tax_paise is None else str(money.tax_paise),
            }
        )
        taxable_total += money.taxable_paise or 0
        tax_total += money.tax_paise or 0
    return out, taxable_total, tax_total


def missing(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What stops the note going to the Owner: a line with no cost or no GST rate."""
    problems: list[dict[str, Any]] = []
    if not lines:
        problems.append(issue("NO_LINES", "The note has no lines."))
    for line in lines:
        if line.get("unit_cost_paise") in (None, ""):
            problems.append(
                issue(
                    "COST_MISSING",
                    f"{line['description']}: the invoice gives no cost per piece. Type it "
                    "from the vendor's invoice.",
                    line_key=line["key"],
                )
            )
        if line.get("gst_rate") in (None, ""):
            problems.append(
                issue(
                    "GST_RATE_MISSING",
                    f"{line['description']}: type the GST rate from the vendor's invoice.",
                    line_key=line["key"],
                )
            )
    return problems


def split(tax_paise: int, vendor_state: str, our_state: str) -> dict[str, Any]:
    """How the tax divides: IGST between states, CGST and SGST within one.

    Within a state the note's whole tax is split once, CGST the half rounded down
    and SGST the rest (ticket 47's B82, so every document splits the same way).
    With the vendor's state not recorded, the split is unknown - never guessed.
    """
    if not vendor_state or not our_state:
        return {"kind": "unknown", "cgst_paise": None, "sgst_paise": None, "igst_paise": None}
    if vendor_state != our_state:
        return {"kind": "inter", "cgst_paise": None, "sgst_paise": None, "igst_paise": tax_paise}
    cgst = tax_paise // 2
    return {"kind": "intra", "cgst_paise": cgst, "sgst_paise": tax_paise - cgst, "igst_paise": None}


def figures(note: DebitNote) -> dict[str, Any]:
    """Exactly what the Owner approves: the lines and the money, nothing else."""
    return {
        "vendor_id": note.vendor_id,
        "grn_id": str(note.grn_id),
        "claim_id": str(note.claim_id),
        "lines": [
            {
                "key": line["key"],
                "qty": int(line["qty"]),
                "unit_cost_paise": line.get("unit_cost_paise"),
                "gst_rate": line.get("gst_rate"),
            }
            for line in note.lines
        ],
        "taxable_paise": note.taxable_paise,
        "tax_paise": note.tax_paise,
        "total_paise": note.total_paise,
    }


def figures_hash(note: DebitNote) -> str:
    return content_hash(figures(note))


def stage_of(note: DebitNote, approval: Approval | None) -> str:
    """Where the note stands, from its status and its newest approval."""
    if note.status == DebitNote.Status.ISSUED:
        return ISSUED
    if note.status == DebitNote.Status.CANCELLED:
        return CANCELLED
    if approval is None:
        return DRAFT
    if approval.status == ApprovalStatus.PENDING:
        return WAITING
    if approval.status == ApprovalStatus.REJECTED:
        return REJECTED
    if approval.status == ApprovalStatus.APPROVED and note.approval_hash == figures_hash(note):
        return APPROVED
    return DRAFT


def snapshot(note: DebitNote, stage: str) -> dict[str, Any]:
    """The note as its audit records hold it.

    Every figure worked out from the invoice's cost sits under ``cost_figures``,
    so the Audit Log page's cost stripping (ticket 02) hides it from anyone who
    may not see cost; the stage and number stay readable.
    """
    return {
        "stage": stage,
        "number": note.number,
        "pieces": sum(int(line["qty"]) for line in note.lines),
        "cost_figures": {
            "lines": [
                {
                    "key": line["key"],
                    "qty": int(line["qty"]),
                    "unit_cost_paise": line.get("unit_cost_paise"),
                    "gst_rate": line.get("gst_rate"),
                    "tax_paise": line.get("tax_paise"),
                }
                for line in note.lines
            ],
            "taxable_paise": str(note.taxable_paise),
            "tax_paise": str(note.tax_paise),
            "total_paise": str(note.total_paise),
        },
    }


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


def may_read(user: Any) -> bool:
    """Owner and Accounts: ``money: manage`` (B80 of ticket 47; B96)."""
    return user_can(user, "money", CAP_MANAGE)


def may_review(user: Any, site_id: int, brand_id: int) -> bool:
    """Accounts (``money: manage`` narrowed to the declared reviewers)."""
    return access_for_user(user).covers_all({'debit_note.manage'}, [(site_id, brand_id)], ['financial'])


def readable_site_ids(user: Any) -> set[int] | None:
    """The sites whose notes this person reads; None means every site."""
    if not may_read(user):
        return set()
    ids = actionable_store_ids(user, section="money", minimum=CAP_MANAGE)
    return None if ids is None else set(ids)


def readable_notes(user: Any, tenant_id: Any) -> Any:
    """Notes of this tenant at the sites this person reads. The table's row-level
    security says the same; the filter keeps the read honest without it."""
    ids = readable_site_ids(user)
    rows = DebitNote.objects.filter(tenant_id=tenant_id).select_related(
        "site", "gstin", "vendor", "brand", "grn__document", "claim", "issued_by", "cancelled_by"
    )
    return rows if ids is None else rows.filter(site_id__in=ids)


# ---------------------------------------------------------------------------
# The approvals spine
# ---------------------------------------------------------------------------


def approvals_of(note_ids: Iterable[int]) -> dict[int, Approval]:
    """The newest approval of each note, in one query."""
    ids = list(note_ids)
    if not ids:
        return {}
    newest: dict[int, Approval] = {}
    for row in (
        Approval.objects.filter(
            kind=APPROVAL_KIND,
            content_type=ContentType.objects.get_for_model(DebitNote),
            object_id__in=ids,
        )
        .select_related("requested_by", "decided_by")
        .order_by("object_id", "-created_at", "-id")
    ):
        newest.setdefault(int(row.object_id), row)
    return newest


def approval_of(note: DebitNote) -> Approval | None:
    return approvals_of([note.pk]).get(note.pk)


def _guard(run: CommandRun, grn_id: Any) -> None:
    """Every change to a GRN's debit notes is taken in turn under one advisory key.

    Drafting runs last in the disposition command, after its lot and journal
    locks, so the key is at the highest rank (as the three-way match's is); the
    note's row lock below is always taken under it, never before.
    """
    run.advisory_lock(LockRank.CHAIN, [f"debit-note:grn:{grn_id}"])


def _lock(run: CommandRun, note_id: int) -> DebitNote:
    grn_id = DebitNote.objects.filter(pk=note_id).values_list("grn_id", flat=True).first()
    if grn_id is None:
        raise Refusal("NOT_FOUND", "That debit note was not found.")
    _guard(run, grn_id)
    note: DebitNote = DebitNote.objects.select_for_update().get(pk=note_id)
    return note


def _check_revision(note: DebitNote, expected_revision: int | None) -> None:
    if expected_revision is not None and expected_revision != note.revision:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Someone changed this note after you loaded it. Reload and review it again.",
        )


def _audit(
    run: CommandRun,
    note: DebitNote,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    reason: str,
) -> None:
    """An audit record beside the command's own (drafting runs inside another's)."""
    run.record(
        AuditEvent(
            action=action,
            subject_key=f"debit_note:{note.pk}",
            site_id=note.site_id,
            outcome="recorded",
            reason_code=reason,
            before=before,
            after=after,
            authority=run.authority,
        )
    )


def _set_command_audit(
    run: CommandRun, note: DebitNote, before: dict[str, Any], after: dict[str, Any]
) -> None:
    run.audit_subject_key = f"debit_note:{note.pk}"
    run.audit_site_id = note.site_id
    run.audit_before = before
    run.audit_after = after


def _reprice(note: DebitNote) -> None:
    lines, taxable, tax = priced(note.lines)
    note.lines = lines
    note.taxable_paise = taxable
    note.tax_paise = tax
    note.total_paise = taxable + tax


# ---------------------------------------------------------------------------
# 1. Drafted when a shortage is approved
# ---------------------------------------------------------------------------


def _claim_line(claim: InvoiceClaimVersion, key: str) -> dict[str, Any] | None:
    return next((line for line in claim.lines if str(line.get("line_key")) == key), None)


def draft_for_shortage(run: CommandRun, grn: GoodsGrn, rows: list[Disposition]) -> None:
    """Draft (or add to) the vendor debit note for shortages accepted in this command.

    Called at the end of the disposition command, where the switch is on at the
    GRN's site. One line per accepted shortage, at the invoice line's cost per
    piece before tax; a shortage already on a note is never charged again (the
    unique ``DebitNoteSource``). A draft that is still with Accounts for the same
    GRN and claim takes the new line; otherwise a new draft is opened.
    """
    shortages = [row for row in rows if row.kind == Disposition.Kind.ACCEPT_SHORTAGE]
    if not shortages:
        return
    arrival = grn.arrival
    if not is_feature_on(arrival.site_id, FEATURE_KEY):
        return
    from inbound.goods_services import latest_claim  # goods_services calls this module

    claim = latest_claim(arrival.pk)
    if claim is None:
        return  # a shortage is only ever decided against a claim line
    site = Store.objects.select_related("gstin").get(pk=arrival.site_id)
    # Last in the command, like the three-way match: the GRN's advisory lock at the
    # highest rank, so two shortages on one GRN draft one after the other.
    _guard(run, grn.pk)
    already = set(
        DebitNoteSource.objects.filter(
            disposition_id__in=[row.pk for row in shortages]
        ).values_list("disposition_id", flat=True)
    )
    new_lines: list[dict[str, Any]] = []
    for row in shortages:
        if row.pk in already:
            continue
        claimed = _claim_line(claim, str(row.source_line_key)) or {}
        cost = claimed.get("invoice_basic_paise")
        new_lines.append(
            {
                "key": str(row.pk),
                "claim_line_key": str(row.source_line_key),
                "description": str(claimed.get("description") or "Invoiced item"),
                "style_code": claimed.get("style_code"),
                "sku_id": claimed.get("sku_id"),
                "qty": int(row.qty),
                "unit_cost_paise": None if cost in (None, "") else str(cost),
                "cost_from": None if cost in (None, "") else COST_FROM_INVOICE,
                "gst_rate": None,
            }
        )
    if not new_lines:
        return
    candidates = list(
        DebitNote.objects.select_for_update()
        .filter(grn=grn, claim=claim, status=DebitNote.Status.DRAFT)
        .order_by("-created_at", "-id")
    )
    newest = approvals_of(note.pk for note in candidates)
    target = next(
        (note for note in candidates if stage_of(note, newest.get(note.pk)) in OPEN_TO_CHANGE),
        None,
    )
    if target is None:
        note = DebitNote(
            site=site,
            gstin=site.gstin,
            vendor_id=arrival.vendor_id,
            brand_id=arrival.brand_id,
            grn=grn,
            claim=claim,
            invoice_number=arrival.invoice_number or "",
            invoice_date=arrival.invoice_date,
            lines=new_lines,
        )
        _reprice(note)
        note.save()
        before: dict[str, Any] | None = None
    else:
        note = target
        before = snapshot(note, stage_of(note, newest.get(note.pk)))
        note.lines = [*note.lines, *new_lines]
        note.revision += 1
        _reprice(note)
        note.save()
    DebitNoteSource.objects.bulk_create(
        [DebitNoteSource(disposition_id=uuid.UUID(line["key"]), note=note) for line in new_lines]
    )
    stage = stage_of(note, newest.get(note.pk)) if target is not None else DRAFT
    _audit(run, note, DRAFT_ACTION, before, snapshot(note, stage), "SHORTAGE_ACCEPTED")


# ---------------------------------------------------------------------------
# 2. Reviewed by Accounts
# ---------------------------------------------------------------------------


def parse_rate(value: Any, field: str, problems: list[dict[str, Any]]) -> str | None:
    if value in (None, ""):
        return None
    try:
        rate = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        rate = Decimal("NaN")
    if not rate.is_finite():
        problems.append(issue("INVALID", "The GST rate must be a number.", field=field))
        return None
    if rate not in gst_rates():
        allowed = ", ".join(f"{rate_text(r)}%" for r in gst_rates())
        problems.append(issue("INVALID", f"The GST rate must be one of {allowed}.", field=field))
        return None
    return rate_text(rate)


def parse_cost(value: Any, field: str, problems: list[dict[str, Any]]) -> str | None:
    """A cost per piece: whole paise written as a base-10 string (goods-v1 money)."""
    if value is None:
        return None
    try:
        paise = paise_from_json(value, maximum=MAX_UNIT_COST_PAISE)
    except MoneyInvalid:
        paise = 0
    if not paise:
        problems.append(
            issue(
                "INVALID",
                "The cost per piece is whole paise written as text, more than 0 and at most "
                "Rs 10 lakh.",
                field=field,
            )
        )
        return None
    return str(paise)


def _require_open(stage: str) -> None:
    if stage == WAITING:
        raise Refusal(
            "STATE_CONFLICT",
            "This note is waiting for the Owner. It can be changed once the Owner has "
            "approved or sent it back.",
        )
    if stage not in OPEN_TO_CHANGE:
        raise Refusal("STATE_CONFLICT", f"This note is {stage}, so it can no longer be changed.")


def _changed_line(
    line: dict[str, Any], change: dict[str, Any], field: str, problems: list[dict[str, Any]]
) -> dict[str, Any]:
    """One line with Accounts' change: its GST rate, and a cost only where the
    invoice gave none - the invoice's own cost is kept as it is."""
    line = dict(line)
    if "gst_rate" in change:
        line["gst_rate"] = parse_rate(change["gst_rate"], f"{field}.gst_rate", problems)
    if "unit_cost_paise" not in change:
        return line
    cost = parse_cost(change["unit_cost_paise"], f"{field}.unit_cost_paise", problems)
    if line.get("cost_from") != COST_FROM_INVOICE:
        line["unit_cost_paise"] = cost
        line["cost_from"] = COST_FROM_ACCOUNTS if cost is not None else None
    elif cost != line.get("unit_cost_paise"):
        problems.append(
            issue(
                "COST_FROM_INVOICE",
                "This cost is the vendor's invoice; it is kept as it is.",
                field=f"{field}.unit_cost_paise",
            )
        )
    return line


def review(
    run: CommandRun,
    note_id: int,
    *,
    changes: list[dict[str, Any]],
    review_note: str | None,
    expected_revision: int | None,
) -> DebitNote:
    """Accounts sets each line's GST rate, and a cost where the invoice gave none."""
    note = _lock(run, note_id)
    _check_revision(note, expected_revision)
    require_feature(note.site_id, FEATURE_KEY)
    stage = stage_of(note, approval_of(note))
    _require_open(stage)
    before = {**snapshot(note, stage), "note": note.review_note}
    by_key = {line["key"]: dict(line) for line in note.lines}
    problems: list[dict[str, Any]] = []
    for index, change in enumerate(changes):
        field = f"lines[{index}]"
        line = by_key.get(str(change.get("key")))
        if line is None:
            problems.append(issue("UNKNOWN_LINE", "That line is not on this note.", field=field))
            continue
        by_key[line["key"]] = _changed_line(line, change, field, problems)
    if review_note is not None and len(review_note) > NOTE_LENGTH:
        problems.append(
            issue("TOO_LONG", f"The note is at most {NOTE_LENGTH} characters.", field="note")
        )
    if problems:
        raise Refusal(
            "DEBIT_NOTE_INVALID", "The debit note cannot be saved.", status=422, issues=problems
        )
    note.lines = [by_key[line["key"]] for line in note.lines]
    if review_note is not None:
        note.review_note = review_note.strip()
    _reprice(note)
    note.revision += 1
    note.save()
    after = {**snapshot(note, stage_of(note, approval_of(note))), "note": note.review_note}
    _set_command_audit(run, note, before, after)
    return note


# ---------------------------------------------------------------------------
# 3. Sent to the Owner, who approves in the approvals inbox
# ---------------------------------------------------------------------------


def _money(paise: int) -> str:
    """``Rs 12,34,567.50``: Indian grouping, paise shown only when there are some."""
    rupees, rest = divmod(abs(paise), 100)
    digits = str(rupees)
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join(part for part in [head, *groups, tail] if part)
    sign = "-" if paise < 0 else ""
    return f"{sign}Rs {grouped}" + (f".{rest:02d}" if rest else "")


def headline(note: DebitNote) -> str:
    """What the Owner reads in the inbox before opening anything."""
    pieces = sum(int(line["qty"]) for line in note.lines)
    grn_number = note.grn.document.official_number or "GRN"
    return (
        f"{note.vendor.name} · {grn_number} · {pieces} piece(s) short · "
        f"{_money(note.total_paise)} with tax"
    )


def request_issue_approval(
    run: CommandRun, note_id: int, *, user: Any, expected_revision: int | None
) -> tuple[DebitNote, Approval]:
    """Accounts asks the Owner to approve issuing the note, exactly as it stands."""
    note = _lock(run, note_id)
    _check_revision(note, expected_revision)
    require_feature(note.site_id, FEATURE_KEY)
    stage = stage_of(note, approval_of(note))
    _require_open(stage)
    problems = missing(note.lines)
    if problems:
        raise Refusal(
            "DEBIT_NOTE_INCOMPLETE",
            "Every line needs a cost per piece and a GST rate before the Owner is asked.",
            status=422,
            issues=problems,
        )
    before = snapshot(note, stage)
    note.approval_hash = figures_hash(note)
    note.revision += 1
    note.save(update_fields=["approval_hash", "revision", "updated_at"])
    # The maker stays the first person who asked, however often it is asked again,
    # as the approvals spine expects; the spine bars both maker and asker.
    first = (
        Approval.objects.filter(
            kind=APPROVAL_KIND,
            content_type=ContentType.objects.get_for_model(DebitNote),
            object_id=note.pk,
        )
        .order_by("created_at", "id")
        .select_related("made_by")
        .first()
    )
    try:
        approval = request_approval(
            note,
            kind=APPROVAL_KIND,
            kind_label=APPROVAL_LABEL,
            title=headline(note),
            made_by=first.made_by if first is not None else user,
            requested_by=user,
            approver_roles=sorted(DEBIT_NOTE_APPROVER_ROLES),
            store=note.site,
            brand=note.brand.name,
            value_paise=note.total_paise,
        )
    except AlreadyPendingError as exc:
        raise Refusal("STATE_CONFLICT", str(exc)) from exc
    _set_command_audit(
        run,
        note,
        before,
        {**snapshot(note, WAITING), "approval_id": approval.pk, "figures_hash": note.approval_hash},
    )
    return note, approval


def _decision_principal(note: DebitNote, actor: Any) -> Principal:
    """The Owner as a named person: a login with no person behind it never decides."""
    human_id = getattr(actor, "human_id", None)
    if human_id is None:
        raise ApprovalError("A debit note is approved by a named person.")
    tenant_id = current_tenant_id() or note.tenant_id
    return Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None))


def _record_decision(note: DebitNote, actor: Any, *, approved: bool, reason: str) -> None:
    """The Owner's decision as its own audited command, inside the decision's transaction.

    The approvals spine has already checked the role, and that the Owner is not
    the person who asked. An approval also needs the switch on and the note
    exactly as it was sent; a refusal undoes the whole decision.
    """
    action = APPROVE_ACTION if approved else REJECT_ACTION

    def handler(run: CommandRun) -> CommandResult:
        locked = _lock(run, note.pk)
        if approved:
            require_feature(locked.site_id, FEATURE_KEY)
            if locked.status != DebitNote.Status.DRAFT:
                raise Refusal(
                    "STATE_CONFLICT", f"This note is {locked.status}; it cannot be approved."
                )
            if locked.approval_hash != figures_hash(locked):
                raise Refusal(
                    "REVISION_SUPERSEDED",
                    "The note changed after the Owner was asked. Accounts must send it again.",
                )
        after = {
            **snapshot(locked, APPROVED if approved else REJECTED),
            "decided_by": getattr(actor, "pk", None),
            **({} if approved else {"reason": reason}),
        }
        _set_command_audit(run, locked, snapshot(locked, WAITING), after)
        run.audit_site_id = locked.site_id
        return CommandResult(resource_type="debit_note", resource_id=str(locked.pk))

    try:
        execute_command(
            _decision_principal(note, actor),
            CommandSpec(
                action=action,
                command_id=uuid.uuid4(),
                business_input={"debit_note_id": note.pk, "approved": approved, "reason": reason},
                subject_key=f"debit_note:{note.pk}",
                site_id=note.site_id,
            ),
            handler,
        )
    except Refusal as refusal:
        raise ApprovalError(refusal.message) from refusal


def on_approved(note: DebitNote, *, actor: Any) -> None:
    _record_decision(note, actor, approved=True, reason="")


def on_rejected(note: DebitNote, *, actor: Any, reason: str) -> None:
    _record_decision(note, actor, approved=False, reason=reason)


def register_approval_hooks() -> None:
    from approvals.hooks import register_on_approved, register_on_rejected

    register_on_approved(DebitNote, on_approved)
    register_on_rejected(DebitNote, on_rejected)


# ---------------------------------------------------------------------------
# 4. Issued by Accounts, or cancelled
# ---------------------------------------------------------------------------


def issue_note(
    run: CommandRun, note_id: int, *, user: Any, expected_revision: int | None, on: date
) -> DebitNote:
    """Give an approved note head office's DN number for its GSTIN. Never edited after.

    Wrap the command in ``masters.document_series.alert_on_refusal`` from outside,
    so a number that cannot be issued raises head office's alert.
    """
    note = _lock(run, note_id)
    _check_revision(note, expected_revision)
    require_feature(note.site_id, FEATURE_KEY)
    approval = approval_of(note)
    stage = stage_of(note, approval)
    if stage != APPROVED:
        raise Refusal(
            "STATE_CONFLICT",
            "Only a note the Owner has approved, exactly as it stands, can be issued."
            if stage in (DRAFT, REJECTED, WAITING)
            else f"This note is {stage}, so it cannot be issued.",
        )
    before = snapshot(note, stage)
    number = issue_number(
        series=DocumentSeries.DEBIT_NOTE,
        gstin=note.gstin,
        on=on,
        document_type="debit_note",
        document_ref=str(note.pk),
    )
    note.number = number.number
    note.issued_on = on
    note.issued_by = user
    note.status = DebitNote.Status.ISSUED
    note.revision += 1
    note.save()
    _set_command_audit(
        run,
        note,
        before,
        {
            **snapshot(note, ISSUED),
            "issued_on": on.isoformat(),
            "approved_by": approval.decided_by_id if approval is not None else None,
        },
    )
    return note


def cancel_note(
    run: CommandRun, note_id: int, *, user: Any, reason: str, expected_revision: int | None
) -> DebitNote:
    """Accounts decides not to issue a note. Allowed with the switch off: it only stops work."""
    note = _lock(run, note_id)
    _check_revision(note, expected_revision)
    reason = reason.strip()
    if not reason:
        raise Refusal("DEBIT_NOTE_INVALID", "Say why the note is cancelled.", status=422)
    if len(reason) > NOTE_LENGTH:
        raise Refusal(
            "DEBIT_NOTE_INVALID", f"The reason is at most {NOTE_LENGTH} characters.", status=422
        )
    stage = stage_of(note, approval_of(note))
    if stage == WAITING:
        raise Refusal(
            "STATE_CONFLICT",
            "This note is waiting for the Owner. The Owner sends it back first.",
        )
    if stage in (ISSUED, CANCELLED):
        raise Refusal("STATE_CONFLICT", f"This note is {stage}, so it cannot be cancelled.")
    before = snapshot(note, stage)
    note.status = DebitNote.Status.CANCELLED
    note.cancel_reason = reason
    note.cancelled_by = user
    note.revision += 1
    note.save()
    _set_command_audit(run, note, before, {**snapshot(note, CANCELLED), "reason": reason})
    return note

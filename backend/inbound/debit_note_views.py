"""Money > Debit Notes (store operations PRD ST-REC-3; ticket 38).

Under ``/api/goods-v1/inbound/``:

``GET  debit-notes``                        notes in reach (``?show=open|issued|cancelled|all``)
``GET  debit-notes/<id>``                   one note
``POST debit-notes/<id>/review``            Accounts: GST rates, a missing cost, a note
``POST debit-notes/<id>/request-approval``  Accounts: ask the Owner to approve issuing it
``POST debit-notes/<id>/issue``             Accounts: take the DN number (after approval)
``POST debit-notes/<id>/cancel``            Accounts: decide not to issue it

The Owner approves or rejects in the approvals inbox (``/api/approvals/<id>/decide``);
``inbound.debit_notes`` records each decision against the note.

Reading needs ``money: manage`` (Owner and Accounts), at the note's site. Writing
is Accounts' alone. New work needs the ``debit-note-draft`` switch on at the
note's site; reading and cancelling never do. Every write is one command, so its
audit record carries the note before and after.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_meta
from approvals.models import Approval
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from inbound import debit_notes as dn
from inbound.debit_note_models import DebitNote
from masters.document_series import alert_on_refusal
from masters.store_feature_registry import DEBIT_NOTE_DRAFT
from masters.store_features import is_feature_on

#: The newest notes a list answers with; ``more`` says when there are others.
LIST_LIMIT = 500

SHOW = {
    "open": [DebitNote.Status.DRAFT],
    "issued": [DebitNote.Status.ISSUED],
    "cancelled": [DebitNote.Status.CANCELLED],
    "all": list(DebitNote.Status.values),
}


# -- the wire ------------------------------------------------------------------------


class DebitNotePartySerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class DebitNoteVendorSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    gstin = serializers.CharField(allow_blank=True)
    state_code = serializers.CharField(allow_blank=True)


class DebitNoteGrnSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="The GRN's document id, as receiving names it.")
    number = serializers.CharField(allow_blank=True)
    arrival_id = serializers.UUIDField()


class DebitNoteClaimSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    revision = serializers.IntegerField()
    invoice_number = serializers.CharField(allow_blank=True)
    invoice_date = serializers.DateField(allow_null=True)


class DebitNoteLineSerializer(serializers.Serializer[Any]):
    key = serializers.CharField(help_text="The accepted shortage (disposition) this line is.")
    claim_line_key = serializers.CharField()
    description = serializers.CharField()
    style_code = serializers.CharField(allow_null=True)
    qty = serializers.IntegerField(help_text="Pieces invoiced and never received.")
    unit_cost_paise = serializers.CharField(
        allow_null=True, help_text="The invoice's cost per piece before tax. Null: not known yet."
    )
    cost_from = serializers.ChoiceField(
        choices=[dn.COST_FROM_INVOICE, dn.COST_FROM_ACCOUNTS], allow_null=True
    )
    gst_rate = serializers.CharField(allow_null=True, help_text="Percent, as typed. Null: not set.")
    taxable_paise = serializers.CharField(allow_null=True)
    tax_paise = serializers.CharField(allow_null=True)
    total_paise = serializers.CharField(allow_null=True)


class DebitNoteSplitSerializer(serializers.Serializer[Any]):
    kind = serializers.ChoiceField(choices=["intra", "inter", "unknown"])
    cgst_paise = serializers.CharField(allow_null=True)
    sgst_paise = serializers.CharField(allow_null=True)
    igst_paise = serializers.CharField(allow_null=True)


class DebitNoteIssueSerializer(serializers.Serializer[Any]):
    code = serializers.CharField()
    message = serializers.CharField()
    line_key = serializers.CharField(required=False)


class DebitNoteApprovalSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    status = serializers.CharField()
    requested_by = serializers.CharField()
    requested_at = serializers.DateTimeField()
    decided_by = serializers.CharField(allow_blank=True)
    decided_at = serializers.DateTimeField(allow_null=True)
    reason = serializers.CharField(allow_blank=True)


class DebitNoteSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    revision = serializers.IntegerField()
    stage = serializers.ChoiceField(
        choices=[dn.DRAFT, dn.WAITING, dn.APPROVED, dn.REJECTED, dn.ISSUED, dn.CANCELLED]
    )
    number = serializers.CharField(allow_null=True)
    issued_on = serializers.DateField(allow_null=True)
    issued_by = serializers.CharField(allow_blank=True)
    cancel_reason = serializers.CharField(allow_blank=True)
    cancelled_by = serializers.CharField(allow_blank=True)
    review_note = serializers.CharField(allow_blank=True)
    site = DebitNotePartySerializer()
    gstin = serializers.CharField(help_text="The GSTIN the note is issued under.")
    vendor = DebitNoteVendorSerializer()
    brand = DebitNotePartySerializer()
    grn = DebitNoteGrnSerializer()
    claim = DebitNoteClaimSerializer()
    lines = DebitNoteLineSerializer(many=True)
    taxable_paise = serializers.CharField()
    tax_paise = serializers.CharField()
    total_paise = serializers.CharField()
    split = DebitNoteSplitSerializer()
    missing = DebitNoteIssueSerializer(many=True)
    approval = DebitNoteApprovalSerializer(allow_null=True)
    switched_on = serializers.BooleanField(help_text="The switch is on at the note's site.")
    posted = serializers.BooleanField(
        help_text="Always false: posting through the accounting export waits for OQ-47."
    )
    allowed_actions = serializers.ListField(child=serializers.CharField())
    created_at = serializers.DateTimeField()


class DebitNoteListSerializer(serializers.Serializer[Any]):
    can_review = serializers.BooleanField(help_text="Accounts: reviews, sends and issues notes.")
    more = serializers.BooleanField(
        help_text="More notes than the newest shown here; narrow the list to see them."
    )
    gst_rates = serializers.ListField(child=serializers.CharField())
    notes = DebitNoteSerializer(many=True)


class DebitNoteReviewLineSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    gst_rate = serializers.CharField(allow_null=True, required=False)
    unit_cost_paise = serializers.CharField(allow_null=True, required=False)


class DebitNoteReviewRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()
    lines = DebitNoteReviewLineSerializer(many=True, required=False)
    note = serializers.CharField(required=False, allow_blank=True)


class DebitNoteStepRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()


class DebitNoteCancelRequestSerializer(DebitNoteStepRequestSerializer):
    reason = serializers.CharField()


# -- building answers ------------------------------------------------------------------


def _name(user: Any) -> str:
    if user is None:
        return ""
    return str(getattr(user, "full_name", "") or getattr(user, "username", "") or "")


def _paise(value: Any) -> str | None:
    """Money on the wire: whole paise as a base-10 string (goods-v1 §3.4)."""
    return None if value in (None, "") else str(int(value))


def _approval_json(approval: Approval | None) -> dict[str, Any] | None:
    if approval is None:
        return None
    return {
        "id": approval.pk,
        "status": approval.status,
        "requested_by": _name(approval.requested_by),
        "requested_at": approval.created_at,
        "decided_by": _name(approval.decided_by),
        "decided_at": approval.decided_at,
        "reason": approval.reason or "",
    }


def _allowed(stage: str, *, reviewer: bool, switched_on: bool, complete: bool) -> list[str]:
    if not reviewer:
        return []
    actions: list[str] = []
    if switched_on and stage in dn.OPEN_TO_CHANGE:
        actions.append("review")
        if complete:
            actions.append("request_approval")
    if switched_on and stage == dn.APPROVED:
        actions.append("issue")
    if stage in (dn.DRAFT, dn.REJECTED, dn.APPROVED):
        actions.append("cancel")
    return actions


def note_json(
    note: DebitNote, approval: Approval | None, *, reviewer: bool, switched_on: bool
) -> dict[str, Any]:
    stage = dn.stage_of(note, approval)
    problems = dn.missing(note.lines) if stage in dn.OPEN_TO_CHANGE else []
    return {
        "id": note.pk,
        "revision": note.revision,
        "stage": stage,
        "number": note.number,
        "issued_on": note.issued_on,
        "issued_by": _name(note.issued_by),
        "cancel_reason": note.cancel_reason,
        "cancelled_by": _name(note.cancelled_by),
        "review_note": note.review_note,
        "site": {"id": note.site.pk, "code": note.site.code, "name": note.site.name},
        "gstin": note.gstin.gstin,
        "vendor": {
            "id": note.vendor.pk,
            "name": note.vendor.name,
            "gstin": note.vendor.gstin,
            "state_code": note.vendor.state_code or note.vendor.gstin[:2],
        },
        "brand": {"id": note.brand.pk, "code": note.brand.code, "name": note.brand.name},
        "grn": {
            "id": note.grn.document_id,
            "number": note.grn.document.official_number or "",
            "arrival_id": note.grn.arrival_id,
        },
        "claim": {
            "id": note.claim.pk,
            "revision": note.claim.revision,
            "invoice_number": note.invoice_number,
            "invoice_date": note.invoice_date,
        },
        "lines": [
            {
                "key": line["key"],
                "claim_line_key": line["claim_line_key"],
                "description": line["description"],
                "style_code": line.get("style_code"),
                "qty": int(line["qty"]),
                "unit_cost_paise": _paise(line.get("unit_cost_paise")),
                "cost_from": line.get("cost_from"),
                "gst_rate": line.get("gst_rate"),
                "taxable_paise": _paise(line.get("taxable_paise")),
                "tax_paise": _paise(line.get("tax_paise")),
                "total_paise": (
                    None
                    if line.get("tax_paise") in (None, "")
                    else str(int(line["taxable_paise"]) + int(line["tax_paise"]))
                ),
            }
            for line in note.lines
        ],
        "taxable_paise": str(note.taxable_paise),
        "tax_paise": str(note.tax_paise),
        "total_paise": str(note.total_paise),
        "split": {
            key: (value if key == "kind" or value is None else str(value))
            for key, value in dn.split(
                note.tax_paise,
                note.vendor.state_code or note.vendor.gstin[:2],
                note.gstin.state_code,
            ).items()
        },
        "missing": problems,
        "approval": _approval_json(approval),
        "switched_on": switched_on,
        "posted": False,
        "allowed_actions": _allowed(
            stage, reviewer=reviewer, switched_on=switched_on, complete=not problems
        ),
        "created_at": note.created_at,
    }


def _require_reader(user: Any) -> None:
    if not dn.may_read(user):
        raise Refusal("ACTION_DENIED", "Debit notes are Accounts' and the Owner's work.")


def _require_reviewer(user: Any) -> None:
    if not dn.may_review(user):
        raise Refusal(
            "ACTION_DENIED",
            "Accounts reviews and issues debit notes. The Owner approves them in the "
            "approvals inbox.",
        )


def _readable(user: Any, tenant_id: Any, pk: int) -> DebitNote:
    note: DebitNote | None = dn.readable_notes(user, tenant_id).filter(pk=pk).first()
    if note is None:
        raise Refusal("NOT_FOUND", "That debit note was not found.")
    return note


def _answer(user: Any, tenant_id: Any, pk: int) -> dict[str, Any]:
    note = dn.readable_notes(user, tenant_id).get(pk=pk)
    return note_json(
        note,
        dn.approval_of(note),
        reviewer=dn.may_review(user),
        switched_on=is_feature_on(note.site_id, DEBIT_NOTE_DRAFT),
    )


# -- the views ---------------------------------------------------------------------------


class GoodsDebitNoteListView(GoodsAPIView):
    @extend_schema(
        parameters=[
            OpenApiParameter("show", str, enum=list(SHOW), description="Which notes (open).")
        ],
        responses=DebitNoteListSerializer,
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, allowed=("show",))
        _require_reader(request.user)
        show = params.get("show") or "open"
        if show not in SHOW:
            raise Refusal("INVALID_REQUEST", f"show is one of {', '.join(SHOW)}.")
        rows = list(
            dn.readable_notes(request.user, access.tenant_id).filter(status__in=SHOW[show])[
                : LIST_LIMIT + 1
            ]
        )
        notes = rows[:LIST_LIMIT]
        newest = dn.approvals_of(note.pk for note in notes)
        switches = {
            site_id: is_feature_on(site_id, DEBIT_NOTE_DRAFT)
            for site_id in {note.site_id for note in notes}
        }
        reviewer = dn.may_review(request.user)
        body = {
            "can_review": reviewer,
            "more": len(rows) > LIST_LIMIT,
            "gst_rates": [dn.rate_text(rate) for rate in dn.gst_rates()],
            "notes": [
                note_json(
                    note,
                    newest.get(note.pk),
                    reviewer=reviewer,
                    switched_on=switches[note.site_id],
                )
                for note in notes
            ],
        }
        return Response(DebitNoteListSerializer(body).data)


class GoodsDebitNoteDetailView(GoodsAPIView):
    @extend_schema(responses=DebitNoteSerializer)
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        _require_reader(request.user)
        _readable(request.user, access.tenant_id, pk)
        return Response(DebitNoteSerializer(_answer(request.user, access.tenant_id, pk)).data)


class _StepView(GoodsAPIView):
    """One Accounts step on one note: the shared checks, then one command."""

    action = ""
    fields: frozenset[str] = frozenset()
    required: tuple[str, ...] = ()

    def step(self, run: CommandRun, note: DebitNote, body: dict[str, Any], meta: Any) -> None:
        raise NotImplementedError

    def run_step(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        user = request.user
        _require_reader(user)
        note = _readable(user, access.tenant_id, pk)
        _require_reviewer(user)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, self.fields, required=self.required)

        def handler(run: CommandRun) -> CommandResult:
            self.step(run, note, body, meta)
            return CommandResult(resource_type="debit_note", resource_id=str(note.pk))

        self.run_command(
            request,
            access=access,
            action=self.action,
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(note.pk)],
            subject_key=f"debit_note:{note.pk}",
            site_id=note.site_id,
        )
        return Response(DebitNoteSerializer(_answer(user, access.tenant_id, pk)).data)


class GoodsDebitNoteReviewView(_StepView):
    action = dn.REVIEW_ACTION
    fields = frozenset({"lines", "note"})

    def step(self, run: CommandRun, note: DebitNote, body: dict[str, Any], meta: Any) -> None:
        lines = body.get("lines") or []
        if not isinstance(lines, list) or not all(isinstance(line, dict) for line in lines):
            raise Refusal("INVALID_REQUEST", "lines must be a list of objects.")
        text = body.get("note")
        if text is not None and not isinstance(text, str):
            raise Refusal("INVALID_REQUEST", "note must be text.")
        dn.review(
            run, note.pk, changes=lines, review_note=text, expected_revision=meta.expected_revision
        )

    @extend_schema(request=DebitNoteReviewRequestSerializer, responses=DebitNoteSerializer)
    def post(self, request: Request, pk: int) -> Response:
        return self.run_step(request, pk)


class GoodsDebitNoteRequestApprovalView(_StepView):
    action = dn.REQUEST_ACTION

    def step(self, run: CommandRun, note: DebitNote, body: dict[str, Any], meta: Any) -> None:
        dn.request_issue_approval(
            run, note.pk, user=self.request.user, expected_revision=meta.expected_revision
        )

    @extend_schema(request=DebitNoteStepRequestSerializer, responses=DebitNoteSerializer)
    def post(self, request: Request, pk: int) -> Response:
        return self.run_step(request, pk)


class GoodsDebitNoteIssueView(_StepView):
    action = dn.ISSUE_ACTION

    def step(self, run: CommandRun, note: DebitNote, body: dict[str, Any], meta: Any) -> None:
        dn.issue_note(
            run,
            note.pk,
            user=self.request.user,
            expected_revision=meta.expected_revision,
            on=timezone.localdate(run.now),
        )

    @extend_schema(request=DebitNoteStepRequestSerializer, responses=DebitNoteSerializer)
    def post(self, request: Request, pk: int) -> Response:
        # A number that cannot be issued raises head office's alert, which has to
        # outlive the command's rolled-back transaction (ticket 04).
        with alert_on_refusal():
            return self.run_step(request, pk)


class GoodsDebitNoteCancelView(_StepView):
    action = dn.CANCEL_ACTION
    fields = frozenset({"reason"})
    required = ("reason",)

    def step(self, run: CommandRun, note: DebitNote, body: dict[str, Any], meta: Any) -> None:
        dn.cancel_note(
            run,
            note.pk,
            user=self.request.user,
            reason=str(body["reason"]),
            expected_revision=meta.expected_revision,
        )

    @extend_schema(request=DebitNoteCancelRequestSerializer, responses=DebitNoteSerializer)
    def post(self, request: Request, pk: int) -> Response:
        return self.run_step(request, pk)

"""Approval read/write shapes.

The read shape is deliberately self-describing: the inbox renders a row from
the approval alone (kind, title, store, value, who asked, when) without the
client fetching the underlying document first.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from approvals.models import Approval
from approvals.services import display_name


class ApprovalReadSerializer(serializers.ModelSerializer[Approval]):
    """One inbox row / one document's approval block."""

    store_code = serializers.CharField(source="store.code", read_only=True, default="")
    store_name = serializers.CharField(source="store.name", read_only=True, default="")
    made_by_name = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    # `created_at` *is* the moment the maker asked; name it honestly for clients.
    requested_at = serializers.DateTimeField(source="created_at", read_only=True)
    steps = serializers.SerializerMethodField()

    def get_made_by_name(self, obj: Approval) -> str:
        return display_name(obj.made_by)

    def get_requested_by_name(self, obj: Approval) -> str:
        return display_name(obj.requested_by)

    def get_decided_by_name(self, obj: Approval) -> str:
        return display_name(obj.decided_by)

    def get_steps(self, obj: Approval) -> list[dict[str, Any]]:
        """The chain, one entry per step — empty where the family has none, so
        the screen renders nothing extra for the single-step families (#172)."""
        return obj.step_trail()

    class Meta:
        model = Approval
        fields = [
            "id",
            "kind",
            "kind_label",
            "title",
            "object_id",
            "store",
            "store_code",
            "store_name",
            "brand",
            "value_paise",
            "status",
            "made_by",
            "made_by_name",
            "requested_by",
            "requested_by_name",
            "requested_at",
            "decided_by",
            "decided_by_name",
            "decided_at",
            "reason",
            "current_step",
            "steps",
        ]
        read_only_fields = fields


class ApprovalDecisionSerializer(serializers.Serializer[dict[str, Any]]):
    """The decide payload — approve, or reject *with* a reason."""

    action = serializers.ChoiceField(choices=["approve", "reject"])
    reason = serializers.CharField(
        max_length=2000, required=False, allow_blank=True, default="", trim_whitespace=True
    )

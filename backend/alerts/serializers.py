"""Alert read shape — self-describing, like ``ApprovalReadSerializer``: the
inbox renders a row from the alert alone, without fetching the document (if
any) behind it first."""

from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from .models import Alert


class AlertReadSerializer(serializers.ModelSerializer[Alert]):
    store_code = serializers.CharField(source="store.code", read_only=True, default="")
    store_name = serializers.CharField(source="store.name", read_only=True, default="")
    days_left = serializers.SerializerMethodField()

    def get_days_left(self, obj: Alert) -> int | None:
        """Days to the deadline, from today — negative once it's blown past.
        ``None`` for an alert with no deadline of its own to count down to."""
        if obj.due_date is None:
            return None
        return (obj.due_date - timezone.localdate()).days

    class Meta:
        model = Alert
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
            "due_date",
            "days_left",
            "threshold_days",
            "status",
            "created_at",
            # Null on everything the open feed carries, which is why adding it
            # there costs its existing consumers nothing; on a history row it is
            # the day the condition stopped being true, and what History groups
            # by (#226).
            "resolved_at",
        ]
        read_only_fields = fields

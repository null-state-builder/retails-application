"""Read shapes for mail.

Two of them, and the split is the point: a list row must be cheap enough to send
150 of, and a body is the expensive part of a message. ``MailListSerializer``
never touches ``body_html``, so opening the popup does not ship a megabyte of
brand newsletters to a store's connection.
"""

from __future__ import annotations

from rest_framework import serializers

from .models import MailAccount, MailAttachment, MailMessage


class MailAttachmentSerializer(serializers.ModelSerializer[MailAttachment]):
    class Meta:
        model = MailAttachment
        fields = ["id", "filename", "mime_type", "size_bytes"]
        read_only_fields = fields


class MailListSerializer(serializers.ModelSerializer[MailMessage]):
    """A row in the popup or the thread list. Bodies deliberately absent."""

    is_sent = serializers.SerializerMethodField()

    def get_is_sent(self, obj: MailMessage) -> bool:
        return "SENT" in (obj.label_ids or [])

    class Meta:
        model = MailMessage
        fields = [
            "id",
            "thread_id",
            "subject",
            "from_name",
            "from_email",
            "to_emails",
            "snippet",
            "sent_at",
            "is_unread",
            "is_starred",
            "is_sent",
        ]
        read_only_fields = fields


class MailDetailSerializer(serializers.ModelSerializer[MailMessage]):
    """One opened message, with its body and its attachment list."""

    attachments = MailAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = MailMessage
        fields = [
            "id",
            "thread_id",
            "subject",
            "from_name",
            "from_email",
            "to_emails",
            "cc_emails",
            "snippet",
            "body_html",
            "body_text",
            "sent_at",
            "is_unread",
            "is_starred",
            "label_ids",
            "attachments",
        ]
        read_only_fields = fields


class MailAccountSerializer(serializers.ModelSerializer[MailAccount]):
    """What the top bar needs to decide which of three states to draw:
    not connected, connected, or connected-but-Google-wants-you-back.

    No token field of any kind is listed, and none may be added: this shape is
    serialised straight into a browser.
    """

    class Meta:
        model = MailAccount
        fields = ["email_address", "status", "last_synced_at", "last_sync_error"]
        read_only_fields = fields

from __future__ import annotations

from django.urls import path

from mail.views import (
    MailAttachmentView,
    MailCallbackView,
    MailCompleteView,
    MailConnectView,
    MailDisconnectView,
    MailMessagesView,
    MailMessageView,
    MailReadView,
    MailSendView,
    MailStatusView,
    MailUnreadView,
)

urlpatterns = [
    path("status", MailStatusView.as_view(), name="mail-status"),
    path("connect", MailConnectView.as_view(), name="mail-connect"),
    path("callback", MailCallbackView.as_view(), name="mail-callback"),
    # Connect is three steps, not two, and this is the third. See `mail.state`:
    # the redirect above can be handed to anybody, so the mailbox attaches here
    # instead, on a request that carries the caller's own credentials.
    path("complete", MailCompleteView.as_view(), name="mail-complete"),
    path("disconnect", MailDisconnectView.as_view(), name="mail-disconnect"),
    path("unread", MailUnreadView.as_view(), name="mail-unread"),
    path("send", MailSendView.as_view(), name="mail-send"),
    path("messages", MailMessagesView.as_view(), name="mail-messages"),
    # `read` before `<int:pk>` would be harmless here (the id is typed), but the
    # detail route is listed first anyway to keep the family in the order the
    # rest of this codebase uses: the noun, then what you do to it.
    path("messages/<int:pk>", MailMessageView.as_view(), name="mail-message"),
    path("messages/<int:pk>/read", MailReadView.as_view(), name="mail-read"),
    path("attachments/<int:pk>", MailAttachmentView.as_view(), name="mail-attachment"),
]

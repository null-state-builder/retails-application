"""The mail API.

    GET  /api/mail/status              am I connected, and to what
    GET  /api/mail/connect             where to send the browser for consent
    GET  /api/mail/callback            Google returns here (browser redirect)
    POST /api/mail/complete            finish the connect, as the signed-in person
    POST /api/mail/disconnect          forget the mailbox and drop the cache
    GET  /api/mail/messages            my inbox / sent / unread, searchable
    GET  /api/mail/messages/<id>       one message, with body
    POST /api/mail/messages/<id>/read  mark read (at Gmail, then here)
    GET  /api/mail/attachments/<id>    stream one attachment's bytes
    POST /api/mail/send                send, or reply in a thread
    GET  /api/mail/unread              the badge count

Every view here reads the request, calls one thing in :mod:`service`, and turns
the answer into a status code. The rules - which mailbox may attach, what a
folder means, what has to succeed before a message counts as read - are all one
layer down, where they can be tested without a URL.

The ownership boundary lives there too, and it is worth saying twice: a message
is only ever reached through ``request.user``'s own account. See the note at the
top of :mod:`service`.

There is no section gate and that is deliberate, not an oversight: mail is
per-person, not per-store or per-role (see ``models``). ``IsAuthenticated`` is
the whole of the entitlement, because the answer is always and only the caller's
own mailbox.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from django.http import HttpResponse
from django.shortcuts import redirect
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import google, service, state, sync
from .serializers import MailAccountSerializer, MailDetailSerializer, MailListSerializer
from .service import MailRefused


def _refused(exc: MailRefused) -> Response:
    return Response({"error": exc.message, "code": exc.code}, status=exc.status)


class MailStatusView(APIView):
    """What the top bar draws. Cheap and always answers - it runs on every page."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        if not service.server_ready():
            return Response({"configured": False, "connected": False})
        account = service.connected_account(request.user)
        if account is None:
            return Response({"configured": True, "connected": False})
        return Response(
            {"configured": True, "connected": True, **MailAccountSerializer(account).data}
        )


class MailConnectView(APIView):
    """Hand back the Google consent URL.

    Returned as JSON for the client to navigate to, rather than a 302: the PWA
    calls this with a bearer token through axios, and a redirect would be
    followed by the XHR instead of by the browser's address bar.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        try:
            return Response({"url": service.begin_connect(request.user)})
        except MailRefused as exc:
            return _refused(exc)


class MailCallbackView(APIView):
    """Where Google sends the browser back.

    Unauthenticated by necessity: this is a top-level browser redirect from
    accounts.google.com and it carries no Authorization header. So it decides
    nothing. It parks the authorization code against the flow that started it
    and bounces the browser to the PWA holding a one-time handoff; the mailbox
    is attached by the *next* request, which is authenticated. The reasoning is
    in :mod:`state`, and it is the difference between this and a callback that
    can be made to attach a mailbox to somebody else's login.
    """

    permission_classes: list[Any] = []
    authentication_classes: list[Any] = []

    def get(self, request: Request) -> HttpResponse:
        frontend = os.environ.get("MAIL_FRONTEND_URL", "").strip() or "/"

        def back(outcome: str, handoff: str = "") -> HttpResponse:
            joiner = "&" if "?" in frontend else "?"
            tail = f"&mail_handoff={quote(handoff)}" if handoff else ""
            return redirect(f"{frontend}{joiner}mail={outcome}{tail}")

        returned_state = request.query_params.get("state", "")
        if request.query_params.get("error"):
            state.abandon(returned_state)
            return back("denied")

        code = request.query_params.get("code", "")
        if not code:
            state.abandon(returned_state)
            return back("no_code")

        handoff = service.receive_callback(returned_state, code)
        if handoff is None:
            return back("bad_state")
        return back("ready", handoff)


class MailCompleteView(APIView):
    """Finish a connect that Google has already approved.

    Authenticated, and that is the entire point of the endpoint: the mailbox
    attaches to the person whose credentials are on *this* request, and only if
    they are also the person who started the flow.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        handoff = str(request.data.get("handoff") or "").strip()
        try:
            account = service.complete_connect(request.user, handoff)
        except MailRefused as exc:
            return _refused(exc)
        return Response({"connected": True, **MailAccountSerializer(account).data})


class MailDisconnectView(APIView):
    """Forget the mailbox: revoke at Google, drop the tokens, drop the cache."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        service.disconnect(request.user)
        return Response({"connected": False})


class MailMessagesView(APIView):
    """The list, for both the popup and the screen.

    ``?box=inbox|sent|unread`` picks the folder, ``?q=`` searches what is
    cached, ``?limit=`` trims for the popup, ``?sync=0`` skips the pull for a
    read that must not wait on Google.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        account = service.connected_account(request.user)
        if account is None:
            return Response({"connected": False, "messages": []})

        if request.query_params.get("sync") != "0":
            sync.sync_account(account)

        try:
            limit = int(request.query_params.get("limit", service.PAGE_SIZE))
        except ValueError:
            limit = service.PAGE_SIZE

        rows = service.folder(
            request.user,
            request.query_params.get("box", "inbox"),
            query=request.query_params.get("q") or "",
            limit=limit,
        )
        return Response(
            {
                "connected": True,
                "status": account.status,
                "email_address": account.email_address,
                "last_sync_error": account.last_sync_error,
                "messages": MailListSerializer(rows, many=True).data,
            }
        )


class MailMessageView(APIView):
    """One message, body and all."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        try:
            message = service.message_or_refuse(request.user, pk)
        except MailRefused as exc:
            return _refused(exc)
        return Response(MailDetailSerializer(message).data)


class MailReadView(APIView):
    """Mark one message read, at Google *and* here - in that order."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, pk: int) -> Response:
        try:
            message = service.message_or_refuse(request.user, pk)
            service.mark_read(message)
        except MailRefused as exc:
            return _refused(exc)
        return Response({"is_unread": message.is_unread})


class MailUnreadView(APIView):
    """The badge. Kept separate from the list so the top bar can poll it
    without pulling sixty rows and their snippets on every page change."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        account = service.connected_account(request.user)
        if account is None:
            return Response({"connected": False, "unread": 0})
        sync.sync_account(account)
        return Response(
            {
                "connected": True,
                "status": account.status,
                "unread": service.unread_count(request.user),
                "last_sync_error": account.last_sync_error,
            }
        )


class MailAttachmentView(APIView):
    """Stream one attachment's bytes from Google. Never stored - see
    ``models.MailAttachment``."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> HttpResponse:
        attachment = service.attachment_of(request.user, pk)
        if attachment is None:
            return HttpResponse(status=404)
        try:
            raw = service.attachment_bytes(attachment)
        except (google.GoogleError, KeyError):
            return HttpResponse(status=502)

        response = HttpResponse(
            raw, content_type=attachment.mime_type or "application/octet-stream"
        )
        # `attachment` rather than `inline`: a mailbox can carry anything, and
        # rendering an arbitrary sender's file in our own origin is how a mail
        # attachment becomes stored XSS against the app.
        safe_name = attachment.filename.replace('"', "")
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


class MailSendView(APIView):
    """Send a new mail, or reply inside a thread."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        try:
            service.send(
                request.user,
                to=str(request.data.get("to") or ""),
                subject=str(request.data.get("subject") or ""),
                body=str(request.data.get("body") or ""),
                cc=str(request.data.get("cc") or ""),
                reply_to=request.data.get("reply_to"),
            )
        except MailRefused as exc:
            return _refused(exc)
        return Response({"sent": True}, status=201)

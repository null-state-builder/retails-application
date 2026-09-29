"""What mail actually *does*, away from the HTTP.

The views below this module have one job each: read the request, call one thing
here, and turn the answer into a status code. Everything with a rule in it -
which mailbox may attach, which rows a folder means, what has to succeed before
a message counts as read, how a reply threads - lives here, where it can be
tested by calling a function instead of by driving a URL.

**The one security rule the whole app turns on.** Every read starts at a person
and reaches a message only through that person's own account:

    MailMessage.objects.filter(account__user=user, pk=...)

never ``MailMessage.objects.get(pk=...)``. A message id is a small integer and
guessing one is trivial, so an id-first lookup with an ownership check bolted on
afterwards is one forgotten line away from letting any logged-in person read the
owner's mail. Beginning at the user makes the wrong query impossible to write by
omission - the filter *is* the lookup. :func:`messages_of` is the only door, and
every function here that takes an id goes through it.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from . import crypto, google, state, sync
from .models import MailAccount, MailAccountStatus, MailAttachment, MailMessage

#: How many rows a list read returns. The popup asks for far fewer.
PAGE_SIZE = 60

#: The folders the screen offers. Gmail holds the rest of a person's labels.
FOLDERS = ("inbox", "sent", "unread")


class MailRefused(Exception):
    """Mail cannot do what was asked, for a reason worth telling the person.

    Carries the machine ``code`` the PWA switches on and the HTTP ``status`` the
    view should answer with, so a view never has to re-derive either.
    """

    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# --- the server's own readiness ---------------------------------------------


def server_ready() -> bool:
    """Does this server have the credentials to offer mail at all?

    Distinct from "you are not connected", because the fix belongs to an
    operator rather than to the person looking at the screen - and a connect
    button that cannot work is worse than no button.
    """
    return crypto.is_configured() and google.oauth_client().configured


def require_ready() -> None:
    if not server_ready():
        raise MailRefused(
            "Mail is not set up on this server yet.",
            code="MAIL_NOT_CONFIGURED",
            status=503,
        )


# --- reading ------------------------------------------------------------------


def account_of(user: Any) -> MailAccount | None:
    return MailAccount.objects.filter(user=user).first()


def connected_account(user: Any) -> MailAccount | None:
    account = account_of(user)
    if account is None or account.status == MailAccountStatus.DISCONNECTED:
        return None
    return account


def messages_of(user: Any) -> QuerySet[MailMessage]:
    """Every message read in this app comes through here. See the module note."""
    return MailMessage.objects.filter(account__user=user)


def folder(user: Any, name: str, query: str = "", limit: int = PAGE_SIZE) -> list[MailMessage]:
    """One folder's rows, searched and trimmed.

    Search runs against our own rows rather than Gmail's search API on purpose:
    it answers instantly, it works when Google is unreachable, and the cached
    window is what the screen shows anyway - searching a range wider than the
    list can display would return results that cannot be opened.
    """
    rows = messages_of(user)
    if name == "sent":
        rows = rows.filter(label_ids__contains=["SENT"])
    elif name == "unread":
        rows = rows.filter(is_unread=True, label_ids__contains=["INBOX"])
    else:
        rows = rows.filter(label_ids__contains=["INBOX"])

    query = query.strip()
    if query:
        rows = rows.filter(
            Q(subject__icontains=query)
            | Q(from_name__icontains=query)
            | Q(from_email__icontains=query)
            | Q(snippet__icontains=query)
            | Q(body_text__icontains=query)
        )
    return list(rows[: max(1, min(limit, PAGE_SIZE))])


def unread_count(user: Any) -> int:
    return messages_of(user).filter(is_unread=True, label_ids__contains=["INBOX"]).count()


def message_or_refuse(user: Any, pk: int) -> MailMessage:
    message = messages_of(user).select_related("account").filter(pk=pk).first()
    if message is None:
        # 404 and not 403: from this person's side that row genuinely does not
        # exist, and a 403 would confirm the id is real - a free oracle for
        # walking the table.
        raise MailRefused("No such message in your mailbox.", code="MAIL_NOT_FOUND", status=404)
    return message


def attachment_of(user: Any, pk: int) -> MailAttachment | None:
    """Reached *through* this person's own messages, so an id from another
    mailbox simply does not exist from here."""
    return (
        MailAttachment.objects.filter(pk=pk, message__account__user=user)
        .select_related("message__account")
        .first()
    )


def attachment_bytes(attachment: MailAttachment) -> bytes:
    token = sync.usable_access_token(attachment.message.account)
    payload = google.gmail_get(
        token,
        f"/messages/{attachment.message.google_id}/attachments/{attachment.google_attachment_id}",
    )
    return google.b64url_decode(str(payload["data"]))


# --- connecting ----------------------------------------------------------------


def begin_connect(user: Any) -> str:
    """The Google consent URL to send this person's browser to."""
    require_ready()
    return google.authorize_url(state.start(user))


def receive_callback(returned_state: str, code: str) -> str | None:
    """Google's redirect landed. Park the code, hand back the one-time handoff.

    Nothing is attached here, deliberately - see ``state`` for why the redirect
    is the wrong moment to decide whose mailbox this is.
    """
    if not server_ready():
        return None
    return state.claim(returned_state, code)


def complete_connect(user: Any, handoff: str) -> MailAccount:
    """Finish the flow, as the person who is signed in right now.

    ``user`` comes from the request's own credentials, and :func:`state.finish`
    refuses a handoff belonging to anybody else. That mismatch is the entire
    defence against a mailbox being attached to the wrong login.
    """
    require_ready()
    code = state.finish(handoff, user)
    if code is None:
        raise MailRefused(
            "That sign-in has expired or was not yours. Please connect again.",
            code="MAIL_BAD_HANDOFF",
            status=400,
        )
    try:
        tokens = google.exchange_code(code)
        address, hosted_domain = google.consented_identity(tokens)
    except google.GoogleError as exc:
        raise MailRefused(str(exc), code="MAIL_CONNECT_FAILED", status=502) from exc

    _require_workspace(address, hosted_domain)
    return _attach(user, address, tokens)


def _require_workspace(address: str, hosted_domain: str) -> None:
    """Only a KDPS Workspace mailbox may attach, when a domain is configured.

    Both halves are checked. ``hd`` is Google's own statement about which
    Workspace the account belongs to and is the one that matters; the address is
    checked too because an account can be in the domain while sending as an
    alias outside it, and the address is what the screen will show over the
    inbox.
    """
    domain = google.workspace_domain()
    if not domain:
        return
    if hosted_domain.lower() != domain or not address.lower().endswith(f"@{domain}"):
        raise MailRefused(
            f"Only {domain} mailboxes can be connected here.",
            code="MAIL_WRONG_DOMAIN",
            status=400,
        )


def _attach(user: Any, address: str, tokens: dict[str, Any]) -> MailAccount:
    account, _ = MailAccount.objects.get_or_create(user=user, defaults={"email_address": address})
    account.email_address = address
    account.access_token = tokens["access_token"]
    account.access_expires_at = google.expires_at(tokens)
    # Only overwrite the refresh token when Google actually sent one. A consent
    # replayed without `prompt=consent` returns none, and blanking the stored
    # one there would disconnect the person by reconnecting them.
    if tokens.get("refresh_token"):
        account.refresh_token = tokens["refresh_token"]
    if not account.refresh_token:
        # Nothing stored and nothing granted: the inbox would work for an hour
        # and then ask to reconnect forever. Say so now rather than look
        # connected and rot.
        raise MailRefused(
            "Google did not grant lasting access. Please connect again and accept the prompt.",
            code="MAIL_NO_REFRESH_TOKEN",
            status=502,
        )
    account.status = MailAccountStatus.CONNECTED
    account.last_sync_error = ""
    # A mailbox changing hands must never inherit the previous person's cached
    # mail or their watermark.
    account.history_id = ""
    account.save()
    MailMessage.objects.filter(account=account).delete()

    sync.sync_account(account, force=True)
    return account


def disconnect(user: Any) -> None:
    """Revoke at Google, drop the tokens, drop the cache.

    The cache goes too. Leaving read mail behind after "disconnect" would mean a
    person who unlinked their account still had their correspondence sitting in
    KDPS's database, which is not what the button says.
    """
    account = account_of(user)
    if account is None:
        return
    if account.refresh_token:
        google.revoke(account.refresh_token)
    account.messages.all().delete()
    account.refresh_token = ""
    account.access_token = ""
    account.access_expires_at = None
    account.history_id = ""
    account.status = MailAccountStatus.DISCONNECTED
    account.last_sync_error = ""
    account.save()


# --- acting on a message -------------------------------------------------------


def mark_read(message: MailMessage) -> None:
    """Read at Google first, then here. That order is the whole of the fix.

    The other order looks kinder and is a trap: Gmail generates no label-change
    history for a modify it refused, so a later incremental sync has nothing to
    reconcile against and the two sides stay different forever - bold on the
    phone, read at the desk, and the badge quietly becomes a number people learn
    to ignore.

    The exception is a message Gmail no longer has. There is nothing to come
    back for, so the local row is settled and the next sync will drop it.
    """
    if not message.is_unread:
        return
    try:
        token = sync.usable_access_token(message.account)
        google.gmail_post(
            token,
            f"/messages/{message.google_id}/modify",
            {"removeLabelIds": ["UNREAD"]},
        )
    except google.GoogleError as exc:
        if not exc.gone:
            raise MailRefused(
                "Google would not mark it read just now, so it is still unread.",
                code="MAIL_READ_FAILED",
                status=502,
            ) from exc

    message.is_unread = False
    message.label_ids = [label for label in (message.label_ids or []) if label != "UNREAD"]
    message.save(update_fields=["is_unread", "label_ids", "updated_at"])


def send(
    user: Any,
    *,
    to: str,
    subject: str = "",
    body: str = "",
    cc: str = "",
    reply_to: Any = None,
) -> None:
    """Send a new mail, or reply inside a thread."""
    account = account_of(user)
    if account is None or account.status != MailAccountStatus.CONNECTED:
        raise MailRefused("Connect your mailbox first.", code="MAIL_NOT_CONNECTED", status=400)
    if not to.strip():
        raise MailRefused("Say who this is going to.", code="MAIL_NO_RECIPIENT", status=400)

    parent = message_or_refuse(user, int(reply_to)) if reply_to else None
    mime = _compose(account, to.strip(), subject.strip(), body, cc.strip(), parent)

    payload: dict[str, Any] = {"raw": google.b64url_encode(mime.as_bytes())}
    if parent:
        payload["threadId"] = parent.thread_id

    try:
        token = sync.usable_access_token(account)
        sent = google.gmail_post(token, "/messages/send", payload)
    except google.GoogleError as exc:
        raise MailRefused(
            f"Google would not send it: {exc}", code="MAIL_SEND_FAILED", status=502
        ) from exc

    _cache_sent_copy(account, token, str(sent.get("id", "")))


def _compose(
    account: MailAccount,
    to: str,
    subject: str,
    body: str,
    cc: str,
    parent: MailMessage | None,
) -> EmailMessage:
    mime = EmailMessage()
    mime["To"] = to
    mime["From"] = account.email_address
    mime["Subject"] = subject or (f"Re: {parent.subject}" if parent else "(no subject)")
    if cc:
        mime["Cc"] = cc
    if parent and parent.rfc_message_id:
        # The RFC Message-ID, not Gmail's `google_id`. They are different
        # identifiers and only this one means anything to the recipient's
        # client; quoting Gmail's would put the reply outside the conversation
        # everywhere except Gmail itself, which threads on `threadId` anyway.
        mime["In-Reply-To"] = parent.rfc_message_id
        chain = f"{parent.rfc_references} {parent.rfc_message_id}".strip()
        mime["References"] = " ".join(chain.split())
    mime.set_content(body)
    return mime


def _cache_sent_copy(account: MailAccount, token: str, sent_id: str) -> None:
    """Pull the sent copy straight back so it appears in Sent without waiting
    for the next sync window. Best-effort: the mail has already gone, and
    failing the request now would tell the person it had not."""
    if not sent_id:
        return
    try:
        raw = google.gmail_get(token, f"/messages/{sent_id}", format="full")
        sync.store_message(account, raw)
    except (google.GoogleError, KeyError):
        pass
    account.last_synced_at = timezone.now()
    account.save(update_fields=["last_synced_at", "updated_at"])

"""Pulling mail down and turning it into rows.

**On demand, not on a timer.** This backend has no worker and no Redis, so a
background poller would mean standing up queue infrastructure for one feature.
Instead the popup and the screen each ask for a sync when they open, rate-limited
to :data:`MIN_SYNC_GAP` so that clicking about the app does not hammer Gmail.

The honest consequence, and it should not be discovered later by surprise: the
unread badge is only as fresh as the last time somebody had KDPS open. A mail
arriving overnight is counted when the person opens the app in the morning, not
at 3am. For an inbox people read during the working day that is the right
trade; when a worker exists for other reasons, ``sync_account`` is already the
whole job and ``manage.py sync_mail`` already calls it.

**Incremental by watermark.** After the first backfill Gmail is asked "what
changed since ``historyId``", which is one small call rather than a re-listing
of the mailbox. Gmail expires a watermark that gets too old; that refusal is
caught and answered with a fresh backfill rather than an error, because "your
inbox is out of date" is not something a person can act on.

**The watermark rule, which is the whole correctness of this file.** A
``historyId`` may only be written after everything up to it has been stored.
Two ways to get that wrong, both of which lose mail silently and for good:

* Taking the watermark *after* a backfill. Anything that arrives while the
  backfill is running falls between the listing and the stamp, and the next
  incremental starts past it. So the watermark is read **before** the listing
  and a little is re-seen rather than any of it missed.
* Stopping at the first page of history and stamping the newest id anyway.
  Gmail pages ``users.history.list``; the records beyond the first page are
  changes we never looked at. So every page is exhausted, and the stamp is the
  id of the last page actually processed - never a number read from somewhere
  else.

The same rule is why a transient failure mid-sync must *raise*: an aborted sync
leaves the old watermark in place and comes back for the same window, where an
aborted sync that still stamped would skip it forever.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from . import google
from .models import MailAccount, MailAccountStatus, MailAttachment, MailMessage

#: How many messages a first backfill pulls. A working mailbox holds years; the
#: screen shows a working window, and "older than this, open Gmail" is a far
#: better answer than a first connect that takes four minutes and stores 40MB
#: of somebody's 2019 mail.
BACKFILL_LIMIT = 150

#: Don't re-sync more often than this, however many times a screen asks.
MIN_SYNC_GAP = timedelta(seconds=45)

#: Which of Gmail's own labels we mirror. INBOX and SENT are the two the screen
#: offers; the rest of a person's folders stay in Gmail.
SYNC_QUERY = "in:inbox OR in:sent"

#: History records per page, and how many pages one sync will walk.
#:
#: The cap is not a "good enough" shortcut - stopping early is safe *because*
#: the watermark only ever advances to the last page actually processed, so the
#: next sync resumes exactly where this one stopped. It exists so that a mailbox
#: with a colossal change backlog cannot hold one HTTP request open forever.
HISTORY_PAGE_SIZE = 200
MAX_HISTORY_PAGES = 25


# --- token plumbing ---------------------------------------------------------


def usable_access_token(account: MailAccount) -> str:
    """A live access token for this account, refreshing if needed.

    The refresh is written back inside the call rather than left to the caller:
    a token refreshed and not saved means every single request refreshes again,
    which is both slow and a good way to hit Google's rate limits.
    """
    if account.access_token and account.access_expires_at:
        if account.access_expires_at - google.EXPIRY_MARGIN > timezone.now():
            return account.access_token

    refresh = account.refresh_token
    if not refresh:
        _mark_needs_reconnect(account, "No stored Google token - please reconnect.")
        raise google.GoogleError("This mailbox is not connected.", needs_reconnect=True)

    try:
        fresh = google.refresh_access_token(refresh)
    except google.GoogleError as exc:
        if exc.needs_reconnect:
            _mark_needs_reconnect(account, "Google asked for this mailbox to be reconnected.")
        raise

    account.access_token = fresh["access_token"]
    account.access_expires_at = google.expires_at(fresh)
    # Google usually omits refresh_token on a refresh; when it does rotate one,
    # dropping it would strand the account at the next refresh.
    if fresh.get("refresh_token"):
        account.refresh_token = fresh["refresh_token"]
    account.status = MailAccountStatus.CONNECTED
    account.save(
        update_fields=[
            "access_token_enc",
            "access_expires_at",
            "refresh_token_enc",
            "status",
            "updated_at",
        ]
    )
    return account.access_token


def _mark_needs_reconnect(account: MailAccount, why: str) -> None:
    account.status = MailAccountStatus.NEEDS_RECONNECT
    account.last_sync_error = why
    account.save(update_fields=["status", "last_sync_error", "updated_at"])


# --- parsing ----------------------------------------------------------------

_ADDRESS = re.compile(r"^\s*(?P<name>.*?)\s*<(?P<email>[^>]+)>\s*$")


def split_address(raw: str) -> tuple[str, str]:
    """``"Levi's Sales" <sales@levi.in>`` → ``("Levi's Sales", "sales@levi.in")``.

    A bare address keeps the address in both halves rather than inventing a
    blank name, so a list row always has something to show.
    """
    if not raw:
        return "", ""
    match = _ADDRESS.match(raw)
    if match:
        return match.group("name").strip('" '), match.group("email").strip()
    return "", raw.strip()


def header(payload: dict[str, Any], name: str) -> str:
    """Headers are a list of dicts, and their case is not guaranteed."""
    wanted = name.lower()
    for entry in payload.get("headers", []) or []:
        if str(entry.get("name", "")).lower() == wanted:
            return str(entry.get("value", ""))
    return ""


def parse_date(raw: str, fallback_ms: str | None) -> datetime:
    """The ``Date:`` header if it parses, Gmail's own receive stamp if not.

    Malformed Date headers are common in bulk brand mail, and a message that
    cannot be dated must not be dropped or dated *now* - dating it now would
    park spam permanently at the top of everybody's inbox.
    """
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except (TypeError, ValueError):
            pass
    if fallback_ms:
        try:
            return datetime.fromtimestamp(int(fallback_ms) / 1000, tz=UTC)
        except (TypeError, ValueError, OSError):
            pass
    return timezone.now()


def walk_parts(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """Flatten a MIME tree into (text, html, attachments).

    Gmail nests arbitrarily - ``multipart/mixed`` holding a
    ``multipart/alternative`` holding the two bodies, with attachments as
    siblings - so this recurses rather than reading ``parts[0]`` and hoping.
    """
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    def visit(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        filename = part.get("filename") or ""

        # An attachment is anything with a filename and a handle to its bytes -
        # inline images included, which is why filename alone is not the test.
        if filename and body.get("attachmentId"):
            attachments.append(
                {
                    "attachment_id": body["attachmentId"],
                    "filename": filename,
                    "mime_type": mime,
                    "size": int(body.get("size") or 0),
                }
            )
        elif body.get("data"):
            decoded = google.b64url_decode(body["data"]).decode("utf-8", errors="replace")
            if mime == "text/html":
                html_parts.append(decoded)
            elif mime == "text/plain":
                text_parts.append(decoded)

        for child in part.get("parts", []) or []:
            visit(child)

    visit(payload)
    return "\n".join(text_parts), "\n".join(html_parts), attachments


# --- writing ----------------------------------------------------------------


@transaction.atomic
def store_message(account: MailAccount, raw: dict[str, Any]) -> MailMessage:
    """Upsert one Gmail message into the cache."""
    payload = raw.get("payload", {}) or {}
    labels = list(raw.get("labelIds", []) or [])
    from_name, from_email = split_address(header(payload, "From"))
    text, html, attachments = walk_parts(payload)

    message, _ = MailMessage.objects.update_or_create(
        account=account,
        google_id=raw["id"],
        defaults={
            "thread_id": raw.get("threadId", ""),
            # Kept because a reply has to quote them, and they are not
            # recoverable from anything else we store - Gmail's own id is a
            # different identifier that means nothing to the recipient's client.
            "rfc_message_id": header(payload, "Message-ID")[:500],
            "rfc_references": header(payload, "References"),
            "subject": header(payload, "Subject")[:500],
            "from_name": from_name[:200],
            "from_email": from_email[:320],
            "to_emails": header(payload, "To"),
            "cc_emails": header(payload, "Cc"),
            "snippet": (raw.get("snippet") or "")[:500],
            "body_text": text,
            "body_html": html,
            "sent_at": parse_date(header(payload, "Date"), raw.get("internalDate")),
            "is_unread": "UNREAD" in labels,
            "is_starred": "STARRED" in labels,
            "label_ids": labels,
        },
    )
    # Replaced wholesale rather than diffed: attachments never change on a sent
    # message, the list is tiny, and a diff would be more code than it saves.
    message.attachments.all().delete()
    MailAttachment.objects.bulk_create(
        [
            MailAttachment(
                message=message,
                google_attachment_id=a["attachment_id"],
                filename=a["filename"][:300],
                mime_type=a["mime_type"][:200],
                size_bytes=a["size"],
            )
            for a in attachments
        ]
    )
    return message


def _fetch_and_store(account: MailAccount, token: str, message_ids: list[str]) -> int:
    stored = 0
    for message_id in message_ids:
        try:
            raw = google.gmail_get(token, f"/messages/{message_id}", format="full")
        except google.GoogleError as exc:
            if exc.gone:
                # Deleted between the listing and the fetch. Genuinely nothing
                # to come back for, so the other 149 carry on.
                continue
            # Everything else - a 429, a 500, a dropped connection - is a
            # question that still has an answer. Aborting leaves the watermark
            # where it was and this message is asked for again next time;
            # skipping it would advance past it and lose it for good.
            raise
        store_message(account, raw)
        stored += 1
    return stored


def current_history_id(token: str) -> str:
    """The mailbox's watermark right now, from ``/profile``."""
    return str(google.gmail_get(token, "/profile").get("historyId", "") or "")


def backfill(account: MailAccount, token: str) -> tuple[int, str]:
    """A first (or recovery) load. Returns what was written and the watermark.

    The watermark is read *before* the listing, so anything that arrives while
    the backfill runs is inside the next incremental's window rather than behind
    it. That re-reads a handful of messages in the worst case, which costs an
    upsert; the other order loses them.
    """
    watermark = current_history_id(token)
    listing = google.gmail_get(token, "/messages", q=SYNC_QUERY, maxResults=BACKFILL_LIMIT)
    ids = [m["id"] for m in listing.get("messages", []) or []]
    return _fetch_and_store(account, token, ids), watermark


def incremental(account: MailAccount, token: str) -> tuple[int, str]:
    """Everything that changed since our watermark, to the last page we finish.

    A watermark Gmail has expired comes back as 404; that is not an error to
    show anybody, it is an instruction to start again from a full listing. Any
    *other* refusal is raised, because treating a 500 as "your watermark is
    stale" would quietly re-download the mailbox every time Google hiccups.
    """
    written = 0
    watermark = account.history_id
    page_token = ""

    for _ in range(MAX_HISTORY_PAGES):
        params: dict[str, Any] = {
            "startHistoryId": account.history_id,
            "maxResults": HISTORY_PAGE_SIZE,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            changes = google.gmail_get(token, "/history", **params)
        except google.GoogleError as exc:
            if exc.gone and not page_token:
                return backfill(account, token)
            raise

        records = changes.get("history", []) or []
        written += _apply_history_page(account, token, records)
        # Only now, with this page stored, may the watermark move - and it moves
        # to the last *record* we processed, never to the `historyId` on the
        # response. That field is the mailbox's current position, which on a
        # paged answer is somewhere ahead of the records in hand; stamping it
        # would step over every page we have not read yet. A page that raised
        # above never reaches this line, so the watermark stays behind it.
        watermark = _last_record_id(records) or watermark
        page_token = str(changes.get("nextPageToken", "") or "")
        if not page_token:
            # Nothing left to page. With the whole window processed, Gmail's own
            # current id is the honest place to resume from - and it is the only
            # thing that moves the watermark at all when nothing changed.
            watermark = str(changes.get("historyId", "") or watermark)
            break

    return written, watermark


def _last_record_id(records: list[dict[str, Any]]) -> str:
    """The highest history id on this page. Compared as a number because these
    are sequence ids in a string, and "9" sorts after "10" as text."""
    ids = [int(r["id"]) for r in records if str(r.get("id", "")).isdigit()]
    return str(max(ids)) if ids else ""


def _apply_history_page(account: MailAccount, token: str, records: list[dict[str, Any]]) -> int:
    touched: list[str] = []
    removed: list[str] = []
    for record in records:
        for key in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
            for entry in record.get(key, []) or []:
                message_id = (entry.get("message") or {}).get("id")
                if not message_id:
                    continue
                (removed if key == "messagesDeleted" else touched).append(message_id)

    # Deletion first: a message both relabelled and then deleted in the same
    # window must end up gone, not re-fetched.
    if removed:
        MailMessage.objects.filter(account=account, google_id__in=removed).delete()
    fresh = [mid for mid in dict.fromkeys(touched) if mid not in set(removed)]
    return _fetch_and_store(account, token, fresh)


def sync_account(account: MailAccount, *, force: bool = False) -> int:
    """Bring one mailbox up to date. Returns how many messages were written.

    Never raises at the caller: a mailbox that cannot be reached records why on
    the account and returns 0, because every caller is a screen that still has
    to render. The reason is shown as a quiet line rather than swallowed - an
    inbox that stops updating in silence is the failure that matters here.

    A failure leaves ``history_id`` exactly where it was. That is the point of
    assigning it only on the success path: the same window is re-read next time
    rather than skipped.
    """
    if not force and account.last_synced_at:
        if timezone.now() - account.last_synced_at < MIN_SYNC_GAP:
            return 0

    try:
        token = usable_access_token(account)
        if account.history_id:
            written, watermark = incremental(account, token)
        else:
            written, watermark = backfill(account, token)
        account.history_id = watermark or account.history_id
        account.last_sync_error = ""
        account.status = MailAccountStatus.CONNECTED
    except google.GoogleError as exc:
        account.last_sync_error = str(exc)[:300]
        if exc.needs_reconnect:
            account.status = MailAccountStatus.NEEDS_RECONNECT
        written = 0
    except Exception as exc:  # malformed payload, anything unforeseen
        account.last_sync_error = f"Could not reach Google just now: {exc}"[:300]
        written = 0

    account.last_synced_at = timezone.now()
    account.save(
        update_fields=[
            "history_id",
            "last_synced_at",
            "last_sync_error",
            "status",
            "updated_at",
        ]
    )
    return written

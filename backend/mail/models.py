"""Mail in the top bar: one person's Google Workspace inbox, read inside KDPS.

Three things this app is *not*, because each was a live option and the choice
shapes every table below:

**It is not a source of truth.** Mail writes no ledger and no document. Google
holds the mailbox; these tables are a cache we may drop and refill at any time
without losing anything (Rule 1 - documents write ledgers, and a mail is not a
document). That is why nothing here is append-only and nothing carries a voucher
number.

**It is not store-scoped.** Every other read in this system narrows by store or
brand (ADR-0003). Mail narrows by *person*, and that is a genuinely new rule
rather than a reuse of the old one: a store manager may see their store's whole
stock ledger and must never see their cashier's inbox. So no ``store`` column
exists to be filtered on by accident - the only key into these rows is the
account, and the only key into an account is ``request.user``.

**It is not a second RBAC section.** Adding "Mail" to ``accounts.sections``
would have meant editing the ratified SIDEBAR RBAC sheet, and it would have been
meaningless: there is no role in KDPS that should be denied *its own* mail. Mail
is available to every authenticated person and shows them exactly one mailbox -
the one they signed into. The gate is authentication, and that is the whole gate.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from core.base import TimeStampedModel

from . import crypto


class MailAccountStatus(models.TextChoices):
    CONNECTED = "connected", "Connected"
    #: Google refused our refresh token - password changed, admin revoked the
    #: grant, or the token simply went stale. Distinct from DISCONNECTED because
    #: the person did not ask for this and the UI must tell them to reconnect
    #: rather than quietly showing an empty inbox.
    NEEDS_RECONNECT = "needs_reconnect", "Needs reconnecting"
    DISCONNECTED = "disconnected", "Disconnected"


class MailAccount(TimeStampedModel):
    """The link between a KDPS login and the Google mailbox behind it.

    ``OneToOne``: a person reads one mailbox here - their own work address. Two
    mailboxes on one login would need a mailbox picker on every screen and an
    answer to "which one did that reply go from", and nobody has asked for it.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mail_account",
        help_text="The KDPS login this mailbox belongs to. CASCADE on purpose - "
        "deleting a person must take their stored mail credentials with it.",
    )
    email_address = models.EmailField(
        help_text="The Google address that actually consented, read back from "
        "Google rather than typed. A person may sign into KDPS as one name and "
        "hold mail at another, and the address shown must be the one whose mail "
        "is on screen."
    )
    # --- credentials, never plain ------------------------------------------
    refresh_token_enc = models.TextField(
        blank=True,
        default="",
        help_text="Fernet ciphertext (mail.crypto). Never read this column "
        "directly - use the refresh_token property, which is also the only "
        "place that knows an unreadable value means 'reconnect'.",
    )
    access_token_enc = models.TextField(blank=True, default="")
    access_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the short-lived access token dies. Refreshed a minute "
        "early (see google.py) so a call never starts on a token that expires "
        "mid-flight.",
    )
    status = models.CharField(
        max_length=20,
        choices=MailAccountStatus.choices,
        default=MailAccountStatus.CONNECTED,
    )
    # --- sync bookkeeping ---------------------------------------------------
    history_id = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text="Gmail's watermark. Sync asks 'what changed since this?' "
        "rather than re-listing the mailbox, which is what keeps a refresh "
        "cheap enough to run every time the popup opens. Blank means the next "
        "sync is a first backfill.",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.CharField(
        max_length=300,
        blank=True,
        default="",
        help_text="Why the last pull failed, shown to the person as a quiet "
        "line. An inbox that silently stops updating is worse than one that "
        "says it could not reach Google.",
    )

    class Meta:
        db_table = "mail_account"

    def __str__(self) -> str:
        return f"{self.user} → {self.email_address}"

    # -- credentials, through one door --------------------------------------
    @property
    def refresh_token(self) -> str:
        return crypto.decrypt(self.refresh_token_enc)

    @refresh_token.setter
    def refresh_token(self, value: str) -> None:
        self.refresh_token_enc = crypto.encrypt(value)

    @property
    def access_token(self) -> str:
        return crypto.decrypt(self.access_token_enc)

    @access_token.setter
    def access_token(self, value: str) -> None:
        self.access_token_enc = crypto.encrypt(value)


class MailOAuthFlow(TimeStampedModel):
    """One consent round trip, in flight.

    A row exists only between "somebody pressed Connect" and "the mailbox was
    attached", and never for more than ten minutes. It exists at all because the
    Google callback arrives as a bare browser redirect with no Authorization
    header, so the server cannot otherwise tell whose flow it is finishing - and
    guessing wrong is how one person's mailbox ends up attached to another
    person's login. See ``state`` for the whole argument.

    The Google authorization code is held here for the few seconds between the
    callback and the browser completing the flow. It is a bearer secret for that
    window, so it is written encrypted like every other credential in this app.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mail_oauth_flows",
        help_text="Who pressed Connect. The mailbox may only ever attach to "
        "this person, and only when this person is the one finishing.",
    )
    state = models.CharField(
        max_length=64,
        unique=True,
        help_text="The random value handed to Google as ?state=. Unique and "
        "single-use: a state that has already come back cannot come back again.",
    )
    handoff = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Minted when Google returns, and the only thing the browser "
        "is told. Blank means this flow has not been back from Google yet, "
        "which is also what makes the claim a one-shot.",
    )
    code_enc = models.TextField(
        blank=True,
        default="",
        help_text="Fernet ciphertext of Google's authorization code (mail.crypto).",
    )

    class Meta:
        db_table = "mail_oauth_flow"

    def __str__(self) -> str:
        return f"mail consent for {self.user}"


class MailMessage(TimeStampedModel):
    """One cached message.

    Cached rather than fetched live because the popup has to open instantly and
    the full screen has to be searchable, and neither is possible at Gmail's
    round-trip latency per row. Bodies are stored too - a reader that fetched
    the body on click would put a spinner between a person and every mail they
    open, all day.

    Nothing here is authoritative. Deleting every row costs one resync.
    """

    account = models.ForeignKey(
        MailAccount,
        on_delete=models.CASCADE,
        related_name="messages",
        help_text="The only route to a message. Every query in views.py starts "
        "from request.user's account, never from a message id - an id-first "
        "lookup is how one person ends up reading another's mail.",
    )
    google_id = models.CharField(max_length=64, help_text="Gmail's own message id.")
    thread_id = models.CharField(max_length=64, db_index=True)
    rfc_message_id = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="The RFC 5322 Message-ID header - a different thing from "
        "google_id, and the only one the rest of the world threads on. A reply "
        "that puts Gmail's own id in In-Reply-To lands outside the conversation "
        "in the recipient's client.",
    )
    rfc_references = models.TextField(
        blank=True,
        default="",
        help_text="The References header as it arrived. A reply appends the "
        "parent's Message-ID to this chain rather than starting a new one, "
        "which is what keeps a long vendor thread from splitting in two.",
    )
    subject = models.CharField(max_length=500, blank=True, default="")
    from_name = models.CharField(max_length=200, blank=True, default="")
    from_email = models.CharField(max_length=320, blank=True, default="")
    to_emails = models.TextField(
        blank=True, default="", help_text="Comma-joined, as Gmail returns them."
    )
    cc_emails = models.TextField(blank=True, default="")
    snippet = models.CharField(max_length=500, blank=True, default="")
    body_html = models.TextField(blank=True, default="")
    body_text = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(db_index=True)
    is_unread = models.BooleanField(default=False)
    is_starred = models.BooleanField(default=False)
    #: Gmail's own labels, kept verbatim so the screen can offer INBOX / SENT /
    #: and whatever folders KDPS invents later without a migration (Rule 12 -
    #: a new folder is data, not code).
    label_ids = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "mail_message"
        ordering = ["-sent_at"]
        constraints = [
            # Sync upserts on this pair. Scoped to the account, not global, so
            # two people on the same thread each keep their own copy with their
            # own read state - the same mail is unread for one and read for the
            # other, which is what a mailbox means.
            models.UniqueConstraint(
                fields=["account", "google_id"], name="uq_mail_message_account_google"
            ),
        ]
        indexes = [
            # The two reads that exist: "my unread, newest first" (the badge)
            # and "my inbox, newest first" (both screens).
            models.Index(fields=["account", "-sent_at"], name="ix_mail_msg_acct_sent"),
            models.Index(fields=["account", "is_unread"], name="ix_mail_msg_acct_unread"),
        ]

    def __str__(self) -> str:
        return self.subject or "(no subject)"


class MailAttachment(TimeStampedModel):
    """An attachment's *description*, never its bytes.

    Deliberately metadata-only. Copying every attachment into our database would
    turn a mailbox into a second document store with no retention rule and no
    owner, and brand PT files run to megabytes each. The bytes are streamed from
    Google on demand when somebody clicks (views.attachment), and the one place
    an attachment is allowed to become permanent is the existing `files` app,
    when a person deliberately saves it against a document.
    """

    message = models.ForeignKey(
        MailMessage,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    google_attachment_id = models.TextField(
        help_text="Gmail's handle for the bytes. Long - these are not short ids."
    )
    filename = models.CharField(max_length=300)
    mime_type = models.CharField(max_length=200, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)

    class Meta:
        db_table = "mail_attachment"
        ordering = ["filename"]

    def __str__(self) -> str:
        return self.filename

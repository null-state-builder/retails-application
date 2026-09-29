"""Document skeleton: docstatus FSM + gap-free naming series (K2, ADR-0004).

The generic skeleton every business document (booking, GRN, PT, sale) inherits —
NOT any business document itself. It owns three things and nothing more:

1. **The docstatus FSM** — `draft → submitted → cancelled`, one `post()` per doc.
   Cancel is a *reversing transition*, never a delete; a posted doc is immutable.
   The reversing half lives in `core.reversal`: `cancel()` mirrors what the
   document posted and walks the status in one transaction, and a document class
   that has not declared what it reverses refuses to cancel (#220).
2. **The naming series** — the Tally join key `{FY}/{store}/{type}/{seq}` (e.g.
   `26-27/DEO/SAL/74`), allocated gap-free and collision-free under parallel
   insert. `doc_type` is in the key because the counter is scoped per
   `(fy, store, doc_type)` — without it, SAL and GRN would both mint `…/1`.
3. **Three keys** per row — surrogate `bigint` PK · business `doc_number` ·
   `idempotency_uuid` (offline-write dedupe).

Ledger posting-leg logic (`post_entries`) is K7 and lives elsewhere — `post()`
here only mints the number and flips the status; the dimensions/snapshots ride
in with K3 and the business slices.

Defence-in-depth mirrors the K1 ledger: the **DB trigger is primary** (it binds
even the superuser CI connects as), the **ORM raises the early, clean error**.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, TypeVar

from django.db import connection, models, transaction

from core.reversal import declare_reversal, reverse_value_legs, run_declared_reversal

#: One shared trigger function for every document table.
TRIGGER_FUNCTION = "kdps_document_fsm"
#: Guard function for the gap-free counter on `core_voucher_series`.
VOUCHER_SERIES_GUARD_FUNCTION = "kdps_voucher_series_guard"

#: Document types whose number is assigned by the *writer*, not by the server.
#:
#: Exactly one exists, and it is the till: a store bills offline, so the bill has
#: to carry a number before the server ever sees it (D10 grill Q1 — one POS per
#: store, so there is exactly one writer per series). Everything else allocates
#: server-side through `VoucherSeries.allocate()`, which is the only way a number
#: can be guaranteed gap-free at the moment it is minted.
EXTERNAL_NUMBER_DOC_TYPES = frozenset({"SAL"})

#: Transaction-local flag `accept_external` raises so the DB guard can tell a
#: declared till accept (which may skip over unsynced bills) from a bulk UPDATE
#: or raw SQL rewriting the counter (which may not). See `voucher_series_guard_sql`.
EXTERNAL_ACCEPT_SETTING = "kdps.external_seq"

#: The widest hole an accept will move the counter over.
#:
#: A hole is real — a till dead for a week is a few thousand unsynced bills — so
#: the bound is generous. What it guards against is a corrupt payload naming, say,
#: sequence 2,147,483,647: the counter may never rewind (that is the one thing the
#: DB guard refuses absolutely), so a single bad accept would otherwise strand the
#: store's series at an unreachable number for the rest of the financial year.
#: Beyond the bound the document is still accepted — flag, never block — but the
#: counter stays where it was and `counter_advanced` says so, which is a condition
#: a human resolves rather than a bill a customer loses.
MAX_COUNTER_JUMP = 10_000


class DocumentError(Exception):
    """Base for every document-skeleton violation."""


class DocumentTransitionError(DocumentError):
    """An illegal docstatus move (skip, cycle, re-post, cancel-a-draft)."""


class DocumentEditError(DocumentError):
    """An attempt to edit or delete a posted/cancelled document."""


class ExternalNumberError(DocumentError):
    """A till-assigned number the series refuses to accept (bad seq, wrong type)."""


@dataclass(frozen=True)
class ExternalAllocation:
    """The outcome of accepting one till-assigned number.

    `hole_from`/`hole_count` describe numbers this accept jumped over — bills the
    till minted earlier that have not reached the server yet, or never will. They
    are reported, never refused: refusing would stop a store selling because an
    *older* bill is stuck, which is the opposite of what a hole means (Rule 8,
    flag don't block). The count is returned rather than the list because a till
    bug could name an absurd `seq`, and the kernel must not build a
    million-element range to describe it.

    `counter_advanced` is false when the hole was too large to believe: the
    document is accepted all the same, but the series counter stays where it was.
    See `MAX_COUNTER_JUMP`.
    """

    series: VoucherSeries
    doc_number: str
    hole_from: int | None
    hole_count: int
    counter_advanced: bool


@dataclass(frozen=True)
class MintedNumber:
    """What `post()` minted, handed back so the caller can act on it.

    `accepted` is set only when the number came from a till. It is the caller's
    single chance to see a hole: the sale slice reads it off `post()` and writes
    the exception row the daily reconciliation picks up.
    """

    series: VoucherSeries
    doc_number: str
    accepted: ExternalAllocation | None = None


class DocStatus(models.IntegerChoices):
    """The lifecycle every document walks, in order. Integer-valued so the DB
    trigger can reason about monotonicity with no table-specific knowledge."""

    DRAFT = 0, "draft"
    SUBMITTED = 1, "submitted"
    CANCELLED = 2, "cancelled"


class VoucherSeries(models.Model):
    """The gap-free counter behind every `doc_number` (store · type · next_seq ·
    prefix/suffix).

    A series is master config (Rule 12 — variation is data): a trained admin adds
    a row, no release. `next_seq` is a plain row counter — *not* a Postgres
    SEQUENCE — because a SEQUENCE leaks gaps on rollback, and the Tally join key
    must be gap-free. Allocation increments the counter DB-side inside the posting
    transaction, so a rolled-back post un-allocates its number; a config `save()`
    can never write the counter (so a stale in-memory instance cannot rewind it).

    Two ways in, and the difference matters:

    * `allocate()` — the server hands out the next number. Gap-free by
      construction, and the counter's sole owner for every document type but one.
    * `accept_external()` — the till arrives holding a number. Only for
      `EXTERNAL_NUMBER_DOC_TYPES`, and the trade is deliberate: that series is
      *not* gap-free, because a bill still sitting on an offline till leaves a
      hole the server can see but not fill. The hole is reported and reconciled;
      the Tally join key stays unique, which is the property that actually has to
      hold. Because a number can be re-presented on this path — a straggler
      syncing days later — the rendered form of a used external series is frozen
      (`prefix`/`suffix` cannot be edited), or the same sequence could render as
      two different keys and become two documents.
    """

    fy = models.CharField(max_length=7)  # financial year, e.g. "26-27"
    store_code = models.CharField(max_length=16)  # e.g. "DEO"
    doc_type = models.CharField(max_length=16)  # e.g. "SAL", "GRN", "PT"
    prefix = models.CharField(max_length=32, blank=True, default="")
    suffix = models.CharField(max_length=32, blank=True, default="")
    next_seq = models.BigIntegerField(default=1)
    # Goods-v1 official series (design §4.3): one counter per tenant + legal
    # entity + document type + Indian FY, rendered EntityCode/Type/YY-YY/000001.
    # Legacy rows keep `scope_version = legacy` and their original format.
    tenant = models.ForeignKey(
        "masters.Tenant", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    legal_entity = models.ForeignKey(
        "masters.LegalEntity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    scope_version = models.CharField(
        max_length=10,
        choices=[("legacy", "legacy"), ("entity_v1", "entity_v1")],
        default="legacy",
    )
    ceiling_block_size = models.IntegerField(default=1000)

    class Meta:
        db_table = "core_voucher_series"
        constraints = [
            models.UniqueConstraint(
                fields=["fy", "store_code", "doc_type"],
                name="uq_voucher_series_scope",
            ),
            models.UniqueConstraint(
                fields=["tenant", "legal_entity", "doc_type", "fy"],
                condition=models.Q(scope_version="entity_v1"),
                name="uq_voucher_series_entity_v1",
            ),
            models.CheckConstraint(
                condition=models.Q(scope_version="legacy")
                | models.Q(tenant__isnull=False, legal_entity__isnull=False),
                name="ck_voucher_series_entity_v1_scope",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.fy}/{self.store_code}/{self.doc_type} → {self.next_seq}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Persist config — but never the counter.

        `next_seq` is owned exclusively by `allocate()`'s DB-side increment. A
        config `save()` (prefix/suffix/…), possibly from a stale instance loaded
        before another till advanced the counter, must not write `next_seq` back
        — that would rewind it and reuse a number. We strip `next_seq` from every
        UPDATE; INSERT (a brand-new row) keeps the `default=1` start value.
        """
        if not self._state.adding:
            fields = kwargs.get("update_fields")
            if fields is None:
                fields = [f.name for f in self._meta.concrete_fields if not f.primary_key]
            kwargs["update_fields"] = [f for f in fields if f != "next_seq"]
        super().save(*args, **kwargs)

    def render(self, seq: int) -> str:
        """Format `seq` as the canonical `{FY}/{store}/{type}/{seq}` key, framed by
        any admin-configured prefix/suffix. Empty affixes give `26-27/DEO/SAL/74`.

        `doc_type` is part of the key: the counter is scoped per
        `(fy, store, doc_type)`, so dropping it would mint the same number for two
        types and corrupt the (globally unique) Tally join key.

        This is also where "the same number twice" would come from on an external
        series, which is why the affixes freeze once one starts counting: the
        unique constraint that makes acceptance exactly-once is on the *rendered*
        string, so a re-presented sequence must render to the byte the first
        acceptance wrote.
        """
        return f"{self.prefix}{self.fy}/{self.store_code}/{self.doc_type}/{seq}{self.suffix}"

    @classmethod
    def allocate(cls, *, fy: str, store_code: str, doc_type: str) -> tuple[VoucherSeries, str]:
        """Consume the next number for a series, gap-free and collision-free.

        MUST run inside an outer `transaction.atomic()` (the caller's posting
        transaction). The counter advances in a single DB-side statement —
        `UPDATE … SET next_seq = next_seq + 1 … RETURNING` — whose row lock is
        held until that transaction commits: concurrent allocators serialise on
        it, a rollback returns the number, and no in-memory `next_seq` is ever
        written (so a stale config save can't rewind the counter, finding #2).
        """
        with connection.cursor() as cur:
            cur.execute(
                f"UPDATE {cls._meta.db_table} "  # noqa: S608 — db_table is a trusted identifier
                "SET next_seq = next_seq + 1 "
                "WHERE fy = %s AND store_code = %s AND doc_type = %s "
                "RETURNING id, next_seq - 1",
                [fy, store_code, doc_type],
            )
            row = cur.fetchone()
        if row is None:
            raise cls.DoesNotExist(f"no VoucherSeries for {fy}/{store_code}/{doc_type}")
        series_id, seq = row
        series = cls.objects.get(pk=series_id)
        return series, series.render(seq)

    @classmethod
    def accept_external(
        cls, *, fy: str, store_code: str, doc_type: str, seq: int
    ) -> ExternalAllocation:
        """Accept a number the *till* assigned, exactly once.

        The mirror image of `allocate()`: there, the server picks the number and
        the caller takes it; here, the caller arrives holding one and the series
        decides whether it may be used. A bill is printed and in the customer's
        hand long before this runs, so the question is never "what number should
        this be" — it is "has this number already been used, and what does it say
        about the ones before it".

        Three answers, and only three:

        * `seq` is the number the series expected, or beyond it — accept, and
          advance `next_seq` past it. Anything skipped is a **hole**: earlier
          bills still queued on the till, or lost with a dead machine. Reported
          on the result, never refused (Rule 8). A hole wider than
          `MAX_COUNTER_JUMP` is still accepted, but the counter is left alone —
          see there for why.
        * `seq` is *behind* `next_seq` — a late arrival filling a hole. Accept;
          the counter does not move.
        * the number is already on a document — refused, but not here: the caller
          writes `doc_number` in this same transaction and the unique constraint
          on it is what makes acceptance exactly-once. That is why this MUST run
          inside the caller's transaction, and why it refuses to run outside one:
          without that shared transaction the row lock below is a no-op and two
          tills could accept the same number.

        Only `EXTERNAL_NUMBER_DOC_TYPES` may come this way; every other document
        still gets its number from `allocate()`.
        """
        if doc_type not in EXTERNAL_NUMBER_DOC_TYPES:
            raise ExternalNumberError(
                f"{doc_type} numbers are allocated by the server, not assigned externally; "
                f"only {sorted(EXTERNAL_NUMBER_DOC_TYPES)} may use accept_external()"
            )
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            raise ExternalNumberError(
                f"an external sequence must be a positive integer, got {seq!r}"
            )
        if not transaction.get_connection().in_atomic_block:
            raise ExternalNumberError(
                "accept_external() must run inside the caller's transaction, so that the "
                "document write which makes acceptance exactly-once commits with it"
            )

        table = cls._meta.db_table
        with connection.cursor() as cur:
            # Lock the series row for the rest of the caller's transaction:
            # concurrent accepts of the same seq serialise here, and the loser's
            # duplicate `doc_number` insert is what refuses it.
            cur.execute(
                f"SELECT id, next_seq FROM {table} "  # noqa: S608 — db_table is a trusted identifier
                "WHERE fy = %s AND store_code = %s AND doc_type = %s FOR UPDATE",
                [fy, store_code, doc_type],
            )
            row = cur.fetchone()
            if row is None:
                raise cls.DoesNotExist(f"no VoucherSeries for {fy}/{store_code}/{doc_type}")
            series_id, next_seq = row
            hole_count = max(0, seq - next_seq)
            advance = seq >= next_seq and hole_count <= MAX_COUNTER_JUMP
            if advance:
                # Declare the accept to the DB guard, which otherwise refuses any
                # jump; scoped to this transaction and cleared straight after, so
                # nothing else riding the same transaction inherits the licence.
                cur.execute(f"SELECT set_config('{EXTERNAL_ACCEPT_SETTING}', %s, true)", [str(seq)])
                cur.execute(
                    f"UPDATE {table} SET next_seq = %s WHERE id = %s",  # noqa: S608 — trusted identifier
                    [seq + 1, series_id],
                )
                cur.execute(f"SELECT set_config('{EXTERNAL_ACCEPT_SETTING}', '', true)")

        series = cls.objects.get(pk=series_id)
        return ExternalAllocation(
            series=series,
            doc_number=series.render(seq),
            hole_from=next_seq if hole_count else None,
            hole_count=hole_count,
            counter_advanced=advance,
        )


#: The concrete document a manager is attached to. `DocumentManager` is declared
#: on the *abstract* base, so without this it would bind to `Document` itself and
#: every subclass's `.objects` would answer questions about the abstract base
#: rather than the real table — `WriteOff.objects.filter(store=…)` reporting "cannot
#: resolve keyword 'store'; choices are: series". Parameterising it lets the
#: django-stubs plugin specialise the manager per concrete model, so
#: `WriteOff.objects` is a `DocumentManager[WriteOff]` and `idempotent_create` hands
#: back a `WriteOff` rather than `Any`.
_DocT = TypeVar("_DocT", bound="Document")


class DocumentManager(models.Manager[_DocT]):
    """Adds the offline-write dedupe entry point to every concrete document."""

    def idempotent_create(self, *, idempotency_uuid: uuid.UUID, **fields: Any) -> _DocT:
        """Create-or-return by `idempotency_uuid`. A retried offline write with
        the same key returns the original document — never a duplicate. The
        unique column makes the create race-safe (a losing insert re-fetches)."""
        obj, _ = self.get_or_create(idempotency_uuid=idempotency_uuid, defaults=fields)
        return obj


class Document(models.Model):
    """Abstract base carrying the three keys + the docstatus FSM.

    Concrete documents override `series_lookup()` to say which `VoucherSeries`
    mints their number; everything else — the lifecycle, immutability, the
    idempotency key — is inherited unchanged.
    """

    #: Surrogate key (key 1 of 3). `BigAutoField` via DEFAULT_AUTO_FIELD.
    #: Business doc-number (key 2 of 3) — the Tally join key. NULL while a draft;
    #: minted gap-free at post() so an abandoned draft never burns a number.
    #: 128 wide so a long-but-valid prefix/suffix config can't overflow it (#5).
    doc_number = models.CharField(max_length=128, unique=True, null=True, blank=True)
    #: Offline-write dedupe (key 3 of 3). Client-supplied for retries.
    idempotency_uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    docstatus = models.IntegerField(choices=DocStatus.choices, default=DocStatus.DRAFT)
    series = models.ForeignKey(VoucherSeries, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = DocumentManager()

    class Meta:
        abstract = True
        constraints = [
            # [#3] A non-draft row MUST carry a number. Fires on INSERT *and*
            # UPDATE, so neither objects.create(docstatus=SUBMITTED) nor a bulk
            # QuerySet.update(docstatus=1) can post a numberless row past the FSM.
            # `%(class)s` materialises a distinct name per concrete table.
            models.CheckConstraint(
                condition=models.Q(docstatus=DocStatus.DRAFT) | models.Q(doc_number__isnull=False),
                name="%(app_label)s_%(class)s_posted_has_number",
            ),
            # [#B] docstatus is a plain integer column; the IntegerChoices are a
            # Python-only convenience. Keep the stored domain to {draft, submitted,
            # cancelled} so a raw write can't park a row at an undefined status.
            models.CheckConstraint(
                condition=models.Q(docstatus__in=DocStatus.values),
                name="%(app_label)s_%(class)s_docstatus_domain",
            ),
        ]

    def __str__(self) -> str:
        return self.doc_number or f"{type(self).__name__}(draft #{self.pk})"

    # -- standard model overrides (immutability guards) ------------------------
    def save(self, *args: Any, **kwargs: Any) -> None:
        in_transition = getattr(self, "_in_transition", False)
        if not self._state.adding and not in_transition:
            loaded = getattr(self, "_loaded_docstatus", None)
            if loaded == DocStatus.SUBMITTED:
                raise DocumentEditError(
                    "a posted document is immutable; cancel and re-issue, never edit"
                )
            if loaded == DocStatus.CANCELLED:
                raise DocumentEditError("a cancelled document is frozen")
            # loaded == DRAFT (or a fresh-in-memory unsaved edit): drafts are
            # editable, but a status change must go through post()/cancel().
            if self.docstatus != DocStatus.DRAFT:
                raise DocumentTransitionError(
                    "use post()/cancel() to change docstatus, not a bare save()"
                )
        super().save(*args, **kwargs)
        self._loaded_docstatus = self.docstatus

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        loaded = getattr(self, "_loaded_docstatus", None)
        if loaded in (DocStatus.SUBMITTED, DocStatus.CANCELLED):
            raise DocumentEditError("a posted/cancelled document is cancelled, never deleted")
        return super().delete(*args, **kwargs)

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any) -> Any:
        instance = super().from_db(db, field_names, values)
        instance._loaded_docstatus = instance.docstatus
        return instance

    # -- series wiring ---------------------------------------------------------
    def series_lookup(self) -> tuple[str, str, str]:
        """Return `(fy, store_code, doc_type)` identifying this doc's series.

        The kernel skeleton is business-agnostic, so the concrete document must
        say where its number comes from. Overridden by every real document.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement series_lookup() to be postable"
        )

    def mint_number(self) -> MintedNumber:
        """Take this document's `doc_number` from its series.

        Server allocation is the default and the rule. The one document that
        overrides this is the sale, which arrives from an offline till already
        numbered and calls `VoucherSeries.accept_external()` instead — see
        `EXTERNAL_NUMBER_DOC_TYPES`. The override point is here, inside `post()`'s
        locked transaction, so an externally-numbered document still walks the
        same FSM, writes its number under the same unique constraint, and rolls
        back the same way.

        An override runs on the *locked re-read*, not on the instance the caller
        holds, which is why what it learns comes back in the return value: `post()`
        hands the `MintedNumber` straight to the caller, and that is where the sale
        slice reads the hole it has to flag.
        """
        fy, store_code, doc_type = self.series_lookup()
        series, number = VoucherSeries.allocate(fy=fy, store_code=store_code, doc_type=doc_type)
        return MintedNumber(series=series, doc_number=number)

    # -- the FSM ---------------------------------------------------------------
    def post(self) -> MintedNumber:
        """`draft → submitted`: mint the number, freeze the doc.

        Run inside `transaction.atomic()` so number allocation and the status
        flip commit together (and roll back together — gap-free).

        Returns what was minted. For a server-allocated document that is only the
        number, already on `self`; for a till-numbered one it also carries whether
        the accepted sequence left a hole behind, which the caller must record.
        """
        if self._state.adding:
            raise DocumentTransitionError("save the draft before posting it")
        if self.docstatus != DocStatus.DRAFT:
            raise DocumentTransitionError(
                f"only a draft can be posted; this is {self.get_docstatus_display()}"
            )
        with transaction.atomic():
            # Re-read the row under a lock before minting (#A). The committed row
            # is the single source of truth, and `mint_number()` is asked of *it*,
            # never of `self`: a concurrent edit to the scope fields (or a
            # concurrent post of the same draft) must not let us mint a number for
            # a stale (fy, store, doc_type), stamp a SAL number onto what the DB
            # now calls a GRN, or accept a till sequence the row no longer carries.
            # The lock also serialises double-post.
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if locked.docstatus != DocStatus.DRAFT:
                raise DocumentTransitionError(
                    f"only a draft can be posted; this is {locked.get_docstatus_display()}"
                )
            minted = locked.mint_number()
            self.series = minted.series
            self.doc_number = minted.doc_number
            self.docstatus = DocStatus.SUBMITTED
            self._save_transition(["series", "doc_number", "docstatus", "updated_at"])
            return minted

    def reverse(self, actor: Any = None) -> Any:
        """Undo everything this document posted — the reversing half of `cancel()`.

        Looked up rather than defaulted, because what a cancel owes is a property
        of what the document *did*: a bill owes its stock back and its collections
        un-taken, a PT owes the vendor bill it raised. A type that has declared
        nothing refuses to cancel rather than walking to `cancelled` over postings
        nobody reversed (#220); `core.reversal` holds the register, the declaring
        idiom, and why refusing is the conservative answer here.

        Whatever it returns is handed back by `cancel()`, so a caller that needs a
        summary of what was undone gets one without a second query.
        """
        return run_declared_reversal(self, actor)

    def cancel(self, actor: Any = None) -> Any:
        """`submitted → cancelled`: a reversing transition, never a delete.

        The row stays and every posting it made is mirrored, both inside one
        transaction: a cancel that reversed the ledger and then failed to move the
        status would be a document that had given its money back twice.

        `actor` is who is cancelling. It rides onto the reversal legs as evidence
        (Rule 10), and on the paths where the posting floor demands a named
        head-office person — a reversal that restores what a brand is owed — it is
        required rather than decorative.

        Returns whatever this document's `reverse()` returned.
        """
        if self.docstatus != DocStatus.SUBMITTED:
            raise DocumentTransitionError(
                f"only a submitted document can be cancelled; this is "
                f"{self.get_docstatus_display()}"
            )
        with transaction.atomic():
            reversed_ = self.reverse(actor)
            self.docstatus = DocStatus.CANCELLED
            self._save_transition(["docstatus", "updated_at"])
            return reversed_

    def _save_transition(self, fields: list[str]) -> None:
        self._in_transition = True
        try:
            self.save(update_fields=fields)
        finally:
            self._in_transition = False


def document_fsm_function_sql() -> str:
    """SQL for the shared docstatus-FSM trigger *function* (no table binding).

    One function guards every document table — it references only `docstatus`, so
    it is generic. It binds even the superuser CI connects as (a trigger, unlike a
    REVOKE, stops a superuser):

    * every INSERT must start in draft — a row may not be born already
      submitted/cancelled (or at an out-of-domain status), so it cannot skip the
      FSM (#B);
    * no UPDATE may walk backwards or re-post (submitted may only reach cancelled;
      a draft may only stay draft or post);
    * cancellation is status-only — it may not rewrite any other column (#4), so
      raw `UPDATE … SET docstatus=2, memo='x'` cannot mutate a posted fact;
    * a cancelled row is frozen;
    * a posted/cancelled row may not be DELETEd — it is cancelled, never deleted.

    `CREATE OR REPLACE`, so a later migration can re-apply it to update behaviour
    in place without dropping any table's trigger.
    """
    return f"""
    CREATE OR REPLACE FUNCTION {TRIGGER_FUNCTION}() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.docstatus <> 0 THEN
                RAISE EXCEPTION
                    'docstatus FSM: a new row in % must start as draft', TG_TABLE_NAME;
            END IF;
            RETURN NEW;
        END IF;
        IF TG_OP = 'DELETE' THEN
            IF OLD.docstatus <> 0 THEN
                RAISE EXCEPTION
                    'docstatus FSM: a posted/cancelled row in % is cancelled, never deleted',
                    TG_TABLE_NAME;
            END IF;
            RETURN OLD;
        END IF;
        IF OLD.docstatus = 2 THEN
            RAISE EXCEPTION 'docstatus FSM: a cancelled row in % is frozen', TG_TABLE_NAME;
        ELSIF OLD.docstatus = 1 THEN
            IF NEW.docstatus <> 2 THEN
                RAISE EXCEPTION
                    'docstatus FSM: a submitted row in % may only move to cancelled',
                    TG_TABLE_NAME;
            END IF;
            -- cancellation reverses; it must not also edit posted facts. Compare
            -- every column except the two the transition legitimately writes.
            IF (to_jsonb(OLD) - 'docstatus' - 'updated_at')
               IS DISTINCT FROM (to_jsonb(NEW) - 'docstatus' - 'updated_at') THEN
                RAISE EXCEPTION
                    'docstatus FSM: cancelling a row in % may not change any other column',
                    TG_TABLE_NAME;
            END IF;
        ELSIF OLD.docstatus = 0 AND NEW.docstatus NOT IN (0, 1) THEN
            RAISE EXCEPTION
                'docstatus FSM: a draft row in % may only stay draft or be posted', TG_TABLE_NAME;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """


def document_fsm_sql(table_name: str) -> str:
    """SQL installing the FSM function (if absent) + its trigger on `table_name`."""
    trigger_name = f"{table_name}_document_fsm"
    return f"""
    {document_fsm_function_sql()}

    CREATE TRIGGER {trigger_name}
        BEFORE INSERT OR UPDATE OR DELETE ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION {TRIGGER_FUNCTION}();
    """


def document_fsm_reverse_sql(table_name: str) -> str:
    """Reverse of `document_fsm_sql` for one table (leaves the shared function)."""
    return f"DROP TRIGGER IF EXISTS {table_name}_document_fsm ON {table_name};"


def voucher_series_guard_sql(table_name: str = "core_voucher_series") -> str:
    """SQL installing the counter-ownership guard on the voucher-series table.

    The gap-free counter is the slice's whole reason to exist, so its protection
    cannot live only in `Model.save()` — `QuerySet.update()`, raw SQL, and bulk
    writes bypass the ORM, and this trigger catches all three:

    * `next_seq` may never rewind. Not once, not by anybody: a rewind hands out a
      number that is already on a posted document.
    * otherwise `next_seq` may only *hold* (a config save of prefix/suffix) or
      advance by exactly one (an allocation) — that is what keeps server-allocated
      numbers gap-free (#D).
    * the single exception is a till accept on an `EXTERNAL_NUMBER_DOC_TYPES`
      series. Those may skip over bills that have not synced yet, so the jump has
      to be legal — but only from `accept_external()`, which declares the sequence
      it is accepting in `EXTERNAL_ACCEPT_SETTING` for the length of its
      transaction, and only to exactly that sequence plus one. A bulk UPDATE or a
      raw `SET next_seq = 999` declares nothing and is still refused, on a SAL
      series as much as any other.
    * a *used* series (one that has already minted a number, `next_seq > 1`) has
      frozen identity: re-pointing `fy`/`store_code`/`doc_type` would strand its
      counter and let the old scope restart at 1, colliding with history (#E). On
      an external series `prefix`/`suffix` freeze with it, because there a
      sequence can legitimately be re-presented (a straggler bill syncing late)
      and it must render to the same key it rendered the first time.

    One honest limit on "binds even the superuser": somebody writing raw SQL can
    also call `set_config` and mint themselves the same licence — as they could
    simply drop the trigger. What this stops is every path that is not deliberately
    impersonating the accept: application code, bulk updates, migrations, a hand
    UPDATE at a psql prompt.
    """
    trigger_name = f"{table_name}_guard"
    external_types = ", ".join(f"'{t}'" for t in sorted(EXTERNAL_NUMBER_DOC_TYPES))
    return f"""
    CREATE OR REPLACE FUNCTION {VOUCHER_SERIES_GUARD_FUNCTION}() RETURNS trigger AS $$
    DECLARE
        declared text;
    BEGIN
        IF NEW.next_seq < OLD.next_seq THEN
            RAISE EXCEPTION
                'voucher series: next_seq may never rewind (got % from %)',
                NEW.next_seq, OLD.next_seq;
        END IF;
        IF NEW.next_seq <> OLD.next_seq AND NEW.next_seq <> OLD.next_seq + 1 THEN
            declared := nullif(current_setting('{EXTERNAL_ACCEPT_SETTING}', true), '');
            IF declared IS NULL
                OR OLD.doc_type NOT IN ({external_types})
                OR NEW.next_seq <> declared::bigint + 1
            THEN
                RAISE EXCEPTION
                    'voucher series: next_seq may only advance by one (got % from %)',
                    NEW.next_seq, OLD.next_seq;
            END IF;
        END IF;
        IF OLD.next_seq > 1
            AND OLD.doc_type IN ({external_types})
            AND (NEW.prefix <> OLD.prefix OR NEW.suffix <> OLD.suffix)
        THEN
            RAISE EXCEPTION
                'voucher series: prefix/suffix are frozen on a till-numbered series once '
                'numbering has started; a re-presented sequence must render the same key';
        END IF;
        IF OLD.next_seq > 1 AND (
            NEW.fy <> OLD.fy
            OR NEW.store_code <> OLD.store_code
            OR NEW.doc_type <> OLD.doc_type
        ) THEN
            RAISE EXCEPTION
                'voucher series: fy/store_code/doc_type are frozen once numbering has started';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER {trigger_name}
        BEFORE UPDATE ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION {VOUCHER_SERIES_GUARD_FUNCTION}();
    """


def voucher_series_guard_reverse_sql(table_name: str = "core_voucher_series") -> str:
    """Reverse of `voucher_series_guard_sql` (leaves the shared function)."""
    return f"DROP TRIGGER IF EXISTS {table_name}_guard ON {table_name};"


class DocumentProbe(Document):
    """Kernel-internal concrete document that exercises the skeleton against real
    Postgres — NOT a business document.

    The real booking/GRN/PT/sale arrive with their slices. This exists only so
    the FSM trigger and the gap-free series genuinely run in the DB and the K2
    golden tests have a concrete table to post against. It carries the minimum to
    resolve a series plus a free-text `memo` to prove edit-after-post is refused.

    `external_seq` gives it the till's shape too: set it and the probe posts by
    accepting that number instead of allocating one, which is what lets the
    anti-cheat suite prove exactly-once acceptance against a real table with a
    real unique constraint, before any sale document exists.
    """

    fy = models.CharField(max_length=7)
    store_code = models.CharField(max_length=16)
    doc_type = models.CharField(max_length=16)
    memo = models.CharField(max_length=120, blank=True, default="")
    external_seq = models.IntegerField(null=True, blank=True)

    class Meta(Document.Meta):
        # Subclassing the abstract Meta inherits its constraints (the #3
        # posted-has-number CHECK); Django resets abstract=False automatically.
        db_table = "core_document_probe"

    def series_lookup(self) -> tuple[str, str, str]:
        return self.fy, self.store_code, self.doc_type

    def mint_number(self) -> MintedNumber:
        if self.external_seq is None:
            return super().mint_number()
        fy, store_code, doc_type = self.series_lookup()
        accepted = VoucherSeries.accept_external(
            fy=fy, store_code=store_code, doc_type=doc_type, seq=self.external_seq
        )
        return MintedNumber(
            series=accepted.series, doc_number=accepted.doc_number, accepted=accepted
        )


# The probe moves no stock and keeps no subledger, so its whole footprint is
# whatever it posted to the value GL — the plain mirror, and nothing else.
# Declared at import rather than in an `AppConfig.ready()` because this document
# and its reversal are both kernel, with no service layer in between to cycle
# against.
declare_reversal(DocumentProbe._meta.label_lower, reverse_value_legs)

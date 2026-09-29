"""Store task checklists (store operations PRD ST-OPS-4; ticket 49).

Three tables:

* ``ChecklistTemplate`` - Admin's list of items (opening, closing, a weekly
  display check, the monthly count), how often it is due and by what time. It is
  the chain's: every store where the switch is on gets it. Changing one bumps its
  revision; stopping it clears ``active``. Nothing is deleted: each change's
  before and after values live in its ``AuditEvent``.
* ``ChecklistTick`` - one item ticked at one store for one due day, by whom and
  when, with an optional photo. Written once. ``late`` says it was ticked after
  the list's time had passed.
* ``ChecklistMiss`` - one list at one store for one due day whose time passed
  with items not ticked, as the worker's check found it. Written once; the items
  it names stay missed even if they are ticked late.

The site-readiness checklist (Setup, Organisation, a site's Readiness tab) is a
different thing and is not touched here.

Deliberately not ``TenantOwned``, like the other store-operations tables (petty
cash, cash counts): a deployment serves one tenant (design section 4.2), and a
tick or miss belongs to a store, which carries its tenant. A template is the
deployment's own.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


class ChecklistEvery(models.TextChoices):
    DAY = "day", "Every day"
    WEEK = "week", "Every week"
    MONTH = "month", "Every month"


class ChecklistTemplate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    every = models.CharField(max_length=8, choices=ChecklistEvery.choices)
    #: Which day of the week a weekly list is due: 0 Monday to 6 Sunday.
    weekday = models.SmallIntegerField(null=True, blank=True)
    #: Which day of the month a monthly list is due, 1 to 31; a shorter month
    #: uses its last day.
    day_of_month = models.SmallIntegerField(null=True, blank=True)
    #: The time of day (India) the list is due by; null is the end of the day.
    due_by = models.TimeField(null=True, blank=True)
    #: ``[{"id": "<uuid>", "text": "...", "opens": ""}]``. An item keeps its id
    #: across changes, so a tick always names the item it was for.
    items = models.JSONField(default=list)
    #: When the template was set or last changed. A list whose time had already
    #: passed then is never judged missed: a change must not make a past time late.
    judged_after = models.DateTimeField()
    active = models.BooleanField(default=True)
    revision = models.IntegerField(default=1)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        db_table = "store_checklist_template"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=Q(active=True), name="uq_checklist_template_live_name"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class ChecklistTick(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: The screen's own id for this tick, so a request sent twice saves one row.
    client_id = models.UUIDField(unique=True)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    template = models.ForeignKey(ChecklistTemplate, on_delete=models.PROTECT, related_name="+")
    item_id = models.CharField(max_length=36)
    #: The item's words when it was ticked.
    item_text = models.CharField(max_length=200)
    due_on = models.DateField()
    ticked_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    ticked_at = models.DateTimeField()
    #: Ticked after the list's time had passed.
    late = models.BooleanField(default=False)
    #: The optional photo, in the write-once store (``core.offbox``), never here.
    photo_key = models.CharField(max_length=255, blank=True, default="")
    photo_sha256 = models.CharField(max_length=64, blank=True, default="")
    photo_media_type = models.CharField(max_length=40, blank=True, default="")
    photo_size = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "store_checklist_tick"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "template", "item_id", "due_on"], name="uq_checklist_tick_item_day"
            ),
        ]
        indexes = [models.Index(fields=["store", "due_on"], name="store_cl_tick_store_day_idx")]

    def __str__(self) -> str:
        return f"{self.store_id} {self.item_text} {self.due_on}"

    @property
    def has_photo(self) -> bool:
        return bool(self.photo_key)


class ChecklistMiss(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    template = models.ForeignKey(ChecklistTemplate, on_delete=models.PROTECT, related_name="+")
    due_on = models.DateField()
    #: ``[{"id": "<uuid>", "text": "..."}]``: the items not ticked by the list's time.
    items = models.JSONField(default=list)
    found_at = models.DateTimeField()

    class Meta:
        db_table = "store_checklist_miss"
        ordering = ["-due_on"]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "template", "due_on"], name="uq_checklist_miss_list_day"
            ),
        ]
        indexes = [models.Index(fields=["store", "due_on"], name="store_cl_miss_store_day_idx")]

    def __str__(self) -> str:
        return f"{self.store_id} {self.template_id} {self.due_on}"

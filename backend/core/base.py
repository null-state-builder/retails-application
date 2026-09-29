"""Shared, abstract base models for the kernel (`core`).

`TimeStampedModel` carries actor-agnostic insert/update stamps for ordinary
master/config rows. The append-only ledger and the document FSM live in
`core.ledger` / `core.documents`; this is the plain mutable-master base every
master and config table reuses (UTC stored, IST shown — ADR-0004).
"""

from __future__ import annotations

from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base adding `created_at` / `updated_at` to a model."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

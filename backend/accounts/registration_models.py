"""Installation claim before tenant creation; never a source of business grants.

This deliberately has no tenant wall while pending: there is no tenant yet.
Only the deployment-bound registration service may read or write it. Password
hashes are temporary, private server data and are erased after the atomic claim.
"""

from __future__ import annotations

import uuid

from django.db import models


class InstallationRegistration(models.Model):
    deployment_key = models.UUIDField(primary_key=True, editable=False)
    command_id = models.UUIDField(default=uuid.uuid4)
    request_fingerprint = models.CharField(max_length=64)
    summary = models.JSONField()
    summary_hash = models.CharField(max_length=64)
    revision = models.PositiveIntegerField(default=1)
    owner_password_hash = models.CharField(max_length=256)
    admin_password_hash = models.CharField(max_length=256)
    owner_confirmed_at = models.DateTimeField(null=True, blank=True)
    admin_confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmation_history = models.JSONField(default=list)
    tenant = models.OneToOneField(
        "masters.Tenant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="registration",
    )
    first_store_id = models.BigIntegerField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

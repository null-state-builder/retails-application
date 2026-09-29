from __future__ import annotations

from typing import Any

from django.contrib.auth.models import BaseUserManager


class UserManager(BaseUserManager[Any]):
    """Username-based manager (no email required — English-only internal ERP)."""

    use_in_migrations = True

    def _create_user(self, username: str, password: str | None, **extra: Any) -> Any:
        if not username:
            raise ValueError("A username is required")
        user = self.model(username=username, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, username: str, password: str | None = None, **extra: Any) -> Any:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(username, password, **extra)

    def create_superuser(self, username: str, password: str | None = None, **extra: Any) -> Any:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("scope_type", "all")
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(username, password, **extra)

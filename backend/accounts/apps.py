from __future__ import annotations

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "Users, Roles & Access"

    def ready(self) -> None:
        from accounts.access_changes import apply_access_change
        from accounts.models import AccessChange, User
        from approvals.hooks import register_on_approved

        # `OnApproved.__call__` is `(subject: Any, *, actor: Any) -> None` -
        # structurally compatible with any real signature, but mypy's callable
        # matching also requires the positional-or-keyword parameter *name* to
        # line up, and `apply_access_change`'s first parameter is named
        # `change` (its natural name, used throughout its body) rather than
        # `subject`. Wrapping rather than renaming keeps that function's
        # signature - and every internal reference to `change` - untouched.
        def _on_access_change_approved(subject: AccessChange, *, actor: User) -> None:
            apply_access_change(subject, actor=actor)

        register_on_approved(AccessChange, _on_access_change_approved)

        from accounts.goods_admin_services import open_privileged_follow_up
        from core.commands import register_success_follow_up

        register_success_follow_up(open_privileged_follow_up)

"""Who may edit master data (ADR-0003 / Rule 12).

Lifted out of ``masters.views`` so the *vendor* master — which lives in the
``vendors`` app because a vendor carries bookings — can be gated by the same
rule as stores, brands, seasons and GSTINs. Before this, ``/api/vendors``
answered any authenticated caller on POST: a store cashier could mint a supplier
that every future booking, GRN and payable then hangs off.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from accounts.actor_policies import user_may_act

MASTER_WRITES = "masters.writes"


class IsMasterSteward(BasePermission):
    """Read-open, write-gated to master-data stewards.

    Reads stay open because every screen that *uses* a master needs to list it —
    the booking form needs vendors, the receive form needs stores. Only the
    edit is a stewardship act.
    """

    message = "Master-data steward role required (owner / IT admin / data steward)."

    def has_permission(self, request: Request, view: Any) -> bool:
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return user_may_act(user, MASTER_WRITES)

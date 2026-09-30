"""
Role-based access control for the billing module.

INTEGRATION NOTE: EDABIP's existing backend already has JWT auth and a
multi-tenant RBAC system (per prior sessions). Replace the two imports
below with your real ones and delete the `_DemoUser` fallback:

    from app.auth.dependencies import get_current_user   # your real dependency
    from app.models.user import User                      # your real User model

The only assumptions this file makes about `User` are that it exposes
`.id`, `.org_id`, and `.role` (a string). Adjust `_role_of()` if your
role field is named or shaped differently (e.g. a list of role objects).

Roles, straight from the wireframe spec ("Users" section, page 2):
  - Administrator          -> full access
  - Billing Administrator   -> full access (spec explicitly names this
                                 role for Upgrade/Renew: "Billing
                                 Administrator / Account Owner")
  - Finance Manager         -> view + export, no plan/payment mutations
  - Client User             -> view only
"""
from enum import Enum
from typing import Callable

from fastapi import Depends, HTTPException, status

try:
    from app.auth.dependencies import get_current_user  # type: ignore
    from app.models.user import User  # type: ignore
except ImportError:
    # ---- Fallback used only until the real imports above are wired in ----
    from dataclasses import dataclass

    @dataclass
    class User:  # minimal stand-in matching the real model's shape
        id: int
        org_id: int
        role: str

    def get_current_user() -> User:  # pragma: no cover - replaced in real app
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Billing module is using its placeholder auth dependency. "
                "Wire app/permissions.py's imports to your real "
                "get_current_user / User before deploying."
            ),
        )


UNAUTHORIZED_MESSAGE = "You do not have permission to perform this action."


class BillingPermission(str, Enum):
    VIEW = "billing:view"
    MANAGE = "billing:manage"      # upgrade / renew / cancel plan, manage payment methods
    EXPORT = "billing:export"


ROLE_PERMISSIONS: dict[str, set[BillingPermission]] = {
    "administrator": {BillingPermission.VIEW, BillingPermission.MANAGE, BillingPermission.EXPORT},
    "billing_administrator": {BillingPermission.VIEW, BillingPermission.MANAGE, BillingPermission.EXPORT},
    "finance_manager": {BillingPermission.VIEW, BillingPermission.EXPORT},
    "client_user": {BillingPermission.VIEW},
}


def _role_of(user: "User") -> str:
    return str(getattr(user, "role", "client_user")).lower().replace(" ", "_")


def require_billing_permission(permission: BillingPermission) -> Callable:
    """FastAPI dependency factory - use as:
    `current_user = Depends(require_billing_permission(BillingPermission.MANAGE))`
    """

    def dependency(current_user: "User" = Depends(get_current_user)) -> "User":
        allowed = ROLE_PERMISSIONS.get(_role_of(current_user), set())
        if permission not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=UNAUTHORIZED_MESSAGE)
        return current_user

    return dependency


# Convenience dependencies for the common cases used throughout router.py
require_view = require_billing_permission(BillingPermission.VIEW)
require_manage = require_billing_permission(BillingPermission.MANAGE)
require_export = require_billing_permission(BillingPermission.EXPORT)

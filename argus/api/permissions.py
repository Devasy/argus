"""Named permissions on top of roles.

Routes ask for a permission ("audit.approve"), never a role, so adding a role or
real per-user identity (GitLab OAuth in resolve_principal) changes this table
only -- not every call site."""
from fastapi import HTTPException, Request

from argus.api.auth import ROLE_RANK, Principal, Role, resolve_principal

PERMISSIONS: dict[str, Role] = {
    "learnings.read": "user",
    "audit.read": "user",
    "audit.run": "admin",
    "audit.approve": "admin",
}


def permissions_for(role: Role) -> list[str]:
    return sorted(p for p, need in PERMISSIONS.items() if ROLE_RANK[role] >= ROLE_RANK[need])


def require_permission(name: str):
    need = PERMISSIONS[name]

    def _dependency(request: Request) -> Principal:
        principal = resolve_principal(request)
        if ROLE_RANK[principal.role] < ROLE_RANK[need]:
            raise HTTPException(status_code=403,
                                detail=f"'{name}' requires the '{need}' role")
        return principal

    return _dependency

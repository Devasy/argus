"""Role-based access, built to be identity-agnostic on purpose.

There is no real per-user identity yet -- that's GitLab OAuth, deliberately
deferred. Until then, a `Principal` is resolved from a static bearer token
(ARGUS_API_TOKEN -> "user", ARGUS_ADMIN_TOKEN -> "admin"), but every route only
ever depends on `require_role`, never on how a Principal was resolved. When
OAuth lands, `resolve_principal` is the only function that changes: it swaps
"compare against a configured secret" for "look up the session's actor and
its role", and every `require_role(...)` call site -- backend routes, the
`/me` endpoint the frontend already reads its role from -- keeps working
unmodified.
"""
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request

from argus.config import get_settings

Role = Literal["user", "admin"]

# Higher ranks satisfy every requirement a lower rank does (admin can do
# anything user can). A third role slots in here with its own rank; nothing
# else about require_role needs to change.
ROLE_RANK: dict[Role, int] = {"user": 0, "admin": 1}


@dataclass(frozen=True)
class Principal:
    role: Role
    # Human-readable only -- "anonymous"/"user-token"/"admin-token" today.
    # Becomes the actual username once identity is real; nothing downstream
    # should parse or rely on its current values.
    label: str


def resolve_principal(request: Request) -> Principal:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip()

    if not token:
        # No credential presented. Anonymous is only ever "user" rank --
        # an unset ARGUS_API_TOKEN means "no auth configured for local dev",
        # not "no auth configured, so grant admin too".
        if not settings.api_token:
            return Principal(role="user", label="anonymous")
        raise HTTPException(status_code=401, detail="invalid or missing token")

    token_b = token.encode("utf-8")
    if settings.admin_token and secrets.compare_digest(
            token_b, settings.admin_token.encode("utf-8")):
        return Principal(role="admin", label="admin-token")
    if settings.api_token and secrets.compare_digest(
            token_b, settings.api_token.encode("utf-8")):
        return Principal(role="user", label="user-token")

    # A token WAS presented and matched nothing configured -- always invalid,
    # even if ARGUS_API_TOKEN happens to be unset. Silently downgrading a wrong
    # credential to anonymous would hide a real misconfiguration.
    raise HTTPException(status_code=401, detail="invalid or missing token")


def require_role(min_role: Role):
    """FastAPI dependency factory: `Depends(require_role("admin"))`."""

    def _dependency(request: Request) -> Principal:
        principal = resolve_principal(request)
        if ROLE_RANK[principal.role] < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"requires '{min_role}' role, this token is '{principal.role}'")
        return principal

    return _dependency

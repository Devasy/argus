import secrets

from fastapi import HTTPException, Request

from argus.config import get_settings


def require_token(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    if not settings.api_token:
        return "anonymous"
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, settings.api_token):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    return token

"""API-key authentication.

Write endpoints (restart, chaos, acknowledge, generate report) always need the
key, sent as `X-API-Key: <key>` or `Authorization: Bearer <key>`. Read endpoints
are open unless OPS_PROTECT_READS=true. The Alertmanager webhook uses its own
token so the main key never has to be written into Alertmanager's config.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, Request, status


def _presented(x_api_key: str | None, authorization: str | None) -> str:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def require_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    expected = request.app.state.platform.settings.effective_api_key()
    if not secrets.compare_digest(_presented(x_api_key, authorization), expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"}
        )
    return "api-key"


def read_access(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    if request.app.state.platform.settings.protect_reads:
        require_key(request, x_api_key, authorization)


def require_webhook_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    settings = request.app.state.platform.settings
    expected = settings.webhook_token or settings.effective_api_key()
    if not secrets.compare_digest(_presented(x_api_key, authorization), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook token")


def operator(x_operator: str | None = Header(default=None)) -> str:
    """Who is acting, for the audit trail. Free text; the API key is what authorizes."""
    return (x_operator or "operator").strip()[:64]


WriteAccess = Depends(require_key)
ReadAccess = Depends(read_access)

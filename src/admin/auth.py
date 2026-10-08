"""Signed httpOnly cookie for the /admin browser session.

Secure is on for HTTPS (Render sets X-Forwarded-Proto) and off on local HTTP.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status

from src.core.config import settings

COOKIE_NAME = "sw_admin_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 12
_COOKIE_PATH = "/"


def cookie_secure(request: Request) -> bool:
    """True on HTTPS and on Render's TLS-terminated HTTP origin."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"


def credentials_match(username: str, password: str) -> bool:
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        return False
    return _secret_equals(username, settings.ADMIN_USERNAME) and _secret_equals(
        password, settings.ADMIN_PASSWORD
    )


def mint_session_token() -> str:
    if not settings.ADMIN_SESSION_SECRET:
        raise RuntimeError("ADMIN_SESSION_SECRET is not configured")
    exp = str(int(time.time()) + SESSION_MAX_AGE_SECONDS)
    return f"{exp}.{_sign(exp)}"


def session_is_valid(token: str | None) -> bool:
    if not token or not settings.ADMIN_SESSION_SECRET or "." not in token:
        return False
    exp, _, sig = token.partition(".")
    if not exp.isdigit() or not hmac.compare_digest(sig, _sign(exp)):
        return False
    return int(exp) >= int(time.time())


def is_logged_in(request: Request) -> bool:
    return session_is_valid(request.cookies.get(COOKIE_NAME))


def set_session_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=mint_session_token(),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        path=_COOKIE_PATH,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path=_COOKIE_PATH)


def require_admin_session(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin login required",
        )


AdminSession = Annotated[None, Depends(require_admin_session)]


def _sign(message: str) -> str:
    return hmac.new(
        settings.ADMIN_SESSION_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _secret_equals(given: str, expected: str) -> bool:
    given_b = given.encode("utf-8")
    expected_b = expected.encode("utf-8")
    if len(given_b) != len(expected_b):
        hmac.compare_digest(expected_b, expected_b)
        return False
    return hmac.compare_digest(given_b, expected_b)

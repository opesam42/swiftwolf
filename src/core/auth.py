"""Shared-API-key auth between SwiftWolf and the bank app — per the System
Design doc's Section 2: "shared API key in a header (X-SwiftWolf-Key),
validated on every SwiftWolf request." Not OAuth, not mTLS — a named,
deliberate hackathon scope tradeoff, not an oversight.
"""
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from src.core.config import settings

API_KEY_NAME = "X-SwiftWolf-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    # Fail closed: an unconfigured SWIFTWOLF_API_KEY must reject every
    # request, never silently accept any key — an empty expected value is a
    # server misconfiguration, not "auth disabled."
    expected_api_key = settings.SWIFTWOLF_API_KEY

    if not expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key not configured on server",
        )

    if not api_key or api_key != expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-SwiftWolf-Key",
        )

    return api_key
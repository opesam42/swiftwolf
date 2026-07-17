"""Shared-API-key auth between SwiftWolf and the bank app — per the System
Design doc's Section 2: "shared API key in a header (X-SwiftWolf-Key),
validated on every SwiftWolf request." Not OAuth, not mTLS — a named,
deliberate hackathon scope tradeoff, not an oversight.
"""
from fastapi import Header, HTTPException, status

from src.config import settings


async def verify_api_key(x_swiftwolf_key: str = Header(...)) -> None:
    # Fail closed: an unconfigured SWIFTWOLF_API_KEY must reject every request,
    # never silently accept any key — an empty expected value is a server
    # misconfiguration, not "auth disabled."
    if not settings.SWIFTWOLF_API_KEY or x_swiftwolf_key != settings.SWIFTWOLF_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-SwiftWolf-Key",
        )

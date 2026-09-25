from fastapi import APIRouter, Depends, HTTPException

from src.core.auth import verify_api_key
from src.core.database import SessionDep
from src.profile.schemas import BaselineResponse, FrictionProfileResponse
from src.profile.services import CustomerProfileService
from src.core.redis import RedisDep

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])


@router.get("/customers/{customer_id}/baseline", response_model=BaselineResponse)
async def get_customer_baseline(customer_id: str, db: SessionDep, redis_client: RedisDep):
    service = CustomerProfileService(db, redis_client)
    baseline = service.get_cached_baseline(customer_id)
    if not baseline:
        raise HTTPException(status_code=404, detail="Customer baseline not found")

    return BaselineResponse(
        customer_id=customer_id,
        risk_tier=baseline["risk_tier"],
        is_cold_start=baseline["is_cold_start"],
        known_destinations_count=len(baseline["known_destinations"]),
        known_banks_count=len(baseline["known_bank_codes"]),
    )


@router.get("/insights/friction-profile/{customer_id}", response_model=FrictionProfileResponse)
async def get_friction_profile(customer_id: str, db: SessionDep, redis_client: RedisDep):
    service = CustomerProfileService(db, redis_client)
    baseline = service.get_cached_baseline(customer_id) or {}

    risk_tier = baseline.get("risk_tier", "standard")
    recommended = "STEP_UP" if risk_tier == "elevated" else "ALLOW"

    return FrictionProfileResponse(
        customer_id=customer_id,
        recommended_friction=recommended,
        risk_tier=risk_tier,
        reasons=["Elevated risk tier"] if risk_tier == "elevated" else ["Normal behavior"],
    )
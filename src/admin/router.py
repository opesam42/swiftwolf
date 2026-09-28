from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlmodel import Session

from src.admin.services import BeneficiaryExportService, OnboardingService
from src.core.config import settings
from src.core.database import SessionDep
from src.core.redis import RedisDep

admin_key_header = APIKeyHeader(name="X-SwiftWolf-Admin-Key", auto_error=True)


def verify_admin_api_key(api_key: str = Security(admin_key_header)):
    """Dedicated security verification for internal administrative routes."""
    if api_key != getattr(settings, "ADMIN_API_KEY", settings.SWIFTWOLF_API_KEY):
        raise HTTPException(status_code=403, detail="Forbidden: Invalid Admin Credentials")
    return api_key


router = APIRouter(prefix="/v1/internal", dependencies=[Depends(verify_admin_api_key)])


@router.get("/beneficiaries")
async def export_beneficiaries_endpoint(db: SessionDep):
    """Admin endpoint to export blacklisted beneficiary records for audit."""
    data = BeneficiaryExportService(db).export_beneficiaries()
    return {"status": "success", "count": len(data), "data": data}


@router.post("/customers/{customer_id}/seed")
async def seed_customer_endpoint(customer_id: str, db: SessionDep, redis_client: RedisDep, risk_tier: str = "standard"):
    """Admin endpoint to initialize or re-seed customer baseline profiles."""
    customer = OnboardingService(db, redis_client).seed_demo_customer(customer_id, risk_tier)
    return {"status": "seeded", "customer_id": customer.customer_id, "risk_tier": customer.risk_tier}
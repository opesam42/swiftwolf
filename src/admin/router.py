import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Security, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from fastapi.templating import Jinja2Templates

from src.admin.auth import (
    AdminSession,
    clear_session_cookie,
    credentials_match,
    is_logged_in,
    set_session_cookie,
)
from src.admin.services import BeneficiaryExportService, DashboardService, OnboardingService
from src.core.config import settings
from src.core.database import SessionDep
from src.core.redis import RedisDep
from src.settlement.models import TransactionType

CATEGORY_CHOICES = [(item.value, item.value.replace("_", " ").title()) for item in TransactionType]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DEFAULT_DASHBOARD_CUSTOMER_ID = "gbenga_opeyemi"

admin_key_header = APIKeyHeader(
    name="X-SwiftWolf-Admin-Key",
    scheme_name="X-SwiftWolf-Admin-Key",
    auto_error=True,
)


def verify_admin_api_key(api_key: str = Security(admin_key_header)):
    """Dedicated security verification for internal administrative routes."""
    expected = settings.ADMIN_API_KEY or settings.SWIFTWOLF_API_KEY
    if api_key != expected:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid Admin Credentials")
    return api_key


router = APIRouter(prefix="/v1/internal", dependencies=[Depends(verify_admin_api_key)])
pages_router = APIRouter()


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


@pages_router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_page(
    request: Request,
    customer_id: str = DEFAULT_DASHBOARD_CUSTOMER_ID,
    category: str = TransactionType.TRANSFER.value,
):
    if category not in {item.value for item in TransactionType}:
        category = TransactionType.TRANSFER.value
    if not is_logged_in(request):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": None, "customer_id": customer_id},
        )
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "customer_id": customer_id or DEFAULT_DASHBOARD_CUSTOMER_ID,
            "category": category,
            "categories": CATEGORY_CHOICES,
        },
    )


@pages_router.post("/admin/login")
async def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    customer_id: str = Form(DEFAULT_DASHBOARD_CUSTOMER_ID),
):
    if not credentials_match(username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password", "customer_id": customer_id},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    target = customer_id.strip() or DEFAULT_DASHBOARD_CUSTOMER_ID
    response = RedirectResponse(
        url=f"/admin?customer_id={target}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_session_cookie(response, request)
    return response


@pages_router.post("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    return response


@pages_router.get("/admin/api/customers/{customer_id}/control-chart")
async def admin_control_chart(
    customer_id: str,
    db: SessionDep,
    _: AdminSession,
    category: str = TransactionType.TRANSFER.value,
):
    started = time.perf_counter()
    try:
        payload = DashboardService(db).control_chart(customer_id, category)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    payload["latency_ms"] = max(1, round((time.perf_counter() - started) * 1000))
    return payload

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from src.core.auth import verify_api_key
from src.core.schemas import (
    FrictionProfileResponse,
    ScoreRequest,
    ScoreResponse,
    SettleRequest,
    SettleResponse,
    SpendingDeltaResponse,
)
from src.core.services import BeneficiaryExportService, InsightsService, ScoreService, SettleService
from src.database import SessionDep
from src.redis import RedisDep

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])


def _run_settle_background(transaction_reference: str, final_status: str, verification_outcome: str) -> None:
    """The background-task body FastAPI runs after the settle response is
    already sent. Deliberately opens its OWN Session/Redis client rather than
    reusing the request-scoped SessionDep — sidesteps any ambiguity about
    exactly when yield-dependencies tear down relative to background tasks,
    and matches the same "own session" pattern the seed jobs already use."""
    from sqlmodel import Session

    from src.database import engine
    from src.redis import get_redis_client

    with Session(engine) as db:
        SettleService(db, get_redis_client()).run_layer2(transaction_reference, final_status, verification_outcome)


@router.post("/score", response_model=ScoreResponse)
async def score_transaction_endpoint(request: ScoreRequest, db: SessionDep, redis_client: RedisDep):
    """Called synchronously by the bank app before it calls NIBSS — the <50ms
    hot path. Read-only towards CustomerProfile/River: this endpoint never
    updates behavioral state, only reads the cached baseline and records the
    decision. The River/anomaly update happens later, in the background task
    on /v1/transactions/settle."""
    transaction = {
        "transaction_reference": request.transaction_reference,
        "customer_id": request.customer_id,
        "beneficiary_account": request.beneficiary_account,
        "beneficiary_bank_code": request.beneficiary_bank_code,
        "beneficiary_name": request.beneficiary_name,
        "amount": request.amount,
        "timestamp": request.timestamp,
        "last_transaction_timestamp": request.last_transaction_timestamp,
        "transaction_type": request.transaction_type,
        "medium": request.medium,
        "geolocation": request.geolocation.model_dump() if request.geolocation else None,
        "session": request.session.model_dump() if request.session else None,
    }

    result = ScoreService(db, redis_client).score(transaction)
    return ScoreResponse(**result)


@router.post("/transactions/settle", response_model=SettleResponse)
async def settle_transaction_endpoint(request: SettleRequest, background_tasks: BackgroundTasks, db: SessionDep):
    """Fire-and-forget from the bank app's perspective: durably records the
    real-world outcome synchronously (fast), then schedules the slower Layer 2
    work (River baseline update + anomaly scoring) as a background task so the
    response doesn't wait on it."""
    settle_service = SettleService(db)

    txn = settle_service.get_transaction(request.transaction_reference)
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown transaction_reference: {request.transaction_reference!r} (no prior /v1/score for it)",
        )

    if txn.final_status is not None:
        # Idempotent retry — already settled once, don't re-apply or re-schedule.
        return SettleResponse(transaction_reference=request.transaction_reference, status="already_processed")

    settle_service.apply_settlement(
        txn, request.final_status, request.verification_outcome, request.nibss_reference
    )
    background_tasks.add_task(
        _run_settle_background, request.transaction_reference, request.final_status, request.verification_outcome
    )

    return SettleResponse(transaction_reference=request.transaction_reference, status="accepted")


@router.get("/internal/beneficiaries")
async def export_beneficiaries(db: SessionDep):
    """Live replacement for manually handing Praise a CSV: he hits this (same
    X-SwiftWolf-Key auth as every other endpoint on this router) to get the
    current distinct beneficiary set — straight from real Transaction rows,
    never stale — and upserts it into his own beneficiaries table for Path A's
    instant-lookup demo path.

    Reuses the same API key as /v1/score and /v1/transactions/settle rather
    than a separate internal-only key — a deliberate hackathon scope choice
    (bulk export vs. per-transaction scoring are different access shapes in
    principle), not an oversight."""
    service = BeneficiaryExportService(db)
    beneficiaries = service.get_distinct_beneficiaries()
    return {"beneficiaries": beneficiaries, "count": len(beneficiaries)}


@router.get("/insights/friction-profile/{customer_id}", response_model=FrictionProfileResponse)
async def get_friction_profile(customer_id: str, db: SessionDep):
    """Layer 3, Tier 1 — Adaptive UX Friction. Aggregates a customer's last 20
    risk_events to characterize how much friction they typically encounter.
    The bank app uses this for UI treatment (e.g. skip an extra confirmation
    tap on a clean PROCEED for a low-friction customer) — never a live scoring
    decision, that's still entirely /v1/score's job."""
    result = InsightsService(db).get_friction_profile(customer_id)
    return FrictionProfileResponse(**result)


@router.get("/insights/spending-delta/{customer_id}", response_model=SpendingDeltaResponse)
async def get_spending_delta(customer_id: str, db: SessionDep):
    """Layer 3, Tier 2 — Deviation Tracking. Compares this week's actual spend
    per category against the customer's own category baseline average, reusing
    CustomerProfile.get_amount_baseline() directly rather than any new
    statistical logic."""
    result = InsightsService(db).get_spending_delta(customer_id)
    return SpendingDeltaResponse(**result)



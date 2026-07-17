from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import select

from src.core.auth import verify_api_key
from src.core.models import Transaction
from src.core.schemas import ScoreRequest, ScoreResponse, SettleRequest, SettleResponse
from src.core.services import ScoreService, SettleService
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

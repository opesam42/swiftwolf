from fastapi import APIRouter, Depends

from src.core.auth import verify_api_key
from src.core.schemas import ScoreRequest, ScoreResponse
from src.core.services import ScoreService
from src.database import SessionDep
from src.redis import RedisDep

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])


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

from fastapi import APIRouter, Depends

from src.core.auth import verify_api_key
from src.core.database import SessionDep
from src.core.redis import RedisDep
from src.scoring.schemas import ScoreRequest, ScoreResponse
from src.scoring.services import ScoreService

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])


@router.post("/score", response_model=ScoreResponse)
async def score_transaction_endpoint(
    request: ScoreRequest,
    db: SessionDep,
    redis_client: RedisDep,
):
    """Called synchronously by the bank app before calling NIBSS (<50ms SLA)."""
    transaction = {
        "transaction_reference": request.transaction_reference,
        "customer_id": request.customer_id,
        "provider": request.provider,
        "recipient": request.recipient,
        "amount": request.amount,
        "timestamp": request.timestamp,
        "last_transaction_timestamp": request.last_transaction_timestamp,
        "transaction_type": request.transaction_type.value,
        "medium": request.medium,
        "geolocation": request.geolocation.model_dump() if request.geolocation else None,
        "session": request.session.model_dump() if request.session else None,
    }

    result = ScoreService(db, redis_client).score(transaction)
    return ScoreResponse(**result)
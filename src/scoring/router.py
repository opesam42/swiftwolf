import logging

from fastapi import APIRouter, Depends

from src.core.auth import verify_api_key
from src.core.database import SessionDep
from src.core.redis import RedisDep
from src.scoring.schemas import ScoreRequest, ScoreResponse
from src.scoring.services import ScoreService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])


@router.post("/score", response_model=ScoreResponse)
async def score_transaction_endpoint(
    request: ScoreRequest,
    db: SessionDep,
    redis_client: RedisDep,
):
    """Scores a payment before the bank sends it to NIBSS, returning PROCEED, STEP_UP or BLOCK
    with the reasons. Repeating a transaction_reference returns the original decision."""
    transaction = {
        "transaction_reference": request.transaction_reference,
        "customer_id": request.customer_id,
        "provider": request.provider,
        "recipient": request.recipient,
        "amount": request.amount,
        "timestamp": request.timestamp,
        "last_transaction_timestamp": request.last_transaction_timestamp,
        "transaction_type": request.transaction_type.value,
        "medium": request.medium.value,
        "geolocation": request.geolocation.model_dump() if request.geolocation else None,
        "session": request.session.model_dump() if request.session else None,
        "behavioural_biometrics": request.behavioural_biometrics.model_dump() if request.behavioural_biometrics else None,
    }

    result = ScoreService(db, redis_client).score(transaction)
    decision = result["decision"]
    decision_value = decision.value if hasattr(decision, "value") else decision
    reasons = result.get("reasons") or []
    reason_values = [reason.value if hasattr(reason, "value") else reason for reason in reasons]
    logger.info(
        "score ref=%s customer=%s decision=%s score=%s",
        request.transaction_reference,
        request.customer_id,
        decision_value,
        result["score"],
        extra={
            "transaction_reference": request.transaction_reference,
            "customer_id": request.customer_id,
            "amount_kobo": request.amount,
            "transaction_type": request.transaction_type.value,
            "medium": request.medium.value,
            "provider": request.provider,
            "decision": decision_value,
            "score": result["score"],
            "reasons": reason_values,
        },
    )
    return ScoreResponse(**result)
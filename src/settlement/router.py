import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from src.core.auth import verify_api_key
from src.core.errors import TransactionNotFoundError, InvalidSettlementData
from src.core.database import SessionDep
from src.core.redis import RedisDep
from src.settlement.schemas import SettleRequest, SettleResponse
from src.settlement.services import SettleService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/transactions", dependencies=[Depends(verify_api_key)])


@router.post("/settle", response_model=SettleResponse)
async def settle_transaction_endpoint(
    request: SettleRequest,
    db: SessionDep,
    redis_client: RedisDep,
):
    """
    Reports a scored payment's final outcome (SUCCESS or FAILED) and, after a step-up,
    which verification the bank used and how it ended. A SUCCESS updates the customer's
    behavioural baseline before responding. Unscored references return 404; retries are safe.
    """
    payload = request.model_dump()
    try:
        result = SettleService(db, redis_client).settle(payload)
    except TransactionNotFoundError as e:
        logger.warning("settle_not_found", extra={"transaction_reference": request.transaction_reference})
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidSettlementData as e:
        logger.warning(
            "settle_invalid",
            extra={"transaction_reference": request.transaction_reference, "detail": str(e)},
        )
        raise HTTPException(status_code=422, detail=str(e))
    txn_status = result["status"]
    txn_status_value = txn_status.value if hasattr(txn_status, "value") else txn_status
    logger.info(
        "settle ref=%s status=%s is_settled=%s",
        request.transaction_reference,
        txn_status_value,
        result["is_settled"],
        extra={
            "transaction_reference": request.transaction_reference,
            "txn_status": txn_status_value,
            "is_settled": result["is_settled"],
            "final_status": request.status,
            "verification_method": (
                request.verification_method.value if request.verification_method is not None else None
            ),
            "verification_outcome": (
                request.verification_outcome.value if request.verification_outcome is not None else None
            ),
        },
    )
    return SettleResponse(**result)
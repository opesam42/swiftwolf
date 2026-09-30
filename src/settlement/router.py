from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from src.core.auth import verify_api_key
from src.core.errors import TransactionNotFoundError, InvalidSettlementData
from src.core.database import SessionDep
from src.core.redis import RedisDep
from src.settlement.schemas import SettleRequest, SettleResponse
from src.settlement.services import SettleService

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
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidSettlementData as e:
        raise HTTPException(status_code=422, detail=str(e))
    return SettleResponse(**result)
from fastapi import APIRouter, BackgroundTasks, Depends

from src.core.auth import verify_api_key
from src.core.database import SessionDep
from src.core.redis import RedisDep
from src.settlement.schemas import SettleRequest, SettleResponse
from src.settlement.services import SettleService

router = APIRouter(prefix="/v1/transactions", dependencies=[Depends(verify_api_key)])


@router.post("/settle", response_model=SettleResponse)
async def settle_transaction_endpoint(
    request: SettleRequest,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    redis_client: RedisDep,
):
    """
    Called asynchronously post-settlement.
    Marks transaction as settled and queues River ML state updates.
    """
    payload = request.model_dump()
    result = SettleService(db, redis_client).settle(payload, background_tasks)
    return SettleResponse(**result)
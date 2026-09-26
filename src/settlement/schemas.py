from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from src.settlement.models import TransactionStatus


class SettleRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_reference: str
    customer_id: str
    amount: int = Field(gt=0, strict=True, description="Amount in integer kobo (₦1 = 100 kobo)")
    transaction_type: str
    medium: str
    settled_at: datetime
    status: Literal["SUCCESS", "FAILED"] = "SUCCESS"


class SettleResponse(BaseModel):
    transaction_reference: str
    status: TransactionStatus
    is_settled: bool
    message: str
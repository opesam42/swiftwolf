from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class SettleRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_reference: str
    customer_id: str
    amount: float
    beneficiary_account: str
    beneficiary_bank_code: str
    beneficiary_name: Optional[str] = None
    transaction_type: str
    medium: str
    settled_at: datetime
    status: Literal["SUCCESS", "FAILED"] = "SUCCESS"


class SettleResponse(BaseModel):
    transaction_reference: str
    status: str
    is_settled: bool
    message: str
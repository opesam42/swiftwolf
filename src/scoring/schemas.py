
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from datetime import datetime

from src.settlement.models import TransactionChannel, TransactionType
from src.scoring.models import Decision, RiskReason


class Geolocation(BaseModel):
    lat: float
    lng: float

class SessionData(BaseModel):
    login_to_transfer_seconds: float
    pasted_beneficiary: bool

class ScoreRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_reference: str
    customer_id: str
    # provider: bank code for transfers, telecom network for airtime/data, DISCO for electricity, etc.
    provider: str
    # recipient: NUBAN account number, phone number, meter number, etc.
    recipient: str
    amount: int = Field(gt=0, strict=True, description="Amount in integer kobo (₦1 = 100 kobo), e.g. 1500050 for ₦15,000.50")
    timestamp: datetime
    last_transaction_timestamp: Optional[datetime] = None  # that catches dormant account
    transaction_type: TransactionType
    medium: TransactionChannel
    geolocation: Optional[Geolocation] = None
    session: Optional[SessionData] = None


class ScoreResponse(BaseModel):
    transaction_reference: str
    score: int
    decision: Decision
    reasons: list[RiskReason] = Field(default_factory=list)

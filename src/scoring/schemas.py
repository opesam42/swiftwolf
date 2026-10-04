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

class BehaviouralBiometrics(BaseModel):
    dwell_time_ms: float
    flight_time_ms: float
    time_to_first_keystroke_ms: float
    backspace_count: float

class ScoreRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Lengths match the DB columns, so an over-long value is a 422 here rather than a DB error (500)
    transaction_reference: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    # provider: bank code for transfers, telecom network for airtime/data, DISCO for electricity, etc.
    provider: str = Field(min_length=1, max_length=30)
    # recipient: NUBAN account number, phone number, meter number, etc.
    # 50 keeps destination_key ("{category}:{provider}:{recipient}", varchar(100)) in bounds
    recipient: str = Field(min_length=1, max_length=50)
    amount: int = Field(gt=0, strict=True, description="Amount in integer kobo (₦1 = 100 kobo), e.g. 1500050 for ₦15,000.50")
    timestamp: datetime
    last_transaction_timestamp: Optional[datetime] = None  # that catches dormant account
    transaction_type: TransactionType
    medium: TransactionChannel
    geolocation: Optional[Geolocation] = None
    session: Optional[SessionData] = None
    behavioural_biometrics: Optional[BehaviouralBiometrics] = None


class ScoreResponse(BaseModel):
    transaction_reference: str
    score: int
    decision: Decision
    reasons: list[RiskReason] = Field(default_factory=list)

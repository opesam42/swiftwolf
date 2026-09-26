
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, Optional
from datetime import datetime

from src.settlement.models import TransactionType


class Geolocation(BaseModel):
    lat: float
    lng: float

class SessionData(BaseModel):
    login_to_transfer_seconds: float
    active_call_detected: bool
    pasted_beneficiary: bool

class ScoreRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_reference: str
    customer_id: str
    new_beneficiary: bool
    # provider: bank code for transfers, telecom network for airtime/data, DISCO for electricity, etc.
    provider: str
    # recipient: NUBAN account number, phone number, meter number, etc.
    recipient: str
    amount: int = Field(gt=0, strict=True, description="Amount in integer kobo (₦1 = 100 kobo), e.g. 1500050 for ₦15,000.50")
    timestamp: datetime
    last_transaction_timestamp: Optional[datetime] = None  # that catches dormant account
    transaction_type: TransactionType
    medium: Literal["app", "ussd", "atm_withdrawal", "online_payment"]
    geolocation: Optional[Geolocation] = None
    session: Optional[SessionData] = None


class ScoreResponse(BaseModel):
    transaction_reference: str
    score: int
    decision: Literal["PROCEED", "STEP_UP_LIGHT", "STEP_UP_LIVENESS", "BLOCK"]
    step_up_method: Optional[Literal["bvn_liveness", "security_question", "otp"]] = None
    reasons: list[
        Literal[
            "blacklisted_account",
            "elevated_risk_tier",
            "new_beneficiary",
            "new_bank",
            "amount_deviation",
            "unusual_hour",
            "bot_speed_timing",
            "active_call",
            "pasted_new_beneficiary",
            "dormant_account_spike",
            "location_deviation_major",
            "location_deviation_minor",
            "high_velocity_burst",
        ]
    ] = Field(default_factory=list)



class SessionData(BaseModel):
    login_to_transfer_seconds: float
    active_call_detected: bool
    pasted_beneficiary: bool

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Geolocation(BaseModel):
    lat: float
    lng: float


class SessionData(BaseModel):
    login_to_transfer_seconds: float
    active_call_detected: bool
    pasted_beneficiary: bool


class ScoreRequest(BaseModel):
    transaction_reference: str
    customer_id: str
    new_beneficiary: bool
    beneficiary_account: str
    beneficiary_bank_code: str
    amount: float
    timestamp: datetime
    last_transaction_timestamp: Optional[datetime] # that catches dormant account
    transaction_type: Literal[
        "transfer",
        "betting",
        "electricity_bill",
        "water_bill",
        "airtime",
        "data",
        "cable_tv",
    ]
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
            "new_beneficiary",
            "amount_deviation",
            "unusual_hour",
            "bot_speed_timing",
            "active_call",
            "dormant_account_spike",
            "location_deviation",
        ]
    ] = Field(default_factory=list)

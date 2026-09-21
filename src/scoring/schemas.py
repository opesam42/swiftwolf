
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, Optional
from datetime import datetime

class ScoreRequest(BaseModel):
    # FIX: without this, a value like "100004 " (accidental trailing
    # whitespace from copy-paste or a form field) is treated as a DIFFERENT
    # bank code from "100004" — silently breaks known_bank_codes/blacklist
    # composite-key matching and produces wrong scoring with no error raised
    # anywhere. str_strip_whitespace strips every string field on this model
    # (transaction_reference, customer_id, beneficiary_account,
    # beneficiary_bank_code, beneficiary_name) before validation runs, so the
    # API never even sees the untrimmed value in the first place.
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_reference: str
    customer_id: str
    new_beneficiary: bool
    beneficiary_account: str
    beneficiary_bank_code: str
    beneficiary_name: Optional[str] = None  # from Praise's account-lookup result; may be absent/unresolved
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
        ]
    ] = Field(default_factory=list)



class SessionData(BaseModel):
    login_to_transfer_seconds: float
    active_call_detected: bool
    pasted_beneficiary: bool


class Geolocation(BaseModel):
    lat: float
    lng: float
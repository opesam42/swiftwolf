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


class SettleRequest(BaseModel):
    transaction_reference: str
    customer_id: str
    final_status: Literal["completed", "failed", "abandoned"]
    verification_outcome: Literal[
        "not_required", "passed", "liveness_failed", "security_question_failed", "otp_failed", "abandoned"
    ]
    # amount/beneficiary_account/medium are accepted for contract fidelity with
    # the bank app's documented payload, but NOT used for Layer 2 logic — the
    # Transaction row already stored at /v1/score time is the source of truth.
    amount: float
    beneficiary_account: str
    medium: Literal["app", "ussd", "atm_withdrawal", "online_payment"]
    timestamp: datetime
    nibss_reference: Optional[str] = None


class SettleResponse(BaseModel):
    transaction_reference: str
    status: Literal["accepted", "already_processed"]


class FrictionProfileResponse(BaseModel):
    customer_id: str
    # None means "no risk_events history yet" — deliberately distinct from 0.0
    # (which would falsely claim a 0% proceed rate for a never-observed customer).
    recent_proceed_rate: Optional[float] = None
    is_low_friction_customer: bool


class SpendingDeltaResponse(BaseModel):
    customer_id: str
    insights: list[str] = Field(default_factory=list)


class SeedRequest(BaseModel):
    # Optional — omit to let OnboardingService pick randomly. Only meaningful
    # on the FIRST call for a given customer_id; once a dataset is persisted
    # (Customer.seed_dataset), every later call is idempotent and ignores this.
    dataset: Optional[str] = None


class SeedResponse(BaseModel):
    customer_id: str
    dataset: str
    status: Literal["seeding_started", "already_seeded"]

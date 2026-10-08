from enum import Enum
from sqlmodel import SQLModel, Field, Column, BigInteger, Boolean, DateTime, Numeric, JSON, Relationship
from sqlalchemy import JSON as SAJSON
from datetime import datetime
from typing import Optional
from src.profile.models import Customer
from src.settlement.models import Transaction
from datetime import datetime, timezone 

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Decision(str, Enum):
    """What SwiftWolf advises the bank app to do. SwiftWolf never picks the
    verification method — on STEP_UP the bank app chooses (OTP, liveness, etc.)."""
    PROCEED = "PROCEED"
    STEP_UP = "STEP_UP"
    BLOCK = "BLOCK"

class RiskReason(str, Enum):
    """Why a transaction scored the way it did. Values are what the API returns
    and what RiskEvent.reasons stores."""
    BLACKLISTED_ACCOUNT = "blacklisted_account"
    ELEVATED_RISK_TIER = "elevated_risk_tier"
    NEW_BENEFICIARY = "new_beneficiary"
    NEW_BANK = "new_bank"
    AMOUNT_DEVIATION = "amount_deviation"
    UNUSUAL_HOUR = "unusual_hour"  # rule currently disabled; kept so re-enabling it isn't an API change
    BOT_SPEED_TIMING = "bot_speed_timing"
    PASTED_NEW_BENEFICIARY = "pasted_new_beneficiary"
    DORMANT_ACCOUNT_SPIKE = "dormant_account_spike"
    LOCATION_DEVIATION_MAJOR = "location_deviation_major"
    LOCATION_DEVIATION_MINOR = "location_deviation_minor"
    HIGH_VELOCITY_BURST = "high_velocity_burst"
    TYPING_DEVIATION = "typing_deviation"

class RiskEvent(SQLModel, table=True):
    __tablename__ = "risk_events"

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    transaction_reference: str = Field(foreign_key="transactions.transaction_reference", unique=True, max_length=64)
    customer_id: str = Field(foreign_key="customers.customer_id", max_length=64)
    score: int = Field()
    telemetry: Optional[dict] = Field(default=None, sa_column=Column(SAJSON, nullable=True))
    decision: str = Field(max_length=20)  # a Decision value
    reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    anomaly_score: Optional[float] = Field(
        default=None,
        sa_column=Column(Numeric(10, 4), nullable=True,
                          comment="HalfSpaceTrees.score_one() output — HIGH = more "
                                  "anomalous (opposite of scikit-learn's convention). "
                                  "This is the model's OUTPUT, not its input."),
    )
    anomaly_flagged: Optional[bool] = Field(default=None, sa_column=Column(Boolean, nullable=True))
    anomaly_zscore: Optional[float] = Field(
        default=None,
        sa_column=Column(Numeric(10, 4), nullable=True,
                          comment="(amount - category_avg) / category_std, computed from "
                                  "the baseline BEFORE this transaction updated it. This "
                                  "is the model's INPUT — different from anomaly_score above."),
    )
    baseline_source: Optional[str] = Field(
        default=None, max_length=20,
        sa_column_kwargs={"comment": "'category' or 'global_fallback' — which baseline "
                                       "get_amount_baseline() actually used for this transaction."},
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))

    customer: Optional[Customer] = Relationship(back_populates="risk_events")
    transaction: Optional[Transaction] = Relationship(back_populates="risk_event")


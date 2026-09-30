from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from enum import Enum
from sqlalchemy import BigInteger, Boolean, Column, DateTime, text
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from src.profile.models import Customer
    from src.scoring.models import RiskEvent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class TransactionStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    STEP_UP_REQUIRED = "STEP_UP_REQUIRED"
    FAILED = "FAILED"

class TransactionType(str, Enum): 
    TRANSFER = "transfer"
    AIRTIME = "airtime"
    DATA = "data"
    ELECTRICITY = "electricity"
    CABLE_TV = "cable_tv"
    BETTING = "betting"
    

class Transaction(SQLModel, table=True):
    __tablename__ = "transactions"

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    transaction_reference: str = Field(unique=True, index=True, max_length=64)
    customer_id: str = Field(foreign_key="customers.customer_id", max_length=64, index=True)
    direction: str = Field(default="debit", max_length=10)

    # Integer kobo (₦15,000.50 -> 1500050). Convert to naira float only for statistics.
    amount: int = Field(sa_column=Column(BigInteger, nullable=False))
    destination_key: str = Field(max_length=100, index=True)
    # Bank code for transfers, telecom network for airtime/data, DISCO for electricity, etc.
    provider: str = Field(max_length=30)
    transaction_type: str = Field(max_length=30)
    medium: str = Field(max_length=20)
    status: str = Field(default=TransactionStatus.PENDING, index=True)

    is_settled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    occurred_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    settled_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )

    geolocation_lat: Optional[float] = Field(default=None)
    geolocation_lng: Optional[float] = Field(default=None)

    # Entity Relationships
    customer: Optional["Customer"] = Relationship(back_populates="transactions")
    risk_event: Optional["RiskEvent"] = Relationship(back_populates="transaction")
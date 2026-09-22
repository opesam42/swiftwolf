from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Numeric, text
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from src.profile.models import Customer
    from src.scoring.models import RiskEvent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Transaction(SQLModel, table=True):
    __tablename__ = "transactions"

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    transaction_reference: str = Field(unique=True, index=True, max_length=64)
    customer_id: str = Field(foreign_key="customers.customer_id", max_length=64, index=True)
    direction: str = Field(default="debit", max_length=10)
    amount: float = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    beneficiary_account: str = Field(max_length=20)
    beneficiary_bank_code: str = Field(max_length=10)
    beneficiary_name: Optional[str] = Field(default=None, max_length=100)
    transaction_type: str = Field(max_length=30)
    medium: str = Field(max_length=20)

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
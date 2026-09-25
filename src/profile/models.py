from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from sqlalchemy import BigInteger, Column, DateTime, text
from sqlalchemy import JSON as SAJSON
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from src.settlement.models import Transaction
    from src.scoring.models import RiskEvent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Customer(SQLModel, table=True):
    __tablename__ = "customers"

    id: Optional[int] = Field(
        default=None, primary_key=True, sa_type=BigInteger,
        description="Internal surrogate primary key.",
    )
    customer_id: str = Field(
        unique=True, index=True, max_length=64,
        description="Bank-issued customer identifier; the key every other table and cache uses.",
    )
    risk_tier: str = Field(
        default="standard", max_length=20,
        description="'standard' or 'elevated'. Elevated customers get +15 risk points on every score.",
    )

    # Statistical Baselines stored as structured JSON
    category_baselines: dict = Field(
        default_factory=dict, sa_column=Column(SAJSON, nullable=False),
        description="Per transaction_type amount stats ({'avg_amount', 'std_amount'}) "
                    "used to compute the amount-deviation Z-score.",
    )
    known_destinations: list[str] = Field(
        default_factory=list, sa_column=Column(SAJSON, nullable=False),
        description="Composite keys ('{category}:{provider}:{recipient}') of every settled "
                    "destination. Built by CustomerProfileService.destination_key_for().",
    )
    known_bank_codes: list[str] = Field(
        default_factory=list, sa_column=Column(SAJSON, nullable=False),
        description="Bank codes the customer has settled transfers to. Transfers only; "
                    "drives the 'new_bank' signal.",
    )
    typical_hours: list[int] = Field(
        default_factory=list, sa_column=Column(SAJSON, nullable=False),
        description="Hours of day (0-23) the customer has settled transactions in; "
                    "drives the 'unusual_hour' signal.",
    )
    known_location_cells: list[list[float]] = Field(
        default_factory=list, sa_column=Column(SAJSON, nullable=False),
        description="[lat, lng] grid cells (rounded to 1 decimal, ~11km) the customer has "
                    "transacted from; drives the location-deviation signals.",
    )

    is_cold_start: bool = Field(
        default=True,
        description="True until the first settled transaction; softens the amount-deviation "
                    "penalty while baselines are still empty.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        description="When the customer profile row was created (UTC).",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        description="Last time settlement updated this profile's baselines (UTC).",
    )

    transactions: list["Transaction"] = Relationship(back_populates="customer")
    risk_events: list["RiskEvent"] = Relationship(back_populates="customer")


    def to_baseline_dict(self) -> dict:
        """Converts database entity into lightweight cacheable dictionary for RuleEngine."""
        return {
            "customer_id": self.customer_id,
            "risk_tier": self.risk_tier,
            "category_baselines": self.category_baselines or {},
            "known_destinations": self.known_destinations or [],
            "known_bank_codes": self.known_bank_codes or [],
            "typical_hours": self.typical_hours or [],
            "known_location_cells": [list(c) for c in (self.known_location_cells or [])],
            "is_cold_start": self.is_cold_start,
        }
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

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigInteger)
    customer_id: str = Field(unique=True, index=True, max_length=64)
    risk_tier: str = Field(default="standard", max_length=20)

    # Statistical Baselines stored as structured JSON
    category_baselines: dict = Field(default_factory=dict, sa_column=Column(SAJSON, nullable=False))
    known_beneficiaries: list[str] = Field(
        default_factory=list, sa_column=Column(SAJSON, nullable=False)
    )
    known_bank_codes: list[str] = Field(default_factory=list, sa_column=Column(SAJSON, nullable=False))
    typical_hours: list[int] = Field(default_factory=list, sa_column=Column(SAJSON, nullable=False))
    known_location_cells: list[list[float]] = Field(
        default_factory=list, sa_column=Column(SAJSON, nullable=False)
    )

    is_cold_start: bool = Field(default=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )

    transactions: list["Transaction"] = Relationship(back_populates="customer")
    risk_events: list["RiskEvent"] = Relationship(back_populates="customer")


    def to_baseline_dict(self) -> dict:
        """Converts database entity into lightweight cacheable dictionary for RuleEngine."""
        return {
            "customer_id": self.customer_id,
            "risk_tier": self.risk_tier,
            "category_baselines": self.category_baselines or {},
            "known_beneficiaries": self.known_beneficiaries or [],
            "known_bank_codes": self.known_bank_codes or [],
            "typical_hours": self.typical_hours or [],
            "known_location_cells": [tuple(c) for c in (self.known_location_cells or [])],
            "is_cold_start": self.is_cold_start,
        }
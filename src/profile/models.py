from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from pydantic import BaseModel, field_validator
from sqlalchemy import BigInteger, Column, DateTime, text
from sqlalchemy import JSON as SAJSON
from sqlmodel import Field, Relationship, SQLModel

from src.settlement.models import TransactionType

if TYPE_CHECKING:
    from src.settlement.models import Transaction
    from src.scoring.models import RiskEvent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class CategoryBaselineStats(BaseModel): 
    count: int = Field(default=0, ge=0, description="Total settled transaction count")
    avg_amount: float = Field( default=0.0, ge=0.0, description="Lifetime Welford mean amount (naira)" ) 
    m2: float = Field( default=0.0, ge=0.0, description="Lifetime Welford sum of squared differences" ) 
    std_amount: float = Field( default=0.0, ge=0.0, description="Lifetime Welford sample standard deviation (naira)" )
    ewma_avg: float = Field(
        default=0.0, ge=0.0,
        description="Recent-habit EWMA mean amount (naira). Scoring uses this for the Z-score.",
    )
    ewma_var: float = Field(
        default=0.0, ge=0.0,
        description="Recent-habit EWMA variance. Kept unrounded for the next update.",
    )
    ewma_std: float = Field(
        default=0.0, ge=0.0,
        description="Recent-habit EWMA standard deviation (naira). Scoring uses this for the Z-score.",
    )

class TypingCluster(BaseModel):
    """One habit mode for a single telemetry field (no human labels)."""
    id: int
    ewma_avg: float = Field(ge=0.0)
    ewma_var: float = Field(default=0.0, ge=0.0)
    ewma_std: float = Field(default=0.0, ge=0.0)
    sample_count: int = Field(default=0, ge=0)


class TypingBaselines(BaseModel):
    """Adaptive Cluster Baselines for behavioural biometrics, stored on Customer."""
    sample_count: int = Field(default=0, ge=0, description="Genuine settled samples with telemetry")
    fields: dict[str, list[TypingCluster]] = Field(default_factory=dict)


class HourHistogram(BaseModel):
    hour_to_count: dict[int, int] = Field(default_factory=lambda: {h: 0 for h in range(24)})

    @field_validator("hour_to_count")
    @classmethod
    def validate_hours(cls, input: dict[int, int]) -> dict[int, int]:
        for hour, count in input.items():
            if not (0 <= hour <= 23):
                raise ValueError(f"Invalid hour key: {hour}. Must be 0-23.")
            if count < 0:
                raise ValueError(f"Invalid count for hour {hour}: {count}. Must be >= 0.")
        return input

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

    # Statistical baselines. JSON columns only ever hold plain JSON-compatible data;
    # use the get_/set_ helpers below to work with them as validated Pydantic objects.
    category_baselines: dict[str, dict] = Field(
        default_factory=dict, sa_column=Column(SAJSON, nullable=False),
        description="Per transaction_type amount stats, as plain CategoryBaselineStats dumps. "
                    "Lifetime Welford lives in avg_amount/m2/std_amount; recent-habit EWMA "
                    "in ewma_avg/ewma_var/ewma_std. The amount-deviation Z-score reads EWMA, "
                    "falling back to lifetime when ewma_std is still 0.",
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
    typical_hours: dict[str, int] = Field(
        default_factory=dict, sa_column=Column(SAJSON, nullable=False),
        description="Settled-transaction count per hour of day, keyed by hour as a string "
                    "('0'-'23', since JSON keys are always strings); empty until the first "
                    "settlement. Drives the 'unusual_hour' signal.",
    )
    known_location_cells: list[list[float]] = Field(
        default_factory=list, sa_column=Column(SAJSON, nullable=False),
        description="[lat, lng] grid cells (rounded to 1 decimal, ~11km) the customer has "
                    "transacted from; drives the location-deviation signals.",
    )
    typing_baselines: dict = Field(
        default_factory=lambda: {"sample_count": 0, "fields": {}},
        sa_column=Column(SAJSON, nullable=False, server_default=text("'{}'::json")),
        description="Adaptive Cluster Baselines per biometric field "
                    "({'sample_count', 'fields': {field: [TypingCluster, ...]}}). "
                    "Score reads this from the same cached baseline blob as amount EWMA.",
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


    def get_category_stats(self, transaction_type: str) -> CategoryBaselineStats:
        """Reads one category's stats out of the JSON column as a validated object."""
        raw = (self.category_baselines or {}).get(TransactionType(transaction_type).value)
        return CategoryBaselineStats.model_validate(raw) if raw else CategoryBaselineStats()

    def set_category_stats(self, transaction_type: str, stats: CategoryBaselineStats) -> None:
        """Writes one category's stats back as plain JSON. Reassigns the whole dict
        so SQLAlchemy notices the change (in-place JSON mutation isn't tracked)."""
        updated = dict(self.category_baselines or {})
        updated[TransactionType(transaction_type).value] = stats.model_dump()
        self.category_baselines = updated

    def get_hour_histogram(self) -> HourHistogram:
        """Reads typical_hours as a validated histogram; Pydantic turns the JSON
        string keys ('5') back into int hours (5)."""
        return HourHistogram.model_validate({"hour_to_count": self.typical_hours or {}})

    def set_hour_histogram(self, histogram: HourHistogram) -> None:
        """Writes the histogram back as plain JSON with string hour keys."""
        self.typical_hours = {str(hour): count for hour, count in histogram.hour_to_count.items()}

    def get_typing_baselines(self) -> TypingBaselines:
        raw = self.typing_baselines or {}
        if not raw or (not raw.get("fields") and not raw.get("sample_count")):
            return TypingBaselines()
        return TypingBaselines.model_validate(raw)

    def set_typing_baselines(self, baselines: TypingBaselines) -> None:
        self.typing_baselines = baselines.model_dump()

    def to_baseline_dict(self) -> dict:
        """Converts database entity into lightweight cacheable dictionary for RuleEngine."""
        return {
            "customer_id": self.customer_id,
            "risk_tier": self.risk_tier,
            "category_baselines": dict(self.category_baselines or {}),
            "known_destinations": self.known_destinations or [],
            "known_bank_codes": self.known_bank_codes or [],
            "typical_hours": dict(self.typical_hours or {}),
            "known_location_cells": [list(c) for c in (self.known_location_cells or [])],
            "typing_baselines": self.get_typing_baselines().model_dump(),
            "is_cold_start": self.is_cold_start,
        }
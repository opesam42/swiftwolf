from typing import Optional

from pydantic import BaseModel


class BaselineResponse(BaseModel):
    customer_id: str
    risk_tier: str
    is_cold_start: bool
    known_destinations_count: int
    known_banks_count: int


class FrictionProfileResponse(BaseModel):
    customer_id: str
    recommended_friction: str
    risk_tier: str
    reasons: list[str]


class SpendingDeltaResponse(BaseModel):
    customer_id: str
    category: str
    current_amount: float
    average_amount: float
    percentage_delta: float
    is_anomalous: bool
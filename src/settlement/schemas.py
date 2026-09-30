from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.settlement.models import TransactionStatus, VerificationMethod, VerificationOutcome


class SettleRequest(BaseModel):
    """Only what the bank knows after the payment. Customer, amount, type, channel and
    destination are read from the transaction SwiftWolf scored, so they can't disagree."""
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_reference: str = Field(min_length=1, max_length=64)
    settled_at: datetime
    status: Literal["SUCCESS", "FAILED"] = "SUCCESS"

    # What the bank's step-up actually did. Omit both when no step-up happened (PROCEED).
    verification_method: VerificationMethod | None = None
    verification_outcome: VerificationOutcome | None = None

    @model_validator(mode="after")
    def _method_and_outcome_together(self) -> "SettleRequest":
        # Half a report (a method with no result, or a result with no method) is useless for audit
        if (self.verification_method is None) != (self.verification_outcome is None):
            raise ValueError("verification_method and verification_outcome must be sent together, or both omitted")
        return self


class SettleResponse(BaseModel):
    transaction_reference: str
    status: TransactionStatus
    is_settled: bool
    message: str

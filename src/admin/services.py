from sqlmodel import Session, select

from src.blacklist.models import BlacklistedAccount
from src.profile.models import Customer


class BeneficiaryExportService:
    """Handles administrative data extraction for compliance reporting."""

    def __init__(self, db_session: Session):
        self.db = db_session

    def export_beneficiaries(self) -> list[dict]:
        """Surfaces resolved account holder names for both blacklisted and regular accounts."""
        stmt = select(BlacklistedAccount).where(BlacklistedAccount.is_active == True)
        rows = self.db.exec(stmt).all()

        return [
            {
                "account": row.beneficiary_account,
                "bank_code": row.beneficiary_bank_code,
                "name": row.beneficiary_name or "UNRESOLVED",
                "reason": row.reason,
                "source": row.source,
                "added_at": row.added_at.isoformat(),
            }
            for row in rows
        ]


class OnboardingService:
    """Manages administrative customer seeding and demo data initialization."""

    def __init__(self, db_session: Session):
        self.db = db_session

    def seed_demo_customer(self, customer_id: str, risk_tier: str = "standard") -> Customer:
        customer = self.db.exec(select(Customer).where(Customer.customer_id == customer_id)).first()
        if customer is None:
            customer = Customer(customer_id=customer_id, risk_tier=risk_tier)
            self.db.add(customer)
            self.db.commit()
            self.db.refresh(customer)
        return customer
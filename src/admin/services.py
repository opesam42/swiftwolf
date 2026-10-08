from sqlmodel import Session, select

from src.blacklist.models import BlacklistedAccount
from src.core.config import settings
from src.profile.clusters import TYPING_FIELDS
from src.profile.models import Customer
from src.profile.repository import CustomerRepository
from src.scoring.models import RiskEvent
from src.settlement.models import Transaction, TransactionChannel, TransactionType


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

    def __init__(self, db_session: Session, redis_client=None):
        self.customer_repo = CustomerRepository(db_session, redis_client)

    def seed_demo_customer(self, customer_id: str, risk_tier: str = "standard") -> Customer:
        customer = self.customer_repo.get(customer_id)
        if customer is None:
            customer = self.customer_repo.save(Customer(customer_id=customer_id, risk_tier=risk_tier))
        return customer


class DashboardService:
    """Read-only EWMA control-chart payload for the admin dashboard. Does not score."""

    POINT_LIMIT = 20
    PENDING_LIMIT = 5
    LIVE_CHANNELS = (TransactionChannel.APP.value, TransactionChannel.USSD.value)
    # 1σ = 0 amount points; 2.5σ = typing/habit edge; 3–5σ = amount sigmoid climbing to cap.
    EXTRA_ZONE_Z = (1.0, 3.0, 4.0, 5.0)

    def __init__(self, db_session: Session):
        self.db = db_session
        self.customer_repo = CustomerRepository(db_session)

    def control_chart(self, customer_id: str, category: str = TransactionType.TRANSFER.value) -> dict | None:
        category = self._require_category(category)
        customer = self.customer_repo.get(customer_id)
        if customer is None:
            return None

        stats = customer.get_category_stats(category)
        mu = float(stats.ewma_avg or 0.0)
        sigma = float(stats.ewma_std or 0.0)
        band_z = float(settings.TYPING_MATCH_Z)
        zones = self._zones(mu, sigma, band_z)
        habit = next(zone for zone in zones if zone["z"] == band_z)

        return {
            "customer_id": customer.customer_id,
            "category": category,
            "ewma_avg": round(mu, 2),
            "ewma_std": round(sigma, 2),
            "k_active": self._cluster_budget(customer),
            "k_max": settings.TYPING_CLUSTER_MAX,
            "band_z": band_z,
            "upper": habit["upper"],
            "lower": habit["lower"],
            "zones": zones,
            "points": self._recent_points(customer.customer_id, category, mu, sigma),
        }

    @staticmethod
    def _require_category(category: str) -> str:
        try:
            return TransactionType(category).value
        except ValueError as exc:
            raise ValueError(f"Unknown category: {category}") from exc

    @classmethod
    def _zone_levels(cls, band_z: float) -> tuple[float, ...]:
        return tuple(sorted({round(z, 4) for z in (*cls.EXTRA_ZONE_Z, band_z)}))

    @classmethod
    def _zones(cls, mu: float, sigma: float, band_z: float) -> list[dict]:
        return [
            {
                "z": z,
                "upper": round(mu + z * sigma, 2),
                "lower": round(max(0.0, mu - z * sigma), 2),
            }
            for z in cls._zone_levels(band_z)
        ]

    def _cluster_budget(self, customer: Customer) -> int:
        fields = customer.get_typing_baselines().fields
        if not fields:
            return 0
        return max((len(fields.get(name) or []) for name in TYPING_FIELDS), default=0)

    def _recent_points(self, customer_id: str, category: str, mu: float, sigma: float) -> list[dict]:
        """Settled history (trains the ribbon) plus unscored-wait: scored app/ussd not yet settled."""
        history = self.db.exec(
            select(Transaction, RiskEvent)
            .outerjoin(RiskEvent, RiskEvent.transaction_reference == Transaction.transaction_reference)
            .where(Transaction.customer_id == customer_id)
            .where(Transaction.transaction_type == category)
            .where(Transaction.is_settled == True)
            .order_by(Transaction.occurred_at.desc())
            .limit(self.POINT_LIMIT)
        ).all()

        pending = self.db.exec(
            select(Transaction, RiskEvent)
            .join(RiskEvent, RiskEvent.transaction_reference == Transaction.transaction_reference)
            .where(Transaction.customer_id == customer_id)
            .where(Transaction.transaction_type == category)
            .where(Transaction.medium.in_(self.LIVE_CHANNELS))
            .where(Transaction.is_settled == False)
            .order_by(Transaction.occurred_at.desc())
            .limit(self.PENDING_LIMIT)
        ).all()

        points = self._serialise(reversed(history), mu, sigma, pending=False, start=1)
        live_start = len(points) + 1
        points.extend(self._serialise(reversed(pending), mu, sigma, pending=True, start=live_start))
        return points

    @staticmethod
    def _serialise(rows, mu: float, sigma: float, *, pending: bool, start: int) -> list[dict]:
        points: list[dict] = []
        for offset, (txn, event) in enumerate(rows):
            amount = round(txn.amount / 100.0, 2)
            z = 0.0 if sigma <= 0 else round(abs(amount - mu) / sigma, 2)
            points.append(
                {
                    "i": start + offset,
                    "amount": amount,
                    "decision": event.decision if event is not None else None,
                    "z": z,
                    "pending": pending,
                }
            )
        return points

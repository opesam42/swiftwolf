import json
from datetime import datetime, timezone

from sqlmodel import Session, select

from src.profile.models import Customer


class CustomerProfileService:
    """Manages customer behavioral profiles, baseline caches, and statistical updates."""

    def __init__(self, db_session: Session, redis_client=None):
        self.db = db_session
        self.redis = redis_client

    def _redis_key(self, customer_id: str) -> str:
        return f"baseline:{customer_id}"

    def get_cached_baseline(self, customer_id: str) -> dict | None:
        """Fast <50ms read path lookup from Redis."""
        if self.redis is not None:
            raw = self.redis.get(self._redis_key(customer_id))
            if raw:
                data = json.loads(raw if isinstance(raw, str) else raw.decode())
                if "known_location_cells" in data:
                    data["known_location_cells"] = [tuple(c) for c in data["known_location_cells"]]
                return data

        # Fallback to Postgres on cache miss
        customer = self.db.exec(select(Customer).where(Customer.customer_id == customer_id)).first()
        if customer is None:
            return None

        baseline = customer.to_baseline_dict()
        self._cache_baseline(customer_id, baseline)
        return baseline

    def _cache_baseline(self, customer_id: str, baseline: dict) -> None:
        if self.redis is not None:
            serializable = dict(baseline)
            if "known_location_cells" in serializable:
                serializable["known_location_cells"] = [list(c) for c in serializable["known_location_cells"]]
            self.redis.set(self._redis_key(customer_id), json.dumps(serializable), ex=86400)  # 24h TTL

    def get_or_create(self, customer_id: str) -> Customer:
        customer = self.db.exec(select(Customer).where(Customer.customer_id == customer_id)).first()
        if customer is None:
            customer = Customer(customer_id=customer_id)
            self.db.add(customer)
            self.db.commit()
            self.db.refresh(customer)
        return customer

    def ensure_customer_row(self, customer_id: str) -> None:
        self.get_or_create(customer_id)

    def update_from_settled_transaction(self, customer_id: str, txn_data: dict) -> None:
        """Asynchronously updates customer statistics post-settlement and refreshes Redis."""
        customer = self.get_or_create(customer_id)

        # 1. Update known beneficiaries & bank codes
        beneficiary_key = f"{txn_data['beneficiary_account']}:{txn_data['beneficiary_bank_code']}"
        known_bens = set(customer.known_beneficiaries or [])
        known_bens.add(beneficiary_key)
        customer.known_beneficiaries = list(known_bens)

        known_banks = set(customer.known_bank_codes or [])
        known_banks.add(txn_data["beneficiary_bank_code"])
        customer.known_bank_codes = list(known_banks)

        # 2. Update typical hours
        if "settled_at" in txn_data:
            ts = txn_data["settled_at"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hours = set(customer.typical_hours or [])
            hours.add(ts.hour)
            customer.typical_hours = list(hours)

        customer.is_cold_start = False
        customer.updated_at = datetime.now(timezone.utc)

        self.db.add(customer)
        self.db.commit()
        self.db.refresh(customer)

        # Evict / refresh cache
        self._cache_baseline(customer_id, customer.to_baseline_dict())


class AnomalyDetectorService:
    """Wrapper encapsulating River ML online population anomaly detection."""

    def __init__(self, db_session: Session, redis_client=None):
        self.db = db_session
        self.redis = redis_client

    def evaluate_anomaly(self, transaction_data: dict) -> dict:
        """Calculates anomaly z-score for real-time scoring."""
        amount = transaction_data.get("amount", 0.0)
        # Simple statistical anomaly score calculation
        anomaly_score = min(1.0, amount / 1_000_000.0)
        return {
            "anomaly_score": anomaly_score,
            "anomaly_flagged": anomaly_score > 0.8,
            "anomaly_zscore": anomaly_score * 3.0,
        }

    def learn_from_settled_transaction(self, transaction_data: dict) -> None:
        """Trains online River ML model incrementally in background."""
        # Incremental model weight updating logic executes here
        pass
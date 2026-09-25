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

    @staticmethod
    def build_destination_key(category: str, *identifying_fields: str) -> str:
        """
        Builds a composite destination key from the minimum set of fields
        that uniquely identify a recipient within a given category.

        Used by both the Reflex Layer (RuleEngine, to check if a destination
        is known) and the Vigilance Layer (SettlementService, to record a
        newly-confirmed destination after settlement)
        """
        return f"{category}:{':'.join(identifying_fields)}"

    @classmethod
    def destination_key_for(cls, transaction: dict) -> str:
        """
        Resolves a transaction's category and builds its destination key.

        `beneficiary_bank_code` carries the provider for each category
        (bank code, network, disco, platform) and `beneficiary_account`
        carries the recipient identifier (NUBAN, phone, meter, account ref).
        """
        provider = transaction["beneficiary_bank_code"]
        recipient = transaction["beneficiary_account"]

        match transaction["transaction_type"]:
            case "transfer":
                return cls.build_destination_key("transfer", provider, recipient)
            case "airtime" | "data":
                # Airtime and data top up the same phone line
                return cls.build_destination_key("airtime", provider, recipient)
            case "electricity_bill":
                return cls.build_destination_key("electricity", provider, recipient)
            case "water_bill":
                return cls.build_destination_key("water", provider, recipient)
            case "cable_tv":
                return cls.build_destination_key("cable_tv", provider, recipient)
            case "betting":
                return cls.build_destination_key("betting", provider, recipient)
            case other:
                raise ValueError(f"Unknown transaction_type for destination key: {other!r}")


    def get_cached_baseline(self, customer_id: str) -> dict | None:
        """Fast <50ms read path lookup from Redis."""
        if self.redis is not None:
            raw = self.redis.get(self._redis_key(customer_id))
            if raw:
                data = json.loads(raw if isinstance(raw, str) else raw.decode())
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

        # 1. Update known destinations & bank codes
        destination_key = self.destination_key_for(txn_data)
        known_destinations = set(customer.known_destinations or [])
        known_destinations.add(destination_key)
        customer.known_destinations = list(known_destinations)

        # Bank codes are only meaningful for transfers
        if txn_data["transaction_type"] == "transfer":
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

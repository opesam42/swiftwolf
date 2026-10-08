from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.profile.models import Customer, HourHistogram, CategoryBaselineStats
from src.profile.repository import CustomerRepository
from src.profile.stats import ewma_update, welford_update

from src.settlement.models import TransactionType
from src.core.config import settings
from src.core.errors import InvalidSettlementData

if TYPE_CHECKING:
    from sqlmodel import Session

class CustomerProfileService:
    """Manages customer behavioral profiles, baseline caches, and statistical updates.
    All database access goes through CustomerRepository."""

    def __init__(self, db_session: "Session", redis_client=None, repository: CustomerRepository | None = None):
        # db_session and redis_client are only used to build the default
        # repository; pass repository directly to swap it out (e.g. in tests).
        self.repo = repository or CustomerRepository(db_session, redis_client)

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
    def destination_key_for(cls, transaction_type: str, provider: str, recipient:str) -> str:
        """
        Resolves a transaction's category and builds its destination key.

        Args: 
            transaction_type: TransactionType value (e.g., 'transfer', 'airtime', 'data', 'electricity').
            provider: Provider identifier for the category (e.g., bank code for transfers, telecom network for data/airtime, DISCO for electricity, platform for betting). 
            recipient: Recipient identifier (e.g., NUBAN account number, phone number, meter number). 
            
        Returns: 
            Uniform composite key formatted as '{category}:{provider}:{recipient}' (e.g., 'transfer:058:0123456789', 'data:MTN:09044556677').
        """
        
        match transaction_type:
            case TransactionType.TRANSFER:
                return cls.build_destination_key("transfer", provider, recipient)
            case TransactionType.AIRTIME | TransactionType.DATA:
                # Airtime and data top up the same phone line
                return cls.build_destination_key("airtime", provider, recipient)
            case TransactionType.ELECTRICITY:
                return cls.build_destination_key("electricity", provider, recipient)
            case TransactionType.CABLE_TV:
                return cls.build_destination_key("cable_tv", provider, recipient)
            case TransactionType.BETTING:
                return cls.build_destination_key("betting", provider, recipient)
            case other:
                raise ValueError(f"Unknown transaction_type for destination key: {other!r}")

    def parse_destination_key(destination_key: str) -> tuple[str, str, str]:
        """ Extract transaction_type, provider and recipient from the destination_key """
        parts = destination_key.split(":", 2)
        if len(parts) != 3: 
            raise ValueError( f"Invalid destination_key format: '{destination_key}'. Expected format: 'transaction\_type:provider:recipient" )

    def update_baseline_from_settled_transaction(
            self, 
            customer_id: str, 
            amount: int, 
            destination_key: str, 
            transaction_type: TransactionType, 
            occurred_at: datetime,
            geolocation_lat: float | None = None, 
            geolocation_lng: float | None = None,
            bank_code: str | None = None
        ) -> None:
        """Updates customer statistics post-settlement.
        Returns the updated Customer, so callers (e.g. SettlementService)
        can build a response without a second database read."""

        # open transaction to prevent race condition
        customer = self.repo.get_or_create_for_update(customer_id)
        
        known_destinations = set(customer.known_destinations or [])
        known_destinations.add(destination_key)
        customer.known_destinations = list(known_destinations)

        if transaction_type == TransactionType.TRANSFER:
            if not bank_code:
                raise InvalidSettlementData(customer_id, "Bank Code Missing for TRANSACTION_TYPE - transfer")
            known_bank_codes = set(customer.known_bank_codes or [])
            known_bank_codes.add(bank_code)
            customer.known_bank_codes = list(known_bank_codes)

        # update hour histogram
        hour = occurred_at.hour
        histogram = customer.get_hour_histogram()
        hour_counts = dict(histogram.hour_to_count)
        hour_counts[hour] = hour_counts.get(hour, 0) + 1
        customer.set_hour_histogram(HourHistogram(hour_to_count=hour_counts))

        # update geolocation details
        if geolocation_lat is not None and geolocation_lng is not None:
            cell = [round(geolocation_lat, 1), round(geolocation_lng, 1)]
            known_location_cells = list( customer.known_location_cells or [] )
            if cell not in known_location_cells:
                known_location_cells.append(cell)
            customer.known_location_cells = known_location_cells

        # Amount baselines: lifetime Welford (global mean) plus recent-habit EWMA.
        # Amount arrives in integer kobo; both series are kept in naira.
        stats = customer.get_category_stats(transaction_type)
        amount_naira = amount / 100.0

        lifetime = welford_update(stats.count, stats.avg_amount, stats.m2, amount_naira)
        recent = ewma_update(
            lifetime.count, stats.ewma_avg, stats.ewma_var, amount_naira, settings.EWMA_ALPHA
        )

        updated_stats = CategoryBaselineStats(
            count=lifetime.count,
            avg_amount=round(lifetime.avg, 2),
            m2=lifetime.m2,
            std_amount=round(lifetime.std, 2),
            ewma_avg=round(recent.avg, 2),
            ewma_var=recent.var,
            ewma_std=round(recent.std, 2),
        )

        customer.set_category_stats(transaction_type, updated_stats)

        # Leave cold start once enough settled history exists. The per-category counts
        # already record every settled transaction, so no extra COUNT query is needed,
        # and the flag commits atomically with the baseline it describes.
        if customer.is_cold_start:
            total_settled = sum(customer.get_category_stats(t).count for t in customer.category_baselines)
            if total_settled >= settings.COLD_START_MIN_SETTLED_TRANSACTIONS:
                customer.is_cold_start = False

        # finish transaction; the cache_sync commit hook invalidates the cached baseline
        self.repo.save(customer)

        return customer
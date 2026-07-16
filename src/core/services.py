"""
customer_profile_service.py — the bridge between CustomerProfile (pure River
statistics, no I/O of its own) and its two persistence layers: Postgres
(durable source of truth) and Redis (fast-read cache for Layer 1's hot path).

This is the ONLY place either store should be written to for profile data —
keeping both in sync by construction, not by convention scattered across
call sites.
"""
import hashlib
import json

from sqlmodel import select

from src.core.repository import CustomerProfile, ZScoreAnomalyDetector
from src.core.scoring import score_transaction


class CustomerProfileService:
    def __init__(self, db_session, redis_client=None):
        # redis_client is optional — a cache should degrade gracefully rather
        # than being a hard dependency. Callers with no live traffic to serve
        # (offline seeding, batch jobs) can skip it entirely; the real HTTP API
        # layer should pass a real client so Layer 1's hot-path reads work.
        self.db = db_session
        self.redis = redis_client

    def get_or_create(self, customer_id: str) -> CustomerProfile:
        """Loads from Postgres. A customer with no row yet is NOT an error —
        that's just the normal first-transaction path, and returns a fresh,
        empty CustomerProfile."""
        from src.core.models import Customer  # local import avoids a circular dependency with models.py
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            return CustomerProfile()

        # profile_json is a Postgres JSON column — SQLAlchemy already
        # deserializes it into a Python dict on read. CustomerProfile.from_json()
        # expects a JSON *string*, so re-serialize before handing it over.
        return CustomerProfile.from_json(json.dumps(customer.profile_json))

    def save(self, customer_id: str, profile: CustomerProfile) -> None:
        """Writes to Postgres always, and to Redis too when a redis_client was
        given — Postgres is the durable source of truth, Redis is Layer 1's
        hot-path cache on top of it. Callers never write to either store
        directly; they call this instead."""
        from src.core.models import Customer

        # to_json() returns a JSON string; the profile_json column expects a
        # dict — json.loads() converts back before assignment.
        profile_dict = json.loads(profile.to_json())

        customer = self.db.get(Customer, customer_id)
        if customer is None:
            customer = Customer(
                customer_id=customer_id,
                profile_json=profile_dict,
                transaction_count=profile.transaction_count,
            )
            self.db.add(customer)
        else:
            customer.profile_json = profile_dict
            customer.transaction_count = profile.transaction_count

        self.db.commit()

        if self.redis is not None:
            # Redis gets the lightweight baseline dict, NOT the full stats
            # state — this is specifically what Layer 1 reads on the <50ms
            # hot path. Skipped entirely when no redis_client was given.
            self.redis.set(f"baseline:{customer_id}", json.dumps(profile.to_baseline_dict()))

    def get_cached_baseline(self, customer_id: str) -> dict | None:
        """Layer 1's actual hot-path read — Redis only, never touches Postgres.
        Returns None if nothing's cached yet (cold start, cache was flushed, or
        no redis_client was configured at all) — caller should fall back to
        get_or_create() + a fresh baseline in that case."""
        if self.redis is None:
            return None
        cached = self.redis.get(f"baseline:{customer_id}")
        return json.loads(cached) if cached else None

    def record_transaction(
        self, customer_id: str, amount: float, hour: int,
        beneficiary_account: str, beneficiary_bank_code: str,
        transaction_type: str, direction: str, timestamp,
    ) -> CustomerProfile:
        """Wraps the full load -> update -> save cycle in one call. This is
        what the settle background task should actually call — NOT
        get_or_create() + profile.update() + save() manually, since splitting
        those three steps across caller code makes it easy to update the
        in-memory profile and forget to persist it, silently losing the update."""
        profile = self.get_or_create(customer_id)
        profile.update(
            amount=amount,
            hour=hour,
            beneficiary_account=beneficiary_account, beneficiary_bank_code=beneficiary_bank_code,
            transaction_type=transaction_type,
        )
        self.save(customer_id, profile)
        return profile


class RuleEngineService:
    """Applies the Layer 1 rules to an incoming transaction and cached baseline."""

    def __init__(self, blacklisted_accounts: set[str] | None = None):
        self.blacklisted_accounts = blacklisted_accounts or set()

    def score(self, transaction: dict, baseline: dict | None = None) -> dict:
        if baseline is None:
            baseline = {
                "known_beneficiaries": [],
                "known_bank_codes": [],
                "category_baselines": {},
                "typical_hours": [],
                "is_cold_start": True,
            }
        return score_transaction(transaction, baseline, self.blacklisted_accounts)


class AnomalyDetectorService:
    """The bridge between ZScoreAnomalyDetector (pure River model, no I/O of its own)
    and its persistence layer: a single AnomalyModelState row in Postgres.

    Unlike CustomerProfileService, there is no per-customer key here — this model is
    population-level, one shared HalfSpaceTrees pipeline scored against every
    customer's transactions, so the table only ever holds one row.
    """

    def __init__(self, db_session, flag_threshold: float = 0.6, window_size: int = 250):
        self.db = db_session
        self.default_flag_threshold = flag_threshold
        self.default_window_size = window_size

    def get_or_create(self) -> ZScoreAnomalyDetector:
        """Loads the singleton row. No row yet is NOT an error — that's just the
        normal pre-first-transaction state, and returns a fresh detector seeded
        with the configured default threshold/window_size. A caller doing
        trusted historical seeding should overwrite flag_threshold afterward via
        detector.calibrate_from_scores() before saving — this method itself has
        no opinion on calibration."""
        from src.core.models import AnomalyModelState  # local import avoids a circular dependency with models.py

        state = self.db.exec(select(AnomalyModelState)).first()
        if state is None:
            return ZScoreAnomalyDetector(
                flag_threshold=self.default_flag_threshold,
                window_size=self.default_window_size,
            )

        return ZScoreAnomalyDetector.from_bytes(
            state.model_bytes,
            flag_threshold=state.flag_threshold,
            window_size=self.default_window_size,
            learned_count=state.learned_count,
            skipped_count=state.skipped_count,
        )

    def save(self, detector: ZScoreAnomalyDetector) -> None:
        """Writes the singleton row every time — upserts against the first (and
        only) row rather than a per-key lookup, since there's exactly one model."""
        from src.core.models import AnomalyModelState

        state = self.db.exec(select(AnomalyModelState)).first()
        if state is None:
            state = AnomalyModelState(
                model_bytes=detector.to_bytes(),
                flag_threshold=detector.flag_threshold,
                learned_count=detector.learned_count,
                skipped_count=detector.skipped_count,
            )
            self.db.add(state)
        else:
            state.model_bytes = detector.to_bytes()
            state.flag_threshold = detector.flag_threshold
            state.learned_count = detector.learned_count
            state.skipped_count = detector.skipped_count

        self.db.commit()

    def score_transaction(self, transaction: dict, profile: CustomerProfile) -> dict:
        """Wraps the full load -> process -> save cycle in one call — the
        background-task equivalent of CustomerProfileService.record_transaction().
        NOT get_or_create() + detector.process() + save() manually, since splitting
        those steps across caller code risks scoring against one model instance and
        persisting a different one.

        CRITICAL ORDERING (inherited from ZScoreAnomalyDetector.process): pass the
        SAME profile here that has NOT yet had this transaction applied via
        profile.update() — call this before CustomerProfileService.record_transaction()
        for the same transaction, never after."""
        detector = self.get_or_create()
        result = detector.process(transaction, profile)
        self.save(detector)
        return result


class TransactionService:
    """Persists raw Transaction rows — the audit trail of what was actually fed
    into CustomerProfile/ZScoreAnomalyDetector training. Separate from either
    model's own state persistence: this is row-level history, not summary
    statistics, and neither CustomerProfileService nor AnomalyDetectorService
    needs it to do their own jobs.
    """

    # Sentinel for rows backfilled from a bank statement CSV, which never went
    # through /v1/score and so have no real medium — see models.Transaction.medium.
    MEDIUM_HISTORICAL_SEED = "historical_seed"

    def __init__(self, db_session):
        self.db = db_session

    @staticmethod
    def make_seed_reference(
        customer_id: str, occurred_at, amount: float, beneficiary_account: str
    ) -> str:
        """Deterministic transaction_reference for historical seed rows — a bank
        statement has no equivalent of the reference SwiftWolf itself generates
        before calling NIBSS, so one is derived instead of left blank. Being
        deterministic (same inputs -> same reference) also makes re-running the
        same CSV idempotent rather than creating duplicate rows."""
        raw = f"{customer_id}:{occurred_at.isoformat()}:{amount}:{beneficiary_account}"
        return f"seed_{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

    def save_many(self, transactions: list) -> int:
        """Bulk insert — ONE commit regardless of row count, matching the same
        bulk pattern used for CustomerProfile/ZScoreAnomalyDetector state during
        seeding. Rows whose transaction_reference already exists in the DB are
        skipped (re-running the same CSV twice won't fail on the unique
        constraint), AND duplicate references within THIS SAME batch are
        collapsed to one (a bank statement can genuinely contain two identical
        lines — same customer/timestamp/amount/beneficiary — which hash to the
        same deterministic reference; those would otherwise collide with each
        other rather than with anything already in the DB).
        Returns the number of rows actually inserted."""
        from src.core.models import Transaction

        if not transactions:
            return 0

        # Collapse in-batch duplicates first, keeping the first occurrence.
        deduped_by_ref = {}
        for t in transactions:
            deduped_by_ref.setdefault(t.transaction_reference, t)

        refs = list(deduped_by_ref.keys())
        existing_refs = set(
            self.db.exec(
                select(Transaction.transaction_reference).where(
                    Transaction.transaction_reference.in_(refs)
                )
            ).all()
        )
        new_rows = [t for ref, t in deduped_by_ref.items() if ref not in existing_refs]

        self.db.add_all(new_rows)
        self.db.commit()
        return len(new_rows)
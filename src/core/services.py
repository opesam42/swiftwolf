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

    def ensure_customer_row(self, customer_id: str) -> None:
        """Creates a bare Customer row (empty profile) if none exists yet.
        Needed because Transaction.customer_id has a FK to
        customers.customer_id, and a customer's very first /v1/score call —
        before Layer 2 has ever run profile.update()+save() for them — is a
        completely normal case, not an error. Deliberately does NOT touch
        Redis: this only satisfies the FK, it is not a baseline write, and
        Layer 1 must never look like it's updating behavioral state."""
        from src.core.models import Customer

        customer = self.db.get(Customer, customer_id)
        if customer is None:
            self.db.add(Customer(
                customer_id=customer_id,
                profile_json=json.loads(CustomerProfile().to_json()),
                transaction_count=0,
            ))
            self.db.flush()

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
        transaction_type: str, direction: str, timestamp, geolocation: dict | None = None,
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
            geolocation=geolocation,
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


class BlacklistService:
    """Bridge between blacklisted_accounts (Postgres, source of truth) and a
    Redis Set cache of active composite (account:bank_code) keys. Layer 1's
    hot path reads the Redis Set only — same cache-first pattern as
    CustomerProfileService's baseline, for the same <50ms-budget reason."""

    REDIS_KEY = "blacklist:active_accounts"

    def __init__(self, db_session, redis_client=None):
        self.db = db_session
        self.redis = redis_client

    def get_active_keys(self) -> set[str]:
        """Layer 1's hot-path read. Redis first; falls back to Postgres (and
        refills Redis from it) on a cache miss — e.g. right after a fresh
        deploy, before anything has synced yet — same fallback shape as
        CustomerProfileService.get_cached_baseline()."""
        if self.redis is not None:
            cached = self.redis.smembers(self.REDIS_KEY)
            if cached:
                return {m.decode() if isinstance(m, bytes) else m for m in cached}

        return self.sync_to_redis()

    def sync_to_redis(self, keys: set[str] | None = None) -> set[str]:
        """Rebuilds the Redis Set from Postgres. Call this after any blacklist
        mutation (seeding, an analyst adding/deactivating an entry) so the
        hot-path cache doesn't silently drift from the source of truth in
        Postgres. No-ops the Redis write if no redis_client was configured —
        still returns the freshly-read Postgres set either way."""
        if keys is None:
            keys = self._load_active_keys_from_postgres()

        if self.redis is not None:
            self.redis.delete(self.REDIS_KEY)
            if keys:
                self.redis.sadd(self.REDIS_KEY, *keys)

        return keys

    def _load_active_keys_from_postgres(self) -> set[str]:
        from src.core.models import BlacklistedAccount  # local import avoids a circular dependency with models.py

        rows = self.db.exec(
            select(BlacklistedAccount).where(BlacklistedAccount.is_active == True)  # noqa: E712
        ).all()
        return {f"{row.beneficiary_account}:{row.beneficiary_bank_code}" for row in rows}


class ScoreService:
    """Orchestrates a single POST /v1/score request end to end: idempotency
    check, cached-baseline read, blacklist read, Layer 1 scoring, and
    persisting the Transaction + RiskEvent rows. This is the ONLY place that
    orchestration should live — routes.py stays a thin (de)serialization
    layer over this, same layering CustomerProfileService/AnomalyDetectorService
    already establish."""

    def __init__(self, db_session, redis_client=None):
        self.db = db_session
        self.profile_service = CustomerProfileService(db_session, redis_client)
        self.blacklist_service = BlacklistService(db_session, redis_client)
        self.rule_engine = RuleEngineService()

    def get_cached_decision(self, transaction_reference: str) -> dict | None:
        """Per the System Design doc's idempotency requirement: if the bank
        app retries /v1/score for a transaction_reference already scored
        (e.g. its own request timed out even though SwiftWolf processed it),
        return the SAME decision rather than re-scoring — session data may
        have drifted slightly between attempts, and re-scoring could produce
        an inconsistent result for what the bank app considers one request."""
        from src.core.models import RiskEvent  # local import avoids a circular dependency with models.py

        existing = self.db.exec(
            select(RiskEvent).where(RiskEvent.transaction_reference == transaction_reference)
        ).first()
        if existing is None:
            return None

        return {
            "transaction_reference": existing.transaction_reference,
            "score": existing.score,
            "decision": existing.decision,
            "step_up_method": existing.step_up_method,
            "reasons": existing.reasons,
        }

    def score(self, transaction: dict) -> dict:
        """transaction must carry everything score_transaction() plus the
        Transaction row need: transaction_reference, customer_id,
        beneficiary_account, beneficiary_bank_code, amount, timestamp,
        transaction_type, medium, session, geolocation,
        last_transaction_timestamp.

        Read-only towards CustomerProfile/River — this is the <50ms hot path,
        so it never calls profile.update(). That happens later, in the
        background task on /v1/transactions/settle, per the System Design doc."""
        from sqlalchemy.exc import IntegrityError

        from src.core.models import RiskEvent, Transaction

        cached = self.get_cached_decision(transaction["transaction_reference"])
        if cached is not None:
            return cached

        baseline = self.profile_service.get_cached_baseline(transaction["customer_id"])
        if baseline is None:
            # Cache miss (cold cache, or a customer whose baseline hasn't been
            # written to Redis yet) — fall back to Postgres rather than
            # scoring against an empty/wrong baseline.
            profile = self.profile_service.get_or_create(transaction["customer_id"])
            baseline = profile.to_baseline_dict()

        self.rule_engine.blacklisted_accounts = self.blacklist_service.get_active_keys()
        result = self.rule_engine.score(transaction, baseline)

        # Satisfies Transaction.customer_id's FK for a brand-new customer's
        # very first /v1/score call — NOT a profile/baseline write.
        self.profile_service.ensure_customer_row(transaction["customer_id"])

        geolocation = transaction.get("geolocation")
        txn_row = Transaction(
            transaction_reference=transaction["transaction_reference"],
            customer_id=transaction["customer_id"],
            direction="debit",  # /v1/score only ever scores outgoing transfers
            amount=transaction["amount"],
            beneficiary_account=transaction["beneficiary_account"],
            beneficiary_bank_code=transaction["beneficiary_bank_code"],
            transaction_type=transaction["transaction_type"],
            medium=transaction["medium"],
            occurred_at=transaction["timestamp"],
            # Persisted (not just used transiently for this score) so settle's
            # background task can read it back and keep location_counts
            # growing from real traffic, not just CSV seeding.
            geolocation_lat=geolocation["lat"] if geolocation else None,
            geolocation_lng=geolocation["lng"] if geolocation else None,
        )
        risk_event = RiskEvent(
            transaction_reference=transaction["transaction_reference"],
            customer_id=transaction["customer_id"],
            score=result["score"],
            decision=result["decision"],
            step_up_method=result["step_up_method"],
            reasons=result["reasons"],
        )

        try:
            self.db.add(txn_row)
            self.db.flush()  # Transaction row must exist before RiskEvent's FK references it
            self.db.add(risk_event)
            self.db.commit()
        except IntegrityError:
            # Concurrent retry landed between our idempotency check and this
            # commit — someone else already persisted this transaction_reference.
            # Don't surface a 500 for what is, from the bank app's perspective,
            # a legitimate retry; return the decision that actually won.
            self.db.rollback()
            cached = self.get_cached_decision(transaction["transaction_reference"])
            if cached is not None:
                return cached
            raise

        return {
            "transaction_reference": transaction["transaction_reference"],
            "score": result["score"],
            "decision": result["decision"],
            "step_up_method": result["step_up_method"],
            "reasons": result["reasons"],
        }


class SettleService:
    """Orchestrates POST /v1/transactions/settle. Split into a fast,
    synchronous part (apply_settlement — durably record the real-world
    outcome) and a slower background part (run_layer2 — River baseline update
    + anomaly detector scoring), matching the System Design doc's Section 5:
    the bank app doesn't wait on Layer 2, only on the outcome being recorded."""

    # verification_outcome values that mean an identity claim was actually
    # REJECTED — not "abandoned", which just means the customer walked away
    # and proves nothing about whether they were legitimate.
    FAILED_VERIFICATION_OUTCOMES = {"liveness_failed", "security_question_failed", "otp_failed"}

    def __init__(self, db_session, redis_client=None):
        self.db = db_session
        self.profile_service = CustomerProfileService(db_session, redis_client)
        self.anomaly_service = AnomalyDetectorService(db_session)

    def get_transaction(self, transaction_reference: str):
        from src.core.models import Transaction  # local import avoids a circular dependency with models.py

        return self.db.exec(
            select(Transaction).where(Transaction.transaction_reference == transaction_reference)
        ).first()

    def apply_settlement(self, txn, final_status: str, verification_outcome: str, nibss_reference: str | None) -> None:
        """The fast, synchronous part — just persists the real-world outcome.
        Caller is responsible for the idempotency check (txn.final_status was
        still None) before calling this; this method itself doesn't re-check."""
        txn.final_status = final_status
        txn.verification_outcome = verification_outcome
        txn.nibss_reference = nibss_reference
        self.db.add(txn)
        self.db.commit()

    def run_layer2(self, transaction_reference: str, final_status: str, verification_outcome: str) -> None:
        """Background-task body — called with its OWN db/redis (see
        routes._run_settle_background), never the request-scoped session.

        Two independent gates on the SAME profile load:
          - a failed step-up verification elevates risk_tier, regardless of
            final_status (a rejected identity claim is a rejected identity
            claim even if the transfer itself technically completed via some
            other path)
          - River/anomaly learning only runs when final_status == "completed"
            — a failed/abandoned transfer never moved real money, so it must
            not shape the spending baseline or train the anomaly model
        Saved once at the end if either gate actually changed anything."""
        from src.core.models import RiskEvent

        txn = self.get_transaction(transaction_reference)
        if txn is None:
            return  # shouldn't happen — the endpoint already validated this exists

        profile = self.profile_service.get_or_create(txn.customer_id)
        changed = False

        if verification_outcome in self.FAILED_VERIFICATION_OUTCOMES:
            profile.elevate_risk()
            changed = True

        if final_status == "completed":
            geolocation = None
            if txn.geolocation_lat is not None and txn.geolocation_lng is not None:
                geolocation = {"lat": txn.geolocation_lat, "lng": txn.geolocation_lng}

            beneficiary_key = f"{txn.beneficiary_account}:{txn.beneficiary_bank_code}"
            transaction_dict = {
                "transaction_type": txn.transaction_type,
                "amount": float(txn.amount),
                "timestamp": txn.occurred_at,
                "new_beneficiary": beneficiary_key not in profile.known_beneficiaries,
            }

            # CRITICAL ORDERING: the anomaly detector reads profile's CURRENT
            # baseline before profile.update() touches it — same rule as
            # everywhere else this codebase does River + anomaly scoring together.
            result = self.anomaly_service.score_transaction(transaction_dict, profile)

            risk_event = self.db.exec(
                select(RiskEvent).where(RiskEvent.transaction_reference == transaction_reference)
            ).first()
            if risk_event is not None:
                risk_event.anomaly_score = result["score"]
                risk_event.anomaly_flagged = result["flagged"]
                risk_event.anomaly_zscore = result["zscore"]
                risk_event.baseline_source = result["baseline_source"]
                self.db.add(risk_event)

            profile.update(
                amount=float(txn.amount),
                hour=txn.occurred_at.hour,
                beneficiary_account=txn.beneficiary_account,
                beneficiary_bank_code=txn.beneficiary_bank_code,
                transaction_type=txn.transaction_type,
                geolocation=geolocation,
            )
            changed = True

        if changed:
            self.profile_service.save(txn.customer_id, profile)
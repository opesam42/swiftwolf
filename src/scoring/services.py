import time
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt, exp
from typing import Any
from dataclasses import dataclass
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from redis.exceptions import RedisError
from src.core.config import settings
from src.core.redis import RedisDep

from src.scoring.models import Decision, RiskEvent, RiskReason
from src.settlement.models import Transaction, TransactionStatus, TransactionType
from src.blacklist.services import BlacklistService
from src.profile.services import CustomerProfileService
from src.profile.repository import CustomerRepository

from src.scoring.utils import pseudonymize

# Every RuleEngine decision maps to the status the Transaction row is saved with
DECISION_TO_STATUS = {
    Decision.PROCEED: TransactionStatus.APPROVED,
    Decision.STEP_UP: TransactionStatus.STEP_UP_REQUIRED,
    Decision.BLOCK: TransactionStatus.BLOCKED,
}


@dataclass(slots=True, frozen=True)
class VelocityResult:
    tx_count: int
    is_velocity_anomaly: bool
    velocity_risk_points: int

class VelocityWindow:
    """ 
    Manages atomic sliding-window velocity counters in Redis ZSET. 
    This class tracks transaction frequency over a short sliding time window to detect rapid-fire account takeover (ATO) or money-mule drains. 
    
    INFRASTRUCTURE & FALLBACK: 
    --------------------------
    Uses an atomic Redis Sorted Set pipeline (ZREMRANGEBYSCORE, ZADD, ZCARD, EXPIRE). If Redis is unavailable (redis=None), gracefully degrades by returning a baseline count of 1 with zero risk points. 
    """

    def __init__(
        self,
        redis: RedisDep | None,
        window_seconds: int | None = None,
        max_tx_threshold: int | None = None,
        penalty_score: int | None = None,
    ):
        self.redis = redis
        self.window_seconds = window_seconds or settings.VELOCITY_WINDOW_SECONDS
        self.max_tx_threshold = max_tx_threshold or settings.VELOCITY_MAX_THRESHOLD
        self.penalty_score = penalty_score or settings.VELOCITY_SCORE_PENALTY

    def record_and_check_velocity(self, customer_id: str, tx_reference: str) -> VelocityResult :
        """ Records a transaction timestamp and checks for burst velocity anomalies. 
        
        ARGS: 
        -----
        customer_id : str 
            Unique customer identifier used for Redis key partitioning (velocity:{id}). 
        tx_reference : str 
            Unique transaction reference. Used as the ZSET value for idempotency (retried requests update timestamps rather than inflating counts). 
        
        RETURNS: 
        -------- 
        VelocityResult: 
            - tx_count (int): 
                Active transactions in the sliding window. 
            - is_velocity_anomaly (bool): 
                True if tx_count >= max_tx_threshold. 
            - velocity_risk_points (int): 
                50 pts if anomaly, else 0 pts. 
        """

        # Graceful fallback if Redis is down or None 
        if self.redis is None:
            return VelocityResult(tx_count=1, is_velocity_anomaly=False, velocity_risk_points=0)

        is_anomaly = False
        risk_points = 0

        key = f"velocity:{customer_id}"
        now = time.time()
        cutoff = now - self.window_seconds

        try:
            # pipeline for batching for network optimization
            pipe = self.redis.pipeline()

            # remove timestamp older than window_seconds ago
            pipe.zremrangebyscore(key, "-inf", cutoff)

            # add current transaction timestamp
            pipe.zadd(key, {tx_reference: now})

            # count the remaining the transaction in the last window_seconds ago
            pipe.zcard(key)

            # set expiration
            pipe.expire(key, self.window_seconds + 60)

            results = pipe.execute()
        except RedisError:
            # Redis down or timed out: fail open so scoring still completes
            return VelocityResult(tx_count=1, is_velocity_anomaly=False, velocity_risk_points=0)

        # output of zcard
        tx_count = results[2]

        if tx_count >= self.max_tx_threshold:
            risk_points = self.penalty_score
            is_anomaly = True

        return VelocityResult(
            tx_count=tx_count,
            is_velocity_anomaly=is_anomaly,
            velocity_risk_points=risk_points,
        )



class RuleEngine:
    """Deep module encapsulating Layer 1 rule evaluation heuristics."""

    def __init__(
        self, 
        blacklisted_accounts: set[str] | None = None,
        amount_score_max_score: int = settings.AMOUNT_SCORE_MAX_SCORE,
        amount_score_steepness: float = settings.AMOUNT_SCORE_STEEPNESS,
        amount_score_midpoint: int = settings.AMOUNT_SCORE_MIDPOINT,
    ):
        self.blacklisted_accounts = blacklisted_accounts or set()
        self.amount_score_max_score = amount_score_max_score
        self.amount_score_steepness = amount_score_steepness
        self.amount_score_midpoint = amount_score_midpoint

    def evaluate(
        self, 
        transaction: dict[str, Any], 
        baseline: dict[str, Any] | None = None,
        velocity_result: VelocityResult | None = None
    ) -> dict[str, Any]:
        if baseline is None:
            # TODO: might need to use a schema - so if anything changes at the customer profile end
            baseline = {
                "known_destinations": [],
                "known_bank_codes": [],
                "category_baselines": {},
                "typical_hours": {},
                "is_cold_start": True,
            }

        is_transfer = transaction["transaction_type"] == TransactionType.TRANSFER

        # Blacklist keys are "{account}:{bank_code}", which only identify NUBAN transfers,
        # where recipient is the account number and provider is the bank code
        if is_transfer:
            blacklist_key = f"{transaction['recipient']}:{transaction['provider']}"
            if blacklist_key in self.blacklisted_accounts:
                return {
                    "score": 999,
                    "decision": Decision.BLOCK,
                    "reasons": [RiskReason.BLACKLISTED_ACCOUNT],
                }

        score = 0
        reasons: list[RiskReason] = []

        if baseline.get("risk_tier") == "elevated":
            score += 15
            reasons.append(RiskReason.ELEVATED_RISK_TIER)

        raw_destination_key = CustomerProfileService.destination_key_for(
            transaction["transaction_type"], transaction["provider"], transaction["recipient"]
        )
        pseudonymized_destination_key = CustomerProfileService.destination_key_for(
            transaction["transaction_type"],
            transaction["provider"],
            pseudonymize(transaction["recipient"]),
        )
        known_destinations = baseline.get("known_destinations", [])
        is_new_destination = (
            raw_destination_key not in known_destinations
            and pseudonymized_destination_key not in known_destinations
        )

        if is_new_destination:
            score += 30
            reasons.append(RiskReason.NEW_BENEFICIARY)

        if is_transfer and transaction["provider"] not in baseline.get("known_bank_codes", []):
            score += 15
            reasons.append(RiskReason.NEW_BANK)

        # SCORE FOR AMOUNT DEVIATION
        cat_baseline = baseline.get("category_baselines", {}).get(transaction["transaction_type"])
        if cat_baseline and cat_baseline.get("std_amount", 0) > 0:
            # Z_SCORE = | amount - mean | / std_amount; baselines are in naira, the payload is in kobo
            amount_naira = transaction["amount"] / 100.0
            z_score = abs(amount_naira - cat_baseline["avg_amount"]) / cat_baseline["std_amount"]
            amount_score = self._calculate_continuous_score(z_score)
            if amount_score > 0:
                score += amount_score
                reasons.append(RiskReason.AMOUNT_DEVIATION)

        # check for transaction velocity spike
        if velocity_result and velocity_result.is_velocity_anomaly:
            score += velocity_result.velocity_risk_points
            reasons.append(RiskReason.HIGH_VELOCITY_BURST) 

        # TODO - would be rewrrtien to fit the histogram stuff
        # hour = transaction["timestamp"].hour
        # if baseline.get("typical_hours") and hour not in baseline["typical_hours"]:
        #     score += 15
        #     reasons.append(RiskReason.UNUSUAL_HOUR)

        session = transaction.get("session")
        if session:
            if session.get("login_to_transfer_seconds", 999) < 2:
                score += 40
                reasons.append(RiskReason.BOT_SPEED_TIMING)
            if session.get("pasted_beneficiary") and is_new_destination:
                score += 10
                reasons.append(RiskReason.PASTED_NEW_BENEFICIARY)

        if baseline.get("is_cold_start") and RiskReason.AMOUNT_DEVIATION in reasons:
            score -= 10

        if transaction.get("last_transaction_timestamp") and RiskReason.AMOUNT_DEVIATION in reasons:
            last_ts = transaction["last_transaction_timestamp"]
            if isinstance(last_ts, str):
                last_ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            current_ts = transaction["timestamp"]
            if last_ts.tzinfo is not None and current_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=None)
            elif last_ts.tzinfo is None and current_ts.tzinfo is not None:
                current_ts = current_ts.replace(tzinfo=None)
            if (current_ts - last_ts).days > 30:
                score += 20
                reasons.append(RiskReason.DORMANT_ACCOUNT_SPIKE)

        geolocation = transaction.get("geolocation")
        if geolocation:
            lat, lng = geolocation["lat"], geolocation["lng"]
            known_cells = baseline.get("known_location_cells", [])
            cell = [round(lat, 1), round(lng, 1)]
            if cell not in known_cells:
                dist = self._nearest_known_distance_km(lat, lng, known_cells)
                if dist is not None:
                    if dist > 500:
                        score += 20
                        reasons.append(RiskReason.LOCATION_DEVIATION_MAJOR)
                    elif dist > 50:
                        score += 10
                        reasons.append(RiskReason.LOCATION_DEVIATION_MINOR)

        return {
            "score": score,
            "decision": self._decide(score),
            "reasons": reasons,
        }

    def _calculate_continuous_score(self, z_score: float) -> int:
        """ Calculates risk points for unusual transaction amounts using a Sigmoid curve. 
        WHY WE USE THIS (Rationale):
        
        Legacy rules used binary thresholds (e.g., "if amount > 2 std_dev, add 25 pts"). 
        This created a "score cliff": a minor 2.01x deviation received the exact same risk penalty as an extreme 15x account-draining transaction. 
        
        The Sigmoid function smoothly ramps up risk points as the deviation grows:
            - Small deviations (<= 1.0x) = 0 pts (normal behavior)
            - Mild deviations (2.0x) ~= 10 pts (low friction) 
            - Moderate (3.0x midpoint) ~= 25 pts (half max risk) 
            - Extreme (>= 6.0x) = 50 pts (capped maximum) 
        PARAMETERS:
            z_score : float How many standard deviations the amount is from the customer's average.
        """

        if z_score <= 1.0:
            return 0 # completely normal transaction
        
        # using sigmoid function
        risk_factor = 1.0 / (1.0 + exp(-self.amount_score_steepness * (z_score - self.amount_score_midpoint)))
        score = self.amount_score_max_score * risk_factor
        return int( round(score, 0) )

    @staticmethod
    def _decide(score: int) -> Decision:
        # On STEP_UP the bank app chooses the verification method, not SwiftWolf
        if score <= 30:
            return Decision.PROCEED
        if score <= 100:
            return Decision.STEP_UP
        return Decision.BLOCK

    @staticmethod
    def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        radius_km = 6371.0
        dlat = radians(lat2 - lat1)
        dlng = radians(lng2 - lng1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
        return radius_km * 2 * atan2(sqrt(a), sqrt(1 - a))

    def _nearest_known_distance_km(self, lat: float, lng: float, known_cells: list[tuple[float, float]]) -> float | None:
        if not known_cells:
            return None
        return min(self._haversine_km(lat, lng, cell[0], cell[1]) for cell in known_cells)

class ScoreService:
    """Orchestrates synchronous scoring end-to-end."""

    def __init__(self, db_session: Session, redis_client=None):
        self.db = db_session
        self.profile_service = CustomerProfileService(db_session, redis_client)
        self.profile_repo = CustomerRepository(db_session, redis_client)
        self.blacklist_service = BlacklistService(db_session, redis_client)
        self.rule_engine = RuleEngine()
        self.velocity_window = VelocityWindow(redis=redis_client)

    def get_cached_decision(self, transaction_reference: str) -> dict | None:
        existing = self.db.exec(
            select(RiskEvent).where(RiskEvent.transaction_reference == transaction_reference)
        ).first()
        if existing is None:
            return None

        return {
            "transaction_reference": existing.transaction_reference,
            "score": existing.score,
            "decision": Decision(existing.decision),
            "reasons": existing.reasons,
        }

    def score(self, transaction: dict) -> dict:
        cached = self.get_cached_decision(transaction["transaction_reference"])
        if cached is not None:
            return cached

        # After this block the customer row is guaranteed to exist, which the
        # Transaction/RiskEvent foreign keys below rely on: a cached baseline is
        # only ever written from an existing row, and a miss creates one.
        baseline = self.profile_repo.get_cached_baseline(transaction["customer_id"])

        if baseline is None:
            profile = self.profile_repo.get_or_create(transaction["customer_id"])
            baseline = profile.to_baseline_dict()

        # transaction velocity check 
        velocity_result = self.velocity_window.record_and_check_velocity(transaction["customer_id"], transaction["transaction_reference"])

        self.rule_engine.blacklisted_accounts = self.blacklist_service.get_active_keys()
        result = self.rule_engine.evaluate(transaction, baseline, velocity_result)

        geolocation = transaction.get("geolocation")
        txn_row = Transaction(
            transaction_reference=transaction["transaction_reference"],
            customer_id=transaction["customer_id"],
            direction="debit",
            amount=transaction["amount"],
            destination_key=CustomerProfileService.destination_key_for(
                transaction["transaction_type"],
                transaction["provider"],
                pseudonymize(transaction["recipient"]),
            ),
            provider=transaction["provider"],
            transaction_type=transaction["transaction_type"],
            medium=transaction["medium"],
            status=DECISION_TO_STATUS[result["decision"]].value,
            occurred_at=transaction["timestamp"],
            geolocation_lat=geolocation["lat"] if geolocation else None,
            geolocation_lng=geolocation["lng"] if geolocation else None,
        )

        # extract biometric if present 
        biometrics = transaction.get("behavioural_biometrics")
        # convert biometric from pydantic model to dict
        if isinstance(biometrics, BaseModel):
            telemetry_payload = biometrics.model_dump()
        else:
            telemetry_payload = biometrics  # already a dict 

        risk_event = RiskEvent(
            transaction_reference=transaction["transaction_reference"],
            customer_id=transaction["customer_id"],
            score=result["score"],
            decision=result["decision"].value,
            reasons=[reason.value for reason in result["reasons"]],
            telemetry=telemetry_payload,
        )

        try:
            self.db.add(txn_row)
            self.db.flush()
            self.db.add(risk_event)
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            cached = self.get_cached_decision(transaction["transaction_reference"])
            if cached is not None:
                return cached
            raise

        return {
            "transaction_reference": transaction["transaction_reference"],
            "score": result["score"],
            "decision": result["decision"],
            "reasons": result["reasons"],
        }

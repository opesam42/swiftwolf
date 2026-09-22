from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from typing import Any
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from src.scoring.models import RiskEvent
from src.settlement.models import Transaction
from src.blacklist.services import BlacklistService
from src.profile.services import CustomerProfileService

class RuleEngine:
    """Deep module encapsulating Layer 1 rule evaluation heuristics."""

    def __init__(self, blacklisted_accounts: set[str] | None = None):
        self.blacklisted_accounts = blacklisted_accounts or set()

    def evaluate(self, transaction: dict[str, Any], baseline: dict[str, Any] | None = None) -> dict[str, Any]:
        if baseline is None:
            baseline = {
                "known_beneficiaries": [],
                "known_bank_codes": [],
                "category_baselines": {},
                "typical_hours": [],
                "is_cold_start": True,
            }

        beneficiary_key = f"{transaction['beneficiary_account']}:{transaction['beneficiary_bank_code']}"
        if beneficiary_key in self.blacklisted_accounts:
            return {
                "score": 999,
                "decision": "BLOCK",
                "step_up_method": None,
                "reasons": ["blacklisted_account"],
            }

        score = 0
        reasons: list[str] = []

        if baseline.get("risk_tier") == "elevated":
            score += 15
            reasons.append("elevated_risk_tier")

        if beneficiary_key not in baseline.get("known_beneficiaries", []):
            score += 30
            reasons.append("new_beneficiary")

        if transaction["beneficiary_bank_code"] not in baseline.get("known_bank_codes", []):
            score += 15
            reasons.append("new_bank")

        cat_baseline = baseline.get("category_baselines", {}).get(transaction["transaction_type"])
        if cat_baseline and cat_baseline.get("std_amount", 0) > 0:
            deviation = abs(transaction["amount"] - cat_baseline["avg_amount"])
            if deviation > 2 * cat_baseline["std_amount"]:
                score += 25
                reasons.append("amount_deviation")

        hour = transaction["timestamp"].hour
        if baseline.get("typical_hours") and hour not in baseline["typical_hours"]:
            score += 15
            reasons.append("unusual_hour")

        session = transaction.get("session")
        if session:
            if session.get("login_to_transfer_seconds", 999) < 2:
                score += 40
                reasons.append("bot_speed_timing")
            if session.get("active_call_detected"):
                score += 50
                reasons.append("active_call")
            if session.get("pasted_beneficiary") and beneficiary_key not in baseline.get("known_beneficiaries", []):
                score += 10
                reasons.append("pasted_new_beneficiary")

        if baseline.get("is_cold_start") and "amount_deviation" in reasons:
            score -= 10

        if transaction.get("last_transaction_timestamp") and "amount_deviation" in reasons:
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
                reasons.append("dormant_account_spike")

        geolocation = transaction.get("geolocation")
        if geolocation:
            lat, lng = geolocation["lat"], geolocation["lng"]
            known_cells = baseline.get("known_location_cells", [])
            cell = (round(lat, 1), round(lng, 1))
            if cell not in known_cells:
                dist = self._nearest_known_distance_km(lat, lng, known_cells)
                if dist is not None:
                    if dist > 500:
                        score += 20
                        reasons.append("location_deviation_major")
                    elif dist > 50:
                        score += 10
                        reasons.append("location_deviation_minor")

        decision = self._decide(score)
        step_up_method = self._pick_step_up_method(decision, transaction["medium"])

        return {
            "score": score,
            "decision": decision,
            "step_up_method": step_up_method,
            "reasons": reasons,
        }

    @staticmethod
    def _decide(score: int) -> str:
        if score <= 30:
            return "PROCEED"
        if score <= 60:
            return "STEP_UP_LIGHT"
        if score <= 100:
            return "STEP_UP_LIVENESS"
        return "BLOCK"

    @staticmethod
    def _pick_step_up_method(decision: str, medium: str) -> str | None:
        if decision == "PROCEED":
            return None
        if decision == "STEP_UP_LIGHT":
            return "otp"
        if medium == "app":
            return "bvn_liveness"
        return "security_question"

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
        return min(self._haversine_km(lat, lng, cell, cell[2]) for cell in known_cells)

class ScoreService:
    """Orchestrates synchronous <50ms scoring end-to-end."""

    def __init__(self, db_session: Session, redis_client=None):
        self.db = db_session
        self.profile_service = CustomerProfileService(db_session, redis_client)
        self.blacklist_service = BlacklistService(db_session, redis_client)
        self.rule_engine = RuleEngine()

    def get_cached_decision(self, transaction_reference: str) -> dict | None:
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
        cached = self.get_cached_decision(transaction["transaction_reference"])
        if cached is not None:
            return cached

        baseline = self.profile_service.get_cached_baseline(transaction["customer_id"])
        if baseline is None:
            profile = self.profile_service.get_or_create(transaction["customer_id"])
            baseline = profile.to_baseline_dict()

        self.rule_engine.blacklisted_accounts = self.blacklist_service.get_active_keys()
        result = self.rule_engine.evaluate(transaction, baseline)

        self.profile_service.ensure_customer_row(transaction["customer_id"])

        geolocation = transaction.get("geolocation")
        txn_row = Transaction(
            transaction_reference=transaction["transaction_reference"],
            customer_id=transaction["customer_id"],
            direction="debit",
            amount=transaction["amount"],
            beneficiary_account=transaction["beneficiary_account"],
            beneficiary_bank_code=transaction["beneficiary_bank_code"],
            beneficiary_name=transaction.get("beneficiary_name"),
            transaction_type=transaction["transaction_type"],
            medium=transaction["medium"],
            occurred_at=transaction["timestamp"],
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
            "step_up_method": result["step_up_method"],
            "reasons": result["reasons"],
        }
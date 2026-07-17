from __future__ import annotations

from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from typing import Any


def _location_grid_cell(lat: float, lng: float, precision: int = 1) -> tuple[float, float]:
    return (round(lat, precision), round(lng, precision))


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_km = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return radius_km * 2 * atan2(sqrt(a), sqrt(1 - a))


def _nearest_known_distance_km(lat: float, lng: float, known_cells: list[tuple[float, float]]) -> float | None:
    if not known_cells:
        return None
    return min(_haversine_km(lat, lng, cell[0], cell[1]) for cell in known_cells)


def score_transaction(transaction: dict[str, Any], baseline: dict[str, Any], blacklisted_accounts: set[str]) -> dict[str, Any]:
    """Pure arithmetic against a cached baseline — no DB calls, no ML inference."""
    beneficiary_key = f"{transaction['beneficiary_account']}:{transaction['beneficiary_bank_code']}"
    if beneficiary_key in blacklisted_accounts:  # blacklisted_accounts should now hold composite keys too
        return {
            "score": 999,
            "decision": "BLOCK",
            "step_up_method": None,
            "reasons": ["blacklisted_account"],
        }

    score = 0
    reasons: list[str] = []

    # Simpler, single-model stand-in for Part 2C's champion/challenger rebase —
    # a customer whose most recent step-up verification failed gets scrutinized
    # harder on every subsequent transaction until CustomerProfile.risk_tier is
    # reset (not built yet — a named scope gap, see CustomerProfile.elevate_risk).
    if baseline.get("risk_tier") == "elevated":
        score += 15
        reasons.append("elevated_risk_tier")

    beneficiary_key = f"{transaction['beneficiary_account']}:{transaction['beneficiary_bank_code']}"

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
            last_ts = last_ts.replace("Z", "+00:00")
            last_ts = datetime.fromisoformat(last_ts)
        current_ts = transaction["timestamp"]
        # Normalize: if one side is naive and the other isn't, force both naive
        # for comparison purposes — safer than assuming either side's tz-awareness
        if last_ts.tzinfo is not None and current_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=None)
        elif last_ts.tzinfo is None and current_ts.tzinfo is not None:
            current_ts = current_ts.replace(tzinfo=None)
        dormancy_days = (current_ts - last_ts).days
        if dormancy_days > 30:
            score += 20
            reasons.append("dormant_account_spike")

    geolocation = transaction.get("geolocation")
    if geolocation:
        lat = geolocation["lat"]
        lng = geolocation["lng"]
        known_cells = baseline.get("known_location_cells", [])
        cell = _location_grid_cell(lat, lng)
        if cell not in known_cells:
            distance = _nearest_known_distance_km(lat, lng, known_cells)
            if distance is None:
                pass
            elif distance > 500:
                score += 20
                reasons.append("location_deviation_major")
            elif distance > 50:
                score += 10
                reasons.append("location_deviation_minor")

    decision = _decide(score)
    step_up_method = _pick_step_up_method(decision, transaction["medium"])

    return {
        "score": score,
        "decision": decision,
        "step_up_method": step_up_method,
        "reasons": reasons,
    }


def _decide(score: int) -> str:
    if score <= 30:
        return "PROCEED"
    if score <= 60:
        return "STEP_UP_LIGHT"
    if score <= 100:
        return "STEP_UP_LIVENESS"
    return "BLOCK"


def nearest_known_distance_km(lat: float, lng: float, known_cells: list[list[float]]) -> float | None:
    if not known_cells:
        return None

    def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        radius_km = 6371
        dlat = radians(lat2 - lat1)
        dlng = radians(lng2 - lng1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
        return radius_km * 2 * atan2(sqrt(a), sqrt(1 - a))

    return min(haversine_km(lat, lng, cell[0], cell[1]) for cell in known_cells)


def _pick_step_up_method(decision: str, medium: str) -> str | None:
    if decision == "PROCEED":
        return None
    if decision == "STEP_UP_LIGHT":
        return "otp"
    if medium == "app":
        return "bvn_liveness"
    return "security_question"

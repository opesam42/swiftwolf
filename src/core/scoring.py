from __future__ import annotations

from typing import Any


def score_transaction(transaction: dict[str, Any], baseline: dict[str, Any], blacklisted_accounts: set[str]) -> dict[str, Any]:
    """Pure arithmetic against a cached baseline — no DB calls, no ML inference."""
    if transaction["beneficiary_account"] in blacklisted_accounts:
        return {
            "score": 999,
            "decision": "BLOCK",
            "step_up_method": None,
            "reasons": ["blacklisted_account"],
        }

    score = 0
    reasons: list[str] = []

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


def _pick_step_up_method(decision: str, medium: str) -> str | None:
    if decision == "PROCEED":
        return None
    if decision == "STEP_UP_LIGHT":
        return "otp"
    if medium == "app":
        return "bvn_liveness"
    return "security_question"

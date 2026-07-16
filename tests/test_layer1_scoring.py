import unittest
from datetime import datetime, timezone

from src.core.repository import CustomerProfile
from src.core.scoring import score_transaction


class Layer1ScoringTests(unittest.TestCase):
    def test_known_transfer_is_allowed(self):
        baseline = {
            "known_beneficiaries": ["0123456789:058"],
            "known_bank_codes": ["058"],
            "category_baselines": {
                "transfer": {"avg_amount": 100000.0, "std_amount": 1000.0, "source": "category"}
            },
            "typical_hours": [9, 10],
            "is_cold_start": False,
        }
        transaction = {
            "beneficiary_account": "0123456789",
            "beneficiary_bank_code": "058",
            "amount": 100000.0,
            "timestamp": datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc),
            "transaction_type": "transfer",
            "medium": "app",
            "session": {"login_to_transfer_seconds": 10.0, "active_call_detected": False, "pasted_beneficiary": False},
        }

        result = score_transaction(transaction, baseline, blacklisted_accounts=set())

        self.assertEqual(result["score"], 0)
        self.assertEqual(result["decision"], "PROCEED")
        self.assertIsNone(result["step_up_method"])
        self.assertEqual(result["reasons"], [])

    def test_scene_two_blocks_new_beneficiary_with_risk_signals(self):
        baseline = {
            "known_beneficiaries": ["1111111111:058"],
            "known_bank_codes": ["058"],
            "category_baselines": {
                "transfer": {"avg_amount": 100000.0, "std_amount": 1000.0, "source": "category"}
            },
            "typical_hours": [9, 10],
            "is_cold_start": False,
        }
        transaction = {
            "beneficiary_account": "0123456789",
            "beneficiary_bank_code": "999",
            "amount": 200000.0,
            "timestamp": datetime(2026, 7, 11, 2, 17, tzinfo=timezone.utc),
            "transaction_type": "transfer",
            "medium": "app",
            "session": {"login_to_transfer_seconds": 1.4, "active_call_detected": True, "pasted_beneficiary": True},
        }

        result = score_transaction(transaction, baseline, blacklisted_accounts=set())

        self.assertEqual(result["score"], 185)
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["step_up_method"], "bvn_liveness")
        self.assertIn("new_beneficiary", result["reasons"])
        self.assertIn("new_bank", result["reasons"])
        self.assertIn("amount_deviation", result["reasons"])
        self.assertIn("unusual_hour", result["reasons"])
        self.assertIn("bot_speed_timing", result["reasons"])
        self.assertIn("active_call", result["reasons"])
        self.assertIn("pasted_new_beneficiary", result["reasons"])

    def test_dormant_account_spike_adds_signal(self):
        baseline = {
            "known_beneficiaries": ["0123456789:058"],
            "known_bank_codes": ["058"],
            "category_baselines": {
                "transfer": {"avg_amount": 100000.0, "std_amount": 1000.0, "source": "category"}
            },
            "typical_hours": [9, 10],
            "known_location_cells": [(6.5, 3.4)],
            "is_cold_start": False,
        }
        transaction = {
            "beneficiary_account": "0123456789",
            "beneficiary_bank_code": "058",
            "amount": 200000.0,
            "timestamp": datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc),
            "last_transaction_timestamp": datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            "transaction_type": "transfer",
            "medium": "app",
            "session": {"login_to_transfer_seconds": 10.0, "active_call_detected": False, "pasted_beneficiary": False},
        }

        result = score_transaction(transaction, baseline, blacklisted_accounts=set())

        self.assertEqual(result["decision"], "STEP_UP_LIGHT")
        self.assertIn("dormant_account_spike", result["reasons"])
        self.assertIn("amount_deviation", result["reasons"])

    def test_location_deviation_major_is_scored(self):
        baseline = {
            "known_beneficiaries": ["0123456789:058"],
            "known_bank_codes": ["058"],
            "category_baselines": {
                "transfer": {"avg_amount": 100000.0, "std_amount": 1000.0, "source": "category"}
            },
            "typical_hours": [9, 10],
            "known_location_cells": [(6.5, 3.4)],
            "is_cold_start": False,
        }
        transaction = {
            "beneficiary_account": "0123456789",
            "beneficiary_bank_code": "058",
            "amount": 100000.0,
            "timestamp": datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc),
            "transaction_type": "transfer",
            "medium": "app",
            "session": {"login_to_transfer_seconds": 10.0, "active_call_detected": False, "pasted_beneficiary": False},
            "geolocation": {"lat": 9.0, "lng": 7.0},
        }

        result = score_transaction(transaction, baseline, blacklisted_accounts=set())

        self.assertIn("location_deviation_minor", result["reasons"])

    def test_blacklisted_account_uses_security_question(self):
        baseline = {
            "known_beneficiaries": ["0123456789:058"],
            "known_bank_codes": ["058"],
            "category_baselines": {
                "transfer": {"avg_amount": 100000.0, "std_amount": 1000.0, "source": "category"}
            },
            "typical_hours": [9, 10],
            "known_location_cells": [(6.5, 3.4)],
            "is_cold_start": False,
        }
        transaction = {
            "beneficiary_account": "9999999999",
            "beneficiary_bank_code": "999",
            "amount": 150000.0,
            "timestamp": datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc),
            "transaction_type": "transfer",
            "medium": "ussd",
            "session": None,
        }

        result = score_transaction(transaction, baseline, blacklisted_accounts={"9999999999"})

        self.assertEqual(result["score"], 999)
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIsNone(result["step_up_method"])
        self.assertEqual(result["reasons"], ["blacklisted_account"])


if __name__ == "__main__":
    unittest.main()

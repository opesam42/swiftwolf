from src.settlement.models import Transaction
from src.scoring.utils import pseudonymize


def test_pseudonymize_is_deterministic_for_the_same_input():
    recipient = "0123456789"

    pseudonym = pseudonymize(recipient)
    assert pseudonym == pseudonymize(recipient)
    assert len(pseudonym) == 67

    destination_key = f"transfer:058:{pseudonym}"
    max_column_length = Transaction.__table__.c.destination_key.type.length
    assert len(destination_key) <= max_column_length


def test_pseudonymize_changes_when_the_input_changes():
    assert pseudonymize("0123456789") != pseudonymize("9876543210")


def test_pseudonymize_includes_the_requested_version():
    assert pseudonymize("0123456789", version="v2").startswith("v2:")

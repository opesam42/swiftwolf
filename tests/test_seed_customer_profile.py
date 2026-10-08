from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from jobs.seed_customer_profile import seed_profile
from jobs.statement_parser import parse_statement_csv
from jobs.type_mapper import resolve_transaction_type
from src.core.config import settings
from src.profile.models import Customer
from src.profile.services import CustomerProfileService
from src.scoring.utils import pseudonymize
from src.settlement.models import Transaction, TransactionChannel, TransactionType


SNIPPET = """date,time,amount_kobo,transaction_id,recipient,provider,detail
2026-10-07,06:06:06,30000,63bxvksab01,09057339147,GLO,Buy Data bundle
2026-10-04,06:06:45,30000,63bsblv830j,09169551050,MTN,Buy Data bundle
2026-10-02,17:31:23,9200,133bphyx2800,09057339147,GLO,Top up Airtime
2026-10-01,15:34:27,74400,63bnhw1d8006,09169551050,MTN,Buy Data bundle
2026-10-01,06:07:52,30000,63bmrnq1104,09057339147,GLO,Buy Data bundle
2026-09-29,19:44:24,58000,63bk44md036,09169551050,MTN,Buy Data bundle
2026-09-29,15:09:24,300000,033bjrea6506,5767940304,000012,Send to A. Y. AMAMA VENTURES
2026-09-29,13:16:57,300000,033bjm6v1404,9914468578,000016,Send to SOMEONE
2026-09-29,09:00:34,60000,033bjabk9a01,4498300165,000010,Send to SOMEONE ELSE
2026-09-28,23:52:44,198000,63bikyi67006,,,
"""


def _write_csv(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "stmt.csv"
    path.write_text(body, encoding="utf-8")
    return path


def test_type_mapper_uses_detail_and_explicit_type():
    assert resolve_transaction_type(transaction_type=None, detail="Buy Data bundle") is TransactionType.DATA
    assert resolve_transaction_type(transaction_type=None, detail="Top up Airtime") is TransactionType.AIRTIME
    assert resolve_transaction_type(transaction_type=None, detail="Send to A. Y. AMAMA VENTURES") is TransactionType.TRANSFER
    assert resolve_transaction_type(transaction_type="DATA", detail="ignored") is TransactionType.DATA
    assert resolve_transaction_type(transaction_type=None, detail="Random POS purchase") is None


def test_parser_skips_empty_recipient_and_sorts_oldest_first(tmp_path: Path):
    result = parse_statement_csv(_write_csv(tmp_path, SNIPPET))

    assert len(result.rows) == 9
    assert len(result.skipped) == 1
    assert result.skipped[0].transaction_id == "63bikyi67006"
    assert "recipient" in result.skipped[0].reason
    assert result.rows[0].transaction_id == "033bjabk9a01"
    assert result.rows[-1].transaction_id == "63bxvksab01"
    assert result.rows[0].occurred_at.tzinfo == ZoneInfo("Africa/Lagos")
    assert result.rows[2].transaction_type is TransactionType.TRANSFER
    assert all(row.medium == TransactionChannel.STATEMENT.value for row in result.rows)


def test_parser_accepts_explicit_transaction_type_column(tmp_path: Path):
    csv = (
        "date,time,amount_kobo,transaction_id,recipient,provider,transaction_type\n"
        "2026-10-07,06:06:06,30000,63bxvksab01,09057339147,GLO,data\n"
    )
    result = parse_statement_csv(_write_csv(tmp_path, csv))
    assert len(result.rows) == 1
    assert result.rows[0].transaction_type is TransactionType.DATA


def test_seed_writes_hmac_destination_key(tmp_path: Path, db_session: Session, fake_redis):
    csv = (
        "date,time,amount_kobo,transaction_id,recipient,provider,transaction_type\n"
        "2026-10-07,06:06:06,30000,SEED_HASH_1,09057339147,GLO,data\n"
    )
    report = seed_profile(
        _write_csv(tmp_path, csv),
        "CUST_SEED_HASH",
        db=db_session,
        redis_client=fake_redis,
    )

    txn = db_session.exec(select(Transaction).where(Transaction.transaction_reference == "SEED_HASH_1")).one()
    expected = CustomerProfileService.destination_key_for(
        TransactionType.DATA, "GLO", pseudonymize("09057339147")
    )
    assert report.inserted == 1
    assert report.settled == 1
    assert txn.destination_key == expected
    assert "09057339147" not in txn.destination_key
    assert txn.medium == TransactionChannel.STATEMENT.value
    assert txn.is_settled is True


def test_seed_rerun_does_not_double_count(tmp_path: Path, db_session: Session, fake_redis):
    path = _write_csv(tmp_path, SNIPPET)
    first = seed_profile(path, "CUST_SEED_RERUN", db=db_session, redis_client=fake_redis)
    second = seed_profile(path, "CUST_SEED_RERUN", db=db_session, redis_client=fake_redis)

    assert first.inserted == 9
    assert first.settled == 9
    assert second.inserted == 0
    assert second.settled == 0
    assert second.skipped_existing == 9

    customer = db_session.exec(select(Customer).where(Customer.customer_id == "CUST_SEED_RERUN")).one()
    total = sum(stats.get("count", 0) for stats in customer.category_baselines.values())
    assert total == 9


def test_seed_leaves_cold_start_after_threshold(tmp_path: Path, db_session: Session, fake_redis):
    threshold = settings.COLD_START_MIN_SETTLED_TRANSACTIONS
    lines = ["date,time,amount_kobo,transaction_id,recipient,provider,transaction_type"]
    for i in range(threshold):
        lines.append(f"2026-09-01,09:00:00,{30000 + i},SEED_COLD_{i:02d},09057339147,GLO,data")
    report = seed_profile(
        _write_csv(tmp_path, "\n".join(lines) + "\n"),
        "CUST_SEED_COLD",
        db=db_session,
        redis_client=fake_redis,
    )
    assert report.is_cold_start is False
    assert report.settled == threshold


def test_dry_run_does_not_write(tmp_path: Path, db_session: Session, fake_redis):
    report = seed_profile(
        _write_csv(tmp_path, SNIPPET),
        "CUST_SEED_DRY",
        db=db_session,
        redis_client=fake_redis,
        dry_run=True,
    )
    assert report.dry_run is True
    assert report.parsed == 9
    assert report.inserted == 0
    assert db_session.exec(select(Customer).where(Customer.customer_id == "CUST_SEED_DRY")).first() is None
    assert db_session.exec(select(Transaction)).first() is None


def test_score_after_seed_does_not_flag_known_destination(
    tmp_path: Path, client, auth_headers, db_session: Session, fake_redis
):
    csv = (
        "date,time,amount_kobo,transaction_id,recipient,provider,transaction_type\n"
        "2026-10-07,06:06:06,30000,SEED_KNOWN_1,09057339147,GLO,data\n"
    )
    seed_profile(
        _write_csv(tmp_path, csv),
        "CUST_SEED_KNOWN",
        db=db_session,
        redis_client=fake_redis,
    )

    response = client.post(
        "/v1/score",
        json={
            "transaction_reference": "LIVE_AFTER_SEED",
            "customer_id": "CUST_SEED_KNOWN",
            "provider": "GLO",
            "recipient": "09057339147",
            "amount": 30000,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "transaction_type": "data",
            "medium": "app",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert "new_beneficiary" not in response.json()["reasons"]

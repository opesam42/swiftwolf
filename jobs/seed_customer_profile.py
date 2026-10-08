import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session

from jobs.statement_parser import NormalizedRow, SkippedRow, parse_statement_csv
from src.profile.repository import CustomerRepository
from src.profile.services import CustomerProfileService
from src.scoring.utils import pseudonymize
from src.settlement.models import Transaction, TransactionChannel, TransactionStatus
from src.settlement.repository import TransactionRepository
from src.settlement.services import SettleService


@dataclass
class SeedReport:
    parsed: int = 0
    inserted: int = 0
    settled: int = 0
    skipped_existing: int = 0
    skipped_invalid: int = 0
    skipped: list[SkippedRow] = field(default_factory=list)
    dry_run: bool = False
    is_cold_start: bool | None = None
    known_destinations_count: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)

    def summary_lines(self) -> list[str]:
        lines = [
            f"{'Dry-run' if self.dry_run else 'Seed'} complete.",
            f"  parsed:            {self.parsed}",
            f"  inserted:          {self.inserted}",
            f"  settled:           {self.settled}",
            f"  skipped existing:  {self.skipped_existing}",
            f"  skipped invalid:   {self.skipped_invalid}",
        ]
        if self.is_cold_start is not None:
            lines.append(f"  is_cold_start:     {self.is_cold_start}")
            lines.append(f"  known destinations:{self.known_destinations_count}")
            if self.category_counts:
                counts = ", ".join(f"{name}={count}" for name, count in sorted(self.category_counts.items()))
                lines.append(f"  category counts:   {counts}")
        if self.skipped:
            lines.append("  skipped rows:")
            for row in self.skipped:
                ref = row.transaction_id or "?"
                lines.append(f"    line {row.source_line} ({ref}): {row.reason}")
        return lines


def seed_reference(customer_id: str, csv_transaction_id: str) -> str:
    """Stable per-(customer, statement row) ledger id. Always under 64 characters.

    The CSV transaction_id is PalmPay's id. SwiftWolf's transaction_reference is
    global and unique, so the same statement can seed more than one customer.
    """
    digest = hashlib.sha256(f"{customer_id}:{csv_transaction_id}".encode()).hexdigest()[:16]
    return f"seed-{digest}"


def seed_profile(
    csv_path: str | Path,
    customer_id: str,
    *,
    db: Session,
    redis_client: Any = None,
    default_medium: str = TransactionChannel.STATEMENT.value,
    timezone_name: str = "Africa/Lagos",
    dry_run: bool = False,
    limit: int | None = None,
) -> SeedReport:
    """Insert historical statement rows, then settle them to warm the baseline.

    Does not score. Live /v1/score is for real payments after this job.
    """
    if not customer_id or not customer_id.strip() or len(customer_id) > 64:
        raise ValueError("customer_id must be 1-64 characters")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")

    parsed = parse_statement_csv(
        csv_path,
        default_medium=default_medium,
        timezone_name=timezone_name,
    )
    rows = parsed.rows[:limit] if limit is not None else parsed.rows

    report = SeedReport(
        parsed=len(rows),
        skipped_invalid=len(parsed.skipped),
        skipped=list(parsed.skipped),
        dry_run=dry_run,
    )
    if dry_run or not rows:
        return report

    customer_repo = CustomerRepository(db, redis_client)
    txn_repo = TransactionRepository(db)
    settle_service = SettleService(db, redis_client, repository=txn_repo)

    customer_repo.get_or_create(customer_id)

    for row in rows:
        _seed_one(row, customer_id, txn_repo, settle_service, report)

    _fill_baseline_snapshot(report, customer_repo.get(customer_id))
    return report


def _seed_one(
    row: NormalizedRow,
    customer_id: str,
    txn_repo: TransactionRepository,
    settle_service: SettleService,
    report: SeedReport,
) -> None:
    ref = seed_reference(customer_id, row.transaction_id)
    existing = txn_repo.get(ref)
    if existing is None:
        txn_repo.save(_build_transaction(row, customer_id, ref))
        report.inserted += 1
    elif existing.is_settled:
        report.skipped_existing += 1
        return

    settle_service.settle(
        {
            "transaction_reference": ref,
            "settled_at": row.occurred_at,
            "status": "SUCCESS",
        }
    )
    report.settled += 1


def _build_transaction(row: NormalizedRow, customer_id: str, transaction_reference: str) -> Transaction:
    return Transaction(
        transaction_reference=transaction_reference,
        customer_id=customer_id,
        direction="debit",
        amount=row.amount_kobo,
        destination_key=CustomerProfileService.destination_key_for(
            row.transaction_type,
            row.provider,
            pseudonymize(row.recipient),
        ),
        provider=row.provider,
        transaction_type=row.transaction_type.value,
        medium=row.medium,
        status=TransactionStatus.APPROVED.value,
        is_settled=False,
        occurred_at=row.occurred_at,
    )


def _fill_baseline_snapshot(report: SeedReport, customer) -> None:
    if customer is None:
        return
    report.is_cold_start = customer.is_cold_start
    report.known_destinations_count = len(customer.known_destinations or [])
    report.category_counts = {
        name: stats.get("count", 0)
        for name, stats in (customer.category_baselines or {}).items()
    }

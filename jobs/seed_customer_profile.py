"""
jobs/seed_customer_profile.py — reads a swiftwolf_seed.csv-shaped file and
seeds a CustomerProfile, the shared ZScoreAnomalyDetector, AND a raw
Transaction row per well-formed CSV row (the audit trail of what was actually
fed into training) — one Postgres read per model, one Postgres write per
model/table, regardless of row count.

The anomaly detector learns from every trainable row unconditionally via
detector.seed_one() — historical seed data is presumed legitimate, so
process()'s self-filtering guard (a LIVE-scoring protection against learning
disguised fraud) has no role here. flag_threshold is only decided AFTER the
whole seed run, from the distribution of scores that run itself produced
(detector.calibrate_from_scores()) — self-filtering only starts applying from
that point forward, against genuinely new incoming transactions.

Deliberately Postgres-only, no Redis: Redis is Layer 1's live hot-path cache,
which has no role in offline/batch seeding. CustomerProfileService is called
here with no redis_client, so its Redis write is skipped entirely (see its
docstring) — the HTTP API layer is where a real redis_client belongs.

Usage:
    python jobs/seed_customer_profile.py path/to/swiftwolf_seed.csv
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

# Allow running this script directly (python jobs/seed_customer_profile.py ...)
# from any working directory — Python only puts this file's own directory on
# sys.path by default, not the project root the src.* imports below need.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.models import Transaction
from src.core.services import AnomalyDetectorService, CustomerProfileService, TransactionService


def load_rows(csv_path: str) -> list[dict]:
    rows = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def seed_profile(
    csv_path: str,
    profile_service: CustomerProfileService,
    anomaly_service: AnomalyDetectorService,
    transaction_service: TransactionService,
    calibration_percentile: float = 95,
    customer_id_override: str | None = None,
) -> None:
    rows = load_rows(csv_path)
    if not rows:
        print(f"No rows found in {csv_path}")
        return

    # Sanity check: every row should belong to the same customer — a quick guard against accidentally pointing this job at the wrong file.
    customer_ids = {r["customer_id"] for r in rows}
    if len(customer_ids) > 1:
        raise ValueError(
            f"Expected one customer_id in {csv_path}, found {len(customer_ids)}: {customer_ids}"
        )
    # customer_id_override lets the onboarding flow relabel a shared demo
    # dataset (e.g. "cust_gbenga_demo") onto a brand-new signup's real
    # customer_id — the CSV's own value is still validated above (guards
    # against pointing this job at the wrong file), just not what gets used.
    customer_id = customer_id_override or customer_ids.pop()

    # CRITICAL: the source statement is newest-first — sort ascending before
    # doing anything else, or recurring-gap calculations and any "what came
    # before this transaction" logic silently breaks.
    rows.sort(key=lambda r: r["timestamp"])

    # First pass: parse every row once into (a) a Transaction row for the full
    # audit trail, and (b) — for well-formed debit/categorized rows only — a
    # plain dict fed to the training loop below.
    skipped, malformed = 0, 0
    transactions_to_persist = []
    trainable_rows = []
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(row["timestamp"])
            amount = float(row["amount"])
        except (KeyError, ValueError) as e:
            # Don't let one malformed row kill a 1000+ row job partway through.
            # Malformed rows are never persisted — there's nothing valid to record.
            print(f"Skipping malformed row: {row} ({e})")
            malformed += 1
            continue

        # Persist the raw row regardless of whether it's trained on below —
        # this is the audit trail of what the statement actually contained,
        # not just what the models happened to learn from. medium is a
        # sentinel here since this row never went through /v1/score.
        transactions_to_persist.append(Transaction(
            transaction_reference=TransactionService.make_seed_reference(
                customer_id, timestamp, amount, row["beneficiary_account"],
            ),
            customer_id=customer_id,
            direction=row["direction"],
            amount=amount,
            beneficiary_account=row["beneficiary_account"],
            beneficiary_bank_code=row["beneficiary_bank_code"],
            # .get(), not [] — older seed CSVs generated before this column
            # existed shouldn't crash the whole job on a missing key.
            beneficiary_name=row.get("beneficiary_name"),
            transaction_type=row["transaction_type"],
            medium=TransactionService.MEDIUM_HISTORICAL_SEED,
            occurred_at=timestamp,
        ))

        if row["direction"] != "debit" or row["transaction_type"] == "uncategorized":
            # CustomerProfile is a spending baseline only — it has no income_mean counterpart (yet), so credit rows are deliberately excluded rather than blended into it.
            # rows with which the transactin type is marked as uncategorized is not fed to the db nor model
            skipped += 1
            continue

        trainable_rows.append({
            "transaction_type": row["transaction_type"],
            "amount": amount,
            "timestamp": timestamp,
            "beneficiary_account": row["beneficiary_account"],
            "beneficiary_bank_code": row["beneficiary_bank_code"],
            "beneficiary_name": row.get("beneficiary_name"),
        })

    # Bulk pattern for BOTH stateful models: ONE Postgres read each, loop
    # calling profile.update()/detector.seed_one() directly (not
    # profile_service.record_transaction() / anomaly_service.score_transaction(),
    # which would each do a read+write PER ROW — correct but needlessly slow for
    # 1000+ rows), ONE save each at the end.
    profile = profile_service.get_or_create(customer_id)
    detector = anomaly_service.get_or_create()

    seeded = 0
    scores = []
    for t in trainable_rows:
        beneficiary_key = f"{t['beneficiary_account']}:{t['beneficiary_bank_code']}"

        # CRITICAL ORDERING: seed_one() reads profile's CURRENT baseline and
        # known-beneficiary set — both must be computed BEFORE profile.update()
        # touches this same row, or the transaction dilutes its own baseline /
        # marks itself as an already-known beneficiary before it's scored.
        transaction = {
            "transaction_type": t["transaction_type"],
            "amount": t["amount"],
            "timestamp": t["timestamp"],
            "new_beneficiary": beneficiary_key not in profile.known_beneficiaries,
        }
        # seed_one(), not process(): historical data is presumed legitimate, so
        # every row is learned from unconditionally — process()'s self-filtering
        # guard is a live-scoring protection that doesn't apply here. The score
        # is collected instead of checked against a threshold, since there's no
        # threshold to check against yet.
        scores.append(detector.seed_one(transaction, profile))

        profile.update(
            amount=t["amount"],
            hour=t["timestamp"].hour,
            beneficiary_account=t["beneficiary_account"],
            beneficiary_bank_code=t["beneficiary_bank_code"],
            transaction_type=t["transaction_type"],
            beneficiary_name=t["beneficiary_name"],
        )
        seeded += 1

    # NOW calibrate: flag_threshold is set from the distribution of scores this
    # SAME detector just produced over real seed data, applying self-filtering
    # only from this point forward, against genuinely new incoming transactions.
    flag_threshold = detector.calibrate_from_scores(scores, percentile=calibration_percentile)
    flagged = sum(1 for s in scores if s >= flag_threshold)

    profile_service.save(customer_id, profile)
    anomaly_service.save(detector)
    persisted_count = transaction_service.save_many(transactions_to_persist)

    print(f"Seeded {seeded} rows for {customer_id} ({skipped} skipped, {malformed} malformed)")
    print(f"Final transaction_count: {profile.transaction_count}")
    print(f"is_cold_start: {profile.transaction_count < 10}")
    print(f"Calibrated flag_threshold: {flag_threshold:.4f} (percentile={calibration_percentile})")
    print(
        f"Anomaly detector: {detector.learned_count} learned unconditionally during seeding, "
        f"{flagged} would have exceeded the calibrated threshold retroactively"
    )
    print(f"Transaction rows persisted: {persisted_count} (of {len(transactions_to_persist)} well-formed)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python jobs/seed_customer_profile.py path/to/seed.csv")
        sys.exit(1)
    # To run command
    # python -m jobs.seed_customer_profile <csv_file>

    from src.core import models  # noqa: F401 — import registers tables on SQLModel.metadata
    from src.database import engine, create_db_and_tables  # reuse the app's own engine, don't build a second one

    from sqlmodel import Session

    create_db_and_tables()

    with Session(engine) as db:
        profile_service = CustomerProfileService(db)  # no redis_client — seeding doesn't need the hot-path cache
        anomaly_service = AnomalyDetectorService(db)
        transaction_service = TransactionService(db)
        seed_profile(sys.argv[1], profile_service, anomaly_service, transaction_service)

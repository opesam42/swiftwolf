"""
jobs/reseed_cust_demo_anonymized.py — reset cust_demo to a clean slate and
re-seed it with jobs/seed_customer_profile.py's now-anonymized names, against
an EXPLICITLY chosen database target. Never falls back to settings.DATABASE_URL
(the "which database did that just run against" bug) — connects using the raw
URL for whichever --target was passed, read straight from .env via
dotenv.dotenv_values(), completely independent of src.database.engine.

Usage:
    python jobs/reseed_cust_demo_anonymized.py --target local
    python jobs/reseed_cust_demo_anonymized.py --target neon
"""
import argparse
import sys
from pathlib import Path

# Allow running this script directly from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CUSTOMER_ID = "cust_demo"
CSV_PATH = "jobs/files/csv/gbenga_palmpay_stmt_swiftwolf_seed.csv"

TARGET_ENV_KEYS = {
    "local": "DATABASE_URL2",
    "neon": "DATABASE_URL",
}


def get_target_url(target: str) -> str:
    from dotenv import dotenv_values

    env_path = Path(__file__).resolve().parent.parent / ".env"
    values = dotenv_values(env_path)
    key = TARGET_ENV_KEYS[target]
    url = values.get(key)
    if not url:
        raise RuntimeError(f"{key} not found in {env_path}")
    return url


def check_anomaly_model_is_cust_demo_only(db) -> None:
    """Refuses to delete anomaly_model_state unless cust_demo is the ONLY
    customer this target has ever recorded real activity for — per the plan,
    this must be re-verified on EACH run, not assumed true from earlier."""
    from sqlmodel import select

    from src.core.models import Customer

    others = db.exec(
        select(Customer.customer_id).where(Customer.customer_id != CUSTOMER_ID)
    ).all()
    if others:
        raise RuntimeError(
            f"Refusing to delete anomaly_model_state: found other customer_id(s) "
            f"on this target besides {CUSTOMER_ID!r}: {others}. This model is "
            f"population-level — deleting it would also erase whatever those "
            f"other customers contributed."
        )


def reset_and_reseed(target: str, keep_anomaly_model: bool = False) -> None:
    from sqlalchemy import create_engine, text
    from sqlmodel import Session

    from src.core import models  # noqa: F401 — registers tables on SQLModel.metadata
    from src.core.services import AnomalyDetectorService, CustomerProfileService, TransactionService
    from src.redis import get_redis_client
    from jobs.seed_customer_profile import seed_profile

    url = get_target_url(target)
    engine = create_engine(url, echo=False)

    print(f"\n=== TARGET: {target} ({TARGET_ENV_KEYS[target]}) ===")

    with Session(engine) as db:
        if not keep_anomaly_model:
            # Step: safety check BEFORE any deletion — re-verified every run.
            check_anomaly_model_is_cust_demo_only(db)
            print(f"[{target}] Safety check passed: {CUSTOMER_ID} is the only customer on this target.")
        else:
            print(f"[{target}] keep_anomaly_model=True — leaving anomaly_model_state untouched "
                  f"(other real customers exist on this target).")

        # Step: full reset — child rows first (FK order), then the customer
        # row itself. Existing transaction rows must actually be deleted, not
        # just left in place: make_seed_reference() is deterministic, so
        # re-running the same CSV against the same customer_id regenerates
        # the SAME transaction_reference values, and TransactionService
        # .save_many() SKIPS rows whose reference already exists — meaning a
        # profile_json-only reset would leave the OLD real names sitting
        # untouched in the transactions table, failing the whole point of
        # this reseed.
        risk_deleted = db.execute(
            text("DELETE FROM risk_events WHERE customer_id = :cid"), {"cid": CUSTOMER_ID}
        )
        txn_deleted = db.execute(
            text("DELETE FROM transactions WHERE customer_id = :cid"), {"cid": CUSTOMER_ID}
        )
        db.execute(text("DELETE FROM customers WHERE customer_id = :cid"), {"cid": CUSTOMER_ID})
        db.commit()
        print(f"[{target}] Deleted {risk_deleted.rowcount} risk_events, {txn_deleted.rowcount} transactions, "
              f"and the customers row for {CUSTOMER_ID}.")

        if not keep_anomaly_model:
            # Step: delete the population-level anomaly model singleton — safe,
            # since the check above just confirmed cust_demo is the only
            # customer this target has ever trained it on.
            anomaly_deleted = db.execute(text("DELETE FROM anomaly_model_state"))
            db.commit()
            print(f"[{target}] Deleted {anomaly_deleted.rowcount} anomaly_model_state row(s).")

        # Step: re-seed from scratch, with a REAL redis_client so save()
        # refreshes baseline:cust_demo in Redis as part of the same run.
        redis_client = get_redis_client()
        profile_service = CustomerProfileService(db, redis_client)
        anomaly_service = AnomalyDetectorService(db)
        transaction_service = TransactionService(db)
        seed_profile(CSV_PATH, profile_service, anomaly_service, transaction_service)

    print(f"[{target}] Re-seed complete.")


def verify(target: str) -> dict:
    from sqlalchemy import create_engine, text
    from sqlmodel import Session

    url = get_target_url(target)
    engine = create_engine(url, echo=False)

    with Session(engine) as db:
        row = db.execute(
            text("SELECT profile_json->'beneficiary_names', profile_json->'category_stats'->'transfer', "
                 "transaction_count FROM customers WHERE customer_id = :cid"),
            {"cid": CUSTOMER_ID},
        ).first()
        beneficiary_names, transfer_stats, transaction_count = row

        txn_names = db.execute(
            text("SELECT beneficiary_name FROM transactions WHERE customer_id = :cid LIMIT 10"),
            {"cid": CUSTOMER_ID},
        ).all()

    print(f"\n--- VERIFICATION: {target} ({TARGET_ENV_KEYS[target]}) ---")
    print(f"beneficiary_names (profile_json): {list(beneficiary_names.values())[:5] if beneficiary_names else beneficiary_names}")
    print(f"category_stats.transfer: {transfer_stats}")
    print(f"transaction_count: {transaction_count}")
    print(f"transactions.beneficiary_name sample: {[t[0] for t in txn_names]}")

    return {
        "target": target,
        "beneficiary_names_sample": list(beneficiary_names.values())[:3] if beneficiary_names else [],
        "transfer_n": transfer_stats.get("n") if transfer_stats else None,
        "transaction_count": transaction_count,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["local", "neon"], required=True)
    parser.add_argument(
        "--keep-anomaly-model", action="store_true",
        help="Skip resetting anomaly_model_state — use when other real customers "
             "exist on this target and share the singleton model.",
    )
    args = parser.parse_args()

    reset_and_reseed(args.target, keep_anomaly_model=args.keep_anomaly_model)
    verify(args.target)

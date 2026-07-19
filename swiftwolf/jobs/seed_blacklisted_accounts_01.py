"""
jobs/seed_blacklisted_accounts.py — seeds blacklisted_accounts with realistic
demo entries covering every reason/source combination, then syncs the active
set to Redis. Unlike jobs/seed_customer_profile.py, this one DOES need Redis:
BlacklistService.get_active_keys() is Layer 1's real hot-path read for
POST /v1/score, so a Postgres-only seed here would leave that cache empty
until something else happened to populate it.

Usage:
    python jobs/seed_blacklisted_accounts.py
"""
import sys
from pathlib import Path

# Allow running this script directly (python jobs/seed_blacklisted_accounts.py)
# from any working directory — same sys.path fix as the other jobs/ scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select

from src.core.models import BlacklistedAccount
from src.core.services import BlacklistService


SEED_ENTRIES = [
    # (account, bank_code, reason, source, added_by, notes)

    # Scene 3's demo account — matches the existing tested blocked-transaction
    # scenario, so the demo shows a real, already-verified path
    ("0666666666", "000015", "confirmed_fraud", "layer2_confirmed_fraud", "system",
     "Flagged by anomaly model, confirmed via Part 2C rebase mechanism"),

    # NIBSS watchlist sync entries (mocked — see the CBN/NIBSS regulation
    # discussion in the Layer 1 doc for why this is account-level, not BVN)
    ("0777777777", "000013", "nibss_watchlist", "nibss_sync", "sync_job",
     "Synced from mocked NIBSS watchlist seed dataset"),
    ("0888888888", "000014", "nibss_watchlist", "nibss_sync", "sync_job", None),
    ("0999999999", "000016", "nibss_watchlist", "nibss_sync", "sync_job", None),

    # Analyst manual flags — the human-in-the-loop path
    ("0111222333", "000018", "manual_flag", "analyst", "gbenga",
     "Reported via customer complaint, pending further investigation"),
    ("0222333444", "000007", "manual_flag", "analyst", "gbenga", None),
    ("0333444555", "090267", "manual_flag", "analyst", "praise",
     "Multiple customers reported unauthorized transfers to this account"),

    # More confirmed-fraud entries, varied banks
    ("0444555666", "100004", "confirmed_fraud", "layer2_confirmed_fraud", "system", None),
    ("0555666777", "100033", "confirmed_fraud", "layer2_confirmed_fraud", "system", None),

    # One DEACTIVATED entry — proves the soft-delete / partial-index design
    # actually works end-to-end, not just that rows can be inserted
    ("0666777888", "000004", "manual_flag", "analyst", "gbenga",
     "False positive — cleared after investigation, kept for audit trail"),
]


def seed_blacklist(db_session, blacklist_service: BlacklistService):
    inserted = 0
    for account, bank_code, reason, source, added_by, notes in SEED_ENTRIES:
        existing = db_session.exec(
            select(BlacklistedAccount).where(
                BlacklistedAccount.beneficiary_account == account,
                BlacklistedAccount.beneficiary_bank_code == bank_code,
                BlacklistedAccount.is_active == True,
            )
        ).first()
        if existing:
            continue  # idempotent — safe to rerun this script

        entry = BlacklistedAccount(
            beneficiary_account=account,
            beneficiary_bank_code=bank_code,
            reason=reason,
            source=source,
            added_by=added_by,
            notes=notes,
        )
        db_session.add(entry)
        inserted += 1

    db_session.commit()

    # The one deliberately-deactivated entry, added THEN deactivated — proves
    # the soft-delete flow works, not just that the column exists
    cleared = db_session.exec(
        select(BlacklistedAccount).where(
            BlacklistedAccount.beneficiary_account == "0666777888"
        )
    ).first()
    if cleared and cleared.is_active:
        cleared.is_active = False
        db_session.commit()

    print(f"Inserted {inserted} new entries ({len(SEED_ENTRIES) - inserted} already existed)")

    # Rebuild the Redis Set from Postgres now that seeding (and the one
    # deactivation above) is done — otherwise Layer 1's hot-path read stays
    # empty/stale until something else happens to touch it.
    synced_keys = blacklist_service.sync_to_redis()
    print(f"Synced {len(synced_keys)} active composite keys to Redis ({BlacklistService.REDIS_KEY})")


if __name__ == "__main__":
    from src.core import models  # noqa: F401 — import registers tables on SQLModel.metadata
    from src.database import engine, create_db_and_tables  # reuse the app's own engine, don't build a second one
    from src.redis import get_redis_client
    from sqlmodel import Session

    create_db_and_tables()

    with Session(engine) as db:
        blacklist_service = BlacklistService(db, get_redis_client())
        seed_blacklist(db, blacklist_service)

# to run the script
# python jobs/seed_blacklisted_accounts_01.py
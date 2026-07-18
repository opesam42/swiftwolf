"""
jobs/seed_blacklisted_accounts.py — seeds blacklisted_accounts with realistic
demo entries covering every reason/source combination, then syncs the active
set to Redis. Unlike jobs/seed_customer_profile.py, this one DOES need Redis:
BlacklistService.get_active_keys() is Layer 1's real hot-path read for
POST /v1/score, so a Postgres-only seed here would leave that cache empty
until something else happened to populate it.

Upsert semantics: an existing (account, bank_code) match -- ACTIVE OR
INACTIVE -- is not skipped outright. If it's missing beneficiary_name (e.g.
seeded before that column existed, or seeded before it was deactivated),
the name gets filled in. Nothing else on an existing row is overwritten.
This lets a schema addition like beneficiary_name reach already-seeded rows,
including previously-deactivated ones, without a truncate+reseed.

Usage:
    python jobs/seed_blacklisted_accounts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select

from src.core.models import BlacklistedAccount
from src.core.services import BlacklistService


SEED_ENTRIES = [
    # (account, bank_code, beneficiary_name, reason, source, added_by, notes)

    # Scene 3's demo account -- matches the existing tested blocked-transaction
    # scenario, so the demo shows a real, already-verified path
    ("0666666666", "000015", "Chinedu Okoro", "confirmed_fraud",
     "layer2_confirmed_fraud", "system",
     "Flagged by anomaly model, confirmed via Part 2C rebase mechanism"),

    # NIBSS watchlist sync entries (mocked -- see the CBN/NIBSS regulation
    # discussion in the Layer 1 doc for why this is account-level, not BVN).
    # Named here for demo completeness -- a real NIBSS sync may only supply
    # account+bank_code with no name at all, so treat beneficiary_name as
    # genuinely optional in production, even though every demo row has one.
    ("0777777777", "000013", "Tunde Bakare", "nibss_watchlist", "nibss_sync", "sync_job",
     "Synced from mocked NIBSS watchlist seed dataset"),
    ("0888888888", "000014", "Amaka Eze", "nibss_watchlist", "nibss_sync", "sync_job", None),
    ("0999999999", "000016", "Yusuf Mohammed", "nibss_watchlist", "nibss_sync", "sync_job", None),

    # Analyst manual flags -- the human-in-the-loop path
    ("0111222333", "000018", "Blessing Nwachukwu", "manual_flag", "analyst", "gbenga",
     "Reported via customer complaint, pending further investigation"),
    ("0222333444", "000007", "Ibrahim Suleiman", "manual_flag", "analyst", "gbenga", None),
    ("0333444555", "090267", "Grace Adeyemi", "manual_flag", "analyst", "praise",
     "Multiple customers reported unauthorized transfers to this account"),

    # More confirmed-fraud entries, varied banks
    ("0444555666", "100004", "Emeka Obinna", "confirmed_fraud", "layer2_confirmed_fraud", "system", None),
    ("0555666777", "100033", "Fatima Bello", "confirmed_fraud", "layer2_confirmed_fraud", "system", None),

    # One DEACTIVATED entry -- proves the soft-delete / partial-index design
    # actually works end-to-end, not just that rows can be inserted
    ("0666777888", "000004", "Samuel Okafor", "manual_flag", "analyst", "gbenga",
     "False positive -- cleared after investigation, kept for audit trail"),
]


def seed_blacklist(db_session, blacklist_service: BlacklistService):
    inserted, updated = 0, 0
    for account, bank_code, name, reason, source, added_by, notes in SEED_ENTRIES:
        # NOTE: no is_active filter here, deliberately -- a row deactivated in
        # a prior run (like the demo's own cleared entry) must still be found
        # and backfilled, not silently skipped because it's inactive. If more
        # than one row ever exists for the same (account, bank_code) (e.g.
        # blacklisted, cleared, blacklisted again), the most recently added
        # one is the correct target for a name backfill.
        existing = db_session.exec(
            select(BlacklistedAccount)
            .where(
                BlacklistedAccount.beneficiary_account == account,
                BlacklistedAccount.beneficiary_bank_code == bank_code,
            )
            .order_by(BlacklistedAccount.added_at.desc())
        ).first()

        if existing:
            if not existing.beneficiary_name and name:
                existing.beneficiary_name = name
                db_session.add(existing)
                updated += 1
            continue

        entry = BlacklistedAccount(
            beneficiary_account=account,
            beneficiary_bank_code=bank_code,
            beneficiary_name=name,
            reason=reason,
            source=source,
            added_by=added_by,
            notes=notes,
        )
        db_session.add(entry)
        inserted += 1

    db_session.commit()

    cleared = db_session.exec(
        select(BlacklistedAccount).where(
            BlacklistedAccount.beneficiary_account == "0666777888"
        )
    ).first()
    if cleared and cleared.is_active:
        cleared.is_active = False
        db_session.commit()

    print(f"Inserted {inserted} new entries, updated {updated} existing entries with a name "
          f"({len(SEED_ENTRIES) - inserted - updated} already fully seeded)")

    synced_keys = blacklist_service.sync_to_redis()
    print(f"Synced {len(synced_keys)} active composite keys to Redis ({BlacklistService.REDIS_KEY})")


if __name__ == "__main__":
    from src.core import models  # noqa: F401
    from src.database import engine, create_db_and_tables
    from src.redis import get_redis_client
    from sqlmodel import Session

    create_db_and_tables()

    with Session(engine) as db:
        blacklist_service = BlacklistService(db, get_redis_client())
        seed_blacklist(db, blacklist_service)

# to run the script
# python jobs/seed_blacklisted_accounts_02.py
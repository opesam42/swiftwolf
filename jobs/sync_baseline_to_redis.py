"""
jobs/sync_baseline_to_redis.py — one-off tool to push a customer's CURRENT
Postgres-side CustomerProfile into Redis. Exists independently of any
particular seeding run: this is for whenever a customer's Redis baseline
cache is missing or stale relative to Postgres (the actual source of truth),
regardless of how it got that way.

Usage:
    python jobs/sync_baseline_to_redis.py <customer_id>
"""
import sys
from pathlib import Path

# Allow running this script directly (python jobs/sync_baseline_to_redis.py ...)
# from any working directory — same sys.path fix as the other jobs/ scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def sync_baseline(customer_id: str, db, redis_client) -> None:
    from src.core.services import CustomerProfileService

    # redis_client attached here, unlike the offline seeding script, which
    # deliberately omits it (seeding has no live traffic to serve) — this
    # tool's entire purpose IS the Redis write, so it must be present.
    service = CustomerProfileService(db, redis_client)

    profile = service.get_or_create(customer_id)  # reads whatever is currently correct in Postgres
    service.save(customer_id, profile)  # writes Postgres (redundant, harmless) AND Redis (the actual goal)

    baseline = profile.to_baseline_dict()
    print(f"Synced baseline:{customer_id} to Redis")
    print(f"  transaction_count: {profile.transaction_count}")
    print(f"  is_cold_start: {baseline['is_cold_start']}")
    print(f"  known_beneficiaries: {len(baseline['known_beneficiaries'])}")
    print(f"  category_baselines: {list(baseline['category_baselines'].keys())}")
    print(f"  risk_tier: {baseline['risk_tier']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python jobs/sync_baseline_to_redis.py <customer_id>")
        sys.exit(1)

    from sqlmodel import Session

    from src.database import engine
    from src.redis import get_redis_client

    with Session(engine) as db:
        sync_baseline(sys.argv[1], db, get_redis_client())
